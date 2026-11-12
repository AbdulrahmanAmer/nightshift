/* Library: one NavTabs row per bank, then the bank's items as cards in the 6-column grid (3 across, 2 at 4 columns).
   Voices gets the full treatment: voice cards with Waveform + TransportControls, and a maker card with a KnobSwitch
   (Design / Clone) and an IntakeStepper for the clone steps. Design and clone call POST /voices/design and /voices/clone
   (CR-C3-UI-7): the clone stepper needs the rights box ticked before Save, NS-E312 shows as a readable error with a way
   back to the sample step, and a made voice appears under the form with its test line ready on the same transport. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useApp, useLoad } from "../lib/app";
import { api, ApiError } from "../lib/api";
import { fmtDay, fmtDur, plural, store } from "../lib/format";
import type { Footage, Format, Idea, Job, PendingVoice, SeriesStory, Story, Voice, VoiceApplied, VoiceMade } from "../lib/types";
import { cx } from "../ui/kit";
import { Scene } from "../visuals/Scene";
import { LayoutDiagram } from "../visuals/charts";
import {
  Badge, Button, Card, CardHeader, Checkbox, DashGrid, DropTile, EmptyState, Field, Icon, Input, IntakeStepper, KnobSwitch, MetaItem, NavTabs, ProgressBar, PromoCard,
  sampleWaveform, SearchInput, SegmentedControl, Select, SeriesIllustration, SPAN, TextArea, TransportControls, Waveform,
  type NavTab, type Tone,
} from "../components/ref12";

type Tab = "ideas" | "stories" | "voices" | "formats" | "footage" | "series";
const TABS: NavTab<Tab>[] = [
  { id: "ideas", label: "Ideas", icon: "lightbulb", href: "#/library?tab=ideas" },
  { id: "stories", label: "Stories", icon: "book", href: "#/library?tab=stories" },
  { id: "voices", label: "Voices", icon: "voice", href: "#/library?tab=voices" },
  { id: "formats", label: "Formats", icon: "dashboard", href: "#/library?tab=formats" },
  { id: "footage", label: "Footage", icon: "film", href: "#/library?tab=footage" },
  { id: "series", label: "Series", icon: "integrations", href: "#/library?tab=series" },
];
const SUBS: Record<Tab, string> = {
  ideas: "Story ideas waiting to be made. Approved ideas are used first on the next run",
  stories: "Every story Nightshift has written, with how it scored",
  voices: "Narrators for your channels",
  formats: "The shape of each video: canvas, layout and captions. Each one is a preset file",
  footage: "Stock clips kept on this PC for 90 days after a video uses them",
  series: "Stories developed with the Assistant part by part, then made automatically",
};
const MOVED = "It is moved to the removed folder in your Nightshift home, and can be put back by hand.";
const SAMPLE_LINE = "The doorbell camera pings at three twelve every night. I stopped checking the clips a week ago.";

export default function Library() {
  const { route } = useApp();
  const first = route.query.get("tab") as Tab | null;
  const [tab, setTabState] = useState<Tab>(first && SUBS[first] ? first : store.get<Tab>("ns-lib-tab", "ideas"));
  const setTab = (t: Tab) => { setTabState(t); store.set("ns-lib-tab", t); };
  return (
    <div className="enter flex flex-col gap-5">
      <div className="flex flex-col gap-1 border-b border-ds-border-subtle">
        <NavTabs label="Library banks" tabs={TABS} active={tab} onChange={setTab} />
      </div>
      <p className="-mt-1 text-ds-md text-ds-text-2">{SUBS[tab]}</p>
      {tab === "ideas" ? <Ideas /> : tab === "stories" ? <Stories /> : tab === "voices" ? <Voices /> : tab === "formats" ? <Formats /> : tab === "footage" ? <FootageTab /> : <Series />}
    </div>
  );
}

/* ---------------------------------------------------------------- Ideas */

const sourceLabel = (s?: string) => { const k = String(s || "").toLowerCase(); return k === "owner" || k === "you" ? "Your idea" : k === "assistant" ? "From the Assistant" : k ? "Written overnight" : ""; };
const IDEA_LABEL: Record<string, string> = { new: "New", approved: "Approved for production", used: "Used", rejected: "Dropped", dropped: "Dropped" };
const IDEA_TONE: Record<string, Tone> = { new: "neutral", approved: "accent", used: "success", rejected: "danger", dropped: "danger" };

function Ideas() {
  const { status, toast, ask, channelName, confirm } = useApp(); const chans = status?.channels || [];
  const ideas = useLoad<Idea[]>(() => api.get("/library/ideas"), [], []);
  const [title, setTitle] = useState(""); const [premise, setPremise] = useState(""); const [chan, setChan] = useState(chans[0]?.slug || "");
  const [filter, setFilter] = useState<string>("all");
  const norm = (s: string) => (s === "dropped" ? "rejected" : s);
  const shown = ideas.data.filter((i) => filter === "all" || norm(i.status) === filter);
  const count = (s: string) => ideas.data.filter((i) => norm(i.status) === s).length;
  const add = async () => {
    if (!premise.trim()) { toast("Write the idea first", "warn"); return; }
    try { await api.post("/library/ideas", { title: title.trim() || premise.trim().slice(0, 60), premise: premise.trim(), channel: chan, status: "new", source: "owner" }); setTitle(""); setPremise(""); toast("Idea added", "ok"); ideas.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const setStatus = async (i: Idea, s: string) => {
    try { await api.put(`/library/ideas/${encodeURIComponent(String(i.idea_id))}`, { status: s }); toast(s === "approved" ? "Approved. It will be used on the next run" : s === "rejected" ? "Dropped" : "Back to new", "ok"); ideas.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const remove = async (i: Idea) => {
    if (!(await confirm({ title: "Remove this idea?", body: `${i.title || "Untitled idea"}. ${MOVED}`, ok: "Remove", danger: true }))) return;
    try { await api.del(`/library/ideas/${encodeURIComponent(String(i.idea_id))}`); toast("Idea removed", "ok"); ideas.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  return (
    <DashGrid label="Ideas">
      <Card as="section" aria-labelledby="l-add" padding="md" className={SPAN["6/4"]}>
        <CardHeader id="l-add" level={3} title="Add an idea" className="!mb-3" actions={<SegmentedControl label="Show" size="sm" value={filter} onChange={setFilter}
          options={[{ value: "all", label: `All ${ideas.data.length}` }, { value: "new", label: `New ${count("new")}` }, { value: "approved", label: `Approved ${count("approved")}` }, { value: "used", label: `Used ${count("used")}` }, { value: "rejected", label: `Dropped ${count("rejected")}` }]} />} />
        <div className="grid grid-cols-1 gap-3 @container lg:grid-cols-[220px_minmax(0,1fr)_200px_auto]">
          <Input aria-label="Title" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
          <Input aria-label="Premise" placeholder="What happens, and the turn, in one or two sentences" value={premise} onChange={(e) => setPremise(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
          <Select aria-label="Channel" value={chan} onChange={(e) => setChan(e.target.value)}>{chans.map((c) => <option key={c.slug} value={c.slug}>{c.display_name}</option>)}</Select>
          <Button size="lg" leadingIcon="plus" onClick={add}>Add idea</Button>
        </div>
      </Card>
      {shown.length ? shown.map((i) => {
        const s = norm(i.status);
        return (
          <Card as="article" key={i.idea_id} padding="md" className={cx("flex flex-col gap-3 self-stretch", SPAN["2/2"])}>
            <header className="flex items-center justify-between gap-2"><Badge tone={IDEA_TONE[s] || "neutral"}>{IDEA_LABEL[s] || s}</Badge><span className="truncate text-ds-2xs font-semibold text-ds-text-2">{channelName(i.channel)}</span></header>
            <div><h3 className="text-ds-md leading-ds-snug font-semibold">{i.title || "Untitled idea"}</h3><p className="mt-1 text-ds-sm leading-ds-normal text-ds-text-2">{i.premise}</p></div>
            <p className="text-ds-2xs text-ds-muted">{sourceLabel(i.source)}{i.created_at ? ` · ${fmtDay(i.created_at)}` : ""}</p>
            <div className="mt-auto flex flex-wrap gap-2 pt-1">
              {s === "new" && <Button size="sm" onClick={() => setStatus(i, "approved")}>Approve for production</Button>}
              {(s === "new" || s === "approved") && <Button size="sm" variant="ghost" onClick={() => setStatus(i, "rejected")}>Drop</Button>}
              {s === "rejected" && <Button size="sm" variant="soft" leadingIcon="restore" onClick={() => setStatus(i, "new")}>Restore</Button>}
              <Button size="sm" variant="ghost" leadingIcon="chat" onClick={() => ask({ page: "library", idea_id: i.idea_id }, `Develop this idea into a story for ${channelName(i.channel)}: ${i.title}. ${i.premise}`)}>Develop with Assistant</Button>
              <Button size="sm" variant="ghost" leadingIcon="trash" onClick={() => remove(i)}>Remove</Button>
            </div>
          </Card>
        );
      }) : <EmptyState icon="lightbulb" className={SPAN["6/4"]} title="No ideas here">Add one above, or ask the Assistant for concepts</EmptyState>}
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Stories */

function Search({ value, onChange, placeholder }: { value: string; onChange: (s: string) => void; placeholder: string }) {
  const [v, setV] = useState(value);
  return <SearchInput label={placeholder} placeholder={placeholder} value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => e.key === "Enter" && onChange(v)} onBlur={() => onChange(v)} className="w-full max-w-sm" />;
}

function Stories() {
  const { channelName, ask, confirm, toast } = useApp(); const [q, setQ] = useState("");
  const stories = useLoad<Story[]>(() => api.get(`/library/stories?q=${encodeURIComponent(q)}`), [q], []);
  const st: Record<string, [string, Tone]> = { none: ["No video yet", "neutral"], pending: ["Waiting for review", "accent"], posted: ["Posted", "success"], approved: ["Approved", "success"], rejected: ["Rejected", "danger"] };
  const remove = async (s: Story) => {
    if (!(await confirm({ title: "Remove this story?", body: `${s.title}. Its videos and files go with it. ${MOVED}`, ok: "Remove", danger: true }))) return;
    try { await api.del(`/library/stories/${encodeURIComponent(s.job_id)}`); toast("Story removed", "ok"); stories.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  return (
    <DashGrid label="Stories">
      <div className={cx("flex flex-wrap items-center justify-between gap-3", SPAN["6/4"])}><Search value={q} onChange={setQ} placeholder="Search stories" /><span className="text-ds-2xs text-ds-muted ds-num">{stories.data.length} stories</span></div>
      {stories.data.length ? stories.data.map((s) => {
        const [label, tone] = st[s.status] || [s.status, "neutral" as Tone];
        return (
          <Card as="article" key={s.job_id} padding="sm" className={cx("flex flex-col gap-3 self-stretch", SPAN["2/2"])}>
            <div className="relative aspect-[16/7] overflow-hidden rounded-ds-md bg-ds-media"><Scene seed={s.title} w={1600} h={700} className="size-full" /><Badge tone="overlay" className="absolute top-2 left-2">{s.format === "shorts_only" ? "One Short" : "Full video + Shorts"}</Badge></div>
            <div className="px-1">
              <div className="flex items-center justify-between gap-2"><Badge tone={tone}>{label}</Badge><span className="text-ds-2xs text-ds-muted">{fmtDay(s.created_at)}</span></div>
              <h3 className="mt-2 text-ds-md leading-ds-snug font-semibold">{s.title}</h3>
              <p className="text-ds-2xs text-ds-text-2">{channelName(s.channel)}</p>
              <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1"><MetaItem icon="file">{s.words.toLocaleString("en-US")} words</MetaItem><MetaItem icon="target">{s.critic_avg != null ? `Critic ${s.critic_avg.toFixed(1)}` : "Not scored"}</MetaItem><MetaItem icon="film">{plural(s.outputs.length, "video")}</MetaItem></p>
            </div>
            <div className="mt-auto flex gap-1 px-1 pb-1">
              <Button size="sm" variant="soft" onClick={() => { location.hash = `#/?tab=${s.status === "pending" ? "ready" : s.status === "rejected" ? "rejected" : "posted"}`; }}>Open</Button>
              <Button size="sm" variant="ghost" leadingIcon="restore" onClick={() => ask({ page: "library", story_id: s.job_id }, `Reuse the story "${s.title}" as the start of a new video`)}>Reuse</Button>
              <Button size="sm" variant="ghost" leadingIcon="phone" onClick={() => { location.hash = `#/create?kind=story&format=shorts_only&channel=${encodeURIComponent(s.channel)}`; }}>Make a Short</Button>
              <Button size="sm" variant="ghost" leadingIcon="integrations" onClick={() => ask({ page: "library", story_id: s.job_id }, `Turn the story "${s.title}" into a series of parts for ${channelName(s.channel)}`)}>Reuse as series</Button>
              <Button size="sm" variant="ghost" leadingIcon="trash" onClick={() => remove(s)}>Remove</Button>
            </div>
          </Card>
        );
      }) : <EmptyState icon="book" className={SPAN["6/4"]} title="No stories match" />}
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Voices */

const modeLabel = (m: string) => (m === "preset" ? "Built-in" : m === "design" ? "Designed" : "Cloned");

function VoiceCard({ v, active, onPlay, line = SAMPLE_LINE, onRemove }: { v: Voice; active: boolean; onPlay: (name: string | null) => void; line?: string; onRemove?: (v: Voice) => void }) {
  const { toast } = useApp();
  const audio = useRef<HTMLAudioElement | null>(null);
  const [progress, setProgress] = useState(0);
  const [loading, setLoading] = useState(false);
  const bars = useRef(sampleWaveform(64, v.name)).current;
  useEffect(() => { if (!active && audio.current) { audio.current.pause(); setProgress(0); } }, [active]);
  useEffect(() => () => audio.current?.pause(), []);
  const toggle = async () => {
    if (active) { audio.current?.pause(); onPlay(null); return; }
    try {
      setLoading(true); onPlay(v.name);
      const url = await api.voicePreview(v.name, line);
      const a = new Audio(url); audio.current = a;
      a.ontimeupdate = () => setProgress(a.duration ? a.currentTime / a.duration : 0);
      a.onended = () => { setProgress(0); onPlay(null); };
      await a.play();
    } catch (e: any) { onPlay(null); toast(e.message, "warn"); }
    setLoading(false);
  };
  return (
    <Card as="article" padding="md" className="flex flex-col gap-4">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0"><h3 className="text-ds-lg leading-ds-snug font-semibold">{v.name}</h3>{v.description && <p className="mt-0.5 line-clamp-2 text-ds-xs text-ds-text-2">{v.description}</p>}</div>
        <Badge tone={v.mode === "preset" ? "neutral" : "accent"}>{modeLabel(v.mode)}</Badge>
      </header>
      <Waveform bars={bars} progress={active ? progress : 0} />
      <TransportControls label={`Sample of ${v.name}`} playing={active && !loading} onToggle={toggle} playLabel={`Play a sample of ${v.name}`} pauseLabel={`Stop ${v.name}`} />
      {onRemove && v.mode !== "preset" && <div><Button size="sm" variant="ghost" leadingIcon="trash" onClick={() => onRemove(v)}>Remove</Button></div>}
    </Card>
  );
}

const VOICE_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;
interface Fail { title: string; text: string; hint?: string; code?: string; /** The voice design model is not installed: offer to get it. */ getModel?: boolean }
/** Voice errors by code: NS-E310 engine failure, NS-E311 stored voice data missing, NS-E312 bad sample, NS-E314 design model missing, NS-E315 voice could not be built, NS-E316 search service failure. */
const VOICE_TITLES: Record<string, string> = { "NS-E310": "The voice engine did not answer", "NS-E311": "This voice's stored data is missing", "NS-E312": "That recording will not work for cloning", "NS-E315": "The voice could not be built", "base_model_missing": "The voice cloning model is not installed", "NS-E316": "The search service failed" };
/** What went wrong, in words the owner can act on. NS-E312 (a clone sample that is too short, quiet, clipped or noisy) gets its own heading. */
const failOf = (e: unknown): Fail => {
  if (e instanceof ApiError && (e.code === "model_missing" || e.code === "NS-E314")) return { title: "The voice design model is not installed", text: e.reason, hint: "Get it with the button below (it is a large download), then try again", code: e.code === "NS-E314" ? e.code : undefined, getModel: true };
  if (e instanceof ApiError) return { title: (e.code && VOICE_TITLES[e.code]) || "The voice was not made", text: e.reason, hint: e.hint, code: e.code && e.code.startsWith("NS-") ? e.code : undefined };
  return { title: "The voice was not made", text: (e as Error)?.message || "Something went wrong" };
};

function GetModelButton() {
  const { toast } = useApp(); const [busy, setBusy] = useState(false);
  const get = async () => {
    setBusy(true);
    try { await api.post("/models/get", { name: "voicedesign" }); toast("Downloading the voice design model. You can follow it in Settings, Models", "ok"); }
    catch (e: any) { toast(e instanceof ApiError && e.code === "models_unavailable" ? "Getting it from here is not available yet. Run the installer's model step again with voice design ticked" : e.message, "warn"); }
    setBusy(false);
  };
  return <div className="mt-2"><Button size="sm" variant="soft" leadingIcon="download" disabled={busy} onClick={get}>Get voice design model</Button></div>;
}

function FailNote({ f, children }: { f: Fail; children?: ReactNode }) {
  return (
    <div role="alert" className="flex gap-3 rounded-ds-md bg-ds-danger-soft px-3 py-3 text-ds-xs leading-ds-normal text-ds-text-2">
      <Icon name="alert" size={16} className="mt-px shrink-0 text-ds-danger" />
      <div className="min-w-0 flex-1">
        <p className="text-ds-sm font-semibold text-ds-text">{f.title}</p>
        <p className="mt-1">{f.text}</p>
        {f.hint && <p className="mt-1 text-ds-text">{f.hint}</p>}
        {f.code && <p className="mt-1 text-ds-2xs text-ds-muted ds-num">{f.code}</p>}
        {f.getModel && <GetModelButton />}
        {children && <div className="mt-2">{children}</div>}
      </div>
    </div>
  );
}

/** The voice the installer's setup asked for and could not make yet (it needs the TTS engine that the smoke test settles): one button queues the job that makes it. The card follows the job (progress, Cancel); the same job shows in the Queue. */

const DONE_STATES = ["done", "failed", "cancelled"];
/** A failed job as the card words it: its code (NS-E314 offers the model download) and message. */
const failOfJob = (j: Job): Fail => {
  const code = j.result?.error?.code;
  if (code === "NS-E314") return { title: "The voice design model is not installed", text: j.message, hint: "Get it with the button below (it is a large download), then try again", code, getModel: true };
  return { title: (code && VOICE_TITLES[code]) || "The voice was not made", text: j.message, hint: j.result?.error?.hint ?? undefined, code: code && code.startsWith("NS-") ? code : undefined };
};

function PendingVoiceCard({ onMade }: { onMade: () => void }) {
  const { toast } = useApp();
  const pending = useLoad<PendingVoice>(() => api.get("/voices/pending"), [], { pending: null });
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [fail, setFail] = useState<Fail | null>(null);
  const p = pending.data.pending;
  const busy = !!jobId;
  // A job that was started before a reload is still running: pick it up.
  useEffect(() => { api.get<Job[]>("/jobs?kind=apply_voice&limit=1").then((l) => { const j = l[0]; if (j && !DONE_STATES.includes(j.status)) { setJob(j); setJobId(j.job_id); } }).catch(() => {}); }, []);
  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    const tick = async () => {
      try {
        const j = await api.get<Job>(`/jobs/${encodeURIComponent(jobId)}`);
        if (!alive) return;
        setJob(j);
        if (!DONE_STATES.includes(j.status)) return;
        setJobId(null);
        if (j.status === "done") { toast(`Voice ${j.result?.created ?? j.params?.name ?? ""} created`.replace("  ", " "), "ok"); onMade(); }
        else if (j.status === "failed") setFail(failOfJob(j));
        else toast("Stopped. The voice was not made", "warn");
        pending.reload();
      } catch { /* the engine is busy for a moment: ask again */ }
    };
    tick();
    const t = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(t); };
  }, [jobId]);
  if (!p) return null;
  const create = async () => {
    setFail(null);
    try {
      const r = await api.post<VoiceApplied>("/voices/apply-pending");
      if (r.job_id) setJobId(r.job_id); else pending.reload();
    } catch (e) { setFail(failOf(e)); }
  };
  const stop = async () => { if (!jobId) return; try { await api.post(`/jobs/${encodeURIComponent(jobId)}/cancel`); toast("Stopping", "warn"); } catch (e: any) { toast(e.message, "bad"); } };
  return (
    <Card as="section" aria-labelledby="v-pending" className="flex flex-col gap-3 @min-[600px]:col-span-2">
      <CardHeader id="v-pending" title="Pending voice from setup" className="!mb-0" adornment={<Badge tone="accent">{p.mode === "clone" ? "Clone" : "Design"}</Badge>} />
      <p className="text-ds-sm leading-ds-normal"><span className="font-semibold">{p.name}</span>{p.mode === "design" && p.description ? `: ${p.description}` : ": cloned from the recording you gave in setup"}. Setup could not make it yet, so a video for {p.channel || "your channel"} cannot be voiced until it exists: the job stops at the voice stage with NS-E311. Press Create it now first.</p>
      {fail && <FailNote f={fail} />}
      {busy && <div role="status" className="flex flex-col gap-2 text-ds-xs text-ds-muted"><span>{job?.message || "Waiting to start"}. This takes up to a minute while the model loads; you can follow it in the Queue</span><ProgressBar value={job?.progress ?? 0} label="Voice progress" /></div>}
      <div className="flex flex-wrap items-center gap-3">
        <Button leadingIcon="wand" disabled={busy} onClick={create}>{busy ? "Creating" : "Create it now"}</Button>
        {busy && <Button variant="ghost" onClick={stop}>Cancel</Button>}
      </div>
    </Card>
  );
}

function Voices() {
  const { ask, toast, confirm } = useApp(); const voices = useLoad<Voice[]>(() => api.get("/voices"), [], []);
  const [playing, setPlaying] = useState<string | null>(null);
  const [clone, setClone] = useState(false);
  const [desc, setDesc] = useState(""); const [designName, setDesignName] = useState("");
  const [step, setStep] = useState(1);
  const [sample, setSample] = useState<File | null>(null); const [name, setName] = useState(""); const [line, setLine] = useState(SAMPLE_LINE);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState<"design" | "clone" | null>(null); const [upload, setUpload] = useState<number | null>(null);
  const [fail, setFail] = useState<Fail | null>(null);
  const [made, setMade] = useState<{ voice: Voice; line: string } | null>(null);
  const nameOk = (n: string) => VOICE_NAME.test(n.trim());
  const canNext = step === 1 ? !!sample : step === 2 ? nameOk(name) : step === 3 ? !!line.trim() : false;
  const CLONE_STEPS = ["Upload a sample", "Name it", "Test line", "Save"];
  const switchMode = (v: boolean) => { setClone(v); setFail(null); };

  /** One path for both makers: call, then refresh the list and show the new voice with its test line ready to play. */
  const make = async (kind: "design" | "clone", run: () => Promise<VoiceMade>, voice: Voice, testLine: string, done: () => void) => {
    setBusy(kind); setFail(null); setMade(null);
    try {
      await run(); toast(`Voice ${voice.name} saved`, "ok");
      setMade({ voice, line: testLine }); voices.reload(); done();
    } catch (e) { setFail(failOf(e)); }
    setBusy(null); setUpload(null);
  };
  const design = () => make("design", () => api.voiceDesign(designName.trim(), desc.trim(), line.trim()), { name: designName.trim(), mode: "design", description: desc.trim() }, line.trim(), () => { setDesc(""); setDesignName(""); });
  const save = () => make("clone", () => api.voiceClone(sample!, name.trim(), line.trim(), setUpload), { name: name.trim(), mode: "clone", description: "Cloned from your recording" }, line.trim(), () => { setSample(null); setName(""); setConsent(false); setStep(1); });

  /** A built-in voice is refused (422) and a voice a channel still uses is refused (409, naming the channels): the toast says what the route said. */
  const removeVoice = async (v: Voice) => {
    if (!(await confirm({ title: `Remove the voice ${v.name}?`, body: MOVED, ok: "Remove", danger: true }))) return;
    try { await api.del(`/voices/${encodeURIComponent(v.name)}`); toast(`Voice ${v.name} removed`, "ok"); if (playing === v.name) setPlaying(null); setMade(null); voices.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  return (
    <DashGrid label="Voices">
      <div className={cx("grid grid-cols-1 content-start gap-5 @min-[600px]:grid-cols-2", SPAN["4/4"])}>
        <PendingVoiceCard onMade={voices.reload} />
        {voices.data.map((v) => <VoiceCard key={v.name} v={v} active={playing === v.name} onPlay={setPlaying} onRemove={removeVoice} />)}
        {!voices.data.length && !voices.loading && <EmptyState icon="voice" className="@min-[600px]:col-span-2" title="No voices yet" />}
      </div>
      <Card as="section" aria-labelledby="v-new" className={cx("flex flex-col gap-5", SPAN["2/4"])}>
        <CardHeader id="v-new" title="Make a new voice" className="!mb-0" />
        <KnobSwitch label="Clone from a sample instead of designing" checked={clone} onChange={switchMode} offLabel="Design" onLabel="Clone" offIcon="wand" onIcon="mic" />
        {!clone ? (
          <div className="flex flex-col gap-4">
            <Field label="Describe the voice" htmlFor="voice-desc" hint="The first one downloads the voice design model, about 2 GB"><TextArea id="voice-desc" rows={3} placeholder="Deep, calm older man, slow, a little raspy" value={desc} onChange={(e) => setDesc(e.target.value)} disabled={busy != null} /></Field>
            <Field label="Name" htmlFor="voice-dname" hint="Letters, digits, - or _, no spaces"><Input id="voice-dname" placeholder="Hollow-Porch" value={designName} onChange={(e) => setDesignName(e.target.value)} disabled={busy != null} aria-invalid={!!designName.trim() && !nameOk(designName)} /></Field>
            <Field label="Test line" htmlFor="voice-dline" hint="Nightshift reads this in the new voice as soon as it is made"><TextArea id="voice-dline" rows={2} value={line} onChange={(e) => setLine(e.target.value)} disabled={busy != null} /></Field>
            {fail && <FailNote f={fail} />}
            <div className="flex flex-wrap items-center gap-2">
              <Button leadingIcon="wand" disabled={!desc.trim() || !nameOk(designName) || !line.trim() || busy != null} onClick={design}>{busy === "design" ? "Designing" : "Design it"}</Button>
              <Button variant="soft" leadingIcon="chat" disabled={!desc.trim() || busy != null} onClick={() => { ask({ page: "library", tab: "voices" }, `Design a new narrator voice: ${desc.trim()}`); setDesc(""); }}>Ask the Assistant</Button>
              {busy === "design" && <span role="status" className="text-ds-xs text-ds-muted">This takes up to a minute while the model loads</span>}
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <IntakeStepper bare title={CLONE_STEPS[step - 1]} caption={`Step ${step} of 4`} value={step} max={4} onChange={(v) => { setStep(v); setFail(null); }} decLabel="Previous step" incLabel="Next step" canIncrement={canNext && busy == null} />
            {step === 1 && (sample
              ? <div className="flex items-center gap-3 rounded-ds-lg bg-ds-sunken p-3 shadow-ds-inset-sm"><Icon name="voice" size={18} className="text-ds-accent-ink" /><span className="min-w-0 flex-1 truncate text-ds-sm font-semibold">{sample.name}</span><Button size="sm" variant="ghost" leadingIcon="x" onClick={() => { setSample(null); setFail(null); }}>Change</Button></div>
              : <DropTile compact title="Drop a 3 to 30 second sample" sub="One speaker, no music or background noise. A voice you have the rights to" accept=".wav,.mp3,.m4a,.flac,.ogg,.webm" onFile={(f) => { setSample(f); setFail(null); }} />)}
            {step === 2 && <Field label="Name" htmlFor="voice-cname" hint="Letters, digits, - or _, no spaces. How it shows in Channels and here"><Input id="voice-cname" placeholder="My-narrator" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!name.trim() && !nameOk(name)} /></Field>}
            {step === 3 && <Field label="Test line" htmlFor="voice-line" hint="Nightshift reads this in the new voice before you save"><TextArea id="voice-line" rows={3} value={line} onChange={(e) => setLine(e.target.value)} /></Field>}
            {step === 4 && (
              <div className="flex flex-col gap-3">
                <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 text-ds-sm"><dt className="text-ds-muted">Sample</dt><dd className="truncate font-semibold">{sample?.name}</dd><dt className="text-ds-muted">Name</dt><dd className="truncate font-semibold">{name}</dd></dl>
                <Checkbox checked={consent} onChange={setConsent} className="text-ds-sm">This is my own voice, or I have written permission to clone it</Checkbox>
                {fail && <FailNote f={fail}>{fail.code === "NS-E312" && <Button size="sm" variant="soft" leadingIcon="mic" onClick={() => { setSample(null); setStep(1); setFail(null); }}>Choose another recording</Button>}</FailNote>}
                <div className="flex flex-wrap items-center gap-3">
                  <Button leadingIcon="check" disabled={!consent || !sample || !nameOk(name) || !line.trim() || busy != null} onClick={save}>{busy === "clone" ? (upload != null && upload < 100 ? "Uploading" : "Cloning") : "Save voice"}</Button>
                  {busy === "clone" && upload != null && upload < 100 ? <ProgressBar value={upload} label="Upload" className="w-40" /> : busy === "clone" ? <span role="status" className="text-ds-xs text-ds-muted">Checking the recording and cloning, up to a minute</span> : !consent ? <span className="text-ds-xs text-ds-muted">Tick the box to save</span> : null}
                </div>
              </div>
            )}
            {step < 4 && <Button variant="soft" trailingIcon="chevronRight" className="self-end" disabled={!canNext} onClick={() => setStep(step + 1)}>Next</Button>}
          </div>
        )}
        {made && (
          <div className="flex flex-col gap-3 border-t border-ds-border-subtle pt-4">
            <p role="status" className="flex items-center gap-2 text-ds-sm font-semibold"><Icon name="check" size={16} className="text-ds-success-ink" />{made.voice.name} is ready</p>
            <VoiceCard v={made.voice} line={made.line} active={playing === made.voice.name} onPlay={setPlaying} />
          </div>
        )}
      </Card>
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Formats */

function Formats() {
  const { toast, channelName } = useApp(); const formats = useLoad<Format[]>(() => api.get("/library/formats"), [], []);
  const dup = async (f: Format) => { try { await api.post("/library/formats", { duplicate_of: f.id }); toast("Duplicated. Edit the copy to change it", "ok"); formats.reload(); } catch (e: any) { toast(e.message, "bad"); } };
  const [editing, setEditing] = useState<string | null>(null);
  const editF = formats.data.find((x) => x.id === editing);
  const groups: [string, string][] = [["video", "Video formats"], ["clip", "Clip layouts"]];
  return (
    <div className="flex flex-col gap-8">
      {editF && <DashGrid label="Edit format"><FormatEditor key={editF.id} f={editF} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); formats.reload(); }} /></DashGrid>}
      {groups.map(([k, label]) => {
        const list = formats.data.filter((f) => (k === "video" ? f.kind !== "clip" : f.kind === "clip"));
        return (
          <section key={k} aria-label={label}>
            <h2 className="mb-4 text-ds-xl leading-ds-tight font-semibold">{label}</h2>
            <DashGrid>
              {list.map((f) => {
                const wide = (f.canvas?.w || 9) > (f.canvas?.h || 16);
                return (
                  <Card as="article" key={f.id} padding="md" className={cx("flex gap-4 self-stretch", SPAN["2/2"])}>
                    <div className="grid h-[112px] w-[96px] shrink-0 place-items-center rounded-ds-lg bg-ds-sunken shadow-ds-inset-sm"><LayoutDiagram id={f.id} w={wide ? 84 : 48} h={wide ? 48 : 84} /></div>
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <h3 className="text-ds-md font-semibold">{f.name || f.id}</h3>
                      <p className="text-ds-2xs text-ds-muted ds-num">{f.canvas?.w} × {f.canvas?.h} · {f.canvas?.fps} fps</p>
                      <p className="text-ds-xs text-ds-text-2">{f.channels?.length ? `Used by ${f.channels.map(channelName).join(", ")}` : "Not used by a channel"}</p>
                      <div className="mt-auto -ml-2 flex gap-1 self-start"><Button size="sm" variant="ghost" leadingIcon="edit" onClick={() => setEditing(f.id)}>Edit</Button><Button size="sm" variant="ghost" leadingIcon="copy" onClick={() => dup(f)}>Duplicate</Button></div>
                    </div>
                  </Card>
                );
              })}
              {!list.length && <EmptyState icon="dashboard" className={SPAN["6/4"]} title="None yet" />}
            </DashGrid>
          </section>
        );
      })}
    </div>
  );
}

/** Edit form of one format: name, length, caption position and shot length. Saves with PUT /library/formats/{id}
 *  as a patch; a shipped preset is saved as the owner's own copy of the same id and the server validates the result. */
function FormatEditor({ f, onClose, onSaved }: { f: Format; onClose: () => void; onSaved: () => void }) {
  const { toast } = useApp();
  const d = f.definition || {};
  const [name, setName] = useState<string>(d.name ?? f.name ?? f.id);
  const [minS, setMinS] = useState(String(d.duration?.min_s ?? ""));
  const [maxS, setMaxS] = useState(String(d.duration?.max_s ?? ""));
  const [anchor, setAnchor] = useState(String(d.captions?.anchor_y_pct ?? ""));
  const [words, setWords] = useState(String(d.captions?.words_on_screen ?? ""));
  const [shotMin, setShotMin] = useState(String(d.shots?.min_s ?? ""));
  const [shotMax, setShotMax] = useState(String(d.shots?.max_s ?? ""));
  const [busy, setBusy] = useState(false); const [problems, setProblems] = useState<string[]>([]);
  const num = (v: string) => (v.trim() === "" || isNaN(Number(v)) ? undefined : Number(v));
  const save = async () => {
    const changes: Record<string, any> = { name: name.trim() || f.id };
    const dur = { min_s: num(minS), max_s: num(maxS) }; if (dur.min_s != null || dur.max_s != null) changes.duration = Object.fromEntries(Object.entries(dur).filter(([, v]) => v != null));
    const cap = { anchor_y_pct: num(anchor), words_on_screen: num(words) }; if (cap.anchor_y_pct != null || cap.words_on_screen != null) changes.captions = Object.fromEntries(Object.entries(cap).filter(([, v]) => v != null));
    const shots = { min_s: num(shotMin), max_s: num(shotMax) }; if (d.shots && (shots.min_s != null || shots.max_s != null)) changes.shots = Object.fromEntries(Object.entries(shots).filter(([, v]) => v != null));
    setBusy(true); setProblems([]);
    try { await api.put(`/library/formats/${encodeURIComponent(f.id)}`, { changes }); toast(f.source === "shipped" ? "Saved as your own version of this format" : "Format saved", "ok"); onSaved(); }
    catch (e: any) { setProblems([e.message]); toast("That format was not saved", "bad"); }
    setBusy(false);
  };
  const row = "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3";
  return (
    <Card as="section" aria-labelledby="fmt-edit" className={SPAN["6/4"]}>
      <CardHeader id="fmt-edit" title={`Edit ${f.name || f.id}`} actions={<Button size="sm" variant="ghost" leadingIcon="x" onClick={onClose}>Close</Button>} />
      <div className={row}>
        <Field label="Name" htmlFor="fmt-name"><Input id="fmt-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Shortest, seconds" htmlFor="fmt-min"><Input id="fmt-min" inputMode="numeric" value={minS} onChange={(e) => setMinS(e.target.value)} /></Field>
        <Field label="Longest, seconds" htmlFor="fmt-max"><Input id="fmt-max" inputMode="numeric" value={maxS} onChange={(e) => setMaxS(e.target.value)} /></Field>
        <Field label="Caption height, % from the top" htmlFor="fmt-anchor" hint="0 is the top edge, 100 the bottom"><Input id="fmt-anchor" inputMode="numeric" value={anchor} onChange={(e) => setAnchor(e.target.value)} /></Field>
        <Field label="Words on screen at once" htmlFor="fmt-words"><Input id="fmt-words" inputMode="numeric" value={words} onChange={(e) => setWords(e.target.value)} /></Field>
        {d.shots && <><Field label="Shortest shot, seconds" htmlFor="fmt-smin"><Input id="fmt-smin" inputMode="decimal" value={shotMin} onChange={(e) => setShotMin(e.target.value)} /></Field>
          <Field label="Longest shot, seconds" htmlFor="fmt-smax"><Input id="fmt-smax" inputMode="decimal" value={shotMax} onChange={(e) => setShotMax(e.target.value)} /></Field></>}
      </div>
      {problems.length > 0 && <p role="alert" className="mt-3 text-ds-sm text-ds-danger">{problems.join(". ")}</p>}
      <div className="mt-4 flex items-center gap-3">
        <Button leadingIcon="check" disabled={busy} onClick={save}>{busy ? "Saving" : "Save format"}</Button>
        <span className="text-ds-xs text-ds-muted">{f.source === "shipped" ? "A shipped format is saved as your own copy, so updates never overwrite it" : "Saved in your formats folder"}</span>
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- Footage */

function FootageTab() {
  const { confirm, toast } = useApp();
  const [q, setQ] = useState(""); const clips = useLoad<Footage[]>(() => api.get(`/library/footage?q=${encodeURIComponent(q)}`), [q], []);
  const [fetching, setFetching] = useState(false);
  const remove = async (c: Footage) => {
    if (!(await confirm({ title: "Remove this clip?", body: `A video that already used it is not changed; the clip is fetched again if a story needs it. ${MOVED}`, ok: "Remove", danger: true }))) return;
    try { await api.del(`/library/footage/${encodeURIComponent(c.source)}/${encodeURIComponent(c.clip_id)}`); toast("Clip removed", "ok"); clips.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const fetchNew = async () => {
    setFetching(true);
    try {
      const r = await api.post("/library/footage/fetch", { q: q.trim(), count: 2, orientation: "landscape" });
      const n = r?.clips?.length ?? 0;
      toast(n ? `${plural(n, "clip")} for "${q.trim()}" in the footage cache${r?.note ? `. ${r.note}` : ""}` : r?.note || "Nothing found", n ? "ok" : "warn");
      clips.reload();
    } catch (e: any) { toast(e.message, "bad"); }
    setFetching(false);
  };
  return (
    <DashGrid label="Footage">
      <div className={cx("flex flex-wrap items-center justify-between gap-3", SPAN["6/4"])}>
        <div className="flex flex-wrap items-center gap-2"><Search value={q} onChange={setQ} placeholder="Search footage by keyword" /><Button size="sm" variant="soft" leadingIcon="download" disabled={!q.trim() || fetching} onClick={fetchNew}>{fetching ? "Fetching" : "Fetch 2 clips"}</Button></div>
        <span className="text-ds-2xs text-ds-muted ds-num">{clips.data.length} clips cached</span>
      </div>
      {clips.data.length ? clips.data.map((c) => (
        <Card as="article" key={c.source + c.clip_id} padding="sm" className={cx("flex flex-col gap-3 self-stretch", SPAN["2/2"])}>
          <div className="relative aspect-[16/7] overflow-hidden rounded-ds-md bg-ds-media"><Scene seed={c.keywords.join(" ")} w={1600} h={700} className="size-full" /><Badge tone="overlay" size="xs" className="absolute right-2 bottom-2 ds-num">{fmtDur(c.duration)}</Badge></div>
          <div className="flex flex-col gap-2 px-1 pb-1">
            <h3 className="text-ds-md leading-ds-snug font-semibold">{c.keywords.join(", ")}</h3>
            <p className="flex flex-wrap gap-x-4 gap-y-1"><MetaItem icon="external"><span className="capitalize">{c.source}</span>&nbsp;#{c.clip_id}</MetaItem><MetaItem icon="refresh">{plural(c.uses, "use")}</MetaItem><MetaItem icon="calendarDays">{fmtDay(c.last_used)}</MetaItem></p>
            <p className="text-ds-2xs text-ds-muted">{c.license}</p>
            {c.local_path && <p className="text-ds-2xs text-ds-muted font-mono break-all">{c.local_path}</p>}
            <div><Button size="sm" variant="ghost" leadingIcon="trash" onClick={() => remove(c)}>Remove</Button></div>
          </div>
        </Card>
      )) : <EmptyState icon="film" className={SPAN["6/4"]} title="No footage cached yet">Clips are kept for 90 days after a video uses them</EmptyState>}
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Series */

const PART_LABEL: Record<string, [string, Tone]> = {
  planned: ["Planned", "neutral"], written: ["Written, waiting for your OK", "neutral"], approved: ["Approved", "accent"],
  producing: ["Being made", "accent"], produced: ["Made, in the Queue", "success"], published: ["Posted", "success"],
};
const NEXT_STEP: Record<string, string> = {
  concepts: "Ask the Assistant for concepts", choose: "Choose a concept", map: "Plan the parts", design: "Pick the look of the series",
  produce: "Produce the next part", "wait for production": "Waiting for the part being made", done: "All parts are made",
};
const nextWords = (s: string) => NEXT_STEP[s] ?? (s.startsWith("write part") ? `Write ${s.slice(6)}` : s.startsWith("approve part") ? `Read and approve ${s.slice(8)}` : s);

function Series() {
  const { ask, go, toast, channelName } = useApp();
  const list = useLoad<SeriesStory[]>(() => api.get("/library/series"), [], []);
  const [busy, setBusy] = useState<string | null>(null);
  const start = () => { ask({ page: "library", tab: "series" }, "Draft 3 story concepts for a series", false); go("#/assistant"); };
  const about = (s: SeriesStory) => { ask({ page: "library", tab: "series", story_slug: s.slug }, `What is the next step for the series ${s.title}?`, false); go("#/assistant"); };
  const produce = async (s: SeriesStory) => {
    setBusy(s.slug);
    try {
      const r = await api.post<{ queued: { part: number }[] }>(`/library/series/${encodeURIComponent(s.slug)}/produce`);
      toast(`Part ${r.queued.map((q) => q.part).join(", ")} of ${s.title} is queued. It lands in the Queue when it is made`, "ok");
      list.reload();
    } catch (e: any) { toast(e.message, "bad"); }
    setBusy(null);
  };
  const steps: [string, string][] = [["Concepts", "Three concepts from the Assistant; keep one or ask for a redo"], ["Part map", "Each part's hook, cliffhanger and length"], ["Scripts", "Full scripts and the Shorts cut from each part, for you to read"], ["Produce", "Approved parts are made one at a time and land in the Queue"]];
  return (
    <DashGrid label="Series">
      {list.data.map((s) => {
        const ready = s.parts.some((p) => p.status === "approved") && s.has_design;
        return (
          <Card as="article" key={s.slug} aria-labelledby={`ser-${s.slug}`} className={cx("self-stretch", SPAN["4/4"])}>
            <CardHeader id={`ser-${s.slug}`} title={s.title} adornment={<Badge tone={s.status === "done" ? "success" : "neutral"}>{s.status}</Badge>} />
            <p className="-mt-2 mb-3 text-ds-xs text-ds-muted">{channelName(s.channel)} · {plural(s.parts.length, "part")} · Next: {nextWords(s.next_step)}</p>
            {s.parts.length ? (
              <ol className="flex flex-col gap-2" aria-label={`Parts of ${s.title}`}>
                {s.parts.map((p) => {
                  const [label, tone] = PART_LABEL[p.status] || [p.status, "neutral" as Tone];
                  return (
                    <li key={p.n} className="flex flex-wrap items-center gap-3 rounded-ds-lg bg-ds-sunken px-4 py-3 shadow-ds-inset-sm">
                      <span className="grid size-7 shrink-0 place-items-center rounded-full bg-ds-accent-soft text-ds-xs font-semibold text-ds-accent-ink ds-num">{p.n}</span>
                      <span className="min-w-0 flex-1"><span className="block truncate text-ds-sm font-semibold">{p.title}</span><span className="block text-ds-2xs text-ds-muted">{p.minutes} min · {plural(p.shorts, "Short")} · {p.words.toLocaleString()} words</span></span>
                      <Badge tone={tone}>{label}</Badge>
                    </li>
                  );
                })}
              </ol>
            ) : <p className="text-ds-sm text-ds-text-2">No parts planned yet. The Assistant plans them with you</p>}
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <Button leadingIcon="play" disabled={!ready || busy === s.slug} onClick={() => produce(s)}>{busy === s.slug ? "Queuing" : "Produce next part"}</Button>
              <Button variant="soft" leadingIcon="chat" onClick={() => about(s)}>Ask the Assistant</Button>
              {!ready && <span className="text-ds-xs text-ds-muted">{s.has_design ? "Approve a written part first" : "Choose the look of the series first"}</span>}
            </div>
          </Card>
        );
      })}
      <Card as="section" aria-labelledby="s-how" className={cx("self-stretch", SPAN["4/4"])}>
        <CardHeader id="s-how" title="How a series is made" />
        <ol className="grid grid-cols-1 gap-3 @container sm:grid-cols-2">
          {steps.map(([t, s], i) => (
            <li key={t} className="flex gap-3 rounded-ds-lg bg-ds-sunken p-4 shadow-ds-inset-sm">
              <span className="grid size-7 shrink-0 place-items-center rounded-full bg-ds-accent-soft text-ds-xs font-semibold text-ds-accent-ink ds-num">{i + 1}</span>
              <span><span className="block text-ds-sm font-semibold">{t}</span><span className="block text-ds-xs leading-ds-normal text-ds-text-2">{s}</span></span>
            </li>
          ))}
        </ol>
        {!list.data.length && !list.loading && <EmptyState icon="integrations" className="mt-5" title="No series yet" action={<Button leadingIcon="chat" onClick={start}>Draft a series with the Assistant</Button>}>Series you develop with the Assistant show up here with each part's status</EmptyState>}
      </Card>
      <PromoCard className={cx("self-stretch", SPAN["2/4"])} illustration={<SeriesIllustration />} title="Start a series" description="Tell the Assistant the premise. It drafts three concepts you can keep or redo" ctaLabel="Open the Assistant" onCta={start} />
    </DashGrid>
  );
}
