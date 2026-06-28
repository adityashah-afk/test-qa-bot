import re
import logging
import base64
from ai_qa_engine import QAEngine

logger = logging.getLogger(__name__)

def extract_changed_functions(diff_text: str) -> dict:
    lines = diff_text.splitlines()
    context = []
    in_func = False
    func_name = ""
    func_start = 0

    for i, line in enumerate(lines):
        if line.startswith('+') and 'def ' in line and ':' in line:
            in_func = True
            func_name = line[1:].strip()
            func_start = i
            for j in range(max(0, i - 5), i):
                if lines[j].startswith('+') or lines[j].startswith(' '):
                    context.append(lines[j][1:] if lines[j].startswith('+') else lines[j])
            break

    if not in_func:
        return {'code': '', 'context': '', 'func_name': ''}

    body = []
    for i in range(func_start + 1, min(len(lines), func_start + 30)):
        line = lines[i]
        if line.startswith('+'):
            body.append(line[1:])
        elif in_func and not line.startswith('+'):
            break
    body_str = '\n'.join(body)

    return {
        'code': body_str,
        'context': '\n'.join(context),
        'func_name': func_name
    }

def get_full_file_content(repo, file_path, branch_name):
    try:
        contents = repo.get_contents(file_path, ref=branch_name)
        return base64.b64decode(contents.content).decode('utf-8')
    except Exception as e:
        logger.warning(f"Could not fetch full file: {e}")
        return None

def analyze_pr_diff(diff_text: str, use_mock: bool = True, model_override: str = None, repo=None, pr=None, team_rules: str = None) -> dict:
    extracted = extract_changed_functions(diff_text)
    code_snippet = extracted['code']
    func_name = extracted['func_name']

    if not code_snippet or len(code_snippet) < 10:
        logger.info("ℹ️ No Python functions detected.")
        return {'success': True, 'message': 'No functions detected.', 'diff_output': '', 'fixed_code': ''}

    # Get some imports for context (optional)
    imports = []
    if repo and pr:
        for file in pr.get_files():
            if file.filename.endswith('.py'):
                full_content = get_full_file_content(repo, file.filename, pr.head.ref)
                if full_content:
                    import_lines = [line for line in full_content.splitlines() if line.startswith(('import ', 'from '))]
                    imports = import_lines[:5]  # Limit to 5 to save tokens
                break

    logger.info(f"🧠 Analyzing optimized snippet ({len(code_snippet)} chars)...")
    engine = QAEngine(use_mock=use_mock, model_override=model_override)
    engine.load_code_from_string(code_snippet)

    # ============================================================
    # We override the generate_test method using a simple function
    # that is defined INSIDE this function, with proper indentation.
    # ============================================================
    def generate_test_func(code_snippet):
        if use_mock:
            return """
import pytest
from buggy_code import calculate_discount
def test_discount_edge_cases():
    assert calculate_discount(100, 10) == 90.0
"""
        prompt = f"""
        Write pytest tests for this function:**Context (Surrounding code):**
{extracted['context']}

**Imports (use these):**
{'\n'.join(imports) if imports else ''}

**Mocking Instructions:**
- Use `mocker.patch` for ALL external dependencies (requests, boto3, sqlalchemy, kafka).
- Ensure tests are deterministic and do NOT hit external APIs.
- Focus on edge cases: negative values, zeroes, nulls, and boundary conditions.
"""
return engine.llm.generate(prompt)

# Assign the function to the engine
engine.generate_test = generate_test_func

passed, final_code, diff_string = engine.run_full_loop()

brand_footprint = "\n\n---\n🛡️ *Fixed automatically by [Aegis](https://test-qa-bot-production.up.railway.app)*"
return {
'success': passed,
'fixed_code': final_code,
'diff_output': diff_string + brand_footprint,
'message': 'Analysis complete.'
}