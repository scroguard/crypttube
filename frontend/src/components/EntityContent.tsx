import { useState } from "react";
import type { Query } from "../api/client";
import { useInfiniteList } from "../api/hooks";
import type { GalleryItem } from "../api/types";
import { GalleryCard } from "./Cards";
import { Tabs } from "./EntityDialogs";
import { SearchBox } from "./FilterBar";
import ImageBrowser from "./ImageBrowser";
import InfiniteGrid from "./InfiniteGrid";
import SceneBrowser from "./SceneBrowser";

/** Scenes / galleries / images tabs for a performer, studio or tag page. Each tab's
 * list shows its own live total (the cached entity counts can lag edits briefly). */
export default function EntityContent({ filter, scope }: {
  filter: Query;
  /** What searches here are limited to, e.g. "this studio". */
  scope: string;
}) {
  const [tab, setTab] = useState<"scenes" | "galleries" | "images">("scenes");
  return (
    <div className="mt-6">
      <Tabs tabs={{ scenes: "Scenes", galleries: "Galleries", images: "Images" }} value={tab} onChange={setTab} />
      {tab === "scenes" && <SceneBrowser fixed={filter} scope={scope} />}
      {tab === "galleries" && <Galleries filter={filter} scope={scope} />}
      {tab === "images" && <ImageBrowser path="/images" query={filter} scope={scope} />}
    </div>
  );
}

function Galleries({ filter, scope }: { filter: Query; scope: string }) {
  const [q, setQ] = useState("");
  const list = useInfiniteList<GalleryItem>("/galleries", q ? { ...filter, q } : filter);
  return (
    <>
      <div className="mb-3"><SearchBox value={q} onChange={setQ} placeholder={`Search in ${scope}…`} /></div>
      <InfiniteGrid list={list} render={(g) => <GalleryCard gallery={g} />} empty={q ? "Nothing matches." : "No galleries."} />
    </>
  );
}
