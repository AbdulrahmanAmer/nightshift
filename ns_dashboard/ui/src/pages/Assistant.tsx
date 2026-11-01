import { useEffect, useRef, useState } from "react";
import { ArrowUp, Check, FileSearch, Pause, Scissors, Sparkles, Wand2, X } from "lucide-react";
import { useApp } from "../lib/app";
import { api } from "../lib/api";
import { store } from "../lib/format";
import { Button, Card, cx, PageHeader } from "../ui/kit";
import { Mark } from "../ui/Mark";

type Confirm = { action_id: string; text: string; tool: string; state: "pending" | "done" | "dismissed" | "failed"; result?: string };
type Msg = { id: number; role: "user" | "assistant" | "note" | "error"; text: string; confirm?: Confirm };
type Saved = { cid: string | null; msgs: Msg[] };

const SUGGEST = ["Draft 3 story concepts for a series", "What is the engine doing?", "Where are my finished videos?", "Pause The Night Desk", "Why did Saturday's run stop?", "Start a story video for After Dark Stories"];
const TOOL_WORDS: Record<string, string> = { list_outputs: "Checking the Queue", list_jobs: "Checking jobs", status: "Checking the engine", find: "Searching files", explain_error: "Reading the log", list_channels: "Reading channels", library_ideas: "Reading ideas" };
const WELCOME: Msg = { id: 0, role: "assistant", text: "I run on this PC with the local model. I can start videos or clips, draft story concepts, pause or resume channels, find files and explain results. Anything that changes something shows you a Yes or No card first." };
let mid = Date.now();

export function Chat({ compact }: { compact?: boolean }) {
  const { askContext, askPrompt, clearAskPrompt, refresh, route } = useApp();
  const [saved, setSaved] = useState<Saved>(() => store.get<Saved>("ns-chat2", { cid: null, msgs: [WELCOME] }));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [toolNote, setToolNote] = useState<string | null>(null);
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
        else if (ev.kind === "tool_call") setToolNote(TOOL_WORDS[ev.payload.tool] || "Looking it up");
        else if (ev.kind === "busy") push({ role: "note", text: ev.payload.message });
        else if (ev.kind === "error") push({ role: "error", text: ev.payload.message + (ev.payload.hint ? `. ${ev.payload.hint}` : "") });
      }
    } catch (e: any) { push({ role: "error", text: `I could not answer: ${e.message}` }); }
    setBusy(false); setToolNote(null); taRef.current?.focus();
  };
  useEffect(() => { if (askPrompt) { send(askPrompt); clearAskPrompt(); } }); // prompt handed over by "Ask about it"
  useEffect(() => { taRef.current?.focus(); }, []);

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
      <div ref={logRef} aria-live="polite" className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-5 scroll-thin">
        {saved.msgs.map((m) => m.confirm ? <ConfirmCard key={m.id} c={m.confirm} onYes={() => decide(m, true)} onNo={() => decide(m, false)} /> : (
          <div key={m.id} className={cx("flex gap-3", m.role === "user" && "justify-end")}>
            {m.role !== "user" && m.role !== "note" && <span className="mt-0.5 shrink-0"><Mark size={28} /></span>}
            <div className={cx("max-w-[78%] rounded-[18px] px-4 py-2.5 text-[14px] leading-[22px] whitespace-pre-wrap",
              m.role === "user" ? "rounded-br-[6px] bg-fill text-on-fill" : m.role === "note" ? "mx-auto bg-transparent text-center text-[12px] text-muted" : m.role === "error" ? "rounded-tl-[6px] bg-bad-soft text-text" : "rounded-tl-[6px] bg-raised text-text")}>{m.text}</div>
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-3">
            <Mark size={28} />
            <span className="flex items-center gap-2 rounded-[18px] rounded-tl-[6px] bg-raised px-4 py-3 text-[13px] text-text-2">
              <span className="flex gap-1" aria-hidden>{[0, 1, 2].map((i) => <i key={i} className="size-1.5 rounded-full bg-text-2" style={{ animation: `typing 1.2s ${i * 0.15}s infinite` }} />)}</span>
              {toolNote && <span>{toolNote}</span>}<span className="sr-only">Thinking</span>
            </span>
          </div>
        )}
      </div>
      <div className="border-t border-line px-4 pt-3 pb-4">
        <div className="mb-3 flex gap-2 overflow-x-auto pb-1 scroll-thin">
          {SUGGEST.map((s) => <button key={s} type="button" disabled={busy} onClick={() => send(s)} className="h-8 shrink-0 rounded-full border border-line-2 bg-card px-3 text-[13px] text-text-2 transition-colors hover:bg-raised hover:text-text disabled:opacity-50">{s}</button>)}
        </div>
        <form onSubmit={(e) => { e.preventDefault(); send(text); }} className="flex items-end gap-2 rounded-[20px] border border-line-2 bg-card p-1.5 pl-4 focus-within:border-text-2">
          <label htmlFor={compact ? "ask-text" : "chat-text"} className="sr-only">Message</label>
          <textarea id={compact ? "ask-text" : "chat-text"} ref={taRef} rows={1} value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask or tell Nightshift"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text); } }}
            className="max-h-40 min-h-[36px] flex-1 resize-none bg-transparent py-2 text-[14px] leading-5 outline-none placeholder:text-muted" />
          <button type="submit" aria-label="Send" disabled={busy || !text.trim()} className="flex size-9 shrink-0 items-center justify-center rounded-full bg-fill text-on-fill transition-opacity disabled:opacity-30"><ArrowUp size={18} strokeWidth={1.75} /></button>
        </form>
        <div className="mt-2 flex items-center justify-between px-1 text-[12px] text-muted"><span>Enter to send, Shift+Enter for a new line</span><button type="button" onClick={reset} className="hover:text-text">New conversation</button></div>
      </div>
    </div>
  );
}

function ConfirmCard({ c, onYes, onNo }: { c: Confirm; onYes: () => void; onNo: () => void }) {
  return (
    <div className="ml-10 max-w-[78%] rounded-[16px] border border-line-2 bg-card p-4 shadow-sm">
      <p className="text-[12px] text-muted">Needs your OK</p>
      <p className="mt-1 text-[14px] font-medium">{c.text}?</p>
      {c.state === "pending" ? (
        <div className="mt-3 flex gap-2"><Button size="sm" variant="primary" onClick={onYes}>Yes, do it</Button><Button size="sm" variant="ghost" onClick={onNo}>No</Button></div>
      ) : (
        <p className={cx("mt-2 flex items-center gap-1.5 text-[13px]", c.state === "done" ? "text-ok" : c.state === "failed" ? "text-bad" : "text-muted")}>
          {c.state === "done" ? <Check size={14} strokeWidth={2} /> : <X size={14} strokeWidth={2} />}
          {c.state === "dismissed" ? "Not done" : c.result || (c.state === "done" ? "Done" : "That did not work")}
        </p>
      )}
    </div>
  );
}

const CAN = [
  { icon: Wand2, t: "Start a story video or clips", s: "For any channel, with the same options as Create" },
  { icon: Sparkles, t: "Draft story concepts", s: "Three concepts for a series, and redo any of them" },
  { icon: Pause, t: "Pause or resume channels", s: "Skips tonight's run for that channel" },
  { icon: FileSearch, t: "Find files and explain results", s: "Where a video is, why a run stopped, what changed" },
  { icon: Scissors, t: "Cut clips from a link", s: "Paste a link to a video you own" },
];

export default function Assistant() {
  const { demo } = useApp();
  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader title="Assistant" sub={demo ? "Scripted answers while the engine is off. On your PC it runs on the local model" : "Runs on the local model. It uses the same tools as the buttons"} />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="flex h-[calc(100vh-196px)] min-h-[520px] flex-col overflow-hidden"><Chat /></Card>
        <div className="flex flex-col gap-4">
          <Card className="p-5">
            <h2 className="text-[15px] font-medium">What it can do</h2>
            <ul className="mt-3 flex flex-col gap-3.5">
              {CAN.map(({ icon: I, t, s }) => <li key={t} className="flex gap-3"><span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-raised text-text-2"><I size={16} strokeWidth={1.5} /></span><div><p className="text-[13px] font-medium">{t}</p><p className="text-[12px] text-muted">{s}</p></div></li>)}
            </ul>
          </Card>
          <Card className="p-5">
            <h2 className="text-[15px] font-medium">It asks first</h2>
            <p className="mt-2 text-[13px] text-text-2">Starting work, pausing a channel or saving ideas shows a card with Yes and No. Nothing changes until you press Yes.</p>
          </Card>
        </div>
      </div>
    </div>
  );
}
