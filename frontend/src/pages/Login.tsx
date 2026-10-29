import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { LogoStacked } from "../components/Logo";
import { ErrorBox, Field } from "../components/ui";

export function AuthCard({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="card w-full max-w-sm p-6">
        <LogoStacked className="mx-auto mb-6 h-24 w-auto" />
        <div className="mb-6 text-center">
          <h1 className="text-xl">{title}</h1>
          {subtitle && <p className="text-sm text-text-muted">{subtitle}</p>}
        </div>
        {children}
      </div>
    </div>
  );
}

export default function Login() {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", { json: { username, password } });
      await qc.invalidateQueries({ queryKey: ["auth"] });
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Sign in">
      <form onSubmit={submit} className="space-y-4">
        <Field label="Username">
          <input className="input" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} required />
        </Field>
        <Field label="Password">
          <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        {error ? <ErrorBox error={error} /> : null}
        <button className="btn-primary w-full" disabled={busy}>Sign in</button>
      </form>
    </AuthCard>
  );
}
