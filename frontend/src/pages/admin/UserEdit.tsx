import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { api } from "../../api/client";
import { useMe, useWrite } from "../../api/hooks";
import type { AdminUser, Role } from "../../api/types";
import EntityPicker from "../../components/EntityPicker";
import { ErrorBox, Field, Spinner, Toggle } from "../../components/ui";
import AdminNav from "./AdminNav";
import { ROLE_HELP } from "./Users";

export default function UserEdit() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const { user: me } = useMe();
  const q = useQuery({ queryKey: ["users", id], queryFn: () => api<AdminUser>(`/users/${id}`) });
  const patch = useWrite((body: object) => api(`/users/${id}`, { method: "PATCH", json: body }));
  const del = useWrite(() => api(`/users/${id}`, { method: "DELETE" }));
  const saveRules = useWrite((body: object) => api(`/users/${id}/restrictions`, { method: "PUT", json: body }));
  const [password, setPassword] = useState("");
  const [tags, setTags] = useState<number[]>([]);
  const [studios, setStudios] = useState<number[]>([]);
  const [performers, setPerformers] = useState<number[]>([]);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const r = q.data?.restrictions;
    if (!r) return;
    setTags(r.tags.map((t) => t.id));
    setStudios(r.studios.map((s) => s.id));
    setPerformers(r.performers.map((p) => p.id));
  }, [q.data]);

  if (q.isPending) return <Spinner className="mt-24" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const u = q.data;
  const error = patch.error || del.error || saveRules.error;

  return (
    <div className="mx-auto max-w-3xl">
      <AdminNav />
      <h1 className="mb-6 text-2xl font-semibold">{u.username}</h1>
      <section className="card mb-6 grid gap-4 p-5 sm:grid-cols-2">
        <Field label="Role">
          <select className="input" value={u.role} onChange={(e) => patch.mutate({ role: e.target.value as Role })}>
            {(Object.keys(ROLE_HELP) as Role[]).map((r) => <option key={r} value={r}>{r} — {ROLE_HELP[r]}</option>)}
          </select>
        </Field>
        <div className="flex items-end"><Toggle checked={u.is_active} onChange={(v) => patch.mutate({ is_active: v })} label="Account enabled" /></div>
        <Field label="Set new password">
          <div className="flex gap-2">
            <input className="input" type="password" autoComplete="new-password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} />
            <button className="btn-ghost" disabled={password.length < 8} onClick={() => patch.mutate({ password }, { onSuccess: () => setPassword("") })}>Set</button>
          </div>
          <span className="mt-1 block text-xs text-zinc-500">Signs the user out everywhere.</span>
        </Field>
        <div className="flex items-end justify-end">
          {me?.id !== u.id && (
            <button className="btn-danger" onClick={() => confirm(`Delete ${u.username}?`) && del.mutate(undefined, { onSuccess: () => navigate("/admin/users") })}>
              Delete user
            </button>
          )}
        </div>
      </section>

      <section className="card space-y-4 p-5">
        <div>
          <h2 className="font-semibold">Content restrictions</h2>
          <p className="text-sm text-zinc-400">
            {u.role === "admin" ? "Admins are never restricted; these rules apply if the role changes. " : ""}
            Hidden content behaves as if it doesn't exist for this user. Hiding a tag also hides its sub-tags;
            hiding a studio also hides its sub-studios; performers carrying a hidden tag are hidden too.
          </p>
        </div>
        <Field label="Hidden tags"><EntityPicker kind="tags" value={tags} onChange={setTags} /></Field>
        <Field label="Hidden studios"><EntityPicker kind="studios" value={studios} onChange={setStudios} /></Field>
        <Field label="Hidden performers"><EntityPicker kind="performers" value={performers} onChange={setPerformers} /></Field>
        <div className="flex items-center justify-end gap-3">
          {saved && <span className="text-sm text-emerald-400">Saved</span>}
          <button className="btn-primary" disabled={saveRules.isPending}
                  onClick={() => saveRules.mutate({ tag_ids: tags, studio_ids: studios, performer_ids: performers }, { onSuccess: () => setSaved(true) })}>
            Save restrictions
          </button>
        </div>
      </section>
      {error && <ErrorBox error={error} className="mt-4" />}
    </div>
  );
}
