from flask import Flask, request, redirect, url_for, session, jsonify, render_template, flash
from flask_session import Session
from dotenv import load_dotenv
import os
import csv
from io import TextIOWrapper
import time
from nylas import Client
from nylas.models.auth import URLForAuthenticationConfig, CodeExchangeRequest
from openai import OpenAI
from datetime import datetime, timedelta
from models import db, Recipient, Campaign
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger




load_dotenv()

# Initialize OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///email_assistant.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)
db.init_app(app)


# Initialize Nylas client
nylas = Client(
    api_key=os.environ.get("NYLAS_API_KEY"),
    api_uri=os.environ.get("NYLAS_API_URI"),
)



@app.route("/nylas/manage-recepients", methods=["GET", "POST"])
def manage_recipients():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        if name and email:
            existing_recipient = Recipient.query.filter_by(email=email).first()
            if existing_recipient:
                existing_recipient.name = name
                flash("Recipient updated successfully", "success")
            else:
                new_recipient = Recipient(name=name, email=email)
                db.session.add(new_recipient)
                flash("Recipient added successfully", "success")
            db.session.commit()
        else:
            flash("Name and email are required", "error")
    recipients = Recipient.query.all()
    return render_template("manage-recepients.html", recipients=recipients)

@app.route("/nylas/import-csv", methods=["POST"])
def import_csv():
    if 'file' not in request.files:
        flash('No file part', 'error')
        return redirect(url_for('manage_recipients'))
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'error')
        return redirect(url_for('manage_recipients'))
    if file and file.filename.endswith('.csv'):
        csv_file = TextIOWrapper(file, encoding='utf-8')
        csv_reader = csv.DictReader(csv_file)
        added_count = 0
        for row in csv_reader:
            name = row.get('name', '').strip()
            email = row.get('email', '').strip()
            if name and email:
                existing_recipient = Recipient.query.filter_by(email=email).first()
                if existing_recipient:
                    existing_recipient.name = name
                else:
                    new_recipient = Recipient(name=name, email=email)
                    db.session.add(new_recipient)
                    added_count += 1
        db.session.commit()
        flash(f'CSV imported successfully. Added {added_count} recipients.', 'success')
    else:
        flash('Invalid file type. Please upload a CSV file.', 'error')
    return redirect(url_for('manage_recipients'))

@app.route("/nylas/import-contacts", methods=["POST"])
def import_contacts():
    if 'grant_id' not in session:
        flash("Please authenticate with Nylas first.", "warning")
        return redirect(url_for('login'))
    try:
        contacts, _, _ = nylas.contacts.list(session["grant_id"])
        for contact in contacts:
            if contact.emails:
                for email in contact.emails:
                    name = f"{contact.given_name} {contact.surname}".strip() or email
                    existing_recipient = Recipient.query.filter_by(email=email).first()
                    if existing_recipient:
                        existing_recipient.name = name
                    else:
                        new_recipient = Recipient(name=name, email=email)
                        db.session.add(new_recipient)
        db.session.commit()
        flash("Contacts imported successfully", "success")
    except Exception as e:
        flash(f"Error importing contacts: {str(e)}", "error")
    return redirect(url_for('manage_recipients'))


def generate_email_content_bulk(prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an email marketing assistant."},
                {"role": "user", "content": f"Create an email for {prompt}"}
            ],
            max_tokens=300,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error generating email content: {str(e)}"

@app.route("/nylas/create-campaign", methods=["GET", "POST"])
def create_campaign():
    if request.method == "POST":
        name = request.form.get("name")
        subject = request.form.get("subject")
        prompt = request.form.get("prompt")
        schedule_type = request.form.get("schedule_type")
        schedule_days = request.form.get("schedule_days", type=int)
        
        if name and subject and prompt:
            body = generate_email_content_bulk(prompt)
            campaign = Campaign(name=name, subject=subject, body=body)
            
            if schedule_type == "once":
                campaign.scheduled_at = datetime.utcnow() + timedelta(days=schedule_days)
                campaign.status = 'scheduled'
            elif schedule_type == "recurring":
                # For recurring campaigns, we'll need to implement a more complex scheduling system
                # For now, let's just set it as 'recurring' in the status
                campaign.status = 'recurring'
            
            db.session.add(campaign)
            db.session.commit()
            flash("Campaign created successfully", "success")
            return redirect(url_for('view_campaigns'))
        else:
            flash("All fields are required", "error")
    return render_template("create-campaign.html")

@app.route("/nylas/view-campaigns")
def view_campaigns():
    campaigns = Campaign.query.all()
    return render_template("view-campaigns.html", campaigns=campaigns)


scheduler = BackgroundScheduler()
scheduler.start()

def send_campaign_emails(campaign_id):
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if campaign:
            recipients = Recipient.query.all()
            for recipient in recipients:
                try:
                    body = {
                        "subject": campaign.subject,
                        "body": campaign.body,
                        "to": [{"name": recipient.name, "email": recipient.email}]
                    }
                    nylas.messages.send(session["grant_id"], request_body=body)
                except Exception as e:
                    print(f"Error sending email to {recipient.email}: {str(e)}")
            if campaign.status == 'scheduled':
                campaign.status = 'sent'
                db.session.commit()

@app.route("/nylas/schedule-campaign/<int:campaign_id>", methods=["POST"])
def schedule_campaign(campaign_id):
    campaign = Campaign.query.get(campaign_id)
    if campaign:
        if campaign.status == 'scheduled':
            scheduler.add_job(
                send_campaign_emails, 
                'date', 
                run_date=campaign.scheduled_at, 
                args=[campaign_id]
            )
        elif campaign.status == 'recurring':
            scheduler.add_job(
                send_campaign_emails,
                CronTrigger(day_of_week='mon'),  # This schedules it for every Monday
                args=[campaign_id]
            )
        flash("Campaign scheduled successfully", "success")
    else:
        flash("Invalid campaign", "error")
    return redirect(url_for('view_campaigns'))

# Function to handle retries for Nylas API calls
def nylas_retry(func, *args, max_retries=3, backoff_factor=2, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)  # Exponential backoff
            else:
                raise e

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/nylas/auth", methods=["GET"])
def login():
    if session.get("grant_id") is None:
        config = URLForAuthenticationConfig({
            "client_id": os.environ.get("NYLAS_CLIENT_ID"),
            "redirect_uri": "http://127.0.0.1:5000/oauth/exchange",
            "provider": "google"
        })
        url = nylas.auth.url_for_oauth2(config)
        return render_template("auth.html", auth_url=url)
    else:
        return render_template("auth.html", grant_id=session["grant_id"])

@app.route("/oauth/exchange", methods=["GET"])
def authorized():
    if session.get("grant_id") is None:
        code = request.args.get("code")
        exchangeRequest = CodeExchangeRequest({
            "redirect_uri": "http://127.0.0.1:5000/oauth/exchange",
            "code": code,
            "client_id": os.environ.get("NYLAS_CLIENT_ID"),
            "client_secret": os.environ.get("NYLAS_API_KEY")
        })
        try:
            exchange = nylas.auth.exchange_code_for_token(exchangeRequest)
            session["grant_id"] = exchange.grant_id
            return redirect(url_for("login"))
        except Exception as e:
            return render_template("auth.html", error=str(e))
    return redirect(url_for("login"))

@app.route("/nylas/recent-emails", methods=["GET"])
def recent_emails():
    query_params = {"limit": 50}
    try:
        messages, _, _ = nylas_retry(nylas.messages.list, session["grant_id"], query_params)
        return render_template("recent-emails.html", messages=messages)
    except Exception as e:
        return render_template("recent-emails.html", error=str(e))
    

@app.route("/nylas/email/<message_id>", methods=["GET", "POST"])
def view_email(message_id):
    try:
        # Unpack the tuple returned by find
        message, request_id = nylas_retry(nylas.messages.find, session["grant_id"], message_id)
        
        # Print message structure for debugging
        print("Message structure:", message)
        
        if request.method == "POST":
            generated_response = generate_response(message.body)
            return render_template("view-email.html", message=message, generated_response=generated_response)
        
        return render_template("view-email.html", message=message)
    except Exception as e:
        print("Error in view_email:", str(e))  # Print the error for debugging
        return render_template("view-email.html", error=str(e))
    
@app.route("/nylas/email/<message_id>/refine", methods=["POST"])
def refine_response(message_id):
    try:
        message, _ = nylas_retry(nylas.messages.find, session["grant_id"], message_id)
        current_response = request.form.get("response")
        refinement_instructions = "Make the response more concise and professional."
        refined_response = generate_refined_response(message.body, current_response, refinement_instructions)
        return render_template("view-email.html", message=message, generated_response=refined_response, refined=True)
    except Exception as e:
        return render_template("view-email.html", error=str(e))
    
    
@app.route("/nylas/email/<message_id>/send", methods=["POST"])
def send_response(message_id):
    try:
        original_message, _ = nylas_retry(nylas.messages.find, session["grant_id"], message_id)
        response_body = request.form.get("response")
        
        # Get the first 'from' email address if available
        reply_to = original_message.from_[0].email if original_message.from_ else None
        
        if not reply_to:
            flash("Could not determine recipient email address.", "error")
            return redirect(url_for('view_email', message_id=message_id))
        
        body = {
            "subject": f"Re: {original_message.subject}",
            "body": response_body,
            "to": [{"email": reply_to}]
        }
        
        nylas_retry(nylas.messages.send, session["grant_id"], request_body=body)
        
        flash("Response sent successfully!", "success")
        return redirect(url_for('recent_emails'))
    except Exception as e:
        flash(f"Error sending response: {str(e)}", "error")
        return redirect(url_for('view_email', message_id=message_id))
    

@app.route("/nylas/send-email", methods=["GET", "POST"])
def send_email():
    if request.method == "POST":
        action = request.form.get("action")
        subject = request.form.get("subject")
        body = request.form.get("body")
        to = request.form.get("to")
        schedule_time = request.form.get("schedule_time")

        email_data = {
            "subject": subject,
            "body": body,
            "to": [{"email": to}]
        }

        if action == "draft":
            try:
                draft = nylas_retry(nylas.drafts.create, session["grant_id"], request_body=email_data)
                flash("Email saved as draft", "success")
            except Exception as e:
                flash(f"Error saving draft: {str(e)}", "error")
        elif action == "send" or action == "schedule":
            try:
                if action == "schedule" and schedule_time:
                    schedule_date = datetime.strptime(schedule_time, "%Y-%m-%dT%H:%M")
                    email_data["send_at"] = int(schedule_date.timestamp())
                
                message = nylas_retry(nylas.messages.send, session["grant_id"], request_body=email_data)
                flash("Email scheduled successfully" if action == "schedule" else "Email sent successfully", "success")
            except Exception as e:
                flash(f"Error sending email: {str(e)}", "error")

        return redirect(url_for("send_email"))

    return render_template("send-email.html")

@app.route("/nylas/generate-email", methods=["POST"])
def generate_email():
    prompt = request.form.get("prompt")
    try:
        generated_email = generate_email_content(prompt)
        return jsonify({"success": True, "email": generated_email})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

def generate_email_content(prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an AI assistant that helps draft professional emails."},
                {"role": "user", "content": f"Draft an email based on this prompt: {prompt}"}
            ],
            max_tokens=300,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise Exception(f"Error generating email content: {str(e)}")

@app.route("/nylas/categorize-emails", methods=["GET"])
def categorize_emails():
    try:
        messages, _, _ = nylas_retry(nylas.messages.list, session["grant_id"], {"limit": 100})
        categorized_emails = {}
        for message in messages:
            category = categorize_email(message.subject, message.snippet)
            if category not in categorized_emails:
                categorized_emails[category] = []
            categorized_emails[category].append({
                "id": message.id,
                "subject": message.subject,
                "snippet": message.snippet
            })
        
        # Sort categories by number of emails (descending)
        sorted_categories = sorted(categorized_emails.items(), key=lambda x: len(x[1]), reverse=True)
        
        return render_template("categorize-email.html", categorized_emails=sorted_categories)
    except Exception as e:
        return render_template("categorize-email.html", error=str(e))
    


@app.route("/nylas/send-bulk-email", methods=["GET", "POST"])
def send_bulk_email():
    if request.method == "POST":
        prompt = request.form.get("prompt")
        subject = request.form.get("subject")
        try:
            email_content = generate_marketing_email(prompt)
            # You would have your own logic to fetch the recipients for bulk email
            recipients = [{"name": "Recipient Name", "email": "recipient@example.com"}]  # Replace with real data
            errors = []
            for recipient in recipients:
                try:
                    body = {
                        "subject": subject,
                        "body": email_content,
                        "to": [recipient]
                    }
                    nylas_retry(nylas.messages.send, session["grant_id"], request_body=body).data
                except Exception as e:
                    errors.append(str(e))
            if errors:
                return render_template("send_bulk_email.html", status="error", errors=errors)
            return render_template("send_bulk_email.html", status="success", message="Bulk emails sent successfully")
        except Exception as e:
            return render_template("send_bulk_email.html", status="error", errors=[str(e)])
    return render_template("send_bulk_email.html")

# Helper functions (make sure these are defined)
def categorize_email(email_subject, email_body):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an email categorization assistant. Categorize emails into specific, concise categories that can be used for grouping. Use categories like 'Work', 'Personal', 'Finance', 'Travel', 'Shopping', 'Social', 'News', 'Marketing', 'Education', etc. If none of these fit, create a suitable category name. Provide only the category name, nothing else."},
                {"role": "user", "content": f"Categorize this email based on the subject and body. Subject: {email_subject}. Body: {email_body}"}
            ],
            temperature=0.3
        )
        category = response.choices[0].message.content.strip()
        return category
    except Exception as e:
        return f"Error categorizing email: {str(e)}"

def generate_response(email_body, tone="professional"):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"You are an assistant that generates {tone} email responses."},
                {"role": "user", "content": f"Write a {tone} response to this email: {email_body}"}
            ],
            max_tokens=150,
            temperature=0.7
        )
        suggested_response = response.choices[0].message.content.strip()
        return suggested_response
    except Exception as e:
        return f"Error generating response: {str(e)}"

def generate_refined_response(original_email, current_response, refinement_instructions):
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",  # or "gpt-4" if available
            messages=[
                {"role": "system", "content": "You are an assistant that refines email responses based on user instructions."},
                {"role": "user", "content": f"Original email: {original_email}"},
                {"role": "assistant", "content": f"Current response: {current_response}"},
                {"role": "user", "content": f"Please refine the response according to these instructions: {refinement_instructions}"}
            ],
            max_tokens=200,
            temperature=0.7
        )
        refined_response = response.choices[0].message.content.strip()
        return refined_response
    except Exception as e:
        return f"Error refining response: {str(e)}"

def generate_marketing_email(advertisement_prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a marketing email generator. Create compelling and engaging marketing emails based on the given prompt."},
                {"role": "user", "content": f"Create a marketing email based on the following input: {advertisement_prompt}"}
            ],
            max_tokens=300,
            temperature=0.8
        )
        email_content = response.choices[0].message.content.strip()
        return email_content
    except Exception as e:
        return f"Error generating marketing email: {str(e)}"
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)