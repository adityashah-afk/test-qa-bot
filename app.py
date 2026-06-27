#!/usr/bin/env python3
"""
Aegis - Complete Market Ready Backend
Includes: 14-Day Trial Expiry, Referral System, Try-It-Now Demo, Chatbot, Auto-Heal
TEAM PLAN: Centralized Keys, Audit Logs, Team Rules
"""

import os
import logging
import sqlite3
import hmac
import secrets
import string
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, url_for, session, flash, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import stripe
from dotenv import load_dotenv
load_dotenv()

from github_client import get_pr_diff, post_comment
from pr_analyzer import analyze_pr_diff, extract_changed_functions
from code_scanner import ask_question_about_code

# ============================================================
# Logging
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
YOUR_DOMAIN = "https://test-qa-bot-production.up.railway.app"

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_OAUTH_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# ============================================================
# Database
# ============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), 'aegis.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Users table (with org_id and role)
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            model TEXT DEFAULT '',
            trial_expires_at TIMESTAMP,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            org_id INTEGER,
            role TEXT DEFAULT 'member'
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
    # ============================================================
    # TEAM / ENTERPRISE TABLES
    # ============================================================
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
    conn.commit()
    conn.close()
    logger.info("Database initialized with Team/Enterprise tables.")

def migrate_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Add missing columns if needed
    try:
        c.execute("ALTER TABLE users ADD COLUMN model TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN trial_expires_at TIMESTAMP")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT UNIQUE")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN org_id INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'member'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()

init_db()
migrate_db()

# ============================================================
# Helpers
# ============================================================
def generate_referral_code():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))

def is_trial_active(user):
    if not user:
        return False
    if user[9] == 'active':  # subscription_status
        return True
    trial_expires_at = user[12] if len(user) > 12 else None
    if trial_expires_at:
        try:
            expiry = datetime.strptime(trial_expires_at, '%Y-%m-%d %H:%M:%S')
            return datetime.utcnow() < expiry
        except:
            return False
    return False

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

def get_user_by_referral_code(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE referral_code = ?', (code,))
    user = c.fetchone()
    conn.close()
    return user

def count_referrals(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ?', (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def add_referral(referrer_id, referred_user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO referrals (referrer_id, referred_user_id) VALUES (?, ?)', (referrer_id, referred_user_id))
    conn.commit()
    conn.close()

def update_user_settings(user_id, provider=None, api_key=None, repo_name=None, github_token=None, model=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE users
        SET provider = COALESCE(?, provider),
            api_key = COALESCE(?, api_key),
            repo_name = COALESCE(?, repo_name),
            github_token = COALESCE(?, github_token),
            model = COALESCE(?, model)
        WHERE id = ?
    ''', (provider, api_key, repo_name, github_token, model, user_id))
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

def save_pending_fix(pr_number, repo_name, fixed_code, diff_output):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO pending_fixes (pr_number, repo_name, fixed_code, diff_output, status)
        VALUES (?, ?, ?, ?, 'pending')
    ''', (pr_number, repo_name, fixed_code, diff_output))
    conn.commit()
    conn.close()

def get_pending_fix(pr_number, repo_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, fixed_code, diff_output FROM pending_fixes
        WHERE pr_number = ? AND repo_name = ? AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    ''', (pr_number, repo_name))
    row = c.fetchone()
    conn.close()
    return row

def update_pending_fix_status(fix_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE pending_fixes SET status = ? WHERE id = ?', (status, fix_id))
    conn.commit()
    conn.close()

def process_natural_language_change(instruction, diff_text, user):
    from ai_qa_engine import QAEngine
    current_code = extract_changed_functions(diff_text)
    if not current_code:
        return {'success': False, 'error': 'No code detected in this PR.'}
    api_key = user[4] if user else None
    use_mock = not api_key
    model_override = user[11] if user and len(user) > 11 else None
    engine = QAEngine(use_mock=use_mock, model_override=model_override)
    llm = engine.llm
    if not llm and not use_mock:
        return {'success': False, 'error': 'No AI provider configured.'}
    prompt = f"""
You are an AI code assistant. The user wants to make the following change:
Instruction: {instruction}
Here is the current code:Apply the change and return ONLY the corrected code. Make sure the code is syntactically correct.
"""
    try:
        fixed_code = llm.generate(prompt) if llm else current_code
        if use_mock:
            fixed_code = current_code
        diff_output = engine.generate_diff(current_code, fixed_code)
        return {
            'success': True,
            'fixed_code': fixed_code,
            'diff_output': diff_output,
            'description': f'Applied change: "{instruction}"',
            'error': None
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}

# ============================================================
# TEAM / ENTERPRISE HELPERS
# ============================================================
def get_org_settings(org_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT setting_key, setting_value FROM org_settings WHERE org_id = ?', (org_id,))
    rows = c.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

def get_org_api_key(org_id):
    settings = get_org_settings(org_id)
    return settings.get('deepseek_api_key') or settings.get('openai_api_key') or None

def get_org_rules(org_id):
    settings = get_org_settings(org_id)
    return settings.get('testing_rules', '')

def log_audit(org_id, user_id, action, pr_number=None, repo_name=None, details=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO audit_logs (org_id, user_id, action, pr_number, repo_name, details)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (org_id, user_id, action, pr_number, repo_name, details))
    conn.commit()
    conn.close()

# ============================================================
# Webhook Verification
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

# ============================================================
# TRY-IT-NOW DEMO
# ============================================================
@app.route("/try", methods=['GET', 'POST'])
def try_endpoint():
    if request.method == 'GET':
        return '''
        <!DOCTYPE html>
        <html>
        <head><title>Aegis - Try It Now</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body { background: #0a0a0a; color: white; font-family: sans-serif; }
            .container { max-width: 800px; margin: 50px auto; padding: 20px; }
            textarea { width: 100%; height: 200px; background: #1a1a1a; border: 1px solid #2a2a2a; color: white; padding: 10px; border-radius: 8px; font-family: monospace; }
            button { background: #3b82f6; color: white; border: none; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: bold; }
            #result { margin-top: 20px; white-space: pre-wrap; background: #1a1a1a; padding: 15px; border-radius: 8px; border: 1px solid #2a2a2a; display: none; }
        </style>
        </head>
        <body>
            <div class="container">
                <h1>⚡ Try Aegis</h1>
                <p>Paste your Python code below and see Aegis find edge-case bugs instantly.</p>
                <form id="try-form">
                    <textarea id="code" placeholder="def divide(a,b): return a/b" required></textarea>
                    <br><br>
                    <button type="submit">Analyze Code</button>
                </form>
                <div id="result"></div>
            </div>
            <script>
                document.getElementById('try-form').addEventListener('submit', async function(e) {
                    e.preventDefault();
                    const code = document.getElementById('code').value;
                    if (!code.trim()) return;
                    const resultDiv = document.getElementById('result');
                    resultDiv.style.display = 'block';
                    resultDiv.textContent = '⏳ Analyzing...';
                    try {
                        const resp = await fetch('/try', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
                        const data = await resp.json();
                        if (data.error) { resultDiv.textContent = '❌ ' + data.error; }
                        else { resultDiv.textContent = data.diff + '\n\n✅ ' + data.message; }
                    } catch (err) { resultDiv.textContent = '❌ Error: ' + err.message; }
                });
            </script>
        </body>
        </html>
        '''
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
            return jsonify({
                'diff': diff_output,
                'message': 'Fix generated. ' + ('✅ PASSED' if passed else '❌ FAILED (Needs Review)')
            })
        else:
            return jsonify({
                'diff': 'No changes needed (or mock mode limited)',
                'message': '✅ Code looks good (mock mode)'
            })

# ============================================================
# WEBHOOK (Full version with Team Support)
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
    if user:
        if not is_trial_active(user):
            logger.warning(f"Trial expired for user {user[1]}. Blocking webhook.")
            return jsonify({"error": "Trial expired. Please subscribe."}), 402
    else:
        logger.warning(f"No user found for repo {repo_name}, falling back to env tokens.")
        user = None

    # ============================================================
    # TEAM LOGIC: Check for Organization Key
    # ============================================================
    org_api_key = None
    org_id = None
    team_rules = None
    if user:
        org_id = user[14] if len(user) > 14 else None
        if org_id:
            org_api_key = get_org_api_key(org_id)
            team_rules = get_org_rules(org_id)
            if org_api_key:
                logger.info(f"Using Organization API Key for Org ID {org_id}")

    # ============================================================
    # CHATBOT & MANUAL COMMANDS
    # ============================================================
    if event == "issue_comment":
        issue = payload.get("issue", {})
        comment_body = payload.get("comment", {}).get("body", "")
        pr_number = issue.get("number")
        repo_name = payload["repository"]["full_name"]

        if issue.get("pull_request") and comment_body.strip().startswith("/ask"):
            question = comment_body.replace("/ask", "").strip() or "What does this code do?"
            logger.info(f"Chatbot triggered on PR #{pr_number} in {repo_name}")
            try:
                # Set keys (prefer Org key over User key)
                if org_api_key:
                    os.environ['LLM_PROVIDER'] = 'deepseek'
                    os.environ['OPENAI_API_KEY'] = org_api_key
                elif user:
                    os.environ['LLM_PROVIDER'] = user[3]
                    os.environ['OPENAI_API_KEY'] = user[4] or ""
                os.environ['GITHUB_TOKEN'] = user[6] if user else os.getenv("GITHUB_TOKEN")
                
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                answer = ask_question_about_code(question, diff_content)
                post_comment(repo, pr_number, f"🤖 **AI Chatbot:**\n\n{answer}")
                
                # Audit Log
                if org_id:
                    log_audit(org_id, user[0], 'ask', pr_number, repo_name, f"Asked: {question}")
                
                logger.info(f"Chatbot replied to PR #{pr_number}")
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
                    os.environ['LLM_PROVIDER'] = user[3]
                    os.environ['OPENAI_API_KEY'] = user[4] or ""
                os.environ['GITHUB_TOKEN'] = user[6] if user else os.getenv("GITHUB_TOKEN")
                
                user_model = user[11] if user and len(user) > 11 else None
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                api_key = user[4] if user else None
                use_mock = not (org_api_key or api_key)
                result = analyze_pr_diff(diff_content, use_mock=use_mock, model_override=user_model, repo=repo, pr=pr, team_rules=team_rules)
                status = "✅ PASSED" if result['success'] else "❌ FAILED (Needs Review)"
                reply = f"""🤖 **Aegis Auto-Heal Report (Triggered via `/fix`)**

**Status:** {status}

**Functions Detected:**
{extract_changed_functions(diff_content)[:500]}...

**AI Suggested Fix (Diff):**
{result['diff_output'][:1500]}

🔔 *This fix was applied automatically. Review the changes and merge if satisfied.*
"""
                post_comment(repo, pr_number, reply)
                if org_id:
                    log_audit(org_id, user[0] if user else None, 'fix', pr_number, repo_name, f"Status: {status}")
                logger.info(f"Direct fix comment posted to PR #{pr_number}")
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
                    os.environ['LLM_PROVIDER'] = user[3]
                    os.environ['OPENAI_API_KEY'] = user[4] or ""
                os.environ['GITHUB_TOKEN'] = user[6] if user else os.getenv("GITHUB_TOKEN")
                
                user_model = user[11] if user and len(user) > 11 else None
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                api_key = user[4] if user else None
                use_mock = not (org_api_key or api_key)
                result = analyze_pr_diff(diff_content, use_mock=use_mock, model_override=user_model, repo=repo, pr=pr, team_rules=team_rules)
                status = "✅ PASSED" if result['success'] else "❌ FAILED (Needs Review)"

                if result['fixed_code'] and result['diff_output']:
                    save_pending_fix(pr_number, repo_name, result['fixed_code'], result['diff_output'])

                error_info = result.get('error_log', 'No errors detected (or the test passed!).')[:500]
                reply = f"""🤖 **Aegis Auto-Heal Report (Triggered via `/fix-ask`)**

**Status:** {status}

**Functions Detected:**
{extract_changed_functions(diff_content)[:500]}...

**AI Suggested Fix (Diff):**
{result['diff_output'][:1500]}

**Errors Found:**
{error_info}

---
✅ **Approve this fix?** Reply to this comment with `approve` to apply the fix, or `reject` to cancel.

🔔 *This fix was manually triggered. You have 24 hours to approve or reject.*
"""
                post_comment(repo, pr_number, reply)
                if org_id:
                    log_audit(org_id, user[0] if user else None, 'fix-ask', pr_number, repo_name, f"Status: {status}")
                logger.info(f"Pending fix (with errors) posted to PR #{pr_number}")
                return jsonify({"msg": "Pending fix with errors posted"}), 200

            except Exception as e:
                logger.error(f"/fix-ask error: {e}")
                return jsonify({"error": str(e)}), 500

        elif issue.get("pull_request") and comment_body.strip().startswith("/change"):
            instruction = comment_body.replace("/change", "").strip()
            if not instruction:
                post_comment(repo, pr_number, "❌ Please specify what you want to change. Example: `/change Change the theme from blue to black`.")
                return jsonify({"msg": "No instruction provided"}), 200

            logger.info(f"Code change instruction triggered on PR #{pr_number} in {repo_name}")
            try:
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                change_result = process_natural_language_change(instruction, diff_content, user)

                if change_result['success']:
                    save_pending_fix(pr_number, repo_name, change_result['fixed_code'], change_result['diff_output'])
                    reply = f"""🤖 **Aegis Code Change (Triggered via `/change`)**

**Instruction:** *"{instruction}"*

**Changes Made:**
{change_result['description']}

**Diff:**
{change_result['diff_output'][:1500]}

---
✅ **Approve this change?** Reply to this comment with `approve` to apply the change, or `reject` to cancel.

🔔 *This change was manually triggered. You have 24 hours to approve or reject.*
"""
                    post_comment(repo, pr_number, reply)
                    if org_id:
                        log_audit(org_id, user[0] if user else None, 'change', pr_number, repo_name, f"Change: {instruction}")
                    logger.info(f"Pending change posted to PR #{pr_number}")
                    return jsonify({"msg": "Pending change posted"}), 200
                else:
                    post_comment(repo, pr_number, f"❌ Failed to process the change: {change_result['error']}")
                    return jsonify({"error": change_result['error']}), 500

            except Exception as e:
                logger.error(f"/change error: {e}")
                return jsonify({"error": str(e)}), 500

        elif issue.get("pull_request") and comment_body.strip().lower() in ["approve", "reject"]:
            decision = comment_body.strip().lower()
            logger.info(f"Approval decision '{decision}' on PR #{pr_number} in {repo_name}")

            pending = get_pending_fix(pr_number, repo_name)
            if pending:
                fix_id, fixed_code, diff_output = pending
                if decision == "approve":
                    reply = f"""✅ **Fix Approved!**

Here is the applied fix (diff):
🔔 *The fix has been generated. Please review the changes and merge if satisfied.*
"""
                    update_pending_fix_status(fix_id, 'approved')
                    post_comment(repo, pr_number, reply)
                    if org_id:
                        log_audit(org_id, user[0] if user else None, 'approve', pr_number, repo_name, "Approved fix")
                    logger.info(f"Fix approved and posted to PR #{pr_number}")
                else:
                    reply = f"""❌ **Fix Rejected.**

The AI fix has been cancelled. No changes were made.

🔔 *You can always run `/fix-ask` or `/change` again to generate a new suggestion.*
"""
                    update_pending_fix_status(fix_id, 'rejected')
                    post_comment(repo, pr_number, reply)
                    if org_id:
                        log_audit(org_id, user[0] if user else None, 'reject', pr_number, repo_name, "Rejected fix")
                    logger.info(f"Fix rejected on PR #{pr_number}")
                return jsonify({"msg": f"Fix {decision}ed"}), 200
            else:
                post_comment(repo, pr_number, "❌ No pending fix found for this PR. Run `/fix-ask` or `/change` first to generate one.")
                return jsonify({"msg": "No pending fix"}), 200

        return jsonify({"msg": "Ignored comment"}), 200

    # ============================================================
    # AUTOMATIC WEBHOOK (Pull Request)
    # ============================================================
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize"]:
        pr_number = payload["number"]
        logger.info(f"Processing PR #{pr_number} in {repo_name}")
        try:
            # Set keys (prefer Org key over User key)
            if org_api_key:
                os.environ['LLM_PROVIDER'] = 'deepseek'
                os.environ['OPENAI_API_KEY'] = org_api_key
            elif user:
                os.environ['LLM_PROVIDER'] = user[3]
                os.environ['OPENAI_API_KEY'] = user[4] or ""
            os.environ['GITHUB_TOKEN'] = user[6] if user else os.getenv("GITHUB_TOKEN")

            diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
            api_key = user[4] if user else None
            use_mock = not (org_api_key or api_key)
            user_model = user[11] if user and len(user) > 11 else None
            result = analyze_pr_diff(diff_content, use_mock=use_mock, model_override=user_model, repo=repo, pr=pr, team_rules=team_rules)
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
            if org_id:
                log_audit(org_id, user[0] if user else None, 'auto-fix', pr_number, repo_name, f"Status: {status}")
            logger.info(f"Comment posted to PR #{pr_number}")
            return jsonify({"msg": "PR processed"}), 200
        except Exception as e:
            logger.error(f"Error: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"msg": "Ignored"}), 200

# ============================================================
# Frontend
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
        referral_code = request.form.get('ref')
        if not username or not password:
            flash('Username and password required')
            return redirect(url_for('signup'))
        password_hash = generate_password_hash(password)
        trial_expiry = (datetime.utcnow() + timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
        my_code = generate_referral_code()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO users (username, password_hash, trial_expires_at, referral_code)
                VALUES (?, ?, ?, ?)
            ''', (username, password_hash, trial_expiry, my_code))
            user_id = c.lastrowid
            if referral_code:
                referrer = get_user_by_referral_code(referral_code)
                if referrer:
                    c.execute('UPDATE users SET referred_by = ? WHERE id = ?', (referrer[0], user_id))
                    add_referral(referrer[0], user_id)
            conn.commit()
            conn.close()
            flash('Account created! Your 14-day trial starts now.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists.')
            return redirect(url_for('signup'))
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
        </style>
        </head>
        <body class="min-h-screen flex items-center justify-center">
            <div class="card p-8 rounded-2xl max-w-md w-full">
                <h1 class="text-2xl font-bold mb-6 text-white">Start Your 14-Day Trial</h1>
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" class="input-dark mb-4" />
                    <input type="password" name="password" placeholder="Password" class="input-dark mb-4" />
                    <input type="hidden" name="ref" value="{{ request.args.get('ref') or '' }}" />
                    <button type="submit" class="btn-primary">Start Free Trial</button>
                </form>
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
            error = 'Invalid username or password.'
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
        </style>
        </head>
        <body class="min-h-screen flex items-center justify-center">
            <div class="card p-8 rounded-2xl max-w-md w-full">
                <h1 class="text-2xl font-bold mb-6 text-white">Log In</h1>
                {f'<p class="text-red-400 text-sm mb-4">{error}</p>' if error else ''}
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" class="input-dark mb-4" />
                    <input type="password" name="password" placeholder="Password" class="input-dark mb-4" />
                    <button type="submit" class="btn-primary">Log In</button>
                </form>
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
# OAuth & Stripe
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

@app.route("/create-checkout-session", methods=['POST'])
def create_checkout_session():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404
    PRICE_IDS = {'monthly': os.getenv("STRIPE_PRICE_MONTHLY")}
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

# ============================================================
# TEAM ROUTES
# ============================================================
@app.route("/team-settings", methods=['POST'])
def team_settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = get_user_by_id(session['user_id'])
    org_id = user[14] if len(user) > 14 else None
    
    if not org_id:
        flash("You are not part of an organization. Please upgrade to the Team plan.")
        return redirect(url_for('dashboard'))
    
    org_api_key = request.form.get('org_api_key')
    testing_rules = request.form.get('testing_rules')
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if org_api_key:
        c.execute('''
            INSERT INTO org_settings (org_id, setting_key, setting_value)
            VALUES (?, 'deepseek_api_key', ?)
            ON CONFLICT(org_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value
        ''', (org_id, org_api_key))
    if testing_rules:
        c.execute('''
            INSERT INTO org_settings (org_id, setting_key, setting_value)
            VALUES (?, 'testing_rules', ?)
            ON CONFLICT(org_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value
        ''', (org_id, testing_rules))
    conn.commit()
    conn.close()
    
    flash("Team settings saved!")
    return redirect(url_for('dashboard'))

@app.route("/audit-logs")
def audit_logs():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = get_user_by_id(session['user_id'])
    org_id = user[14] if len(user) > 14 else None
    
    if not org_id:
        flash("No organization found.")
        return redirect(url_for('dashboard'))
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT u.username, a.action, a.pr_number, a.repo_name, a.details, a.timestamp
        FROM audit_logs a
        JOIN users u ON a.user_id = u.id
        WHERE a.org_id = ?
        ORDER BY a.timestamp DESC
        LIMIT 50
    ''', (org_id,))
    logs = c.fetchall()
    conn.close()
    
    # Render a simple table (you can upgrade this to a proper HTML template later)
    html = "<h1>Audit Logs</h1><table border='1'><tr><th>User</th><th>Action</th><th>PR</th><th>Repo</th><th>Details</th><th>Time</th></tr>"
    for log in logs:
        html += f"<tr><td>{log[0]}</td><td>{log[1]}</td><td>{log[2]}</td><td>{log[3]}</td><td>{log[4]}</td><td>{log[5]}</td></tr>"
    html += "</table><a href='/dashboard'>Back to Dashboard</a>"
    return html

# ============================================================
# DASHBOARD
# ============================================================
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
        model = request.form.get('model')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            UPDATE users SET provider = ?, api_key = ?, repo_name = ?, model = ?
            WHERE id = ?
        ''', (provider, api_key, repo_name, model, user_id))
        conn.commit()
        conn.close()
        flash('Settings saved!')
        return redirect(url_for('dashboard'))

    referral_count = count_referrals(user_id)
    referral_link = f"{YOUR_DOMAIN}/signup?ref={user[13]}"  # referral_code is at index 13
    html = load_html('dashboard.html')
    # Inject dynamic values
    html = html.replace('value="deepseek"', f'value="{user[3]}" selected' if user[3] == 'deepseek' else 'value="deepseek"')
    html = html.replace('placeholder="owner/repo"', f'value="{user[5] or ""}"')
    html = html.replace('placeholder="sk-..."', f'value="{user[4] or ""}"')
    html = html.replace('placeholder="e.g. gpt-4o, claude-3-5-sonnet..."', f'value="{user[11] or ""}"')
    # Subscription status
    sub_status = user[9] or 'inactive'
    if sub_status == 'active':
        html = html.replace('Billing Status: Inactive', 'Billing Status: ✅ Active')
    else:
        trial_expires_at = user[12] if len(user) > 12 else None
        if trial_expires_at:
            try:
                expiry = datetime.strptime(trial_expires_at, '%Y-%m-%d %H:%M:%S')
                if datetime.utcnow() < expiry:
                    html = html.replace('Billing Status: Inactive', f'Billing Status: ⏳ Trial active (expires {expiry.strftime("%b %d")})')
                else:
                    html = html.replace('Billing Status: Inactive', 'Billing Status: ❌ Trial Expired - Subscribe!')
            except:
                html = html.replace('Billing Status: Inactive', 'Billing Status: ❌ Trial Expired')
        else:
            html = html.replace('Billing Status: Inactive', 'Billing Status: ❌ Trial Expired')
    github_status = '✅ Connected' if user[6] else '❌ Not Connected'
    html = html.replace('GitHub Status: Not Connected', f'GitHub Status: {github_status}')
    # Referral section
    html = html.replace('{{ referral_link }}', referral_link)
    html = html.replace('{{ referral_count }}', str(referral_count))
    return html

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)