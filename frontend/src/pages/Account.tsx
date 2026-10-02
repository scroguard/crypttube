import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useLogout, useMe, useWrite } from "../api/hooks";
import type { SessionInfo } from "../api/types";
import { FormButtons } from "../components/EditForms";
import EntityPicker from "../components/EntityPicker";
import { ErrorBox, Field, Toggle } from "../components/ui";
import { formatDateTime } from "../lib/format";

export default function Account() {
  const { user } = useMe();
  const qc = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [done, setDone] = useState(false);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => api<SessionInfo[]>("/auth/sessions") });
  const change = useWrite((body: object) => api("/auth/password", { json: body }));
  const revoke = useWrite((id: number) => api(`/auth/sessions/${id}`, { method: "DELETE" }));

  function submit(e: FormEvent) {
    e.preventDefault();
    change.mutate({ current_password: current, new_password: next }, {
      onSuccess: () => { setCurrent(""); setNext(""); setDone(true); qc.invalidateQueries({ queryKey: ["sessions"] }); },
    });
  }
  const logout = useLogout();

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">{user?.username}</h1>
        <p className="text-sm capitalize text-zinc-400">{user?.role}</p>
      </div>
      <section className="card p-5">
        <h2 className="mb-4 font-semibold">Change password</h2>
        <form onSubmit={submit} className="space-y-3">
          <Field label="Current password"><input className="input" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></Field>
          <Field label="New password (8+ characters)"><input className="input" type="password" autoComplete="new-password" minLength={8} value={next} onChange={(e) => setNext(e.target.value)} required /></Field>
          {done && <p className="text-sm text-emerald-400">Password changed. Other devices were signed out.</p>}
          <FormButtons busy={change.isPending} error={change.error} label="Change password" />
        </form>
      </section>
      <ContentFilters />
      <section className="card p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold">Signed-in devices</h2>
          <button className="btn-ghost" onClick={() => logout(true)}>Sign out everywhere</button>
        </div>
        {sessions.isError && <ErrorBox error={sessions.error} />}
        <ul className="divide-y divide-zinc-800">
          {sessions.data?.map((s) => (
            <li key={s.id} className="flex items-center gap-3 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <div className="truncate">{s.user_agent || "Unknown device"}</div>
                <div className="text-xs text-zinc-500">Last active {formatDateTime(s.last_seen_at)}</div>
              </div>
              {s.current ? <span className="text-xs text-emerald-400">this device</span>
                : <button className="btn-ghost py-1" onClick={() => revoke.mutate(s.id)}>Sign out</button>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

interface FilterState { enabled: boolean; tags: { id: number; name: string }[]; performers: { id: number; name: string }[] }

/** Tags and performers this user doesn't want to see, anywhere in the app. */
function ContentFilters() {
  const q = useQuery({ queryKey: ["my-filters"], queryFn: () => api<FilterState>("/me/filters") });
  const [enabled, setEnabled] = useState(true);
  const [tags, setTags] = useState<number[]>([]);
  const [performers, setPerformers] = useState<number[]>([]);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!q.data) return;
    setEnabled(q.data.enabled);
    setTags(q.data.tags.map((t) => t.id));
    setPerformers(q.data.performers.map((p) => p.id));
  }, [q.data]);
  const tagNames = useMemo(() => new Map(q.data?.tags.map((t) => [t.id, t.name])), [q.data]);
  const performerNames = useMemo(() => new Map(q.data?.performers.map((p) => [p.id, p.name])), [q.data]);
  const save = useWrite((body: object) => api<FilterState>("/me/filters", { method: "PUT", json: body }));
  const same = (a: number[], b: { id: number }[]) => a.length === b.length && b.every((x) => a.includes(x.id));
  const dirty = !!q.data && (enabled !== q.data.enabled || !same(tags, q.data.tags) || !same(performers, q.data.performers));
  return (
    <section className="card p-5" data-testid="content-filters">
      <h2 className="mb-1 font-semibold">Content filters</h2>
      <p className="mb-4 text-sm text-zinc-400">
        Choose tags and performers you don't want to see. Anything with those tags (including the tags under them)
        or featuring those performers disappears from every page for you only. Switch the filters off to see
        everything again without losing your choices.
      </p>
      {q.isError && <ErrorBox error={q.error} />}
      <div className="space-y-3">
        <Toggle checked={enabled} onChange={(v) => { setEnabled(v); setSaved(false); }} label="Apply my filters" />
        <Field label="Hide these tags">
          <EntityPicker kind="tags" value={tags} onChange={(v) => { setTags(v); setSaved(false); }} knownNames={tagNames} placeholder="Add a tag to hide…" />
        </Field>
        <Field label="Hide these performers">
          <EntityPicker kind="performers" value={performers} onChange={(v) => { setPerformers(v); setSaved(false); }} knownNames={performerNames} placeholder="Add a performer to hide…" />
        </Field>
        {saved && !dirty && <p className="text-sm text-emerald-400" role="status">Filters saved.</p>}
        {save.error && <ErrorBox error={save.error} />}
        <div className="flex justify-end">
          <button className="btn-primary" disabled={!dirty || save.isPending}
                  onClick={() => save.mutate({ enabled, tag_ids: tags, performer_ids: performers }, { onSuccess: () => setSaved(true) })}>
            {save.isPending ? "Saving…" : "Save filters"}
          </button>
        </div>
      </div>
    </section>
  );
}
