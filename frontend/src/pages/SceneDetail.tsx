import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api } from "../api/client";
import { useDetail, useWrite } from "../api/hooks";
import type { Playback, SceneDetail as Scene } from "../api/types";
import Chips from "../components/Chips";
import DeleteMediaButton from "../components/DeleteMedia";
import { ContentEditModal } from "../components/EditForms";
import EntityPicker, { creator } from "../components/EntityPicker";
import VideoPlayer from "../components/VideoPlayer";
import { ErrorBox, Field, Initials, Rating, Spinner } from "../components/ui";
import { formatBytes, formatDate, formatDateTime, formatDuration, resolutionLabel } from "../lib/format";

export default function SceneDetail() {
  const id = Number(useParams().id);
  const scene = useDetail<Scene>(`/scenes/${id}`);
  const playback = useDetail<Playback>(`/scenes/${id}/playback`);
  const [editing, setEditing] = useState(false);
  const navigate = useNavigate();
  const rate = useWrite((rating: number | null) => api(`/scenes/${id}`, { method: "PATCH", json: { rating } }));
  const assign = useWrite((body: object) => api(`/scenes/${id}`, { method: "PATCH", json: body }));
  const qc = useQueryClient();
  const [rebuilding, setRebuilding] = useState(false);
  const regenerate = useWrite(() => api(`/scenes/${id}/regenerate`, { method: "POST" }));
  function rebuild() {
    setRebuilding(true);
    regenerate.mutate(undefined, {
      onSettled: () => {
        // The worker rebuilds in the background; pick up the new image URLs shortly after.
        const refresh = () => qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
        setTimeout(refresh, 4000);
        setTimeout(() => { refresh(); setRebuilding(false); }, 12000);
      },
    });
  }

  if (scene.isPending) return <Spinner className="mt-24" />;
  if (scene.isError) return <ErrorBox error={scene.error} />;
  const s = scene.data;
  const canEdit = s.can_edit;
  const meta = [
    s.release_date && formatDate(s.release_date),
    formatDuration(s.duration),
    resolutionLabel(s.height),
    s.framerate && `${Math.round(s.framerate * 100) / 100} fps`,
    [s.video_codec, s.audio_codec].filter(Boolean).join(" / "),
    formatBytes(s.file.size),
    `${s.play_count} play${s.play_count === 1 ? "" : "s"}`,
  ].filter(Boolean);

  return (
    <div className="mx-auto max-w-6xl">
      {playback.data ? (
        <VideoPlayer key={id} sceneId={id} playback={playback.data} poster={s.screenshot_url} resumeTime={s.resume_time} />
      ) : playback.isError ? (
        <div className="flex aspect-video items-center justify-center rounded-lg bg-zinc-900 text-zinc-400">
          {s.file.missing_since ? "The video file is missing from the library." : "Playback unavailable."}
        </div>
      ) : (
        <div className="aspect-video rounded-lg bg-zinc-900" />
      )}

      <div className="mt-4 flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold" data-testid="scene-title">{s.title || "Untitled"}</h1>
          <div className="mt-1 flex flex-wrap gap-x-3 text-sm text-zinc-400">{meta.map((m, i) => <span key={i}>{m}</span>)}</div>
        </div>
        <Rating value={s.rating} onChange={canEdit ? (r) => rate.mutate(r) : undefined} />
        {canEdit && <button className="btn-ghost" onClick={() => setEditing(true)}>Edit</button>}
        {canEdit && (
          <button className="btn-ghost" disabled={rebuilding} onClick={rebuild}
                  title="Rebuild this video's thumbnail and scrub previews">
            {rebuilding ? "Rebuilding previews…" : "Rebuild previews"}
          </button>
        )}
        <DeleteMediaButton kind="scenes" ids={[id]} onDeleted={() => navigate("/scenes", { replace: true })} />
      </div>
      {regenerate.isError && <ErrorBox error={regenerate.error} className="mt-2" />}

      <div className="mt-5 grid gap-6 md:grid-cols-[2fr_1fr]">
        <div className="space-y-4">
          {s.performers.length > 0 && (
            <div className="flex flex-wrap gap-3">
              {s.performers.map((p) => (
                <Link key={p.id} to={`/performers/${p.id}`} className="flex items-center gap-2 rounded-full bg-zinc-900 py-1 pl-1 pr-3 hover:bg-zinc-800">
                  {p.image_url
                    ? <img src={p.image_url} alt="" className="h-8 w-8 rounded-full object-cover" />
                    : <Initials name={p.name} className="h-8 w-8 rounded-full text-xs" />}
                  <span className="text-sm">{p.name}</span>
                </Link>
              ))}
            </div>
          )}
          {canEdit ? (
            <div className="grid gap-3 sm:grid-cols-2" data-testid="quick-assign">
              <Field label="Performers">
                <EntityPicker kind="performers" linked value={s.performers.map((p) => p.id)} onCreate={creator("performers")}
                              placeholder="Add performers…" onChange={(ids) => assign.mutate({ performer_ids: ids })} />
              </Field>
              <Field label="Tags">
                <EntityPicker kind="tags" linked value={s.tags.map((t) => t.id)} onCreate={creator("tags")}
                              placeholder="Add tags…" onChange={(ids) => assign.mutate({ tag_ids: ids })} />
              </Field>
              <Field label="Studio">
                <EntityPicker kind="studios" single linked value={s.studio ? [s.studio.id] : []} onCreate={creator("studios")}
                              placeholder="Set studio…" onChange={(ids) => assign.mutate({ studio_id: ids[0] ?? null })} />
              </Field>
              {assign.isError && <ErrorBox error={assign.error} className="sm:col-span-2" />}
            </div>
          ) : (
            <Chips items={s.tags} to="tags" />
          )}
          {s.details && <p className="whitespace-pre-line text-sm leading-relaxed text-zinc-300">{s.details}</p>}
          {s.urls.length > 0 && (
            <ul className="text-sm">
              {s.urls.map((u) => <li key={u}><a href={u} target="_blank" rel="noreferrer noopener" className="text-accent-hover hover:underline">{u}</a></li>)}
            </ul>
          )}
        </div>
        <dl className="card h-fit space-y-2 p-4 text-sm">
          {s.studio && <Row label="Studio"><Link to={`/studios/${s.studio.id}`} className="hover:text-accent-hover">{s.studio.name}</Link></Row>}
          <Row label="Added">{formatDateTime(s.created_at)}</Row>
          {s.last_played_at && <Row label="Last played">{formatDateTime(s.last_played_at)}</Row>}
          <Row label="Organized">{s.organized ? "Yes" : "No"}</Row>
          {s.mine && <Row label="Library">Your upload</Row>}
          {s.file.path && <Row label="File"><span className="break-all font-mono text-xs text-zinc-400">{s.file.path}</span></Row>}
        </dl>
      </div>

      {editing && <ContentEditModal kind="scenes" item={s} onClose={() => setEditing(false)} />}
    </div>
  );
}

export function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[7rem_1fr] gap-2">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}
