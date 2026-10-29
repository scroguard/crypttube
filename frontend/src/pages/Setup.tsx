import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { ErrorBox, Field } from "../components/ui";
import { AuthCard } from "./Login";

export default function Setup() {
  const qc = useQueryClient();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== confirm) return setError(new Error("Passwords don't match"));
    try {
      await api("/auth/setup", { json: { username, password } });
      await qc.invalidateQueries({ queryKey: ["auth"] });
    } catch (err) {
      setError(err);
    }
  }

  return (
    <AuthCard title="Welcome" subtitle="Create the administrator account">
      <form onSubmit={submit} className="space-y-4">
        <Field label="Username">
          <input className="input" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </Field>
        <Field label="Password (8+ characters)">
          <input className="input" type="password" autoComplete="new-password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        <Field label="Confirm password">
          <input className="input" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
        </Field>
        {error ? <ErrorBox error={error} /> : null}
        <button className="btn-primary w-full">Create account</button>
      </form>
    </AuthCard>
  );
}
