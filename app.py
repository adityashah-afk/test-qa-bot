#!/usr/bin/env python3
"""
Aegis - AI QA Engineer
Complete Backend + Frontend (Dashboard, Auth, Settings)
"""

# ============================================================
# 1. IMPORTS & SETUP
# ============================================================
import os
import logging
import sqlite3
import hashlib
import hmac
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, flash
from flask_session import Session
from werkzeug.security import generate_password_hash, check_password_hash

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Import existing backend modules
from github_client import get_pr_diff, post_comment
from pr_analyzer import analyze_pr_diff, extract_changed_functions
from code_scanner import ask_question_about_code

# ============================================================
# 2. LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# 3. FLASK APP CONFIG
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SESSION_TYPE'] = 'filesystem'  # Simple server-side sessions
app.config['SESSION_PERMANENT'] = False
Session(app)

# ============================================================
# 4. DATABASE SETUP (SQLite)
# ============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), 'aegis.db')

def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            github_token TEXT,
            repo_name TEXT,
            provider TEXT DEFAULT 'deepseek',
            api_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Table for stats (optional)
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

init_db()

def get_user_settings(username):
    """Retrieve settings for a given user."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT provider, api_key, repo_name, github_token FROM users WHERE username = ?', (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'provider': row[0], 'api_key': row[1], 'repo_name': row[2], 'github_token': row[3]}
    return None

def update_user_settings(username, provider, api_key, repo_name, github_token=None):
    """Update user settings."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE users 
        SET provider = ?, api_key = ?, repo_name = ?, github_token = COALESCE(?, github_token)
        WHERE username = ?
    ''', (provider, api_key, repo_name, github_token, username))
    conn.commit()
    conn.close()

# ============================================================
# 5. WEBHOOK & CORE ROUTES (unchanged logic)
# ============================================================

# Global fallback for webhook (uses environment variables if user not found)
GITHUB_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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
    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(request.data, signature):
        logger.warning("Invalid signature")
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event")
    payload = request.get_json()

    if event == "ping":
        return jsonify({"msg": "pong"}), 200

    # Try to get user settings based on repo (if we have a repo_name in payload)
    # For MVP, we use the first user's settings (if no user context, fallback to env)
    # In a real multi-tenant setup, you'd map repo to user.
    repo_name = payload.get("repository", {}).get("full_name")
    user_settings = None
    if repo_name:
        # Find user with this repo
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT username, provider, api_key FROM users WHERE repo_name = ?', (repo_name,))
        row = c.fetchone()
        conn.close()
        if row:
            # We'll set environment variables temporarily for this request
            # For simplicity, we just store them in a global variable or use them directly
            # But since we have multiple functions that expect env vars, we can set os.environ
            # This is a bit hacky but works for MVP
            user_settings = {'provider': row[1], 'api_key': row[2], 'username': row[0]}

    # Override environment for this request (if user settings found)
    if user_settings:
        os.environ['LLM_PROVIDER'] = user_settings['provider']
        os.environ['OPENAI_API_KEY'] = user_settings['api_key']  # we'll map later

    # --- Chatbot (issue_comment) ---
    if event == "issue_comment":
        issue = payload.get("issue", {})
        comment_body = payload.get("comment", {}).get("body", "")
        pr_number = issue.get("number")
        repo_name = payload["repository"]["full_name"]

        if issue.get("pull_request") and comment_body.strip().startswith("/ask"):
            logger.info(f"💬 Chatbot triggered on PR #{pr_number} in {repo_name}")
            question = comment_body.replace("/ask", "").strip() or "What does this code do?"
            try:
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                answer = ask_question_about_code(question, diff_content)
                post_comment(repo, pr_number, f"🤖 **AI Chatbot:**\n\n{answer}")
                logger.info(f"✅ Chatbot replied to PR #{pr_number}")
                return jsonify({"msg": "Chatbot replied"}), 200
            except Exception as e:
                logger.error(f"❌ Chatbot Error: {e}")
                return jsonify({"error": str(e)}), 500
        return jsonify({"msg": "Ignored comment"}), 200

    # --- Pull Request event ---
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize"]:
        pr_number = payload["number"]
        repo_name = payload["repository"]["full_name"]
        logger.info(f"🔍 Processing PR #{pr_number} in {repo_name}")

        try:
            diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
            # Determine if we have API key (from user settings or env)
            api_key = user_settings['api_key'] if user_settings else None
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
            logger.error(f"❌ Error processing PR: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"msg": "Ignored"}), 200

# ============================================================
# 6. FRONTEND ROUTES (Authentication + Dashboard)
# ============================================================

# Helper to load HTML from file
def load_html(filename):
    path = os.path.join(os.path.dirname(__file__), 'frontend', filename)
    with open(path, 'r') as f:
        return f.read()

@app.route("/")
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    # Serve landing page
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
        # Hash password
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
    # GET: show signup form (simple HTML)
    return '''
        <!DOCTYPE html>
        <html>
        <head><title>Aegis - Sign Up</title>
        <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gray-950 text-white min-h-screen flex items-center justify-center">
            <div class="bg-gray-900 p-8 rounded-2xl border border-gray-800 max-w-md w-full">
                <h1 class="text-2xl font-bold mb-6 text-cyan-400">⚡ Create Account</h1>
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 mb-4 focus:border-cyan-500">
                    <input type="password" name="password" placeholder="Password" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 mb-4 focus:border-cyan-500">
                    <button type="submit" class="w-full bg-cyan-600 hover:bg-cyan-700 py-2 rounded-lg font-semibold">Sign Up</button>
                </form>
                <p class="text-sm text-gray-500 mt-4">Already have an account? <a href="/login" class="text-cyan-400">Log in</a></p>
            </div>
        </body>
        </html>
    '''

@app.route("/login", methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT id, username, password_hash FROM users WHERE username = ?', (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['username'] = user[1]
            flash('Logged in successfully.')
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.')
        return redirect(url_for('login'))
    return '''
        <!DOCTYPE html>
        <html>
        <head><title>Aegis - Log In</title>
        <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gray-950 text-white min-h-screen flex items-center justify-center">
            <div class="bg-gray-900 p-8 rounded-2xl border border-gray-800 max-w-md w-full">
                <h1 class="text-2xl font-bold mb-6 text-cyan-400">⚡ Log In</h1>
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 mb-4 focus:border-cyan-500">
                    <input type="password" name="password" placeholder="Password" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 mb-4 focus:border-cyan-500">
                    <button type="submit" class="w-full bg-cyan-600 hover:bg-cyan-700 py-2 rounded-lg font-semibold">Log In</button>
                </form>
                <p class="text-sm text-gray-500 mt-4">No account? <a href="/signup" class="text-cyan-400">Sign up</a></p>
            </div>
        </body>
        </html>
    '''

@app.route("/logout")
def logout():
    session.clear()
    flash('Logged out.')
    return redirect(url_for('home'))

@app.route("/dashboard", methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    username = session['username']

    if request.method == 'POST':
        # Update settings
        provider = request.form.get('provider')
        api_key = request.form.get('api_key')
        repo_name = request.form.get('repo_name')
        github_token = request.form.get('github_token')  # optional for now
        update_user_settings(username, provider, api_key, repo_name, github_token)
        flash('Settings saved successfully!')
        return redirect(url_for('dashboard'))

    # GET: load settings
    settings = get_user_settings(username) or {}
    # Serve dashboard HTML with placeholders
    html = load_html('dashboard.html')
    # Replace placeholders with actual values (basic string replacement)
    html = html.replace('value="deepseek"', f'value="{settings.get("provider", "deepseek")}" selected')
    html = html.replace('placeholder="owner/repo"', f'value="{settings.get("repo_name", "")}"')
    html = html.replace('placeholder="sk-..."', f'value="{settings.get("api_key", "")}"')
    return html

# ============================================================
# 7. RUN APP
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)