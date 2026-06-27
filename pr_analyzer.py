import re
import logging
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

def analyze_pr_diff(diff_text: str, use_mock: bool = True, model_override: str = None) -> dict:
    """Run the AI engine on the extracted functions, with optional model override."""
    code_snippet = extract_changed_functions(diff_text)
    
    if not code_snippet or len(code_snippet) < 10:
        logger.info("ℹ️ No Python functions detected in this PR.")
        return {
            'success': True,
            'message': 'No Python functions detected in this PR.',
            'diff_output': '',
            'fixed_code': ''
        }
    
    logger.info(f"🧠 Analyzing code snippet ({len(code_snippet)} characters)...")
    engine = QAEngine(use_mock=use_mock, model_override=model_override)  # <-- Pass model here
    engine.load_code_from_string(code_snippet)
    
    passed, final_code, diff_string = engine.run_full_loop()
    
    return {
        'success': passed,
        'fixed_code': final_code,
        'diff_output': diff_string,
        'message': 'Analysis complete.'
    }