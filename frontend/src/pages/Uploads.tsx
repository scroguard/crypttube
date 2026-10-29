import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../api/client";
import type { MyLibraries } from "../api/types";
import LibrarySettings, { VisibilityBadge } from "../components/LibrarySettings";
import { Empty, ErrorBox, Spinner } from "../components/ui";
import { formatBytes, plural } from "../lib/format";

export function useMyLibraries() {
  return useQuery({ queryKey: ["my-libraries"], queryFn: () => api<MyLibraries>("/my/libraries") });
}

export function QuotaBar({ used, quota }: { used: number; quota: number | null }) {
  const pct = quota ? Math.min(100, (used / quota) * 100) : 0;
  return (
    <div className="w-64 text-xs text-text-muted" data-testid="quota">
      <div className="mb-1 flex justify-between">
        <span>{formatBytes(used)} used</span>
        <span>{quota ? `of ${formatBytes(quota)}` : "no limit"}</span>
      </div>
      {quota !== null && (
        <div className="h-1.5 rounded bg-surface-raised"><div className={`h-full rounded ${pct > 90 ? "bg-danger" : "bg-accent"}`} style={{ width: `${pct}%` }} /></div>
      )}
    </div>
  );
}

export default function Uploads() {
  const q = useMyLibraries();
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  if (q.isPending) return <Spinner className="mt-24" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data.can_upload) return <Empty>Uploads aren't enabled for your account. Ask an administrator.</Empty>;
  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-6 flex flex-wrap items-end gap-4">
        <div className="mr-auto">
          <h1 className="text-2xl font-semibold">My uploads</h1>
          <p className="text-sm text-text-muted">Your own libraries. You decide who else can see each one.</p>
        </div>
        <QuotaBar used={q.data.used_bytes} quota={q.data.quota_bytes} />
        <button className="btn-primary" onClick={() => setCreating(true)}>New library</button>
      </div>
      {q.data.libraries.length === 0 ? <Empty>No libraries yet. Create one to start uploading.</Empty> : (
        <div className="grid gap-3 sm:grid-cols-2">
          {q.data.libraries.map((lib) => (
            <Link key={lib.id} to={`/uploads/${lib.id}`} className="card block p-4 hover:border-border-strong" data-testid="my-library">
              <div className="flex items-center gap-2">
                <h2 className="mr-auto truncate font-semibold">{lib.name}</h2>
                <VisibilityBadge library={lib} />
              </div>
              <p className="mt-1 text-sm text-text-muted">
                {plural(lib.scene_count, "video")} · {plural(lib.image_count, "image")} · {formatBytes(lib.bytes)}
              </p>
            </Link>
          ))}
        </div>
      )}
      {creating && <LibrarySettings onClose={() => setCreating(false)} onSaved={(lib) => navigate(`/uploads/${lib.id}`)} />}
    </div>
  );
}
