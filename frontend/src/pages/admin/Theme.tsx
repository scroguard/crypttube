import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import { useWrite } from "../../api/hooks";
import { ErrorBox, Spinner } from "../../components/ui";
import { contrastWarnings, parseColor } from "../../lib/contrast";
import { PRESETS, ROLES, ROLE_GROUPS, type Colors, type Role } from "../../lib/themes";
import AdminNav from "./AdminNav";

interface Saved { preset: string; colors: Colors }

const BUILT_IN = PRESETS[0];
const toHex = (n: number) => n.toString(16).padStart(2, "0").toUpperCase();
const roleName = (r: Role) => r.replace(/-/g, " ");

/** The page's theme stylesheet (index.html) is re-fetched so a saved theme shows at once. */
function reloadThemeCss() {
  const link = document.getElementById("theme-css") as HTMLLinkElement | null;
  if (link) link.href = `/api/theme.css?v=${Date.now()}`;
}

export default function AdminTheme() {
  const q = useQuery({ queryKey: ["theme"], queryFn: () => api<{ theme: Saved | null }>("/theme") });
  const saved: Saved = q.data?.theme ?? { preset: BUILT_IN.id, colors: BUILT_IN.colors };
  const [draft, setDraft] = useState<Saved | null>(null);
  useEffect(() => { if (q.data) setDraft(q.data.theme ?? { preset: BUILT_IN.id, colors: BUILT_IN.colors }); }, [q.data]);

  // Live preview of the whole app while editing; undone on leaving the page.
  useEffect(() => {
    if (!draft) return;
    const style = document.documentElement.style;
    for (const r of ROLES) if (parseColor(draft.colors[r])) style.setProperty(`--color-${r}`, draft.colors[r]);
    return () => ROLES.forEach((r) => style.removeProperty(`--color-${r}`));
  }, [draft]);

  const customCss = useQuery({
    queryKey: ["custom-css"],
    queryFn: async () => {
      const r = await fetch("/custom.css");
      return r.ok && (await r.text()).replace(/\/\*[\s\S]*?\*\//g, "").trim().length > 0;
    },
    staleTime: Infinity,
  });

  const save = useWrite((t: Saved) => api("/theme", { method: "PUT", json: t }));
  const reset = useWrite(() => api("/theme", { method: "DELETE" }));
  const warnings = useMemo(() => (draft ? contrastWarnings(draft.colors) : []), [draft]);

  if (q.isError) return <><AdminNav /><ErrorBox error={q.error} /></>;
  if (!draft) return <><AdminNav /><Spinner /></>;

  const preset = PRESETS.find((p) => p.id === draft.preset) ?? BUILT_IN;
  const customized = ROLES.some((r) => draft.colors[r] !== preset.colors[r]);
  const invalid = ROLES.filter((r) => !parseColor(draft.colors[r]));
  const dirty = draft.preset !== saved.preset || ROLES.some((r) => draft.colors[r] !== saved.colors[r]);
  const set = (r: Role, v: string) => setDraft({ ...draft, colors: { ...draft.colors, [r]: v } });

  const onSave = () => save.mutate(draft, { onSuccess: reloadThemeCss });
  const onReset = () => reset.mutate(undefined, {
    onSuccess: () => {
      setDraft({ preset: BUILT_IN.id, colors: BUILT_IN.colors });
      reloadThemeCss();
    },
  });

  return (
    <>
      <AdminNav />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="mr-auto text-xl">Theme</h1>
        <button className="btn-ghost" disabled={!dirty} onClick={() => setDraft(saved)}>Undo changes</button>
        <button className="btn-ghost" disabled={!q.data?.theme || reset.isPending} onClick={onReset}>Use the built-in theme</button>
        <button className="btn-primary" disabled={!dirty || invalid.length > 0 || save.isPending} onClick={onSave}>Save for everyone</button>
      </div>
      <p className="mb-4 text-sm text-text-muted">
        One theme for every user. Changes preview here as you make them; nobody else sees them until you save.
        {customCss.data && <> A server stylesheet (<code className="font-mono">CUSTOM_CSS_FILE</code>) is also loaded and wins over these colors.</>}
      </p>
      {save.error || reset.error ? <ErrorBox error={save.error ?? reset.error} className="mb-4" /> : null}

      <section className="mb-6" aria-label="Presets">
        <h2 className="label">Preset</h2>
        <div className="grid gap-3 sm:grid-cols-3">
          {PRESETS.map((p) => (
            <button key={p.id} aria-pressed={draft.preset === p.id} onClick={() => setDraft({ preset: p.id, colors: p.colors })}
                    className={`card p-3 text-left ${draft.preset === p.id ? "border-accent" : "hover:border-border-strong"}`}>
              <div className="mb-2 flex h-8 overflow-hidden rounded-sm border border-border">
                {(["bg", "surface", "surface-raised", "primary", "accent", "text"] as Role[]).map((r) => (
                  <span key={r} className="flex-1" style={{ background: p.colors[r] }} />
                ))}
              </div>
              <div className="font-medium">{p.name}{draft.preset === p.id && customized ? " (customized)" : ""}</div>
              <div className="text-xs text-text-muted">{p.note}</div>
            </button>
          ))}
        </div>
      </section>

      {warnings.length > 0 && (
        <div className="mb-6 rounded-md border border-warning/40 bg-warning-bg px-4 py-3 text-sm text-warning" role="status" data-testid="contrast-warnings">
          <p className="mb-1 font-medium">Hard to read: these pairs are below the contrast the brand requires.</p>
          <ul className="list-disc pl-5">
            {warnings.map((w) => (
              <li key={`${w.fg}-${w.bg}`}>{roleName(w.fg)} on {roleName(w.bg)}: {w.ratio.toFixed(2)}:1 (needs {w.min}:1)</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        {ROLE_GROUPS.map((g) => (
          <section key={g.title} className="card p-4" aria-label={g.title}>
            <h2 className="mb-3 text-lg">{g.title}</h2>
            <div className="space-y-2">
              {g.roles.map(([r, use]) => (
                <RoleRow key={r} role={r} use={use} value={draft.colors[r]} onChange={(v) => set(r, v)} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </>
  );
}

function RoleRow({ role, use, value, onChange }: { role: Role; use: string; value: string; onChange: (v: string) => void }) {
  const parsed = parseColor(value);
  const hex = parsed ? `#${parsed.rgb.map(toHex).join("")}` : "#000000";
  const overlay = role === "overlay";
  const rgba = (h: string, a: number) => {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    return `rgba(${r}, ${g}, ${b}, ${Math.round(a * 100) / 100})`;
  };
  return (
    <div className="flex flex-wrap items-center gap-3">
      <input type="color" aria-label={`${roleName(role)} color`} value={hex} className="h-9 w-12 cursor-pointer rounded-sm border border-border-strong bg-surface"
             onChange={(e) => onChange(overlay ? rgba(e.target.value, parsed?.alpha ?? 1) : e.target.value.toUpperCase())} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{roleName(role)}</div>
        <div className="truncate text-xs text-text-muted">{use}</div>
      </div>
      {overlay ? (
        <label className="flex items-center gap-2 text-xs text-text-muted">
          Opacity
          <input type="range" min={0} max={1} step={0.01} value={parsed?.alpha ?? 1} aria-label="overlay opacity" className="accent-accent"
                 onChange={(e) => onChange(rgba(hex, Number(e.target.value)))} />
          <span className="w-9 font-mono">{Math.round((parsed?.alpha ?? 1) * 100)}%</span>
        </label>
      ) : (
        <input className={`input w-28 font-mono text-xs ${parsed ? "" : "border-danger"}`} aria-label={roleName(role)} value={value}
               aria-invalid={!parsed} onChange={(e) => onChange(e.target.value.trim().toUpperCase())} />
      )}
    </div>
  );
}
