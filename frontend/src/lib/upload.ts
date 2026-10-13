import { buildUrl } from "../api/client";

/** Is this a file the server accepts? `accepted` is the server's list of extensions
 *  (from /api/my/libraries); the server checks again on upload. */
export function isMediaFile(name: string, accepted: Iterable<string>): boolean {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return !name.startsWith(".") && new Set(accepted).has(ext);
}

export interface PendingFile {
  file: File;
  /** Destination inside the library, e.g. "Holiday/img1.jpg". */
  path: string;
}

export function joinPath(...parts: string[]): string {
  return parts.flatMap((p) => p.split("/")).map((p) => p.trim()).filter(Boolean).join("/");
}

/** Upload one file as the raw request body. fetch() can't report upload progress; XHR can. */
export function uploadFile(libraryId: number, item: PendingFile, onProgress: (fraction: number) => void) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<void>((resolve, reject) => {
    xhr.open("PUT", buildUrl(`/my/libraries/${libraryId}/files`, { path: item.path }));
    xhr.setRequestHeader("X-Requested-With", "goon");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status === 201) return resolve();
      let detail = xhr.statusText || `HTTP ${xhr.status}`;
      try {
        const body = JSON.parse(xhr.responseText);
        if (typeof body.detail === "string") detail = body.detail;
      } catch { /* not JSON */ }
      reject(new Error(detail));
    };
    xhr.onerror = () => reject(new Error("network error"));
    xhr.onabort = () => reject(new Error("cancelled"));
    xhr.send(item.file);
  });
  return { promise, abort: () => xhr.abort() };
}

// --- collecting files from drops and pickers -------------------------------------

async function walkEntry(entry: FileSystemEntry, prefix: string, out: PendingFile[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((ok, err) => (entry as FileSystemFileEntry).file(ok, err));
    out.push({ file, path: joinPath(prefix, entry.name) });
  } else if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    // readEntries returns results in batches until it yields an empty one.
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((ok, err) => reader.readEntries(ok, err));
      if (!batch.length) break;
      for (const child of batch) await walkEntry(child, joinPath(prefix, entry.name), out);
    }
  }
}

/** Files from a drop event, descending into dropped folders. */
export async function filesFromDrop(data: DataTransfer): Promise<PendingFile[]> {
  const out: PendingFile[] = [];
  // Entries must be read synchronously, before the first await invalidates the DataTransfer.
  const entries = Array.from(data.items)
    .map((item) => item.webkitGetAsEntry?.() ?? null)
    .filter((e): e is FileSystemEntry => e !== null);
  const plain = Array.from(data.files);
  if (entries.length) {
    for (const entry of entries) await walkEntry(entry, "", out);
    return out;
  }
  return plain.map((file) => ({ file, path: file.name }));
}

/** Files from an <input type="file"> (with or without webkitdirectory). */
export function filesFromInput(list: FileList): PendingFile[] {
  return Array.from(list).map((file) => ({
    file,
    path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
  }));
}
