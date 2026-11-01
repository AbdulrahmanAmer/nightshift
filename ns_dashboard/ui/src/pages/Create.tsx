import { useEffect, useState } from "react";
import { Check, FileVideo, Link2, Minus, Plus, X } from "lucide-react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { CLIP_STAGES, dayInline, fmtDur, fmtClock, POOL_LABELS, RUN_STAGES, stageWords, store } from "../lib/format";
import type { Job, Output, Template } from "../lib/types";
import { Button, Card, cx, Dropzone, Empty, LinkButton, PageHeader, Progress } from "../ui/kit";
import { Scene } from "../visuals/Scene";
import { LayoutDiagram } from "../visuals/charts";
import { Poster } from "./Queue";

type Kind = "story" | "clips";

export default function Create() {
  const { status, route, toast, refresh, channelName } = useApp();
  const [kind, setKind] = useState<Kind>((route.query.get("kind") as Kind) || store.get<Kind>("ns-create-kind", "story"));
  const jobs = useLoad<Job[]>(() => api.get("/jobs"), [], []);
  const clips = useLoad<Output[]>(() => api.get("/outputs?status=pending"), [], []);
  const running = jobs.data.filter((j) => ["queued", "running"].includes(j.status));
  useEffect(() => {
    const t = setInterval(() => { jobs.reload(); if (running.length) clips.reload(); }, running.length ? 1500 : 6000);
    return () => clearInterval(t);
  }, [running.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const pick = (k: Kind) => { setKind(k); store.set("ns-create-kind", k); };
  const started = () => { toast("Started. You can leave this page", "ok"); jobs.reload(); refresh(); };
  const cancel = async (id: string) => { try { await api.post(`/jobs/${encodeURIComponent(id)}/cancel`); toast("Cancelling", "warn"); jobs.reload(); } catch (e: any) { toast(e.message, "bad"); } };
  const finishedClips = clips.data.filter((o) => o.kind === "clip");
  const earlier = jobs.data.filter((j) => !["queued", "running"].includes(j.status)).slice(0, 6);

  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader title="Create" sub="Pick what you want made. The result lands in your Queue" />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex min-w-0 flex-col gap-4">
          <div role="radiogroup" aria-label="What to make" className="grid gap-4 sm:grid-cols-2">
            <ChoiceTile on={kind === "story"} onClick={() => pick("story")} title="A new story video" sub="An original story for one of your channels, narrated and edited with footage and captions" scene={<Scene seed="The Laundromat That Only Opens at Night" w={1600} h={600} className="size-full" />} />
            <ChoiceTile on={kind === "clips"} onClick={() => pick("clips")} title="Clips from my own video" sub="Drop a stream or podcast, or paste a link. Nightshift finds the best moments and cuts vertical clips" scene={<Scene seed="night-desk stream" kind="clip" w={1600} h={600} className="size-full" />} />
          </div>
          <Card className="p-6">{kind === "story" ? <StoryFlow onStarted={started} /> : <ClipFlow onStarted={started} />}</Card>
        </div>

        <div className="flex flex-col gap-4">
          <Card className="p-5">
            <h2 className="text-[17px] font-medium">Working now</h2>
            {running.length ? <div className="mt-4 flex flex-col gap-5">{running.map((j) => <JobRow key={j.job_id} j={j} channelName={channelName} onCancel={cancel} />)}</div>
              : <p className="mt-2 text-[13px] text-text-2">Nothing running. Tonight's run starts at <span className="font-mono num">{fmtClock(status?.next_run) || "03:00"}</span></p>}
          </Card>
          <Card className="p-5">
            <h2 className="text-[17px] font-medium">Earlier</h2>
            {earlier.length ? <ul className="mt-3 flex flex-col divide-y divide-line">{earlier.map((j) => <EarlierRow key={j.job_id} j={j} channelName={channelName} />)}</ul> : <p className="mt-2 text-[13px] text-text-2">No earlier jobs</p>}
          </Card>
        </div>
      </div>

      <section aria-label="Finished clips">
        <div className="mb-3 flex items-baseline justify-between px-1"><h2 className="text-[17px] font-medium">Finished clips</h2><a href="#/" className="text-[13px] text-text-2 hover:text-text">Review in the Queue</a></div>
        {finishedClips.length ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
            {finishedClips.map((o) => (
              <a key={o.output_id} href={`#/?id=${encodeURIComponent(o.output_id)}`} className="group flex flex-col gap-2 rounded-[16px] border border-line bg-card p-2 no-underline shadow-sm hover:border-line-2">
                <div className="relative aspect-[9/16] overflow-hidden rounded-[10px] bg-[#0b0d14]"><Poster o={o} className="size-full" /><span className="glass-chip absolute right-2 bottom-2 rounded-full px-2 py-0.5 font-mono text-[11px] num">{fmtDur(o.duration_s)}</span></div>
                <p className="line-clamp-2 px-1 pb-1 text-[13px] leading-[18px] font-medium">{o.title}</p>
              </a>
            ))}
          </div>
        ) : <Empty title="No clips waiting">Clips you make from your own videos show up here and in the Queue</Empty>}
      </section>
    </div>
  );
}

function ChoiceTile({ on, onClick, title, sub, scene }: { on: boolean; onClick: () => void; title: string; sub: string; scene: JSX.Element }) {
  return (
    <button type="button" role="radio" aria-checked={on} onClick={onClick} className={cx("card group flex flex-col overflow-hidden p-2 text-left transition-[border-color,box-shadow] duration-150", on ? "border-fill ring-1 ring-fill" : "hover:border-line-2")}>
      <div className="relative h-[120px] overflow-hidden rounded-[14px] bg-[#0b0d14]">{scene}
        <span className={cx("absolute top-3 right-3 flex size-7 items-center justify-center rounded-full transition-colors", on ? "bg-accent text-on-accent" : "glass-chip")}>{on && <Check size={16} strokeWidth={2} />}</span>
      </div>
      <div className="px-3 pt-3 pb-2"><p className="text-[16px] font-medium">{title}</p><p className="mt-1 text-[13px] text-text-2">{sub}</p></div>
    </button>
  );
}

function Step({ n, title, children, done }: { n: number; title: string; children: React.ReactNode; done?: boolean }) {
  return (
    <div className="grid grid-cols-[32px_minmax(0,1fr)] gap-x-4 gap-y-3">
      <span className={cx("flex size-8 items-center justify-center rounded-full font-mono text-[13px] num", done ? "bg-fill text-on-fill" : "border border-line-2 text-text-2")}>{done ? <Check size={16} strokeWidth={2} /> : n}</span>
      <h3 className="self-center text-[15px] font-medium">{title}</h3>
      <div className="col-start-2 min-w-0">{children}</div>
    </div>
  );
}

function ChannelPick({ value, onChange }: { value: string; onChange: (s: string) => void }) {
  const { status } = useApp(); const chans = status?.channels || [];
  if (!chans.length) return <Empty title="No channels yet" action={<LinkButton href="#/channels" variant="primary">Add your first channel</LinkButton>} />;
  return (
    <div role="radiogroup" aria-label="Channel" className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
      {chans.map((c) => (
        <button key={c.slug} type="button" role="radio" aria-checked={value === c.slug} onClick={() => onChange(c.slug)} className={cx("flex items-center gap-3 rounded-[14px] border p-2 pr-3 text-left transition-colors", value === c.slug ? "border-fill bg-raised" : "border-line-2 hover:bg-raised/60")}>
          <span className="h-10 w-14 shrink-0 overflow-hidden rounded-[8px] bg-[#0b0d14]"><Scene seed={c.display_name + " cover"} kind={c.format === "shorts_only" && /clip|stream/i.test(c.niche || "") ? "clip" : "story"} w={560} h={400} className="size-full" /></span>
          <span className="min-w-0"><span className="block truncate text-[14px] font-medium">{c.display_name}</span><span className="block truncate text-[12px] text-muted">{c.status === "active" ? `Voice ${typeof c.voice === "string" ? c.voice : c.voice?.speaker || ""}` : "Paused"}</span></span>
        </button>
      ))}
    </div>
  );
}

function Option({ on, onClick, title, sub }: { on: boolean; onClick: () => void; title: string; sub: string }) {
  return (
    <button type="button" role="radio" aria-checked={on} onClick={onClick} className={cx("flex items-start gap-3 rounded-[14px] border p-4 text-left transition-colors", on ? "border-fill bg-raised" : "border-line-2 hover:bg-raised/60")}>
      <span className={cx("mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border", on ? "border-fill bg-fill" : "border-line-2")}>{on && <span className="size-2 rounded-full bg-on-fill" />}</span>
      <span><span className="block text-[14px] font-medium">{title}</span><span className="mt-0.5 block text-[13px] text-text-2">{sub}</span></span>
    </button>
  );
}

function StoryFlow({ onStarted }: { onStarted: () => void }) {
  const { status, toast } = useApp(); const chans = status?.channels || [];
  const [channel, setChannel] = useState(chans.find((c) => c.status === "active")?.slug || chans[0]?.slug || "");
  const [format, setFormat] = useState<"master_first" | "shorts_only">("master_first");
  const [busy, setBusy] = useState(false);
  const start = async () => { setBusy(true); try { await api.post("/jobs/run", { channel, format }); onStarted(); } catch (e: any) { toast(e.message, "bad"); } setBusy(false); };
  return (
    <div className="flex flex-col gap-6">
      <Step n={1} title="Which channel" done={!!channel}><ChannelPick value={channel} onChange={setChannel} /></Step>
      <Step n={2} title="What to make" done>
        <div role="radiogroup" aria-label="Format" className="grid gap-2 sm:grid-cols-2">
          <Option on={format === "master_first"} onClick={() => setFormat("master_first")} title="Full video + Shorts" sub="A 10 to 20 minute story with chapters, plus 4 to 6 Shorts cut from it. About an hour" />
          <Option on={format === "shorts_only"} onClick={() => setFormat("shorts_only")} title="One Short" sub="A single short story as a vertical video. A few minutes" />
        </div>
      </Step>
      <Step n={3} title="Start">
        <div className="flex flex-wrap items-center gap-3"><Button variant="primary" size="lg" onClick={start} disabled={!channel || busy}>Start</Button><span className="text-[13px] text-muted">The engine loads its model first, which takes about a minute</span></div>
      </Step>
    </div>
  );
}

function Stepper({ value, onChange, min, max, label, suffix }: { value: number; onChange: (n: number) => void; min: number; max: number; label: string; suffix?: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[13px] font-medium text-text-2">{label}</span>
      <div className="inline-flex h-10 items-center gap-1 self-start rounded-full border border-line-2 bg-card p-1">
        <button type="button" aria-label={`Fewer: ${label}`} onClick={() => onChange(Math.max(min, value - (max > 30 ? 5 : 1)))} className="flex size-8 items-center justify-center rounded-full text-text-2 hover:bg-raised"><Minus size={14} strokeWidth={1.75} /></button>
        <span className="min-w-[56px] text-center font-mono text-[14px] num" aria-live="polite">{value}{suffix}</span>
        <button type="button" aria-label={`More: ${label}`} onClick={() => onChange(Math.min(max, value + (max > 30 ? 5 : 1)))} className="flex size-8 items-center justify-center rounded-full text-text-2 hover:bg-raised"><Plus size={14} strokeWidth={1.75} /></button>
      </div>
    </div>
  );
}

function ClipFlow({ onStarted }: { onStarted: () => void }) {
  const { status, toast } = useApp(); const chans = status?.channels || [];
  const templates = useLoad<Template[]>(() => api.get("/templates"), [], []);
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [channel, setChannel] = useState(chans.find((c) => /stream|clip|podcast/i.test(c.niche || ""))?.slug || chans[0]?.slug || "");
  const [template, setTemplate] = useState("auto");
  const [maxClips, setMaxClips] = useState(6); const [minS, setMinS] = useState(20); const [maxS, setMaxS] = useState(60);
  const [caption, setCaption] = useState("punch_yellow");
  const [rights, setRights] = useState(false);
  const [upload, setUpload] = useState<number | null>(null);
  const hasSource = !!file || /^https?:\/\/\S+\.\S+/.test(url.trim());
  const start = async () => {
    try {
      if (file) {
        const fd = new FormData(); Object.entries({ channel, template, max_clips: maxClips, min_s: minS, max_s: maxS, caption_style: caption, rights_confirmed: "true" }).forEach(([k, v]) => fd.append(k, String(v))); fd.append("file", file, file.name);
        setUpload(0); await api.upload("/jobs/clip", fd, setUpload); setFile(null);
      } else {
        await api.post("/jobs/clip", { url: url.trim(), channel, template, max_clips: maxClips, min_s: minS, max_s: maxS, caption_style: caption, rights_confirmed: true }); setUrl("");
      }
      setRights(false); onStarted();
    } catch (e: any) { toast(e.message, "bad"); }
    setUpload(null);
  };
  return (
    <div className="flex flex-col gap-6">
      <Step n={1} title="Your video" done={hasSource}>
        {file ? (
          <div className="flex items-center gap-3 rounded-[14px] border border-line-2 p-3">
            <span className="flex size-10 items-center justify-center rounded-full bg-raised text-text-2"><FileVideo size={18} strokeWidth={1.5} /></span>
            <div className="min-w-0 flex-1"><p className="truncate text-[14px] font-medium">{file.name}</p><p className="font-mono text-[12px] text-muted num">{(file.size / 1e9).toFixed(2)} GB</p></div>
            <Button size="sm" variant="ghost" icon={<X size={14} />} onClick={() => setFile(null)}>Change</Button>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <Dropzone compact title="Drop a video here" sub="A stream, podcast or recording you own. MP4, MKV or MOV, up to 4 hours" accept="video/*,.mkv" onFile={setFile} />
            <div className="flex items-center gap-3 text-[12px] text-muted"><span className="h-px flex-1 bg-line" />or paste a link<span className="h-px flex-1 bg-line" /></div>
            <div className="relative"><Link2 size={16} strokeWidth={1.5} className="pointer-events-none absolute top-1/2 left-3.5 -translate-y-1/2 text-muted" /><input aria-label="YouTube link" type="url" className="field-input pl-10" placeholder="https://youtu.be/" value={url} onChange={(e) => setUrl(e.target.value)} /></div>
          </div>
        )}
      </Step>
      <Step n={2} title="Channel and layout" done={!!channel}>
        <div className="flex flex-col gap-4">
          <ChannelPick value={channel} onChange={setChannel} />
          <div role="radiogroup" aria-label="Layout" className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
            {templates.data.map((t) => (
              <button key={t.id} type="button" role="radio" aria-checked={template === t.id} onClick={() => setTemplate(t.id)} title={t.description} className={cx("flex flex-col items-center gap-2 rounded-[14px] border px-2 pt-3 pb-2.5 text-center transition-colors", template === t.id ? "border-fill bg-raised" : "border-line-2 hover:bg-raised/60")}>
                <LayoutDiagram id={t.id} w={40} h={70} />
                <span className="text-[12px] leading-4 font-medium">{t.label}</span>
              </button>
            ))}
          </div>
          <p className="text-[12px] text-muted">{templates.data.find((t) => t.id === template)?.description}</p>
        </div>
      </Step>
      <Step n={3} title="Options">
        <div className="flex flex-wrap gap-x-8 gap-y-4">
          <Stepper label="How many clips, at most" value={maxClips} onChange={setMaxClips} min={1} max={10} />
          <Stepper label="Shortest" value={minS} onChange={(v) => setMinS(Math.min(v, maxS - 5))} min={10} max={120} suffix=" s" />
          <Stepper label="Longest" value={maxS} onChange={(v) => setMaxS(Math.max(v, minS + 5))} min={15} max={179} suffix=" s" />
          <div className="flex flex-col gap-1.5">
            <span className="text-[13px] font-medium text-text-2">Caption look</span>
            <div className="flex flex-wrap gap-2">{Object.entries(POOL_LABELS.caption_styles.opts).map(([k, l]) => <button key={k} type="button" aria-pressed={caption === k} onClick={() => setCaption(k)} className={cx("h-10 rounded-full border px-3.5 text-[13px] transition-colors", caption === k ? "border-transparent bg-fill text-on-fill" : "border-line-2 text-text-2 hover:text-text")}>{l}</button>)}</div>
          </div>
        </div>
      </Step>
      <Step n={4} title="Confirm and start">
        <div className="flex flex-col gap-4">
          <label className="flex cursor-pointer items-start gap-3 text-[14px]">
            <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} className="mt-0.5 size-[18px] accent-[var(--fill)]" />
            <span>I own this video or have permission to use it</span>
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" size="lg" onClick={start} disabled={!hasSource || !rights || !channel || upload != null}>Start</Button>
            {upload != null ? <div className="flex w-56 items-center gap-3"><Progress value={upload} /><span className="font-mono text-[12px] text-muted num">{upload}%</span></div> : !hasSource ? <span className="text-[13px] text-muted">Add a video or a link first</span> : !rights ? <span className="text-[13px] text-muted">Confirm the rights to start</span> : null}
          </div>
        </div>
      </Step>
    </div>
  );
}

function jobTitle(j: Job, channelName: (s?: string) => string) {
  if (j.kind === "clip") return j.title ? `Clips from ${j.title}` : `Clips for ${channelName(j.channel)}`;
  return j.title || `Story video for ${channelName(j.channel)}`;
}

function JobRow({ j, channelName, onCancel }: { j: Job; channelName: (s?: string) => string; onCancel: (id: string) => void }) {
  const stages = j.kind === "clip" ? CLIP_STAGES : RUN_STAGES; const cur = stages.indexOf(j.stage || "");
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0"><p className="truncate text-[14px] font-medium">{jobTitle(j, channelName)}</p><p className="text-[13px] text-accent-ink">{j.status === "queued" ? "Waiting for the engine" : stageWords(j.stage)}<span className="text-muted">{j.step && j.steps ? `, step ${j.step} of ${j.steps}` : ""}</span></p></div>
        <Button size="sm" variant="ghost" onClick={() => onCancel(j.job_id)}>Cancel</Button>
      </div>
      <Progress value={j.progress ?? 0} />
      <ol className="flex flex-wrap gap-x-3 gap-y-1">
        {stages.map((s, i) => <li key={s} className={cx("flex items-center gap-1 text-[12px]", i < cur ? "text-text-2" : i === cur ? "font-medium text-text" : "text-muted/70")}>{i < cur ? <Check size={12} strokeWidth={2} className="text-ok" /> : <span className={cx("size-1.5 rounded-full", i === cur ? "bg-accent" : "bg-line-2")} />}{stageWords(s)}</li>)}
      </ol>
    </div>
  );
}

function EarlierRow({ j, channelName }: { j: Job; channelName: (s?: string) => string }) {
  const failed = j.status === "failed"; const cancelled = j.status === "cancelled";
  return (
    <li className="flex items-start gap-3 py-3 first:pt-1 last:pb-0">
      <span className={cx("mt-1.5 size-2 shrink-0 rounded-full", failed ? "bg-bad" : cancelled ? "bg-muted" : "bg-ok")} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[14px]">{jobTitle(j, channelName)}</p>
        <p className={cx("text-[12px]", failed ? "text-bad" : "text-muted")}>{failed ? `Stopped at ${stageWords(j.stage).toLowerCase()}. ${j.message || ""}` : cancelled ? "Cancelled" : `Done ${dayInline(j.created_at)} · ${j.result?.output_ids?.length ? `${j.result.output_ids.length} videos` : "1 video"}`}</p>
      </div>
      {j.status === "done" && <a href="#/" className="shrink-0 rounded-full border border-line-2 px-3 py-1 text-[12px] font-medium no-underline hover:bg-raised">Review it</a>}
    </li>
  );
}
