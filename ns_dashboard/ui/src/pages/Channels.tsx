/* Channels: one card per channel in the 6-column grid (two across, one under 600 px of container). Each card has the
   cover still, a pause switch, two StatCards (videos this week against the plan, then the share of videos that passed QC against the
   channel's target, or watched to the end against the 55% target while no video has been checked yet), the formats it may use as Badges, and its voice and caption settings as a settings list.
   The editor keeps every field and the same calls (GET /channels, POST/PUT, pause/resume, validate). */
import { useMemo, useState } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { POOL_LABELS } from "../lib/format";
import type { Channel, Format, Pools, Voice } from "../lib/types";
import { cx } from "../ui/kit";
import { Scene } from "../visuals/Scene";
import {
  AppGrid, Badge, Button, Card, CardHeader, ChoiceChip, DashGrid, DayChips, EmptyState, Field, Icon, Input, SegmentedControl, Select, SettingRow,
  SPAN, StatCard, Switch, TextArea,
} from "../components/ref12";

const voiceName = (c: Channel) => (typeof c.voice === "string" ? c.voice : c.voice?.name || c.voice?.speaker || "Built-in");
const isClips = (c: Channel) => /clip|stream|podcast/i.test(c.niche || "");
/** Watched-to-the-end level below which a style counts against itself (Results page rule). */
const RETENTION_TARGET = 55;
/** QC pass-rate target when the channel stats do not carry one (the API's default). */
const QC_TARGET = 0.9;

export default function Channels() {
  const { route, refresh, toast, status } = useApp();
  const chans = useLoad<Channel[]>(() => api.get("/channels"), [], []);
  const formats = useLoad<Format[]>(() => api.get("/library/formats"), [], []);
  const [editing, setEditing] = useState<string | null>(route.query.get("edit"));
  const toggle = async (c: Channel, on: boolean) => {
    chans.setData((l) => l.map((x) => (x.slug === c.slug ? { ...x, status: on ? "active" : "paused" } : x)));
    try { await api.post(`/channels/${encodeURIComponent(c.slug)}/${on ? "resume" : "pause"}`); toast(on ? `${c.display_name} runs again from tonight` : `${c.display_name} is paused`, "ok"); refresh(); }
    catch (e: any) { toast(e.message, "bad"); chans.reload(); }
  };
  if (editing) {
    const c = editing === "new" ? null : chans.data.find((x) => x.slug === editing);
    if (editing !== "new" && !c) return chans.loading ? null : <EmptyState icon="channels" title="Channel not found" action={<Button variant="soft" onClick={() => setEditing(null)}>Back to channels</Button>} />;
    return <Editor initial={c} onDone={() => { setEditing(null); chans.reload(); refresh(); }} />;
  }
  const pendingOf = (slug: string) => status?.channels.find((x) => x.slug === slug)?.pending;
  return (
    <div className="enter flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-ds-md text-ds-text-2">Each channel has its own voice, style and schedule</p>
        <Button leadingIcon="plus" onClick={() => setEditing("new")}>Add a channel</Button>
      </div>
      {chans.data.length ? (
        <DashGrid label="Channels">
          {chans.data.map((c) => {
            const on = c.status === "active"; const st = c.stats || {}; const pending = pendingOf(c.slug) ?? c.pending;
            const plan = c.masters_per_week ?? 1; const week = st.week ?? 0;
            const spark = st.spark; const delta = spark && spark.length > 1 ? Math.round(spark[spark.length - 1]! - spark[0]!) : null;
            const allowed = formats.data.filter((f) => f.channels?.includes(c.slug));
            return (
              <Card as="article" key={c.slug} aria-labelledby={`ch-${c.slug}`} className={cx("flex flex-col gap-5 self-stretch", SPAN["3/4"])}>
                <div className="relative h-[136px] overflow-hidden rounded-ds-lg bg-ds-media">
                  <Scene seed={c.display_name + " cover"} kind={isClips(c) ? "clip" : "story"} w={1200} h={420} className={cx("size-full transition-opacity duration-200", !on && "opacity-50")} />
                  <div className="absolute top-3 left-3 flex gap-2"><Badge tone="overlay" size="md"><span aria-hidden className={cx("size-[7px] rounded-full", on ? "bg-ds-online" : "bg-ds-muted")} />{on ? "Runs tonight" : "Paused"}</Badge><Badge tone="overlay" size="md">{isClips(c) ? "Clips from your videos" : "Original fiction"}</Badge></div>
                </div>
                <header className="flex items-start justify-between gap-3">
                  <div className="min-w-0"><h2 id={`ch-${c.slug}`} className="truncate text-ds-xl leading-ds-tight font-semibold">{c.display_name}</h2><p className="mt-1 line-clamp-2 text-ds-sm text-ds-text-2">{c.niche}</p></div>
                  <Switch checked={on} onChange={(v) => toggle(c, v)} label={on ? `Pause ${c.display_name}` : `Resume ${c.display_name}`} />
                </header>
                <div className="grid grid-cols-1 gap-4 @container sm:grid-cols-2">
                  <StatCard tone="accent" value={Math.min(100, (week / Math.max(1, plan)) * 100)} eyebrow="This week" title={`${week} of ${plan} made`} caption={`${pending ?? 0} waiting for review`} detail={`${plan} full video${plan === 1 ? "" : "s"} a week`} />
                  {st.qc_pass_rate != null
                    ? <StatCard tone={st.qc_pass_rate >= (st.qc_pass_target ?? QC_TARGET) ? "success" : "danger"} value={Math.round(st.qc_pass_rate * 100)} eyebrow="Quality" title="Passed QC" caption={`Target ${Math.round((st.qc_pass_target ?? QC_TARGET) * 100)}%`} detail="Videos from the last 30 days" />
                    : <StatCard tone={(st.watched_pct ?? 0) >= RETENTION_TARGET ? "success" : "danger"} value={st.watched_pct ?? 0} eyebrow="Retention" title="Watched to the end" caption={`Target ${RETENTION_TARGET}%`} detail={delta == null ? "No trend yet" : `${delta > 0 ? "+" : ""}${delta} pts vs 14 days ago`} />}
                </div>
                <div>
                  <p className="mb-2 text-ds-xs font-semibold text-ds-text-2">Formats allowed</p>
                  <div className="flex flex-wrap gap-2">
                    <Badge tone="accent" size="md">{c.format === "shorts_only" ? "Shorts only" : "Full video + Shorts"}</Badge>
                    {allowed.map((f) => <Badge key={f.id} tone="neutral" size="md">{f.name || f.id}</Badge>)}
                  </div>
                </div>
                <div className="-my-2">
                  <SettingRow title="Voice" sub={`${voiceName(c)}${typeof c.voice === "object" && c.voice?.speed && c.voice.speed !== 1 ? `, speed ${c.voice.speed}` : ""}`} />
                  <SettingRow title="Caption looks" sub={(c.pools?.caption_styles || []).map((k) => POOL_LABELS.caption_styles.opts[k] || k).join(", ") || "Default"} />
                  <SettingRow title="Posts between" sub={<span className="ds-num">{(c.publish_window || "").replace("-", " – ")}</span>} />
                </div>
                <Button variant="soft" leadingIcon="edit" className="mt-auto self-start" onClick={() => setEditing(c.slug)}>Edit channel</Button>
              </Card>
            );
          })}
        </DashGrid>
      ) : <EmptyState icon="channels" title="No channels yet" action={<Button onClick={() => setEditing("new")}>Add a channel</Button>}>Add one to start making videos</EmptyState>}
    </div>
  );
}

const BLANK: Channel = { slug: "", display_name: "", status: "active", niche: "", language: "en", voice: { mode: "preset", speaker: "Ryan", speed: 1 }, format: "master_first", masters_per_week: 1, publish_window: "18:00-21:00", audience: "general", fiction_label: true, voice_notes: "", pools: { hooks: ["cold_open", "question", "confession"], structures: ["twist", "reverse_reveal", "slow_burn"], caption_styles: ["punch_yellow", "clean_white"], visual_treatments: ["dark_slowzoom", "handheld_grit"] }, avoid_topics: [], long_minutes: [10, 14], shorts_per_master: [4, 6] };
const SECTIONS = [["basics", "Basics"], ["voice", "Voice"], ["schedule", "Schedule"], ["style", "Content style"]] as const;

function Editor({ initial, onDone }: { initial: Channel | null | undefined; onDone: () => void }) {
  const { toast } = useApp(); const isNew = !initial;
  const voices = useLoad<Voice[]>(() => api.get("/voices"), [], []);
  const [c, setC] = useState<Channel>(() => ({ ...BLANK, ...(initial || {}), pools: { ...BLANK.pools!, ...(initial?.pools || {}) } as Pools, voice: typeof initial?.voice === "object" ? initial.voice : { mode: "preset", speaker: initial?.voice || "Ryan", speed: 1 } }));
  const [avoid, setAvoid] = useState("");
  const [saving, setSaving] = useState(false);
  const set = (p: Partial<Channel>) => setC((x) => ({ ...x, ...p }));
  const [from, to] = (c.publish_window || "18:00-21:00").split("-");
  const pools = c.pools!;
  const togglePool = (pool: keyof Pools, k: string, on: boolean) => set({ pools: { ...pools, [pool]: on ? [...pools[pool], k] : pools[pool].filter((x) => x !== k) } });
  const warnings = useMemo(() => {
    const w: string[] = []; const h = pools.hooks.length, s = pools.structures.length;
    if (h < 3) w.push("Pick at least 3 openings so stories don't start the same way");
    if (s < 3) w.push("Pick at least 3 structures");
    if (h >= 3 && s >= 3 && h * s < 9) w.push("Openings times structures must reach 9 so videos don't repeat");
    if (pools.caption_styles.length < 2) w.push("Pick at least 2 caption looks");
    if (pools.visual_treatments.length < 2) w.push("Pick at least 2 footage looks");
    return w;
  }, [pools]);
  const hear = async () => {
    try { const url = await api.voicePreview(c.voice?.speaker, "The doorbell camera pings at three twelve every night. I stopped checking the clips a week ago."); new Audio(url).play(); }
    catch (e: any) { toast(e.message, "warn"); }
  };
  const save = async () => {
    if (!c.display_name.trim() || !c.slug.trim() || !c.niche?.trim()) { toast("Fill in the name, short id and what the channel is about", "warn"); return; }
    if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(c.slug)) { toast("The short id can only have lowercase letters, numbers and dashes", "warn"); return; }
    setSaving(true);
    const payload: any = { ...c, voice: { mode: "preset", speaker: c.voice?.speaker, name: null, description: null, speed: Number(c.voice?.speed || 1) } };
    delete payload.status; delete payload.stats; delete payload.pending; delete payload.tonight;
    try {
      if (isNew) await api.post("/channels", payload); else await api.put(`/channels/${encodeURIComponent(c.slug)}`, payload);
      const v = await api.post(`/channels/${encodeURIComponent(c.slug)}/validate`).catch(() => ({ warnings: [] }));
      (v?.warnings || []).forEach((w: string) => toast(w, "warn"));
      toast("Channel saved", "ok"); onDone();
    } catch (e: any) { toast(e.message, "bad"); }
    setSaving(false);
  };
  const jump = (id: string) => document.getElementById("sec-" + id)?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className="enter flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0"><h2 className="text-ds-2xl leading-ds-tight font-semibold tracking-[-0.02em]">{isNew ? "New channel" : c.display_name}</h2><p className="mt-1 text-ds-md text-ds-text-2">{isNew ? "A YouTube channel Nightshift makes videos for" : "Changes apply from the next run"}</p></div>
        <div className="flex gap-2"><Button variant="ghost" leadingIcon="chevronLeft" onClick={onDone}>Back</Button><Button onClick={save} disabled={saving}>Save channel</Button></div>
      </header>
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[200px_minmax(0,1fr)]">
        <nav aria-label="Sections" className="hidden lg:block"><ul className="sticky top-4 flex flex-col gap-1">{SECTIONS.map(([id, l]) => <li key={id}><button type="button" onClick={() => jump(id)} className="ds-focus h-9 w-full rounded-ds-md px-4 text-left text-ds-sm font-semibold text-ds-text-2 hover:bg-ds-hover hover:text-ds-text">{l}</button></li>)}</ul></nav>
        <div className="flex min-w-0 flex-col gap-5">
          <Card as="section" id="sec-basics" aria-labelledby="h-basics" className="scroll-mt-4">
            <CardHeader id="h-basics" title="Basics" />
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Channel name" htmlFor="f-name"><Input id="f-name" value={c.display_name} onChange={(e) => set({ display_name: e.target.value, ...(isNew ? { slug: e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") } : {}) })} /></Field>
              <Field label="Short id" htmlFor="f-slug" hint="Used for folder names. Can't change later"><Input id="f-slug" className="font-mono" value={c.slug} readOnly={!isNew} onChange={(e) => set({ slug: e.target.value })} /></Field>
              <Field label="What the channel is about" htmlFor="f-niche" hint="One sentence. Every story is written to fit it" className="md:col-span-2"><Input id="f-niche" value={c.niche} placeholder="Original short horror fiction set in everyday places" onChange={(e) => set({ niche: e.target.value })} /></Field>
              <Field label="Language" htmlFor="f-lang"><Select id="f-lang" value={c.language} onChange={(e) => set({ language: e.target.value })}>{[["en", "English"], ["es", "Spanish"], ["pt", "Portuguese"], ["fr", "French"], ["de", "German"], ["it", "Italian"], ["ja", "Japanese"]].map(([k, l]) => <option key={k} value={k}>{l}</option>)}</Select></Field>
              <Field label="Audience" htmlFor="f-aud" hint={c.audience === "mature" ? "Adds an age-restriction note when a story gets intense" : undefined}><Select id="f-aud" value={c.audience} onChange={(e) => set({ audience: e.target.value })}><option value="general">General</option><option value="mature">Mature</option></Select></Field>
              <Field label="Style notes" htmlFor="f-notes" className="md:col-span-2"><TextArea id="f-notes" rows={2} placeholder="Plain, intimate first person. Short sentences. Dread over gore" value={c.voice_notes} onChange={(e) => set({ voice_notes: e.target.value })} /></Field>
            </div>
          </Card>

          <Card as="section" id="sec-voice" aria-labelledby="h-voice" className="scroll-mt-4">
            <CardHeader id="h-voice" title="Voice" actions={<Button size="sm" variant="soft" leadingIcon="play" onClick={hear}>Hear a sample</Button>} />
            <AppGrid bare choice label="Narrator" apps={voices.data.map((v) => ({ id: v.name, icon: "voice", label: v.name, caption: v.description || (v.mode === "preset" ? "Built-in" : "Custom"), selected: c.voice?.speaker === v.name, onClick: () => set({ voice: { ...c.voice, speaker: v.name } }) }))} />
            <Field label="Speed" className="mt-5"><SegmentedControl label="Speed" value={String(c.voice?.speed ?? 1)} onChange={(v) => set({ voice: { ...c.voice, speed: Number(v) } })} options={[{ value: "0.9", label: "A little slower" }, { value: "1", label: "Normal" }, { value: "1.1", label: "A little faster" }]} /></Field>
          </Card>

          <Card as="section" id="sec-schedule" aria-labelledby="h-schedule" className="scroll-mt-4">
            <CardHeader id="h-schedule" title="Schedule" />
            <div className="flex flex-wrap gap-x-10 gap-y-5">
              <Field label="Full videos per week" hint="New channels start at 1"><DayChips label="Full videos per week" days={[1, 2, 3].map((n) => ({ value: n, label: String(n), ariaLabel: `${n} a week` }))} selected={[c.masters_per_week ?? 1]} onToggle={(n) => set({ masters_per_week: n })} /></Field>
              <Field label="Post between" hint="Your local time">
                <div className="flex items-center gap-2"><Input aria-label="From" type="time" className="!w-[120px] font-mono" value={from} onChange={(e) => set({ publish_window: `${e.target.value}-${to}` })} /><span className="text-ds-muted">to</span><Input aria-label="To" type="time" className="!w-[120px] font-mono" value={to} onChange={(e) => set({ publish_window: `${from}-${e.target.value}` })} /></div>
              </Field>
              <Field label="Default type"><SegmentedControl label="Default type" value={c.format || "master_first"} onChange={(v) => set({ format: v })} options={[{ value: "master_first", label: "Full video + Shorts" }, { value: "shorts_only", label: "Shorts only" }]} /></Field>
            </div>
          </Card>

          <Card as="section" id="sec-style" aria-labelledby="h-style" className="scroll-mt-4">
            <CardHeader id="h-style" title="Content style" actions={<label className="mr-2 flex items-center gap-3 text-ds-xs font-semibold text-ds-text-2"><span>Add <q>Original fiction</q> to every description</span><Switch checked={!!c.fiction_label} onChange={(v) => set({ fiction_label: v })} label="Original fiction label" /></label>} />
            <p className="-mt-3 text-ds-sm text-ds-text-2">Nightshift mixes these so videos never look the same twice</p>
            {warnings.length > 0 && <div role="alert" className="mt-4 flex flex-col gap-1 rounded-ds-md bg-ds-danger-soft px-4 py-3 text-ds-sm">{warnings.map((w) => <p key={w} className="flex items-start gap-2"><Icon name="alert" size={16} className="mt-0.5 shrink-0 text-ds-danger" />{w}</p>)}</div>}
            <div className="mt-5 flex flex-col gap-5">
              {(Object.keys(POOL_LABELS) as (keyof Pools)[]).map((pool) => (
                <div key={pool}><p className="mb-2 text-ds-sm font-semibold text-ds-text-2">{POOL_LABELS[pool].label} <span className="font-normal text-ds-muted ds-num">{pools[pool].length}</span></p>
                  <div className="flex flex-wrap gap-2">{Object.entries(POOL_LABELS[pool].opts).map(([k, l]) => <ChoiceChip key={k} on={pools[pool].includes(k)} onChange={(on) => togglePool(pool, k, on)}>{l}</ChoiceChip>)}</div></div>
              ))}
              <div><label htmlFor="f-avoid" className="mb-2 block text-ds-sm font-semibold text-ds-text-2">Topics to avoid</label>
                <div className="flex flex-wrap items-center gap-2 rounded-ds-md border border-ds-border bg-ds-surface p-2 shadow-ds-inset-sm focus-within:border-ds-focus focus-within:shadow-ds-focus">
                  {(c.avoid_topics || []).map((t) => <span key={t} className="inline-flex h-7 items-center gap-1 rounded-ds-sm bg-ds-sunken pr-1 pl-3 text-ds-xs font-semibold">{t}<button type="button" aria-label={`Remove ${t}`} onClick={() => set({ avoid_topics: (c.avoid_topics || []).filter((x) => x !== t) })} className="ds-focus grid size-5 place-items-center rounded-full text-ds-muted hover:text-ds-text"><Icon name="x" size={12} /></button></span>)}
                  <input id="f-avoid" className="h-7 min-w-[160px] flex-1 bg-transparent px-1 text-ds-sm outline-none placeholder:text-ds-muted" placeholder="Type a topic and press Enter" value={avoid} onChange={(e) => setAvoid(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && avoid.trim()) { e.preventDefault(); set({ avoid_topics: [...new Set([...(c.avoid_topics || []), avoid.trim()])] }); setAvoid(""); } }} />
                </div>
              </div>
            </div>
          </Card>
          <div className="flex gap-2"><Button size="lg" onClick={save} disabled={saving}>Save channel</Button><Button variant="ghost" size="lg" onClick={onDone}>Cancel</Button></div>
        </div>
      </div>
    </div>
  );
}
