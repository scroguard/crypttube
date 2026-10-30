import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client";
import { useDetail, useRate, useWrite } from "../api/hooks";
import type { StudioDetail as Studio } from "../api/types";
import Chips from "../components/Chips";
import { EntityImageControls, FormButtons } from "../components/EditForms";
import EntityContent from "../components/EntityContent";
import { DeleteEntityButton } from "../components/EntityDialogs";
import EntityPicker from "../components/EntityPicker";
import FolderLinks from "../components/FolderLinks";
import { ErrorBox, Field, Modal, Rating, Spinner } from "../components/ui";

export default function StudioDetail() {
  const id = Number(useParams().id);
  const q = useDetail<Studio>(`/studios/${id}`);
  const [editing, setEditing] = useState(false);
  const rate = useRate(`/studios/${id}`);
  if (q.isPending) return <Spinner className="mt-24" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const s = q.data;
  const canEdit = s.can_edit;
  return (
    <div>
      <div className="flex flex-wrap items-start gap-6">
        {s.image_url && <img src={s.image_url} alt="" className="h-24 max-w-xs rounded bg-surface object-contain p-2" />}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-3xl font-semibold">{s.name}</h1>
            <Rating key={id} value={s.rating} onChange={canEdit ? rate.save : undefined} error={rate.error} />
          </div>
          {s.parent && <p className="text-sm text-text-muted">Part of <Link to={`/studios/${s.parent.id}`} className="hover:text-text">{s.parent.name}</Link></p>}
          {s.url && <a href={s.url} target="_blank" rel="noreferrer noopener" className="text-sm text-link hover:underline">{s.url}</a>}
          {s.children.length > 0 && <div className="mt-3"><span className="label">Sub-studios</span><Chips items={s.children} to="studios" /></div>}
          {s.details && <p className="mt-3 whitespace-pre-line text-sm text-text">{s.details}</p>}
          <FolderLinks kind="studios" id={id} />
        </div>
        {canEdit && (
          <div className="flex flex-col items-end gap-2">
            <div className="flex gap-2"><button className="btn-ghost" onClick={() => setEditing(true)}>Edit</button><DeleteEntityButton kind="studios" id={id} name={s.name} /></div>
            <EntityImageControls kind="studios" id={id} hasImage={!!s.image_url} />
          </div>
        )}
      </div>
      <EntityContent filter={{ studio_ids: [id] }} scope="this studio" />
      {editing && <StudioEditModal studio={s} onClose={() => setEditing(false)} />}
    </div>
  );
}

function StudioEditModal({ studio, onClose }: { studio: Studio; onClose: () => void }) {
  const [name, setName] = useState(studio.name);
  const [parent, setParent] = useState(studio.parent ? [studio.parent.id] : []);
  const [url, setUrl] = useState(studio.url ?? "");
  const [details, setDetails] = useState(studio.details ?? "");
  const save = useWrite((body: object) => api(`/studios/${studio.id}`, { method: "PATCH", json: body }));
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate({ name: name.trim(), parent_id: parent[0] ?? null, url: url.trim() || null, details: details.trim() || null }, { onSuccess: onClose });
  }
  return (
    <Modal title="Edit studio" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} required /></Field>
        <Field label="Parent studio"><EntityPicker kind="studios" single value={parent} onChange={setParent} /></Field>
        <Field label="URL"><input className="input" value={url} onChange={(e) => setUrl(e.target.value)} /></Field>
        <Field label="Details"><textarea className="input min-h-20" value={details} onChange={(e) => setDetails(e.target.value)} /></Field>
        <FormButtons busy={save.isPending} error={save.error} onCancel={onClose} />
      </form>
    </Modal>
  );
}
