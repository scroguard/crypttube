import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import type { MyLibrary, Visibility } from "../api/types";
import { FormButtons } from "./EditForms";
import { Field, Modal } from "./ui";

export const VISIBILITY_LABEL: Record<Visibility, string> = {
  private: "Private (only you)",
  users: "Shared with selected users",
  everyone: "Shared with everyone",
};

export function VisibilityBadge({ library }: { library: Pick<MyLibrary, "visibility" | "shared_with"> }) {
  if (library.visibility === "everyone") return <span className="chip bg-info-bg text-info">Everyone</span>;
  if (library.visibility === "users")
    return <span className="chip bg-info-bg text-info">Shared with {library.shared_with.length}</span>;
  return <span className="chip">Private</span>;
}

/** Create a library, or edit an existing one's name and sharing. */
export default function LibrarySettings({ library, onClose, onSaved }: {
  library?: MyLibrary; onClose: () => void; onSaved?: (lib: MyLibrary) => void;
}) {
  const [name, setName] = useState(library?.name ?? "");
  const [visibility, setVisibility] = useState<Visibility>(library?.visibility ?? "private");
  const [shared, setShared] = useState<number[]>(library?.shared_with.map((u) => u.id) ?? []);
  const users = useQuery({
    queryKey: ["share-targets"],
    queryFn: () => api<{ id: number; username: string }[]>("/my/share-targets"),
  });
  const save = useWrite((body: object) =>
    library ? api<MyLibrary>(`/my/libraries/${library.id}`, { method: "PATCH", json: body })
            : api<MyLibrary>("/my/libraries", { json: body }));

  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate({ name: name.trim(), visibility, shared_user_ids: shared },
                { onSuccess: (lib) => { onSaved?.(lib); onClose(); } });
  }
  const toggle = (id: number) => setShared((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  return (
    <Modal title={library ? "Library settings" : "New library"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} /></Field>
        <Field label="Who can see it">
          <select className="input" value={visibility} onChange={(e) => setVisibility(e.target.value as Visibility)}>
            {(Object.keys(VISIBILITY_LABEL) as Visibility[]).map((v) => <option key={v} value={v}>{VISIBILITY_LABEL[v]}</option>)}
          </select>
        </Field>
        {visibility === "users" && (
          <fieldset>
            <legend className="label">Share with</legend>
            <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-border p-2">
              {users.data?.map((u) => (
                <label key={u.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm hover:bg-surface-raised">
                  <input type="checkbox" className="accent-accent" checked={shared.includes(u.id)} onChange={() => toggle(u.id)} />
                  {u.username}
                </label>
              ))}
              {users.data?.length === 0 && <p className="px-2 py-1 text-sm text-text-subtle">No other users yet.</p>}
            </div>
          </fieldset>
        )}
        <p className="text-xs text-text-subtle">
          {visibility === "private"
            ? "Nobody else can see this library or anything in it, including administrators."
            : "People you share with can browse and play, but not change, what's in this library. You can stop sharing at any time."}
        </p>
        <FormButtons busy={save.isPending} error={save.error} onCancel={onClose} label={library ? "Save" : "Create"} />
      </form>
    </Modal>
  );
}
