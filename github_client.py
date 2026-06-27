import os
import logging
from github import Github

logger = logging.getLogger(__name__)

# Read strictly from environment - NO HARDCODING
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

def get_pr_diff(repo_name: str, pr_number: int):
    """Fetch the raw diff and repo object."""
    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN is not set in the environment.")
    
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    
    diff_content = ""
    for file in pr.get_files():
        diff_content += f"--- {file.filename}\n"
        diff_content += file.patch or "(binary or no changes)\n\n"
    
    return diff_content, repo, pr

def post_comment(repo, pr_number: int, comment: str):
    """Post a comment on the PR."""
    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN is not set in the environment.")
    
    pr = repo.get_pull(pr_number)
    pr.create_issue_comment(comment)
    logger.info("✅ Comment posted to PR #%s", pr_number)