#!/usr/bin/env python3
"""
Aegis Local CLI - Run Aegis on any Python file instantly.
Usage: python cli.py path/to/file.py
"""

import sys
from ai_qa_engine import QAEngine

def main():
    if len(sys.argv) < 2:
        print("Usage: python cli.py <path_to_python_file>")
        sys.exit(1)

    filepath = sys.argv[1]
    try:
        with open(filepath, "r") as f:
            code = f.read()
    except FileNotFoundError:
        print(f"❌ File not found: {filepath}")
        sys.exit(1)

    print("⚡ Running Aegis on your code...")
    engine = QAEngine(use_mock=False)
    engine.load_code_from_string(code)
    passed, fixed_code, diff = engine.run_full_loop()

    if passed:
        print("\n✅ Tests PASSED! No bugs found.")
    else:
        print("\n❌ Tests FAILED. Here is the suggested fix:\n")
        print(diff[:1500])
        # Optionally write the fixed code to a new file
        with open("fixed_" + filepath, "w") as f:
            f.write(fixed_code)
        print(f"\n✅ Fixed code saved to: fixed_{filepath}")

if __name__ == "__main__":
    main()