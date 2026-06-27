#!/usr/bin/env python3
"""
AI QA Engine - GitHub Webhook Server
Production-Ready Version (No Hardcoded Secrets)
"""

# ============================================================
# 1. LOAD ENVIRONMENT VARIABLES FIRST
# ============================================================
import logging
from dotenv import load_dotenv
load_dotenv()

import os
import hmac
import hashlib
from flask import Flask, request, jsonify
from github_client import get_pr_diff, post_comment
from pr_analyzer import analyze_pr_diff, extract_changed_functions
from code_scanner import ask_question_about_code

# ============================================================
# 2. LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# 3. CONFIGURATION (Strictly from Environment)
# ============================================================
app = Flask(__name__)

GITHUB_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Ensure required secrets are present
if not GITHUB_SECRET:
    logger.warning("⚠️ GITHUB_WEBHOOK_SECRET is not set! Webhook verification will fail.")
if not GITHUB_TOKEN:
    logger.warning("⚠️ GITHUB_TOKEN is not set! Cannot post comments.")

# ============================================================
# 4. SECURITY: Signature Verification (ENABLED)
# ============================================================
def verify_signature(payload_body, signature_header):
    if not signature_header or not GITHUB_SECRET:
        return False
    hash_object = hmac.new(GITHUB_SECRET.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected, signature_header)

# ============================================================
# 5. HEALTH CHECK (For Uptime Monitoring)
# ============================================================
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "message": "Aegis is running"}), 200

# ============================================================
# 6. MAIN WEBHOOK
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(request.data, signature):
        logger.warning("⚠️ Invalid signature received from %s", request.remote_addr)
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event")
    payload = request.get_json()

    if event == "ping":
        return jsonify({"msg": "pong"}), 200

    # ============================================================
    # CHATBOT: Handle PR comments that start with /ask
    # ============================================================
    if event == "issue_comment":
        issue = payload.get("issue", {})
        comment_body = payload.get("comment", {}).get("body", "")
        pr_number = issue.get("number")
        repo_name = payload["repository"]["full_name"]

        if issue.get("pull_request") and comment_body.strip().startswith("/ask"):
            logger.info("💬 Chatbot triggered on PR #%s in %s", pr_number, repo_name)
            question = comment_body.replace("/ask", "").strip() or "What does this code do?"
            
            try:
                diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
                answer = ask_question_about_code(question, diff_content)
                post_comment(repo, pr_number, f"🤖 **AI Chatbot:**\n\n{answer}")
                logger.info("✅ Chatbot replied to PR #%s", pr_number)
                return jsonify({"msg": "Chatbot replied"}), 200
            except Exception as e:
                logger.error("❌ Chatbot Error: %s", str(e))
                return jsonify({"error": str(e)}), 500
        
        return jsonify({"msg": "Ignored comment"}), 200

    # ============================================================
    # MAIN QA ENGINE: Handle Pull Request events
    # ============================================================
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize"]:
        pr_number = payload["number"]
        repo_name = payload["repository"]["full_name"]
        
        logger.info("🔍 Processing PR #%s in %s", pr_number, repo_name)
        
        try:
            diff_content, repo, pr = get_pr_diff(repo_name, pr_number)
            
            # Automatically use Mock Mode if no API key
            use_mock = not OPENAI_API_KEY
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
            logger.info("✅ Comment posted to PR #%s", pr_number)
            return jsonify({"msg": "PR processed"}), 200
            
        except Exception as e:
            logger.error("❌ Error processing PR #%s: %s", pr_number, str(e))
            return jsonify({"error": str(e)}), 500

    return jsonify({"msg": "Ignored"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)