import type { Run, RunList, RunStats, Session } from "./types";

const SESSION_KEY = "harness_session";

export function loadSession(): Session | null {
  const raw = localStorage.getItem(SESSION_KEY);
  return raw ? (JSON.parse(raw) as Session) : null;
}

export function saveSession(session: Session | null): void {
  if (session) {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } else {
    localStorage.removeItem(SESSION_KEY);
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const session = loadSession();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (session) headers["Authorization"] = `Bearer ${session.token}`;
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as unknown as T;
}

export const api = {
  register: (tenant_name: string, email: string, password: string) =>
    request<Session>("/auth/register", { method: "POST", body: JSON.stringify({ tenant_name, email, password }) }),
  login: (email: string, password: string) =>
    request<Session>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  listRuns: (params: { q?: string; status?: string; limit?: number; offset?: number } = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.status && params.status !== "all") search.set("status", params.status);
    search.set("limit", String(params.limit ?? 20));
    search.set("offset", String(params.offset ?? 0));
    return request<RunList>(`/runs?${search.toString()}`);
  },
  runStats: () => request<RunStats>("/runs/stats"),
  getRun: (id: string) => request<Run>(`/runs/${id}`),
  createRun: (repo_url: string) =>
    request<Run>("/runs", { method: "POST", body: JSON.stringify({ repo_url }) }),
  deleteRun: (id: string) => request<void>(`/runs/${id}`, { method: "DELETE" }),
  retryRun: (id: string) => request<Run>(`/runs/${id}/retry`, { method: "POST" }),
  approveRun: (id: string) => request<Run>(`/runs/${id}/approve`, { method: "POST" }),
  stepDiff: (id: string, step: number) => request<string>(`/runs/${id}/steps/${step}/diff`),
  finalPatch: (id: string) => request<string>(`/runs/${id}/patch`),
};

export function eventStreamUrl(runId: string): string {
  const session = loadSession();
  return `/api/runs/${runId}/events?token=${encodeURIComponent(session?.token ?? "")}`;
}
