import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { GitHubIcon } from "../components/Logo";
import StatusBadge from "../components/StatusBadge";
import Stepper from "../components/Stepper";
import { api } from "../lib/api";
import { formatTokens, repoName, timeAgo } from "../lib/format";
import { useRunStream } from "../lib/useRunStream";
import type { Run, RunEvent } from "../lib/types";
import { TERMINAL_STATUSES } from "../lib/types";

function describeEvent(event: RunEvent): { text: string; cls: string } {
  const d = event.data;
  switch (event.type) {
    case "run_status":
      return { text: `Stage: ${d.status.replace("_", " ")}`, cls: "" };
    case "log":
      return { text: d.message, cls: "" };
    case "plan_created":
      return { text: `Audit complete — plan created with ${d.plan.steps.length} step(s)`, cls: "pass" };
    case "step_started":
      return { text: `Step ${d.step + 1}, attempt ${d.attempt + 1}: ${d.title}`, cls: "" };
    case "verify_result":
      return d.passed
        ? { text: `Step ${d.step + 1}: tests & checks passed (attempt ${d.attempt + 1})`, cls: "pass" }
        : {
            text: `Step ${d.step + 1}: checks failed (attempt ${d.attempt + 1}) — ${d.errors
              .map((e: any) => e.kind)
              .join(", ")}`,
            cls: "fail",
          };
    case "critic_result":
      return d.approved
        ? { text: `Step ${d.step + 1}: reviewer approved the change`, cls: "pass" }
        : {
            text: `Step ${d.step + 1}: reviewer found blocking issues — ${(d.blocking_issues ?? []).join("; ")}`,
            cls: "fail",
          };
    case "supervisor_decision": {
      const labels: Record<string, string> = {
        retry: `Supervisor: retrying step ${d.step + 1}`,
        revise_step: `Supervisor: revised the scope of step ${d.step + 1}`,
        accept_step: `Supervisor: accepting step ${d.step + 1} (tests pass)`,
        skip_step: `Supervisor: skipping step ${d.step + 1}`,
        abort: "Supervisor: stopping the run",
      };
      return {
        text: `${labels[d.action] ?? `Supervisor: ${d.action}`} — ${d.reason}`,
        cls: d.action === "abort" ? "fail" : d.action === "accept_step" ? "pass" : "warn",
      };
    }
    case "step_skipped":
      return { text: `Step ${d.step + 1} skipped: ${d.title}`, cls: "warn" };
    case "step_committed":
      return { text: `Step ${d.step + 1} committed: ${d.title}`, cls: "pass" };
    case "token_usage":
      return { text: `Progress checkpoint (${d.node})`, cls: "" };
    case "run_completed":
      if (d.pr_url) return { text: `Pull request opened: ${d.pr_url}`, cls: "pass" };
      if (d.pr_error) return { text: `Run completed, but the PR could not be opened: ${d.pr_error}`, cls: "warn" };
      return { text: "Run completed — final patch available", cls: "pass" };
    case "run_failed":
      return { text: `Run failed: ${d.failure}`, cls: "fail" };
    default:
      return { text: JSON.stringify(d), cls: "" };
  }
}

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [diff, setDiff] = useState<{ step: number; text: string } | null>(null);
  const [approving, setApproving] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);

  const terminal = run !== null && TERMINAL_STATUSES.includes(run.status);
  const events = useRunStream(runId ?? "", runId !== undefined);

  useEffect(() => {
    if (!runId || terminal) return;
    const load = () => api.getRun(runId).then(setRun).catch(() => undefined);
    load();
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [runId, terminal]);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight });
  }, [events.length]);

  const stepInfo = useMemo(() => {
    const committed = new Set<number>();
    const skipped = new Set<number>();
    let active = -1;
    let lastStage = "";
    const attempts = new Map<number, { attempt: number; passed: boolean }[]>();
    for (const event of events) {
      if (event.type === "step_committed") committed.add(event.data.step);
      if (event.type === "step_skipped") skipped.add(event.data.step);
      if (event.type === "step_started") active = event.data.step;
      if (event.type === "run_status" && event.data.status !== "failed") lastStage = event.data.status;
      if (event.type === "verify_result" || event.type === "critic_result") {
        const passed = event.type === "verify_result" ? event.data.passed : event.data.approved;
        const list = attempts.get(event.data.step) ?? [];
        list.push({ attempt: event.data.attempt, passed });
        attempts.set(event.data.step, list);
      }
    }
    return { committed, skipped, active, attempts, lastStage };
  }, [events]);

  async function approve() {
    if (!runId) return;
    setApproving(true);
    try {
      setRun(await api.approveRun(runId));
    } finally {
      setApproving(false);
    }
  }

  async function showDiff(step: number) {
    if (!runId) return;
    const text = await api.stepDiff(runId, step).catch(() => "Diff is not available yet.");
    setDiff({ step, text });
  }

  if (!run) {
    return (
      <div className="page-loading">
        <div className="spinner" /> Loading run…
      </div>
    );
  }

  const feedEvents = events.filter((event) => event.type !== "token_usage");
  const prError = events.find((event) => event.type === "run_completed")?.data.pr_error as string | undefined;

  return (
    <>
      <Link className="back-link" to="/">
        ← All runs
      </Link>

      <div className="run-header">
        <h1>
          <GitHubIcon size={20} />
          {repoName(run.repo_url)}
          <StatusBadge status={run.status} />
        </h1>
        {run.status === "awaiting_approval" && (
          <button className="btn btn-primary" onClick={approve} disabled={approving}>
            {approving ? "Starting…" : "Approve plan & start"}
          </button>
        )}
        {run.pr_url && (
          <a className="btn btn-primary" href={run.pr_url} target="_blank" rel="noreferrer">
            View pull request ↗
          </a>
        )}
        {run.status === "succeeded" && !run.pr_url && (
          <button
            className="btn btn-ghost"
            onClick={() => api.finalPatch(run.id).then((text) => setDiff({ step: -1, text }))}
          >
            View final patch
          </button>
        )}
      </div>

      <div className="run-meta">
        <span className="meta-chip mono">
          branch <b>{run.branch || "—"}</b>
        </span>
        <span className="meta-chip">
          started <b>{timeAgo(run.created_at)}</b>
        </span>
        <span className="meta-chip">
          compute <b>{formatTokens(run.tokens)} tokens</b>
        </span>
      </div>

      {run.failure && <div className="failure-banner">⚠ {run.failure}</div>}
      {run.status === "succeeded" && !run.pr_url && prError && (
        <div className="warn-banner">
          ⚠ The refactor completed and is preserved as the final patch, but the pull request could not
          be opened: {prError}
        </div>
      )}

      <div className="card">
        <Stepper status={run.status} failedAt={stepInfo.lastStage} />
      </div>

      <div className="card objective-card">
        <div className="card-header">
          <h2>What the agent found</h2>
        </div>
        <div className="card-body">
          {run.objective ? (
            <>
              <div className="headline">{run.objective}</div>
              {run.plan?.summary && <div className="summary">{run.plan.summary}</div>}
            </>
          ) : (
            <div className="discovering">
              <div className="spinner" />
              Auditing the codebase to identify what needs refactoring…
            </div>
          )}
        </div>
      </div>

      {run.plan && (
        <div className="card">
          <div className="card-header">
            <h2>Execution plan</h2>
            <span className="meta">
              {stepInfo.committed.size}/{run.plan.steps.length} steps completed
            </span>
          </div>
          <ul className="steps">
            {run.plan.steps.map((step, index) => {
              const done = stepInfo.committed.has(index);
              const wasSkipped = stepInfo.skipped.has(index);
              const active = stepInfo.active === index && !done && !wasSkipped && !terminal;
              const stepAttempts = stepInfo.attempts.get(index) ?? [];
              return (
                <li key={step.id} className={done ? "done" : wasSkipped ? "skipped" : active ? "active" : ""}>
                  <span className="step-marker">{done ? "✓" : wasSkipped ? "↷" : index + 1}</span>
                  <div className="step-body">
                    <div className="step-title">
                      {step.title}
                      {wasSkipped && <span className="skip-tag">skipped</span>}
                    </div>
                    <div className="step-files">
                      {step.files.map((file) => (
                        <span key={file} className="file-tag">
                          {file}
                        </span>
                      ))}
                    </div>
                    {stepAttempts.length > 0 && (
                      <div className="attempt-row">
                        <span className="lbl">verification</span>
                        {stepAttempts.map((a, i) => (
                          <span key={i} className={`attempt ${a.passed ? "pass" : "fail"}`} title={`attempt ${a.attempt + 1}`}>
                            {a.attempt + 1}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  {done && (
                    <button className="btn btn-ghost btn-sm" onClick={() => showDiff(index)}>
                      View diff
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {diff && (
        <div className="card">
          <div className="card-header">
            <h2>{diff.step >= 0 ? `Step ${diff.step + 1} — changes` : "Final patch"}</h2>
            <button className="btn btn-ghost btn-sm" onClick={() => setDiff(null)}>
              Close
            </button>
          </div>
          <pre className="diff">{diff.text}</pre>
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h2>Live activity</h2>
          {!terminal && <span className="meta">streaming</span>}
        </div>
        <div className="feed" ref={feedRef}>
          {feedEvents.length === 0 ? (
            <div className="feed-empty">
              <div className="spinner" /> Waiting for the agent to start…
            </div>
          ) : (
            feedEvents.map((event) => {
              const { text, cls } = describeEvent(event);
              return (
                <div key={event.seq} className={`feed-line ${cls}`}>
                  <span className="feed-time">{new Date(event.ts * 1000).toLocaleTimeString()}</span>
                  <span className="feed-msg">{text}</span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </>
  );
}
