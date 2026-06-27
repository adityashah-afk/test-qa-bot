#!/usr/bin/env python3
"""
AI QA ENGINE - Universal Provider Support
Supports: OpenAI, Anthropic, DeepSeek, Grok, Azure, and any OpenAI-compatible API.
BYOK (Bring Your Own Key) - We never see your data.
"""

import os
import sys
import subprocess
import difflib
import tempfile
from pathlib import Path
from typing import Tuple

# ================================================================
# 1. Environment Variables
# ================================================================
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()

# Provider-specific API keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROK_API_KEY = os.getenv("GROK_API_KEY")
AZURE_API_KEY = os.getenv("AZURE_API_KEY")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")

# ================================================================
# 2. Universal LLM Client
# ================================================================

class LLMClient:
    """Universal adapter for all LLM providers."""
    
    def __init__(self):
        self.provider = LLM_PROVIDER
        self.client = None
        self.model = None
        
        if self.provider == "openai":
            from openai import OpenAI
            self.client = OpenAI(api_key=OPENAI_API_KEY)
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            
        elif self.provider == "anthropic":
            import anthropic
            self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            self.model = os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
            
        elif self.provider == "deepseek":
            from openai import OpenAI
            self.client = OpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url="https://api.deepseek.com/v1"
            )
            self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
            
        elif self.provider == "grok":
            from openai import OpenAI
            self.client = OpenAI(
                api_key=GROK_API_KEY,
                base_url="https://api.x.ai/v1"
            )
            self.model = os.getenv("GROK_MODEL", "grok-beta")
            
        elif self.provider == "azure":
            from openai import OpenAI
            self.client = OpenAI(
                api_key=AZURE_API_KEY,
                base_url=AZURE_ENDPOINT
            )
            self.model = os.getenv("AZURE_MODEL", "gpt-4o-mini")
            
        else:
            raise Exception(f"Unsupported provider: {self.provider}. Supported: openai, anthropic, deepseek, grok, azure")
        
        print(f"🧠 Using LLM Provider: {self.provider.upper()} (Model: {self.model})")
    
    def generate(self, prompt: str, temperature: float = 0.3) -> str:
        """Generate a response using the configured provider."""
        try:
            if self.provider == "anthropic":
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                )
                return response.content[0].text
            
            else:
                # OpenAI-compatible providers (OpenAI, DeepSeek, Grok, Azure)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                )
                return response.choices[0].message.content
                
        except Exception as e:
            print(f"❌ LLM Error: {e}")
            raise e

# ================================================================
# 3. Mock Data
# ================================================================
MOCK_BUGGY_CODE = """
def calculate_discount(price, discount_percent):
    discount_amount = price * (discount_percent / 100)
    final_price = price - discount_amount
    return final_price
"""

MOCK_FIXED_CODE = """
def calculate_discount(price, discount_percent):
    if price < 0:
        return 0.0
    if discount_percent < 0:
        discount_percent = 0.0
    if discount_percent > 100:
        discount_percent = 100.0
    discount_amount = price * (discount_percent / 100)
    final_price = price - discount_amount
    return final_price
"""

# ================================================================
# 4. Core QA Engine (Provider-Agnostic)
# ================================================================

MAX_RETRIES = 3

class QAEngine:
    def __init__(self, use_mock: bool = True):
        self.use_mock = use_mock
        self.llm = None if use_mock else LLMClient()
        self.original_code = ""
        self.current_code = ""
        self.test_code = ""
        self.attempts = 0

    def load_code_from_string(self, code: str):
        self.original_code = code
        self.current_code = code
        return self

    def load_code_from_file(self, filepath: str):
        if self.use_mock:
            self.original_code = MOCK_BUGGY_CODE
            self.current_code = self.original_code
            return self
        with open(filepath, "r") as f:
            self.original_code = f.read()
            self.current_code = self.original_code
        return self

    def generate_test(self, code_snippet: str) -> str:
        if self.use_mock:
            return """
import pytest
from buggy_code import calculate_discount

def test_discount_edge_cases():
    assert calculate_discount(100, 10) == 90.0
    assert calculate_discount(0, 10) == 0.0
    assert calculate_discount(-10, 10) == 0.0
"""
        prompt = f"Write pytest tests for:\n{code_snippet}"
        return self.llm.generate(prompt)

    def run_test(self, test_code: str, source_code: str) -> Tuple[bool, str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = Path(tmpdir) / "buggy_code.py"
            tst_path = Path(tmpdir) / "test_buggy.py"
            src_path.write_text(source_code)
            tst_path.write_text(test_code)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(tst_path), "--tb=short", "-v"],
                capture_output=True, text=True, cwd=tmpdir,
            )
            return result.returncode == 0, result.stdout + result.stderr

    def fix_code(self, error_log: str, current_code: str) -> str:
        if self.use_mock:
            return MOCK_FIXED_CODE
        prompt = f"Fix this code. Error:\n{error_log}\n\nCode:\n{current_code}"
        return self.llm.generate(prompt, temperature=0.4)

    def generate_diff(self, original: str, fixed: str) -> str:
        """Generate a properly formatted diff with newlines."""
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile="Original",
            tofile="Fixed (AI)",
            lineterm="\n",  # <-- FIXED: Ensures proper newline formatting
        )
        return "".join(diff)

    def run_full_loop(self) -> Tuple[bool, str, str]:
        print(f"🧠 AI QA Loop starting... (Provider: {self.llm.provider.upper() if self.llm else 'MOCK'})")
        for attempt in range(1, MAX_RETRIES + 1):
            self.attempts = attempt
            print(f"  Attempt {attempt}...")
            test_code = self.generate_test(self.current_code)
            passed, output = self.run_test(test_code, self.current_code)
            if passed:
                print("  ✅ Tests PASSED!")
                diff = self.generate_diff(self.original_code, self.current_code)
                return True, self.current_code, diff
            print("  ❌ Tests FAILED. Fixing...")
            self.current_code = self.fix_code(output, self.current_code)
        diff = self.generate_diff(self.original_code, self.current_code)
        return False, self.current_code, diff