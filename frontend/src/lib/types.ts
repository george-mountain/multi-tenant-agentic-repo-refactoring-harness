export interface Session {
  token: string;
  tenant_id: string;
  tenant_name: string;
  email: string;
}

export interface PlanStep {
  id: string;
  title: string;
  rationale: string;
  action: "modify" | "create";
  files: string[];
  verification_hint: string;
}

export interface Plan {
  objective: string;
  summary: string;
  steps: PlanStep[];
}

export interface Run {
  id: string;
  repo_url: string;
  base_branch: string;
  objective: string;
  provider: string;
  model: string;
  status: string;
  branch: string;
  pr_url: string;
  failure: string;
  approval_required: boolean;
  plan: Plan | null;
  tokens: { input_tokens: number; output_tokens: number } | null;
  created_at: string;
}

export interface RunList {
  items: Run[];
  total: number;
  limit: number;
  offset: number;
}

export interface RunStats {
  total: number;
  active: number;
  succeeded: number;
  failed: number;
}

export type StatusFilter = "all" | "active" | "succeeded" | "failed";

export interface RunEvent {
  run_id: string;
  seq: number;
  type: string;
  ts: number;
  data: Record<string, any>;
}

export const TERMINAL_STATUSES = ["succeeded", "failed"];
