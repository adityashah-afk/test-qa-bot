import re
import logging
import base64
import json
from ai_qa_engine import QAEngine

logger = logging.getLogger(__name__)

# ============================================================
# Helper: Extract import context (for correct mocking)
# ============================================================
def get_import_context(full_content: str) -> dict:
    import_lines = [line for line in full_content.splitlines() if line.startswith(('import ', 'from '))]
    import_map = {}
    for line in import_lines:
        if line.startswith('from '):
            parts = line.split(' import ')
            if len(parts) == 2:
                module = parts[0].replace('from ', '').strip()
                imported = parts[1].strip().split(',')
                for item in imported:
                    clean_item = item.strip()
                    if ' as ' in clean_item:
                        alias_parts = clean_item.split(' as ')
                        import_map[alias_parts[1].strip()] = f"{module}.{alias_parts[0].strip()}"
                    else:
                        import_map[clean_item] = f"{module}.{clean_item}"
        elif line.startswith('import '):
            parts = line.replace('import ', '').split(' as ')
            if len(parts) == 2:
                import_map[parts[1].strip()] = parts[0].strip()
            else:
                for item in parts:
                    clean_item = item.strip().split('.')[0]
                    import_map[clean_item] = clean_item
    return import_map

# ============================================================
# Helper: Extract local function signatures
# ============================================================
def extract_local_function_signatures(full_content: str, called_funcs: list) -> str:
    if not called_funcs:
        return ""
    signatures = []
    skip_list = {'len', 'print', 'range', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple',
                 'sum', 'max', 'min', 'abs', 'sorted', 'enumerate', 'zip', 'map', 'filter', 'any', 'all'}
    for func in set(called_funcs):
        if func in skip_list or func.startswith('_'):
            continue
        pattern = rf'def\s+{func}\s*\([^)]*\)\s*->?\s*[^:]*:'
        match = re.search(pattern, full_content)
        if match:
            signatures.append(match.group(0))
        else:
            pattern2 = rf'async\s+def\s+{func}\s*\([^)]*\)\s*->?\s*[^:]*:'
            match2 = re.search(pattern2, full_content)
            if match2:
                signatures.append(match2.group(0))
    return '\n'.join(signatures)

# ============================================================
# Helper: Extract changed function from diff
# ============================================================
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

# ============================================================
# Helper: Get full file content from GitHub
# ============================================================
def get_full_file_content(repo, file_path, branch_name):
    try:
        contents = repo.get_contents(file_path, ref=branch_name)
        return base64.b64decode(contents.content).decode('utf-8')
    except Exception as e:
        logger.warning(f"Could not fetch full file: {e}")
        return None

# ============================================================
# Helper: Impact radius scan (find other files that call the function)
# ============================================================
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

# ============================================================
# Helper: Check if file is a migration
# ============================================================
def is_migration_file(file_path):
    migration_patterns = ['migrations/', 'alembic/', 'prisma/schema.prisma', 'db/migrate/', 'schema.sql']
    for pattern in migration_patterns:
        if pattern in file_path:
            return True
    return False

# ============================================================
# MAIN ANALYZER
# ============================================================
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
                    import_map = get_import_context(full_content)
                break

    # Local function signatures
    if full_content:
        call_pattern = r'(\w+)\('
        called_funcs = re.findall(call_pattern, code_snippet)
        local_sigs = extract_local_function_signatures(full_content, called_funcs)

    # Impact radius
    if repo and pr and func_name:
        impact_radius = scan_impact_radius(repo, func_name, pr.head.ref)

    # Initialize QAEngine
    logger.info(f"🧠 Analyzing optimized snippet ({len(code_snippet)} chars)...")
    engine = QAEngine(use_mock=use_mock, model_override=model_override)
    engine.load_code_from_string(code_snippet)

    # ============================================================
    # Override the generate_test method with a properly indented function
    # ============================================================
    def generate_test_func(code_snippet):
        if use_mock:
            return """
import pytest
from buggy_code import calculate_discount
def test_discount_edge_cases():
    assert calculate_discount(100, 10) == 90.0
"""
        # Build the prompt
        mock_instructions = ""
        if import_map:
            mock_instructions = "**Detected Imports & Mocking Strategy:**\n"
            for var, path in import_map.items():
                if 'boto3' in path or 'kafka' in path or 'sqlalchemy' in path or 'requests' in path:
                    mock_instructions += "- The code uses `{}` (imported from `{}`). To mock it, use `mocker.patch('{}')` if it's a module-level import.\n".format(var, path, path)
                    mock_instructions += "- If the code uses `from {} import {}`, patch the local reference using `mocker.patch('__main__.{}')`.\n".format(path.split('.')[0], var, var)
        else:
            mock_instructions = "**Mocking Strategy:** Use `mocker.patch` for all external dependencies."

        impact_warning = ""
        if impact_radius:
            impact_warning = """
**⚠️ IMPACT RADIUS WARNING (CRITICAL):**
This function is used in {} other file(s) in this repository.
- **DO NOT change the function signature, return type, or input parameters.**
- You must maintain backward compatibility.
- Here is where it is used:
{}
""".format(len(impact_radius), json.dumps(impact_radius[:3], indent=2))

        prompt = """
        Write pytest tests for this function:**Context (Surrounding code):**
{}

**Imports (use these):**
{}

**Local Function Signatures (Called by your function):**
{}

{}

{}

**Mocking Instructions (Detailed):**
- Use `mocker.patch` for ALL external dependencies.
- If the code imports a library using `from lib import func`, patch the local reference using `mocker.patch('__main__.func')`.
- Ensure tests are deterministic and do NOT hit external APIs.
- Focus on edge cases: negative values, zeroes, nulls, and boundary conditions.
""".format(
    code_snippet,
    extracted['context'],
    '\n'.join(imports) if imports else '',
    local_sigs,
    impact_warning,
    mock_instructions
)
# This return is properly inside the function
return engine.llm.generate(prompt)

# Assign the new test generation function
engine.generate_test = generate_test_func

# Run the full Auto-Heal loop
passed, final_code, diff_string = engine.run_full_loop()

# Add viral branding
brand_footprint = "\n\n---\n🛡️ *Fixed automatically by [Aegis](https://test-qa-bot-production.up.railway.app)*"
return {
'success': passed,
'fixed_code': final_code,
'diff_output': diff_string + brand_footprint,
'message': 'Analysis complete.'
}