import { downloadUrl, FileItem } from "../../lib/api";

// The tile says what a file is at a glance: an image shows itself, anything else its extension in the family colour.
const TONES: [RegExp, string][] = [
  [/^pdf$/, "red"],
  [/^(docx?|rtf|odt|pages)$/, "blue"],
  [/^(xlsx?|xlsm|csv|tsv|ods|numbers)$/, "green"],
  [/^(pptx?|odp|key)$/, "orange"],
  [/^(png|jpe?g|gif|webp|svg|bmp|heic|avif)$/, "violet"],
  [/^(zip|tar|gz|tgz|7z|rar)$/, "amber"],
];

export function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function isImage(file: FileItem): boolean {
  return file.content_type.startsWith("image/");
}

const INLINE_MAX = 256 * 1024; // small enough to show as is (SVGs, icons) when there is no thumbnail

/** The Worker's thumbnail; never the original of a big image just to fill a 36px tile. Only once scanned. */
export function thumbUrl(file: FileItem): string | null {
  if (file.status !== "ready") return null;
  if (file.thumbnail_url) return downloadUrl(file.thumbnail_url);
  return isImage(file) && file.size <= INLINE_MAX ? downloadUrl(file.download_url) : null;
}

export default function FileTile({ file, size = 36 }: { file: FileItem; size?: number }) {
  const thumb = thumbUrl(file);
  const ext = extOf(file.filename);
  const tone = TONES.find(([re]) => re.test(ext))?.[1] || "gray";
  return (
    <span className={`file-tile tone-${tone}`} style={{ width: size, height: size }} aria-hidden="true">
      {thumb ? <img src={thumb} alt="" loading="lazy" decoding="async" /> : <span className="file-tile-ext">{(ext || "file").slice(0, 4)}</span>}
    </span>
  );
}
