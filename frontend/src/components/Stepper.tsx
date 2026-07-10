import { Fragment } from "react";

const STAGES = [
  { key: "ingest", label: "Analyze" },
  { key: "plan", label: "Audit & Plan" },
  { key: "execute", label: "Refactor & Verify" },
  { key: "finalize", label: "Final checks" },
  { key: "pr", label: "Pull request" },
];

function stageIndex(status: string): number {
  switch (status) {
    case "queued":
    case "ingesting":
      return 0;
    case "planning":
    case "awaiting_approval":
      return 1;
    case "executing":
    case "verifying":
      return 2;
    case "finalizing":
      return 3;
    case "succeeded":
      return 5;
    default:
      return -1;
  }
}

export default function Stepper({ status, failedAt }: { status: string; failedAt?: string }) {
  const active = status === "failed" ? stageIndex(failedAt ?? "") : stageIndex(status);
  return (
    <div className="stepper">
      {STAGES.map((stage, index) => {
        const done = status === "succeeded" || index < active;
        const isActive = index === active && status !== "succeeded" && status !== "failed";
        const failed = status === "failed" && index === Math.max(active, 0);
        const cls = failed ? "failed" : done ? "done" : isActive ? "active" : "";
        return (
          <Fragment key={stage.key}>
            {index > 0 && <div className={`step-connector ${index <= active || status === "succeeded" ? "done" : ""}`} />}
            <div className={`step ${cls}`}>
              <div className="step-bubble">{done ? "✓" : failed ? "✕" : index + 1}</div>
              <div className="step-label">{stage.label}</div>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
