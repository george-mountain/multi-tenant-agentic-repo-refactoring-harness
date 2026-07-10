const LABELS: Record<string, string> = {
  queued: "Queued",
  ingesting: "Analyzing repo",
  planning: "Auditing & planning",
  awaiting_approval: "Awaiting approval",
  executing: "Refactoring",
  verifying: "Verifying",
  finalizing: "Finalizing",
  succeeded: "Completed",
  failed: "Failed",
};

const RUNNING = ["queued", "ingesting", "planning", "executing", "verifying", "finalizing"];

export default function StatusBadge({ status }: { status: string }) {
  const cls = RUNNING.includes(status)
    ? "running"
    : status === "awaiting_approval"
      ? "awaiting"
      : status;
  return (
    <span className={`pill ${cls}`}>
      <span className="dot" />
      {LABELS[status] ?? status}
    </span>
  );
}
