"""
JavaScript Analyzer - Full Jest Execution
Requires Node.js to be installed (already on Railway via nixpacks)
"""

import os
import subprocess
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
        if line.startswith('+') and ('function ' in line or '=>' in line or 'const ' in line or 'class ' in line):
            if current:
                functions.append('\n'.join(current))
                current = []
            in_func = True
            current.append(line[1:])
        elif in_func and line.startswith('+'):
            current.append(line[1:])
            if '}' in line or ';' in line:
                if current:
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
    """Run Jest tests in a temporary sandbox with security checks."""
    # Security scan for dangerous JS
    dangerous = ['child_process', 'execSync', 'spawn', 'fs.rmSync', 'process.exit', 'require("child_process")']
    for pattern in dangerous:
        if pattern in test_code or pattern in source_code:
            return False, f"❌ Security violation: Blocked '{pattern}'"
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Write source and test files
            src_path = Path(tmpdir) / "index.js"
            tst_path = Path(tmpdir) / "index.test.js"
            src_path.write_text(source_code)
            tst_path.write_text(test_code)

            # 2. Write package.json with Jest
            pkg_path = Path(tmpdir) / "package.json"
            pkg_path.write_text('{"scripts": {"test": "jest"}, "devDependencies": {"jest": "29.7.0"}}')

            # 3. Install dependencies (timeout 30s)
            install = subprocess.run(
                ['npm', 'install', '--silent'],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=30
            )
            if install.returncode != 0:
                return False, f"❌ npm install failed: {install.stderr[:200]}"

            # 4. Run tests (timeout 10s)
            result = subprocess.run(
                ['npm', 'test', '--', '--silent'],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                return True, result.stdout
            else:
                return False, result.stdout + "\n" + result.stderr

    except subprocess.TimeoutExpired:
        return False, "❌ Test timed out (10s). Potential infinite loop."
    except Exception as e:
        return False, f"❌ Jest Error: {e}"