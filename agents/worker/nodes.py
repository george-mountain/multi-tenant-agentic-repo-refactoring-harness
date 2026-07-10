
import functools
import json
import logging
import re
import shlex

import httpx

from harness_shared.schemas import (
    EVENT_CRITIC_RESULT,
    EVENT_PLAN,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_STEP_COMMITTED,
    EVENT_STEP_SKIPPED,
    EVENT_STEP_STARTED,
    EVENT_SUPERVISOR,
    EVENT_TOKEN_USAGE,
    EVENT_VERIFY_RESULT,
    CriticVerdict,
    Plan,
    PRSummary,
    SupervisorDecision,
)
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from worker import events, runtime
from worker.config import settings
from worker.github import create_pull_request
from worker.llm import BudgetExceeded, cached_call, cached_structured_call, make_llm
from worker.sandbox_client import SandboxError
from worker.state import RunState
from worker.storage import final_patch_key, report_key, snapshot_key, step_bundle_key, step_diff_key

log = logging.getLogger("worker.nodes")

REPO_DIR = "/workspace/repo"
FILE_CONTEXT_LIMIT = 12_000
TREE_LIMIT = 400

PLANNER_SYSTEM = """You are the Planner stage of an autonomous refactoring harness.
You inspect a repository (read-only), identify what needs refactoring, and produce a strict JSON execution plan.
Rules:
- You CANNOT modify files. You may only read files and list directories.
- If no objective is given, audit the codebase yourself and decide the highest-value refactor: look for
  synchronous/blocking I/O on async paths, API endpoints missing input validation, deprecated or unsafe
  API usage, duplicated logic, missing error handling, resource leaks, and outdated language patterns.
  Read the entry points and the most load-bearing modules before deciding.
- Write a precise `objective` describing what you chose and why it matters.
- Break the work into small, independently verifiable steps (1-3 files each).
- Each step becomes ONE git commit, so write its `title` as a Conventional-Commit subject: imperative
  mood, lower case, no trailing period, no type prefix (e.g. "add request validation to the generate
  endpoint"). Set `commit_type` to the right Conventional Commits type — feat for new capability, fix for
  a bug, refactor for behavior-preserving restructuring, perf, test, docs, build, ci, chore, style.
- Order steps so earlier steps never depend on later ones.
- Each step's `files` list must contain every file that step is allowed to touch.
- Choose a scope you can realistically complete and verify: prefer one coherent, high-impact refactor
  over a scattering of trivial fixes, and never exceed what the test suite can protect."""

EXECUTOR_SYSTEM = """You are the Executor stage of an autonomous refactoring harness.
You implement exactly one plan step inside a sandboxed repository, and you make it COMPLETE and consistent.
Rules:
- The step's file list is a STARTING POINT, not a hard boundary. Refactors are cross-cutting: if you rename
  or reshape something, you MUST update every consumer of it across the repo, or the change is broken.
- Use `search` (git grep) to discover the real file paths and EVERY call site affected by your change before
  and after editing — never assume a path; verify it exists. Filenames in the plan may be wrong.
- Use search_replace with an EXACT, UNIQUE snippet of existing text (enough surrounding lines to be unique).
- Use ast_edit for repetitive structural rewrites (ast-grep pattern/rewrite, meta-variables like $X).
- Use write_file ONLY to create genuinely new files; for existing files use search_replace/ast_edit.
- Edit any file the change requires to stay correct and consistent, but do NOT touch modules unrelated to this
  step — keep the change focused on this step's concern.
- Never weaken, skip, or delete tests to make them pass.
- If previous attempts failed, the error log (tests, reviewer findings, or supervisor guidance) tells you what
  broke; fix the ROOT cause, including any call sites you missed.
- When the step is fully implemented AND consistent across all call sites, reply WITHOUT tool calls,
  summarizing what you changed."""

SUPERVISOR_SYSTEM = """You are the Supervisor of an autonomous refactoring loop — the perceive/reason/act
controller that decides what happens after a failed verification attempt.
You receive the step being attempted, the full error trajectory so far, the current uncommitted diff summary,
attempts used vs. the hard ceiling, and the token budget consumed. Decide ONE action:
- retry: the failures are fixable and errors are changing between attempts (progress). Give concrete,
  specific guidance for the next attempt when you can.
- revise_step: the step's intent or scope is genuinely wrong — it bundles unrelated concerns, targets the
  wrong area, or needs a sharper description. Provide a corrected step. (Note: you do NOT need to fix file
  lists for cross-cutting edits — the verifier already auto-adopts related files the executor touches. Only
  the test-deletion and too-many-files guards are hard, and revising the file list will not clear those.)
- accept_step: TESTS PASS and the only thing blocking is the reviewer, whose objections are non-blocking,
  subjective, shifting between attempts, or speculative about unseen callers. Commit the work as-is and move
  on — a human reviews the PR. Choosing to loop forever on reviewer nitpicks while tests are green is a
  failure mode; prefer accept_step over burning attempts once the deterministic gates are satisfied and the
  remaining findings are not concrete runtime defects.
- skip_step: the step is not essential to the overall objective and attempts show no convergence.
  The run continues with the remaining steps; already-verified work is preserved.
- abort: continuing is futile or counterproductive (identical TEST failures repeating with no progress, the
  repo fundamentally resists this refactor, or correctness cannot be assured).
Reason about the TRAJECTORY, not a single failure: changing error messages usually mean progress; the exact
same error twice in a row usually means the executor is stuck and needs different guidance or a revised scope.
Distinguish TEST failures (hard evidence of a real defect — keep working) from reviewer-only objections when
tests already pass (often nitpicking — lean toward accept_step). Be economical: do not spend the remaining
budget re-litigating subjective review points on a change whose tests are green."""

PR_SUMMARY_SYSTEM = """You write the pull-request description for a completed autonomous refactor.
You are given the objective, the list of commits that landed, and the full diff of the merged branch.
Describe WHAT was actually changed and its impact, in the past tense, as a senior engineer would for
teammates reviewing the PR. Be concrete and grounded in the diff — do NOT restate the plan or say how it
"will" be done. Keep the summary tight (2-4 sentences) and the highlights specific (files/functions/behavior
that changed). Write a Conventional-Commit style title."""

CRITIC_SYSTEM = """You are a pragmatic senior code reviewer inside an autonomous refactoring harness.
The deterministic gates (tests, linter) ALREADY PASSED — tests are the source of truth for behavior.
Your job is to catch real defects those gates can miss, then get out of the way. This diff becomes a
pull request that a human will review and merge, so you are a safety net, not the final authority.

Classify each concern into exactly one bucket:
- blocking_issues: ONLY concrete, demonstrable defects — a real bug or logic error, a security hole,
  data loss/corruption, a mutable-default-argument trap, an unhandled error path that will crash. Something
  you could point to and say "this is wrong and will misbehave at runtime."
- suggestions: everything else — style, naming, "a hypothetical client might depend on the old shape",
  "this could be more consistent", or the mere fact that behavior changed. The step's PURPOSE is to change
  and harden behavior, so behavior changes are expected and are NOT defects.

Rules of thumb:
- If the tests pass and you cannot name a concrete runtime failure, do NOT put it in blocking_issues.
- Do not invent new objections each round to justify blocking; if your prior blocking issues were addressed,
  approve. Speculation about unseen callers is a suggestion, never blocking.
- An empty blocking_issues list means APPROVE."""


def node_guard(func):
    """Convert unexpected node exceptions into a state-level failure for routing to abort."""

    @functools.wraps(func)
    async def wrapper(state: RunState) -> dict:
        try:
            return await func(state)
        except BudgetExceeded as exc:
            await events.log(state["run_id"], f"budget guardrail tripped: {exc}")
            return {"failure": str(exc)}
        except Exception as exc:
            log.exception("node %s failed for run %s", func.__name__, state.get("run_id"))
            await events.log(state["run_id"], f"{func.__name__} failed: {exc}")
            return {"failure": f"{func.__name__}: {exc}"}

    return wrapper


def _merge_tokens(state: RunState, usage: dict[str, int]) -> dict[str, int]:
    tokens = dict(state.get("tokens") or {"input_tokens": 0, "output_tokens": 0})
    tokens["input_tokens"] += usage.get("input_tokens", 0)
    tokens["output_tokens"] += usage.get("output_tokens", 0)
    return tokens


def _budget_ok(tokens: dict[str, int]) -> bool:
    return tokens["input_tokens"] + tokens["output_tokens"] <= settings.max_run_tokens


async def _publish_tokens(run_id: str, node: str, tokens: dict[str, int]) -> None:
    await events.publish(run_id, EVENT_TOKEN_USAGE, {"node": node, **tokens})


async def _git(run_id: str, cmd: str, timeout: int = 300):
    return await runtime.sandbox.exec(run_id, cmd, workdir=REPO_DIR, timeout=timeout)


def _repo_path(path: str) -> str:
    return f"repo/{path.lstrip('/')}"


def _commit_message(step: dict) -> str:
    """Build a Conventional-Commit message (subject + rationale body) for a verified step."""
    subject = (step.get("title") or "apply refactoring step").strip().rstrip(".")
    commit_type = step.get("commit_type") or "refactor"
    header = f"{commit_type}: {subject}"[:100]
    rationale = (step.get("rationale") or "").strip()
    return f"{header}\n\n{rationale}\n" if rationale else f"{header}\n"


async def _install_dependencies(run_id: str, tree: list[str]) -> None:
    if "package.json" in tree:
        await _git(run_id, "npm install --no-audit --no-fund", timeout=900)
    if "requirements.txt" in tree:
        await _git(run_id, "pip install --user -q -r requirements.txt", timeout=900)
    elif "pyproject.toml" in tree:
        await _git(run_id, "pip install --user -q -e . || pip install --user -q .", timeout=900)


def _detect_commands(tree: list[str]) -> tuple[str, str]:
    has_py = any(p.endswith(".py") for p in tree)
    has_js = "package.json" in tree
    test_cmd = ""
    lint_cmd = ""
    if has_py and any("test" in p for p in tree if p.endswith(".py")):
        # No -x: run the whole suite so failing-test ids can be diffed against the baseline.
        # -rfE emits parseable "FAILED/ERROR <nodeid>" summary lines; --tb=short keeps tracebacks
        # for the executor without flooding output.
        test_cmd = "python -m pytest -q --tb=short -rfE -p no:cacheprovider"
        lint_cmd = "ruff check ."
    elif has_js:
        test_cmd = "npm test --silent"
    return test_cmd, lint_cmd


def _parse_pytest(result) -> dict:
    """Extract failing/erroring test node-ids and collectability from a pytest run."""
    failing: set[str] = set()
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        s = line.strip()
        if s.startswith(("FAILED ", "ERROR ")):
            node = s.split(" ", 1)[1].split(" - ", 1)[0].strip()
            if node:
                failing.add(node)
    return {"exit_code": result.exit_code, "failing": sorted(failing)}


def _parse_ruff(result) -> dict[str, list[str]]:
    """Group ruff findings as {file: [codes]} (line numbers ignored so they survive edits)."""
    by_file: dict[str, set[str]] = {}
    pattern = re.compile(r"^(.+?):\d+:\d+:\s+([A-Z]+\d+)\b")
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        m = pattern.match(line.strip())
        if m:
            by_file.setdefault(m.group(1), set()).add(m.group(2))
    return {f: sorted(c) for f, c in by_file.items()}


def _is_ruff(lint_cmd: str) -> bool:
    return lint_cmd.strip().startswith("ruff")


async def _capture_baseline(run_id: str, test_cmd: str, lint_cmd: str) -> dict:
    """Record test/lint failures on the pristine checkout so verification only blocks regressions."""
    baseline: dict = {"test": None, "lint": None}
    if test_cmd:
        result = await _git(run_id, test_cmd, timeout=900)
        baseline["test"] = _parse_pytest(result) if "pytest" in test_cmd else {
            "exit_code": result.exit_code, "failing": []
        }
    if lint_cmd and _is_ruff(lint_cmd):
        result = await _git(run_id, "ruff check . --output-format=concise", timeout=300)
        baseline["lint"] = _parse_ruff(result)
    return baseline


def _test_regressed(base: dict | None, cur: dict) -> list[str]:
    """Return the list of newly-failing tests the change introduced (empty = no regression)."""
    base = base or {"exit_code": 0, "failing": []}
    base_fail = set(base.get("failing") or [])
    cur_fail = set(cur.get("failing") or [])
    new_failures = sorted(cur_fail - base_fail)
    base_collectable = base.get("exit_code") in (0, 1)
    broke_collection = base_collectable and cur.get("exit_code") == 5
    if broke_collection:
        return ["test collection broke (previously-runnable tests can no longer be discovered)"]
    return new_failures


async def _lint_gate(run_id: str, lint_cmd: str, changed_py: list[str], baseline_lint: dict | None) -> dict | None:
    """Lint only the files the step changed, and only flag codes not present in the baseline."""
    if not lint_cmd or not _is_ruff(lint_cmd) or not changed_py:
        return None
    quoted = " ".join(shlex.quote(p) for p in changed_py)
    result = await _git(run_id, f"ruff check {quoted} --output-format=concise", timeout=300)
    if result.ok:
        return None
    cur = _parse_ruff(result)
    base = baseline_lint or {}
    new_findings: dict[str, list[str]] = {}
    for path, codes in cur.items():
        extra = sorted(set(codes) - set(base.get(path, [])))
        if extra:
            new_findings[path] = extra
    if not new_findings:
        return None
    return {"kind": "lint", "detail": f"new lint issues introduced by this step: {new_findings}"}


@node_guard
async def ingest(state: RunState) -> dict:
    """Clone the repo at a pinned base SHA, cut the run branch, snapshot to MinIO."""
    run_id = state["run_id"]
    await events.status(run_id, "ingesting")
    await runtime.sandbox.create(run_id)

    already_cloned = (await runtime.sandbox.exec(run_id, "test -d repo/.git")).ok
    branch = f"refactor/{run_id[:8]}"
    base_branch = state.get("base_branch") or ""
    if not already_cloned:
        await runtime.sandbox.set_network(run_id, True)
        clone = await runtime.sandbox.exec(
            run_id, f"git clone --depth 50 {state['repo_url']} repo", timeout=600
        )
        if not clone.ok:
            raise RuntimeError(f"git clone failed: {clone.stderr[-2000:]}")
        if base_branch:
            checkout = await _git(run_id, f"git checkout {base_branch}")
            if not checkout.ok:
                raise RuntimeError(f"base branch {base_branch!r} not found")
        else:
            base_branch = (await _git(run_id, "git rev-parse --abbrev-ref HEAD")).stdout.strip()
        await _git(run_id, f"git checkout -b {branch}")
    elif not base_branch:
        detected = await _git(run_id, "git symbolic-ref --short refs/remotes/origin/HEAD")
        base_branch = detected.stdout.strip().removeprefix("origin/") if detected.ok else "main"

    base_sha = (await _git(run_id, "git rev-parse HEAD")).stdout.strip()
    tree_result = await _git(run_id, "git ls-files")
    tree = [line for line in tree_result.stdout.splitlines() if line.strip()]

    if not already_cloned:
        await events.log(run_id, f"cloned at {base_sha[:10]}, installing dependencies")
        await _install_dependencies(run_id, tree)
        bundle = await _git(run_id, "git bundle create /workspace/snapshot.bundle --all")
        if not bundle.ok:
            raise RuntimeError(f"snapshot bundle failed: {bundle.stderr[-1000:]}")
        data = await runtime.sandbox.read_file_bytes(run_id, "snapshot.bundle")
        runtime.storage.put(snapshot_key(run_id), data)
        await runtime.sandbox.set_network(run_id, False)

    test_cmd, lint_cmd = state.get("test_cmd") or "", state.get("lint_cmd") or ""
    if not test_cmd:
        detected_test, detected_lint = _detect_commands(tree)
        test_cmd = detected_test
        lint_cmd = lint_cmd or detected_lint

    baseline = await _capture_baseline(run_id, test_cmd, lint_cmd)
    base_fail = len((baseline.get("test") or {}).get("failing") or [])
    base_lint = sum(len(v) for v in (baseline.get("lint") or {}).values())
    await events.log(
        run_id,
        f"baseline captured: {base_fail} pre-existing test failure(s), {base_lint} pre-existing lint finding(s) "
        "(these will not block the refactor — only new regressions do)",
    )

    if settings.github_token:
        try:
            from worker.github import check_push_access

            ok, detail = await check_push_access(state["repo_url"], settings.github_token)
            if not ok:
                await events.log(
                    run_id,
                    f"warning — the GitHub token cannot open a PR on this repo: {detail} "
                    "The run will continue and finish with a downloadable patch unless the token is fixed.",
                )
        except httpx.HTTPError:
            pass

    await events.log(run_id, f"ingest complete: {len(tree)} files, test={test_cmd!r}, lint={lint_cmd!r}")
    return {
        "base_sha": base_sha,
        "base_branch": base_branch,
        "branch": branch,
        "repo_tree": tree,
        "test_cmd": test_cmd,
        "lint_cmd": lint_cmd,
        "baseline": baseline,
        "current_step": 0,
        "attempt": 0,
        "error_log": [],
        "committed_steps": [],
        "skipped_steps": [],
        "tokens": state.get("tokens") or {"input_tokens": 0, "output_tokens": 0},
    }


def planner_read_file(path: str) -> str:
    """Read a file from the repository (path relative to the repo root)."""


def planner_list_dir(path: str) -> str:
    """List the contents of a repository directory (path relative to the repo root, '.' for root)."""


async def _dispatch_planner_tool(run_id: str, name: str, args: dict) -> str:
    if name == "planner_read_file":
        content = await runtime.sandbox.read_file(run_id, _repo_path(args["path"]), stage="planner")
        return content[:FILE_CONTEXT_LIMIT]
    if name == "planner_list_dir":
        result = await runtime.sandbox.exec(
            run_id, f"ls -la {args.get('path', '.')}", stage="planner", workdir=REPO_DIR
        )
        return result.stdout[:4000]
    return f"unknown tool {name}"


@node_guard
async def plan_node(state: RunState) -> dict:
    """Planner stage: read-only exploration, then a strict JSON plan with schema retries."""
    run_id = state["run_id"]
    await events.status(run_id, "planning")
    llm = make_llm(state["provider"], state.get("model"))
    provider, model = state["provider"], state.get("model") or ""

    tree_head = "\n".join(state["repo_tree"][:TREE_LIMIT])
    readme = ""
    for candidate in ("README.md", "README.rst", "readme.md"):
        if candidate in state["repo_tree"]:
            readme = (await runtime.sandbox.read_file(run_id, _repo_path(candidate), stage="planner"))[:4000]
            break

    objective = state.get("objective") or ""
    goal_text = (
        f"Objective (set by the user): {objective}"
        if objective
        else (
            "No objective was given. Audit this repository yourself, decide the most valuable refactor "
            "you can complete and verify safely, and state it precisely as the plan's objective."
        )
    )
    messages = [
        SystemMessage(content=PLANNER_SYSTEM),
        HumanMessage(
            content=(
                f"{goal_text}\n\n"
                f"Repository file tree (first {TREE_LIMIT} files):\n{tree_head}\n\n"
                f"README excerpt:\n{readme}\n\n"
                "Explore the files you need, then produce the execution plan."
            )
        ),
    ]

    tokens = dict(state["tokens"])
    tool_llm = llm.bind_tools([planner_read_file, planner_list_dir])
    for call_idx in range(settings.max_planner_tool_calls):
        response, usage = await cached_call(
            tool_llm, messages,
            run_id=run_id, node="planner", step_idx=-1, attempt=0, call_idx=call_idx,
            provider=provider, model=model,
        )
        tokens = _merge_tokens({"tokens": tokens}, usage)
        messages.append(response)
        if not response.tool_calls:
            break
        for tool_call in response.tool_calls:
            output = await _dispatch_planner_tool(run_id, tool_call["name"], tool_call["args"])
            messages.append(ToolMessage(content=output, tool_call_id=tool_call["id"]))
    if not _budget_ok(tokens):
        raise BudgetExceeded("token budget exhausted during planning")

    messages.append(HumanMessage(content="Now produce the strict JSON execution plan."))
    plan = None
    for retry in range(3):
        try:
            plan, usage = await cached_structured_call(
                llm, Plan, messages,
                run_id=run_id, node="planner_output", step_idx=-1, attempt=retry, call_idx=0,
                provider=provider, model=model,
            )
            tokens = _merge_tokens({"tokens": tokens}, usage)
            break
        except ValueError as exc:
            messages.append(HumanMessage(content=f"Your plan was invalid: {exc}. Emit a corrected plan."))
    if plan is None:
        raise RuntimeError("planner failed to produce a schema-valid plan after 3 attempts")

    invalid = [f for step in plan.steps if step.action == "modify" for f in step.files if f not in state["repo_tree"]]
    if invalid:
        raise RuntimeError(f"plan references files not in the repository: {invalid[:5]}")

    await events.publish(run_id, EVENT_PLAN, {"plan": plan.model_dump()})
    await _publish_tokens(run_id, "planner", tokens)
    return {"plan": plan.model_dump(), "objective": plan.objective, "tokens": tokens}


async def plan_gate(state: RunState) -> dict:
    """Optional human approval gate implemented as a LangGraph interrupt."""
    if state.get("approval_required"):
        await events.status(state["run_id"], "awaiting_approval")
        interrupt("plan awaiting approval")
        await events.log(state["run_id"], "plan approved by user")
    return {}


def executor_read_file(path: str) -> str:
    """Read a file from the repository (path relative to the repo root)."""


def executor_search_replace(path: str, old: str, new: str, replace_all: bool = False) -> str:
    """Replace an exact, unique text snippet in a file. Include enough surrounding lines to make `old` unique."""


def executor_ast_edit(path: str, lang: str, pattern: str, rewrite: str) -> str:
    """Apply a structural ast-grep codemod to a file. `lang` is e.g. 'python' or 'javascript'; use meta-variables like $ARG in pattern/rewrite."""


def executor_write_file(path: str, content: str) -> str:
    """Create a NEW file with the given content. For files that already exist, use search_replace instead."""


def executor_search(query: str) -> str:
    """Search the repository for a literal string or symbol (git grep) to find real file paths and every call site that must change. Returns matching file:line results."""


async def _dispatch_executor_tool(run_id: str, repo_tree: set[str], name: str, args: dict) -> str:
    if name == "executor_search":
        query = args.get("query", "")
        result = await runtime.sandbox.exec(
            run_id, f"git grep -n -F -- {shlex.quote(query)} || true", stage="executor", workdir=REPO_DIR
        )
        out = result.stdout.strip()
        return out[:6000] if out else f"no matches for {query!r}"

    path = args.get("path", "")
    if name == "executor_read_file":
        try:
            return (await runtime.sandbox.read_file(run_id, _repo_path(path), stage="executor"))[:FILE_CONTEXT_LIMIT]
        except SandboxError as exc:
            return f"ERROR: {exc}"
    try:
        if name == "executor_search_replace":
            result = await runtime.sandbox.search_replace(
                run_id, _repo_path(path), args["old"], args["new"], bool(args.get("replace_all"))
            )
            return f"ok: {result['replacements']} replacement(s) applied to {path}"
        if name == "executor_ast_edit":
            result = await runtime.sandbox.ast_edit(
                run_id, _repo_path(path), args["lang"], args["pattern"], args["rewrite"]
            )
            return f"exit={result['exit_code']} {result['stdout'][:1000]}{result['stderr'][:1000]}"
        if name == "executor_write_file":
            if path in repo_tree:
                return f"ERROR: {path!r} already exists; use search_replace/ast_edit for existing files"
            await runtime.sandbox.write_file(run_id, _repo_path(path), args["content"], stage="executor")
            return f"ok: created {path}"
    except SandboxError as exc:
        return f"ERROR: {exc}"
    return f"unknown tool {name}"


@node_guard
async def execute_step(state: RunState) -> dict:
    """Executor stage: implement the current plan step with surgical, sandbox-scoped edits."""
    run_id = state["run_id"]
    step = state["plan"]["steps"][state["current_step"]]
    await events.status(run_id, "executing", step=state["current_step"], attempt=state["attempt"])
    await events.publish(
        run_id, EVENT_STEP_STARTED,
        {"step": state["current_step"], "attempt": state["attempt"], "title": step["title"]},
    )

    llm = make_llm(state["provider"], state.get("model"))
    provider, model = state["provider"], state.get("model") or ""

    file_context = []
    for path in step["files"]:
        try:
            content = await runtime.sandbox.read_file(run_id, _repo_path(path), stage="executor")
            file_context.append(f"===== {path} =====\n{content[:FILE_CONTEXT_LIMIT]}")
        except SandboxError:
            file_context.append(f"===== {path} ===== (does not exist yet)")

    error_context = ""
    if state["error_log"]:
        error_context = "\nPrevious attempts on this step FAILED:\n" + json.dumps(state["error_log"][-3:], indent=2)

    messages = [
        SystemMessage(content=EXECUTOR_SYSTEM),
        HumanMessage(
            content=(
                f"Overall objective: {state['plan']['objective']}\n\n"
                f"Current step {step['id']}: {step['title']}\n"
                f"Rationale: {step['rationale']}\n"
                f"Allowed files: {step['files']}\n"
                f"Verification hint: {step.get('verification_hint', '')}\n"
                f"{error_context}\n\n"
                f"Current contents of the allowed files:\n\n" + "\n\n".join(file_context)
            )
        ),
    ]

    tokens = dict(state["tokens"])
    repo_tree = set(state.get("repo_tree") or [])
    tools = [executor_read_file, executor_search, executor_search_replace, executor_ast_edit, executor_write_file]
    tool_llm = llm.bind_tools(tools)
    for call_idx in range(settings.max_executor_tool_calls):
        response, usage = await cached_call(
            tool_llm, messages,
            run_id=run_id, node="executor", step_idx=state["current_step"],
            attempt=state["attempt"], call_idx=call_idx,
            provider=provider, model=model,
        )
        tokens = _merge_tokens({"tokens": tokens}, usage)
        if not _budget_ok(tokens):
            raise BudgetExceeded("token budget exhausted during execution")
        messages.append(response)
        if not response.tool_calls:
            break
        for tool_call in response.tool_calls:
            output = await _dispatch_executor_tool(run_id, repo_tree, tool_call["name"], tool_call["args"])
            messages.append(ToolMessage(content=output, tool_call_id=tool_call["id"]))

    await _publish_tokens(run_id, "executor", tokens)
    return {"tokens": tokens}


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return "test" in lowered or "spec" in lowered


@node_guard
async def verify_gates(state: RunState) -> dict:
    """Deterministic verification: guard checks, then tests and linter inside the sandbox."""
    run_id = state["run_id"]
    step = state["plan"]["steps"][state["current_step"]]
    await events.status(run_id, "verifying", step=state["current_step"], attempt=state["attempt"])

    errors: list[dict] = []
    adopted_plan: dict | None = None
    status_result = await _git(run_id, "git status --porcelain")
    changed: list[tuple[str, str]] = []
    for line in status_result.stdout.splitlines():
        if line.strip():
            changed.append((line[:2].strip(), line[3:].strip()))

    changed_paths = [p for _, p in changed]
    out_of_scope = [p for p in changed_paths if p not in step["files"]]
    deleted_tests = [p for flag, p in changed if "D" in flag and _is_test_path(p)]

    # Hard guard: deleting tests is never allowed (anti-gaming).
    if deleted_tests:
        errors.append(
            {"kind": "guard", "step_id": step["id"], "attempt": state["attempt"],
             "detail": f"test files were deleted, which is forbidden: {deleted_tests}"}
        )
    # Hard guard: runaway sprawl beyond a sanity cap suggests the step lost focus.
    elif len(changed_paths) > settings.max_files_per_step:
        errors.append(
            {"kind": "guard", "step_id": step["id"], "attempt": state["attempt"],
             "detail": (
                 f"this step changed {len(changed_paths)} files (cap {settings.max_files_per_step}); "
                 "that is too broad for one reviewable step — split the work or narrow the change"
             )}
        )
    # Otherwise: refactors are cross-cutting. Adopt the extra files the executor legitimately had to
    # touch (all call sites of a renamed field, etc.) into the step's scope, and let tests + the critic
    # judge correctness. The plan's file list is a hint, not a boundary.
    elif out_of_scope:
        adopted_plan = json.loads(json.dumps(state["plan"]))
        merged = list(dict.fromkeys(step["files"] + out_of_scope))
        adopted_plan["steps"][state["current_step"]]["files"] = merged
        await events.log(
            run_id,
            f"step {state['current_step'] + 1}: expanded scope to cover related files {out_of_scope[:8]}",
        )

    if not errors:
        baseline = state.get("baseline") or {}
        test_cmd = state.get("test_cmd")
        if test_cmd:
            result = await _git(run_id, test_cmd, timeout=900)
            cur = _parse_pytest(result) if "pytest" in test_cmd else {
                "exit_code": result.exit_code, "failing": []
            }
            if "pytest" in test_cmd:
                new_failures = _test_regressed(baseline.get("test"), cur)
            else:
                base_ok = (baseline.get("test") or {}).get("exit_code", 0) == 0
                new_failures = ["tests failed"] if (not result.ok and base_ok) else []
            if new_failures:
                errors.append(
                    {"kind": "test", "step_id": step["id"], "attempt": state["attempt"], "command": test_cmd,
                     "exit_code": result.exit_code, "detail": f"new test failures: {new_failures[:15]}",
                     "stdout_tail": result.stdout[-4000:], "stderr_tail": result.stderr[-4000:]}
                )

        changed_py = [p for flag, p in changed if "D" not in flag and p.endswith(".py")]
        lint_error = await _lint_gate(run_id, state.get("lint_cmd") or "", changed_py, baseline.get("lint"))
        if lint_error:
            errors.append({**lint_error, "step_id": step["id"], "attempt": state["attempt"]})

    passed = not errors
    await events.publish(
        run_id, EVENT_VERIFY_RESULT,
        {"step": state["current_step"], "attempt": state["attempt"], "passed": passed,
         "errors": [{k: v for k, v in e.items() if k != "stdout_tail"} for e in errors]},
    )
    if passed:
        result = {"verdict": "gates_pass"}
        if adopted_plan is not None:
            result["plan"] = adopted_plan
        return result
    return {"verdict": "gates_fail", "error_log": state["error_log"] + errors}


@node_guard
async def critic_node(state: RunState) -> dict:
    """Semantic review of a gate-passing diff; findings feed back like test failures."""
    run_id = state["run_id"]
    if not settings.critic_enabled:
        return {"verdict": "pass"}
    step = state["plan"]["steps"][state["current_step"]]
    diff = (await _git(run_id, "git diff HEAD")).stdout[-24_000:]
    if not diff.strip():
        error = {"kind": "critic", "step_id": step["id"], "attempt": state["attempt"],
                 "detail": "no changes were made for this step"}
        return {"verdict": "fail", "error_log": state["error_log"] + [error]}

    llm = make_llm(state["provider"], state.get("model"))
    messages = [
        SystemMessage(content=CRITIC_SYSTEM),
        HumanMessage(
            content=(
                f"Step intent: {step['title']}\nRationale: {step['rationale']}\n\nDiff:\n{diff}"
            )
        ),
    ]
    verdict, usage = await cached_structured_call(
        llm, CriticVerdict, messages,
        run_id=run_id, node="critic", step_idx=state["current_step"],
        attempt=state["attempt"], call_idx=0,
        provider=state["provider"], model=state.get("model") or "",
    )
    tokens = _merge_tokens(state, usage)
    await events.publish(
        run_id, EVENT_CRITIC_RESULT,
        {"step": state["current_step"], "attempt": state["attempt"],
         "approved": verdict.approved,
         "blocking_issues": verdict.blocking_issues, "suggestions": verdict.suggestions},
    )
    if verdict.approved:
        return {"verdict": "pass", "tokens": tokens}
    error = {"kind": "critic", "step_id": step["id"], "attempt": state["attempt"],
             "detail": "; ".join(verdict.blocking_issues)[:2000]}
    return {"verdict": "fail", "error_log": state["error_log"] + [error], "tokens": tokens}


def _error_trajectory(error_log: list[dict]) -> str:
    lines = []
    for entry in error_log[-8:]:
        detail = entry.get("detail") or entry.get("stderr_tail") or entry.get("stdout_tail") or ""
        lines.append(f"attempt {entry.get('attempt', '?')} [{entry.get('kind')}]: {detail[:400]}")
    return "\n".join(lines) or "(no recorded errors)"


@node_guard
async def supervise_failure(state: RunState) -> dict:
    """Adaptive loop control: perceive the failure trajectory, decide retry / revise / skip / abort.

    Deterministic backstops still apply: a generous per-step attempt ceiling and the
    run token budget route to abort regardless of what the supervisor prefers.
    """
    run_id = state["run_id"]
    attempt = state["attempt"] + 1
    step_idx = state["current_step"]
    step = state["plan"]["steps"][step_idx]

    if attempt >= settings.max_verify_attempts:
        return {
            "attempt": attempt,
            "supervisor_action": "abort",
            "failure": f"hard ceiling of {settings.max_verify_attempts} verification attempts reached on step {step['id']}",
        }
    if not _budget_ok(state["tokens"]):
        raise BudgetExceeded("token budget exhausted; stopping the verification loop")

    if not settings.supervisor_enabled:
        await events.log(run_id, f"step {step_idx + 1} attempt {attempt}/{settings.max_verify_attempts} failed; retrying")
        return {"attempt": attempt, "supervisor_action": "retry", "verdict": ""}

    diff_stat = (await _git(run_id, "git diff HEAD --stat")).stdout[-1500:]
    changed_now = (await _git(run_id, "git diff HEAD --name-only")).stdout.strip() or "(no changes staged yet)"
    budget = state["tokens"]["input_tokens"] + state["tokens"]["output_tokens"]
    llm = make_llm(state["provider"], state.get("model"))
    messages = [
        SystemMessage(content=SUPERVISOR_SYSTEM),
        HumanMessage(
            content=(
                f"Overall objective: {state['plan']['objective']}\n\n"
                f"Current step ({step_idx + 1} of {len(state['plan']['steps'])}): {json.dumps(step, indent=1)}\n"
                f"Attempts used on this step: {attempt} (hard ceiling {settings.max_verify_attempts})\n"
                f"Token budget: {budget:,} used of {settings.max_run_tokens:,}\n"
                f"Steps already completed: {state['committed_steps']}, skipped: {state.get('skipped_steps', [])}\n\n"
                f"Files actually changed in this attempt:\n{changed_now}\n\n"
                f"Error trajectory (oldest to newest):\n{_error_trajectory(state['error_log'])}\n\n"
                f"Uncommitted diff summary:\n{diff_stat}\n\n"
                "Decide the next action."
            )
        ),
    ]
    decision, usage = await cached_structured_call(
        llm, SupervisorDecision, messages,
        run_id=run_id, node="supervisor", step_idx=step_idx, attempt=attempt, call_idx=0,
        provider=state["provider"], model=state.get("model") or "",
    )
    tokens = _merge_tokens(state, usage)

    action = decision.action
    # Deterministic safeguard: accept_step is only valid when the block was the reviewer, not a failing
    # gate. If the latest failure was a test/lint/guard failure, never commit broken code — downgrade to retry.
    if action == "accept_step":
        last_gate = next(
            (e["kind"] for e in reversed(state["error_log"]) if e.get("kind") in ("test", "lint", "guard", "critic")),
            None,
        )
        if last_gate != "critic":
            action = "retry"
            await events.log(run_id, "supervisor chose accept, but a gate is still failing; retrying instead")

    await events.publish(
        run_id, EVENT_SUPERVISOR,
        {"step": step_idx, "attempt": attempt, "action": action, "reason": decision.reason},
    )

    updates: dict = {"attempt": attempt, "supervisor_action": action, "verdict": "", "tokens": tokens}
    error_log = list(state["error_log"])
    if decision.guidance and action in ("retry", "revise_step"):
        error_log.append(
            {"kind": "supervisor", "step_id": step["id"], "attempt": attempt, "detail": decision.guidance}
        )
        updates["error_log"] = error_log
    if action == "revise_step":
        if decision.revised_step is None:
            updates["supervisor_action"] = "retry"
        else:
            plan = json.loads(json.dumps(state["plan"]))
            plan["steps"][step_idx] = decision.revised_step.model_dump()
            updates["plan"] = plan
            await events.publish(run_id, EVENT_PLAN, {"plan": plan})
    if action == "abort":
        updates["failure"] = f"supervisor stopped the run: {decision.reason}"
    return updates


@node_guard
async def skip_step(state: RunState) -> dict:
    """Abandon the current step (discarding its uncommitted edits) and continue the run."""
    run_id = state["run_id"]
    step_idx = state["current_step"]
    step = state["plan"]["steps"][step_idx]
    await _git(run_id, "git checkout -- . && git clean -fd")
    await events.publish(run_id, EVENT_STEP_SKIPPED, {"step": step_idx, "title": step["title"]})
    return {
        "current_step": step_idx + 1,
        "attempt": 0,
        "error_log": [],
        "verdict": "",
        "supervisor_action": "",
        "skipped_steps": state.get("skipped_steps", []) + [step_idx],
    }


@node_guard
async def commit_step(state: RunState) -> dict:
    """Commit the verified step on the run branch and export diff + bundle to MinIO."""
    run_id = state["run_id"]
    step_idx = state["current_step"]
    step = state["plan"]["steps"][step_idx]

    await _git(run_id, "git add -A")
    message = _commit_message(step)
    await runtime.sandbox.write_file_bytes(run_id, "commitmsg.txt", message.encode("utf-8"))
    commit = await _git(run_id, "git commit -F /workspace/commitmsg.txt --allow-empty")
    if not commit.ok:
        raise RuntimeError(f"git commit failed: {commit.stderr[-1000:]}")

    diff = (await _git(run_id, "git show HEAD --format=medium")).stdout
    runtime.storage.put(step_diff_key(run_id, step_idx), diff.encode("utf-8"), "text/x-diff")

    bundle = await _git(run_id, f"git bundle create /workspace/step.bundle {state['branch']}")
    if bundle.ok:
        data = await runtime.sandbox.read_file_bytes(run_id, "step.bundle")
        runtime.storage.put(step_bundle_key(run_id, step_idx), data)

    await events.publish(
        run_id, EVENT_STEP_COMMITTED,
        {"step": step_idx, "title": step["title"], "diff_preview": diff[:4000]},
    )
    return {
        "current_step": step_idx + 1,
        "attempt": 0,
        "error_log": [],
        "verdict": "",
        "committed_steps": state["committed_steps"] + [step_idx],
    }


@node_guard
async def final_verify(state: RunState) -> dict:
    """Full-suite verification of the completed tree (regression-only), plus final patch export."""
    run_id = state["run_id"]
    await events.status(run_id, "finalizing")
    baseline = state.get("baseline") or {}

    test_cmd = state.get("test_cmd")
    if test_cmd:
        result = await _git(run_id, test_cmd, timeout=1200)
        cur = _parse_pytest(result) if "pytest" in test_cmd else {"exit_code": result.exit_code, "failing": []}
        if "pytest" in test_cmd:
            new_failures = _test_regressed(baseline.get("test"), cur)
        else:
            base_ok = (baseline.get("test") or {}).get("exit_code", 0) == 0
            new_failures = ["tests failed"] if (not result.ok and base_ok) else []
        if new_failures:
            return {"failure": f"final test verification found regressions: {new_failures[:15]}"}

    changed_all = (await _git(run_id, f"git diff --name-only {state['base_sha']}..HEAD")).stdout.splitlines()
    changed_py = [p.strip() for p in changed_all if p.strip().endswith(".py")]
    lint_error = await _lint_gate(run_id, state.get("lint_cmd") or "", changed_py, baseline.get("lint"))
    if lint_error:
        return {"failure": f"final lint verification found regressions: {lint_error['detail']}"}

    patch = (await _git(run_id, f"git diff {state['base_sha']}..HEAD")).stdout
    runtime.storage.put(final_patch_key(run_id), patch.encode("utf-8"), "text/x-diff")
    return {"verdict": "final_pass"}


async def _generate_pr_content(state: RunState) -> tuple[str, str]:
    """Summarize the merged diff into a (title, markdown body) describing what actually shipped."""
    run_id = state["run_id"]
    plan = state["plan"]
    commits = (await _git(run_id, f"git log --format='%h %s' {state['base_sha']}..HEAD")).stdout.strip()
    diff = (await _git(run_id, f"git diff {state['base_sha']}..HEAD")).stdout

    fallback_title = f"{plan['steps'][0].get('commit_type', 'refactor')}: {plan['objective'][:70]}"
    summary_md = plan.get("summary", "")
    highlights_md = ""
    try:
        llm = make_llm(state["provider"], state.get("model"))
        result, usage = await cached_structured_call(
            llm, PRSummary,
            [
                SystemMessage(content=PR_SUMMARY_SYSTEM),
                HumanMessage(
                    content=(
                        f"Objective: {plan['objective']}\n\n"
                        f"Commits that landed:\n{commits}\n\n"
                        f"Full diff of the branch:\n{diff[:40000]}"
                    )
                ),
            ],
            run_id=run_id, node="pr_summary", step_idx=-1, attempt=0, call_idx=0,
            provider=state["provider"], model=state.get("model") or "",
        )
        await _publish_tokens(run_id, "pr_summary", _merge_tokens(state, usage))
        fallback_title = result.title or fallback_title
        summary_md = result.summary or summary_md
        highlights_md = "".join(f"- {h}\n" for h in result.highlights)
    except Exception:
        log.exception("PR summary generation failed for run %s; using plan-derived fallback", run_id)

    skipped = set(state.get("skipped_steps") or [])
    committed_md = "".join(
        f"- `{s.get('commit_type', 'refactor')}` {s['title']}\n"
        for i, s in enumerate(plan["steps"]) if i not in skipped
    )
    body = f"## Summary\n\n{summary_md}\n"
    if highlights_md:
        body += f"\n### Changes\n\n{highlights_md}"
    if committed_md:
        body += f"\n### Commits\n\n{committed_md}"
    if skipped:
        body += (
            "\n### Not included\n\n"
            + "".join(f"- {plan['steps'][i]['title']}\n" for i in sorted(skipped))
            + "\nThese planned changes could not be verified safely and were left out.\n"
        )
    body += "\n---\n*Automated refactor — please review before merging.*"
    return fallback_title, body


async def _push_and_open_pr(state: RunState) -> tuple[str, str]:
    """Attempt push + PR creation; return (pr_url, pr_error) — never raises."""
    run_id = state["run_id"]
    from worker.github import check_push_access, parse_repo

    try:
        ok, detail = await check_push_access(state["repo_url"], settings.github_token)
    except httpx.HTTPError as exc:
        ok, detail = False, f"could not reach the GitHub API to verify the token: {exc}"
    if not ok:
        return "", detail

    owner, repo = parse_repo(state["repo_url"])
    await runtime.sandbox.set_network(run_id, True)
    try:
        push = await runtime.sandbox.exec(
            run_id,
            f'git push "https://x-access-token:${{GIT_TOKEN}}@github.com/{owner}/{repo}.git" '
            f'{state["branch"]}:{state["branch"]}',
            workdir=REPO_DIR,
            env={"GIT_TOKEN": settings.github_token},
            timeout=300,
        )
        if not push.ok:
            return "", f"git push was rejected: {push.stderr[-800:]}"
        title, body = await _generate_pr_content(state)
        try:
            pr_url = await create_pull_request(
                state["repo_url"], state["branch"], state["base_branch"], title, body, settings.github_token
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            return "", f"branch {state['branch']} was pushed, but PR creation failed: {exc}"
        return pr_url, ""
    finally:
        await runtime.sandbox.set_network(run_id, False)


@node_guard
async def open_pr(state: RunState) -> dict:
    """Push the run branch and open a PR (never against main directly).

    A push or PR failure never discards a verified refactor: the run still
    completes with the final patch artifact, and the reason is surfaced.
    """
    run_id = state["run_id"]
    pr_url = ""
    pr_error = ""
    if settings.github_token:
        pr_url, pr_error = await _push_and_open_pr(state)
        if pr_error:
            await events.log(run_id, f"could not open a pull request: {pr_error}")
            await events.log(run_id, "the verified refactor is preserved as the final patch artifact")
    else:
        await events.log(run_id, "GITHUB_TOKEN not set; skipping push, final patch stored as artifact")

    await runtime.sandbox.destroy(run_id)
    await events.publish(
        run_id, EVENT_RUN_COMPLETED,
        {"pr_url": pr_url, "pr_error": pr_error, "steps_completed": len(state["committed_steps"])},
    )
    await events.status(run_id, "succeeded", pr_url=pr_url)
    return {"pr_url": pr_url}


async def abort_rollback(state: RunState) -> dict:
    """Guardrail exit: roll back, persist a failure report, destroy the sandbox."""
    run_id = state["run_id"]
    failure = state.get("failure") or "run aborted by verification guardrails"
    try:
        await _git(run_id, "git reset --hard && git clean -fd")
    except SandboxError:
        pass
    report = {
        "run_id": run_id,
        "failure": failure,
        "completed_steps": state.get("committed_steps", []),
        "skipped_steps": state.get("skipped_steps", []),
        "current_step": state.get("current_step", 0),
        "error_log": state.get("error_log", []),
        "tokens": state.get("tokens", {}),
    }
    runtime.storage.put(report_key(run_id), json.dumps(report, indent=2).encode("utf-8"), "application/json")
    try:
        await runtime.sandbox.destroy(run_id)
    except SandboxError:
        pass
    await events.publish(run_id, EVENT_RUN_FAILED, {"failure": failure})
    await events.status(run_id, "failed", failure=failure)
    return {"failure": failure}
