#!/usr/bin/env python3
"""
Aegis - Complete Market Ready Backend
Professional Dark Theme, Error Messages, Social Login UI
"""

import os
import logging
import sqlite3
import hmac
import secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, url_for, session, flash, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import stripe

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Import existing backend modules
from github_client import get_pr_diff, post_comment
from pr_analyzer import analyze_pr_diff, extract_changed_functions
from code_scanner import ask_question_about_code

# ============================================================
# 1. LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# 2. FLASK APP CONFIG (Production-Ready)
# ============================================================
app = Flask(__name__)

SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    logger.critical("🚨 SECRET_KEY environment variable is NOT SET! Session cookies will be invalid on restart!")
    SECRET_KEY = secrets.token_hex(32)
    logger.warning("⚠️ Using temporary SECRET_KEY. Sessions will break on restart!")

app.secret_key = SECRET_KEY
logger.info("🔐 SECRET_KEY is loaded from environment.")

app.config['SESSION_COOKIE_NAME'] = 'aegis_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_DOMAIN'] = None
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

# ============================================================
# 3. STRIPE CONFIG
# ============================================================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
YOUR_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "https://test-qa-bot-production.up.railway.app")

# ============================================================
# 4. GITHUB OAUTH CONFIG
# ============================================================
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_OAUTH_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# ============================================================
# 5. DATABASE SETUP (SQLite)
# ============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), 'aegis.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            provider TEXT DEFAULT 'deepseek',
            api_key TEXT,
            repo_name TEXT,
            github_token TEXT,
            stripe_customer_id TEXT,
            subscription_id TEXT,
            subscription_status TEXT DEFAULT 'inactive',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pr_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            pr_number INTEGER,
            repo_name TEXT,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized.")

init_db()

# ============================================================
# 6. DATABASE HELPERS
# ============================================================
def get_user(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()
    return user

def get_user_by_id(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def update_user_settings(user_id, provider=None, api_key=None, repo_name=None, github_token=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE users 
        SET provider = COALESCE(?, provider),
            api_key = COALESCE(?, api_key),
            repo_name = COALESCE(?, repo_name),
            github_token = COALESCE(?, github_token)
        WHERE id = ?
    ''', (provider, api_key, repo_name, github_token, user_id))
    conn.commit()
    conn.close()

def update_subscription(user_id, customer_id, subscription_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE users 
        SET stripe_customer_id = ?, subscription_id = ?, subscription_status = ?
        WHERE id = ?
    ''', (customer_id, subscription_id, status, user_id))
    conn.commit()
    conn.close()

def get_user_by_github_repo(repo_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE repo_name = ? ORDER BY id LIMIT 1', (repo_name,))
    user = c.fetchone()
    conn.close()
    return user

# ============================================================
# 7. WEBHOOK
# ============================================================
GITHUB_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

def verify_signature(payload_body, signature_header):
    if not signature_header or not GITHUB_SECRET:
        return False
    hash_object = hmac.new(GITHUB_SECRET.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected, signature_header)

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "message": "Aegis is running"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(request.data, signature):
        logger.warning("Invalid signature")
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event")
    payload = request.get_json()
    repo_name = payload.get("repository", {}).get("full_name")

    if event == "ping":
        return jsonify({"msg": "pong"}), 200

    user = get_user_by_github_repo(repo_name)
    if not user:
        logger.warning(f"No user found for repo {repo_name}, falling back to env tokens.")
        user = None

    # --- Chatbot ---
    if event == "issue_comment":
        issue = payload.get("issue", {})
        comment_body = payload.get("comment", {}).get("body", "")
        pr_number = issue.get("number")
        if issue.get("pull_request") and comment_body.strip().startswith("/ask"):
            logger.info(f"💬 Chatbot triggered on PR #{pr_number} in {repo_name}")
            question = comment_body.replace("/ask", "").strip() or "What does this code do?"
            try:
                if user:
                    os.environ['LLM_PROVIDER'] = user[3]
                    os.environ['OPENAI_API_KEY'] = user[4] or ""
                    os.environ['GITHUB_TOKEN'] = user[6] or os.getenv("GITHUB_TOKEN")
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                answer = ask_question_about_code(question, diff_content)
                post_comment(repo, pr_number, f"🤖 **AI Chatbot:**\n\n{answer}")
                logger.info(f"✅ Chatbot replied to PR #{pr_number}")
                return jsonify({"msg": "Chatbot replied"}), 200
            except Exception as e:
                logger.error(f"❌ Chatbot Error: {e}")
                return jsonify({"error": str(e)}), 500
        return jsonify({"msg": "Ignored comment"}), 200

    # --- Pull Request ---
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize"]:
        pr_number = payload["number"]
        logger.info(f"🔍 Processing PR #{pr_number} in {repo_name}")
        try:
            if user:
                os.environ['LLM_PROVIDER'] = user[3]
                os.environ['OPENAI_API_KEY'] = user[4] or ""
                os.environ['GITHUB_TOKEN'] = user[6] or os.getenv("GITHUB_TOKEN")
            diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
            api_key = user[4] if user else None
            use_mock = not api_key
            result = analyze_pr_diff(diff_content, use_mock=use_mock)
            status = "✅ PASSED" if result['success'] else "❌ FAILED (Needs Review)"
            comment = f"""🤖 **AI QA Report for PR #{pr_number}**

**Status:** {status}

**Functions Detected:**
{extract_changed_functions(diff_content)[:500]}...

**AI Suggested Fix (Diff):**
{result['diff_output'][:1500]}

🔔 *This is an automated analysis. Please review the suggested changes.*
"""
            post_comment(repo, pr_number, comment)
            logger.info(f"✅ Comment posted to PR #{pr_number}")
            return jsonify({"msg": "PR processed"}), 200
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"msg": "Ignored"}), 200

# ============================================================
# 8. FRONTEND ROUTES (Professional Dark Theme)
# ============================================================
def load_html(filename):
    path = os.path.join(os.path.dirname(__file__), 'frontend', filename)
    with open(path, 'r') as f:
        return f.read()

@app.route("/")
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    try:
        return load_html('index.html')
    except:
        return "Landing page not found.", 404

@app.route("/signup", methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if not username or not password:
            flash('Username and password required')
            return redirect(url_for('signup'))
        password_hash = generate_password_hash(password)
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
            conn.commit()
            conn.close()
            flash('Account created! Please log in.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists.')
            return redirect(url_for('signup'))
    # Professional signup page with social login UI
    return '''
        <!DOCTYPE html>
        <html>
        <head><title>Aegis - Sign Up</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body { background: #000000; }
            .card { background: #0a0a0a; border: 1px solid #1a1a1a; }
            .input-dark { background: #000000; border: 1px solid #1a1a1a; color: #e5e7eb; padding: 0.75rem 1rem; border-radius: 0.5rem; width: 100%; }
            .input-dark:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
            .btn-primary { background: #3b82f6; color: white; font-weight: 600; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; }
            .btn-primary:hover { background: #2563eb; }
            .btn-social { background: #1a1a1a; border: 1px solid #2a2a2a; color: white; font-weight: 500; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; display: block; text-align: center; }
            .btn-social:hover { background: #2a2a2a; border-color: #3b82f6; }
        </style>
        </head>
        <body class="min-h-screen flex items-center justify-center">
            <div class="card p-8 rounded-2xl max-w-md w-full">
                <h1 class="text-2xl font-bold mb-6 text-white">Create Account</h1>
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" class="input-dark mb-4" />
                    <input type="password" name="password" placeholder="Password" class="input-dark mb-4" />
                    <button type="submit" class="btn-primary">Sign Up</button>
                </form>
                <div class="relative my-6">
                    <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-[#1a1a1a]"></div></div>
                    <div class="relative flex justify-center text-xs"><span class="bg-[#0a0a0a] px-2 text-[#4b5563]">OR</span></div>
                </div>
                <div class="space-y-3">
                    <a href="#" class="btn-social">🔵 Sign up with Google</a>
                    <a href="#" class="btn-social">⚫ Sign up with GitHub</a>
                </div>
                <p class="text-sm text-[#4b5563] mt-4">Already have an account? <a href="/login" class="text-[#3b82f6] hover:underline">Log in</a></p>
            </div>
        </body>
        </html>
    '''

@app.route("/login", methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = get_user(username)
        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['username'] = user[1]
            session.permanent = True
            flash('Logged in successfully.')
            return redirect(url_for('dashboard'))
        else:
            error = 'Invalid username or password. Please try again.'
    # Professional login page with error message and "No account?" link
    error_html = f'<p class="text-red-400 text-sm mb-4">{error}</p>' if error else ''
    return f'''
        <!DOCTYPE html>
        <html>
        <head><title>Aegis - Log In</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body {{ background: #000000; }}
            .card {{ background: #0a0a0a; border: 1px solid #1a1a1a; }}
            .input-dark {{ background: #000000; border: 1px solid #1a1a1a; color: #e5e7eb; padding: 0.75rem 1rem; border-radius: 0.5rem; width: 100%; }}
            .input-dark:focus {{ outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }}
            .btn-primary {{ background: #3b82f6; color: white; font-weight: 600; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; }}
            .btn-primary:hover {{ background: #2563eb; }}
            .btn-social {{ background: #1a1a1a; border: 1px solid #2a2a2a; color: white; font-weight: 500; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; display: block; text-align: center; }}
            .btn-social:hover {{ background: #2a2a2a; border-color: #3b82f6; }}
        </style>
        </head>
        <body class="min-h-screen flex items-center justify-center">
            <div class="card p-8 rounded-2xl max-w-md w-full">
                <h1 class="text-2xl font-bold mb-6 text-white">Log In</h1>
                {error_html}
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" class="input-dark mb-4" />
                    <input type="password" name="password" placeholder="Password" class="input-dark mb-4" />
                    <button type="submit" class="btn-primary">Log In</button>
                </form>
                <div class="relative my-6">
                    <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-[#1a1a1a]"></div></div>
                    <div class="relative flex justify-center text-xs"><span class="bg-[#0a0a0a] px-2 text-[#4b5563]">OR</span></div>
                </div>
                <div class="space-y-3">
                    <a href="#" class="btn-social">🔵 Sign in with Google</a>
                    <a href="#" class="btn-social">⚫ Sign in with GitHub</a>
                </div>
                <p class="text-sm text-[#4b5563] mt-4">No account? <a href="/signup" class="text-[#3b82f6] hover:underline">Create one</a></p>
            </div>
        </body>
        </html>
    '''

@app.route("/logout")
def logout():
    session.clear()
    flash('Logged out.')
    return redirect(url_for('home'))

# ============================================================
# 9. GITHUB OAUTH ROUTES
# ============================================================
@app.route("/github-oauth/authorize")
def github_oauth_authorize():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    redirect_uri = f"{YOUR_DOMAIN}/github-oauth/callback"
    return redirect(f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={redirect_uri}&scope=repo")

@app.route("/github-oauth/callback")
def github_oauth_callback():
    code = request.args.get('code')
    if not code:
        flash('Authorization failed.')
        return redirect(url_for('dashboard'))

    resp = requests.post(
        GITHUB_OAUTH_URL,
        headers={'Accept': 'application/json'},
        data={'client_id': GITHUB_CLIENT_ID, 'client_secret': GITHUB_CLIENT_SECRET, 'code': code}
    )
    data = resp.json()
    if 'access_token' not in data:
        flash('Failed to get access token.')
        return redirect(url_for('dashboard'))

    access_token = data['access_token']
    user_id = session['user_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE users SET github_token = ? WHERE id = ?', (access_token, user_id))
    conn.commit()
    conn.close()

    flash('GitHub account connected successfully!')
    return redirect(url_for('dashboard'))

# ============================================================
# 10. STRIPE BILLING ROUTES
# ============================================================
@app.route("/create-checkout-session", methods=['POST'])
def create_checkout_session():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    PRICE_IDS = {
        'monthly': os.getenv("STRIPE_PRICE_MONTHLY"),
    }
    price_id = PRICE_IDS.get('monthly')
    if not price_id:
        return jsonify({"error": "Stripe price ID not configured"}), 500

    try:
        checkout_session = stripe.checkout.Session.create(
            customer=user[7] or None,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=f"{YOUR_DOMAIN}/dashboard?success=true",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?canceled=true",
            metadata={'user_id': user[0]}
        )
        return jsonify({'url': checkout_session.url})
    except Exception as e:
        logger.error(f"Stripe error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/stripe-webhook", methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400

    data_object = event['data']['object']
    
    if event['type'] == 'checkout.session.completed':
        session_data = data_object
        customer_id = session_data['customer']
        subscription_id = session_data['subscription']
        user_id = int(session_data['metadata']['user_id'])
        sub = stripe.Subscription.retrieve(subscription_id)
        status = sub.status
        update_subscription(user_id, customer_id, subscription_id, status)
        logger.info(f"Subscription {subscription_id} activated for user {user_id}")

    elif event['type'] == 'customer.subscription.updated':
        subscription = data_object
        customer_id = subscription['customer']
        status = subscription['status']
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE users SET subscription_status = ? WHERE stripe_customer_id = ?', (status, customer_id))
        conn.commit()
        conn.close()
        logger.info(f"Subscription {subscription['id']} updated to {status}")

    elif event['type'] == 'customer.subscription.deleted':
        subscription = data_object
        customer_id = subscription['customer']
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE users SET subscription_status = "canceled" WHERE stripe_customer_id = ?', (customer_id,))
        conn.commit()
        conn.close()
        logger.info(f"Subscription {subscription['id']} canceled")

    return jsonify({"status": "success"}), 200

@app.route("/dashboard", methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    
    if request.method == 'POST':
        provider = request.form.get('provider')
        api_key = request.form.get('api_key')
        repo_name = request.form.get('repo_name')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            UPDATE users SET provider = ?, api_key = ?, repo_name = ?
            WHERE id = ?
        ''', (provider, api_key, repo_name, user_id))
        conn.commit()
        conn.close()
        flash('Settings saved!')
        return redirect(url_for('dashboard'))
    
    html = load_html('dashboard.html')
    # Inject dynamic values
    html = html.replace('value="deepseek"', f'value="{user[3]}" selected' if user[3] == 'deepseek' else 'value="deepseek"')
    html = html.replace('placeholder="owner/repo"', f'value="{user[5] or ""}"')
    html = html.replace('placeholder="sk-..."', f'value="{user[4] or ""}"')
    sub_status = user[9] or 'inactive'
    if sub_status == 'active':
        html = html.replace('Billing Status: Inactive', 'Billing Status: ✅ Active')
    else:
        html = html.replace('Billing Status: Inactive', 'Billing Status: ❌ Inactive')
    github_status = '✅ Connected' if user[6] else '❌ Not Connected'
    html = html.replace('GitHub Status: Not Connected', f'GitHub Status: {github_status}')
    return html

# ============================================================
# 11. RUN APP
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)