/* Create: a six-step wizard in one card (StepRail + IntakeStepper), with the job cards beside it.
     6 columns  wizard x4 | working now x2, earlier x2 (stacked)
                finished clips x6
   Same fields and calls as before: POST /jobs/run {channel, format}; POST /jobs/clip as an upload (FormData) or JSON
   with a link, with channel, template, max_clips, min_s, max_s, caption_style, rights_confirmed. Polling unchanged. */
import { useEffect, useState, type ReactNode } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { CLIP_STAGES, dayInline, fmtClock, fmtDur, fmtWhen, POOL_LABELS, RUN_STAGES, stageWords, store } from "../lib/format";
import type { ChannelSummary, Idea, Job, Output, Template } from "../lib/types";
import { cx } from "../ui/kit";
import { LayoutDiagram } from "../visuals/charts";
import {
  AppGrid, Badge, Button, Card, CardHeader, Checkbox, ChoiceChip, DashGrid, DayChips, DropTile, EmptyState, Field, Icon, Input, IntakeStepper,
  LinkButton, MetaItem, NotificationItem, ProgressBar, SegmentedControl, Select, SPAN, StatusDot, StepRail, TimeChip, type SegmentOption,
} from "../components/ref12";
import { Poster, jobTitle } from "./Queue";

type Kind = "story" | "clips";
const STEPS: Record<Kind, { title: string; sub: string }[]> = {
  story: [{ title: "What to make", sub: "A story or clips" }, { title: "Format", sub: "Full video or one Short" }, { title: "Channel and idea", sub: "Who it is for" }, { title: "Voice", sub: "The narrator" }, { title: "When", sub: "Now or at a time" }, { title: "Review", sub: "Check and start" }],
  clips: [{ title: "What to make", sub: "A story or clips" }, { title: "Layout", sub: "How clips are framed" }, { title: "Your video", sub: "File or link" }, { title: "Options", sub: "Count, length, captions" }, { title: "When", sub: "Now or at a time" }, { title: "Review", sub: "Rights and start" }],
};
const isClipChannel = (c: ChannelSummary) => /stream|clip|podcast/i.test(c.niche || "");
const voiceOf = (c?: ChannelSummary) => (!c ? "" : typeof c.voice === "string" ? c.voice : c.voice?.name || c.voice?.speaker || "Built-in voice");

export default function Create() {
  const { status, route, toast, refresh, channelName } = useApp();
  const chans = status?.channels || [];
  const [kind, setKind] = useState<Kind>((route.query.get("kind") as Kind) || store.get<Kind>("ns-create-kind", "story"));
  const jobs = useLoad<Job[]>(() => api.get("/jobs"), [], []);
  const clips = useLoad<Output[]>(() => api.get("/outputs?status=pending"), [], []);
  const running = jobs.data.filter((j) => ["queued", "running"].includes(j.status));
  useEffect(() => {
    const t = setInterval(() => { jobs.reload(); if (running.length) clips.reload(); }, running.length ? 1500 : 6000);
    return () => clearInterval(t);
  }, [running.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const cancel = async (id: string) => { try { await api.post(`/jobs/${encodeURIComponent(id)}/cancel`); toast("Cancelling", "warn"); jobs.reload(); } catch (e: any) { toast(e.message, "bad"); } };
  const finishedClips = clips.data.filter((o) => o.kind === "clip");
  const earlier = jobs.data.filter((j) => !["queued", "running"].includes(j.status)).slice(0, 6);

  // wizard state: every field from the previous StoryFlow and ClipFlow
  const [step, setStep] = useState(route.query.get("kind") ? 1 : 0);
  const [reached, setReached] = useState(step);
  const [storyChannel, setStoryChannel] = useState(chans.find((c) => c.status === "active")?.slug || chans[0]?.slug || "");
  const [format, setFormat] = useState<"master_first" | "shorts_only">("master_first");
  const [busy, setBusy] = useState(false);
  const templates = useLoad<Template[]>(() => api.get("/templates"), [], []);
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [clipChannel, setClipChannel] = useState(chans.find(isClipChannel)?.slug || chans[0]?.slug || "");
  const [template, setTemplate] = useState("auto");
  const [maxClips, setMaxClips] = useState(6); const [minS, setMinS] = useState(20); const [maxS, setMaxS] = useState(60);
  const [caption, setCaption] = useState("punch_yellow");
  const [rights, setRights] = useState(false);
  const [upload, setUpload] = useState<number | null>(null);
  // When: "now" sends schedule_at null; "later" sends the picked local time as an ISO instant (UTC, with Z).
  const [whenMode, setWhenMode] = useState<"now" | "later">("now"); const [dayOff, setDayOff] = useState(() => (new Date().getHours() === 23 ? 1 : 0)); const [time, setTime] = useState(nextHour);
  const pickedAt = whenMode === "later" ? localTime(dayOff, time) : null;
  const pastPick = whenMode === "later" && (!pickedAt || pickedAt.getTime() <= Date.now() + 60_000);
  const scheduleIso = pickedAt && !pastPick ? pickedAt.toISOString() : null;
  const hasSource = !!file || /^https?:\/\/\S+\.\S+/.test(url.trim());
  const channel = kind === "story" ? storyChannel : clipChannel;

  const pick = (k: Kind) => { setKind(k); store.set("ns-create-kind", k); };
  const go = (i: number) => { const n = Math.max(0, Math.min(5, i)); setStep(n); setReached((r) => Math.max(r, n)); };
  const canNext = step === 2 ? (kind === "story" ? !!storyChannel : hasSource && !!clipChannel) : step === 4 ? !pastPick : true;
  const started = () => { toast(scheduleIso ? `Scheduled for ${fmtWhen(scheduleIso)}. You can leave this page` : "Started. You can leave this page", "ok"); setWhenMode("now"); jobs.reload(); refresh(); go(0); setReached(0); };

  const startStory = async () => { setBusy(true); try { await api.post("/jobs/run", { channel: storyChannel, format, schedule_at: scheduleIso }); started(); } catch (e: any) { toast(e.message, "bad"); } setBusy(false); };
  const startClips = async () => {
    try {
      if (file) {
        const fd = new FormData(); Object.entries({ channel: clipChannel, template, max_clips: maxClips, min_s: minS, max_s: maxS, caption_style: caption, rights_confirmed: "true" }).forEach(([k, v]) => fd.append(k, String(v))); if (scheduleIso) fd.append("schedule_at", scheduleIso); fd.append("file", file, file.name);
        setUpload(0); await api.upload("/jobs/clip", fd, setUpload); setFile(null);
      } else {
        await api.post("/jobs/clip", { url: url.trim(), channel: clipChannel, template, max_clips: maxClips, min_s: minS, max_s: maxS, caption_style: caption, rights_confirmed: true, schedule_at: scheduleIso }); setUrl("");
      }
      setRights(false); started();
    } catch (e: any) { toast(e.message, "bad"); }
    setUpload(null);
  };

  const steps = STEPS[kind];
  const body: ReactNode = (() => {
    if (step === 0) return (
      <AppGrid bare choice label="What to make" apps={[
        { id: "story", icon: "notes", label: "A new story video", caption: "An original story for one of your channels, narrated and edited with footage and captions", selected: kind === "story", onClick: () => pick("story") },
        { id: "clips", icon: "scissors", label: "Clips from my own video", caption: "Drop a stream or podcast, or paste a link. Nightshift finds the best moments", selected: kind === "clips", onClick: () => pick("clips") },
      ]} />
    );
    if (kind === "story") {
      if (step === 1) return (
        <AppGrid bare choice label="Format" apps={[
          { id: "master_first", icon: "monitor", label: "Full video + Shorts", caption: "A 10 to 20 minute story with chapters, plus 4 to 6 Shorts cut from it. About an hour", selected: format === "master_first", onClick: () => setFormat("master_first") },
          { id: "shorts_only", icon: "phone", label: "One Short", caption: "A single short story as a vertical video. A few minutes", selected: format === "shorts_only", onClick: () => setFormat("shorts_only") },
        ]} />
      );
      if (step === 2) return <div className="flex flex-col gap-5"><ChannelPick value={storyChannel} onChange={setStoryChannel} /><IdeasFor channel={storyChannel} /></div>;
      if (step === 3) return <VoiceStep channel={chans.find((c) => c.slug === storyChannel)} />;
    } else {
      if (step === 1) return (
        <div className="flex flex-col gap-3">
          <AppGrid bare choice label="Layout" apps={templates.data.map((t) => ({ id: t.id, icon: "dashboard", art: <LayoutDiagram id={t.id} w={32} h={56} />, label: t.label, selected: template === t.id, onClick: () => setTemplate(t.id) }))} />
          <p className="text-ds-xs text-ds-text-2">{templates.data.find((t) => t.id === template)?.description}</p>
        </div>
      );
      if (step === 2) return (
        <div className="flex flex-col gap-5">
          {file ? (
            <div className="flex items-center gap-3 rounded-ds-lg bg-ds-surface p-3 shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)]">
              <span aria-hidden className="grid size-10 place-items-center rounded-ds-md bg-ds-accent-soft text-ds-accent-ink"><Icon name="fileVideo" size={18} /></span>
              <div className="min-w-0 flex-1"><p className="truncate text-ds-sm font-semibold">{file.name}</p><p className="text-ds-2xs text-ds-muted ds-num">{(file.size / 1e9).toFixed(2)} GB</p></div>
              <Button size="sm" variant="ghost" leadingIcon="x" onClick={() => setFile(null)}>Change</Button>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <DropTile compact title="Drop a video here" sub="A stream, podcast or recording you own. MP4, MKV or MOV, up to 4 hours" accept="video/*,.mkv" onFile={setFile} />
              <div className="flex items-center gap-3 text-ds-2xs text-ds-muted"><span className="h-px flex-1 bg-ds-border" />or paste a link<span className="h-px flex-1 bg-ds-border" /></div>
              <Input aria-label="YouTube link" type="url" placeholder="https://youtu.be/" value={url} onChange={(e) => setUrl(e.target.value)} />
            </div>
          )}
          <div><p className="mb-2 text-ds-sm font-semibold text-ds-text-2">Channel</p><ChannelPick value={clipChannel} onChange={setClipChannel} /></div>
        </div>
      );
      if (step === 3) return (
        <div className="flex flex-col gap-6">
          <Field label="How many clips, at most"><DayChips label="How many clips, at most" days={Array.from({ length: 10 }, (_, i) => ({ value: i + 1, label: String(i + 1), ariaLabel: `${i + 1} clips` }))} selected={[maxClips]} onToggle={setMaxClips} /></Field>
          <div className="flex flex-wrap gap-x-8 gap-y-4">
            <Stepper label="Shortest" value={minS} onChange={(v) => setMinS(Math.min(v, maxS - 5))} min={10} max={120} suffix=" s" />
            <Stepper label="Longest" value={maxS} onChange={(v) => setMaxS(Math.max(v, minS + 5))} min={15} max={179} suffix=" s" />
          </div>
          <Field label="Caption look"><div className="flex flex-wrap gap-2">{Object.entries(POOL_LABELS.caption_styles.opts).map(([k, l]) => <ChoiceChip key={k} on={caption === k} onChange={() => setCaption(k)}>{l}</ChoiceChip>)}</div></Field>
        </div>
      );
    }
    if (step === 4) return <WhenStep kind={kind} channel={chans.find((c) => c.slug === channel)} file={file} mode={whenMode} onMode={setWhenMode} dayOff={dayOff} onDay={setDayOff} time={time} onTime={setTime} past={pastPick} />;
    // review
    const ch = channelName(channel);
    const rows: [string, ReactNode][] = kind === "story"
      ? [["Make", format === "master_first" ? "Full video + Shorts" : "One Short"], ["Channel", ch || "None picked"], ["Voice", voiceOf(chans.find((c) => c.slug === storyChannel))], ["Starts", scheduleIso ? fmtWhen(scheduleIso) : "Now, after the model loads"]]
      : [["Video", file ? file.name : url.trim() || "None yet"], ["Channel", ch || "None picked"], ["Layout", templates.data.find((t) => t.id === template)?.label || template], ["Clips", `Up to ${maxClips}, ${minS} to ${maxS} s`], ["Captions", POOL_LABELS.caption_styles.opts[caption] || caption], ["Starts", scheduleIso ? fmtWhen(scheduleIso) : "After the upload"]];
    return (
      <div className="flex flex-col gap-5">
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-3 rounded-ds-lg bg-ds-sunken p-4 shadow-ds-inset-sm">
          {rows.map(([k, v]) => <div key={k} className="contents"><dt className="text-ds-xs text-ds-muted">{k}</dt><dd className="min-w-0 truncate text-ds-sm font-semibold">{v}</dd></div>)}
        </dl>
        {kind === "clips" && <Checkbox checked={rights} onChange={setRights}>I own this video or have permission to use it</Checkbox>}
        <div className="flex flex-wrap items-center gap-3">
          {kind === "story"
            ? <Button size="lg" onClick={startStory} disabled={!storyChannel || busy || pastPick}>{scheduleIso ? "Schedule" : "Start"}</Button>
            : <Button size="lg" onClick={startClips} disabled={!hasSource || !rights || !clipChannel || upload != null || pastPick}>{scheduleIso ? "Schedule" : "Start"}</Button>}
          {kind === "story" ? <span className="text-ds-xs text-ds-muted">The engine loads its model first, which takes about a minute</span>
            : upload != null ? <ProgressBar value={upload} label="Upload" className="w-56" />
            : !hasSource ? <span className="text-ds-xs text-ds-muted">Add a video or a link first</span>
            : !rights ? <span className="text-ds-xs text-ds-muted">Confirm the rights to start</span> : null}
        </div>
      </div>
    );
  })();

  return (
    <div className="enter">
      <DashGrid label="Create">
        <Card as="section" aria-labelledby="c-wiz" className={cx("@container", SPAN["4/4"])}>
          <CardHeader id="c-wiz" title={kind === "story" ? "New story video" : "Clips from my video"} actions={
            <IntakeStepper bare title={<span className="sr-only">Step</span>} value={step + 1} max={6} onChange={(v) => go(v - 1)} decLabel="Previous step" incLabel="Next step" canIncrement={canNext} />
          } />
          <div className="grid grid-cols-1 gap-6 @min-[720px]:grid-cols-[220px_minmax(0,1fr)]">
            <StepRail label="Steps" steps={steps} current={step} reached={reached} onPick={go} className="hidden @min-[720px]:flex" />
            <div className="flex min-w-0 flex-col gap-5">
              <div>
                <p className="text-ds-2xs text-ds-muted ds-num">Step {step + 1} of 6</p>
                <h3 className="text-ds-lg leading-ds-snug font-semibold">{steps[step]!.title}</h3>
              </div>
              {body}
              {step < 5 && (
                <div className="flex items-center justify-between gap-3 border-t border-ds-border-subtle pt-4">
                  <Button variant="ghost" leadingIcon="chevronLeft" disabled={step === 0} onClick={() => go(step - 1)}>Back</Button>
                  <Button trailingIcon="chevronRight" disabled={!canNext} onClick={() => go(step + 1)}>Next</Button>
                </div>
              )}
            </div>
          </div>
        </Card>

        <div className={cx("flex flex-col gap-5", SPAN["2/4"])}>
          <Card as="section" aria-labelledby="c-now">
            <CardHeader id="c-now" title="Working now" adornment={running.length ? <StatusDot tone="accent" label="Running" /> : undefined} />
            {running.length ? <ul className="flex flex-col gap-3">{running.map((j) => <JobRow key={j.job_id} j={j} channelName={channelName} onCancel={cancel} />)}</ul>
              : <p className="text-ds-sm text-ds-text-2">Nothing running. Tonight's run starts at <span className="ds-num">{fmtClock(status?.next_run) || "03:00"}</span></p>}
          </Card>
          <Card as="section" aria-labelledby="c-earlier">
            <CardHeader id="c-earlier" title="Earlier" />
            {earlier.length ? <ul className="flex flex-col gap-3">{earlier.map((j) => <EarlierRow key={j.job_id} j={j} channelName={channelName} templates={templates.data} onChanged={jobs.reload} />)}</ul> : <p className="text-ds-sm text-ds-text-2">No earlier jobs</p>}
          </Card>
        </div>

        <Card as="section" aria-labelledby="c-clips" className={SPAN["6/4"]}>
          <CardHeader id="c-clips" title="Finished clips" actions={<LinkButton href="#/" variant="ghost" size="sm" trailingIcon="chevronRight">Review in the Queue</LinkButton>} />
          {finishedClips.length ? (
            <ul className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-4">
              {finishedClips.map((o) => (
                <li key={o.output_id}>
                  <a href={`#/?id=${encodeURIComponent(o.output_id)}`} className="ds-focus flex flex-col gap-2 rounded-ds-lg bg-ds-surface p-2 text-ds-text no-underline shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)] transition-[box-shadow,transform] duration-200 ease-ds-out hover:-translate-y-0.5 hover:shadow-ds-2">
                    <div className="relative aspect-[9/16] overflow-hidden rounded-ds-sm bg-ds-media"><Poster o={o} className="size-full" /><Badge tone="overlay" size="xs" className="absolute right-2 bottom-2 ds-num">{fmtDur(o.duration_s)}</Badge></div>
                    <p className="line-clamp-2 px-1 pb-1 text-ds-xs leading-ds-snug font-semibold">{o.title}</p>
                  </a>
                </li>
              ))}
            </ul>
          ) : <EmptyState icon="scissors" title="No clips waiting">Clips you make from your own videos show up here and in the Queue</EmptyState>}
        </Card>
      </DashGrid>
    </div>
  );
}

function ChannelPick({ value, onChange }: { value: string; onChange: (s: string) => void }) {
  const { status } = useApp(); const chans = status?.channels || [];
  if (!chans.length) return <EmptyState icon="channels" title="No channels yet" action={<LinkButton href="#/channels?edit=new" variant="primary">Add your first channel</LinkButton>} />;
  return <AppGrid bare choice label="Channel" apps={chans.map((c) => ({ id: c.slug, icon: isClipChannel(c) ? "scissors" : "channels", label: c.display_name, caption: c.status === "active" ? `Voice ${voiceOf(c)}` : "Paused", selected: value === c.slug, onClick: () => onChange(c.slug) }))} />;
}

/** Approved ideas for the channel are drawn first (§20.2); this lists them so the owner knows which story comes next. */
function IdeasFor({ channel }: { channel: string }) {
  const ideas = useLoad<Idea[]>(() => api.get("/library/ideas"), [], []);
  const mine = ideas.data.filter((i) => i.channel === channel && i.status === "approved");
  return (
    <div className="rounded-ds-lg bg-ds-sunken p-4 shadow-ds-inset-sm">
      <p className="text-ds-sm font-semibold">Story source</p>
      {mine.length ? (
        <>
          <p className="mt-1 text-ds-xs text-ds-text-2">Approved ideas are used first, in this order</p>
          <ol className="mt-3 flex flex-col gap-2">{mine.map((i) => <li key={i.id} className="flex items-start gap-2 text-ds-sm"><Icon name="lightbulb" size={16} className="mt-0.5 shrink-0 text-ds-accent-ink" /><span><span className="font-semibold">{i.title}</span><span className="text-ds-text-2">. {i.premise}</span></span></li>)}</ol>
        </>
      ) : <p className="mt-1 text-ds-xs text-ds-text-2">No approved idea for this channel, so Nightshift writes a new one. <a href="#/library?tab=ideas" className="font-semibold text-ds-accent-ink">Approve one in Library</a></p>}
    </div>
  );
}

function VoiceStep({ channel }: { channel?: ChannelSummary }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-4 rounded-ds-lg bg-ds-surface p-4 shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)]">
        <span aria-hidden className="grid size-12 place-items-center rounded-full bg-ds-accent-soft text-ds-accent-ink"><Icon name="voice" size={22} /></span>
        <div className="min-w-0 flex-1"><p className="text-ds-md font-semibold">{voiceOf(channel) || "No channel picked"}</p><p className="text-ds-xs text-ds-text-2">{channel ? `The narrator for ${channel.display_name}` : "Pick a channel first"}</p></div>
        {channel && <LinkButton href={`#/channels?edit=${encodeURIComponent(channel.slug)}`} variant="soft" size="sm" leadingIcon="edit">Change in Channels</LinkButton>}
      </div>
      <p className="text-ds-xs text-ds-text-2">Each story uses its channel's narrator. New voices are made in <a href="#/library?tab=voices" className="font-semibold text-ds-accent-ink">Library, Voices</a></p>
    </div>
  );
}

/** Default for the time box: the next full hour, as "HH:MM" (a time that has not passed yet today). */
const nextHour = () => { const d = new Date(); d.setHours(d.getHours() + 1, 0, 0, 0); return `${String(d.getHours()).padStart(2, "0")}:00`; };

/** The picked day (0 = today) and "HH:MM" as a local Date; null while the time is empty. The ISO string sent to the API is its UTC instant. */
function localTime(dayOff: number, time: string): Date | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(time); if (!m) return null;
  const d = new Date(); d.setDate(d.getDate() + dayOff); d.setHours(Number(m[1]), Number(m[2]), 0, 0); return d;
}
const WHEN_OPTIONS: SegmentOption<"now" | "later">[] = [{ value: "now", label: "Now", icon: "play" }, { value: "later", label: "Pick a time", icon: "clock" }];
const dayLabel = (off: number) => { const d = new Date(); d.setDate(d.getDate() + off); return off === 0 ? "Today" : off === 1 ? "Tomorrow" : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }); };

function WhenStep({ kind, channel, file, mode, onMode, dayOff, onDay, time, onTime, past }: {
  kind: Kind; channel?: ChannelSummary; file: File | null; mode: "now" | "later"; onMode: (m: "now" | "later") => void; dayOff: number; onDay: (n: number) => void; time: string; onTime: (t: string) => void; past: boolean;
}) {
  const { status } = useApp();
  const [from, to] = (channel?.publish_window || "").split("-");
  const at = mode === "later" ? localTime(dayOff, time) : null;
  return (
    <div className="flex flex-col gap-5">
      <SegmentedControl label="When to start" options={WHEN_OPTIONS} value={mode} onChange={onMode} />
      {mode === "later" && (
        <div className="flex flex-col gap-4">
          <Field label="Day"><div className="flex flex-wrap gap-2">{Array.from({ length: 7 }, (_, i) => <ChoiceChip key={i} on={dayOff === i} onChange={() => onDay(i)}>{dayLabel(i)}</ChoiceChip>)}</div></Field>
          <Field label="Time" htmlFor="when-time" hint="Your PC's local time. Nightshift starts the job then, waking the engine if it is off"><Input id="when-time" type="time" value={time} onChange={(e) => onTime(e.target.value)} className="w-40" aria-invalid={past} /></Field>
          {past && <p role="alert" className="text-ds-xs font-semibold text-ds-danger">That time has already passed. Pick a later time or choose Now</p>}
        </div>
      )}
      <div className="flex flex-wrap items-end gap-6">
        <div><p className="mb-2 text-ds-xs text-ds-muted">Starts</p>{at && !past ? <span className="inline-flex items-end gap-2"><TimeChip value={fmtClock(at.toISOString())} /><span className="pb-1 text-ds-xs">{dayInline(at.toISOString())}</span></span> : <TimeChip value="Now" />}</div>
        {from && to && <div><p className="mb-2 text-ds-xs text-ds-muted">Post between</p><span className="inline-flex items-end gap-2"><TimeChip value={from} /><span className="pb-1 text-ds-xs">to</span><TimeChip value={to} /></span></div>}
      </div>
      <p className="text-ds-sm leading-ds-normal text-ds-text-2">
        {mode === "later" ? "The job waits in the Queue until then, then loads the model and runs. " : kind === "story" ? "The job starts as soon as you press Start and runs beside tonight's schedule. " : file ? "The file uploads first, then the job starts. " : "Nightshift downloads the video first, then the job starts. "}
        The result lands in the Queue for you to approve. {status?.next_run ? <>The automatic run is still at <span className="ds-num">{fmtClock(status.next_run)}</span> {dayInline(status.next_run)}.</> : null}
      </p>
    </div>
  );
}

function Stepper({ value, onChange, min, max, label, suffix }: { value: number; onChange: (n: number) => void; min: number; max: number; label: string; suffix?: string }) {
  const btn = "ds-focus grid size-8 place-items-center rounded-full text-ds-accent-ink hover:bg-ds-hover";
  return (
    <div className="flex flex-col gap-2">
      <span className="text-ds-sm font-semibold text-ds-text-2">{label}</span>
      <div className="inline-flex h-10 items-center gap-1 self-start rounded-ds-md border border-ds-border bg-ds-surface p-1 shadow-ds-inset-sm">
        <button type="button" aria-label={`Fewer: ${label}`} onClick={() => onChange(Math.max(min, value - (max > 30 ? 5 : 1)))} className={btn}><Icon name="minus" size={14} /></button>
        <span className="min-w-14 text-center text-ds-sm font-semibold ds-num" aria-live="polite">{value}{suffix}</span>
        <button type="button" aria-label={`More: ${label}`} onClick={() => onChange(Math.min(max, value + (max > 30 ? 5 : 1)))} className={btn}><Icon name="plus" size={14} /></button>
      </div>
    </div>
  );
}

function JobRow({ j, channelName, onCancel }: { j: Job; channelName: (s?: string) => string; onCancel: (id: string) => void }) {
  const stages = j.kind === "clip" ? CLIP_STAGES : RUN_STAGES; const cur = stages.indexOf(j.stage || "");
  const held = j.status === "queued" && !!j.schedule_at && new Date(j.schedule_at).getTime() > Date.now();
  return (
    <li>
      <NotificationItem state="raised" title={jobTitle(j, channelName)}
        subtitle={<span className="text-ds-accent-ink">{j.status === "queued" && j.schedule_at && new Date(j.schedule_at).getTime() > Date.now() ? `Scheduled for ${fmtWhen(j.schedule_at)}` : j.status === "queued" ? "Waiting for the engine" : stageWords(j.stage)}<span className="text-ds-muted">{j.step && j.steps ? `, step ${j.step} of ${j.steps}` : ""}</span></span>}>
        {!held && <ProgressBar value={j.progress ?? 0} label="Job progress" />}
        {!held && <ol className="mt-3 flex flex-wrap gap-x-3 gap-y-1">
          {stages.map((s, i) => <li key={s} className={cx("flex items-center gap-1 text-ds-2xs", i < cur ? "text-ds-text-2" : i === cur ? "font-semibold text-ds-text" : "text-ds-muted")}>{i < cur ? <Icon name="check" size={12} className="text-ds-success-ink" /> : <span aria-hidden className={cx("size-1.5 rounded-full", i === cur ? "bg-ds-accent" : "bg-ds-border")} />}{stageWords(s)}</li>)}
        </ol>}
        <Button size="sm" variant="ghost" className="mt-2 -ml-2" onClick={() => onCancel(j.job_id)}>Cancel</Button>
      </NotificationItem>
    </li>
  );
}

function EarlierRow({ j, channelName, templates, onChanged }: { j: Job; channelName: (s?: string) => string; templates: Template[]; onChanged: () => void }) {
  const { toast } = useApp();
  const [open, setOpen] = useState(false); const [tpl, setTpl] = useState("");
  const canRerun = j.kind === "clip" && j.status === "done";
  const rerun = async () => { try { await api.post("/jobs/clip/rerun", { job_id: j.job_id, template: tpl }); toast("Making the clips again with that layout. The old clips are replaced", "ok"); setOpen(false); onChanged(); } catch (e: any) { toast(e.message, "bad"); } };
  const failed = j.status === "failed"; const cancelled = j.status === "cancelled";
  return (
    <li>
      <NotificationItem title={jobTitle(j, channelName)}
        titleAdornment={<StatusDot tone={failed ? "danger" : cancelled ? "attention" : "online"} label={failed ? "Stopped" : cancelled ? "Cancelled" : "Done"} />}
        subtitle={<span className={failed ? "text-ds-danger" : undefined}>{failed ? `Stopped at ${stageWords(j.stage).toLowerCase()}. ${j.message || ""}` : cancelled ? "Cancelled" : `Done ${dayInline(j.created_at)} · ${j.result?.output_ids?.length ? `${j.result.output_ids.length} videos` : "1 video"}`}</span>}
        meta={j.status === "done" ? <MetaItem icon="play"><a href="#/" className="font-semibold text-ds-accent-ink">Review it</a></MetaItem> : undefined} />
      {canRerun && (open ? (
        <div className="mt-2 flex flex-wrap items-center gap-2 pl-1">
          <Select aria-label="Layout for the new clips" className="!h-9 !w-auto" value={tpl} onChange={(e) => setTpl(e.target.value)}>
            <option value="">Pick a layout</option>{templates.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </Select>
          <Button size="sm" disabled={!tpl} onClick={rerun}>Re-run</Button>
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
        </div>
      ) : <Button size="sm" variant="ghost" className="mt-1 -ml-1" leadingIcon="refresh" onClick={() => setOpen(true)}>Re-run with another template</Button>)}
    </li>
  );
}
