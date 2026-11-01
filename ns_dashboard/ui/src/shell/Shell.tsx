import { useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Clapperboard, LineChart, MessageSquare, Moon, Play, Power, Settings, Sun, Tv, X } from "lucide-react";
import { useApp } from "../lib/app";
import { api } from "../lib/api";
import { fmtWhen } from "../lib/format";
import type { Status } from "../lib/types";
import { Button, cx, IconButton } from "../ui/kit";
import { Chat } from "../pages/Assistant";
import { Mark } from "../ui/Mark";
export { Mark };

const NAV = [
  { id: "queue", href: "#/", label: "Queue", icon: Play },
  { id: "create", href: "#/create", label: "Create", icon: Clapperboard },
  { id: "library", href: "#/library", label: "Library", icon: BookOpen },
  { id: "assistant", href: "#/assistant", label: "Assistant", icon: MessageSquare },
  { id: "channels", href: "#/channels", label: "Channels", icon: Tv },
  { id: "results", href: "#/results", label: "Results", icon: LineChart },
  { id: "settings", href: "#/settings", label: "Settings", icon: Settings },
];

function Rail() {
  const { route, status } = useApp();
  const pending = status?.outbox?.pending ?? 0;
  return (
    <nav aria-label="Main" className="card fixed inset-x-3 bottom-3 z-30 flex items-center justify-between rounded-[24px] px-2 py-1.5 md:sticky md:inset-auto md:top-4 md:bottom-auto md:w-[84px] md:flex-col md:justify-start md:gap-0.5 md:self-start md:px-0 md:py-4">
      <a href="#/" className="mb-3 hidden md:block" aria-label="Nightshift, Queue"><Mark size={36} /></a>
      {NAV.map((n) => {
        const on = route.name === n.id; const I = n.icon;
        return (
          <a key={n.id} href={n.href} aria-current={on ? "page" : undefined} className="group relative flex flex-col items-center gap-0.5 px-1 py-1 no-underline md:w-full">
            <span className={cx("relative flex size-10 items-center justify-center rounded-full transition-colors duration-150", on ? "bg-fill text-on-fill" : "text-text-2 group-hover:bg-raised group-hover:text-text")}>
              <I size={18} strokeWidth={1.5} aria-hidden />
              {n.id === "queue" && pending > 0 && <span className="absolute -top-0.5 -right-1 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent px-1 font-mono text-[11px] leading-none text-on-accent num" aria-label={`${pending} ready`}>{pending}</span>}
            </span>
            <span className={cx("hidden text-[12px] leading-4 md:block", on ? "text-text" : "text-muted group-hover:text-text-2")}>{n.label}</span>
          </a>
        );
      })}
    </nav>
  );
}

export function engineText(s: Status | null) {
  const e = s?.engine; if (!e) return { text: "Connecting", tone: "muted" as const };
  const clean = (t?: string) => (t || "").replace(/\.{3}|…/g, "").trim();
  if (e.state === "loading") {
    const svc = e.service && s?.services?.[e.service]; const gb = e.vram_gb ?? (svc && svc.vram_mb ? Math.round(svc.vram_mb / 1024) : null);
    return { text: `Loading model${gb ? ` ${gb} GB` : ""}${e.elapsed_s != null ? ` · ${e.elapsed_s} s` : ""}`, tone: "accent" as const };
  }
  if (e.state === "working") return { text: `Working: ${clean(e.detail) || "Working"}${e.step && e.steps ? `, step ${e.step} of ${e.steps}` : ""}`, tone: "accent" as const, live: true };
  if (e.state === "ready") return { text: "Ready", tone: "ok" as const };
  if (e.state === "starting") return { text: "Starting", tone: "accent" as const };
  if (e.state === "error") return { text: clean(e.detail) || "Engine problem", tone: "bad" as const };
  return { text: clean(e.detail) === "Engine stopped" ? "Engine stopped" : "Idle", tone: "muted" as const };
}

function Strip() {
  const { status, demo, ask, toast, refresh, theme, setTheme } = useApp();
  const [quitting, setQuitting] = useState(false);
  const eng = engineText(status);
  const dot = { muted: "bg-muted", accent: "bg-accent", ok: "bg-ok", bad: "bg-bad" }[eng.tone];
  const quit = async () => {
    setQuitting(false);
    try { await api.post("/engine/shutdown", { grace_s: 30 }); toast("Shutting down. You can close this tab", "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); }
  };
  return (
    <header aria-label="Engine status" className="card flex h-14 items-center gap-3 rounded-full py-2 pr-2 pl-3">
      <span role="status" aria-live="polite" className="inline-flex h-10 min-w-0 items-center gap-2 rounded-full bg-raised px-3.5 text-[13px]">
        <span className="relative flex size-2 shrink-0">{eng.live && <span className="absolute inset-0 animate-ping rounded-full bg-accent opacity-60" />}<span className={cx("relative size-2 rounded-full", dot)} /></span>
        <span className="truncate font-medium">{eng.text}</span>
      </span>
      <span className="hidden items-center gap-1.5 text-[13px] whitespace-nowrap text-text-2 lg:inline-flex">
        <Moon size={14} strokeWidth={1.5} className="text-muted" aria-hidden />
        {status?.next_run ? <>Next automatic run <span className="font-mono num text-text">{fmtWhen(status.next_run)}</span></> : "Automatic runs off"}
      </span>
      {demo && <span title="No engine answered, so this is sample data" className="hidden text-[12px] whitespace-nowrap text-muted xl:inline">Demo data</span>}
      <span className="flex-1" />
      {quitting ? (
        <span className="fade-in flex items-center gap-2 text-[13px]">
          <span className="hidden text-text-2 lg:inline">Stop the engine and free the graphics card?</span>
          <Button size="sm" variant="ghost" onClick={() => setQuitting(false)}>Keep running</Button>
          <Button size="sm" variant="danger" onClick={quit} autoFocus>Quit</Button>
        </span>
      ) : (
        <>
          <Button size="sm" variant="secondary" className="h-10 px-4 max-sm:px-3" icon={<MessageSquare size={16} strokeWidth={1.5} aria-hidden />} onClick={() => ask({})} aria-label="Ask Nightshift"><span className="hidden sm:inline">Ask Nightshift</span></Button>
          <IconButton label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"} className="size-10" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? <Sun size={18} strokeWidth={1.5} /> : <Moon size={18} strokeWidth={1.5} />}
          </IconButton>
          <Button size="sm" variant="ghost" className="h-10 max-sm:px-3" icon={<Power size={16} strokeWidth={1.5} aria-hidden />} onClick={() => setQuitting(true)} aria-label="Quit Nightshift"><span className="hidden sm:inline">Quit</span></Button>
        </>
      )}
    </header>
  );
}

function Toasts() {
  const { toasts } = useApp();
  return (
    <div aria-live="polite" className="pointer-events-none fixed right-4 bottom-24 z-50 flex flex-col items-end gap-2 md:bottom-4">
      {toasts.map((t) => (
        <div key={t.id} className="enter pointer-events-auto flex max-w-sm items-center gap-2 rounded-[14px] border border-line bg-card px-4 py-3 text-[13px] shadow-float">
          <span className={cx("size-2 shrink-0 rounded-full", t.tone === "ok" ? "bg-ok" : t.tone === "bad" ? "bg-bad" : t.tone === "warn" ? "bg-accent" : "bg-muted")} />{t.text}
        </div>
      ))}
    </div>
  );
}

function ConfirmDialog() {
  const { confirmReq: c, closeConfirm } = useApp();
  const [val, setVal] = useState(""); const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { setVal(""); if (c) setTimeout(() => (ref.current?.querySelector("input,button[data-ok]") as HTMLElement | null)?.focus(), 30); }, [c]);
  if (!c) return null;
  const ok = () => closeConfirm(c.input ? val : true);
  return (
    <div className="fade-in fixed inset-0 z-50 flex items-center justify-center bg-[rgba(8,12,24,0.35)] p-4" onMouseDown={(e) => e.target === e.currentTarget && closeConfirm(null)} onKeyDown={(e) => { if (e.key === "Escape") closeConfirm(null); if (e.key === "Enter" && c.input) ok(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby="dlg-t" className="enter card w-full max-w-md p-6">
        <h2 id="dlg-t" className="text-[17px] font-medium">{c.title}</h2>
        {c.body && <p className="mt-2 text-[14px] text-text-2">{c.body}</p>}
        {c.input && <div className="mt-4"><label className="mb-1.5 block text-[13px] font-medium text-text-2" htmlFor="dlg-in">{c.input.label}</label><input id="dlg-in" className="field-input" value={val} placeholder={c.input.placeholder} onChange={(e) => setVal(e.target.value)} /></div>}
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => closeConfirm(null)}>Cancel</Button>
          <Button data-ok variant={c.danger ? "danger" : "primary"} onClick={ok}>{c.ok || "OK"}</Button>
        </div>
      </div>
    </div>
  );
}

function AskDrawer() {
  const { askOpen, setAskOpen, route } = useApp();
  useEffect(() => { if (route.name === "assistant") setAskOpen(false); }, [route.name, setAskOpen]);
  useEffect(() => { if (!askOpen) return; const on = (e: KeyboardEvent) => e.key === "Escape" && setAskOpen(false); addEventListener("keydown", on); return () => removeEventListener("keydown", on); }, [askOpen, setAskOpen]);
  if (!askOpen) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-[rgba(8,12,24,0.25)]" onMouseDown={(e) => e.target === e.currentTarget && setAskOpen(false)}>
      <aside aria-label="Ask Nightshift" className="enter card flex w-full flex-col overflow-hidden max-sm:rounded-none max-sm:border-0 sm:m-3 sm:max-w-[460px]">
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <div className="flex items-center gap-2.5"><Mark size={24} /><span className="text-[15px] font-medium">Ask Nightshift</span></div>
          <div className="flex items-center gap-1"><a href="#/assistant" className="rounded-full px-3 py-1.5 text-[13px] text-text-2 no-underline hover:bg-raised">Open full page</a><IconButton label="Close" onClick={() => setAskOpen(false)}><X size={18} strokeWidth={1.5} /></IconButton></div>
        </div>
        <Chat compact />
      </aside>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto flex max-w-[1600px] gap-4 p-3 pb-24 md:p-4">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-full focus:bg-card focus:px-4 focus:py-2">Skip to content</a>
      <Rail />
      <div className="flex min-w-0 flex-1 flex-col gap-5">
        <Strip />
        <main id="main" tabIndex={-1} className="min-w-0 outline-none">{children}</main>
      </div>
      <Toasts />
      <ConfirmDialog />
      <AskDrawer />
    </div>
  );
}
