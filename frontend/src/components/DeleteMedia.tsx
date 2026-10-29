import { api } from "../api/client";
import { useMe, useWrite } from "../api/hooks";

export interface DeleteResult { deleted: number; files: number; bytes: number; skipped: { id: number; reason: string }[] }
type Kind = "scenes" | "images" | "galleries";
const NOUN: Record<Kind, [string, string]> = { scenes: ["scene", "scenes"], images: ["image", "images"], galleries: ["gallery", "galleries"] };
const CHUNK = 500;

/** Delete from disk, in chunks. Throws with the reason when nothing at all could be deleted. */
export async function deleteMedia(kind: Kind, ids: number[]): Promise<DeleteResult> {
  const total: DeleteResult = { deleted: 0, files: 0, bytes: 0, skipped: [] };
  for (let i = 0; i < ids.length; i += CHUNK) {
    const r = await api<DeleteResult>("/delete", { json: { kind, ids: ids.slice(i, i + CHUNK) } });
    total.deleted += r.deleted;
    total.files += r.files;
    total.bytes += r.bytes;
    total.skipped.push(...r.skipped);
  }
  if (!total.deleted && total.skipped.length) throw new Error(`Not deleted: ${total.skipped[0].reason}.`);
  return total;
}

/** Permanently delete the files behind the given items, after confirming: admins in the
 *  main libraries, users with uploads in their own (the server checks each item). */
export default function DeleteMediaButton({ kind, ids, onDeleted, className = "btn-danger" }: {
  kind: Kind; ids: number[]; onDeleted?: (r: DeleteResult) => void; className?: string;
}) {
  const { isAdmin, canUpload } = useMe();
  // onDeleted runs as part of the mutation: by the time the caches refresh, a detail page
  // for the deleted item may already have unmounted this button (and its callbacks).
  const del = useWrite(async () => {
    const result = await deleteMedia(kind, ids);
    onDeleted?.(result);
    return result;
  });
  if (!isAdmin && !canUpload) return null;
  const what = ids.length === 1 ? `this ${NOUN[kind][0]}` : `${ids.length.toLocaleString()} ${NOUN[kind][1]}`;
  const extra = kind === "galleries" ? " A gallery's archive, or every image in its folder, is deleted with it." : "";
  return (
    <>
      <button className={className} disabled={del.isPending || !ids.length}
              onClick={() => confirm(`Delete ${what} from disk?${extra} The files are removed permanently, along with their details and previews. This cannot be undone.`)
                && del.mutate(undefined)}>
        {del.isPending ? "Deleting…" : ids.length > 1 ? "Delete files" : "Delete"}
      </button>
      {del.isError && <span className="text-sm text-danger" role="alert">{(del.error as Error).message}</span>}
      {del.data && del.data.skipped.length > 0 && (
        <span className="text-sm text-warning" role="status">
          {del.data.deleted} deleted, {del.data.skipped.length} skipped ({del.data.skipped[0].reason})
        </span>
      )}
    </>
  );
}
