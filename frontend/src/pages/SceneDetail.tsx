import { useState } from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client";
import { useDetail, useMe, useWrite } from "../api/hooks";
import type { Playback, SceneDetail as Scene } from "../api/types";
import Chips from "../components/Chips";
import { ContentEditModal } from "../components/EditForms";
import VideoPlayer from "../components/VideoPlayer";
import { ErrorBox, Initials, Rating, Spinner } from "../components/ui";
import { formatBytes, formatDate, formatDateTime, formatDuration, resolutionLabel } from "../lib/format";

export default function SceneDetail() {
  const id = Number(useParams().id);
  const scene = useDetail<Scene>(`/scenes/${id}`);
  const playback = useDetail<Playback>(`/scenes/${id}/playback`);
  const { canEdit } = useMe();
  const [editing, setEditing] = useState(false);
  const rate = useWrite((rating: number | null) => api(`/scenes/${id}`, { method: "PATCH", json: { rating } }));

  if (scene.isPending) return <Spinner className="mt-24" />;
  if (scene.isError) return <ErrorBox error={scene.error} />;
  const s = scene.data;
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
      </div>

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
          <Chips items={s.tags} to="tags" />
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
          {canEdit && <Row label="File"><span className="break-all font-mono text-xs text-zinc-400">{s.file.path}</span></Row>}
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
