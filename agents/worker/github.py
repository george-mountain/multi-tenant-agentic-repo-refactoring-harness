
import re

import httpx


def parse_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from an https or ssh GitHub URL."""
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if not match:
        raise ValueError(f"could not parse GitHub owner/repo from {repo_url!r}")
    return match.group(1), match.group(2)


async def check_push_access(repo_url: str, token: str) -> tuple[bool, str]:
    """Verify the token can push to the repo; return (ok, actionable diagnostic)."""
    owner, repo = parse_repo(repo_url)
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if response.status_code == 401:
        return False, "GitHub rejected the token (401): it is invalid, expired, or revoked."
    if response.status_code == 404:
        return False, (
            f"the token cannot see {owner}/{repo} (404). For fine-grained tokens, add this repository "
            "under the token's 'Repository access'."
        )
    if response.status_code >= 300:
        return False, f"GitHub API error {response.status_code} while checking token access: {response.text[:300]}"
    permissions = response.json().get("permissions") or {}
    if not permissions.get("push"):
        return False, (
            f"the token authenticates but has NO push permission on {owner}/{repo}. "
            "Fine-grained token: grant 'Contents: Read and write' for this repository. "
            "Classic token: it needs the 'repo' scope. "
            "Organization repos may additionally require SSO authorization for the token."
        )
    return True, ""


async def create_pull_request(
    repo_url: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    token: str,
) -> str:
    owner, repo = parse_repo(repo_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"title": title, "head": head_branch, "base": base_branch, "body": body},
        )
    if response.status_code >= 300:
        raise RuntimeError(f"GitHub PR creation failed ({response.status_code}): {response.text[:500]}")
    return response.json()["html_url"]
