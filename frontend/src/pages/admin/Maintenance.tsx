import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import { ErrorBox, Spinner, Toggle } from "../../components/ui";
import { formatBytes, formatDateTime } from "../../lib/format";
import AdminNav from "./AdminNav";

interface Tally { files: number; bytes: number }
interface Swept extends Tally {
  scene_assets: Tally; image_thumbnails: Tally; entity_images: Tally; temporary: Tally; links_fixed: number;
}
interface Removed {
  scenes: number; images: number; galleries: number; files: number; still_on_disk: number; libraries_skipped: number;
}
interface CleanupReport { at: string; dry_run: boolean; min_missing_days: number; removed: Removed; generated: Swept }
interface MaintenanceReport {
  at: string; sessions_removed: number; jobs_removed: number; generated: Swept; removed?: Removed;
  tables_optimized: number; database_bytes: number; database_bytes_before: number; seconds: number;
}
interface Settings { enabled: boolean; hour: number; remove_missing_after_days: number }
interface JobState { id: number; status: string; progress: number | null; message: string | null; error: string | null }
interface Overview {
  settings: Settings;
  server_time: string;
  database_bytes: number;
  missing_files: number;
  maintenance_report: MaintenanceReport | null;
  cleanup_report: CleanupReport | null;
  jobs: { cleanup?: JobState; maintenance?: JobState };
}

const active = (j?: JobState) => j?.status === "queued" || j?.status === "running";
const nothingToDo = (r: CleanupReport) =>
  !r.removed.scenes && !r.removed.images && !r.removed.galleries && !r.generated.files && !r.generated.links_fixed;

export default function Maintenance() {
  const q = useQuery({
    queryKey: ["maintenance"],
    queryFn: () => api<Overview>("/maintenance"),
    refetchInterval: (query) => (active(query.state.data?.jobs.cleanup) || active(query.state.data?.jobs.maintenance) ? 1500 : false),
  });
  return (
    <div className="mx-auto max-w-5xl">
      <AdminNav />
      <h1 className="mb-4 text-xl font-semibold">Maintenance</h1>
      {q.isPending ? <Spinner /> : q.isError ? <ErrorBox error={q.error} /> : (
        <>
          <Encryption />
          <Unreadable />
          <Cleanup data={q.data} />
          <Routine data={q.data} />
        </>
      )}
    </div>
  );
}

function JobLine({ job }: { job?: JobState }) {
  if (!job) return null;
  if (active(job)) {
    return (
      <p className="mt-3 text-sm text-info" role="status" data-testid="job-progress">
        {job.status === "queued" ? "Waiting to start…" : `Working… ${job.message?.split("\n")[0] ?? ""}`}
      </p>
    );
  }
  if (job.status === "failed") return <p className="mt-3 whitespace-pre-line text-sm text-danger" role="alert">{job.error}</p>;
  return null;
}

function SweptLine({ g }: { g: Swept }) {
  const parts = [
    g.scene_assets.files && `${g.scene_assets.files} scene previews`,
    g.image_thumbnails.files && `${g.image_thumbnails.files} image thumbnails`,
    g.entity_images.files && `${g.entity_images.files} pictures of deleted performers, studios or tags`,
    g.temporary.files && `${g.temporary.files} temporary files`,
  ].filter(Boolean);
  return <>{g.files} generated files ({formatBytes(g.bytes)}){parts.length > 0 && `: ${parts.join(", ")}`}</>;
}

function Cleanup({ data }: { data: Overview }) {
  const [days, setDays] = useState(0);
  const start = useWrite((dry_run: boolean) =>
    api("/maintenance/cleanup", { json: { dry_run, min_missing_days: days } }));
  const busy = active(data.jobs.cleanup) || start.isPending;
  const r = data.cleanup_report;
  const remove = () => confirm(
    "Remove the entries of media that is no longer on disk, including their ratings, tags and other details? "
    + "This cannot be undone. Your media files are never touched.") && start.mutate(false);
  return (
    <section className="card p-5" data-testid="cleanup">
      <h2 className="mb-1 text-lg font-semibold">Clean up removed media</h2>
      <p className="mb-4 text-sm text-text-muted">
        When files are deleted outside of this app, their entries stay (marked missing) so nothing is lost if they
        come back. A cleanup rescans the libraries, then removes the entries of files that are really gone, together
        with the previews and thumbnails made for them. Moved or renamed files are followed, not removed.
        A library whose folder is unavailable, or that is disabled, is left alone.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={busy} onClick={() => start.mutate(true)}>Check what would be removed</button>
        <button className="btn-danger" disabled={busy} onClick={remove}>Clean up now…</button>
        <label className="flex items-center gap-2 text-sm text-text-muted">
          Only files missing for at least
          <input type="number" min={0} max={3650} className="input w-20" aria-label="Minimum days missing"
                 value={days} onChange={(e) => setDays(Math.max(0, Number(e.target.value) || 0))} />
          days
        </label>
      </div>
      {start.error && <ErrorBox error={start.error} className="mt-3" />}
      <JobLine job={data.jobs.cleanup} />
      {r && !active(data.jobs.cleanup) && (
        <div className="mt-4 rounded-md border border-border bg-bg/40 p-4 text-sm" data-testid="cleanup-report">
          <div className="mb-1 text-xs uppercase tracking-wide text-text-subtle">
            {r.dry_run ? "Check" : "Cleanup"} · {formatDateTime(r.at)}
            {r.min_missing_days > 0 && ` · missing ${r.min_missing_days}+ days`}
          </div>
          {nothingToDo(r) ? <p>Nothing to clean up.</p> : (
            <ul className="space-y-1">
              <li>
                {r.dry_run ? "Would remove" : "Removed"} <b>{r.removed.scenes}</b> scenes, <b>{r.removed.images}</b> images
                and <b>{r.removed.galleries}</b> galleries that are no longer on disk.
              </li>
              <li>{r.dry_run ? "Would delete" : "Deleted"} <SweptLine g={r.generated} />.</li>
              {r.generated.links_fixed > 0 && (
                <li>{r.generated.links_fixed} pictures of performers, studios or tags are lost; {r.dry_run ? "they would be" : "they were"} reset to no picture.</li>
              )}
            </ul>
          )}
          {r.removed.still_on_disk > 0 && (
            <p className="mt-1 text-text-muted">{r.removed.still_on_disk} files are marked missing but exist on disk (excluded by a pattern, or unreadable) and are kept.</p>
          )}
          {r.removed.libraries_skipped > 0 && (
            <p className="mt-1 text-warning">{r.removed.libraries_skipped} disabled or unavailable libraries were left alone.</p>
          )}
          {r.dry_run && !nothingToDo(r) && (
            <button className="btn-danger mt-3" disabled={busy} onClick={remove}>Remove these…</button>
          )}
        </div>
      )}
    </section>
  );
}

function Routine({ data }: { data: Overview }) {
  const [form, setForm] = useState<Settings>(data.settings);
  const saved = JSON.stringify(data.settings);
  useEffect(() => setForm(JSON.parse(saved)), [saved]);
  const save = useWrite((s: Settings) => api("/maintenance/settings", { method: "PUT", json: s }));
  const run = useWrite(() => api("/maintenance/run", { method: "POST" }));
  const busy = active(data.jobs.maintenance) || run.isPending;
  const r = data.maintenance_report;
  const dirty = JSON.stringify(form) !== saved;
  return (
    <section className="card mt-6 p-5" data-testid="routine">
      <h2 className="mb-1 text-lg font-semibold">Routine maintenance</h2>
      <p className="mb-4 text-sm text-text-muted">
        Once a day: removes expired sign-ins and old job records, deletes generated files nothing refers to, and
        optimizes every database table (vacuum and fresh statistics) so browsing stays fast. It runs in the
        background and doesn't interrupt anyone.
      </p>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
        <Toggle checked={form.enabled} onChange={(enabled) => setForm({ ...form, enabled })} label="Run every day" />
        <label className="flex items-center gap-2">
          at
          <select className="input w-auto" aria-label="Hour" value={form.hour} disabled={!form.enabled}
                  onChange={(e) => setForm({ ...form, hour: Number(e.target.value) })}>
            {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>)}
          </select>
          <span className="text-text-subtle">server time (now {data.server_time.slice(11, 16)})</span>
        </label>
        <label className="flex items-center gap-2">
          Forget files missing for more than
          <input type="number" min={0} max={3650} className="input w-20" aria-label="Forget missing files after days"
                 value={form.remove_missing_after_days}
                 onChange={(e) => setForm({ ...form, remove_missing_after_days: Math.max(0, Number(e.target.value) || 0) })} />
          days <span className="text-text-subtle">(0 = never automatically)</span>
        </label>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={!dirty || save.isPending} onClick={() => save.mutate(form)}>Save</button>
        <button className="btn-ghost" disabled={busy} onClick={() => run.mutate(undefined)}>Run now</button>
        <span className="ml-auto text-sm text-text-muted">Database size: {formatBytes(data.database_bytes)}</span>
      </div>
      {(save.error || run.error) && <ErrorBox error={save.error || run.error} className="mt-3" />}
      <JobLine job={data.jobs.maintenance} />
      {r && !active(data.jobs.maintenance) && (
        <div className="mt-4 rounded-md border border-border bg-bg/40 p-4 text-sm" data-testid="maintenance-report">
          <div className="mb-1 text-xs uppercase tracking-wide text-text-subtle">Last run · {formatDateTime(r.at)} · took {r.seconds}s</div>
          <ul className="space-y-1">
            <li>Optimized {r.tables_optimized} tables; database {formatBytes(r.database_bytes_before)} → {formatBytes(r.database_bytes)}.</li>
            <li>Removed {r.sessions_removed} expired sign-ins and {r.jobs_removed} old job records.</li>
            <li>Deleted <SweptLine g={r.generated} />.</li>
            {r.removed && <li>Forgot {r.removed.scenes} scenes, {r.removed.images} images and {r.removed.galleries} galleries that had been missing too long.</li>}
          </ul>
        </div>
      )}
    </section>
  );
}

interface EncryptionStatus {
  enabled: boolean; key_id: string | null; uploaded_files: number; encrypted_files: number; job: JobState | null;
}

/** Encryption at rest of users' uploads: on/off (set by the stack's key file), progress. */
function Encryption() {
  const q = useQuery({
    queryKey: ["encryption"],
    queryFn: () => api<EncryptionStatus>("/maintenance/encryption"),
    refetchInterval: (query) => (active(query.state.data?.job ?? undefined) ? 2000 : false),
  });
  const run = useWrite(() => api("/maintenance/encryption/run", { method: "POST" }));
  const e = q.data;
  if (!e) return null;
  const pending = e.uploaded_files - e.encrypted_files;
  return (
    <section className="card mb-6 p-5" data-testid="encryption">
      <h2 className="mb-1 text-lg font-semibold">Encryption of user uploads</h2>
      {e.enabled ? (
        <>
          <p className="text-sm text-text">
            <span className="chip mr-2 bg-success-bg text-success">On</span>
            Files users upload, and the previews made from them, are stored encrypted (key {e.key_id}).
          </p>
          <p className="mt-2 text-sm text-text-muted" data-testid="encryption-counts">
            {e.encrypted_files} of {e.uploaded_files} uploaded files encrypted
            {pending > 0 && !active(e.job ?? undefined) && " (the rest are encrypted by a background job)"}.
          </p>
          {pending > 0 && (
            <button className="btn-ghost mt-3" disabled={run.isPending || active(e.job ?? undefined)}
                    onClick={() => run.mutate(undefined)}>Encrypt the rest now</button>
          )}
          <JobLine job={e.job ?? undefined} />
          {e.job && !active(e.job) && e.job.message && <p className="mt-2 text-xs text-text-subtle">Last run: {e.job.message}</p>}
        </>
      ) : (
        <p className="text-sm text-text-muted">
          <span className="chip mr-2">Off</span>
          Uploads are stored as they are. To turn encryption on, give the stack a key file (UPLOADS_KEY_FILE, see the
          README); existing uploads are then encrypted automatically.
        </p>
      )}
      {run.isError && <ErrorBox error={run.error} className="mt-3" />}
    </section>
  );
}

interface UnreadableFile {
  path: string; size: number; reason: string; explanation: string; ignored: boolean; last_seen: string;
}

/** Files the scanner couldn't read (main libraries): why, and what to do about them. */
function Unreadable() {
  const [showIgnored, setShowIgnored] = useState(false);
  const q = useQuery({
    queryKey: ["unreadable", showIgnored],
    queryFn: () => api<{ items: UnreadableFile[]; total: number; ignored: number }>("/maintenance/unreadable",
      { query: { show_ignored: showIgnored || undefined } }),
  });
  const act = useWrite(({ action, paths, ignored }: { action: "ignore" | "retry" | "delete"; paths: string[]; ignored?: boolean }) =>
    api<{ problems?: string[] }>(`/maintenance/unreadable/${action}`, { json: { paths, ...(ignored === undefined ? {} : { ignored }) } }));
  const data = q.data;
  if (!data || (data.total === 0 && data.ignored === 0)) return null;
  const all = data.items.filter((f) => !f.ignored).map((f) => f.path);
  return (
    <section className="card mb-6 p-5" data-testid="unreadable">
      <h2 className="mb-1 text-lg font-semibold">Unreadable files ({data.total})</h2>
      <p className="mb-3 text-sm text-text-muted">
        Files in the libraries that couldn't be read as a video or picture, usually because they are damaged or
        incomplete. Scans skip them until they change. If one plays fine elsewhere, use “Try again”; if it's broken,
        delete it or ignore it.
      </p>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Toggle checked={showIgnored} onChange={setShowIgnored} label={`Show ignored (${data.ignored})`} />
        <span className="mr-auto" />
        {all.length > 1 && (
          <>
            <button className="btn-ghost py-1" disabled={act.isPending} onClick={() => act.mutate({ action: "retry", paths: all })}>Try all again</button>
            <button className="btn-danger py-1" disabled={act.isPending}
                    onClick={() => confirm(`Delete all ${all.length} unreadable files from disk? This cannot be undone.`) && act.mutate({ action: "delete", paths: all })}>
              Delete all…
            </button>
          </>
        )}
      </div>
      {act.isError && <ErrorBox error={act.error} className="mb-3" />}
      {act.data?.problems?.length ? <p className="mb-3 text-sm text-warning">Not deleted: {act.data.problems.join("; ")}</p> : null}
      <ul className="divide-y divide-border rounded-md border border-border">
        {data.items.map((f) => (
          <li key={f.path} className={`flex flex-wrap items-center gap-3 px-3 py-2 text-sm ${f.ignored ? "opacity-60" : ""}`} data-testid="unreadable-file">
            <div className="min-w-0 flex-1">
              <code className="block break-all text-xs text-text">{f.path}</code>
              <span className="text-xs text-text-muted" title={f.reason}>{f.explanation}</span>
              <span className="ml-2 font-mono text-xs text-text-subtle">{formatBytes(f.size)}</span>
            </div>
            <button className="btn-ghost py-1 text-xs" disabled={act.isPending} onClick={() => act.mutate({ action: "retry", paths: [f.path] })}>Try again</button>
            <button className="btn-ghost py-1 text-xs" disabled={act.isPending}
                    onClick={() => act.mutate({ action: "ignore", paths: [f.path], ignored: !f.ignored })}>{f.ignored ? "Unignore" : "Ignore"}</button>
            <button className="btn-danger py-1 text-xs" disabled={act.isPending} aria-label={`Delete ${f.path}`}
                    onClick={() => confirm(`Delete ${f.path} from disk? This cannot be undone.`) && act.mutate({ action: "delete", paths: [f.path] })}>Delete</button>
          </li>
        ))}
      </ul>
    </section>
  );
}
