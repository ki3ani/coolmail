from dotenv import load_dotenv
import os
import time  # For implementing retry logic
from nylas import Client
from flask import Flask, request, redirect, url_for, session, jsonify
from flask_session import Session
from nylas.models.auth import URLForAuthenticationConfig, CodeExchangeRequest
from openai import OpenAI

load_dotenv()

# Initialize OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

app = Flask(__name__)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

# Initialize Nylas client
nylas = Client(
    api_key=os.environ.get("NYLAS_API_KEY"),
    api_uri=os.environ.get("NYLAS_API_URI"),
)

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


@app.route("/nylas/auth", methods=["GET"])
def login():
    if session.get("grant_id") is None:
        config = URLForAuthenticationConfig({
            "client_id": os.environ.get("NYLAS_CLIENT_ID"),
            "redirect_uri": "http://127.0.0.1:5000/oauth/exchange",
            "provider": "google"
        })
        url = nylas.auth.url_for_oauth2(config)
        return redirect(url)
    else:
        return f'Grant ID: {session["grant_id"]}'


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
            return f"Error during code exchange: {str(e)}", 400
    return redirect(url_for("login"))


@app.route("/nylas/recent-emails", methods=["GET"])
def recent_emails():
    query_params = {"limit": 5}
    try:
        messages, _, _ = nylas_retry(nylas.messages.list, session["grant_id"], query_params)
        return jsonify(messages)
    except Exception as e:
        return f'Error fetching recent emails: {str(e)}', 400


@app.route("/nylas/send-email", methods=["GET"])
def send_email():
    try:
        body = {
            "subject": "Your Subject Here",
            "body": "Your Email Here",
            "reply_to": [{"name": "Name", "email": os.environ.get("EMAIL")}],
            "to": [{"name": "Name", "email": os.environ.get("EMAIL")}]
        }
        message = nylas_retry(nylas.messages.send, session["grant_id"], request_body=body).data
        return jsonify(message)
    except Exception as e:
        return f'Error sending email: {str(e)}', 400


@app.route("/test-nylas", methods=["GET"])
def test_nylas():
    try:
        config = URLForAuthenticationConfig({
            "client_id": os.environ.get("NYLAS_CLIENT_ID"),
            "redirect_uri": "http://127.0.0.1:5000/oauth/exchange"
        })
        url = nylas.auth.url_for_oauth2(config)
        return jsonify({
            "status": "success",
            "message": "Nylas client is working correctly",
            "generated_url": url
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "type": type(e).__name__
        }), 500


def categorize_email(email_subject, email_body):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Update with the model you're using
            messages=[
                {"role": "system", "content": "You are an email categorization assistant. Categorize emails into specific, concise categories that can be used for grouping. Use categories like 'Work', 'Personal', 'Finance', 'Travel', 'Shopping', 'Social', 'News', 'Marketing', 'Education', etc. If none of these fit, create a suitable category name. Provide only the category name, nothing else."},
                {"role": "user", "content": f"Categorize this email based on the subject and body. Subject: {email_subject}. Body: {email_body}"}
            ],
            temperature=0.3  # Slightly increased for more varied responses
        )
        category = response.choices[0].message.content.strip()
        return category
    except Exception as e:
        return f"Error categorizing email: {str(e)}"

@app.route("/nylas/categorize-emails", methods=["GET"])
def categorize_emails():
    try:
        messages, _, _ = nylas_retry(nylas.messages.list, session["grant_id"], {"limit": 10})  # Increased to 10 emails
        categorized_emails = []
        for message in messages:
            email_subject = message.subject
            email_body = message.body
            category = categorize_email(email_subject, email_body)
            categorized_emails.append({
                "subject": email_subject,
                "category": category,
                "snippet": message.snippet  # Adding a snippet for context
            })
        return jsonify(categorized_emails)
    except Exception as e:
        return f'Error fetching and categorizing emails: {str(e)}', 400

def generate_response(email_body, tone="professional"):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Ensure this model is available and correct
            messages=[
                {"role": "system", "content": f"You are an assistant that generates {tone} email responses."},
                {
                    "role": "user",
                    "content": f"Write a {tone} response to this email: {email_body}"
                }
            ],
            max_tokens=150
        )
        suggested_response = response.choices[0].message.content.strip()
        return suggested_response
    except Exception as e:
        return f"Error generating response: {str(e)}"

@app.route("/nylas/suggest-response", methods=["POST"])
def suggest_response():
    email_body = request.json.get("email_body")
    tone = request.json.get("tone", "professional")
    suggested_response = generate_response(email_body, tone)
    return jsonify({
        "suggested_response": suggested_response,
        "original_email": email_body
    })

@app.route("/nylas/refine-response", methods=["POST"])
def refine_response():
    original_email = request.json.get("original_email")
    current_response = request.json.get("current_response")
    refinement_instructions = request.json.get("refinement_instructions")
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an assistant that refines email responses based on user instructions."},
                {"role": "user", "content": f"Original email: {original_email}"},
                {"role": "assistant", "content": f"Current response: {current_response}"},
                {"role": "user", "content": f"Please refine the response according to these instructions: {refinement_instructions}"}
            ],
            max_tokens=200
        )
        refined_response = response.choices[0].message.content.strip()
        return jsonify({"refined_response": refined_response})
    except Exception as e:
        return jsonify({"error": f"Error refining response: {str(e)}"}), 400

@app.route("/nylas/send-refined-email", methods=["POST"])
def send_refined_email():
    final_response = request.json.get("final_response")
    recipient_email = request.json.get("recipient_email")
    subject = request.json.get("subject", "Re: Your Email")

    try:
        body = {
            "subject": subject,
            "body": final_response,
            "to": [{"email": recipient_email}]
        }
        message = nylas_retry(nylas.messages.send, session["grant_id"], request_body=body).data
        return jsonify({"status": "success", "message": "Email sent successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error sending email: {str(e)}"}), 400


def generate_marketing_email(advertisement_prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Ensure this model is available and correct
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": f"Create a marketing email based on the following input: {advertisement_prompt}"
                }
            ],
            max_tokens=300
        )
        email_content = response.choices[0].message['content'].strip()
        return email_content
    except Exception as e:
        return f"Error generating marketing email: {str(e)}"


@app.route("/nylas/send-bulk-email", methods=["POST"])
def send_bulk_email():
    prompt = request.json.get("prompt")
    subject = request.json.get("subject")
    email_content = generate_marketing_email(prompt)

    # You would have your own logic to fetch the recipients for bulk email
    recipients = [{"name": "Recipient Name", "email": "recipient@example.com"}]  # Replace with real data

    errors = []
    for recipient in recipients:
        try:
            body = {
                "subject": subject,
                "body": email_content,
                "reply_to": [{"name": "Your Name", "email": os.environ.get("EMAIL")}],
                "to": [recipient]
            }
            message = nylas_retry(nylas.messages.send, session["grant_id"], request_body=body).data
        except Exception as e:
            errors.append(str(e))

    if errors:
        return jsonify({"status": "error", "errors": errors}), 400

    return jsonify({"status": "success", "message": "Bulk emails sent successfully"})


if __name__ == "__main__":
    app.run(debug=True)
