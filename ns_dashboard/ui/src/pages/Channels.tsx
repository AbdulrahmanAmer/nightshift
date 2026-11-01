import { useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, Pencil, Play, Plus, X } from "lucide-react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { POOL_LABELS } from "../lib/format";
import type { Channel, Pools, Voice } from "../lib/types";
import { Button, Card, CheckChip, cx, Empty, Field, PageHeader, SegTabs, StatusDot, Toggle } from "../ui/kit";
import { Scene } from "../visuals/Scene";

const voiceName = (c: Channel) => (typeof c.voice === "string" ? c.voice : c.voice?.name || c.voice?.speaker || "Built-in");
const isClips = (c: Channel) => /clip|stream|podcast/i.test(c.niche || "");

export default function Channels() {
  const { route, refresh, toast, status } = useApp();
  const chans = useLoad<Channel[]>(() => api.get("/channels"), [], []);
  const [editing, setEditing] = useState<string | null>(route.query.get("edit"));
  const toggle = async (c: Channel, on: boolean) => {
    chans.setData((l) => l.map((x) => (x.slug === c.slug ? { ...x, status: on ? "active" : "paused" } : x)));
    try { await api.post(`/channels/${encodeURIComponent(c.slug)}/${on ? "resume" : "pause"}`); toast(on ? `${c.display_name} runs again from tonight` : `${c.display_name} is paused`, "ok"); refresh(); }
    catch (e: any) { toast(e.message, "bad"); chans.reload(); }
  };
  if (editing) {
    const c = editing === "new" ? null : chans.data.find((x) => x.slug === editing);
    if (editing !== "new" && !c) return chans.loading ? null : <Empty title="Channel not found" action={<Button onClick={() => setEditing(null)}>Back to channels</Button>} />;
    return <Editor initial={c} onDone={() => { setEditing(null); chans.reload(); refresh(); }} />;
  }
  const pendingOf = (slug: string) => status?.channels.find((x) => x.slug === slug)?.pending;
  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader title="Channels" sub="Each channel has its own voice, style and schedule" right={<Button variant="primary" icon={<Plus size={16} strokeWidth={1.5} />} onClick={() => setEditing("new")}>Add a channel</Button>} />
      {chans.data.length ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-4">
          {chans.data.map((c) => {
            const on = c.status === "active"; const st = c.stats || {}; const pending = pendingOf(c.slug) ?? c.pending;
            return (
              <Card key={c.slug} className="flex flex-col overflow-hidden p-2">
                <div className="relative h-[148px] overflow-hidden rounded-[12px] bg-[#0b0d14]">
                  <Scene seed={c.display_name + " cover"} kind={isClips(c) ? "clip" : "story"} w={1200} h={480} className={cx("size-full transition-opacity", !on && "opacity-50")} />
                  <span className="glass-chip absolute top-3 left-3 inline-flex h-7 items-center gap-2 rounded-full px-3 text-[12px] font-medium"><StatusDot tone={on ? "ok" : "muted"} />{on ? "Runs tonight" : "Paused"}</span>
                  <span className="glass-chip absolute bottom-3 left-3 inline-flex h-7 items-center rounded-full px-3 text-[12px]">{isClips(c) ? "Clips from your videos" : "Original fiction"}</span>
                </div>
                <div className="flex flex-1 flex-col gap-4 px-3 pt-4 pb-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0"><h2 className="truncate text-[17px] font-medium">{c.display_name}</h2><p className="mt-0.5 line-clamp-2 text-[13px] text-text-2">{c.niche}</p></div>
                    <Toggle on={on} onChange={(v) => toggle(c, v)} label={on ? `Pause ${c.display_name}` : `Resume ${c.display_name}`} />
                  </div>
                  <p className="text-[12px] text-muted">Voice {voiceName(c)} · {c.masters_per_week ?? 1} full video{(c.masters_per_week ?? 1) === 1 ? "" : "s"} a week · posts <span className="font-mono num">{(c.publish_window || "").replace("-", "–")}</span></p>
                  <div className="grid grid-cols-3 gap-3 rounded-[12px] bg-raised p-3">
                    <Stat n={st.week} label="this week" />
                    <Stat n={pending} label="to review" />
                    <Stat n={st.watched_pct} suffix="%" label="avg watched" />
                    <Trend spark={st.spark} />
                  </div>
                  <Button variant="secondary" size="sm" className="self-start" icon={<Pencil size={14} strokeWidth={1.5} />} onClick={() => setEditing(c.slug)}>Edit channel</Button>
                </div>
              </Card>
            );
          })}
        </div>
      ) : <Empty title="No channels yet" action={<Button variant="primary" onClick={() => setEditing("new")}>Add a channel</Button>}>Add one to start making videos</Empty>}
    </div>
  );
}

/** Change in average watched against 14 days ago, as a figure with what it compares to. */
function Trend({ spark }: { spark?: number[] }) {
  if (!spark || spark.length < 2) return null;
  const d = Math.round(spark[spark.length - 1] - spark[0]);
  return <p className="col-span-3 border-t border-line pt-2 text-[12px] text-muted">Watched to the end <span className={cx("font-mono num", d > 0 ? "text-ok" : d < 0 ? "text-bad" : "text-text-2")}>{d > 0 ? "+" : ""}{d} pts</span> vs 14 days ago</p>;
}

function Stat({ n, label, suffix = "" }: { n?: number; label: string; suffix?: string }) {
  return <div className="min-w-0"><p className="font-mono text-[20px] leading-6 num">{n == null ? "–" : `${n}${suffix}`}</p><p className="truncate text-[11px] text-muted">{label}</p></div>;
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
      <PageHeader title={isNew ? "New channel" : c.display_name} sub={isNew ? "A YouTube channel Nightshift makes videos for" : "Changes apply from the next run"} right={<><Button variant="ghost" icon={<ArrowLeft size={16} strokeWidth={1.5} />} onClick={onDone}>Back</Button><Button variant="primary" onClick={save} disabled={saving}>Save channel</Button></>} />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-[200px_minmax(0,1fr)]">
        <nav aria-label="Sections" className="hidden lg:block"><ul className="sticky top-4 flex flex-col gap-1">{SECTIONS.map(([id, l]) => <li key={id}><button type="button" onClick={() => jump(id)} className="h-9 w-full rounded-full px-4 text-left text-[14px] text-text-2 hover:bg-card hover:text-text">{l}</button></li>)}</ul></nav>
        <div className="flex min-w-0 flex-col gap-4">
          <Card id="sec-basics" className="scroll-mt-4 p-6">
            <h2 className="text-[17px] font-medium">Basics</h2>
            <div className="mt-5 grid gap-4 md:grid-cols-2">
              <Field label="Channel name" htmlFor="f-name"><input id="f-name" className="field-input" value={c.display_name} onChange={(e) => set({ display_name: e.target.value, ...(isNew ? { slug: e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") } : {}) })} /></Field>
              <Field label="Short id" htmlFor="f-slug" hint="Used for folder names. Can't change later"><input id="f-slug" className="field-input font-mono" value={c.slug} readOnly={!isNew} onChange={(e) => set({ slug: e.target.value })} /></Field>
              <Field label="What the channel is about" htmlFor="f-niche" hint="One sentence. Every story is written to fit it" className="md:col-span-2"><input id="f-niche" className="field-input" value={c.niche} placeholder="Original short horror fiction set in everyday places" onChange={(e) => set({ niche: e.target.value })} /></Field>
              <Field label="Language" htmlFor="f-lang"><select id="f-lang" className="field-input" value={c.language} onChange={(e) => set({ language: e.target.value })}>{[["en", "English"], ["es", "Spanish"], ["pt", "Portuguese"], ["fr", "French"], ["de", "German"], ["it", "Italian"], ["ja", "Japanese"]].map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
              <Field label="Audience" htmlFor="f-aud" hint={c.audience === "mature" ? "Adds an age-restriction note when a story gets intense" : undefined}><select id="f-aud" className="field-input" value={c.audience} onChange={(e) => set({ audience: e.target.value })}><option value="general">General</option><option value="mature">Mature</option></select></Field>
              <Field label="Style notes" htmlFor="f-notes" className="md:col-span-2"><textarea id="f-notes" rows={2} className="field-input" placeholder="Plain, intimate first person. Short sentences. Dread over gore" value={c.voice_notes} onChange={(e) => set({ voice_notes: e.target.value })} /></Field>
            </div>
          </Card>

          <Card id="sec-voice" className="scroll-mt-4 p-6">
            <div className="flex items-center justify-between"><h2 className="text-[17px] font-medium">Voice</h2><Button size="sm" variant="secondary" icon={<Play size={14} strokeWidth={1.5} />} onClick={hear}>Hear a sample</Button></div>
            <div role="radiogroup" aria-label="Narrator" className="mt-5 grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-5">
              {voices.data.map((v) => { const on = c.voice?.speaker === v.name; return (
                <button key={v.name} type="button" role="radio" aria-checked={on} onClick={() => set({ voice: { ...c.voice, speaker: v.name } })} className={cx("rounded-[14px] border p-3 text-left transition-colors", on ? "border-fill bg-raised" : "border-line-2 hover:bg-raised/60")}>
                  <span className="block text-[14px] font-medium">{v.name}</span><span className="mt-0.5 line-clamp-2 block text-[12px] text-muted">{v.description || (v.mode === "preset" ? "Built-in" : "Custom")}</span>
                </button>); })}
            </div>
            <div className="mt-5"><Field label="Speed"><SegTabs size="sm" label="Speed" value={String(c.voice?.speed ?? 1)} onChange={(v) => set({ voice: { ...c.voice, speed: Number(v) } })} items={[{ id: "0.9", label: "A little slower" }, { id: "1", label: "Normal" }, { id: "1.1", label: "A little faster" }]} /></Field></div>
          </Card>

          <Card id="sec-schedule" className="scroll-mt-4 p-6">
            <h2 className="text-[17px] font-medium">Schedule</h2>
            <div className="mt-5 flex flex-wrap gap-x-10 gap-y-5">
              <Field label="Full videos per week" hint="New channels start at 1"><SegTabs size="sm" label="Full videos per week" value={String(c.masters_per_week ?? 1)} onChange={(v) => set({ masters_per_week: Number(v) })} items={[{ id: "1", label: "1" }, { id: "2", label: "2" }, { id: "3", label: "3" }]} /></Field>
              <Field label="Post between" hint="Your local time">
                <div className="flex items-center gap-2"><input aria-label="From" type="time" className="field-input w-[120px] font-mono" value={from} onChange={(e) => set({ publish_window: `${e.target.value}-${to}` })} /><span className="text-muted">to</span><input aria-label="To" type="time" className="field-input w-[120px] font-mono" value={to} onChange={(e) => set({ publish_window: `${from}-${e.target.value}` })} /></div>
              </Field>
              <Field label="Default type"><SegTabs size="sm" label="Default type" value={c.format || "master_first"} onChange={(v) => set({ format: v })} items={[{ id: "master_first", label: "Full video + Shorts" }, { id: "shorts_only", label: "Shorts only" }]} /></Field>
            </div>
          </Card>

          <Card id="sec-style" className="scroll-mt-4 p-6">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-[17px] font-medium">Content style</h2><p className="mt-1 text-[13px] text-text-2">Nightshift mixes these so videos never look the same twice</p></div>
              <label className="flex items-center gap-3 rounded-full bg-raised py-1.5 pr-1.5 pl-4 text-[13px]"><span>Add <q>Original fiction</q> to every description</span><Toggle size="md" on={!!c.fiction_label} onChange={(v) => set({ fiction_label: v })} label="Original fiction label" /></label>
            </div>
            {warnings.length > 0 && <div role="alert" className="mt-4 flex flex-col gap-1 rounded-[14px] bg-accent-soft px-4 py-3 text-[13px]">{warnings.map((w) => <p key={w} className="flex items-start gap-2"><AlertTriangle size={16} strokeWidth={1.5} className="mt-0.5 shrink-0 text-accent-ink" />{w}</p>)}</div>}
            <div className="mt-5 flex flex-col gap-5">
              {(Object.keys(POOL_LABELS) as (keyof Pools)[]).map((pool) => (
                <div key={pool}><p className="mb-2 text-[13px] font-medium text-text-2">{POOL_LABELS[pool].label} <span className="font-mono font-normal text-muted num">{pools[pool].length}</span></p>
                  <div className="flex flex-wrap gap-2">{Object.entries(POOL_LABELS[pool].opts).map(([k, l]) => <CheckChip key={k} on={pools[pool].includes(k)} onChange={(on) => togglePool(pool, k, on)}>{l}</CheckChip>)}</div></div>
              ))}
              <div><label htmlFor="f-avoid" className="mb-2 block text-[13px] font-medium text-text-2">Topics to avoid</label>
                <div className="flex flex-wrap items-center gap-2 rounded-[12px] border border-line-2 p-2">
                  {(c.avoid_topics || []).map((t) => <span key={t} className="inline-flex h-7 items-center gap-1 rounded-full bg-raised pr-1 pl-3 text-[13px]">{t}<button type="button" aria-label={`Remove ${t}`} onClick={() => set({ avoid_topics: (c.avoid_topics || []).filter((x) => x !== t) })} className="flex size-5 items-center justify-center rounded-full text-muted hover:text-text"><X size={12} strokeWidth={1.5} /></button></span>)}
                  <input id="f-avoid" className="h-7 min-w-[160px] flex-1 bg-transparent px-1 text-[13px] outline-none placeholder:text-muted" placeholder="Type a topic and press Enter" value={avoid} onChange={(e) => setAvoid(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && avoid.trim()) { e.preventDefault(); set({ avoid_topics: [...new Set([...(c.avoid_topics || []), avoid.trim()])] }); setAvoid(""); } }} />
                </div>
              </div>
            </div>
          </Card>
          <div className="flex gap-2"><Button variant="primary" size="lg" onClick={save} disabled={saving}>Save channel</Button><Button variant="ghost" size="lg" onClick={onDone}>Cancel</Button></div>
        </div>
      </div>
    </div>
  );
}
