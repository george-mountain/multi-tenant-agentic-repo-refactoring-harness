
from typing import TypedDict


class RunState(TypedDict, total=False):
    run_id: str
    tenant_id: str
    provider: str
    model: str
    repo_url: str
    base_branch: str
    objective: str
    test_cmd: str
    lint_cmd: str
    approval_required: bool

    base_sha: str
    branch: str
    repo_tree: list[str]
    baseline: dict
    plan: dict
    current_step: int
    attempt: int
    error_log: list[dict]
    committed_steps: list[int]
    skipped_steps: list[int]
    tokens: dict[str, int]
    verdict: str
    supervisor_action: str
    failure: str
    pr_url: str
