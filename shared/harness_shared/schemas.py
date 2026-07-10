"""Schemas shared between the backend control plane and the agent worker."""

from typing import Literal

from pydantic import BaseModel, Field


COMMIT_TYPES = ("feat", "fix", "refactor", "perf", "test", "docs", "build", "ci", "chore", "style")


class PlanStep(BaseModel):
    """One unit of refactoring work targeting a small set of files."""

    id: str = Field(description="Short stable identifier, e.g. 's1'")
    title: str = Field(
        description="Imperative, lower-case commit subject WITHOUT a type prefix, e.g. "
        "'add request validation to the generate endpoint' (max ~70 chars)"
    )
    commit_type: Literal[COMMIT_TYPES] = Field(  # type: ignore[valid-type]
        default="refactor",
        description="Conventional Commits type: feat (new capability), fix (bug), refactor "
        "(behavior-preserving restructure), perf, test, docs, build, ci, chore, style",
    )
    rationale: str = Field(description="Why this step is needed (becomes the commit body)")
    action: Literal["modify", "create"] = Field(description="Whether files are modified or created")
    files: list[str] = Field(min_length=1, description="Repo-relative paths this step may touch")
    verification_hint: str = Field(default="", description="What the tests should prove after this step")


class Plan(BaseModel):
    """Strict JSON execution plan produced by the Planner stage."""

    objective: str
    summary: str = Field(description="Short description of the overall approach")
    steps: list[PlanStep] = Field(min_length=1, max_length=20)


class CriticVerdict(BaseModel):
    """Structured output of the llm_critic semantic review.

    A step is blocked ONLY by `blocking_issues`. `suggestions` are advisory notes for the human
    reviewer and never block a commit.
    """

    blocking_issues: list[str] = Field(
        default_factory=list,
        description="ONLY concrete, demonstrable defects that MUST be fixed before commit: real bugs, "
        "logic errors, regressions the tests would not catch, security holes, or data loss. NOT style, "
        "NOT speculation about hypothetical downstream consumers, NOT the fact that behavior changed "
        "(the step's purpose is to change behavior).",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Non-blocking observations or improvement ideas for the human PR reviewer.",
    )

    @property
    def approved(self) -> bool:
        return not self.blocking_issues


class PRSummary(BaseModel):
    """Human-facing pull-request description generated from the final diff."""

    title: str = Field(
        description="Conventional-Commit style PR title describing what shipped, e.g. "
        "'refactor: harden async chat generation and history flow' (max ~70 chars)"
    )
    summary: str = Field(
        description="2-4 sentences, past tense, describing WHAT was changed and the user-facing "
        "impact — not how it was planned"
    )
    highlights: list[str] = Field(
        default_factory=list, description="Concise bullet points of the concrete changes made"
    )


class SupervisorDecision(BaseModel):
    """Adaptive loop-control decision taken after a failed verification."""

    action: Literal["retry", "revise_step", "accept_step", "skip_step", "abort"] = Field(
        description="retry: try again (optionally with guidance); revise_step: rewrite the step's "
        "intent/scope and try again; accept_step: commit the current work as-is because tests pass and "
        "the only remaining objections are non-blocking, subjective, or speculative; skip_step: discard "
        "this step's edits but continue the run; abort: stop the whole run"
    )
    reason: str = Field(description="One-sentence justification for the decision")
    guidance: str = Field(
        default="", description="Concrete instructions for the executor's next attempt (retry/revise only)"
    )
    revised_step: PlanStep | None = Field(
        default=None, description="Replacement step definition, required when action is revise_step"
    )


RUN_STATUSES = (
    "queued",
    "ingesting",
    "planning",
    "awaiting_approval",
    "executing",
    "verifying",
    "finalizing",
    "succeeded",
    "failed",
)

EVENT_RUN_STATUS = "run_status"
EVENT_LOG = "log"
EVENT_PLAN = "plan_created"
EVENT_STEP_STARTED = "step_started"
EVENT_VERIFY_RESULT = "verify_result"
EVENT_CRITIC_RESULT = "critic_result"
EVENT_SUPERVISOR = "supervisor_decision"
EVENT_STEP_SKIPPED = "step_skipped"
EVENT_STEP_COMMITTED = "step_committed"
EVENT_TOKEN_USAGE = "token_usage"
EVENT_RUN_COMPLETED = "run_completed"
EVENT_RUN_FAILED = "run_failed"

JOB_STREAM = "harness:runs"
JOB_GROUP = "workers"
EVENTS_CHANNEL_ALL = "runs:all"


def run_channel(run_id: str) -> str:
    """Redis pub/sub channel carrying live events for one run."""
    return f"run:{run_id}"
