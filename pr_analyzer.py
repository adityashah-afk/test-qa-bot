import logging
from ai_qa_engine import QAEngine

logger = logging.getLogger(__name__)

def extract_changed_functions(diff_text: str) -> dict:
    # Placeholder - the engine will handle it
    return {'code': diff_text, 'context': '', 'func_name': ''}

def analyze_pr_diff(diff_text: str, use_mock: bool = True, model_override: str = None, repo=None, pr=None, team_rules: str = None) -> dict:
    logger.info("🔍 Analyzing PR diff (stable version)")
    
    # Just use the engine directly – it already has a working generate_test method
    engine = QAEngine(use_mock=use_mock, model_override=model_override)
    engine.load_code_from_string(diff_text)
    
    passed, final_code, diff_string = engine.run_full_loop()
    
    return {
        'success': passed,
        'fixed_code': final_code,
        'diff_output': diff_string,
        'message': 'Analysis complete (stable mode)'
    }