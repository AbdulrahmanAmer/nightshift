/* Assistant: the chat in a tall card, with tool proposals as MeetingCard-style action cards, and a side column of
   recent runs (NotificationItems) and what it can do. The chat protocol is unchanged: api.chat events, confirm and
   dismiss calls, the stored conversation (ns-chat2) and the prompt handed over by "Ask about it" and the top bar search. */
import { useEffect, useRef, useState, type RefObject } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtDay, fmtClock, stageWords, stoppedAt, store } from "../lib/format";
import type { Job, ToolInfo } from "../lib/types";
import { cx } from "../ui/kit";
import { Mark } from "../ui/Mark";
import { Lockup } from "../ui/Lockup";
import { Badge, Button, Card, Icon, IconButton, MeetingCard, MetaItem, NotificationItem, SegmentedControl, StatusDot, type IconName, type MenuItem } from "../components/ref12";
import { SuggestMenu } from "./SuggestMenu";
import { jobTitle } from "./Queue";

type Confirm = { action_id: string; text: string; tool: string; state: "pending" | "done" | "dismissed" | "failed"; result?: string };
/** A story draft file the Assistant writes part by part (WP7b, G-47): GET /drafts/{id} answers the same shape. */
type DraftInfo = { draft_id: string; path: string; title: string; words: number; status: string; parts: { n: number; title: string; words: number }[] | number; job_id?: string; warning?: string };
type Msg = { id: number; role: "user" | "assistant" | "note" | "error"; text: string; confirm?: Confirm; draft?: DraftInfo };
/** The tools whose result carries a `draft`: their result shows as the draft's card (one card per draft, the newest state). */
const DRAFT_TOOLS = ["draft_create", "draft_append_part", "draft_read"];
const draftOf = (tool: string, ok: boolean, result: any): DraftInfo | null => (ok && DRAFT_TOOLS.includes(tool) && result?.draft?.path ? (result.draft as DraftInfo) : null);
/** One card per draft: a newer state of the same draft replaces the old card and moves to the end. */
const withDraft = (msgs: Msg[], d: DraftInfo): Msg[] => [...msgs.filter((m) => m.draft?.draft_id !== d.draft_id), { id: ++mid, role: "assistant", text: "", draft: d }];
type Saved = { cid: string | null; msgs: Msg[] };

/** Registry entry of one tool (GET /assistant/tools). The page words its busy line and Yes/No cards from these, never from a list of its own. */
const firstSentence = (t: string) => t.split(/(?<=[.!?])\s/)[0]!.replace(/[.!?]+$/, "");
const toolWords = (tools: ToolInfo[], name: string) => { const d = tools.find((t) => t.name === name)?.description; return d ? firstSentence(d) : name.replace(/_/g, " "); };
const WELCOME: Msg = { id: 0, role: "assistant", text: "I run on this PC with the local model. I can start videos or clips, draft story concepts, pause or resume channels, find files and explain results. Anything that changes something shows you a Yes or No card first." };
let mid = Date.now();

/** Messages of a stored conversation (GET /assistant/history) as chat messages; actions still waiting come back as cards. */
const fromHistory = (h: { messages?: { role: string; content?: string; name?: string }[]; pending_actions?: { action_id: string; tool: string; confirm_text: string }[] }): Msg[] => {
  let out: Msg[] = [];
  for (const m of h.messages || []) {
    if ((m.role === "user" || m.role === "assistant") && m.content?.trim()) out.push({ id: ++mid, role: m.role, text: m.content });
    else if (m.role === "tool" && m.name && DRAFT_TOOLS.includes(m.name)) {
      let d: DraftInfo | null = null; try { d = draftOf(m.name, true, JSON.parse(m.content || "null")); } catch { /* a result too long to store whole: no card */ }
      if (d) out = withDraft(out, d);
    }
  }
  for (const a of h.pending_actions || []) out.push({ id: ++mid, role: "assistant", text: "", confirm: { action_id: a.action_id, text: a.confirm_text, tool: a.tool, state: "pending" } });
  return out.length ? out : [WELCOME];
};
type Conv = { conversation_id: string; title?: string | null; updated_at: string; queued?: number };

export function Chat({ compact, initialText, openCid, openTick }: { compact?: boolean; initialText?: string; openCid?: string | null; openTick?: number }) {
  const { askContext, askPrompt, clearAskPrompt, refresh, route, status } = useApp();
  // Set when the engine was busy and the request was saved: this chat then asks for the answer until it arrives.
  const [waiting, setWaiting] = useState(false);
  const [saved, setSaved] = useState<Saved>(() => store.get<Saved>("ns-chat2", { cid: null, msgs: [WELCOME] }));
  const [text, setText] = useState(initialText ?? "");
  const [busy, setBusy] = useState(false);
  const [toolNote, setToolNote] = useState<string | null>(null);
  const tools = useLoad<ToolInfo[]>(() => api.get("/assistant/tools"), [], []);
  const logRef = useRef<HTMLDivElement>(null); const taRef = useRef<HTMLTextAreaElement>(null);
  const savedRef = useRef(saved); savedRef.current = saved;

  const update = (fn: (s: Saved) => Saved) => setSaved((s) => { const n = fn(s); store.set("ns-chat2", { cid: n.cid, msgs: n.msgs.slice(-80) }); return n; });
  const push = (m: Omit<Msg, "id">) => update((s) => ({ ...s, msgs: [...s.msgs, { ...m, id: ++mid }] }));
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" }); }, [saved.msgs.length, busy, toolNote]);

  const ctx = () => {
    const q = route.query; const c: Record<string, unknown> = { page: route.name, ...askContext };
    if (q.get("about")) c.output_id = q.get("about"); if (q.get("idea")) c.idea_id = q.get("idea"); if (q.get("story")) c.story_id = q.get("story");
    return c;
  };
  const send = async (raw: string) => {
    const msg = raw.trim(); if (!msg || busy) return;
    setText(""); push({ role: "user", text: msg }); setBusy(true);
    try {
      for await (const ev of api.chat(msg, savedRef.current.cid, ctx())) {
        if (ev.kind === "conversation") update((s) => ({ ...s, cid: ev.payload.conversation_id }));
        else if (ev.kind === "message") { setToolNote(null); push({ role: "assistant", text: ev.payload.text }); }
        else if (ev.kind === "confirm") push({ role: "assistant", text: "", confirm: { action_id: ev.payload.action_id, text: ev.payload.text, tool: ev.payload.tool, state: "pending" } });
        else if (ev.kind === "tool_call") setToolNote(toolWords(tools.data, ev.payload.tool));
        else if (ev.kind === "tool_result") { const d = draftOf(ev.payload.tool, ev.payload.ok, ev.payload.result); if (d) update((s) => ({ ...s, msgs: withDraft(s.msgs, d) })); }
        else if (ev.kind === "busy") { push({ role: "note", text: ev.payload.message }); setWaiting(true); }
        else if (ev.kind === "error") push({ role: "error", text: ev.payload.message + (ev.payload.hint ? `. ${ev.payload.hint}` : "") });
      }
    } catch (e: any) { push({ role: "error", text: `I could not answer: ${e.message}` }); }
    setBusy(false); setToolNote(null); taRef.current?.focus();
  };
  // Prompt handed over by "Ask about it" or the top bar search. Runs only when a prompt arrives (it used to run after every render).
  useEffect(() => { if (askPrompt) { send(askPrompt); clearAskPrompt(); } }, [askPrompt]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { taRef.current?.focus(); }, []);
  // A saved request is answered by the dashboard itself (when a job ends, and every 30 s while it is idle). The chat only looks:
  // it never starts the answer, so a slow answer cannot be started twice. The answer shows in THIS conversation (saved.cid).
  useEffect(() => {
    const cid = saved.cid; if (!waiting || !cid) return; let live = true;
    const look = async () => {
      try {
        const convs = await api.get<{ conversation_id: string; queued?: number }[]>("/assistant/conversations");
        if (convs.find((c) => c.conversation_id === cid)?.queued) return;
        const h = await api.get(`/assistant/history?conversation_id=${encodeURIComponent(cid)}`);
        if (live) { update(() => ({ cid, msgs: fromHistory(h) })); setWaiting(false); }
      } catch { /* try again on the next tick */ }
    };
    const t = setInterval(look, 6000); look();
    return () => { live = false; clearInterval(t); };
  }, [waiting, saved.cid, status?.engine?.state]); // eslint-disable-line react-hooks/exhaustive-deps
  // A past conversation picked in the list (or refreshed after saved requests were answered): load its transcript.
  useEffect(() => {
    if (!openCid) return;
    api.get(`/assistant/history?conversation_id=${encodeURIComponent(openCid)}`).then((h) => update(() => ({ cid: openCid, msgs: fromHistory(h) }))).catch((e: any) => push({ role: "error", text: `I could not open that conversation: ${e.message}` }));
  }, [openCid, openTick]); // eslint-disable-line react-hooks/exhaustive-deps

  const decide = async (m: Msg, yes: boolean) => {
    if (!m.confirm) return;
    const setState = (state: Confirm["state"], result?: string) => update((s) => ({ ...s, msgs: s.msgs.map((x) => (x.id === m.id && x.confirm ? { ...x, confirm: { ...x.confirm, state, result } } : x)) }));
    try {
      if (yes) { const r = await api.post("/assistant/confirm", { action_id: m.confirm.action_id }); setState(r?.status === "failed" ? "failed" : "done", r?.message); refresh(); }
      else { await api.post("/assistant/dismiss", { action_id: m.confirm.action_id }).catch(() => null); setState("dismissed"); }
    } catch (e: any) { setState("failed", e.message); }
  };
  const reset = () => update(() => ({ cid: null, msgs: [WELCOME] }));

  return (
    <div className={cx("flex min-h-0 flex-1 flex-col", !compact && "h-full")}>
      <div ref={logRef} aria-live="polite" className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4 scroll-thin">
        {!compact && saved.msgs.length <= 1 && !busy && <div className="flex justify-center pt-4 pb-2"><Lockup width={220} /></div>}
        {saved.msgs.map((m) => m.draft ? <DraftCard key={m.id} d={m.draft} /> : m.confirm ? <ConfirmCard key={m.id} c={m.confirm} what={toolWords(tools.data, m.confirm.tool)} onYes={() => decide(m, true)} onNo={() => decide(m, false)} /> : (
          <div key={m.id} className={cx("flex gap-3", m.role === "user" && "justify-end")}>
            {m.role !== "user" && m.role !== "note" && <span className="mt-0.5 shrink-0"><Mark size={28} /></span>}
            <div className={cx("max-w-[78%] rounded-ds-lg px-3 py-2 text-ds-md leading-ds-normal whitespace-pre-wrap",
              m.role === "user" ? "rounded-br-ds-xs bg-ds-accent text-ds-on-accent" : m.role === "note" ? "mx-auto bg-transparent text-center text-ds-2xs text-ds-muted" : m.role === "error" ? "rounded-tl-ds-xs bg-ds-danger-soft text-ds-text" : "rounded-tl-ds-xs bg-ds-sunken text-ds-text shadow-ds-inset-sm")}>{m.text}</div>
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-3">
            <Mark size={28} />
            <span className="flex items-center gap-2 rounded-ds-lg rounded-tl-ds-xs bg-ds-sunken px-4 py-3 text-ds-sm text-ds-text-2">
              <span className="flex gap-1" aria-hidden>{[0, 1, 2].map((i) => <i key={i} className="size-1.5 rounded-full bg-ds-text-2" style={{ animation: `typing 1.2s ${i * 0.15}s infinite` }} />)}</span>
              {toolNote && <span>{toolNote}</span>}<span className="sr-only">Thinking</span>
            </span>
          </div>
        )}
      </div>
      <div className="border-t border-ds-border-subtle px-4 pt-3 pb-4">
        <form onSubmit={(e) => { e.preventDefault(); send(text); }} className="flex items-end gap-2 rounded-ds-lg border border-ds-border bg-ds-surface p-1.5 pl-1.5 shadow-ds-inset-sm transition-[border-color,box-shadow] duration-[120ms] focus-within:border-ds-focus focus-within:shadow-ds-focus">
          <SuggestMenu disabled={busy} onPick={(p) => { setText(p); requestAnimationFrame(() => taRef.current?.focus()); }} />
          <label htmlFor={compact ? "ask-text" : "chat-text"} className="sr-only">Message</label>
          <textarea id={compact ? "ask-text" : "chat-text"} ref={taRef} rows={1} value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask or tell Nightshift"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text); } }}
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent py-2 text-ds-md leading-5 text-ds-text outline-none placeholder:text-ds-muted" />
          <IconButton type="submit" icon="arrowUp" label="Send" variant="accent" disabled={busy || !text.trim()} className="disabled:opacity-40" />
        </form>
        <div className="mt-2 flex items-center justify-between px-1 text-ds-2xs text-ds-muted"><span>Enter to send, Shift+Enter for a new line</span><button type="button" onClick={reset} className="ds-focus rounded-ds-xs font-semibold hover:text-ds-text">New conversation</button></div>
      </div>
    </div>
  );
}


/** A tool proposal: the MeetingCard recipe (title, a when-line with the accent dot, the text, No and Yes). */
function ConfirmCard({ c, what, onYes, onNo }: { c: Confirm; what: string; onYes: () => void; onNo: () => void }) {
  if (c.state === "pending") {
    return <MeetingCard className="ml-10 max-w-[min(78%,440px)] !bg-ds-surface" title="Needs your OK" when={what} description={c.text} secondaryLabel="No" onSecondary={onNo} primaryLabel="Yes, do it" onPrimary={onYes} />;
  }
  return (
    <div className="ml-10 max-w-[min(78%,440px)]">
      <NotificationItem title={`${c.text}`} titleAdornment={<StatusDot tone={c.state === "done" ? "online" : c.state === "failed" ? "danger" : "attention"} label={c.state === "done" ? "Done" : c.state === "failed" ? "Failed" : "Not done"} />}
        subtitle={<span className={cx("inline-flex items-start gap-1.5 whitespace-pre-wrap break-all", c.state === "done" ? "text-ds-success-ink" : c.state === "failed" ? "text-ds-danger" : "text-ds-muted")}>
          <Icon name={c.state === "done" ? "check" : "x"} size={14} className="mt-0.5 shrink-0" />{c.state === "dismissed" ? "Not done" : c.result || (c.state === "done" ? "Done" : "That did not work")}
        </span>} />
    </div>
  );
}

/** A story draft (WP7b, G-47): the file's path, its length and an Open button. The file is the story: the owner can edit it,
    Refresh reads it again, and the video is made from the file as it reads when he presses Yes. */
function DraftCard({ d }: { d: DraftInfo }) {
  const { toast } = useApp();
  const [info, setInfo] = useState(d);
  useEffect(() => setInfo(d), [d]);
  const at = `/drafts/${encodeURIComponent(info.draft_id)}`;
  const open = async () => { try { await api.post(`${at}/open`); } catch (e: any) { toast(e.message, "bad"); } };
  const reread = async () => { try { setInfo(await api.get<DraftInfo>(at)); } catch (e: any) { toast(e.message, "bad"); } };
  const parts = Array.isArray(info.parts) ? info.parts.length : info.parts;
  const copy = async () => { try { await navigator.clipboard.writeText(info.path); toast("Path copied", "ok"); } catch { toast("Copy was blocked", "warn"); } };
  return (
    <div className="ml-10 max-w-[min(78%,440px)] rounded-ds-lg border border-ds-border-subtle bg-ds-surface p-3 shadow-ds-inset-sm">
      <div className="flex items-start gap-2.5">
        <span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-ds-md bg-ds-accent-soft text-ds-accent-ink"><Icon name="book" size={16} /></span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-ds-sm font-semibold" title={info.title}>{info.title}</p>
          <p className="text-ds-2xs text-ds-muted">Story draft, {parts} part{parts === 1 ? "" : "s"}, {info.words.toLocaleString()} words{info.status === "started" ? ", video started" : ""}</p>
        </div>
      </div>
      <p className="mt-2 font-mono text-ds-2xs break-all text-ds-text-2" title={info.path}>{info.path}</p>
      {info.warning && <p className="mt-1 text-ds-2xs text-ds-danger">{info.warning}</p>}
      <div className="mt-2.5 flex flex-wrap gap-2">
        <Button size="sm" variant="soft" leadingIcon="external" onClick={open}>Open</Button>
        <Button size="sm" variant="ghost" leadingIcon="refresh" onClick={reread}>Refresh</Button>
        <Button size="sm" variant="ghost" leadingIcon="copy" onClick={copy}>Copy path</Button>
      </div>
    </div>
  );
}

const CAN: { icon: IconName; t: string; s: string }[] = [
  { icon: "wand", t: "Start a story video or clips", s: "For any channel, with the same options as Create" },
  { icon: "lightbulb", t: "Draft story concepts", s: "Three concepts for a series, and redo any of them" },
  { icon: "pause", t: "Pause or resume channels", s: "A paused channel makes no story videos until it is resumed" },
  { icon: "fileSearch", t: "Find files and explain results", s: "Where a video is, why a run stopped, what changed" },
  { icon: "scissors", t: "Cut clips from a link", s: "Paste a link to a video you own" },
];

/** Overlay behaviour: Esc closes, a click outside closes, focus goes to the first control; Esc returns focus to the opener. */
function useDismiss(open: boolean, close: () => void, root: RefObject<HTMLElement | null>, opener: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { const t = e.target as Node; if (!root.current?.contains(t) && !opener.current?.contains(t)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); close(); opener.current?.focus(); } };
    addEventListener("pointerdown", away); addEventListener("keydown", esc);
    const t = setTimeout(() => root.current?.querySelector<HTMLElement>("button, [href], [tabindex]")?.focus(), 0);
    return () => { removeEventListener("pointerdown", away); removeEventListener("keydown", esc); clearTimeout(t); };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
}

export default function Assistant() {
  const { demo, ask, channelName, go, toast } = useApp();
  const jobs = useLoad<Job[]>(() => api.get("/jobs"), [], []);
  const convs = useLoad<Conv[]>(() => api.get("/assistant/conversations"), [], []);
  const [openCid, setOpenCid] = useState<string | null>(null); const [tick, setTick] = useState(0);
  const [panel, setPanel] = useState<null | "history" | "can">(null);
  const [tab, setTab] = useState<"runs" | "chats">("runs");
  const histRoot = useRef<HTMLDivElement>(null); const histBtn = useRef<HTMLButtonElement>(null);
  const canRoot = useRef<HTMLDivElement>(null); const canBtn = useRef<HTMLButtonElement>(null);
  useDismiss(panel === "history", () => setPanel(null), histRoot, histBtn);
  useDismiss(panel === "can", () => setPanel(null), canRoot, canBtn);
  const queued = convs.data.reduce((n, c) => n + (c.queued || 0), 0);
  const [answering, setAnswering] = useState(false);
  const runQueue = async () => {
    if (answering) return; setAnswering(true);
    try {
      const r = await api.post<unknown[]>("/assistant/queue/run");
      toast(r.length ? `Answered ${r.length} saved request${r.length === 1 ? "" : "s"}` : "The engine is still busy. Try again in a minute", r.length ? "ok" : "warn");
      convs.reload(); setTick((t) => t + 1);
    } catch (e: any) { toast(e.message, "bad"); } finally { setAnswering(false); }
  };
  const recent = jobs.data.slice(0, 6);
  const copy = async (text: string) => { try { await navigator.clipboard.writeText(text); toast("Details copied", "ok"); } catch { toast("Copy was blocked", "warn"); } };
  const sub = demo ? "Scripted answers while the engine is off. On your PC it runs on the local model" : "Runs on the local model. It uses the same tools as the buttons";
  // The chat is the whole page: header, scrolling message list, pinned composer. History and "What it can do" open over it.
  return (
    <div className="enter">
      <Card as="section" aria-label="Chat with Nightshift" padding="none" className="relative flex h-[75dvh] min-h-[480px] flex-col overflow-hidden md:h-[calc(100dvh-132px)] md:min-h-[360px]">
        <div className="relative z-10 flex items-center justify-between gap-3 border-b border-ds-border-subtle px-5 py-3">
          <div className="flex min-w-0 items-center gap-3"><Mark size={28} /><div className="min-w-0"><h2 className="text-ds-lg leading-ds-tight font-semibold">Nightshift</h2><p className="truncate text-ds-2xs text-ds-muted" title={sub}>{sub}</p></div></div>
          <div className="flex shrink-0 items-center gap-1.5">
            <StatusDot tone={demo ? "attention" : "online"} label={demo ? "Sample answers" : "Local model"} />
            <button ref={histBtn} type="button" aria-haspopup="dialog" aria-expanded={panel === "history"} aria-label="History" title="History" onClick={() => setPanel((p) => (p === "history" ? null : "history"))}
              className="ds-focus inline-flex h-10 items-center justify-center gap-2 rounded-ds-sm px-2.5 text-ds-sm font-semibold text-ds-text-2 transition-colors duration-[120ms] hover:bg-ds-hover hover:text-ds-text sm:h-9">
              <Icon name="history" size={18} /><span className="hidden sm:inline">History</span>
            </button>
            <div className="relative">
              <button ref={canBtn} type="button" aria-haspopup="dialog" aria-expanded={panel === "can"} aria-label="What it can do" title="What it can do" onClick={() => setPanel((p) => (p === "can" ? null : "can"))}
                className="ds-focus inline-grid size-10 place-items-center rounded-full text-ds-text-2 transition-colors duration-[120ms] hover:bg-ds-hover hover:text-ds-text sm:size-9"><Icon name="info" size={18} /></button>
              {panel === "can" && (
                <div ref={canRoot} role="dialog" aria-label="What it can do" className="fade-in absolute top-full right-0 z-30 mt-2 flex max-h-[min(460px,calc(100dvh-220px))] w-[360px] max-w-[calc(100vw-2rem)] flex-col rounded-ds-md bg-ds-surface p-4 shadow-[var(--ds-shadow-3),0_0_0_1px_var(--ds-border-subtle)]">
                  <h3 className="mb-3 text-ds-md font-semibold">What it can do</h3>
                  <ul className="flex min-h-0 flex-col gap-3 overflow-y-auto scroll-thin">
                    {CAN.map(({ icon, t, s }) => <li key={t} className="flex gap-3"><span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-ds-md bg-ds-accent-soft text-ds-accent-ink"><Icon name={icon} size={16} /></span><div className="min-w-0"><p className="text-ds-sm font-semibold">{t}</p><p className="text-ds-2xs text-ds-muted">{s}</p></div></li>)}
                  </ul>
                  <p className="mt-3 border-t border-ds-border-subtle pt-3 text-ds-xs leading-ds-normal text-ds-text-2">It asks first. Starting work, pausing a channel or saving ideas shows a card with Yes and No. Nothing changes until you press Yes</p>
                </div>
              )}
            </div>
          </div>
        </div>
        <Chat openCid={openCid} openTick={tick} />
        {panel === "history" && (
          <div ref={histRoot} role="dialog" aria-label="History" className="fade-in absolute inset-y-0 right-0 z-20 flex w-[360px] max-w-full flex-col border-l border-ds-border-subtle bg-ds-surface p-4 shadow-[var(--ds-shadow-3)]">
            <div className="mb-3 flex items-center justify-between gap-3">
              <SegmentedControl label="Runs or chats" size="sm" value={tab} onChange={setTab} options={[{ value: "runs", label: "Runs" }, { value: "chats", label: "Chats" }]} />
              <div className="flex items-center gap-1.5">
                {tab === "chats" && queued ? <Button size="sm" variant="soft" leadingIcon="play" disabled={answering} onClick={runQueue}>Answer {queued} saved</Button> : null}
                <IconButton icon="x" label="Close history" onClick={() => { setPanel(null); histBtn.current?.focus(); }} />
              </div>
            </div>
            {tab === "runs" ? (
              recent.length ? (
                <ul className="-mx-1 flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-1 pt-1 pb-1 scroll-thin">
                  {recent.map((j) => {
                    const failed = j.status === "failed"; const live = ["queued", "running"].includes(j.status); const title = jobTitle(j, channelName);
                    const menu: MenuItem[] = [
                      { icon: "chat", label: failed ? "Ask why it stopped" : "Ask about it", onSelect: () => ask({ page: "assistant", job_id: j.job_id }, failed ? "Why did the last run stop?" : `What happened in the job ${title}?`, false) },
                      { icon: "scenes", label: "Open in Create", onSelect: () => go("#/create") },
                      { icon: "copy", label: "Copy details", onSelect: () => copy(`${title}: ${j.status}${j.message ? `, ${j.message}` : ""} (job ${j.job_id})`) },
                    ];
                    return (
                      <li key={j.job_id}>
                        <NotificationItem title={title} moreLabel={`Options for ${title}`} menu={menu}
                          titleAdornment={<StatusDot tone={failed ? "danger" : live ? "accent" : j.status === "cancelled" ? "attention" : "online"} label={failed ? "Stopped" : live ? "Running" : j.status === "cancelled" ? "Cancelled" : "Done"} />}
                          subtitle={live ? stageWords(j.stage) : failed ? `${stoppedAt(j.stage)}` : j.status === "cancelled" ? "Cancelled" : "Done"}
                          meta={<><MetaItem icon="calendarDays">{fmtDay(j.created_at)}</MetaItem><MetaItem icon="clock">{fmtClock(j.created_at)}</MetaItem></>} />
                      </li>
                    );
                  })}
                </ul>
              ) : <p className="py-8 text-center text-ds-sm text-ds-text-2">No runs yet</p>
            ) : (
              convs.data.length ? (
                <ul className="-mx-1 flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-1 scroll-thin">
                  {convs.data.map((c) => (
                    <li key={c.conversation_id}>
                      <button type="button" onClick={() => { setOpenCid(c.conversation_id); setTick((t) => t + 1); setPanel(null); }} aria-current={openCid === c.conversation_id || undefined}
                        className={cx("ds-focus flex min-h-10 w-full items-center justify-between gap-3 rounded-ds-md px-3 py-2 text-left text-ds-sm hover:bg-ds-sunken", openCid === c.conversation_id && "bg-ds-accent-soft")}>
                        <span className="min-w-0 flex-1 truncate font-semibold" title={c.title || "Untitled"}>{c.title || "Untitled"}</span>
                        {c.queued ? <Badge tone="accent" size="xs">Saved</Badge> : null}
                        <span className="shrink-0 text-ds-2xs text-ds-muted">{fmtDay(c.updated_at)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : <p className="py-8 text-center text-ds-sm text-ds-text-2">Your earlier chats show up here</p>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
