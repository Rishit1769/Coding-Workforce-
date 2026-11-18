"use client";

import { useEffect, useState } from "react";
import { Overview, postJSON, usePoll } from "@/lib/api";
import { AgentsView, ApprovalsView, ExecutionsView, GitView, LogsView, TasksView, TraceView, withProject } from "@/components/views";
import { Dot, ErrorNote, ago } from "@/components/ui";

const TABS = ["Overview", "Tasks", "Approvals", "Traceability", "Git", "Executions", "Logs"] as const;
type Tab = (typeof TABS)[number];

export default function Dashboard() {
  const [tab, setTab] = useState<Tab>("Overview");
  const [project, setProject] = useState<number | null>(null);
  const { data: ov, error, refresh } = usePoll<Overview>(withProject("/api/overview", project), 2500);

  useEffect(() => {
    if (project === null && ov?.projects.length) setProject(ov.projects[ov.projects.length - 1].id);
  }, [ov, project]);

  const control = async (action: "pause" | "resume" | "stop") => {
    if (action === "stop" && !confirm("Stop the engine gracefully?")) return;
    try {
      await postJSON(withProject(`/api/control/${action}`, project));
    } catch (e) {
      alert(String(e));
    }
    refresh();
  };

  return (
    <div className="mx-auto max-w-[1500px] px-4 py-4">
      <header className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-bold">Coding Workforce</h1>
        <span className="text-sm" data-testid="run-state">
          <Dot ok={ov ? ov.engine_alive : null} title={ov?.heartbeat ?? ""} /> engine {ov ? (ov.engine_alive ? "running" : "not running") : "…"} · state <b>{ov?.run_state ?? "…"}</b> · heartbeat {ago(ov?.heartbeat)} · mode {ov?.mode ?? "…"}
        </span>
        <div className="flex gap-1">
          <button className="btn" onClick={() => control("pause")}>Pause</button>
          <button className="btn" onClick={() => control("resume")}>Resume</button>
          <button className="btn" onClick={() => control("stop")}>Stop</button>
        </div>
        <select className="btn ml-auto" value={project ?? ""} onChange={(e) => setProject(e.target.value ? Number(e.target.value) : null)} data-testid="project-select">
          {(ov?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.status})</option>)}
          {(ov?.projects ?? []).length === 0 && <option value="">no projects</option>}
        </select>
      </header>
      <ErrorNote error={error} />
      <nav className="mb-4 flex flex-wrap gap-1 border-b border-[var(--line)]">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`px-3 py-1.5 text-sm ${tab === t ? "border-b-2 border-[var(--accent)] font-semibold" : "muted"}`}>
            {t}
            {t === "Approvals" && ov?.pending_approvals ? <span className="ml-1 rounded-full bg-red-600 px-1.5 text-[11px] text-white">{ov.pending_approvals}</span> : null}
          </button>
        ))}
      </nav>
      <main className="space-y-4">
        {tab === "Overview" && (
          <>
            <AgentsView project={project} />
            <TasksView project={project} />
            {ov && (
              <p className="text-xs muted">
                Unattended mode: {ov.safety_check ? (ov.safety_check.passed ? `safety check passed ${ago(ov.safety_check.at)}` : "last safety check failed") : "safety check never run - unattended mode disabled"}
              </p>
            )}
          </>
        )}
        {tab === "Tasks" && <TasksView project={project} />}
        {tab === "Approvals" && <ApprovalsView project={project} />}
        {tab === "Traceability" && <TraceView project={project} />}
        {tab === "Git" && <GitView project={project} />}
        {tab === "Executions" && <ExecutionsView project={project} />}
        {tab === "Logs" && <LogsView project={project} />}
      </main>
    </div>
  );
}
