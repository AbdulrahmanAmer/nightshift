import { useEffect, useRef, useState, type CSSProperties } from "react";
import { BookOpen, Clapperboard, Ellipsis, LineChart, MessageSquare, Moon, Play, Power, Settings, Sun, Tv, type LucideIcon } from "lucide-react";
import { useApp } from "../lib/app";
import { api } from "../lib/api";
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

/* Geometry, taken from the Planify side rail. Each cap is drawn in a 100x150 box and rendered at 1.5 x the rail width
   (a fixed 2:3 aspect, never stretched); only the flat body between the caps grows. A centred 44 px tile ends at x = 74 px,
   where the cap curve is at about 55% of its height, so the list starts at 58% of the cap. The bottom cap is the same path
   mirrored with scaleY(-1).
   Height = 2 x 0.58 x cap + n x 44 + (n - 1) x gap, where n counts every slot: brand, nav items, theme and quit. */
export const RAIL_W = 104;
export const CAP_PATH = "M0 0 C0 50 26 62 56 74 C88 88 100 108 100 150 L0 150 Z";
const CAP = RAIL_W * 1.5;
const INSET = CAP * 0.58;
const SLOT = 44;
const GAP_MAX = 12;
const GAP_MIN = 6;
const MARGIN = 12;

const capUri = (flip: boolean) => {
  const g = flip ? `<g transform="translate(0 150) scale(1 -1)"><path d="${CAP_PATH}"/></g>` : `<path d="${CAP_PATH}"/>`;
  return `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 150" preserveAspectRatio="none">${g}</svg>`)}")`;
};
const CAP_START = capUri(false);
const CAP_END = capUri(true);

interface Layout { gap: number; brand: boolean; full: boolean; h: number }

/* Fit the rail in the viewport: first shrink the gap (12 down to 6 px), then fold theme and quit into a "more" menu,
   then drop the brand slot. The caps never shrink. Below 600 px tall the CSS switches to the bottom bar instead. */
export function layoutFor(vh: number): Layout {
  const tiers = [{ brand: true, full: true, n: 10 }, { brand: true, full: false, n: 9 }, { brand: false, full: false, n: 8 }];
  const avail = vh - 2 * MARGIN;
  for (let i = 0; i < tiers.length; i++) {
    const t = tiers[i]; const raw = Math.floor((avail - 2 * INSET - t.n * SLOT) / (t.n - 1));
    if (raw >= GAP_MIN || i === tiers.length - 1) {
      const gap = Math.min(GAP_MAX, Math.max(GAP_MIN, raw));
      return { gap, brand: t.brand, full: t.full, h: Math.round(2 * INSET + t.n * SLOT + (t.n - 1) * gap) };
    }
  }
  throw new Error("unreachable");
}

function useRailLayout() {
  const [l, setL] = useState(() => layoutFor(window.innerHeight));
  useEffect(() => {
    const on = () => setL((p) => { const n = layoutFor(window.innerHeight); return n.h === p.h && n.gap === p.gap && n.brand === p.brand && n.full === p.full ? p : n; });
    addEventListener("resize", on);
    return () => removeEventListener("resize", on);
  }, []);
  return l;
}

export function SideTab() {
  const { route, status, theme, setTheme, confirm, toast, refresh } = useApp();
  const l = useRailLayout();
  const [menu, setMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const pending = status?.outbox?.pending ?? 0;
  const idx = NAV.findIndex((n) => n.id === route.name);

  useEffect(() => {
    if (!menu) return;
    const away = (e: PointerEvent) => { if (!menuRef.current?.contains(e.target as Node)) setMenu(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setMenu(false); menuRef.current?.querySelector<HTMLElement>(".sidetab-more")?.focus(); } };
    addEventListener("pointerdown", away); addEventListener("keydown", esc);
    return () => { removeEventListener("pointerdown", away); removeEventListener("keydown", esc); };
  }, [menu]);

  const quit = async () => {
    setMenu(false);
    const ok = await confirm({ title: "Quit Nightshift?", body: "Stops the engine and frees the graphics card.", ok: "Quit", danger: true });
    if (ok !== true) return;
    try { await api.post("/engine/shutdown", { grace_s: 30 }); toast("Shutting down. You can close this tab", "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const flip = () => { setMenu(false); setTheme(theme === "dark" ? "light" : "dark"); };
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  const ThemeIcon = theme === "dark" ? Sun : Moon;
  const items = NAV.map((n) => (n.id === "queue" && pending > 0 ? { ...n, badge: String(pending) } : n));
  const vars = { "--h": `${l.h}px`, "--gap": `${l.gap}px`, "--i": Math.max(idx, 0), "--cap-start": CAP_START, "--cap-end": CAP_END } as CSSProperties;

  return (
    <nav aria-label="Main" className="sidetab" data-brand={l.brand ? "on" : "off"} data-tools={l.full ? "full" : "more"} style={vars}>
      <div className="sidetab-shape" aria-hidden><div className="sidetab-fill" /></div>
      <div className="sidetab-list">
        <a href="#/" className="sidetab-item sidetab-brand" aria-label="Nightshift, Queue"><Mark size={28} tone="white" /></a>

        <div className="sidetab-items">
          <span className="sidetab-cell" data-off={idx < 0 || undefined} aria-hidden><span className="sidetab-square" /></span>
          {items.map((n, i) => {
            const on = i === idx; const I = n.icon;
            return (
              <a key={n.id} href={n.href} aria-current={on ? "page" : undefined} aria-label={n.badge ? `${n.label}, ${n.badge} ready` : n.label} className={cx("sidetab-item", on && "is-on")}>
                <I size={20} strokeWidth={1.75} aria-hidden />
                {n.dot && <span className="sidetab-dot" data-tone={n.dot} aria-hidden />}
                {n.badge && <span key={n.badge} className="sidetab-badge num" aria-hidden>{n.badge}</span>}
                <span className="sidetab-tip" aria-hidden>{n.label}</span>
              </a>
            );
          })}
        </div>

        <div className="sidetab-tools" ref={menuRef}>
          <button type="button" className="sidetab-item sidetab-full" aria-label={themeLabel} onClick={flip}>
            <ThemeIcon size={20} strokeWidth={1.75} aria-hidden />
            <span className="sidetab-tip" aria-hidden>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
          </button>
          <button type="button" className="sidetab-item sidetab-full" aria-label="Quit Nightshift" onClick={quit}>
            <Power size={20} strokeWidth={1.75} aria-hidden />
            <span className="sidetab-tip" aria-hidden>Quit</span>
          </button>
          <button type="button" className="sidetab-item sidetab-more" aria-label="More" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
            <Ellipsis size={20} strokeWidth={1.75} aria-hidden />
            {!menu && <span className="sidetab-tip" aria-hidden>More</span>}
          </button>
          {menu && (
            <div role="menu" aria-label="More" className="sidetab-menu">
              <button type="button" role="menuitem" onClick={flip}><ThemeIcon size={16} strokeWidth={1.75} aria-hidden />{themeLabel}</button>
              <button type="button" role="menuitem" onClick={quit}><Power size={16} strokeWidth={1.75} aria-hidden />Quit Nightshift</button>
            </div>
          )}
        </div>
      </div>
    </nav>
  );
}
