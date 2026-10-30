import { useState, type FormEvent } from "react";
import { useParams } from "react-router";
import { api } from "../api/client";
import { useDetail, useRate, useWrite } from "../api/hooks";
import type { PerformerDetail as Performer } from "../api/types";
import Chips from "../components/Chips";
import { EntityImageControls, FormButtons } from "../components/EditForms";
import EntityContent from "../components/EntityContent";
import { DeleteEntityButton } from "../components/EntityDialogs";
import EntityPicker, { creator } from "../components/EntityPicker";
import FolderLinks from "../components/FolderLinks";
import { ErrorBox, Field, Initials, Modal, Rating, Spinner } from "../components/ui";
import { age, formatDate, GENDERS } from "../lib/format";
import { Row } from "./SceneDetail";

export default function PerformerDetail() {
  const id = Number(useParams().id);
  const q = useDetail<Performer>(`/performers/${id}`);
  const [editing, setEditing] = useState(false);
  const rate = useRate(`/performers/${id}`);
  const favorite = useWrite((on: boolean) => api(`/performers/${id}/favorite`, { method: "PUT", json: { favorite: on } }));

  if (q.isPending) return <Spinner className="mt-24" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const p = q.data;
  const canEdit = p.can_edit;
  const years = age(p.birthdate);
  const facts: [string, React.ReactNode][] = [
    ["Gender", p.gender && GENDERS[p.gender]],
    ["Born", p.birthdate && `${formatDate(p.birthdate)}${years != null ? ` (${years})` : ""}`],
    ["Country", p.country], ["Ethnicity", p.ethnicity], ["Hair", p.hair_color], ["Eyes", p.eye_color],
    ["Height", p.height_cm && `${p.height_cm} cm`], ["Weight", p.weight_kg && `${p.weight_kg} kg`],
    ["Measurements", p.measurements], ["Tattoos", p.tattoos], ["Piercings", p.piercings],
    ["Career", (p.career_start || p.career_end) && `${p.career_start ?? "?"} – ${p.career_end ?? "present"}`],
  ];

  return (
    <div>
      <div className="flex flex-col gap-6 md:flex-row">
        <div className="w-full shrink-0 md:w-64">
          <div className="aspect-[2/3] overflow-hidden rounded-lg bg-surface">
            {p.image_url ? <img src={p.image_url} alt="" className="h-full w-full object-cover" /> : <Initials name={p.name} className="h-full w-full text-5xl" />}
          </div>
          {canEdit && <div className="mt-2"><EntityImageControls kind="performers" id={id} hasImage={!!p.image_url} /></div>}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-3xl font-semibold">{p.name}</h1>
            {p.disambiguation && <span className="text-text-subtle">({p.disambiguation})</span>}
            <button className={`text-2xl hover:text-link ${p.favorite ? "text-link" : "text-text-subtle"}`}
                    onClick={() => favorite.mutate(!p.favorite)} aria-pressed={p.favorite}
                    aria-label={p.favorite ? "Remove from my favourites" : "Add to my favourites"}
                    title={p.favorite ? "One of your favourites" : "Add to your favourites"}>♥</button>
            <Rating key={id} value={p.rating} onChange={canEdit ? rate.save : undefined} error={rate.error} />
            {canEdit && <div className="ml-auto flex gap-2"><button className="btn-ghost" onClick={() => setEditing(true)}>Edit</button><DeleteEntityButton kind="performers" id={id} name={p.name} /></div>}
          </div>
          {p.aliases.length > 0 && <p className="mt-1 text-sm text-text-muted">Also known as {p.aliases.join(", ")}</p>}
          <dl className="mt-4 grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
            {facts.filter(([, v]) => v).map(([k, v]) => <Row key={k} label={k}>{v}</Row>)}
          </dl>
          <div className="mt-4"><Chips items={p.tags} to="tags" /></div>
          {p.details && <p className="mt-4 whitespace-pre-line text-sm text-text">{p.details}</p>}
          <FolderLinks kind="performers" id={id} />
        </div>
      </div>
      <EntityContent filter={{ performer_ids: [id] }} scope="this performer" />
      {editing && <PerformerEditModal performer={p} onClose={() => setEditing(false)} />}
    </div>
  );
}

const TEXT_FIELDS: [keyof Performer, string, string?][] = [
  ["name", "Name"], ["disambiguation", "Disambiguation"], ["birthdate", "Birthdate", "date"], ["deathdate", "Death date", "date"],
  ["country", "Country"], ["ethnicity", "Ethnicity"], ["hair_color", "Hair color"], ["eye_color", "Eye color"],
  ["height_cm", "Height (cm)", "number"], ["weight_kg", "Weight (kg)", "number"], ["measurements", "Measurements"],
  ["career_start", "Career start (year)", "number"], ["career_end", "Career end (year)", "number"],
  ["tattoos", "Tattoos"], ["piercings", "Piercings"],
];

function PerformerEditModal({ performer, onClose }: { performer: Performer; onClose: () => void }) {
  const [form, setForm] = useState<Record<string, string>>(() =>
    Object.fromEntries(TEXT_FIELDS.map(([k]) => [k, performer[k] == null ? "" : String(performer[k])])));
  const [gender, setGender] = useState(performer.gender ?? "");
  const [aliases, setAliases] = useState(performer.aliases.join(", "));
  const [details, setDetails] = useState(performer.details ?? "");
  const [tags, setTags] = useState(performer.tags.map((t) => t.id));
  const save = useWrite((body: object) => api(`/performers/${performer.id}`, { method: "PATCH", json: body }));

  function submit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = { gender: gender || null, details: details.trim() || null, tag_ids: tags,
      aliases: aliases.split(",").map((a) => a.trim()).filter(Boolean) };
    for (const [k, , type] of TEXT_FIELDS) {
      const v = form[k as string].trim();
      body[k as string] = v === "" ? null : type === "number" ? Number(v) : v;
    }
    save.mutate(body, { onSuccess: onClose });
  }

  return (
    <Modal title="Edit performer" onClose={onClose} wide>
      <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2">
        {TEXT_FIELDS.map(([k, label, type]) => (
          <Field key={k as string} label={label}>
            <input className="input" type={type ?? "text"} value={form[k as string]} required={k === "name"}
                   onChange={(e) => setForm((f) => ({ ...f, [k]: e.target.value }))} />
          </Field>
        ))}
        <Field label="Gender">
          <select className="input" value={gender} onChange={(e) => setGender(e.target.value)}>
            <option value="">—</option>
            {Object.entries(GENDERS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
        <div className="sm:col-span-2"><Field label="Aliases (comma separated)"><input className="input" value={aliases} onChange={(e) => setAliases(e.target.value)} /></Field></div>
        <div className="sm:col-span-2"><Field label="Tags"><EntityPicker kind="tags" value={tags} onChange={setTags} onCreate={creator("tags")} /></Field></div>
        <div className="sm:col-span-2"><Field label="Details"><textarea className="input min-h-24" value={details} onChange={(e) => setDetails(e.target.value)} /></Field></div>
        <span />
        <FormButtons busy={save.isPending} error={save.error} onCancel={onClose} />
      </form>
    </Modal>
  );
}
