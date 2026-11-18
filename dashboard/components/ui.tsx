"use client";

export const STATE_COLORS: Record<string, string> = {
  BACKLOG: "bg-zinc-400",
  READY: "bg-sky-500",
  ASSIGNED: "bg-indigo-400",
  IMPLEMENTING: "bg-indigo-600",
  TESTING: "bg-amber-500",
  REVIEWING: "bg-violet-500",
  INTEGRATING: "bg-teal-500",
  COMPLETED: "bg-emerald-600",
  NEEDS_FIX: "bg-orange-500",
  BLOCKED: "bg-red-600",
  PAUSED: "bg-zinc-500",
  FAILED: "bg-red-800",
  CANCELLED: "bg-zinc-700",
};

export const STATE_ORDER = [
  "BACKLOG", "READY", "ASSIGNED", "IMPLEMENTING", "TESTING", "REVIEWING", "INTEGRATING", "COMPLETED",
  "NEEDS_FIX", "BLOCKED", "PAUSED", "FAILED", "CANCELLED",
];

export function StateBadge({ state }: { state: string }) {
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-semibold text-white ${STATE_COLORS[state] ?? "bg-zinc-500"}`}>
      {state}
    </span>
  );
}

export function Dot({ ok, title }: { ok: boolean | null; title?: string }) {
  const c = ok === null ? "bg-zinc-400" : ok ? "bg-emerald-500" : "bg-red-500";
  return <span title={title} className={`inline-block h-2.5 w-2.5 rounded-full ${c}`} />;
}

export function Section({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="panel p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide muted">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-6 text-center text-sm muted">{children}</p>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <p className="rounded bg-red-500/10 px-3 py-2 text-sm text-red-600">Live data unavailable: {error}</p>;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "-";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(iso).toLocaleString();
}
