/* SuggestMenu: the "Suggestions" button on the composer row. It opens a popover menu of starter prompts grouped by
   topic (Make, Check, Fix, Learn). Choosing one only fills the composer; it never sends. Keyboard: arrows, Home/End,
   Esc, Tab closes; a click outside closes. Under 640 px the popover becomes a bottom sheet. */
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { cx } from "../ui/kit";
import { Icon } from "../components/ref12";

const GROUPS: { topic: string; items: { prompt: string; hint: string }[] }[] = [
  { topic: "Make", items: [
    { prompt: "Draft 3 story concepts for a series", hint: "Three ideas you can save for later" },
    { prompt: "Start a story video for After Dark Stories", hint: "Proposes a new video for that channel" },
  ] },
  { topic: "Check", items: [
    { prompt: "What is the engine doing?", hint: "Current status of the engine" },
    { prompt: "Where are my finished videos?", hint: "Finds finished videos and their folder" },
  ] },
  { topic: "Fix", items: [
    { prompt: "Pause The Night Desk", hint: "Stops that channel until you resume it" },
    { prompt: "Why did Saturday's run stop?", hint: "Reads the log and explains it" },
  ] },
  { topic: "Learn", items: [
    { prompt: "What can you do for me?", hint: "A short tour of what the assistant can do" },
    { prompt: "How do I post a finished video?", hint: "Walks through review and posting" },
  ] },
];

export function SuggestMenu({ onPick, disabled }: { onPick: (prompt: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null); const btn = useRef<HTMLButtonElement>(null);
  const id = useId();
  const items = () => Array.from(wrap.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]") || []);
  const focusItem = (i: number) => { const els = items(); if (els.length) els[(i + els.length) % els.length]!.focus(); };
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("pointerdown", away); const t = setTimeout(() => focusItem(0), 0);
    return () => { removeEventListener("pointerdown", away); clearTimeout(t); };
  }, [open]);
  const onKey = (e: KeyboardEvent) => {
    const i = items().indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { e.preventDefault(); focusItem(i + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); focusItem(i - 1); }
    else if (e.key === "Home") { e.preventDefault(); focusItem(0); }
    else if (e.key === "End") { e.preventDefault(); focusItem(-1); }
    else if (e.key === "Escape" || e.key === "Tab") { if (e.key === "Escape") e.preventDefault(); setOpen(false); btn.current?.focus(); }
  };
  return (
    <div ref={wrap} className="relative shrink-0" onKeyDown={open ? onKey : undefined}>
      <button ref={btn} type="button" disabled={disabled} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} aria-label="Suggestions" title="Suggestions"
        onClick={() => setOpen((o) => !o)}
        className="ds-focus inline-flex h-9 items-center justify-center gap-2 rounded-ds-sm bg-transparent px-2.5 text-ds-sm font-semibold text-ds-text-2 transition-colors duration-[120ms] hover:bg-ds-hover hover:text-ds-text disabled:opacity-50">
        <Icon name="lightbulb" size={18} /><span className="hidden sm:inline">Suggestions</span>
      </button>
      {open && (
        <div id={id} role="menu" aria-label="Suggestions"
          className={cx("fade-in z-40 flex flex-col overflow-y-auto overscroll-contain rounded-ds-md bg-ds-surface p-1 scroll-thin shadow-[var(--ds-shadow-3),0_0_0_1px_var(--ds-border-subtle)]",
            "fixed inset-x-2 bottom-[calc(72px+env(safe-area-inset-bottom))] max-h-[min(70dvh,calc(100dvh-160px))] rounded-ds-lg sm:absolute sm:inset-x-auto sm:bottom-full sm:left-0 sm:mb-2 sm:max-h-[min(420px,60vh)] sm:w-[360px] sm:max-w-[calc(100vw-2rem)]")}>
          {GROUPS.map((g) => (
            <div key={g.topic} role="group" aria-label={g.topic} className="flex flex-col">
              <div aria-hidden className="px-3 pt-2 pb-1 text-ds-2xs font-semibold text-ds-muted">{g.topic}</div>
              {g.items.map((it) => (
                <button key={it.prompt} type="button" role="menuitem" tabIndex={-1} onClick={() => { setOpen(false); onPick(it.prompt); }}
                  className="flex flex-col items-start gap-0.5 rounded-ds-sm px-3 py-2 text-left outline-none hover:bg-ds-hover focus-visible:bg-ds-hover">
                  <span className="text-ds-sm font-medium text-ds-text">{it.prompt}</span>
                  <span className="text-ds-xs text-ds-muted">{it.hint}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
