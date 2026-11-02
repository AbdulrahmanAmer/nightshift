import { useEffect, useState, type CSSProperties } from "react";
import { BookOpen, Clapperboard, LineChart, MessageSquare, MoreHorizontal, Play, Settings, Tv, type LucideIcon } from "lucide-react";
import { useApp } from "../lib/app";
import { cx } from "../ui/kit";
import { Mark } from "../ui/Mark";

export interface RailItem {
  id: string; href: string; label: string; icon: LucideIcon;
  /** Pill next to the tile, e.g. "5". */
  badge?: string;
  /** Status dot on the tile corner. */
  dot?: "success" | "attention";
}

export const NAV: RailItem[] = [
  { id: "queue", href: "#/", label: "Queue", icon: Play },
  { id: "create", href: "#/create", label: "Create", icon: Clapperboard },
  { id: "library", href: "#/library", label: "Library", icon: BookOpen },
  { id: "assistant", href: "#/assistant", label: "Assistant", icon: MessageSquare },
  { id: "channels", href: "#/channels", label: "Channels", icon: Tv },
  { id: "results", href: "#/results", label: "Results", icon: LineChart },
  { id: "settings", href: "#/settings", label: "Settings", icon: Settings },
];

/* Geometry. Each cap is drawn in a 100x120 box and rendered at the rail width (so 1.2 x the width tall, never stretched);
   only the flat body between the caps grows. The curve is the Planify wave with its control points pulled in by about 35%,
   so the right-hand shoulders read as a gentle bevel. A centred 40 px tile ends at x = 64 px (73% of 88), where the cap
   curve is at about 57% of its height, so the list starts at 58% of the cap. The bottom cap is the same path mirrored.
   Height = 2 x 0.58 x cap + n x 40 + (n - 1) x gap, where n counts the brand slot and the seven pages. Theme and Quit
   live in the top bar. */
export const RAIL_W = 88;
export const CAP_PATH = "M0 0 C0 36 22 48 50 58 C80 68 100 84 100 120 L0 120 Z";
const CAP = RAIL_W * 1.2;
const INSET = CAP * 0.58;
const SLOT = 40;
const GAP_MAX = 12;
const GAP_MIN = 6;
const MARGIN = 12;

const capUri = (flip: boolean) => {
  const g = flip ? `<g transform="translate(0 120) scale(1 -1)"><path d="${CAP_PATH}"/></g>` : `<path d="${CAP_PATH}"/>`;
  return `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 120" preserveAspectRatio="none">${g}</svg>`)}")`;
};
const CAP_START = capUri(false);
const CAP_END = capUri(true);

interface Layout { gap: number; brand: boolean; h: number; more: boolean }
/** With "more", only the first KEEP pages stay on the rail; the rest move into the More menu. */
const KEEP = 5;

/* Fit the rail in the viewport: shrink the gap (12 down to 6 px), then drop the brand slot. The caps never shrink.
   If even that is too tall, the last pages fold into a More menu. The bottom bar is for narrow widths only (below md). */
export function layoutFor(vh: number, withBrand = true): Layout {
  const tiers = [...(withBrand ? [{ brand: true, n: 8, more: false }] : []), { brand: false, n: 7, more: false }, { brand: false, n: KEEP + 1, more: true }];
  const avail = vh - 2 * MARGIN;
  for (let i = 0; i < tiers.length; i++) {
    const t = tiers[i]!; const raw = Math.floor((avail - 2 * INSET - t.n * SLOT) / (t.n - 1));
    if (raw >= GAP_MIN || i === tiers.length - 1) {
      const gap = Math.min(GAP_MAX, Math.max(GAP_MIN, raw));
      return { gap, brand: t.brand, more: t.more, h: Math.round(2 * INSET + t.n * SLOT + (t.n - 1) * gap) };
    }
  }
  throw new Error("unreachable");
}

function useRailLayout() {
  const [l, setL] = useState(() => layoutFor(window.innerHeight));
  useEffect(() => {
    const on = () => setL((p) => { const n = layoutFor(window.innerHeight); return n.h === p.h && n.gap === p.gap && n.brand === p.brand && n.more === p.more ? p : n; });
    addEventListener("resize", on);
    return () => removeEventListener("resize", on);
  }, []);
  return l;
}

/** `brand={false}` drops the brand slot (in the app host the mark sits in the top-left window notch). */
export function SideTab({ brand = true }: { brand?: boolean }) {
  const { route, status } = useApp();
  const l0 = useRailLayout();
  const l = brand ? l0 : { ...l0, ...layoutFor(window.innerHeight, false) };
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    if (!menu) return;
    const away = (e: PointerEvent) => { if (!(e.target as Element).closest?.(".sidetab-menu, .sidetab-more")) setMenu(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    addEventListener("pointerdown", away); addEventListener("keydown", esc);
    return () => { removeEventListener("pointerdown", away); removeEventListener("keydown", esc); };
  }, [menu]);
  useEffect(() => { setMenu(false); }, [route.name]);
  const pending = status?.outbox?.pending ?? 0;
  const idx = NAV.findIndex((n) => n.id === route.name);
  const items = NAV.map((n) => (n.id === "queue" && pending > 0 ? { ...n, badge: String(pending) } : n));
  const vars = { "--h": `${l.h}px`, "--gap": `${l.gap}px`, "--i": l.more && idx >= KEEP ? KEEP : Math.max(idx, 0), "--cap-start": CAP_START, "--cap-end": CAP_END } as CSSProperties;

  return (
    <nav aria-label="Main" className="sidetab" data-brand={l.brand ? "on" : "off"} data-more={l.more ? "on" : "off"} style={vars}>
      <div className="sidetab-shape" aria-hidden><div className="sidetab-fill" /></div>
      <div className="sidetab-list">
        <a href="#/" className="sidetab-item sidetab-brand" aria-label="Nightshift, Queue"><Mark size={32} tone="white" /></a>
        <div className="sidetab-items">
          <span className="sidetab-cell" data-off={idx < 0 || undefined} aria-hidden><span className="sidetab-square" /></span>
          {items.map((n, i) => {
            const on = i === idx; const I = n.icon;
            return (
              <a key={n.id} href={n.href} aria-current={on ? "page" : undefined} aria-label={n.badge ? `${n.label}, ${n.badge} ready` : n.label} className={cx("sidetab-item", on && "is-on", i >= KEEP && "sidetab-fold")}>
                <I size={18} strokeWidth={1.75} aria-hidden />
                {n.dot && <span className="sidetab-dot" data-tone={n.dot} aria-hidden />}
                {n.badge && <span key={n.badge} className="sidetab-badge num" aria-hidden>{n.badge}</span>}
                <span className="sidetab-tip" aria-hidden>{n.label}</span>
              </a>
            );
          })}
          <button type="button" className={cx("sidetab-item sidetab-more", idx >= KEEP && "is-on")} aria-label="More pages" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
            <MoreHorizontal size={18} strokeWidth={1.75} aria-hidden />
          </button>
        </div>
      </div>
      {l.more && menu && (
        <div className="sidetab-menu" role="menu" aria-label="More pages">
          {items.slice(KEEP).map((n) => { const I = n.icon; return (
            <button key={n.id} type="button" role="menuitem" onClick={() => { setMenu(false); location.hash = n.href; }}><I size={16} strokeWidth={1.75} aria-hidden />{n.label}</button>
          ); })}
        </div>
      )}
    </nav>
  );
}
