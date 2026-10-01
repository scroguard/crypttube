import { useState, type FormEvent } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useDetail, useMe, useWrite } from "../api/hooks";
import type { TagDetail as Tag } from "../api/types";
import Chips from "../components/Chips";
import { EntityImageControls, FormButtons } from "../components/EditForms";
import EntityContent from "../components/EntityContent";
import { DeleteEntityButton } from "../components/EntityDialogs";
import EntityPicker from "../components/EntityPicker";
import { ErrorBox, Field, Modal, Spinner } from "../components/ui";

export default function TagDetail() {
  const id = Number(useParams().id);
  const q = useDetail<Tag>(`/tags/${id}`);
  const { canEdit } = useMe();
  const [dialog, setDialog] = useState<"edit" | "merge" | null>(null);
  if (q.isPending) return <Spinner className="mt-24" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const t = q.data;
  return (
    <div>
      <div className="flex flex-wrap items-start gap-6">
        {t.image_url && <img src={t.image_url} alt="" className="h-24 w-24 rounded-lg object-cover" />}
        <div className="min-w-0 flex-1 space-y-2">
          <h1 className="text-3xl font-semibold">{t.name}</h1>
          {t.description && <p className="text-sm text-zinc-300">{t.description}</p>}
          {t.parents.length > 0 && <div><span className="label">Parents</span><Chips items={t.parents} to="tags" /></div>}
          {t.children.length > 0 && <div><span className="label">Sub-tags</span><Chips items={t.children} to="tags" /></div>}
          <p className="text-xs text-zinc-500">{t.performer_count} performers carry this tag</p>
        </div>
        {canEdit && (
          <div className="flex flex-col items-end gap-2">
            <div className="flex gap-2">
              <button className="btn-ghost" onClick={() => setDialog("edit")}>Edit</button>
              <button className="btn-ghost" onClick={() => setDialog("merge")}>Merge…</button>
              <DeleteEntityButton kind="tags" id={id} name={t.name} />
            </div>
            <EntityImageControls kind="tags" id={id} hasImage={!!t.image_url} />
          </div>
        )}
      </div>
      <EntityContent filter={{ tag_ids: [id] }} />
      {dialog === "edit" && <TagEditModal tag={t} onClose={() => setDialog(null)} />}
      {dialog === "merge" && <TagMergeModal tag={t} onClose={() => setDialog(null)} />}
    </div>
  );
}

function TagEditModal({ tag, onClose }: { tag: Tag; onClose: () => void }) {
  const [name, setName] = useState(tag.name);
  const [description, setDescription] = useState(tag.description ?? "");
  const [parents, setParents] = useState(tag.parents.map((p) => p.id));
  const save = useWrite((body: object) => api(`/tags/${tag.id}`, { method: "PATCH", json: body }));
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate({ name: name.trim(), description: description.trim() || null, parent_ids: parents }, { onSuccess: onClose });
  }
  return (
    <Modal title="Edit tag" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} required /></Field>
        <Field label="Description"><textarea className="input min-h-16" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <Field label="Parent tags"><EntityPicker kind="tags" value={parents} onChange={setParents} /></Field>
        <FormButtons busy={save.isPending} error={save.error} onCancel={onClose} />
      </form>
    </Modal>
  );
}

function TagMergeModal({ tag, onClose }: { tag: Tag; onClose: () => void }) {
  const [sources, setSources] = useState<number[]>([]);
  const merge = useWrite(() => api(`/tags/${tag.id}/merge`, { json: { source_ids: sources } }));
  return (
    <Modal title={`Merge into “${tag.name}”`} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); merge.mutate(undefined, { onSuccess: onClose }); }} className="space-y-3">
        <p className="text-sm text-zinc-400">The selected tags are deleted; their scenes, galleries, images, performers and sub-tags move to “{tag.name}”.</p>
        <Field label="Tags to merge"><EntityPicker kind="tags" value={sources} onChange={(ids) => setSources(ids.filter((i) => i !== tag.id))} /></Field>
        <FormButtons busy={merge.isPending} error={merge.error} onCancel={onClose} label="Merge" />
      </form>
    </Modal>
  );
}
