"use client";

import { useState } from "react";
import { Agent, Approval, Task, postJSON, usePoll } from "@/lib/api";
import { Dot, Empty, ErrorNote, Section, STATE_COLORS, STATE_ORDER, StateBadge, ago } from "./ui";

/* ------------------------------------------------------------------ agents */
export function AgentsView() {
  const { data, error } = usePoll<Agent[]>("/api/agents");
  return (
    <Section title="Agents">
      <ErrorNote error={error} />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="agents">
        {(data ?? []).map((a) => (
          <div key={a.role} className="rounded-lg border border-[var(--line)] p-3" data-testid={`agent-${a.role}`}>
            <div className="flex items-center justify-between">
              <span className="font-semibold">{a.role.replace("_", " ")}</span>
              <Dot ok={a.verified} title={a.detail} />
            </div>
            <div className="mt-1 text-sm">{a.desired_model}</div>
            <div className="mono text-xs muted">
              {a.provider} · {a.model}
              {a.reasoning_effort ? ` · ${a.reasoning_effort}` : ""}
            </div>
            <div className="mt-2 space-y-0.5 text-xs">
              <div>
                availability:{" "}
                {a.verified === null ? <span className="muted">not verified</span> : a.verified ? "verified" : <span className="text-red-600">unavailable</span>}
                {!a.enabled && <span className="text-red-600"> (disabled)</span>}
              </div>
              {a.verified === false && <div className="text-red-600 break-words">{a.detail.slice(0, 160)}</div>}
              {a.override_reason && <div className="text-amber-600">override: {a.override_reason}</div>}
              <div>
                status: {a.busy ? <b>working ({a.running_purpose})</b> : "idle"}
                {a.current_task ? ` · task ${a.current_task}` : ""}
              </div>
              <div>
                circuit: {a.breaker}
                {a.breaker === "open" && a.breaker_until ? ` until ${new Date(a.breaker_until).toLocaleTimeString()}` : ""}
              </div>
              <div className="muted">
                {a.calls} calls · last {a.last_execution ? `${a.last_execution.purpose} ${a.last_execution.status} ${ago(a.last_execution.at)}` : "never"}
              </div>
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

/* ------------------------------------------------------------------- tasks */
type TaskDetail = {
  key: string;
  title: string;
  description: string;
  state: string;
  attempts: number;
  acceptance_criteria: string[];
  feedback: { source: string; type: string; message: string }[];
  events: { from: string | null; to: string; reason: string; ts: string }[];
  tests: { id: number; kind: string; attempt: number; passed: boolean; summary: string; command: string; ts: string; output_tail: string }[];
  reviews: { verdict: string; summary: string; attempt: number; findings: { severity: string; category: string; requirement_keys: string[]; evidence: string; recommendation: string }[] }[];
  commits: { sha: string; branch: string; role: string; message: string; ts: string }[];
  executions: { id: number; role: string; purpose: string; status: string; model: string; duration_s: number | null; error_kind: string | null }[];
};

export function TasksView({ project }: { project: number | null }) {
  const { data, error, refresh } = usePoll<Task[]>(project ? `/api/tasks?project=${project}` : "/api/tasks");
  const [selected, setSelected] = useState<number | null>(null);
  const tasks = data ?? [];
  const counts: Record<string, number> = {};
  tasks.forEach((t) => (counts[t.state] = (counts[t.state] ?? 0) + 1));
  const done = counts["COMPLETED"] ?? 0;
  return (
    <div className="space-y-4">
      <Section title={`Development progress · ${done}/${tasks.length} completed`}>
        <ErrorNote error={error} />
        {tasks.length > 0 && (
          <div className="flex h-3 overflow-hidden rounded" data-testid="progress">
            {STATE_ORDER.filter((s) => counts[s]).map((s) => (
              <div key={s} title={`${s}: ${counts[s]}`} className={STATE_COLORS[s]} style={{ width: `${(100 * counts[s]) / tasks.length}%` }} />
            ))}
          </div>
        )}
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          {STATE_ORDER.filter((s) => counts[s]).map((s) => (
            <span key={s}>
              <StateBadge state={s} /> {counts[s]}
            </span>
          ))}
        </div>
      </Section>
      <Section title="Tasks & dependencies">
        {tasks.length === 0 ? (
          <Empty>No tasks yet. Approve requirements and run `workforce plan &lt;project&gt;`.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="grid-table" data-testid="tasks-table">
              <thead>
                <tr>
                  <th>Task</th><th>State</th><th>Developer</th><th>Attempts</th><th>Depends on</th><th>Requirements</th><th>Owned paths</th><th>Updated</th><th></th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id} className="cursor-pointer hover:bg-black/5" onClick={() => setSelected(t.id)}>
                    <td><b>{t.key}</b> <span className="muted">{t.title}</span>
                      {t.blocked_reason && <div className="text-xs text-red-600">{t.blocked_reason.slice(0, 160)}</div>}
                    </td>
                    <td><StateBadge state={t.state} /></td>
                    <td>{t.assigned_role ?? <span className="muted">{t.preferred_role ?? "-"}</span>}</td>
                    <td>{t.attempts}</td>
                    <td className="mono text-xs">{t.depends_on.join(", ") || "-"}</td>
                    <td className="mono text-xs">{t.requirement_keys.join(", ")}</td>
                    <td className="mono text-xs">{t.owned_paths.join(", ")}</td>
                    <td className="text-xs muted">{ago(t.updated_at)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <TaskActions task={t} onDone={refresh} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      {selected && <TaskDetailPanel id={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function TaskActions({ task, onDone }: { task: Task; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const act = async (action: string) => {
    setBusy(true);
    try {
      await postJSON(`/api/tasks/${task.id}/${action}`);
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy(false);
      onDone();
    }
  };
  return (
    <div className="flex gap-1">
      {(task.state === "BLOCKED" || task.state === "FAILED") && <button className="btn" disabled={busy} onClick={() => act("retry")}>Retry</button>}
      {task.state === "PAUSED" && <button className="btn" disabled={busy} onClick={() => act("resume")}>Resume</button>}
      {!["COMPLETED", "CANCELLED", "PAUSED"].includes(task.state) && (
        <>
          <button className="btn" disabled={busy} onClick={() => act("pause")}>Pause</button>
          <button className="btn" disabled={busy} onClick={() => confirm(`Cancel ${task.key}?`) && act("cancel")}>Cancel</button>
        </>
      )}
    </div>
  );
}

function TaskDetailPanel({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error } = usePoll<TaskDetail>(`/api/tasks/${id}`);
  return (
    <Section title={data ? `${data.key} · ${data.title}` : "Task"} right={<button className="btn" onClick={onClose}>Close</button>}>
      <ErrorNote error={error} />
      {data && (
        <div className="grid gap-4 lg:grid-cols-2" data-testid="task-detail">
          <div className="space-y-3 text-sm">
            <p className="whitespace-pre-wrap">{data.description}</p>
            <div>
              <h3 className="font-semibold">Timeline</h3>
              <ul className="mt-1 space-y-1 text-xs">
                {data.events.map((e, i) => (
                  <li key={i}><span className="muted">{new Date(e.ts).toLocaleTimeString()}</span> {e.from ?? "∅"} → <b>{e.to}</b> <span className="muted">{e.reason}</span></li>
                ))}
              </ul>
            </div>
            {data.feedback.length > 0 && (
              <div>
                <h3 className="font-semibold">Correction feedback</h3>
                <ul className="mt-1 space-y-1 text-xs">
                  {data.feedback.map((f, i) => <li key={i}><b>{f.source}/{f.type}</b>: {f.message}</li>)}
                </ul>
              </div>
            )}
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <h3 className="font-semibold">Verification results</h3>
              {data.tests.length === 0 ? <p className="muted text-xs">none yet</p> : (
                <ul className="mt-1 space-y-1 text-xs">
                  {data.tests.map((t) => (
                    <li key={t.id}>
                      <Dot ok={t.passed} /> <b>{t.kind}</b> (attempt {t.attempt}) {t.summary} <span className="mono muted">{t.command}</span>
                      {!t.passed && <details><summary className="cursor-pointer muted">output</summary><pre className="mono max-h-60 overflow-auto whitespace-pre-wrap text-[11px]">{t.output_tail}</pre></details>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <h3 className="font-semibold">Review verdicts</h3>
              {data.reviews.length === 0 ? <p className="muted text-xs">none yet</p> : data.reviews.map((r, i) => (
                <div key={i} className="mt-1 text-xs">
                  <b className={r.verdict === "APPROVED" ? "text-emerald-600" : "text-red-600"}>{r.verdict}</b> (attempt {r.attempt}) {r.summary}
                  <ul className="ml-4 list-disc">
                    {r.findings.map((f, j) => <li key={j}>[{f.severity}/{f.category}] {f.requirement_keys.join(",")}: {f.evidence} → {f.recommendation}</li>)}
                  </ul>
                </div>
              ))}
            </div>
            <div>
              <h3 className="font-semibold">Commits</h3>
              <ul className="mt-1 space-y-1 text-xs">
                {data.commits.map((c) => <li key={c.sha + c.branch}><span className="mono">{c.sha.slice(0, 10)}</span> {c.role} · {c.branch} · {c.message}</li>)}
              </ul>
            </div>
            <div>
              <h3 className="font-semibold">Agent executions</h3>
              <ul className="mt-1 space-y-1 text-xs">
                {data.executions.map((e) => <li key={e.id}>#{e.id} {e.role}/{e.purpose} <b>{e.status}</b>{e.error_kind ? ` (${e.error_kind})` : ""} · {e.model} · {e.duration_s?.toFixed(1) ?? "-"}s</li>)}
              </ul>
            </div>
          </div>
        </div>
      )}
    </Section>
  );
}

/* --------------------------------------------------------------- approvals */
export function ApprovalsView() {
  const [showAll, setShowAll] = useState(false);
  const { data, error, refresh } = usePoll<Approval[]>(`/api/approvals?status=${showAll ? "all" : "pending"}`);
  const [comment, setComment] = useState<Record<number, string>>({});
  const decide = async (id: number, approve: boolean) => {
    try {
      await postJSON(`/api/approvals/${id}/decide`, { approve, comment: comment[id] || null });
    } catch (e) {
      alert(String(e));
    }
    refresh();
  };
  return (
    <Section title="Approval requests" right={<label className="text-xs"><input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> history</label>}>
      <ErrorNote error={error} />
      {(data ?? []).length === 0 ? <Empty>No {showAll ? "" : "pending "}approval requests.</Empty> : (
        <ul className="space-y-2" data-testid="approvals">
          {(data ?? []).map((a) => (
            <li key={a.id} className="rounded border border-[var(--line)] p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-black/10 px-1.5 text-xs font-semibold uppercase">{a.kind}</span>
                <span className="muted text-xs">#{a.id} · {ago(a.created_at)}</span>
                {a.status !== "pending" && <span className="text-xs">→ {a.status} by {a.decided_by}{a.comment ? `: ${a.comment}` : ""}</span>}
              </div>
              <p className="mt-1">{a.subject}</p>
              {a.status === "pending" && (
                <div className="mt-2 flex flex-wrap gap-2">
                  <input className="btn flex-1" placeholder="comment (optional)" value={comment[a.id] ?? ""} onChange={(e) => setComment({ ...comment, [a.id]: e.target.value })} />
                  <button className="btn btn-primary" onClick={() => decide(a.id, true)}>Approve</button>
                  <button className="btn" onClick={() => decide(a.id, false)}>Reject</button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/* ------------------------------------------------------------ traceability */
type TraceRow = {
  requirement: string; text: string; status: string; category: string; decisions: string[];
  tasks: { key: string; state: string }[]; commits: string[]; tests: { task: string; kind: string; passed: boolean }[];
  reviews: { task: string; verdict: string }[]; final_status: string;
};

export function TraceView({ project }: { project: number | null }) {
  const { data, error } = usePoll<TraceRow[]>(project ? `/api/traceability?project=${project}` : null, 5000);
  if (!project) return <Section title="Requirement traceability"><Empty>Select a project.</Empty></Section>;
  return (
    <Section title="Requirement traceability">
      <ErrorNote error={error} />
      <div className="overflow-x-auto">
        <table className="grid-table" data-testid="trace-table">
          <thead><tr><th>Requirement</th><th>Status</th><th>Decisions</th><th>Tasks</th><th>Commits</th><th>Tests</th><th>Review</th><th>Final</th></tr></thead>
          <tbody>
            {(data ?? []).map((r) => (
              <tr key={r.requirement}>
                <td><b className="mono">{r.requirement}</b><div className="text-xs muted">{r.text}</div></td>
                <td className="text-xs">{r.status}</td>
                <td className="mono text-xs">{r.decisions.join(", ") || "-"}</td>
                <td className="text-xs">{r.tasks.map((t) => <div key={t.key}>{t.key} <StateBadge state={t.state} /></div>)}</td>
                <td className="mono text-xs">{r.commits.slice(0, 4).join(" ") || "-"}{r.commits.length > 4 ? " …" : ""}</td>
                <td className="text-xs">{r.tests.map((t, i) => <div key={i}><Dot ok={t.passed} /> {t.task}:{t.kind}</div>)}</td>
                <td className="text-xs">{r.reviews.map((v, i) => <div key={i}>{v.task}: {v.verdict}</div>)}</td>
                <td className="text-xs font-semibold">{r.final_status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

/* --------------------------------------------------------------------- git */
type GitState = {
  branches: { name: string; sha: string; date: string }[];
  integration_branch: string; target_branch: string; push_authorized: boolean;
  commits: { sha: string; author: string; date: string; subject: string }[];
};

export function GitView({ project }: { project: number | null }) {
  const { data, error } = usePoll<GitState>(project ? `/api/git?project=${project}` : null, 5000);
  if (!project) return <Section title="Git"><Empty>Select a project.</Empty></Section>;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section title="Branches">
        <ErrorNote error={error} />
        <p className="mb-2 text-xs muted">Target <b>{data?.target_branch}</b> is protected; promotion requires approval. Push {data?.push_authorized ? "authorized" : "not authorized"}.</p>
        <table className="grid-table"><tbody>
          {(data?.branches ?? []).map((b) => <tr key={b.name}><td className="mono text-xs">{b.name}</td><td className="mono text-xs">{b.sha.slice(0, 10)}</td><td className="text-xs muted">{ago(b.date)}</td></tr>)}
        </tbody></table>
      </Section>
      <Section title={`Commits on ${data?.integration_branch ?? "integration"}`}>
        <table className="grid-table"><tbody>
          {(data?.commits ?? []).map((c) => <tr key={c.sha}><td className="mono text-xs">{c.sha.slice(0, 10)}</td><td className="text-xs">{c.subject}</td><td className="text-xs muted">{c.author}</td></tr>)}
        </tbody></table>
      </Section>
    </div>
  );
}

/* -------------------------------------------------------------- executions */
type Execution = { id: number; task_id: number | null; role: string; purpose: string; provider: string; model: string; status: string; error_kind: string | null; error: string; duration_s: number | null; usage: Record<string, number>; started_at: string };
type Errors = { providers: { provider: string; breaker: string; consecutive_failures: number; open_until: string | null; last_error: string }[]; failures: { id: number; role: string; provider: string; status: string; error_kind: string; error: string; at: string }[] };
type Usage = { note: string; rows: { role: string; provider: string; model: string; calls: number; ok: number; failed: number; input_tokens: number; output_tokens: number; seconds: number; est_cost_usd: number }[] };

export function ExecutionsView() {
  const ex = usePoll<Execution[]>("/api/executions?limit=100");
  const er = usePoll<Errors>("/api/errors", 5000);
  const us = usePoll<Usage>("/api/usage", 10000);
  return (
    <div className="space-y-4">
      <Section title="Provider health & errors">
        <ErrorNote error={er.error} />
        <div className="mb-3 flex flex-wrap gap-3 text-sm">
          {(er.data?.providers ?? []).map((p) => (
            <span key={p.provider} className="rounded border border-[var(--line)] px-2 py-1">
              <Dot ok={p.breaker === "closed"} /> {p.provider}: circuit {p.breaker} ({p.consecutive_failures} consecutive failures)
            </span>
          ))}
          {(er.data?.providers ?? []).length === 0 && <span className="muted">No provider activity recorded yet.</span>}
        </div>
        <table className="grid-table"><tbody>
          {(er.data?.failures ?? []).slice(0, 20).map((f) => <tr key={f.id}><td className="text-xs muted">{ago(f.at)}</td><td className="text-xs">{f.role}</td><td className="text-xs">{f.provider}</td><td className="text-xs">{f.status}/{f.error_kind}</td><td className="text-xs">{f.error}</td></tr>)}
        </tbody></table>
      </Section>
      <Section title="Usage (reported by each CLI)">
        <p className="mb-2 text-xs muted">{us.data?.note}</p>
        <table className="grid-table"><thead><tr><th>Role</th><th>Model</th><th>Calls</th><th>OK</th><th>Failed</th><th>In tokens</th><th>Out tokens</th><th>Time</th><th>Est. list cost</th></tr></thead><tbody>
          {(us.data?.rows ?? []).map((u) => <tr key={u.role + u.model}><td>{u.role}</td><td className="mono text-xs">{u.provider}/{u.model}</td><td>{u.calls}</td><td>{u.ok}</td><td>{u.failed}</td><td>{u.input_tokens.toLocaleString()}</td><td>{u.output_tokens.toLocaleString()}</td><td>{Math.round(u.seconds)}s</td><td>${u.est_cost_usd.toFixed(2)}</td></tr>)}
        </tbody></table>
      </Section>
      <Section title="Execution history">
        <ErrorNote error={ex.error} />
        <div className="overflow-x-auto"><table className="grid-table"><thead><tr><th>#</th><th>When</th><th>Role</th><th>Purpose</th><th>Model</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead><tbody>
          {(ex.data ?? []).map((e) => <tr key={e.id}><td>{e.id}</td><td className="text-xs muted">{ago(e.started_at)}</td><td>{e.role}</td><td>{e.purpose}</td><td className="mono text-xs">{e.model}</td><td>{e.status}</td><td>{e.duration_s?.toFixed(1) ?? "-"}s</td><td className="text-xs text-red-600">{e.error_kind ? `${e.error_kind}: ${e.error.slice(0, 120)}` : ""}</td></tr>)}
        </tbody></table></div>
      </Section>
    </div>
  );
}

/* -------------------------------------------------------------------- logs */
type Logs = { audit: { ts: string; actor: string; action: string; task_id: number | null; details: Record<string, unknown> }[]; system: Record<string, unknown>[] };

export function LogsView() {
  const { data, error } = usePoll<Logs>("/api/logs?limit=200", 5000);
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Section title="Audit log">
        <ErrorNote error={error} />
        <ul className="mono max-h-[70vh] space-y-1 overflow-auto text-[11px]">
          {(data?.audit ?? []).map((a, i) => <li key={i}><span className="muted">{new Date(a.ts).toLocaleTimeString()}</span> <b>{a.actor}</b> {a.action} {JSON.stringify(a.details).slice(0, 220)}</li>)}
        </ul>
      </Section>
      <Section title="System log (structured JSON)">
        <ul className="mono max-h-[70vh] space-y-1 overflow-auto text-[11px]">
          {(data?.system ?? []).map((l, i) => <li key={i}>{JSON.stringify(l).slice(0, 300)}</li>)}
          {(data?.system ?? []).length === 0 && <li className="muted">empty</li>}
        </ul>
      </Section>
    </div>
  );
}
