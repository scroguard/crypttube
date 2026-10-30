import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useDetail, useRate } from "../api/hooks";
import type { GalleryDetail as Gallery } from "../api/types";
import { RefLinks } from "../components/Cards";
import Chips from "../components/Chips";
import DeleteMediaButton from "../components/DeleteMedia";
import { ContentEditModal } from "../components/EditForms";
import ImageBrowser from "../components/ImageBrowser";
import { ErrorBox, Rating, Spinner } from "../components/ui";
import { formatDate } from "../lib/format";

export default function GalleryDetail() {
  const id = Number(useParams().id);
  const gallery = useDetail<Gallery>(`/galleries/${id}`);
  const [editing, setEditing] = useState(false);
  const navigate = useNavigate();
  const rate = useRate(`/galleries/${id}`);

  if (gallery.isPending) return <Spinner className="mt-24" />;
  if (gallery.isError) return <ErrorBox error={gallery.error} />;
  const g = gallery.data;
  const canEdit = g.can_edit;
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold">{g.title || "Untitled"}</h1>
          <div className="mt-1 flex flex-wrap gap-x-3 text-sm text-text-muted">
            <span>{g.image_count} images</span>
            {g.release_date && <span>{formatDate(g.release_date)}</span>}
            {g.studio && <Link to={`/studios/${g.studio.id}`} className="hover:text-text">{g.studio.name}</Link>}
            {g.performers.length > 0 && <RefLinks items={g.performers} to="performers" max={6} />}
          </div>
        </div>
        <Rating key={id} value={g.rating} onChange={canEdit ? rate.save : undefined} error={rate.error} />
        {canEdit && <button className="btn-ghost" onClick={() => setEditing(true)}>Edit</button>}
        <DeleteMediaButton kind="galleries" ids={[id]} onDeleted={() => navigate("/galleries", { replace: true })} />
      </div>
      <div className="mb-4"><Chips items={g.tags} to="tags" /></div>
      {g.details && <p className="mb-4 whitespace-pre-line text-sm text-text">{g.details}</p>}
      <ImageBrowser path={`/galleries/${id}/images`} />
      {editing && <ContentEditModal kind="galleries" item={g} onClose={() => setEditing(false)} />}
    </div>
  );
}
