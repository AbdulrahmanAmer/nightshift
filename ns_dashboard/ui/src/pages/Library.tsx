/* Library: one NavTabs row per bank, then the bank's items as cards in the 6-column grid (3 across, 2 at 4 columns).
   Voices gets the full treatment: voice cards with Waveform + TransportControls, and a maker card with a KnobSwitch
   (Design / Clone) and an IntakeStepper for the clone steps. Design and clone endpoints are not in the backend yet:
   the steps say so and call the typed stubs in lib/api (never a fake success). All other calls are unchanged. */
import { useEffect, useRef, useState } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtDay, fmtDur, plural, store } from "../lib/format";
import type { Footage, Format, Idea, Story, Voice } from "../lib/types";
import { cx } from "../ui/kit";
import { Scene } from "../visuals/Scene";
import { LayoutDiagram } from "../visuals/charts";
import {
  Badge, Button, Card, CardHeader, DashGrid, DropTile, EmptyState, Field, Icon, Input, IntakeStepper, KnobSwitch, MetaItem, NavTabs, PromoCard,
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
  const { status, toast, ask, channelName } = useApp(); const chans = status?.channels || [];
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
    try { await api.put(`/library/ideas/${encodeURIComponent(String(i.id))}`, { status: s }); toast(s === "approved" ? "Approved. It will be used on the next run" : s === "rejected" ? "Dropped" : "Back to new", "ok"); ideas.reload(); } catch (e: any) { toast(e.message, "bad"); }
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
          <Card as="article" key={i.id} padding="md" className={cx("flex flex-col gap-3 self-stretch", SPAN["2/2"])}>
            <header className="flex items-center justify-between gap-2"><Badge tone={IDEA_TONE[s] || "neutral"}>{IDEA_LABEL[s] || s}</Badge><span className="truncate text-ds-2xs font-semibold text-ds-text-2">{channelName(i.channel)}</span></header>
            <div><h3 className="text-ds-md leading-ds-snug font-semibold">{i.title || "Untitled idea"}</h3><p className="mt-1 text-ds-sm leading-ds-normal text-ds-text-2">{i.premise}</p></div>
            <p className="text-ds-2xs text-ds-muted">{sourceLabel(i.source)}{i.created_at ? ` · ${fmtDay(i.created_at)}` : ""}</p>
            <div className="mt-auto flex flex-wrap gap-2 pt-1">
              {s === "new" && <Button size="sm" onClick={() => setStatus(i, "approved")}>Approve for production</Button>}
              {(s === "new" || s === "approved") && <Button size="sm" variant="ghost" onClick={() => setStatus(i, "rejected")}>Drop</Button>}
              {s === "rejected" && <Button size="sm" variant="soft" leadingIcon="restore" onClick={() => setStatus(i, "new")}>Restore</Button>}
              <Button size="sm" variant="ghost" leadingIcon="chat" onClick={() => ask({ page: "library", idea_id: i.id }, `Develop this idea into a story for ${channelName(i.channel)}: ${i.title}. ${i.premise}`)}>Develop with Assistant</Button>
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
  const { channelName, ask } = useApp(); const [q, setQ] = useState("");
  const stories = useLoad<Story[]>(() => api.get(`/library/stories?q=${encodeURIComponent(q)}`), [q], []);
  const st: Record<string, [string, Tone]> = { pending: ["Waiting for review", "accent"], posted: ["Posted", "success"], approved: ["Approved", "success"], rejected: ["Rejected", "danger"] };
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
              <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1"><MetaItem icon="file">{s.words.toLocaleString("en-US")} words</MetaItem><MetaItem icon="target">Critic {s.critic_avg.toFixed(1)}</MetaItem><MetaItem icon="film">{plural(s.outputs, "video")}</MetaItem></p>
            </div>
            <div className="mt-auto flex gap-1 px-1 pb-1">
              <Button size="sm" variant="soft" onClick={() => { location.hash = `#/?tab=${s.status === "pending" ? "ready" : s.status === "rejected" ? "rejected" : "posted"}`; }}>Open</Button>
              <Button size="sm" variant="ghost" leadingIcon="restore" onClick={() => ask({ page: "library", story_id: s.job_id }, `Reuse the story "${s.title}" as the start of a new video`)}>Reuse</Button>
            </div>
          </Card>
        );
      }) : <EmptyState icon="book" className={SPAN["6/4"]} title="No stories match" />}
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Voices */

const modeLabel = (m: string) => (m === "preset" ? "Built-in" : m === "design" ? "Designed" : "Cloned");

function VoiceCard({ v, active, onPlay }: { v: Voice; active: boolean; onPlay: (name: string | null) => void }) {
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
      const url = await api.voicePreview(v.name, SAMPLE_LINE);
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
    </Card>
  );
}

function Voices() {
  const { ask, toast } = useApp(); const voices = useLoad<Voice[]>(() => api.get("/voices"), [], []);
  const [playing, setPlaying] = useState<string | null>(null);
  const [clone, setClone] = useState(false);
  const [desc, setDesc] = useState(""); const [designName, setDesignName] = useState("");
  const [step, setStep] = useState(1);
  const [sample, setSample] = useState<File | null>(null); const [name, setName] = useState(""); const [line, setLine] = useState(SAMPLE_LINE);
  const ready = api.voiceMakingReady;
  const canNext = step === 1 ? !!sample : step === 2 ? !!name.trim() : step === 3 ? !!line.trim() : false;
  const save = async () => { try { await api.voiceClone(sample!, name.trim(), line.trim()); toast("Voice saved", "ok"); voices.reload(); } catch (e: any) { toast(e.message, "warn"); } };
  const design = async () => { try { await api.voiceDesign(designName.trim() || "New voice", desc.trim()); voices.reload(); } catch (e: any) { toast(e.message, "warn"); } };
  const CLONE_STEPS = ["Upload a sample", "Name it", "Test line", "Save"];

  return (
    <DashGrid label="Voices">
      <div className={cx("grid grid-cols-1 content-start gap-5 @min-[600px]:grid-cols-2", SPAN["4/4"])}>
        {voices.data.map((v) => <VoiceCard key={v.name} v={v} active={playing === v.name} onPlay={setPlaying} />)}
        {!voices.data.length && !voices.loading && <EmptyState icon="voice" className="@min-[600px]:col-span-2" title="No voices yet" />}
      </div>
      <Card as="section" aria-labelledby="v-new" className={cx("flex flex-col gap-5", SPAN["2/4"])}>
        <CardHeader id="v-new" title="Make a new voice" className="!mb-0" />
        <KnobSwitch label="Clone from a sample instead of designing" checked={clone} onChange={setClone} offLabel="Design" onLabel="Clone" offIcon="wand" onIcon="mic" />
        {!ready && (
          <p role="note" className="flex gap-2 rounded-ds-md bg-ds-accent-subtle px-3 py-2.5 text-ds-xs leading-ds-normal text-ds-text-2 shadow-[inset_0_0_0_1px_var(--ds-border-dashed)]">
            <Icon name="clock" size={16} className="mt-px shrink-0 text-ds-accent-ink" />Designing and cloning voices on this PC are coming in the next update. You can fill the steps now; saving waits for the engine to support it
          </p>
        )}
        {!clone ? (
          <div className="flex flex-col gap-4">
            <Field label="Describe the voice" htmlFor="voice-desc" hint="The first one downloads the voice design model, about 2 GB"><TextArea id="voice-desc" rows={3} placeholder="Deep, calm older man, slow, a little raspy" value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
            <Field label="Name" htmlFor="voice-dname"><Input id="voice-dname" placeholder="Hollow Porch" value={designName} onChange={(e) => setDesignName(e.target.value)} /></Field>
            <div className="flex flex-wrap gap-2">
              <Button leadingIcon="wand" disabled={!desc.trim() || !ready} onClick={design}>Design it</Button>
              <Button variant="soft" leadingIcon="chat" disabled={!desc.trim()} onClick={() => { ask({ page: "library", tab: "voices" }, `Design a new narrator voice: ${desc.trim()}`); setDesc(""); }}>Ask the Assistant</Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <IntakeStepper bare title={CLONE_STEPS[step - 1]} caption={`Step ${step} of 4`} value={step} max={4} onChange={setStep} decLabel="Previous step" incLabel="Next step" canIncrement={canNext} />
            {step === 1 && (sample
              ? <div className="flex items-center gap-3 rounded-ds-lg bg-ds-sunken p-3 shadow-ds-inset-sm"><Icon name="voice" size={18} className="text-ds-accent-ink" /><span className="min-w-0 flex-1 truncate text-ds-sm font-semibold">{sample.name}</span><Button size="sm" variant="ghost" leadingIcon="x" onClick={() => setSample(null)}>Change</Button></div>
              : <DropTile compact title="Drop a 3 to 10 second sample" sub="A clean recording of a voice you have the rights to" accept="audio/*" onFile={setSample} />)}
            {step === 2 && <Field label="Name" htmlFor="voice-cname" hint="How it shows in Channels and here"><Input id="voice-cname" placeholder="My narrator" value={name} onChange={(e) => setName(e.target.value)} /></Field>}
            {step === 3 && <Field label="Test line" htmlFor="voice-line" hint="Nightshift reads this in the new voice before you save"><TextArea id="voice-line" rows={3} value={line} onChange={(e) => setLine(e.target.value)} /></Field>}
            {step === 4 && (
              <div className="flex flex-col gap-3">
                <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 text-ds-sm"><dt className="text-ds-muted">Sample</dt><dd className="truncate font-semibold">{sample?.name}</dd><dt className="text-ds-muted">Name</dt><dd className="truncate font-semibold">{name}</dd></dl>
                <Button leadingIcon="check" disabled={!ready} onClick={save}>Save voice</Button>
                {!ready && <p className="text-ds-2xs text-ds-muted">Saving is switched off until cloning arrives in the next update</p>}
              </div>
            )}
            {step < 4 && <Button variant="soft" trailingIcon="chevronRight" className="self-end" disabled={!canNext} onClick={() => setStep(step + 1)}>Next</Button>}
          </div>
        )}
      </Card>
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Formats */

function Formats() {
  const { toast, channelName } = useApp(); const formats = useLoad<Format[]>(() => api.get("/library/formats"), [], []);
  const dup = async (f: Format) => { try { await api.post("/library/formats", { ...f, id: f.id + "_copy", name: (f.name || f.id) + " copy", channels: [] }); toast("Duplicated. Edit the preset file to change it", "ok"); formats.reload(); } catch (e: any) { toast(e.message, "bad"); } };
  const groups: [string, string][] = [["video", "Video formats"], ["clip", "Clip layouts"]];
  return (
    <div className="flex flex-col gap-8">
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
                      <Button size="sm" variant="ghost" className="mt-auto -ml-2 self-start" leadingIcon="copy" onClick={() => dup(f)}>Duplicate</Button>
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

/* ---------------------------------------------------------------- Footage */

function FootageTab() {
  const [q, setQ] = useState(""); const clips = useLoad<Footage[]>(() => api.get(`/library/footage?q=${encodeURIComponent(q)}`), [q], []);
  return (
    <DashGrid label="Footage">
      <div className={cx("flex flex-wrap items-center justify-between gap-3", SPAN["6/4"])}><Search value={q} onChange={setQ} placeholder="Search footage by keyword" /><span className="text-ds-2xs text-ds-muted ds-num">{clips.data.length} clips cached</span></div>
      {clips.data.length ? clips.data.map((c) => (
        <Card as="article" key={c.source + c.id} padding="sm" className={cx("flex flex-col gap-3 self-stretch", SPAN["2/2"])}>
          <div className="relative aspect-[16/7] overflow-hidden rounded-ds-md bg-ds-media"><Scene seed={c.keywords.join(" ")} w={1600} h={700} className="size-full" /><Badge tone="overlay" size="xs" className="absolute right-2 bottom-2 ds-num">{fmtDur(c.duration)}</Badge></div>
          <div className="flex flex-col gap-2 px-1 pb-1">
            <h3 className="text-ds-md leading-ds-snug font-semibold">{c.keywords.join(", ")}</h3>
            <p className="flex flex-wrap gap-x-4 gap-y-1"><MetaItem icon="external"><span className="capitalize">{c.source}</span>&nbsp;#{c.id}</MetaItem><MetaItem icon="refresh">{plural(c.uses, "use")}</MetaItem><MetaItem icon="calendarDays">{fmtDay(c.last_used)}</MetaItem></p>
            <p className="text-ds-2xs text-ds-muted">{c.license}</p>
          </div>
        </Card>
      )) : <EmptyState icon="film" className={SPAN["6/4"]} title="No footage cached yet">Clips are kept for 90 days after a video uses them</EmptyState>}
    </DashGrid>
  );
}

/* ---------------------------------------------------------------- Series */

function Series() {
  const { ask, go } = useApp();
  const start = () => { ask({ page: "library", tab: "series" }, "Draft 3 story concepts for a series", false); go("#/assistant"); };
  const steps: [string, string][] = [["Concepts", "Three concepts from the Assistant; keep one or ask for a redo"], ["Part map", "Each part's hook, cliffhanger and length"], ["Scripts", "Full scripts and the Shorts cut from each part, for you to read"], ["Produce", "Approved parts are made one a week and land in the Queue"]];
  return (
    <DashGrid label="Series">
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
        <EmptyState icon="integrations" className="mt-5" title="No series yet" action={<Button leadingIcon="chat" onClick={start}>Draft a series with the Assistant</Button>}>Series you develop show up here with each part's status. The series workshop files arrive with the next update; until then the Assistant drafts concepts and saves them to Ideas</EmptyState>
      </Card>
      <PromoCard className={cx("self-stretch", SPAN["2/4"])} illustration={<SeriesIllustration />} title="Start a series" description="Tell the Assistant the premise. It drafts three concepts you can keep or redo" ctaLabel="Open the Assistant" onCta={start} />
    </DashGrid>
  );
}
