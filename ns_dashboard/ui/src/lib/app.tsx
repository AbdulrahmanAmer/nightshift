import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { store } from "./format";
import type { Status } from "./types";

export type Toast = { id: number; text: string; tone?: "ok" | "bad" | "warn" };
export type ConfirmReq = { title: string; body?: string; ok?: string; danger?: boolean; input?: { label: string; placeholder?: string }; resolve: (v: string | boolean | null) => void };

interface AppCtx {
  status: Status | null; demo: boolean; refresh: () => Promise<void>;
  toast: (text: string, tone?: Toast["tone"]) => void; toasts: Toast[];
  confirm: (r: Omit<ConfirmReq, "resolve">) => Promise<string | boolean | null>; confirmReq: ConfirmReq | null; closeConfirm: (v: string | boolean | null) => void;
  route: { name: string; query: URLSearchParams }; go: (hash: string) => void;
  askOpen: boolean; setAskOpen: (v: boolean) => void; askContext: Record<string, unknown>; ask: (ctx?: Record<string, unknown>, prompt?: string) => void; askPrompt: string | null; clearAskPrompt: () => void;
  theme: "light" | "dark"; setTheme: (t: "light" | "dark") => void;
  channelName: (slug?: string) => string;
}
const Ctx = createContext<AppCtx>(null as unknown as AppCtx);
export const useApp = () => useContext(Ctx);

const ROUTES: Record<string, string> = { "": "queue", queue: "queue", create: "create", library: "library", assistant: "assistant", channels: "channels", results: "results", settings: "settings" };
const parseHash = () => { const [n, q] = location.hash.replace(/^#\/?/, "").split("?"); return { name: ROUTES[n] ?? "queue", query: new URLSearchParams(q || "") }; };

export function AppProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [demo, setDemo] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmReq, setConfirmReq] = useState<ConfirmReq | null>(null);
  const [route, setRoute] = useState(parseHash);
  const [askOpen, setAskOpen] = useState(false);
  const [askContext, setAskContext] = useState<Record<string, unknown>>({});
  const [askPrompt, setAskPrompt] = useState<string | null>(null);
  const [theme, setThemeState] = useState<"light" | "dark">(() => store.get<"light" | "dark" | null>("ns-theme", null) ?? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  const tid = useRef(0);

  const lastStatus = useRef("");
  const refresh = useCallback(async () => {
    try {
      const s = await api.status(); const key = JSON.stringify(s);
      // Only publish a new status when something changed, so the 1.5 s demo poll does not re-render every page.
      if (key !== lastStatus.current) { lastStatus.current = key; setStatus(s); }
      setDemo(api.demo);
    } catch { /* keep last status */ }
  }, []);
  const toast = useCallback((text: string, tone?: Toast["tone"]) => {
    const id = ++tid.current; setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);
  const confirm = useCallback((r: Omit<ConfirmReq, "resolve">) => new Promise<string | boolean | null>((resolve) => setConfirmReq({ ...r, resolve })), []);
  const closeConfirm = useCallback((v: string | boolean | null) => { setConfirmReq((c) => { c?.resolve(v); return null; }); }, []);
  const go = useCallback((hash: string) => { location.hash = hash; }, []);
  const ask = useCallback((ctx?: Record<string, unknown>, prompt?: string) => { setAskContext(ctx || {}); if (prompt) setAskPrompt(prompt); setAskOpen(true); }, []);
  const setTheme = useCallback((t: "light" | "dark") => { setThemeState(t); store.set("ns-theme", t); }, []);
  const channelName = useCallback((slug?: string) => status?.channels?.find((c) => c.slug === slug)?.display_name || slug || "", [status]);

  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => { const on = () => setRoute(parseHash()); addEventListener("hashchange", on); return () => removeEventListener("hashchange", on); }, []);
  useEffect(() => {
    let es: EventSource | null = null; let timer: number;
    refresh().then(() => {
      if (!api.demo && "EventSource" in window) {
        try { es = new EventSource("/api/events"); es.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (m.kind === "status") setStatus(m.payload); } catch { /* ignore */ } }; } catch { /* polling covers it */ }
      }
      timer = window.setInterval(() => { if (document.visibilityState === "visible") refresh(); }, api.demo ? 1500 : 10000);
    });
    return () => { es?.close(); clearInterval(timer); };
  }, [refresh]);

  const value = useMemo<AppCtx>(() => ({ status, demo, refresh, toast, toasts, confirm, confirmReq, closeConfirm, route, go, askOpen, setAskOpen, askContext, ask, askPrompt, clearAskPrompt: () => setAskPrompt(null), theme, setTheme, channelName }),
    [status, demo, refresh, toast, toasts, confirm, confirmReq, closeConfirm, route, go, askOpen, askContext, ask, askPrompt, theme, setTheme, channelName]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Fetch helper for pages: loads on mount and whenever deps change; reload() re-runs it. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], fallback: T) {
  const [data, setData] = useState<T>(fallback);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true; setLoading(true);
    fn().then((d) => { if (live) { setData(d); setError(null); } }).catch((e) => { if (live) setError(e.message); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return { data, setData, loading, error, reload: () => setN((x) => x + 1) };
}
