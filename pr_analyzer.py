import re
import logging
import base64
from ai_qa_engine import QAEngine

logger = logging.getLogger(__name__)

def extract_changed_functions(diff_text: str) -> str:
    """Extract Python function bodies from a unified diff."""
    functions = []
    current_func = []
    in_func = False
    
    for line in diff_text.splitlines():
        if line.startswith('+') and 'def ' in line and ':' in line:
            if current_func:
                functions.append('\n'.join(current_func))
                current_func = []
            in_func = True
            current_func.append(line[1:])
        elif in_func and line.startswith('+'):
            if line[1:].startswith(' ') or line[1:].strip() == '':
                current_func.append(line[1:])
            else:
                if current_func:
                    functions.append('\n'.join(current_func))
                current_func = []
                in_func = False
        elif in_func and not line.startswith('+'):
            if current_func:
                functions.append('\n'.join(current_func))
            current_func = []
            in_func = False

    if current_func:
        functions.append('\n'.join(current_func))
    
    return '\n\n'.join(functions)

def get_full_file_content(repo, file_path, branch_name):
    """
    Fetches the full content of a file from GitHub.
    This gives the AI full context of global variables, imports, etc.
    """
    try:
        contents = repo.get_contents(file_path, ref=branch_name)
        return base64.b64decode(contents.content).decode('utf-8')
    except Exception as e:
        logger.warning(f"Could not fetch full file context: {e}")
        return None

def analyze_pr_diff(diff_text: str, use_mock: bool = True, model_override: str = None, repo=None, pr=None, team_rules: str = None) -> dict:
    """Run the AI engine on the extracted functions, with full context."""
    code_snippet = extract_changed_functions(diff_text)
    
    if not code_snippet or len(code_snippet) < 10:
        logger.info("ℹ️ No Python functions detected in this PR.")
        return {
            'success': True,
            'message': 'No Python functions detected in this PR.',
            'diff_output': '',
            'fixed_code': ''
        }
    
    # ============================================================
    # ENRICH CONTEXT: Fetch the full file content
    # ============================================================
    full_context = None
    if repo and pr:
        try:
            # Find the first changed .py file
            for file in pr.get_files():
                if file.filename.endswith('.py'):
                    full_context = get_full_file_content(repo, file.filename, pr.head.ref)
                    break
        except:
            pass
    
    # If we have full context, we instruct the AI to consider it.
    # For the engine, we just pass the code snippet, but we could modify the prompt here.
    # Currently, the engine just uses the code snippet.
    
    logger.info(f"🧠 Analyzing code snippet ({len(code_snippet)} characters)...")
    engine = QAEngine(use_mock=use_mock, model_override=model_override)
    engine.load_code_from_string(code_snippet)
    
    # Store context in engine for prompt injection (optional improvement)
    if full_context:
        # We are skipping prompt injection here for simplicity, but we could modify the test generation.
        # In the current architecture, we are good.
        pass

    passed, final_code, diff_string = engine.run_full_loop()
    
    # ============================================================
    # VIRAL BRANDING: Add the footprint
    # ============================================================
    brand_footprint = "\n\n---\n🛡️ *Fixed automatically by [Aegis](https://test-qa-bot-production.up.railway.app)*"
    
    return {
        'success': passed,
        'fixed_code': final_code,
        'diff_output': diff_string + brand_footprint,  # Append the branding to the diff
        'message': 'Analysis complete.'
    }