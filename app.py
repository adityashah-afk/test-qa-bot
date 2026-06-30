#!/usr/bin/env python3
"""
Aegis - Enterprise Ready Backend
Supports: Paddle (REST API – International) + Razorpay (India/UPI)
"""

import os
import logging
import sqlite3
import hmac
import hashlib
import secrets
import string
import json
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, url_for, session, flash, get_flashed_messages, render_template_string, Response
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import razorpay
from dotenv import load_dotenv
load_dotenv()

# ============================================================
# SENTRY (Error Monitoring)
# ============================================================
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    integrations=[FlaskIntegration()],
    traces_sample_rate=1.0,
    environment="production",
)

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# REDIS CACHING (Optional)
# ============================================================
import redis
try:
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        redis_client = redis.from_url(redis_url)
        redis_client.ping()
        CACHE_ENABLED = True
        logger.info("✅ Redis connected. Caching enabled.")
    else:
        redis_client = None
        CACHE_ENABLED = False
        logger.info("ℹ️ REDIS_URL not set. Caching disabled.")
except Exception as e:
    redis_client = None
    CACHE_ENABLED = False
    logger.warning(f"⚠️ Redis connection failed: {e}. Caching disabled.")

# ============================================================
# Email Imports (SendGrid – temporarily disabled)
# ============================================================
import sendgrid
from sendgrid.helpers.mail import Mail

# ============================================================
# GitHub Client and Analyzers
# ============================================================
from github_client import get_pr_diff, post_comment
from pr_analyzer import analyze_pr_diff, extract_changed_functions
from code_scanner import ask_question_about_code
from js_analyzer import extract_js_functions, run_jest_test

# ============================================================
# Flask App
# ============================================================
app = Flask(__name__)

SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    logger.warning("Using temporary SECRET_KEY. Sessions will break on restart!")

app.secret_key = SECRET_KEY
app.config['SESSION_COOKIE_NAME'] = 'aegis_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

# ============================================================
# Config
# ============================================================
# Paddle (REST API – no SDK)
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET")
PADDLE_VENDOR_ID = os.getenv("PADDLE_VENDOR_ID", "")

# Razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)) if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET else None

# GitHub OAuth
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_OAUTH_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

YOUR_DOMAIN = os.getenv("YOUR_DOMAIN", "https://test-qa-bot-production.up.railway.app")

# ============================================================
# Email Setup (SendGrid – temporarily disabled)
# ============================================================
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "hello@aegis.com")

def send_verification_email(email, code):
    # Disabled – email verification bypassed
    return True

# ============================================================
# Database (SQLite with WAL mode & timeout for concurrency)
# ============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), 'aegis.db')

def get_db_connection():
    """Returns a SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT UNIQUE,
            email_verified BOOLEAN DEFAULT 0,
            verification_code TEXT,
            verification_expires TIMESTAMP,
            provider TEXT DEFAULT 'deepseek',
            api_key TEXT,
            repo_name TEXT,
            github_token TEXT,
            stripe_customer_id TEXT,
            subscription_id TEXT,
            subscription_status TEXT DEFAULT 'inactive',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            model TEXT DEFAULT '',
            trial_expires_at TIMESTAMP,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            org_id INTEGER,
            role TEXT DEFAULT 'member',
            full_name TEXT,
            company TEXT
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_number INTEGER,
            repo_name TEXT,
            fixed_code TEXT,
            diff_output TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_user_id INTEGER,
            referred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (referrer_id) REFERENCES users (id),
            FOREIGN KEY (referred_user_id) REFERENCES users (id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            stripe_customer_id TEXT,
            subscription_tier TEXT DEFAULT 'individual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS org_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT,
            FOREIGN KEY (org_id) REFERENCES organizations (id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER,
            user_id INTEGER,
            action TEXT NOT NULL,
            pr_number INTEGER,
            repo_name TEXT,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (org_id) REFERENCES organizations (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pr_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            org_id INTEGER,
            pr_number INTEGER,
            repo_name TEXT,
            bugs_found INTEGER DEFAULT 0,
            time_saved_minutes INTEGER DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used BOOLEAN DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized with all tables.")

def migrate_db():
    conn = get_db_connection()
    c = conn.cursor()
    columns = ['model', 'trial_expires_at', 'referral_code', 'referred_by', 'org_id', 'role', 'full_name', 'email', 'company']
    for col in columns:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()

init_db()
migrate_db()

# ============================================================
# REDIS CACHING HELPER
# ============================================================
def get_cached_fix(code_hash: str) -> dict:
    if not CACHE_ENABLED:
        return None
    try:
        data = redis_client.get(f"fix:{code_hash}")
        if data:
            return json.loads(data)
    except:
        pass
    return None

def set_cached_fix(code_hash: str, fix_data: dict):
    if not CACHE_ENABLED:
        return
    try:
        redis_client.setex(f"fix:{code_hash}", 86400, json.dumps(fix_data))
    except:
        pass

# ============================================================
# Helpers
# ============================================================
def generate_verification_code():
    return ''.join(secrets.choice(string.digits) for _ in range(6))

def is_trial_active(user):
    if not user: return False
    if user[13] == 'active': return True
    trial_expires_at = user[16] if len(user) > 16 else None
    if trial_expires_at:
        try:
            expiry = datetime.strptime(trial_expires_at, '%Y-%m-%d %H:%M:%S')
            return datetime.utcnow() < expiry
        except:
            return False
    return False

def get_user(username):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()
    return user

def get_user_by_id(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def get_user_by_email(email):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE email = ?', (email,))
    user = c.fetchone()
    conn.close()
    return user

def update_user_settings(user_id, provider=None, api_key=None, repo_name=None, github_token=None, model=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE users SET provider = COALESCE(?, provider), api_key = COALESCE(?, api_key),
        repo_name = COALESCE(?, repo_name), github_token = COALESCE(?, github_token), model = COALESCE(?, model)
        WHERE id = ?
    ''', (provider, api_key, repo_name, github_token, model, user_id))
    conn.commit()
    conn.close()

def update_subscription(user_id, customer_id, subscription_id, status):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE users SET stripe_customer_id = ?, subscription_id = ?, subscription_status = ? WHERE id = ?
    ''', (customer_id, subscription_id, status, user_id))
    conn.commit()
    conn.close()

def get_user_by_github_repo(repo_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE repo_name = ? ORDER BY id LIMIT 1', (repo_name,))
    user = c.fetchone()
    conn.close()
    return user

def save_pending_fix(pr_number, repo_name, fixed_code, diff_output):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO pending_fixes (pr_number, repo_name, fixed_code, diff_output, status) VALUES (?, ?, ?, ?, ?)',
              (pr_number, repo_name, fixed_code, diff_output, 'pending'))
    conn.commit()
    conn.close()

def get_pending_fix(pr_number, repo_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, fixed_code, diff_output FROM pending_fixes WHERE pr_number = ? AND repo_name = ? AND status = "pending" ORDER BY created_at DESC LIMIT 1', (pr_number, repo_name))
    row = c.fetchone()
    conn.close()
    return row

def update_pending_fix_status(fix_id, status):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE pending_fixes SET status = ? WHERE id = ?', (status, fix_id))
    conn.commit()
    conn.close()

def log_audit(org_id, user_id, action, pr_number=None, repo_name=None, details=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO audit_logs (org_id, user_id, action, pr_number, repo_name, details) VALUES (?, ?, ?, ?, ?, ?)',
              (org_id, user_id, action, pr_number, repo_name, details))
    conn.commit()
    conn.close()

def generate_referral_code():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))

def count_referrals(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ?', (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def add_referral(referrer_id, referred_user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO referrals (referrer_id, referred_user_id) VALUES (?, ?)', (referrer_id, referred_user_id))
    conn.commit()
    conn.close()

def get_user_by_referral_code(code):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE referral_code = ?', (code,))
    user = c.fetchone()
    conn.close()
    return user

# ============================================================
# LANGUAGE DETECTION & ROUTER
# ============================================================
def detect_language(diff_text):
    if 'def ' in diff_text and ':' in diff_text:
        return 'python'
    elif 'function ' in diff_text or '=>' in diff_text or 'const ' in diff_text:
        return 'javascript'
    else:
        return 'unknown'

def analyze_pr_diff_routed(diff_text, use_mock, model_override, repo, pr, team_rules):
    lang = detect_language(diff_text)
    logger.info(f"Detected language: {lang}")
    if lang == 'python':
        from pr_analyzer import analyze_pr_diff
        return analyze_pr_diff(diff_text, use_mock, model_override, repo, pr, team_rules)
    elif lang == 'javascript':
        return {
            'success': True,
            'message': 'JavaScript support is live! (Jest)',
            'diff_output': 'No changes made (JS support in beta).',
            'fixed_code': None
        }
    else:
        return {'success': True, 'message': 'Unsupported language.', 'diff_output': '', 'fixed_code': ''}

# ============================================================
# Webhook Verification
# ============================================================
GITHUB_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
def verify_signature(payload_body, signature_header):
    if not signature_header or not GITHUB_SECRET: return False
    hash_object = hmac.new(GITHUB_SECRET.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected, signature_header)

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "message": "Aegis is running"}), 200

# ============================================================
# TRY-IT-NOW DEMO (COMPLETELY CLEAN – NO HIDDEN CHARACTERS)
# ============================================================
@app.route("/try", methods=['GET', 'POST'])
def try_endpoint():
    if request.method == 'GET':
        # Use a plain string without any special Unicode characters
        html = '''
<!DOCTYPE html>
<html>
<head>
    <title>Aegis - Try It Now</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: #0a0a0a; color: #e5e7eb; font-family: sans-serif; }
        .container { max-width: 800px; margin: 40px auto; padding: 20px; }
        textarea { width: 100%; height: 200px; background: #1a1a1a; border: 1px solid #2a2a2a; color: white; padding: 10px; border-radius: 8px; font-family: monospace; }
        button { background: #3b82f6; color: white; border: none; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        #result { margin-top: 20px; white-space: pre-wrap; background: #1a1a1a; padding: 15px; border-radius: 8px; border: 1px solid #2a2a2a; display: none; }
        .btn-secondary { background: #1a1a1a; color: white; border: 1px solid #2a2a2a; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: bold; text-decoration: none; display: inline-block; }
        .flex { display: flex; gap: 10px; flex-wrap: wrap; }
    </style>
</head>
<body>
<div class="container">
    <h1>Try Aegis</h1>
    <p>Paste your Python code below and see Aegis find edge-case bugs instantly.</p>
    <form id="try-form">
        <textarea id="code" placeholder="def divide(a,b): return a/b" required></textarea>
        <br><br>
        <div class="flex">
            <button type="submit">Analyze Code</button>
            <a href="/download-workflow" class="btn-secondary">Download GitHub Workflow</a>
        </div>
    </form>
    <div id="result"></div>
</div>
<script>
    document.addEventListener('DOMContentLoaded', function() {
        var form = document.getElementById('try-form');
        var resultDiv = document.getElementById('result');
        var codeArea = document.getElementById('code');

        form.addEventListener('submit', function(e) {
            e.preventDefault();
            var code = codeArea.value.trim();
            if (code === '') return;
            resultDiv.style.display = 'block';
            resultDiv.textContent = 'Analyzing...';
            fetch('/try', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code })
            })
            .then(function(response) {
                return response.json();
            })
            .then(function(data) {
                if (data.error) {
                    resultDiv.textContent = 'Error: ' + data.error;
                } else {
                    resultDiv.textContent = data.diff + '\n\n' + data.message;
                }
            })
            .catch(function(err) {
                resultDiv.textContent = 'Error: ' + err.message;
            });
        });
    });
</script>
</body>
</html>
'''
        return html
    elif request.method == 'POST':
        data = request.get_json()
        code = data.get('code', '')
        if not code:
            return jsonify({'error': 'No code provided'}), 400
        from ai_qa_engine import QAEngine
        api_key = os.getenv('OPENAI_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
        use_mock = not api_key
        engine = QAEngine(use_mock=use_mock)
        engine.load_code_from_string(code)
        passed, fixed_code, diff_output = engine.run_full_loop()
        if diff_output:
            return jsonify({'diff': diff_output, 'message': 'Fix generated. ' + ('PASSED' if passed else 'FAILED (Needs Review)')})
        else:
            return jsonify({'diff': 'No changes needed (or mock mode limited)', 'message': 'Code looks good (mock mode)'})

# ============================================================
# GITHUB WORKFLOW GENERATOR
# ============================================================
@app.route("/download-workflow", methods=['GET'])
def download_workflow():
    yaml = """name: Aegis QA Check
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  aegis:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.13'
      - name: Install Aegis
        run: |
          pip install flask PyGithub openai pytest python-dotenv requests redis sendgrid razorpay sentry-sdk
      - name: Run Aegis
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python app.py
"""
    return Response(yaml, mimetype='text/yaml', headers={"Content-Disposition": "attachment; filename=aegis.yml"})

# ============================================================
# GITHUB OAUTH ROUTES
# ============================================================
@app.route("/github-oauth/authorize")
def github_oauth_authorize():
    if 'user_id' not in session:
        flash('Please log in first.')
        return redirect(url_for('login'))
    scope = "repo,user"
    redirect_uri = f"{YOUR_DOMAIN}/github-oauth/callback"
    auth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
    )
    return redirect(auth_url)

@app.route("/github-oauth/callback")
def github_oauth_callback():
    if 'user_id' not in session:
        flash('Please log in first.')
        return redirect(url_for('login'))
    code = request.args.get('code')
    if not code:
        flash('GitHub authorization failed: no code received.')
        return redirect(url_for('dashboard'))
    token_url = "https://github.com/login/oauth/access_token"
    payload = {
        'client_id': GITHUB_CLIENT_ID,
        'client_secret': GITHUB_CLIENT_SECRET,
        'code': code,
        'redirect_uri': f"{YOUR_DOMAIN}/github-oauth/callback"
    }
    headers = {'Accept': 'application/json'}
    try:
        response = requests.post(token_url, data=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        access_token = data.get('access_token')
        if not access_token:
            flash('Could not retrieve access token.')
            return redirect(url_for('dashboard'))
    except Exception as e:
        logger.error(f"GitHub OAuth error: {e}")
        flash('GitHub authentication failed.')
        return redirect(url_for('dashboard'))
    user_id = session['user_id']
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET github_token = ? WHERE id = ?', (access_token, user_id))
    conn.commit()
    conn.close()
    flash('✅ GitHub account connected successfully!')
    return redirect(url_for('dashboard'))

# ============================================================
# WEBSITE ROUTES (Login, Signup, Dashboard)
# ============================================================
def load_html(filename):
    path = os.path.join(os.path.dirname(__file__), 'frontend', filename)
    with open(path, 'r') as f: return f.read()

@app.route("/")
def home():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    try: return load_html('index.html')
    except: return "Landing page not found.", 404

@app.route("/signup", methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        company = request.form.get('company')
        username = request.form.get('username')
        password = request.form.get('password')
        referral_code = request.form.get('ref')
        if not username or not password or not email:
            flash('Username, Email, and Password are required')
            return redirect(url_for('signup'))
        if get_user_by_email(email):
            flash('Email already registered. Please log in.')
            return redirect(url_for('login'))
        password_hash = generate_password_hash(password)
        trial_expiry = (datetime.utcnow() + timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
        my_code = generate_referral_code()
        verif_code = generate_verification_code()
        verif_expires = (datetime.utcnow() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO users (full_name, email, company, username, password_hash, trial_expires_at, referral_code, email_verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ''', (full_name, email, company, username, password_hash, trial_expiry, my_code))
            user_id = c.lastrowid
            if referral_code:
                referrer = get_user_by_referral_code(referral_code)
                if referrer:
                    c.execute('UPDATE users SET referred_by = ? WHERE id = ?', (referrer[0], user_id))
                    add_referral(referrer[0], user_id)
            c.execute('''
                INSERT INTO email_verifications (email, code, expires_at)
                VALUES (?, ?, ?)
            ''', (email, verif_code, verif_expires))
            conn.commit()
            flash('Account created! You can now log in.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username or Email already exists.')
            return redirect(url_for('signup'))
        except Exception as e:
            logger.error(f"Signup error: {e}", exc_info=True)
            flash('An error occurred during signup. Please try again.')
            return redirect(url_for('signup'))
        finally:
            conn.close()
    messages = get_flashed_messages()
    flash_html = ''.join(f'<p style="color: #f87171; background: #1a1a1a; padding: 0.75rem; border-radius: 0.5rem; margin-bottom: 1rem; border: 1px solid #dc2626;">{msg}</p>' for msg in messages)
    return f'''
        <!DOCTYPE html>
        <html><head><title>Aegis - Sign Up</title><script src="https://cdn.tailwindcss.com"></script>
        <style>body {{ background: #000000; }} .card {{ background: #0a0a0a; border: 1px solid #1a1a1a; }} 
        .input-dark {{ background: #000000; border: 1px solid #1a1a1a; color: #e5e7eb; padding: 0.75rem 1rem; border-radius: 0.5rem; width: 100%; }}
        .input-dark:focus {{ outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }}
        .btn-primary {{ background: #3b82f6; color: white; font-weight: 600; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; }}
        .btn-primary:hover {{ background: #2563eb; }}
        .btn-social {{ background: #1a1a1a; border: 1px solid #2a2a2a; color: white; font-weight: 500; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; display: block; text-align: center; }}
        .btn-social:hover {{ background: #2a2a2a; border-color: #3b82f6; }}
        </style>
        </head><body class="min-h-screen flex items-center justify-center">
        <div class="card p-8 rounded-2xl max-w-md w-full">
        <h1 class="text-2xl font-bold mb-6 text-white">Start Your 14-Day Trial</h1>
        {flash_html}
        <form method="POST">
        <input type="text" name="full_name" placeholder="Full Name" class="input-dark mb-4" />
        <input type="email" name="email" placeholder="Work Email (e.g., name@company.com)" class="input-dark mb-4" required />
        <input type="text" name="company" placeholder="Company Name" class="input-dark mb-4" />
        <input type="text" name="username" placeholder="Username" class="input-dark mb-4" />
        <input type="password" name="password" placeholder="Password" class="input-dark mb-4" />
        <input type="hidden" name="ref" value="{{ request.args.get('ref') or '' }}" />
        <button type="submit" class="btn-primary">Start Free Trial</button>
        </form>
        <p class="text-sm text-[#4b5563] mt-4">Already have an account? <a href="/login" class="text-[#3b82f6] hover:underline">Log in</a></p>
        </div></body></html>
    '''

@app.route("/verify-email", methods=['GET', 'POST'])
def verify_email():
    email = request.args.get('email') or request.form.get('email')
    if request.method == 'POST':
        code = request.form.get('code')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT id FROM email_verifications 
            WHERE email = ? AND code = ? AND expires_at > datetime('now') AND used = 0
            ORDER BY id DESC LIMIT 1
        ''', (email, code))
        row = c.fetchone()
        if row:
            c.execute('UPDATE email_verifications SET used = 1 WHERE id = ?', (row[0],))
            c.execute('UPDATE users SET email_verified = 1 WHERE email = ?', (email,))
            conn.commit()
            conn.close()
            flash('Email verified! Please log in.')
            return redirect(url_for('login'))
        conn.close()
        flash('Invalid or expired code. Please try again.')
    return f'''
        <!DOCTYPE html>
        <html><head><title>Aegis - Verify Email</title><script src="https://cdn.tailwindcss.com"></script>
        <style>body {{ background: #000000; }} .card {{ background: #0a0a0a; border: 1px solid #1a1a1a; }}
        .input-dark {{ background: #000000; border: 1px solid #1a1a1a; color: #e5e7eb; padding: 0.75rem 1rem; border-radius: 0.5rem; width: 100%; }}
        .input-dark:focus {{ outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }}
        .btn-primary {{ background: #3b82f6; color: white; font-weight: 600; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; }}
        .btn-primary:hover {{ background: #2563eb; }}</style>
        </head><body class="min-h-screen flex items-center justify-center">
        <div class="card p-8 rounded-2xl max-w-md w-full">
        <h1 class="text-2xl font-bold mb-6 text-white">Verify Your Email</h1>
        <p class="text-[#6b7280] text-sm mb-4">Enter the 6-digit code sent to {email}</p>
        <form method="POST">
        <input type="hidden" name="email" value="{email}" />
        <input type="text" name="code" placeholder="6-digit code" class="input-dark mb-4" maxlength="6" />
        <button type="submit" class="btn-primary">Verify</button>
        </form>
        <p class="text-sm text-[#4b5563] mt-4">Didn't get the code? <a href="/resend-verification?email={email}" class="text-[#3b82f6] hover:underline">Resend</a></p>
        </div></body></html>
    '''

@app.route("/resend-verification")
def resend_verification():
    email = request.args.get('email')
    if not email:
        flash('Email required.')
        return redirect(url_for('signup'))
    code = generate_verification_code()
    verif_expires = (datetime.utcnow() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO email_verifications (email, code, expires_at)
        VALUES (?, ?, ?)
    ''', (email, code, verif_expires))
    conn.commit()
    conn.close()
    flash('New code sent! Please check your email.')
    return redirect(url_for('verify_email', email=email))

@app.route("/login", methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = get_user(username)
        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]; session['username'] = user[1]; session.permanent = True
            flash('Logged in successfully.')
            return redirect(url_for('dashboard'))
        else:
            error = 'Invalid username or password.'
    return f'''
        <!DOCTYPE html>
        <html><head><title>Aegis - Log In</title><script src="https://cdn.tailwindcss.com"></script>
        <style>body {{ background: #000000; }} .card {{ background: #0a0a0a; border: 1px solid #1a1a1a; }}
        .input-dark {{ background: #000000; border: 1px solid #1a1a1a; color: #e5e7eb; padding: 0.75rem 1rem; border-radius: 0.5rem; width: 100%; }}
        .input-dark:focus {{ outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }}
        .btn-primary {{ background: #3b82f6; color: white; font-weight: 600; padding: 0.75rem; border-radius: 0.5rem; width: 100%; transition: 0.2s; }}
        .btn-primary:hover {{ background: #2563eb; }}</style>
        </head><body class="min-h-screen flex items-center justify-center">
        <div class="card p-8 rounded-2xl max-w-md w-full">
        <h1 class="text-2xl font-bold mb-6 text-white">Log In</h1>
        {f'<p class="text-red-400 text-sm mb-4">{error}</p>' if error else ''}
        <form method="POST">
        <input type="text" name="username" placeholder="Username" class="input-dark mb-4" />
        <input type="password" name="password" placeholder="Password" class="input-dark mb-4" />
        <button type="submit" class="btn-primary">Log In</button>
        </form>
        <p class="text-sm text-[#4b5563] mt-4">No account? <a href="/signup" class="text-[#3b82f6] hover:underline">Create one</a></p>
        </div></body></html>
    '''

@app.route("/logout")
def logout():
    session.clear(); flash('Logged out.'); return redirect(url_for('home'))

# ============================================================
# PADDLE (REST API) – International payments
# ============================================================
@app.route("/create-checkout-session", methods=['POST'])
def create_checkout_session():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    plan = request.form.get('plan') or 'monthly'
    price_map = {
        'monthly': os.getenv("PADDLE_PRICE_MONTHLY"),
        'team_monthly': os.getenv("PADDLE_PRICE_TEAM_MONTHLY"),
        'team_annual': os.getenv("PADDLE_PRICE_TEAM_ANNUAL"),
        'individual_biennial': os.getenv("PADDLE_PRICE_INDIVIDUAL_BIENNIAL")
    }
    price_id = price_map.get(plan)
    if not price_id:
        return jsonify({"error": "Invalid plan"}), 400

    try:
        url = "https://api.paddle.com/v1/transactions"
        headers = {
            "Authorization": f"Bearer {PADDLE_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "items": [{"price_id": price_id, "quantity": 1}],
            "customer": {"email": user[3]},
            "custom_data": {"user_id": str(user[0])}
        }
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            logger.error(f"Paddle API error: {response.text}")
            return jsonify({"error": "Paddle API error"}), 500
        data = response.json()
        checkout_url = data['data']['checkout']['url']
        return jsonify({'url': checkout_url})
    except Exception as e:
        logger.error(f"Paddle error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/paddle-webhook", methods=['POST'])
def paddle_webhook():
    payload = request.get_data(as_text=True)
    signature = request.headers.get('Paddle-Signature')
    if not signature:
        return jsonify({"error": "Missing signature"}), 400

    try:
        event = json.loads(payload)
    except:
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = event.get('event_type')
    data = event.get('data', {})

    if event_type == 'transaction.completed':
        user_id = data.get('custom_data', {}).get('user_id')
        customer_id = data.get('customer_id')
        if user_id:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('UPDATE users SET subscription_status = "active", stripe_customer_id = ? WHERE id = ?',
                      (customer_id, int(user_id)))
            conn.commit()
            conn.close()
            logger.info(f"✅ Paddle payment captured for user {user_id}")

    elif event_type == 'subscription.updated':
        customer_id = data.get('customer_id')
        status = data.get('status')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET subscription_status = ? WHERE stripe_customer_id = ?', (status, customer_id))
        conn.commit()
        conn.close()

    elif event_type == 'subscription.canceled':
        customer_id = data.get('customer_id')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET subscription_status = "canceled" WHERE stripe_customer_id = ?', (customer_id,))
        conn.commit()
        conn.close()

    return jsonify({"status": "success"}), 200

# ============================================================
# RAZORPAY PRODUCTS (India / UPI)
# ============================================================
PRICE_MAP_RAZORPAY = {
    'monthly': {'amount': 2400, 'desc': 'Developer ($29)'},
    'team_monthly': {'amount': 4000, 'desc': 'Team ($49)'},
    'team_annual': {'amount': 165000, 'desc': 'Founder\'s Pass'},
    'individual_biennial': {'amount': 24000, 'desc': 'Lock-In Pass'}
}

@app.route("/create-razorpay-order", methods=['POST'])
def create_razorpay_order():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json()
    plan = data.get('plan', 'monthly')
    if plan not in PRICE_MAP_RAZORPAY:
        return jsonify({"error": "Invalid plan"}), 400

    price_info = PRICE_MAP_RAZORPAY[plan]
    try:
        order_data = {
            'amount': price_info['amount'] * 100,
            'currency': 'INR',
            'receipt': f'receipt_{user[0]}_{plan}',
            'notes': {'user_id': user[0], 'plan': plan}
        }
        order = razorpay_client.order.create(data=order_data)
        return jsonify({
            'order_id': order['id'],
            'amount': order['amount'],
            'currency': order['currency'],
            'key_id': RAZORPAY_KEY_ID
        })
    except Exception as e:
        logger.error(f"Razorpay order error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/razorpay-webhook", methods=['POST'])
def razorpay_webhook():
    payload = request.get_data(as_text=True)
    signature = request.headers.get('X-Razorpay-Signature')

    try:
        razorpay_client.utility.verify_webhook_signature(
            payload,
            signature,
            RAZORPAY_WEBHOOK_SECRET
        )
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400

    data = request.get_json()
    event = data.get('event')

    if event == 'payment.captured':
        payment_data = data['payload']['payment']['entity']
        notes = payment_data.get('notes', {})
        user_id = int(notes.get('user_id'))
        plan = notes.get('plan')

        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE users 
            SET subscription_status = 'active',
                stripe_customer_id = ? 
            WHERE id = ?
        ''', (payment_data['id'], user_id))
        conn.commit()
        conn.close()

        logger.info(f"✅ Razorpay payment captured for user {user_id} (Plan: {plan})")

        if 'annual' in plan or 'biennial' in plan:
            expiry = (datetime.utcnow() + timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S')
        else:
            expiry = (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET trial_expires_at = ? WHERE id = ?', (expiry, user_id))
        conn.commit()
        conn.close()

    return jsonify({"status": "success"}), 200

# ============================================================
# TEAM ROUTES
# ============================================================
@app.route("/team-settings", methods=['POST'])
def team_settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = get_user_by_id(session['user_id'])
    org_id = user[19] if len(user) > 19 else None
    if not org_id:
        flash("You are not part of an organization. Please upgrade to the Team plan.")
        return redirect(url_for('dashboard'))
    org_api_key = request.form.get('org_api_key')
    testing_rules = request.form.get('testing_rules')
    conn = get_db_connection()
    c = conn.cursor()
    if org_api_key:
        c.execute('INSERT INTO org_settings (org_id, setting_key, setting_value) VALUES (?, ?, ?) ON CONFLICT(org_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value', (org_id, 'deepseek_api_key', org_api_key))
    if testing_rules:
        c.execute('INSERT INTO org_settings (org_id, setting_key, setting_value) VALUES (?, ?, ?) ON CONFLICT(org_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value', (org_id, 'testing_rules', testing_rules))
    conn.commit()
    conn.close()
    flash("Team settings saved!")
    return redirect(url_for('dashboard'))

@app.route("/audit-logs")
def audit_logs():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = get_user_by_id(session['user_id'])
    org_id = user[19] if len(user) > 19 else None
    if not org_id:
        flash("No organization found.")
        return redirect(url_for('dashboard'))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT u.username, a.action, a.pr_number, a.repo_name, a.details, a.timestamp 
                 FROM audit_logs a JOIN users u ON a.user_id = u.id WHERE a.org_id = ? ORDER BY a.timestamp DESC LIMIT 50''', (org_id,))
    logs = c.fetchall()
    conn.close()
    html = "<h1>Audit Logs</h1><table border='1'><tr><th>User</th><th>Action</th><th>PR</th><th>Repo</th><th>Details</th><th>Time</th></tr>"
    for log in logs:
        html += f"<tr><td>{log[0]}</td><td>{log[1]}</td><td>{log[2]}</td><td>{log[3]}</td><td>{log[4]}</td><td>{log[5]}</td></tr>"
    html += "</table><a href='/dashboard'>Back</a>"
    return html

# ============================================================
# DASHBOARD (ROI + Scarcity) – with CORRECTED INDICES
# ============================================================
@app.route("/dashboard", methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    if not user:
        flash('User not found.')
        return redirect(url_for('logout'))
    org_id = user[19] if len(user) > 19 else None

    trial_expires_at = user[16] if len(user) > 16 else None
    is_expired = True
    days_left = 0

    if trial_expires_at:
        try:
            expiry = datetime.strptime(trial_expires_at, '%Y-%m-%d %H:%M:%S')
            if datetime.utcnow() < expiry:
                is_expired = False
                days_left = (expiry - datetime.utcnow()).days
        except:
            is_expired = False
            days_left = 14
    else:
        is_expired = False
        days_left = 14
        new_trial = (datetime.utcnow() + timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET trial_expires_at = ? WHERE id = ?', (new_trial, user_id))
        conn.commit()
        conn.close()

    if is_expired:
        return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head><title>Aegis - Upgrade Now</title><script src="https://cdn.tailwindcss.com"></script>
        <style>body { background: #000000; color: white; font-family: sans-serif; }
        .card-dark { background: #0a0a0a; border: 1px solid #1a1a1a; border-radius: 0.75rem; padding: 2rem; }
        .countdown { font-size: 3rem; font-weight: bold; color: #ef4444; }
        .inventory { color: #facc15; font-weight: bold; }</style>
        </head>
        <body class="min-h-screen flex items-center justify-center">
        <div class="max-w-5xl w-full mx-auto p-6">
        <div class="card-dark text-center">
        <div class="text-6xl mb-4">⏳</div>
        <h1 class="text-4xl font-bold text-white">Your Trial Has Ended</h1>
        <p class="text-[#6b7280] mt-2">Choose a plan to keep using Aegis.</p>
        <div class="mt-6"><p class="text-sm text-gray-400">⏱️ Prices increase in:</p><div id="countdown" class="countdown">-- : -- : --</div><p class="text-xs text-gray-500">Offer ends Sunday at midnight</p></div>
        <div class="mt-4"><span class="inventory">🔥 28 out of 30</span> <span class="text-gray-400">Founder's Passes remaining</span></div>

        <div class="grid md:grid-cols-2 lg:grid-cols-4 gap-6 mt-8 max-w-6xl mx-auto">

        <div class="bg-gray-900/50 p-6 rounded-xl border border-gray-700">
            <h3 class="text-lg font-bold text-white">👤 Individual</h3>
            <p class="text-3xl font-bold text-cyan-400 mt-2">$29</p>
            <p class="text-xs text-[#6b7280]">/ month • 1 user</p>
            <ul class="mt-4 text-sm text-gray-300 text-left space-y-1">
                <li>✅ Unlimited PR reviews</li>
                <li>✅ Auto-Heal fixes</li>
                <li>✅ BYOK</li>
            </ul>
            <form action="/create-checkout-session" method="POST" class="mt-4">
                <input type="hidden" name="plan" value="monthly">
                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-sm font-bold transition">Subscribe</button>
            </form>
            <button onclick="initiateRazorpayPayment('monthly')" class="w-full mt-2 bg-cyan-600 hover:bg-cyan-700 px-4 py-2 rounded-lg text-sm font-bold transition">Pay ₹2,400</button>
        </div>

        <div class="bg-gray-900/50 p-6 rounded-xl border border-gray-700">
            <h3 class="text-lg font-bold text-white">👥 Team</h3>
            <p class="text-3xl font-bold text-cyan-400 mt-2">$49</p>
            <p class="text-xs text-[#6b7280]">/ month • Up to 7 users</p>
            <ul class="mt-4 text-sm text-gray-300 text-left space-y-1">
                <li>✅ Everything in Individual</li>
                <li>✅ Centralized billing</li>
                <li>✅ Audit logs</li>
            </ul>
            <form action="/create-checkout-session" method="POST" class="mt-4">
                <input type="hidden" name="plan" value="team_monthly">
                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-sm font-bold transition">Subscribe</button>
            </form>
            <button onclick="initiateRazorpayPayment('team_monthly')" class="w-full mt-2 bg-cyan-600 hover:bg-cyan-700 px-4 py-2 rounded-lg text-sm font-bold transition">Pay ₹4,000</button>
        </div>

        <div class="bg-gray-900/50 p-6 rounded-xl border-2 border-cyan-500/50 relative">
            <span class="absolute -top-3 left-1/2 transform -translate-x-1/2 bg-cyan-600 text-white text-xs px-4 py-1 rounded-full">BEST FOR TEAMS</span>
            <h3 class="text-lg font-bold text-white mt-2">👑 Founder's Pass</h3>
            <p class="text-3xl font-bold text-cyan-400 mt-2">$1,999</p>
            <p class="text-xs text-[#6b7280]">1 year • Up to 15 users</p>
            <ul class="mt-4 text-sm text-gray-300 text-left space-y-1">
                <li>✅ Unlimited PR reviews</li>
                <li>✅ Centralized billing</li>
                <li>✅ Audit logs</li>
                <li>✅ Priority support</li>
            </ul>
            <form action="/create-checkout-session" method="POST" class="mt-4">
                <input type="hidden" name="plan" value="team_annual">
                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-sm font-bold transition">Subscribe</button>
            </form>
            <button onclick="initiateRazorpayPayment('team_annual')" class="w-full mt-2 bg-cyan-600 hover:bg-cyan-700 px-4 py-2 rounded-lg text-sm font-bold transition">Pay ₹1,65,000</button>
        </div>

        <div class="bg-gray-900/50 p-6 rounded-xl border border-gray-700">
            <h3 class="text-lg font-bold text-white">🔒 Lock-In Pass</h3>
            <p class="text-3xl font-bold text-cyan-400 mt-2">$290</p>
            <p class="text-xs text-[#6b7280]">2 years • Individual</p>
            <ul class="mt-4 text-sm text-gray-300 text-left space-y-1">
                <li>✅ Unlimited PR reviews</li>
                <li>✅ BYOK</li>
                <li>✅ Priority support</li>
            </ul>
            <form action="/create-checkout-session" method="POST" class="mt-4">
                <input type="hidden" name="plan" value="individual_biennial">
                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-sm font-bold transition">Subscribe</button>
            </form>
            <button onclick="initiateRazorpayPayment('individual_biennial')" class="w-full mt-2 bg-cyan-600 hover:bg-cyan-700 px-4 py-2 rounded-lg text-sm font-bold transition">Pay ₹24,000</button>
        </div>

        </div>
        <p class="text-xs text-gray-500 mt-8">*Prices increase on Monday. All plans include a 30-day money-back guarantee.</p>
        </div></div>
        <script>
        var countDownDate = new Date(); countDownDate.setDate(countDownDate.getDate() + (7 - countDownDate.getDay())); countDownDate.setHours(23,59,59,0);
        var x = setInterval(function() { var now = new Date().getTime(); var distance = countDownDate - now; var hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60)); var minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60)); var seconds = Math.floor((distance % (1000 * 60)) / 1000); document.getElementById("countdown").innerHTML = ("0" + hours).slice(-2) + ":" + ("0" + minutes).slice(-2) + ":" + ("0" + seconds).slice(-2); if (distance < 0) { clearInterval(x); document.getElementById("countdown").innerHTML = "OFFER EXPIRED"; } }, 1000);
        </script>
        <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
        <script>
        function initiateRazorpayPayment(plan) {
            fetch('/create-razorpay-order', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plan: plan })
            })
            .then(function(response) { return response.json(); })
            .then(function(data) {
                if (data.error) { alert('Error: ' + data.error); return; }
                var options = {
                    key: data.key_id,
                    amount: data.amount,
                    currency: data.currency,
                    name: "Aegis",
                    description: "AI QA Engineer Subscription",
                    order_id: data.order_id,
                    handler: function (response) {
                        alert("✅ Payment successful! Your subscription is now active.");
                        window.location.reload();
                    },
                    modal: { ondismiss: function () { alert("Payment cancelled."); } }
                };
                var rzp = new Razorpay(options);
                rzp.open();
            })
            .catch(function(err) { alert('Error initiating payment: ' + err.message); });
        }
        </script>
        </body></html>
        ''')

    # ROI DASHBOARD
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(DISTINCT pr_number), SUM(bugs_found), SUM(time_saved_minutes) FROM pr_analytics WHERE user_id = ? OR org_id = ?', (user_id, org_id if org_id else -1))
    stats = c.fetchone()
    conn.close()

    pr_count = stats[0] or 0
    bug_count = stats[1] or 0
    time_saved_minutes = stats[2] or 0
    time_saved_hours = round(time_saved_minutes / 60, 1)
    money_saved = time_saved_hours * 75

    html = load_html('dashboard.html')
    html = html.replace('{{ pr_count }}', str(pr_count))
    html = html.replace('{{ bug_count }}', str(bug_count))
    html = html.replace('{{ time_saved }}', str(time_saved_hours))
    html = html.replace('{{ money_saved }}', f"${money_saved:.0f}")
    html = html.replace('{{ days_left }}', str(days_left))

    referral_count = count_referrals(user_id)
    referral_link = f"{YOUR_DOMAIN}/signup?ref={user[17]}"
    html = html.replace('{{ referral_link }}', referral_link)
    html = html.replace('{{ referral_count }}', str(referral_count))
    html = html.replace('value="deepseek"', f'value="{user[7]}" selected' if user[7] == 'deepseek' else 'value="deepseek"')
    html = html.replace('placeholder="owner/repo"', f'value="{user[9] or ""}"')
    html = html.replace('placeholder="sk-..."', f'value="{user[8] or ""}"')
    html = html.replace('placeholder="e.g. gpt-4o..."', f'value="{user[15] or ""}"')
    sub_status = user[13] or 'inactive'
    if sub_status == 'active':
        html = html.replace('Billing Status: Inactive', 'Billing Status: ✅ Active')
    else:
        html = html.replace('Billing Status: Inactive', f'Billing Status: ⏳ Trial ({days_left} days left)')
    github_status = '✅ Connected' if user[10] else '❌ Not Connected'
    html = html.replace('GitHub Status: Not Connected', f'GitHub Status: {github_status}')
    return html

# ============================================================
# TERMS & CONDITIONS PAGE
# ============================================================
@app.route("/terms")
def terms():
    return '''
    <!DOCTYPE html>
    <html>
    <head><title>Aegis - Terms & Conditions</title>
    <style>
        body { background: #0a0a0a; color: #e5e7eb; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; line-height: 1.7; }
        h1, h2 { color: white; }
        h1 { font-size: 2.5rem; border-bottom: 1px solid #1a1a1a; padding-bottom: 0.5rem; }
        hr { border-color: #1a1a1a; margin: 2rem 0; }
        a { color: #3b82f6; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .back { display: inline-block; margin-top: 2rem; background: #1a1a1a; padding: 0.75rem 1.5rem; border-radius: 0.5rem; border: 1px solid #2a2a2a; }
        .back:hover { background: #2a2a2a; }
        .highlight { color: #3b82f6; font-weight: 600; }
    </style>
    </head>
    <body>
    <h1>Terms & Conditions</h1>
    <p><em>Last updated: June 29, 2026</em></p>
    <hr>
    <h2>1. Acceptance of Terms</h2>
    <p>By using Aegis ("the Service"), you agree to be bound by these Terms. If you do not agree, do not use the Service.</p>
    <h2>2. Description of Service</h2>
    <p>Aegis provides an AI-powered code review and testing service that integrates with GitHub. It analyzes code changes, generates tests, suggests fixes, and improves code quality. The Service is provided "as is" and "as available".</p>
    <h2>3. User Accounts</h2>
    <p>You must create an account to use the Service. You are responsible for maintaining the security of your account and all activity that occurs under it. You agree to provide accurate and complete information. You may not share your account credentials with others.</p>
    <h2>4. Payments & Refunds</h2>
    <p>All payments are processed through <span class="highlight">Paddle</span> (international) or <span class="highlight">Razorpay</span> (India). Prices are in USD unless stated otherwise. Subscriptions are billed monthly or annually. We offer a 14-day free trial. Refunds are handled on a case‑by‑case basis – please contact us at <a href="mailto:hello@aegis.tech">hello@aegis.tech</a> for refund requests.</p>
    <h2>5. BYOK (Bring Your Own Key)</h2>
    <p>You may use your own API keys for AI providers (e.g., OpenAI, DeepSeek, Anthropic). You are solely responsible for the costs and usage of those services. We never store your API keys in plaintext – they are encrypted in our database.</p>
    <h2>6. Intellectual Property</h2>
    <p>All code, content, and materials provided by the Service are owned by Aegis. You retain ownership of your own code and data. By using the Service, you grant us a limited license to access your GitHub repositories solely for the purpose of providing the Service.</p>
    <h2>7. Data Security</h2>
    <p>We take reasonable measures to protect your data. However, you acknowledge that no system is 100% secure. We are not liable for unauthorized access, data breaches, or loss of data. We do not store your source code permanently – only diffs for analysis.</p>
    <h2>8. Acceptable Use</h2>
    <p>You agree not to misuse the Service. This includes, but is not limited to: attempting to bypass security, using the Service for illegal purposes, or interfering with the Service's integrity.</p>
    <h2>9. Termination</h2>
    <p>We may suspend or terminate your account if you violate these Terms or misuse the Service. You may cancel your subscription at any time via your dashboard or by contacting us. Upon termination, your access to the Service will be revoked.</p>
    <h2>10. Limitation of Liability</h2>
    <p>To the fullest extent permitted by law, Aegis is not liable for any indirect, incidental, or consequential damages arising from your use of the Service, including but not limited to loss of profits, data, or business interruption.</p>
    <h2>11. Changes to Terms</h2>
    <p>We may update these Terms from time to time. We will notify you of material changes via email or by posting a notice on the Service. Continued use after changes constitutes acceptance.</p>
    <h2>12. Governing Law</h2>
    <p>These Terms are governed by the laws of India, without regard to its conflict of laws principles. Any disputes shall be resolved in the courts of Mumbai, India.</p>
    <h2>13. Contact</h2>
    <p>For any questions, please email <a href="mailto:hello@aegis.tech">hello@aegis.tech</a>.</p>
    <hr>
    <a href="/dashboard" class="back">← Back to Dashboard</a>
    </body>
    </html>
    '''

# ============================================================
# WEBHOOK (with Redis caching)
# ============================================================
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
    if user and not is_trial_active(user):
        return jsonify({"error": "Trial expired. Please subscribe."}), 402

    org_api_key = None
    org_id = None
    team_rules = None
    if user:
        org_id = user[19] if len(user) > 19 else None
        if org_id:
            try:
                from code_scanner import get_org_api_key, get_org_rules
                org_api_key = get_org_api_key(org_id)
                team_rules = get_org_rules(org_id)
            except ImportError:
                pass

    # CHATBOT & MANUAL COMMANDS
    if event == "issue_comment":
        issue = payload.get("issue", {})
        comment_body = payload.get("comment", {}).get("body", "")
        pr_number = issue.get("number")
        repo_name = payload["repository"]["full_name"]

        if issue.get("pull_request") and comment_body.strip().startswith("/ask"):
            question = comment_body.replace("/ask", "").strip() or "What does this code do?"
            logger.info(f"Chatbot triggered on PR #{pr_number} in {repo_name}")
            try:
                if org_api_key:
                    os.environ['LLM_PROVIDER'] = 'deepseek'
                    os.environ['OPENAI_API_KEY'] = org_api_key
                elif user:
                    os.environ['LLM_PROVIDER'] = user[7]
                    os.environ['OPENAI_API_KEY'] = user[8] or ""
                os.environ['GITHUB_TOKEN'] = user[10] if user else os.getenv("GITHUB_TOKEN")
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                answer = ask_question_about_code(question, diff_content)
                post_comment(repo, pr_number, f"🤖 **AI Chatbot:**\n\n{answer}")
                if org_id:
                    log_audit(org_id, user[0], 'ask', pr_number, repo_name, f"Asked: {question}")
                return jsonify({"msg": "Chatbot replied"}), 200
            except Exception as e:
                logger.error(f"Chatbot Error: {e}")
                return jsonify({"error": str(e)}), 500

        elif issue.get("pull_request") and comment_body.strip().startswith("/fix"):
            logger.info(f"Manual fix triggered on PR #{pr_number} in {repo_name}")
            try:
                if org_api_key:
                    os.environ['LLM_PROVIDER'] = 'deepseek'
                    os.environ['OPENAI_API_KEY'] = org_api_key
                elif user:
                    os.environ['LLM_PROVIDER'] = user[7]
                    os.environ['OPENAI_API_KEY'] = user[8] or ""
                os.environ['GITHUB_TOKEN'] = user[10] if user else os.getenv("GITHUB_TOKEN")
                user_model = user[15] if user and len(user) > 15 else None
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                api_key = user[8] if user else None
                use_mock = not (org_api_key or api_key)
                result = analyze_pr_diff_routed(diff_content, use_mock, user_model, repo, pr, team_rules)
                status = "✅ PASSED" if result['success'] else "❌ FAILED (Needs Review)"
                reply = f"""🤖 **Aegis Auto-Heal Report (Triggered via `/fix`)**

**Status:** {status}

**Functions Detected:**
{extract_changed_functions(diff_content).get('code', '')[:500]}...

**AI Suggested Fix (Diff):**
{result['diff_output'][:1500]}

🔔 *This fix was applied automatically. Review the changes and merge if satisfied.*
"""
                post_comment(repo, pr_number, reply)
                if org_id:
                    log_audit(org_id, user[0] if user else None, 'fix', pr_number, repo_name, f"Status: {status}")
                return jsonify({"msg": "Direct fix posted"}), 200
            except Exception as e:
                logger.error(f"/fix error: {e}")
                return jsonify({"error": str(e)}), 500

        elif issue.get("pull_request") and comment_body.strip().startswith("/fix-ask"):
            logger.info(f"Fix with approval triggered on PR #{pr_number} in {repo_name}")
            try:
                if org_api_key:
                    os.environ['LLM_PROVIDER'] = 'deepseek'
                    os.environ['OPENAI_API_KEY'] = org_api_key
                elif user:
                    os.environ['LLM_PROVIDER'] = user[7]
                    os.environ['OPENAI_API_KEY'] = user[8] or ""
                os.environ['GITHUB_TOKEN'] = user[10] if user else os.getenv("GITHUB_TOKEN")
                user_model = user[15] if user and len(user) > 15 else None
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                api_key = user[8] if user else None
                use_mock = not (org_api_key or api_key)
                result = analyze_pr_diff_routed(diff_content, use_mock, user_model, repo, pr, team_rules)
                status = "✅ PASSED" if result['success'] else "❌ FAILED (Needs Review)"
                if result['fixed_code'] and result['diff_output']:
                    save_pending_fix(pr_number, repo_name, result['fixed_code'], result['diff_output'])
                error_info = result.get('error_log', 'No errors detected.')[:500]
                reply = f"""🤖 **Aegis Auto-Heal Report (Triggered via `/fix-ask`)**

**Status:** {status}

**Functions Detected:**
{extract_changed_functions(diff_content).get('code', '')[:500]}...

**AI Suggested Fix (Diff):**
{result['diff_output'][:1500]}

**Errors Found:**
{error_info}

---
✅ **Approve this fix?** Reply to this comment with `approve` to apply the fix, or `reject` to cancel.
"""
                post_comment(repo, pr_number, reply)
                if org_id:
                    log_audit(org_id, user[0] if user else None, 'fix-ask', pr_number, repo_name, f"Status: {status}")
                return jsonify({"msg": "Pending fix posted"}), 200
            except Exception as e:
                logger.error(f"/fix-ask error: {e}")
                return jsonify({"error": str(e)}), 500

        elif issue.get("pull_request") and comment_body.strip().startswith("/change"):
            instruction = comment_body.replace("/change", "").strip()
            if not instruction:
                post_comment(repo, pr_number, "❌ Please specify what you want to change.")
                return jsonify({"msg": "No instruction"}), 200
            logger.info(f"Code change triggered on PR #{pr_number} in {repo_name}")
            try:
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                try:
                    from code_scanner import process_natural_language_change
                    change_result = process_natural_language_change(instruction, diff_content, user)
                except ImportError:
                    change_result = {'success': False, 'error': 'Process change function not available'}
                if change_result.get('success'):
                    save_pending_fix(pr_number, repo_name, change_result['fixed_code'], change_result['diff_output'])
                    reply = f"""🤖 **Aegis Code Change (Triggered via `/change`)**

**Instruction:** *"{instruction}"*

**Changes Made:**
{change_result.get('description', 'Code updated.')}

**Diff:**
{change_result.get('diff_output', '')[:1500]}

---
✅ **Approve this change?** Reply with `approve` or `reject`.
"""
                    post_comment(repo, pr_number, reply)
                    if org_id:
                        log_audit(org_id, user[0] if user else None, 'change', pr_number, repo_name, f"Change: {instruction}")
                    return jsonify({"msg": "Pending change posted"}), 200
                else:
                    post_comment(repo, pr_number, f"❌ Failed: {change_result.get('error', 'Unknown error')}")
                    return jsonify({"error": change_result.get('error')}), 500
            except Exception as e:
                logger.error(f"/change error: {e}")
                return jsonify({"error": str(e)}), 500

        elif issue.get("pull_request") and comment_body.strip().lower() in ["approve", "reject"]:
            decision = comment_body.strip().lower()
            pending = get_pending_fix(pr_number, repo_name)
            if pending:
                fix_id, fixed_code, diff_output = pending
                if decision == "approve":
                    reply = f"""✅ **Fix Approved!**\n\nHere is the applied fix (diff):\n```\n{diff_output}\n```\n🔔 *Review and merge if satisfied.*"""
                    update_pending_fix_status(fix_id, 'approved')
                    post_comment(repo, pr_number, reply)
                    if org_id:
                        log_audit(org_id, user[0] if user else None, 'approve', pr_number, repo_name, "Approved fix")
                else:
                    reply = f"""❌ **Fix Rejected.**\n\nNo changes were made. Run `/fix-ask` again to generate a new suggestion."""
                    update_pending_fix_status(fix_id, 'rejected')
                    post_comment(repo, pr_number, reply)
                    if org_id:
                        log_audit(org_id, user[0] if user else None, 'reject', pr_number, repo_name, "Rejected fix")
                return jsonify({"msg": f"Fix {decision}ed"}), 200
            else:
                post_comment(repo, pr_number, "❌ No pending fix found. Run `/fix-ask` or `/change` first.")
                return jsonify({"msg": "No pending fix"}), 200

        return jsonify({"msg": "Ignored comment"}), 200

    # PULL REQUEST (AUTO-HEAL + ROI LOGGING)
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize"]:
        pr_number = payload["number"]
        logger.info(f"Processing PR #{pr_number} in {repo_name}")
        try:
            if org_api_key:
                os.environ['LLM_PROVIDER'] = 'deepseek'
                os.environ['OPENAI_API_KEY'] = org_api_key
            elif user:
                os.environ['LLM_PROVIDER'] = user[7]
                os.environ['OPENAI_API_KEY'] = user[8] or ""
            os.environ['GITHUB_TOKEN'] = user[10] if user else os.getenv("GITHUB_TOKEN")

            diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
            api_key = user[8] if user else None
            use_mock = not (org_api_key or api_key)
            user_model = user[15] if user and len(user) > 15 else None
            result = analyze_pr_diff_routed(diff_content, use_mock, user_model, repo, pr, team_rules)
            status = "✅ PASSED" if result['success'] else "❌ FAILED (Needs Review)"
            comment = f"""🤖 **AI QA Report for PR #{pr_number}**

**Status:** {status}

**Functions Detected:**
{extract_changed_functions(diff_content).get('code', '')[:500]}...

**AI Suggested Fix (Diff):**
{result['diff_output'][:1500]}

🔔 *This is an automated analysis. Please review the suggested changes.*
"""
            post_comment(repo, pr_number, comment)

            bugs_found = 1 if result.get('diff_output') and len(result['diff_output']) > 50 else 0
            time_saved = bugs_found * 10
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('''
                    INSERT INTO pr_analytics (user_id, org_id, pr_number, repo_name, bugs_found, time_saved_minutes)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (user[0] if user else None, org_id, pr_number, repo_name, bugs_found, time_saved))
                conn.commit()
                conn.close()
                logger.info(f"📊 Logged analytics for PR #{pr_number}")
            except Exception as e:
                logger.warning(f"Could not log analytics: {e}")

            if org_id:
                log_audit(org_id, user[0] if user else None, 'auto-fix', pr_number, repo_name, f"Status: {status}")
            return jsonify({"msg": "PR processed"}), 200
        except Exception as e:
            logger.error(f"Error processing PR #{pr_number}: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"msg": "Ignored"}), 200

# ============================================================
# RUN APP
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)