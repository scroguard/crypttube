import { useState } from "react";
import type { Query } from "../api/client";
import { useInfiniteList } from "../api/hooks";
import type { GalleryItem } from "../api/types";
import { GalleryCard } from "./Cards";
import { Tabs } from "./EntityDialogs";
import ImageBrowser from "./ImageBrowser";
import InfiniteGrid from "./InfiniteGrid";
import SceneBrowser from "./SceneBrowser";

/** Scenes / galleries / images tabs for a performer, studio or tag page. Each tab's
 * list shows its own live total (the cached entity counts can lag edits briefly). */
export default function EntityContent({ filter }: { filter: Query }) {
  const [tab, setTab] = useState<"scenes" | "galleries" | "images">("scenes");
  return (
    <div className="mt-6">
      <Tabs tabs={{ scenes: "Scenes", galleries: "Galleries", images: "Images" }} value={tab} onChange={setTab} />
      {tab === "scenes" && <SceneBrowser fixed={filter} />}
      {tab === "galleries" && <Galleries filter={filter} />}
      {tab === "images" && <ImageBrowser path="/images" query={filter} />}
    </div>
  );
}

function Galleries({ filter }: { filter: Query }) {
  const list = useInfiniteList<GalleryItem>("/galleries", filter);
  return <InfiniteGrid list={list} render={(g) => <GalleryCard gallery={g} />} empty="No galleries." />;
}
