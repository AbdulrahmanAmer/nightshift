/* The Nightshift mark: the owner's N-with-moon-and-play-triangle logo, vectorized (public/brand/, README in the owner-vector
   folder). Inline SVG so it needs no request and follows `size`: the pixel masters are hand-simplified per size, so this
   picks mark-16 / mark-24 / mark-32 rather than scaling one drawing. tone="white" is the one-colour glyph for the purple
   rail and the app-host notch. Ids inside each SVG are renamed per instance, so a hidden copy (the rail drops below
   600 px tall) can never break the gradients of a visible one. */
import { useId, useMemo } from "react";
import mark16 from "../../public/brand/mark-16.svg?raw";
import mark24 from "../../public/brand/mark-24.svg?raw";
import mark32 from "../../public/brand/mark-32.svg?raw";
import white16 from "../../public/brand/glyph-white-16.svg?raw";
import white24 from "../../public/brand/glyph-white-24.svg?raw";
import white32 from "../../public/brand/glyph-white-32.svg?raw";

interface Parsed { viewBox: string; body: string; ids: string[] }
const parse = (src: string): Parsed => {
  const open = /<svg\b[^>]*>/.exec(src)!;
  return {
    viewBox: /viewBox="([^"]+)"/.exec(open[0])![1]!,
    body: src.slice(open.index + open[0].length, src.lastIndexOf("</svg>")),
    ids: [...src.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]!),
  };
};
const COLOR = { 16: parse(mark16), 24: parse(mark24), 32: parse(mark32) };
const WHITE = { 16: parse(white16), 24: parse(white24), 32: parse(white32) };

export function Mark({ size = 28, tone = "color" }: { size?: number; tone?: "color" | "white" }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const master = size <= 16 ? 16 : size <= 24 ? 24 : 32;
  const p = (tone === "white" ? WHITE : COLOR)[master];
  const html = useMemo(() => p.ids.reduce((s, id) => s.replaceAll(`id="${id}"`, `id="n${uid}${id}"`).replaceAll(`url(#${id})`, `url(#n${uid}${id})`), p.body), [p, uid]);
  return <svg width={size} height={size} viewBox={p.viewBox} aria-hidden focusable="false" dangerouslySetInnerHTML={{ __html: html }} />;
}
