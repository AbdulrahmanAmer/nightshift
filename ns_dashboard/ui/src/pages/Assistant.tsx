/* Assistant: the chat in a tall card, with tool proposals as MeetingCard-style action cards, and a side column of
   recent runs (NotificationItems) and what it can do. The chat protocol is unchanged: api.chat events, confirm and
   dismiss calls, the stored conversation (ns-chat2) and the prompt handed over by "Ask about it" and the top bar search. */
import { useEffect, useRef, useState } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtDay, fmtClock, stageWords, store } from "../lib/format";
import type { Job } from "../lib/types";
import { cx } from "../ui/kit";
import { Mark } from "../ui/Mark";
import { Button, Card, CardHeader, DashGrid, Icon, IconButton, MeetingCard, MetaItem, NotificationItem, SPAN, StatusDot, type IconName } from "../components/ref12";
import { jobTitle } from "./Queue";

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
  // Prompt handed over by "Ask about it" or the top bar search. Runs only when a prompt arrives (it used to run after every render).
  useEffect(() => { if (askPrompt) { send(askPrompt); clearAskPrompt(); } }, [askPrompt]); // eslint-disable-line react-hooks/exhaustive-deps
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
            <div className={cx("max-w-[78%] rounded-ds-lg px-4 py-2.5 text-ds-md leading-ds-normal whitespace-pre-wrap",
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
        <div className="-mx-1 mb-3 flex gap-2 overflow-x-auto px-1 pt-1 pb-2 scroll-thin">
          {SUGGEST.map((s) => <Button key={s} size="sm" variant="soft" disabled={busy} onClick={() => send(s)} className="!font-medium">{s}</Button>)}
        </div>
        <form onSubmit={(e) => { e.preventDefault(); send(text); }} className="flex items-end gap-2 rounded-ds-lg border border-ds-border bg-ds-surface p-1.5 pl-4 shadow-ds-inset-sm transition-[border-color,box-shadow] duration-[120ms] focus-within:border-ds-focus focus-within:shadow-ds-focus">
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

const TOOL_LABEL: Record<string, string> = { start_run: "Start a story video", start_clips: "Cut clips", save_ideas: "Save ideas", pause_channel: "Pause a channel", resume_channel: "Resume a channel" };

/** A tool proposal: the MeetingCard recipe (title, a when-line with the accent dot, the text, No and Yes). */
function ConfirmCard({ c, onYes, onNo }: { c: Confirm; onYes: () => void; onNo: () => void }) {
  const what = TOOL_LABEL[c.tool] || c.tool.replace(/_/g, " ");
  if (c.state === "pending") {
    return <MeetingCard className="ml-10 max-w-[min(78%,440px)] !bg-ds-surface" title="Needs your OK" when={what} description={`${c.text}?`} secondaryLabel="No" onSecondary={onNo} primaryLabel="Yes, do it" onPrimary={onYes} />;
  }
  return (
    <div className="ml-10 max-w-[min(78%,440px)]">
      <NotificationItem title={`${c.text}`} titleAdornment={<StatusDot tone={c.state === "done" ? "online" : c.state === "failed" ? "danger" : "attention"} label={c.state === "done" ? "Done" : c.state === "failed" ? "Failed" : "Not done"} />}
        subtitle={<span className={cx("inline-flex items-center gap-1.5", c.state === "done" ? "text-ds-success-ink" : c.state === "failed" ? "text-ds-danger" : "text-ds-muted")}>
          <Icon name={c.state === "done" ? "check" : "x"} size={14} />{c.state === "dismissed" ? "Not done" : c.result || (c.state === "done" ? "Done" : "That did not work")}
        </span>} />
    </div>
  );
}

const CAN: { icon: IconName; t: string; s: string }[] = [
  { icon: "wand", t: "Start a story video or clips", s: "For any channel, with the same options as Create" },
  { icon: "lightbulb", t: "Draft story concepts", s: "Three concepts for a series, and redo any of them" },
  { icon: "pause", t: "Pause or resume channels", s: "Skips tonight's run for that channel" },
  { icon: "fileSearch", t: "Find files and explain results", s: "Where a video is, why a run stopped, what changed" },
  { icon: "scissors", t: "Cut clips from a link", s: "Paste a link to a video you own" },
];

export default function Assistant() {
  const { demo, ask, channelName } = useApp();
  const jobs = useLoad<Job[]>(() => api.get("/jobs"), [], []);
  const recent = jobs.data.slice(0, 4);
  return (
    <div className="enter">
      <DashGrid label="Assistant">
        <Card as="section" aria-label="Chat with Nightshift" padding="none" className={cx("flex h-[calc(100dvh-196px)] min-h-[520px] flex-col overflow-hidden", SPAN["4/4"])}>
          <div className="flex items-center justify-between gap-3 border-b border-ds-border-subtle px-5 py-4">
            <div className="flex min-w-0 items-center gap-3"><Mark size={28} /><div className="min-w-0"><h2 className="text-ds-lg leading-ds-tight font-semibold">Nightshift</h2><p className="truncate text-ds-2xs text-ds-muted">{demo ? "Scripted answers while the engine is off. On your PC it runs on the local model" : "Runs on the local model. It uses the same tools as the buttons"}</p></div></div>
            <StatusDot tone={demo ? "attention" : "online"} label={demo ? "Sample answers" : "Local model"} />
          </div>
          <Chat />
        </Card>
        <div className={cx("flex flex-col gap-5", SPAN["2/4"])}>
          <Card as="section" aria-labelledby="a-runs">
            <CardHeader id="a-runs" title="Recent runs" />
            {recent.length ? (
              <ul className="flex flex-col gap-3">
                {recent.map((j) => {
                  const failed = j.status === "failed"; const live = ["queued", "running"].includes(j.status);
                  return (
                    <li key={j.job_id}>
                      <NotificationItem title={jobTitle(j, channelName)} moreLabel={`Ask about ${jobTitle(j, channelName)}`} onMore={() => ask({ page: "assistant", job_id: j.job_id }, failed ? "Why did the last run stop?" : `What happened in the job ${jobTitle(j, channelName)}?`, false)}
                        titleAdornment={<StatusDot tone={failed ? "danger" : live ? "accent" : j.status === "cancelled" ? "attention" : "online"} label={failed ? "Stopped" : live ? "Running" : j.status === "cancelled" ? "Cancelled" : "Done"} />}
                        subtitle={live ? stageWords(j.stage) : failed ? `Stopped at ${stageWords(j.stage).toLowerCase()}` : j.status === "cancelled" ? "Cancelled" : "Done"}
                        meta={<><MetaItem icon="calendarDays">{fmtDay(j.created_at)}</MetaItem><MetaItem icon="clock">{fmtClock(j.created_at)}</MetaItem></>} />
                    </li>
                  );
                })}
              </ul>
            ) : <p className="text-ds-sm text-ds-text-2">No runs yet</p>}
          </Card>
          <Card as="section" aria-labelledby="a-can">
            <CardHeader id="a-can" title="What it can do" />
            <ul className="flex flex-col gap-4">
              {CAN.map(({ icon, t, s }) => <li key={t} className="flex gap-3"><span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-ds-md bg-ds-accent-soft text-ds-accent-ink"><Icon name={icon} size={16} /></span><div><p className="text-ds-sm font-semibold">{t}</p><p className="text-ds-2xs text-ds-muted">{s}</p></div></li>)}
            </ul>
            <p className="mt-5 border-t border-ds-border-subtle pt-4 text-ds-xs leading-ds-normal text-ds-text-2">It asks first. Starting work, pausing a channel or saving ideas shows a card with Yes and No. Nothing changes until you press Yes</p>
          </Card>
        </div>
      </DashGrid>
    </div>
  );
}
