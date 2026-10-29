import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { Virtuoso } from "react-virtuoso";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import DirectoryPicker from "../../components/DirectoryPicker";
import { ErrorBox, Spinner, Tabs, Toggle } from "../../components/ui";
import AdminNav from "./AdminNav";

type Kind = "tag" | "performer" | "studio";
interface KindCounts { total: number; imported: number; existing: number; pending: number }
interface StashImport {
  id: number;
  filename: string | null;
  stash_version: number | null;
  kinds: Record<Kind, KindCounts>;
}
interface Item {
  source_id: number;
  name: string;
  usage: number;
  status: "pending" | "existing" | "imported";
  target_id: number | null;
  data: {
    description?: string | null; parents?: string[];
    disambiguation?: string | null; gender?: string | null; aliases?: string[]; tags?: string[];
    parent?: string | null; url?: string | null;
  };
}
interface ApplyResult { created: number; existing: number }
interface ImagesResult { files: number; matched: number; skipped: number; failed: number; unmatched: string[]; unmatched_total: number }

const LABELS: Record<Kind, string> = { tag: "Tags", performer: "Performers", studio: "Studios" };
const PATHS: Record<Kind, string> = { tag: "tags", performer: "performers", studio: "studios" };

export default function StashImportPage() {
  const current = useQuery({ queryKey: ["stash-import"], queryFn: () => api<StashImport | null>("/stash-import") });
  return (
    <div className="mx-auto max-w-5xl">
      <AdminNav />
      <h1 className="mb-1 text-xl font-semibold">Import from Stash</h1>
      <p className="mb-5 text-sm text-text-muted">
        Bring tags, performers and studios over from a Stash database. Nothing is added until you approve it,
        and pictures are not copied from Stash.
      </p>
      {current.isPending ? <Spinner /> : current.isError ? <ErrorBox error={current.error} />
        : current.data ? <Review imp={current.data} /> : <Start />}
      <ImagesFromFolder />
    </div>
  );
}

function Start() {
  const file = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState("");
  const upload = useWrite((f: File) =>
    api<StashImport>("/stash-import/upload", { body: f, headers: { "X-Filename": encodeURIComponent(f.name) } }));
  const fromPath = useWrite((p: string) => api<StashImport>("/stash-import/path", { json: { path: p } }));
  const busy = upload.isPending || fromPath.isPending;
  return (
    <section className="card space-y-4 p-5">
      <p className="text-sm text-text">
        Choose your Stash database file (usually <code>stash-go.sqlite</code>). Use a backup made from Stash
        (Settings → Tasks → Backup), or stop Stash before copying the file, so the copy is complete.
        The file is read once and not kept.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <input ref={file} type="file" className="hidden" aria-label="Stash database file"
               onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
        <button className="btn-primary" disabled={busy} onClick={() => file.current?.click()}>
          {upload.isPending ? "Reading…" : "Upload database…"}
        </button>
        <span className="text-sm text-text-subtle">or read a copy on the server:</span>
        <form className="flex min-w-64 flex-1 gap-2" onSubmit={(e) => { e.preventDefault(); if (path.trim()) fromPath.mutate(path.trim()); }}>
          <input className="input font-mono" placeholder="/media/stash-go.sqlite" aria-label="Path on the server"
                 value={path} onChange={(e) => setPath(e.target.value)} />
          <button className="btn-ghost" disabled={busy || !path.trim()}>{fromPath.isPending ? "Reading…" : "Read"}</button>
        </form>
      </div>
      {(upload.error || fromPath.error) && <ErrorBox error={upload.error || fromPath.error} />}
    </section>
  );
}

function Review({ imp }: { imp: StashImport }) {
  const [kind, setKind] = useState<Kind>("tag");
  const close = useWrite(() => api("/stash-import", { method: "DELETE" }));
  const importedAny = Object.values(imp.kinds).some((k) => k.imported > 0);
  const pending = Object.values(imp.kinds).reduce((n, k) => n + k.pending, 0);
  const tabs = Object.fromEntries((Object.keys(LABELS) as Kind[]).map((k) =>
    [k, `${LABELS[k]} (${imp.kinds[k].pending} to review)`])) as Record<Kind, string>;
  return (
    <section data-testid="stash-review">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="mr-auto text-sm text-text-muted">
          <span className="font-medium text-text">{imp.filename ?? "Stash database"}</span>
          {imp.stash_version != null && <> · schema {imp.stash_version}</>}
        </div>
        {importedAny ? (
          <button className="btn-primary" disabled={close.isPending}
                  onClick={() => (!pending || confirm(`Finish the import? ${pending} items you did not approve are left out.`)) && close.mutate(undefined)}>
            Finish
          </button>
        ) : (
          <button className="btn-danger" disabled={close.isPending}
                  onClick={() => confirm("Reject this import? Nothing from it will be added.") && close.mutate(undefined)}>
            Reject import
          </button>
        )}
      </div>
      {close.error && <ErrorBox error={close.error} className="mb-3" />}
      <Tabs tabs={tabs} value={kind} onChange={setKind} />
      <KindReview key={kind} kind={kind} counts={imp.kinds[kind]} />
    </section>
  );
}

function KindReview({ kind, counts }: { kind: Kind; counts: KindCounts }) {
  const items = useQuery({
    queryKey: ["stash-import", "items", kind],
    queryFn: () => api<Item[]>("/stash-import/items", { query: { kind } }),
  });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState("");
  const [usedOnly, setUsedOnly] = useState(false);
  const [done, setDone] = useState<ApplyResult | null>(null);
  const apply = useWrite((source_ids: number[] | null) =>
    api<ApplyResult>("/stash-import/apply", { json: { kind, source_ids } }));
  const run = (ids: number[] | null) => apply.mutate(ids, {
    onSuccess: (r) => { setDone(r); setSelected(new Set()); },
  });

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (items.data ?? []).filter((i) => (!usedOnly || i.usage > 0) && (!needle
      || i.name.toLowerCase().includes(needle)
      || i.data.aliases?.some((a) => a.toLowerCase().includes(needle))));
  }, [items.data, search, usedOnly]);
  const selectable = useMemo(() => shown.filter((i) => i.status === "pending"), [shown]);
  const allTicked = selectable.length > 0 && selectable.every((i) => selected.has(i.source_id));
  const label = LABELS[kind].toLowerCase();

  const toggle = (id: number) => setSelected((s) => {
    const next = new Set(s);
    if (!next.delete(id)) next.add(id);
    return next;
  });
  const toggleAll = () => setSelected((s) => {
    const next = new Set(s);
    for (const i of selectable) if (allTicked) next.delete(i.source_id); else next.add(i.source_id);
    return next;
  });

  if (items.isPending) return <Spinner />;
  if (items.isError) return <ErrorBox error={items.error} />;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <input className="input max-w-xs" placeholder={`Filter ${label}…`} aria-label={`Filter ${label}`}
               value={search} onChange={(e) => setSearch(e.target.value)} />
        <Toggle checked={usedOnly} onChange={setUsedOnly} label="Only ones in use in Stash" />
        <span className="mr-auto" />
        <button className="btn-ghost" disabled={apply.isPending || selected.size === 0}
                onClick={() => run([...selected])}>
          Import selected ({selected.size})
        </button>
        <button className="btn-primary" disabled={apply.isPending || counts.pending === 0}
                onClick={() => confirm(`Import all ${counts.pending} ${label} that are not here yet?`) && run(null)}>
          Import all {counts.pending}
        </button>
      </div>
      <p className="mb-2 text-xs text-text-subtle" data-testid="stash-counts">
        {counts.total} in Stash · {counts.imported} imported · {counts.existing} already here · {counts.pending} to review
      </p>
      {done && (
        <p className="mb-2 rounded-md border border-success/40 bg-success-bg px-3 py-2 text-sm text-success" role="status">
          Imported {done.created} {label}{done.existing > 0 && `; ${done.existing} were already here and left as they are`}.
        </p>
      )}
      {apply.error && <ErrorBox error={apply.error} className="mb-2" />}
      <div className="card overflow-hidden">
        <label className="flex items-center gap-3 border-b border-border px-3 py-2 text-xs uppercase tracking-wide text-text-subtle">
          <input type="checkbox" className="h-4 w-4 accent-accent" checked={allTicked} disabled={!selectable.length}
                 onChange={toggleAll} aria-label="Select all shown" />
          <span className="mr-auto">Select all shown ({selectable.length})</span>
          <span>Used in Stash</span>
        </label>
        {shown.length === 0 ? <p className="p-6 text-center text-sm text-text-subtle">Nothing to show.</p> : (
          <Virtuoso style={{ height: "60vh" }} data={shown} computeItemKey={(_, i) => i.source_id}
                    itemContent={(_, i) => <Row kind={kind} item={i} checked={selected.has(i.source_id)} onToggle={toggle} />} />
        )}
      </div>
    </div>
  );
}

function Row({ kind, item, checked, onToggle }: { kind: Kind; item: Item; checked: boolean; onToggle: (id: number) => void }) {
  const d = item.data;
  const info = [
    d.disambiguation && `(${d.disambiguation})`,
    d.gender?.replace("_", " "),
    d.aliases?.length ? `aka ${d.aliases.join(", ")}` : null,
    d.parents?.length ? `under ${d.parents.join(", ")}` : null,
    d.parent && `part of ${d.parent}`,
    d.tags?.length ? `${d.tags.length} tags` : null,
    d.url,
  ].filter(Boolean).join(" · ");
  const pending = item.status === "pending";
  return (
    <label className={`flex items-center gap-3 border-b border-border/60 px-3 py-2 text-sm ${pending ? "cursor-pointer hover:bg-surface-raised/40" : "text-text-subtle"}`}
           data-testid="stash-item">
      <input type="checkbox" className="h-4 w-4 shrink-0 accent-accent" disabled={!pending}
             checked={pending && checked} onChange={() => onToggle(item.source_id)} aria-label={item.name} />
      <span className="min-w-0 flex-1">
        <span className="font-medium">{item.name}</span>
        {info && <span className="ml-2 text-xs text-text-subtle">{info}</span>}
      </span>
      {!pending && (
        item.target_id != null
          ? <Link to={`/${PATHS[kind]}/${item.target_id}`} className={`chip ${item.status === "imported" ? "bg-success-bg text-success" : ""}`}>
              {item.status === "imported" ? "Imported" : "Already here"}
            </Link>
          : <span className="chip">{item.status === "imported" ? "Imported" : "Already here"}</span>
      )}
      <span className="w-14 shrink-0 text-right tabular-nums text-text-subtle">{item.usage}</span>
    </label>
  );
}

/** Pictures are not copied out of Stash; this assigns them from a folder by file name. */
function ImagesFromFolder() {
  const [kind, setKind] = useState<"studios" | "performers" | "tags">("studios");
  const [path, setPath] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [picking, setPicking] = useState(false);
  const run = useWrite(() => api<ImagesResult>("/stash-import/images", { json: { kind, path: path.trim(), overwrite } }));
  const r = run.data;
  return (
    <section className="mt-10">
      <h2 className="mb-1 text-lg font-semibold">Pictures from a folder</h2>
      <p className="mb-3 text-sm text-text-muted">
        Put one picture per studio in a folder of the media store, named after the studio
        (for example <code>Some Studio.png</code>), and they are matched by name. Case and punctuation are ignored.
        The folder is only read. Keep it outside your libraries, or the pictures will also show up as images.
      </p>
      <form className="card flex flex-wrap items-center gap-3 p-4" onSubmit={(e) => { e.preventDefault(); run.mutate(undefined); }}>
        <select className="input w-auto" aria-label="Picture kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
          <option value="studios">Studios</option>
          <option value="performers">Performers</option>
          <option value="tags">Tags</option>
        </select>
        <input className="input min-w-56 flex-1 font-mono" placeholder="/media/_studio-images" aria-label="Picture folder"
               value={path} onChange={(e) => setPath(e.target.value)} />
        <button type="button" className="btn-ghost" onClick={() => setPicking(true)}>Browse…</button>
        <Toggle checked={overwrite} onChange={setOverwrite} label="Replace existing pictures" />
        <button className="btn-primary" disabled={run.isPending || !path.trim()}>{run.isPending ? "Matching…" : "Assign pictures"}</button>
      </form>
      {run.error && <ErrorBox error={run.error} className="mt-3" />}
      {r && !run.isPending && (
        <div className="mt-3 text-sm text-text" role="status" data-testid="images-result">
          Assigned {r.matched} of {r.files} pictures
          {r.skipped > 0 && `; ${r.skipped} skipped because a picture was already set`}
          {r.failed > 0 && `; ${r.failed} could not be read`}.
          {r.unmatched_total > 0 && (
            <details className="mt-1 text-text-muted">
              <summary className="cursor-pointer">{r.unmatched_total} files matched nothing</summary>
              <p className="mt-1 break-words text-xs">{r.unmatched.join(", ")}{r.unmatched_total > r.unmatched.length && " …"}</p>
            </details>
          )}
        </div>
      )}
      {picking && (
        <DirectoryPicker title="Choose the picture folder" start={path.trim().replace(/\/+$/, "") || undefined}
                         selectLabel="Use this folder"
                         onSelect={(p) => { setPath(p); setPicking(false); }} onClose={() => setPicking(false)} />
      )}
    </section>
  );
}
