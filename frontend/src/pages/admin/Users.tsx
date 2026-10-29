import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import type { AdminUser, Role } from "../../api/types";
import { FormButtons } from "../../components/EditForms";
import { ErrorBox, Field, Modal, Spinner } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import AdminNav from "./AdminNav";

export const ROLE_HELP: Record<Role, string> = {
  viewer: "Browse and watch",
  editor: "Browse, watch and edit metadata",
  admin: "Everything, incl. users and libraries; never restricted",
};

export default function Users() {
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<AdminUser[]>("/users") });
  const [creating, setCreating] = useState(false);
  return (
    <div className="mx-auto max-w-4xl">
      <AdminNav />
      <div className="mb-4 flex items-center">
        <h1 className="mr-auto text-xl font-semibold">Users</h1>
        <button className="btn-primary" onClick={() => setCreating(true)}>New user</button>
      </div>
      {users.isPending ? <Spinner /> : users.isError ? <ErrorBox error={users.error} /> : (
        <div className="card divide-y divide-zinc-800">
          {users.data.map((u) => (
            <Link key={u.id} to={`/admin/users/${u.id}`} className="flex items-center gap-4 p-4 hover:bg-zinc-800/50" data-testid="user-row">
              <div className="min-w-0 flex-1">
                <div className="font-medium">{u.username} {!u.is_active && <span className="text-xs text-red-400">(disabled)</span>}</div>
                <div className="text-xs text-zinc-500">Last login {formatDateTime(u.last_login_at)}</div>
              </div>
              {!!u.restriction_count && <span className="chip">{u.restriction_count} restrictions</span>}
              <span className="w-16 text-right text-sm capitalize text-zinc-400">{u.role}</span>
            </Link>
          ))}
        </div>
      )}
      {creating && <CreateUserModal onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateUserModal({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const create = useWrite((body: object) => api<AdminUser>("/users", { json: body }));
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate({ username, password, role }, { onSuccess: (u) => navigate(`/admin/users/${u.id}`) });
  }
  return (
    <Modal title="New user" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Username"><input className="input" value={username} onChange={(e) => setUsername(e.target.value)} pattern="[A-Za-z0-9._\-]+" required /></Field>
        <Field label="Password (8+ characters)"><input className="input" type="password" autoComplete="new-password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
        <Field label="Role">
          <select className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {(Object.keys(ROLE_HELP) as Role[]).map((r) => <option key={r} value={r}>{r} — {ROLE_HELP[r]}</option>)}
          </select>
        </Field>
        <FormButtons busy={create.isPending} error={create.error} onCancel={onClose} label="Create" />
      </form>
    </Modal>
  );
}
