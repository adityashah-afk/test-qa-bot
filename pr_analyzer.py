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

def scan_impact_radius(repo, func_name, branch_name):
    impact = []
    try:
        contents = repo.get_contents("", ref=branch_name)

        def traverse(items):
            for item in items:
                if item.type == 'dir':
                    try:
                        sub_items = repo.get_contents(item.path, ref=branch_name)
                        traverse(sub_items)
                    except:
                        pass
                elif item.name.endswith('.py'):
                    try:
                        file_content = base64.b64decode(item.content).decode('utf-8')
                        if func_name in file_content and 'def ' + func_name not in file_content:
                            lines = file_content.splitlines()
                            for i, line in enumerate(lines):
                                if func_name in line and 'def ' not in line:
                                    ctx = '\n'.join(lines[max(0, i - 3):min(len(lines), i + 3)])
                                    impact.append({
                                        'file': item.path,
                                        'context': ctx
                                    })
                                    break
                    except:
                        pass

        traverse(contents)
    except Exception as e:
        logger.warning(f"Impact radius scan failed: {e}")
    return impact

def is_migration_file(file_path):
    migration_patterns = ['migrations/', 'alembic/', 'prisma/schema.prisma', 'db/migrate/', 'schema.sql']
    for pattern in migration_patterns:
        if pattern in file_path:
            return True
    return False

def analyze_pr_diff(diff_text: str, use_mock: bool = True, model_override: str = None, repo=None, pr=None, team_rules: str = None) -> dict:
    extracted = extract_changed_functions(diff_text)
    code_snippet = extracted['code']
    func_name = extracted['func_name']

    if not code_snippet or len(code_snippet) < 10:
        logger.info("ℹ️ No Python functions detected.")
        return {'success': True, 'message': 'No functions detected.', 'diff_output': '', 'fixed_code': ''}

    # Migration exclusion
    if repo and pr:
        for file in pr.get_files():
            if is_migration_file(file.filename):
                return {
                    'success': True,
                    'message': '⚠️ Database migration file detected. Aegis is in Audit-Only mode.',
                    'diff_output': 'No changes made to migration files.',
                    'fixed_code': None
                }

    # Fetch full file content for context
    full_content = None
    imports = []
    import_map = {}
    local_sigs = ""
    impact_radius = []

    if repo and pr:
        for file in pr.get_files():
            if file.filename.endswith('.py'):
                full_content = get_full_file_content(repo, file.filename, pr.head.ref)
                if full_content:
                    import_lines = [line for line in full_content.splitlines() if line.startswith(('import ', 'from '))]
                    imports = import_lines[:5]
                break

    # Local function signatures
    if full_content:
        call_pattern = r'(\w+)\('
        called_funcs = re.findall(call_pattern, code_snippet)
        # We'll just pass the code snippet directly; the engine will generate tests.
        # The additional context is not injected into the prompt here, but we can later enhance.

    # Impact radius (for informational purposes)
    if repo and pr and func_name:
        impact_radius = scan_impact_radius(repo, func_name, pr.head.ref)

    logger.info(f"🧠 Analyzing optimized snippet ({len(code_snippet)} chars)...")
    engine = QAEngine(use_mock=use_mock, model_override=model_override)
    engine.load_code_from_string(code_snippet)

    # Run the full Auto-Heal loop (uses the default generate_test)
    passed, final_code, diff_string = engine.run_full_loop()

    brand_footprint = "\n\n---\n🛡️ *Fixed automatically by [Aegis](https://test-qa-bot-production.up.railway.app)*"
    return {
        'success': passed,
        'fixed_code': final_code,
        'diff_output': diff_string + brand_footprint,
        'message': 'Analysis complete.'
    }