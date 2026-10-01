import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { api } from "../api/client";
import { useWrite } from "../api/hooks";
import type { Ref } from "../api/types";
import type { EntityKind } from "./EntityPicker";
import { FormButtons } from "./EditForms";
import { Field, Modal } from "./ui";

export function CreateEntityButton({ kind, label }: { kind: EntityKind; label: string }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const navigate = useNavigate();
  const create = useWrite((n: string) => api<Ref>(`/${kind}`, { json: { name: n } }));
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate(name.trim(), { onSuccess: (r) => navigate(`/${kind}/${r.id}`) });
  }
  return (
    <>
      <button className="btn-primary" onClick={() => setOpen(true)}>New {label}</button>
      {open && (
        <Modal title={`New ${label}`} onClose={() => setOpen(false)}>
          <form onSubmit={submit} className="space-y-4">
            <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} required /></Field>
            <FormButtons busy={create.isPending} error={create.error} onCancel={() => setOpen(false)} label="Create" />
          </form>
        </Modal>
      )}
    </>
  );
}

export function DeleteEntityButton({ kind, id, name }: { kind: EntityKind; id: number; name: string }) {
  const navigate = useNavigate();
  const del = useWrite(() => api(`/${kind}/${id}`, { method: "DELETE" }));
  return (
    <button className="btn-danger" disabled={del.isPending} onClick={() => {
      if (confirm(`Delete “${name}”? Content stays; only the ${kind.slice(0, -1)} and its links are removed.`))
        del.mutate(undefined, { onSuccess: () => navigate(`/${kind}`, { replace: true }) });
    }}>
      Delete
    </button>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: Record<T, string>; value: T; onChange: (t: T) => void }) {
  return (
    <div className="mb-4 flex gap-1 border-b border-zinc-800" role="tablist">
      {(Object.entries(tabs) as [T, string][]).map(([k, label]) => (
        <button key={k} role="tab" aria-selected={value === k} onClick={() => onChange(k)}
                className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${value === k ? "border-accent text-white" : "border-transparent text-zinc-400 hover:text-white"}`}>
          {label}
        </button>
      ))}
    </div>
  );
}
