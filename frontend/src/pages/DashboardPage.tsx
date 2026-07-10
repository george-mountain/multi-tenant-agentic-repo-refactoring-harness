import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import ConfirmDialog from "../components/ConfirmDialog";
import { GitHubIcon } from "../components/Logo";
import StatusBadge from "../components/StatusBadge";
import { api, ApiError } from "../lib/api";
import { repoName, timeAgo } from "../lib/format";
import type { Run, RunStats, StatusFilter } from "../lib/types";

const PAGE_SIZE = 20;
const ACTIVE = ["queued", "ingesting", "planning", "executing", "verifying", "finalizing", "awaiting_approval"];

const FILTERS: { key: StatusFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "active", label: "In progress" },
  { key: "succeeded", label: "Completed" },
  { key: "failed", label: "Failed" },
];

export default function DashboardPage() {
  const [repoUrl, setRepoUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [runs, setRuns] = useState<Run[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<RunStats | null>(null);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Run | null>(null);
  const [deleting, setDeleting] = useState(false);

  const navigate = useNavigate();
  const loadedCount = runs.length;
  const hasMore = loadedCount < total;
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 300);
    return () => clearTimeout(timer);
  }, [query]);

  const refreshStats = useCallback(async () => {
    try {
      setStats(await api.runStats());
    } catch {
      /* transient */
    }
  }, []);

  // (Re)load the currently-visible window of runs — keeps live statuses fresh and
  // preserves how far the user has scrolled. Runs on filter/search change and on poll.
  const reload = useCallback(
    async (count: number) => {
      const page = await api.listRuns({
        q: debouncedQuery,
        status: filter,
        limit: Math.max(count, PAGE_SIZE),
        offset: 0,
      });
      setRuns(page.items);
      setTotal(page.total);
    },
    [debouncedQuery, filter],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        await reload(PAGE_SIZE);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reload]);

  useEffect(() => {
    refreshStats();
    const timer = setInterval(() => {
      refreshStats();
      reload(runs.length || PAGE_SIZE);
    }, 5000);
    return () => clearInterval(timer);
  }, [refreshStats, reload, runs.length]);

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const page = await api.listRuns({
        q: debouncedQuery,
        status: filter,
        limit: PAGE_SIZE,
        offset: loadedCount,
      });
      setRuns((prev) => {
        const seen = new Set(prev.map((r) => r.id));
        return [...prev, ...page.items.filter((r) => !seen.has(r.id))];
      });
      setTotal(page.total);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, hasMore, debouncedQuery, filter, loadedCount]);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) loadMore();
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadMore]);

  async function launch(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const run = await api.createRun(repoUrl.trim());
      navigate(`/runs/${run.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the run. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    const run = pendingDelete;
    if (!run) return;
    setDeleting(true);
    setError("");
    try {
      await api.deleteRun(run.id);
      setRuns((prev) => prev.filter((r) => r.id !== run.id));
      setTotal((t) => Math.max(0, t - 1));
      refreshStats();
      setPendingDelete(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete the run.");
      setPendingDelete(null);
    } finally {
      setDeleting(false);
    }
  }

  async function retry(event: React.MouseEvent, run: Run) {
    event.stopPropagation();
    try {
      const fresh = await api.retryRun(run.id);
      navigate(`/runs/${fresh.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not retry the run.");
    }
  }

  return (
    <>
      <section className="hero">
        <h1>
          Refactor any repository, <em>automatically</em>
        </h1>
        <p>
          Paste a GitHub URL. The agent audits the codebase, decides what needs refactoring, verifies
          every change against your tests, and opens a pull request.
        </p>
      </section>

      <div className="launcher-bar">
        <form className="repo-launcher" onSubmit={launch}>
          <span className="repo-icon">
            <GitHubIcon size={18} />
          </span>
          <input
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/your-org/your-repo"
            pattern="https://(www\.)?github\.com/[\w.\-]+/[\w.\-]+/?"
            title="A GitHub repository URL, e.g. https://github.com/org/repo"
            required
            aria-label="GitHub repository URL"
          />
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? "Starting…" : "Refactor"}
          </button>
        </form>
      </div>
      {error && <div className="form-error" style={{ maxWidth: 640, margin: "0 auto 20px" }}>⚠ {error}</div>}

      <div className="stats-row">
        <div className="stat-card">
          <div className="label">Total runs</div>
          <div className="value">{stats?.total ?? "—"}</div>
        </div>
        <div className="stat-card">
          <div className="label">In progress</div>
          <div className="value">{stats?.active ?? "—"}</div>
        </div>
        <div className="stat-card">
          <div className="label">Completed</div>
          <div className="value">{stats?.succeeded ?? "—"}</div>
        </div>
        <div className="stat-card">
          <div className="label">Failed</div>
          <div className="value">{stats?.failed ?? "—"}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-header history-header">
          <h2>Run history</h2>
          <div className="history-controls">
            <div className="search-box">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="11" cy="11" r="7" />
                <path d="M21 21l-4.3-4.3" strokeLinecap="round" />
              </svg>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search repo or objective…"
                aria-label="Search runs"
              />
            </div>
            <div className="segmented">
              {FILTERS.map((f) => (
                <button
                  key={f.key}
                  className={filter === f.key ? "active" : ""}
                  onClick={() => setFilter(f.key)}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {loading ? (
          <div className="empty-state">
            <div className="spinner" style={{ margin: "0 auto 12px" }} />
            Loading runs…
          </div>
        ) : runs.length === 0 ? (
          <div className="empty-state">
            <div className="icon">🗂️</div>
            {debouncedQuery || filter !== "all"
              ? "No runs match your search."
              : "No runs yet. Paste a repository URL above to start your first refactor."}
          </div>
        ) : (
          <>
            <table className="table">
              <thead>
                <tr>
                  <th>Repository</th>
                  <th>Status</th>
                  <th>Pull request</th>
                  <th style={{ textAlign: "right" }}>Started</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="clickable" onClick={() => navigate(`/runs/${run.id}`)}>
                    <td>
                      <div className="repo-cell">
                        <GitHubIcon size={15} />
                        {repoName(run.repo_url)}
                      </div>
                      <div className="cell-sub">
                        {run.objective || (ACTIVE.includes(run.status) ? "Auditing codebase…" : "—")}
                      </div>
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td>
                      {run.pr_url ? (
                        <a href={run.pr_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                          View PR ↗
                        </a>
                      ) : (
                        <span className="cell-dim">—</span>
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <span className="cell-dim">{timeAgo(run.created_at)}</span>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {ACTIVE.includes(run.status) ? (
                        <span className="cell-dim" title="Finish before deleting">—</span>
                      ) : (
                        <div className="row-actions">
                          {run.status === "failed" && (
                            <button className="icon-btn retry-btn" title="Retry run" onClick={(e) => retry(e, run)}>
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                <path d="M21 12a9 9 0 11-3-6.7L21 8" strokeLinecap="round" strokeLinejoin="round" />
                                <path d="M21 3v5h-5" strokeLinecap="round" strokeLinejoin="round" />
                              </svg>
                            </button>
                          )}
                          <button
                            className="icon-btn"
                            title="Delete run"
                            onClick={(e) => {
                              e.stopPropagation();
                              setPendingDelete(run);
                            }}
                          >
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                              <path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2m2 0v14a1 1 0 01-1 1H6a1 1 0 01-1-1V6" strokeLinecap="round" strokeLinejoin="round" />
                              <path d="M10 11v6M14 11v6" strokeLinecap="round" />
                            </svg>
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div ref={sentinelRef} className="scroll-sentinel">
              {loadingMore && <div className="spinner" />}
              {!hasMore && runs.length > 0 && <span className="cell-dim">End of history</span>}
            </div>
          </>
        )}
      </div>

      {pendingDelete && (
        <ConfirmDialog
          destructive
          title="Delete this run?"
          message={`The run for ${repoName(pendingDelete.repo_url)} and its stored artifacts will be permanently removed. This cannot be undone.`}
          confirmLabel="Delete run"
          busy={deleting}
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </>
  );
}
