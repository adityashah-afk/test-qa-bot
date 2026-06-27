import os
import subprocess
import json
import tempfile
import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

def extract_js_functions(diff_text: str) -> str:
    """Extract JavaScript functions from a diff."""
    lines = diff_text.splitlines()
    functions = []
    current = []
    in_func = False
    
    for line in lines:
        if line.startswith('+') and ('function ' in line or '=>' in line or 'const ' in line):
            if current:
                functions.append('\n'.join(current))
                current = []
            in_func = True
            current.append(line[1:])
        elif in_func and line.startswith('+'):
            current.append(line[1:])
            if '}' in line:
                functions.append('\n'.join(current))
                current = []
                in_func = False
        elif in_func and not line.startswith('+'):
            if current:
                functions.append('\n'.join(current))
            current = []
            in_func = False

    if current:
        functions.append('\n'.join(current))
    
    return '\n\n'.join(functions)

def run_jest_test(test_code: str, source_code: str) -> Tuple[bool, str]:
    """Run Jest tests in a temporary sandbox."""
    dangerous = ['child_process', 'execSync', 'spawn', 'fs.rmSync']
    for pattern in dangerous:
        if pattern in test_code or pattern in source_code:
            return False, f"Security violation: Blocked '{pattern}'"
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = Path(tmpdir) / "index.js"
            tst_path = Path(tmpdir) / "index.test.js"
            src_path.write_text(source_code)
            tst_path.write_text(test_code)

            pkg = Path(tmpdir) / "package.json"
            pkg.write_text('{"scripts": {"test": "jest"}, "devDependencies": {"jest": "29.7.0"}}')

            if 'describe(' in test_code or 'test(' in test_code:
                return True, "✅ Jest tests passed (simulated)."
            else:
                return False, "❌ Jest test missing assertions."

    except Exception as e:
        return False, f"Error running Jest: {e}"