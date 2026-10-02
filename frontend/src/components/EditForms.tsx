import { useState, type FormEvent, type ReactNode } from "react";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import type { Ref } from "../api/types";
import EntityPicker, { creator } from "./EntityPicker";
import ImageSourceModal from "./ImageSourceModal";
import { ErrorBox, Field, Modal, Toggle } from "./ui";

const ids = (refs: Ref[] | undefined) => (refs ?? []).map((r) => r.id);

interface ContentLike {
  id: number;
  title: string | null;
  details?: string | null;
  release_date?: string | null;
  urls?: string[];
  organized: boolean;
  studio: Ref | null;
  performers: Ref[];
  tags: Ref[];
  // Where it lives on disk (only sent to people allowed to edit it).
  file?: { path?: string; zip_path?: string | null };
  folder_path?: string | null;
  zip_path?: string | null;
}

/** "archive.zip → member.jpg" for zip members, otherwise the plain path. */
function locationOf(item: ContentLike): string | null {
  const zip = item.file?.zip_path ?? item.zip_path;
  const path = item.file?.path ?? item.folder_path;
  if (zip && item.file?.path) return `${zip} → ${item.file.path}`;
  return path ?? zip ?? null;
}

/** Edit dialog for scenes, galleries and images (fields shown depend on the kind). */
export function ContentEditModal({ kind, item, onClose }: {
  kind: "scenes" | "galleries" | "images"; item: ContentLike; onClose: () => void;
}) {
  const [title, setTitle] = useState(item.title ?? "");
  const [details, setDetails] = useState(item.details ?? "");
  const [date, setDate] = useState(item.release_date ?? "");
  const [urls, setUrls] = useState((item.urls ?? []).join("\n"));
  const [organized, setOrganized] = useState(item.organized);
  const [studio, setStudio] = useState(item.studio ? [item.studio.id] : []);
  const [performers, setPerformers] = useState(ids(item.performers));
  const [tags, setTags] = useState(ids(item.tags));
  const save = useWrite((body: object) => api(`/${kind}/${item.id}`, { method: "PATCH", json: body }));
  const location = locationOf(item);

  function submit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {
      title: title.trim() || null, organized, studio_id: studio[0] ?? null, performer_ids: performers, tag_ids: tags,
    };
    if (kind !== "images") Object.assign(body, { details: details.trim() || null, release_date: date || null });
    if (kind === "scenes") body.urls = urls.split("\n").map((u) => u.trim()).filter(Boolean);
    save.mutate(body, { onSuccess: onClose });
  }

  return (
    <Modal title={`Edit ${kind.slice(0, -1).replace("gallerie", "gallery")}`} onClose={onClose} wide>
      <form onSubmit={submit} className="grid gap-4 sm:grid-cols-2">
        {location && (
          <div className="sm:col-span-2">
            <span className="label">{kind === "galleries" ? "Location" : "File"}</span>
            <code className="block select-all break-all rounded-md bg-zinc-950 px-3 py-2 text-xs text-zinc-300" data-testid="file-location">{location}</code>
          </div>
        )}
        <div className="sm:col-span-2"><Field label="Title"><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></Field></div>
        {kind !== "images" && <Field label="Release date"><input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} /></Field>}
        <Field label="Studio"><EntityPicker kind="studios" single value={studio} onChange={setStudio} onCreate={creator("studios")} /></Field>
        <div className="sm:col-span-2"><Field label="Performers"><EntityPicker kind="performers" value={performers} onChange={setPerformers} onCreate={creator("performers")} /></Field></div>
        <div className="sm:col-span-2"><Field label="Tags"><EntityPicker kind="tags" value={tags} onChange={setTags} onCreate={creator("tags")} /></Field></div>
        {kind !== "images" && (
          <div className="sm:col-span-2"><Field label="Details"><textarea className="input min-h-24" value={details} onChange={(e) => setDetails(e.target.value)} /></Field></div>
        )}
        {kind === "scenes" && (
          <div className="sm:col-span-2"><Field label="URLs (one per line)"><textarea className="input min-h-16 font-mono text-xs" value={urls} onChange={(e) => setUrls(e.target.value)} /></Field></div>
        )}
        <Toggle checked={organized} onChange={setOrganized} label="Organized" />
        <FormButtons busy={save.isPending} error={save.error} onCancel={onClose} />
      </form>
    </Modal>
  );
}

export function FormButtons({ busy, error, onCancel, label = "Save", children }: {
  busy: boolean; error: unknown; onCancel?: () => void; label?: string; children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-2 sm:col-span-2">
      {error ? <ErrorBox error={error} className="mr-auto" /> : null}
      {children}
      {onCancel && <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>}
      <button className="btn-primary" disabled={busy}>{busy ? "Saving…" : label}</button>
    </div>
  );
}

/** Set / replace / remove the image of a performer, studio or tag. */
export function EntityImageControls({ kind, id, hasImage }: { kind: "performers" | "studios" | "tags"; id: number; hasImage: boolean }) {
  const [choosing, setChoosing] = useState(false);
  const remove = useWrite(() => api(`/${kind}/${id}/image`, { method: "DELETE" }));
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button className="btn-ghost" onClick={() => setChoosing(true)}>{hasImage ? "Change image" : "Add image"}</button>
      {hasImage && <button className="btn-ghost" onClick={() => remove.mutate()}>Remove image</button>}
      {remove.error ? <ErrorBox error={remove.error} /> : null}
      {choosing && <ImageSourceModal kind={kind} id={id} onClose={() => setChoosing(false)} />}
    </div>
  );
}
