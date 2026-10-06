import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import type { Job, Page } from "../../api/types";
import { ErrorBox, Spinner } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import AdminNav from "./AdminNav";

const STATUS_COLORS: Record<string, string> = {
  queued: "text-zinc-400", running: "text-sky-400", done: "text-emerald-400", failed: "text-red-400", cancelled: "text-zinc-500",
};

export default function Jobs() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<Page<Job>>("/jobs", { query: { limit: 100 } }),
    // Poll quickly while something is happening, slowly otherwise.
    refetchInterval: (q) => (q.state.data?.items.some((j) => j.status === "running" || j.status === "queued") ? 1500 : 10_000),
  });
  const cancel = useWrite((id: number) => api(`/jobs/${id}/cancel`, { method: "POST" }));
  return (
    <div className="mx-auto max-w-5xl">
      <AdminNav />
      <h1 className="mb-4 text-xl font-semibold">Jobs</h1>
      <WorkerSettings />
      {jobs.isPending ? <Spinner /> : jobs.isError ? <ErrorBox error={jobs.error} /> : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
              <tr><th className="p-3">#</th><th className="p-3">Job</th><th className="p-3">Status</th><th className="p-3">Details</th><th className="p-3">Started</th><th /></tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {jobs.data.items.map((j) => (
                <tr key={j.id} data-testid="job-row">
                  <td className="p-3 text-zinc-500">{j.id}</td>
                  <td className="p-3">
                    <div className="font-medium">{j.kind}</div>
                    {typeof j.payload.path === "string" && <div className="max-w-xs truncate font-mono text-xs text-zinc-500">{j.payload.path}</div>}
                  </td>
                  <td className={`p-3 ${STATUS_COLORS[j.status]}`}>
                    {j.status}
                    {j.status === "running" && j.progress != null && (
                      <div className="mt-1 h-1 w-24 rounded bg-zinc-800"><div className="h-full rounded bg-sky-500" style={{ width: `${j.progress * 100}%` }} /></div>
                    )}
                  </td>
                  <td className="max-w-md p-3 text-xs text-zinc-400">
                    {j.error ? <pre className="whitespace-pre-wrap text-red-300">{j.error}</pre>
                      : <span className="whitespace-pre-line">{j.message}</span>}
                  </td>
                  <td className="whitespace-nowrap p-3 text-xs text-zinc-500">{formatDateTime(j.started_at ?? j.created_at)}</td>
                  <td className="p-3">
                    {(j.status === "queued" || j.status === "running") && (
                      <button className="btn-ghost py-1 text-xs" onClick={() => cancel.mutate(j.id)}>Cancel</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!jobs.data.items.length && <p className="p-6 text-center text-sm text-zinc-500">No jobs yet.</p>}
        </div>
      )}
    </div>
  );
}

/** How many preview-generation jobs may run at once (scans always run alone). */
function WorkerSettings() {
  const q = useQuery({
    queryKey: ["worker-settings"],
    queryFn: () => api<{ generate_jobs: number; max_generate_jobs: number }>("/jobs/settings"),
  });
  const [value, setValue] = useState(1);
  useEffect(() => { if (q.data) setValue(q.data.generate_jobs); }, [q.data]);
  const save = useWrite((n: number) => api("/jobs/settings", { method: "PUT", json: { generate_jobs: n } }));
  if (!q.data) return null;
  return (
    <section className="card mb-4 flex flex-wrap items-center gap-3 p-4 text-sm" data-testid="worker-settings">
      <label className="flex items-center gap-2">
        Run up to
        <select className="input w-auto" aria-label="Preview jobs at once" value={value}
                onChange={(e) => setValue(Number(e.target.value))}>
          {Array.from({ length: q.data.max_generate_jobs }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        preview jobs at once
      </label>
      <button className="btn-primary py-1" disabled={value === q.data.generate_jobs || save.isPending}
              onClick={() => save.mutate(value)}>Save</button>
      <p className="w-full text-xs text-zinc-500">
        Thumbnails and scrub previews are made in batches; more at once finishes sooner but uses more CPU. Scans and
        other disk-heavy jobs always run on their own. The worker's CPU limit (WORKER_CPUS, default 2) still applies,
        so raise it too on a strong server.
      </p>
      {save.isError && <ErrorBox error={save.error} />}
    </section>
  );
}
