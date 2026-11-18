"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type Overview = {
  run_state: string;
  heartbeat: string | null;
  engine_alive: boolean;
  mode: string;
  task_counts: Record<string, number>;
  total_tasks: number;
  pending_approvals: number;
  safety_check: { passed: boolean; at: string } | null;
  projects: { id: number; name: string; status: string }[];
};

export type Agent = {
  role: string;
  desired_model: string;
  plan: string;
  provider: string;
  model: string;
  reasoning_effort: string | null;
  enabled: boolean;
  override_reason: string | null;
  verified: boolean | null;
  verified_at: string | null;
  detail: string;
  breaker: string;
  breaker_until: string | null;
  busy: boolean;
  running_purpose: string | null;
  current_task: string | null;
  calls: number;
  last_execution: { id: number; status: string; purpose: string; at: string; error_kind: string | null } | null;
};

export type Task = {
  id: number;
  project_id: number;
  key: string;
  title: string;
  state: string;
  assigned_role: string | null;
  preferred_role: string | null;
  attempts: number;
  depends_on: string[];
  requirement_keys: string[];
  owned_paths: string[];
  branch: string | null;
  head_commit: string | null;
  integrated_commit: string | null;
  blocked_reason: string | null;
  updated_at: string;
};

export type Approval = {
  id: number;
  kind: string;
  subject: string;
  status: string;
  project_id: number | null;
  task_id: number | null;
  created_at: string;
  decided_by: string | null;
  comment: string | null;
};

export class ApiError extends Error {}

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new ApiError(`${r.status} ${r.statusText} for ${path}`);
  return (await r.json()) as T;
}

export async function postJSON<T>(path: string, body: unknown = {}): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Workforce-Client": "dashboard" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {}
    throw new ApiError(`${r.status}: ${detail}`);
  }
  return (await r.json()) as T;
}

/** Poll an endpoint. Data is always the live server response - never synthesised. */
export function usePoll<T>(path: string | null, intervalMs = 3000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    if (!path) return;
    try {
      const d = await getJSON<T>(path);
      if (!alive.current) return;
      setData(d);
      setError(null);
      setUpdatedAt(new Date());
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    refresh();
    const t = setInterval(refresh, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [refresh, intervalMs]);

  return { data, error, updatedAt, refresh };
}
