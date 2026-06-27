"""
Code Scanner - Business Logic Extraction
Production Ready - Strictly BYOK (Bring Your Own Key)
"""

import os
import logging
from ai_qa_engine import LLMClient

logger = logging.getLogger(__name__)

def is_valid_key() -> bool:
    """Check if a real API key exists in the environment."""
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return False
    # Reject obvious placeholders
    if "xxxx" in key or "XXX" in key or len(key) < 10:
        return False
    return True

def ask_question_about_code(question: str, diff_text: str) -> str:
    """Answer a specific question about the code using AI or Mock."""
    
    # ============================================================
    # MOCK MODE: Runs when no valid API key is found
    # ============================================================
    if not is_valid_key():
        logger.info("🔮 Running Chatbot in MOCK mode (no valid API key)")
        return f"""
🤖 **Mock Chatbot Response**

You asked: "{question}"

Based on the static analysis of the code diff:
- Detected function: `calculate_discount`
- Takes `price` and `discount_percent` as inputs.
- Warning: Does NOT handle negative values or percent > 100.

🔔 *To get real AI-powered answers, add a valid `DEEPSEEK_API_KEY` or `OPENAI_API_KEY` to your `.env` file.*
"""
    
    # ============================================================
    # LIVE MODE: Uses the user's actual API key
    # ============================================================
    logger.info("🧠 Running Chatbot in LIVE mode...")
    try:
        llm = LLMClient()
        
        # Extract business logic
        prompt_context = f"""
        You are a code scanner. Analyze this code diff and extract:
        1. All webhook URLs triggered.
        2. All database tables accessed.
        3. All external API calls made.
        4. List of functions changed.
        
        Return the result in plain English bullet points.
        
        Code Diff:
        {diff_text[:4000]}
        """
        context = llm.generate(prompt_context, temperature=0.1)
        
        # Answer the specific question
        prompt_answer = f"""
        You are an AI QA reviewer.
        Here is the business logic extracted from the code:
        {context}
        
        The user asks: "{question}"
        
        Answer accurately based ONLY on the extracted logic. 
        If the answer is not in the context, say "I don't see that in the code."
        """
        return llm.generate(prompt_answer, temperature=0.1)
        
    except Exception as e:
        logger.error(f"❌ Live AI failed: {e}")
        return "⚠️ The AI service is currently unavailable. Please check your API key or try again later."