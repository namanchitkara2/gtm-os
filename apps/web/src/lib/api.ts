/**
 * The dashboard's data layer.
 *
 * Every screen here is a projection of a run that is happening somewhere else —
 * started from someone's Claude Code, executing against a browser on someone's
 * laptop. So the UI is a reader, and the only interesting question is when to
 * read again. Polling is scoped to what is actually in flight: an active run is
 * worth a second, a finished one is worth nothing.
 */

const json = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error ?? detail;
    } catch {
      /* non-JSON error body; the status text is all we have */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
};

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

// ------------------------------------------------------------------- types --

export type RunStatus =
  | "queued" | "running" | "awaiting_bridge" | "awaiting_approval"
  | "succeeded" | "failed" | "cancelled";

export type StepStatus = "running" | "succeeded" | "failed" | "skipped" | "cached";

export interface Me {
  tenant_id: string;
  user_id: string;
  dev_mode: boolean;
  tenant: { name: string; slug: string; plan: string; daily_bridge_budget: number } | null;
  user: { email: string; display_name: string | null } | null;
  today: { bridge_calls: number; write_actions: number; cost_usd: number };
}

export interface Bridge {
  id: string;
  linkedin_username: string | null;
  bridge_version: string | null;
  platform: string | null;
  status: "offline" | "online" | "busy" | "error";
  attached: boolean;
  ready: boolean;
  actions_today: number | null;
  last_seen_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface RunSummary {
  id: string;
  workflow: string;
  status: RunStatus;
  progress: { found?: number; people?: number; from_cache?: number };
  limits: { max_people?: number };
  blocked_reason: string | null;
  error: string | null;
  total_bridge_calls: number;
  total_cost_usd: number;
  target_name: string | null;
  target_brief: string | null;
  people: number;
  from_cache: number;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
}

export interface RunStep {
  sequence: number;
  node: string;
  status: StepStatus;
  input_summary: Record<string, unknown>;
  output_summary: Record<string, unknown>;
  bridge_calls: number;
  cost_usd: number;
  retries: number;
  error: string | null;
  duration_ms: number | null;
}

export interface RunDetail extends RunSummary {
  steps: RunStep[];
  target_spec: Record<string, unknown> | null;
}

export interface Person {
  id: string;
  name: string;
  headline: string | null;
  location: string | null;
  linkedin_username: string | null;
  linkedin_url: string | null;
  score: number | null;
  score_rationale: string | null;
  from_cache: boolean;
  email_status: string;
  phone_status: string;
  emails: { value: string; sources: (string | null)[] }[];
  phones: { value: string; sources: (string | null)[] }[];
  refreshed_at: string;
}

export interface Target {
  id: string;
  kind: string;
  name: string;
  brief: string;
  spec: Record<string, unknown>;
  interpretation_notes: unknown[];
  runs: number;
  created_at: string;
}

export interface Workflow {
  slug: string;
  name?: string;
  title?: string;
  description?: string;
  [k: string]: unknown;
}

// ------------------------------------------------------------------- calls --

export const api = {
  me: () => json<Me>("/api/me"),
  bridges: () => json<{ bridges: Bridge[]; any_online: boolean }>("/api/bridges"),
  runs: () => json<{ runs: RunSummary[] }>("/api/runs"),
  run: (id: string) => json<RunDetail>(`/api/runs/${id}`),
  runPeople: (id: string) => json<{ people: Person[] }>(`/api/runs/${id}/people`),
  cancelRun: (id: string) =>
    json<{ cancelled: boolean }>(`/api/runs/${id}/cancel`, { method: "POST" }),
  people: (q?: string) =>
    json<{ people: Person[] }>(`/api/people${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  targets: () => json<Target[]>("/api/targets"),
  workflows: () => json<Workflow[]>("/api/workflows"),
  approvePairing: (code: string) =>
    json<{ ok?: boolean; bridge_id?: string; error?: string }>("/api/bridge/pair/approve", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
};

/** A run is worth re-reading only while it can still change. */
export const isLive = (status: RunStatus) =>
  status === "queued" || status === "running" || status === "awaiting_bridge" ||
  status === "awaiting_approval";
