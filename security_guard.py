import ast
import re

class CodeSecurityError(Exception):
    pass

def is_code_safe(code: str) -> tuple[bool, str]:
    """
    Advanced security scan using Python's AST parser.
    Blocks obfuscated attacks that regex cannot catch.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False, "Syntax error in code (possibly malicious)."

    # List of dangerous functions and attributes
    DANGEROUS_ATTRS = {
        'os', 'subprocess', 'sys', 'eval', 'exec', '__import__', 
        'compile', 'open', 'file', 'input', 'raw_input'
    }
    DANGEROUS_CALLS = {'system', 'popen', 'spawn', 'fork', 'exec'}

    for node in ast.walk(tree):
        # Block dangerous imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] in {'os', 'subprocess', 'socket', 'pty'}:
                    return False, f"Blocked dangerous import: {alias.name}"
        
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split('.')[0] in {'os', 'subprocess', 'socket'}:
                return False, f"Blocked dangerous import from: {node.module}"

        # Block dangerous attribute access (e.g., getattr(os, 'system'))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                if len(node.args) >= 2:
                    if isinstance(node.args[1], ast.Constant):
                        attr = node.args[1].value
                        if attr in DANGEROUS_ATTRS or attr in DANGEROUS_CALLS:
                            return False, f"Blocked obfuscated call: getattr(..., '{attr}')"

        # Check function calls (eval, exec)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {'eval', 'exec', 'compile'}:
                return False, f"Blocked dangerous function: {node.func.id}"

        # Check for __import__('os')
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                if node.args:
                    if isinstance(node.args[0], ast.Constant):
                        mod = node.args[0].value
                        if mod in {'os', 'subprocess', 'socket'}:
                            return False, f"Blocked __import__('{mod}')"

    return True, "Safe"

def is_code_safe_regex(code: str) -> tuple[bool, str]:
    """Fallback scanner for non-Python code snippets (or if AST fails)."""
    dangerous_patterns = [
        r"os\.system", r"subprocess\.", r"__import__", r"eval\(", r"exec\(", 
        r"open\(", r"file\(", r"import\s+os", r"import\s+subprocess",
        r"rm\s+-rf", r"dd\s+if=", r"dev\/null", r"pty\.spawn"
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, code):
            return False, f"Blocked dangerous pattern: {pattern}"
    return True, "Safe"

def hybrid_security_scan(code: str) -> tuple[bool, str]:
    """Use AST first, then fallback to regex."""
    safe, msg = is_code_safe(code)
    if not safe:
        return safe, msg
    return is_code_safe_regex(code)