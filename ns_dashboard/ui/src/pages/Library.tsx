import { useState } from "react";
import { Copy, MessageSquare, Mic, Play, Plus, RotateCcw, Search, Wand2 } from "lucide-react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtDay, fmtDur, store } from "../lib/format";
import type { Footage, Format, Idea, Story, Voice } from "../lib/types";
import { Button, Card, cx, Dropzone, Empty, PageHeader, SegTabs, StatusDot } from "../ui/kit";
import { Scene } from "../visuals/Scene";
import { LayoutDiagram, Waveform } from "../visuals/charts";

type Tab = "ideas" | "stories" | "voices" | "formats" | "footage";
const SUBS: Record<Tab, string> = {
  ideas: "Story ideas waiting to be made. Approved ideas are used first on the next run",
  stories: "Every story Nightshift has written, with how it scored",
  voices: "Narrators for your channels",
  formats: "The shape of each video: canvas, layout and captions. Each one is a preset file",
  footage: "Stock clips kept on this PC for 90 days after a video uses them",
};

export default function Library() {
  const { route } = useApp();
  const [tab, setTabState] = useState<Tab>((route.query.get("tab") as Tab) || store.get<Tab>("ns-lib-tab", "ideas"));
  const setTab = (t: Tab) => { setTabState(t); store.set("ns-lib-tab", t); };
  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader title="Library" sub={SUBS[tab]} right={<SegTabs label="Library" value={tab} onChange={setTab} items={[{ id: "ideas", label: "Ideas" }, { id: "stories", label: "Stories" }, { id: "voices", label: "Voices" }, { id: "formats", label: "Formats" }, { id: "footage", label: "Footage" }]} />} />
      {tab === "ideas" ? <Ideas /> : tab === "stories" ? <Stories /> : tab === "voices" ? <Voices /> : tab === "formats" ? <Formats /> : <FootageTab />}
    </div>
  );
}

const sourceLabel = (s?: string) => { const k = String(s || "").toLowerCase(); return k === "owner" || k === "you" ? "Your idea" : k === "assistant" ? "From the Assistant" : k ? "Written overnight" : ""; };
const IDEA_LABEL: Record<string, string> = { new: "New", approved: "Approved for production", used: "Used", rejected: "Dropped", dropped: "Dropped" };
const IDEA_TONE: Record<string, "muted" | "accent" | "ok" | "bad"> = { new: "muted", approved: "accent", used: "ok", rejected: "bad", dropped: "bad" };

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
    <div className="flex flex-col gap-4">
      <Card className="p-5">
        <div className="grid gap-3 lg:grid-cols-[220px_minmax(0,1fr)_200px_auto]">
          <input aria-label="Title" className="field-input" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
          <input aria-label="Premise" className="field-input" placeholder="What happens, and the turn, in one or two sentences" value={premise} onChange={(e) => setPremise(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
          <select aria-label="Channel" className="field-input" value={chan} onChange={(e) => setChan(e.target.value)}>{chans.map((c) => <option key={c.slug} value={c.slug}>{c.display_name}</option>)}</select>
          <Button variant="primary" icon={<Plus size={16} strokeWidth={1.75} />} onClick={add}>Add idea</Button>
        </div>
      </Card>
      <div className="flex flex-wrap gap-2">
        {[["all", "All", ideas.data.length], ["new", "New", count("new")], ["approved", "Approved for production", count("approved")], ["used", "Used", count("used")], ["rejected", "Dropped", count("rejected")]].map(([k, l, n]) => (
          <button key={k as string} type="button" aria-pressed={filter === k} onClick={() => setFilter(k as string)} className={cx("inline-flex h-8 items-center gap-2 rounded-full border px-3 text-[13px] transition-colors", filter === k ? "border-transparent bg-fill text-on-fill" : "border-line-2 text-text-2 hover:text-text")}>{l}<span className={cx("font-mono text-[12px] num", filter === k ? "opacity-70" : "text-muted")}>{n}</span></button>
        ))}
      </div>
      {shown.length ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-4">
          {shown.map((i) => {
            const s = norm(i.status);
            return (
              <Card key={i.id} className="flex flex-col gap-3 p-5">
                <div className="flex items-center justify-between gap-2 text-[12px]"><span className="inline-flex items-center gap-2 text-text-2"><StatusDot tone={IDEA_TONE[s]} />{IDEA_LABEL[s] || s}</span><span className="truncate text-muted">{channelName(i.channel)}</span></div>
                <div><h3 className="text-[16px] leading-6 font-medium">{i.title || "Untitled idea"}</h3><p className="mt-1 text-[14px] leading-[21px] text-text-2">{i.premise}</p></div>
                <p className="text-[12px] text-muted">{sourceLabel(i.source)}{i.created_at ? ` · ${fmtDay(i.created_at)}` : ""}</p>
                <div className="mt-auto flex flex-wrap gap-2 pt-1">
                  {s === "new" && <Button size="sm" variant="primary" onClick={() => setStatus(i, "approved")}>Approve for production</Button>}
                  {(s === "new" || s === "approved") && <Button size="sm" variant="ghost" onClick={() => setStatus(i, "rejected")}>Drop</Button>}
                  {s === "rejected" && <Button size="sm" variant="secondary" icon={<RotateCcw size={14} strokeWidth={1.5} />} onClick={() => setStatus(i, "new")}>Restore</Button>}
                  <Button size="sm" variant="ghost" icon={<MessageSquare size={14} strokeWidth={1.5} />} onClick={() => ask({ page: "library", idea_id: i.id }, `Develop this idea into a story for ${channelName(i.channel)}: ${i.title}. ${i.premise}`)}>Develop with Assistant</Button>
                </div>
              </Card>
            );
          })}
        </div>
      ) : <Empty title="No ideas here">Add one above, or ask the Assistant for concepts</Empty>}
    </div>
  );
}

function Stories() {
  const { channelName, ask } = useApp(); const [q, setQ] = useState("");
  const stories = useLoad<Story[]>(() => api.get(`/library/stories?q=${encodeURIComponent(q)}`), [q], []);
  const st: Record<string, [string, "accent" | "ok" | "bad" | "muted"]> = { pending: ["Waiting for review", "accent"], posted: ["Posted", "ok"], approved: ["Approved", "ok"], rejected: ["Rejected", "bad"] };
  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-3 border-b border-line p-4"><SearchBox value={q} onChange={setQ} placeholder="Search stories" /></div>
      {stories.data.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-left text-[13px]">
            <thead className="text-[12px] text-muted"><tr className="border-b border-line">{["Story", "Channel", "Words", "Critic score", "Videos", "Status", ""].map((h, i) => <th key={h + i} className={cx("px-4 py-2.5 font-normal", ["Words", "Critic score", "Videos"].includes(h) && "text-right")}>{h}</th>)}</tr></thead>
            <tbody>
              {stories.data.map((s) => (
                <tr key={s.job_id} className="border-b border-line last:border-0 hover:bg-raised/50">
                  <td className="px-4 py-3"><div className="flex items-center gap-3"><span className="h-10 w-16 shrink-0 overflow-hidden rounded-[8px] bg-[#0b0d14]"><Scene seed={s.title} w={640} h={400} className="size-full" /></span><div className="min-w-0"><p className="truncate text-[14px] font-medium">{s.title}</p><p className="text-[12px] text-muted">{s.format === "shorts_only" ? "One Short" : "Full video + Shorts"} · {fmtDay(s.created_at)}</p></div></div></td>
                  <td className="px-4 py-3 text-text-2">{channelName(s.channel)}</td>
                  <td className="px-4 py-3 text-right font-mono num">{s.words.toLocaleString("en-US")}</td>
                  <td className="px-4 py-3 text-right font-mono num">{s.critic_avg.toFixed(1)}</td>
                  <td className="px-4 py-3 text-right font-mono num">{s.outputs}</td>
                  <td className="px-4 py-3"><span className="inline-flex items-center gap-2 text-text-2"><StatusDot tone={(st[s.status] || [s.status, "muted"])[1]} />{(st[s.status] || [s.status])[0]}</span></td>
                  <td className="px-4 py-3"><div className="flex justify-end gap-1"><a href={`#/?tab=${s.status === "pending" ? "ready" : s.status === "rejected" ? "rejected" : "posted"}`} className="rounded-full px-3 py-1.5 text-[13px] text-text-2 no-underline hover:bg-raised hover:text-text">Open</a><button type="button" onClick={() => ask({ page: "library", story_id: s.job_id }, `Reuse the story "${s.title}" as the start of a new video`)} className="rounded-full px-3 py-1.5 text-[13px] text-text-2 hover:bg-raised hover:text-text">Reuse</button></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="p-5"><Empty title="No stories match" /></div>}
    </Card>
  );
}

function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (s: string) => void; placeholder: string }) {
  const [v, setV] = useState(value);
  return (
    <div className="relative w-full max-w-sm"><Search size={16} strokeWidth={1.5} className="pointer-events-none absolute top-1/2 left-3.5 -translate-y-1/2 text-muted" />
      <input aria-label={placeholder} className="field-input h-9 rounded-full pl-10" placeholder={placeholder} value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => e.key === "Enter" && onChange(v)} onBlur={() => onChange(v)} />
    </div>
  );
}

function Voices() {
  const { toast, ask, demo } = useApp(); const voices = useLoad<Voice[]>(() => api.get("/voices"), [], []);
  const [playing, setPlaying] = useState<string | null>(null);
  const [desc, setDesc] = useState("");
  const play = async (v: Voice) => {
    try { setPlaying(v.name); const url = await api.voicePreview(v.name, "The doorbell camera pings at three twelve every night. I stopped checking the clips a week ago."); const a = new Audio(url); a.onended = () => setPlaying(null); await a.play(); }
    catch (e: any) { setPlaying(null); toast(e.message, "warn"); }
  };
  const mode = (m: string) => (m === "preset" ? "Built-in" : m === "design" ? "Designed from a description" : "Cloned from your sample");
  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] content-start gap-4">
        {voices.data.map((v) => (
          <Card key={v.name} className="flex flex-col gap-3 p-5">
            <div className="flex items-start justify-between gap-2"><div><h3 className="text-[16px] font-medium">{v.name}</h3><p className="text-[12px] text-muted">{mode(v.mode)}</p></div><span className="flex size-9 items-center justify-center rounded-full bg-raised text-text-2"><Mic size={16} strokeWidth={1.5} /></span></div>
            {v.description && <p className="text-[13px] text-text-2">{v.description}</p>}
            <Waveform seed={v.name} active={playing === v.name} />
            <Button size="sm" variant="secondary" className="self-start" icon={<Play size={14} strokeWidth={1.5} />} onClick={() => play(v)}>{playing === v.name ? "Playing" : "Play sample"}</Button>
          </Card>
        ))}
      </div>
      <Card className="flex flex-col gap-5 self-start p-5">
        <div><h2 className="text-[17px] font-medium">Make a new voice</h2><p className="mt-1 text-[13px] text-text-2">The first one downloads the voice design model, about 2 GB</p></div>
        <div className="flex flex-col gap-2">
          <label htmlFor="voice-desc" className="text-[13px] font-medium text-text-2">Design from a description</label>
          <textarea id="voice-desc" rows={3} className="field-input" placeholder="Deep, calm older man, slow, a little raspy" value={desc} onChange={(e) => setDesc(e.target.value)} />
          <Button variant="primary" className="self-start" icon={<Wand2 size={16} strokeWidth={1.5} />} disabled={!desc.trim()} onClick={() => { ask({ page: "library", tab: "voices" }, `Design a new narrator voice: ${desc.trim()}`); setDesc(""); }}>Design it</Button>
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-[13px] font-medium text-text-2">Clone from a sample</span>
          <Dropzone compact title="Drop a 3 to 10 second sample" sub="A clean recording of a voice you have the rights to" accept="audio/*" onFile={(f) => toast(demo ? "Cloning needs the engine running" : `Cloning from ${f.name} is not wired to the engine yet. Ask the Assistant to do it`, "warn")} />
        </div>
      </Card>
    </div>
  );
}

function Formats() {
  const { toast, channelName } = useApp(); const formats = useLoad<Format[]>(() => api.get("/library/formats"), [], []);
  const dup = async (f: Format) => { try { await api.post("/library/formats", { ...f, id: f.id + "_copy", name: (f.name || f.id) + " copy", channels: [] }); toast("Duplicated. Edit the preset file to change it", "ok"); formats.reload(); } catch (e: any) { toast(e.message, "bad"); } };
  const groups: [string, string][] = [["long", "Video formats"], ["short", ""], ["clip", "Clip layouts"]];
  return (
    <div className="flex flex-col gap-6">
      {groups.filter(([k]) => k !== "short").map(([k, label]) => {
        const list = formats.data.filter((f) => (k === "long" ? f.kind !== "clip" : f.kind === "clip"));
        return (
          <section key={k}>
            <h2 className="mb-3 px-1 text-[15px] font-medium">{label}</h2>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-4">
              {list.map((f) => {
                const wide = (f.canvas?.w || 9) > (f.canvas?.h || 16);
                return (
                  <Card key={f.id} className="flex gap-5 p-5">
                    <div className="flex h-[112px] w-[104px] shrink-0 items-center justify-center rounded-[14px] bg-raised"><LayoutDiagram id={f.id} w={wide ? 96 : 54} h={wide ? 54 : 96} /></div>
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <h3 className="text-[15px] font-medium">{f.name || f.id}</h3>
                      <p className="font-mono text-[12px] text-muted num">{f.canvas?.w}×{f.canvas?.h} · {f.canvas?.fps} fps</p>
                      <p className="text-[12px] text-text-2">{f.channels?.length ? `Used by ${f.channels.map(channelName).join(", ")}` : "Not used by a channel"}</p>
                      <Button size="sm" variant="ghost" className="mt-auto -ml-3 self-start" icon={<Copy size={14} strokeWidth={1.5} />} onClick={() => dup(f)}>Duplicate</Button>
                    </div>
                  </Card>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function FootageTab() {
  const [q, setQ] = useState(""); const clips = useLoad<Footage[]>(() => api.get(`/library/footage?q=${encodeURIComponent(q)}`), [q], []);
  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-line p-4"><SearchBox value={q} onChange={setQ} placeholder="Search footage by keyword" /><span className="hidden text-[12px] text-muted sm:block">{clips.data.length} clips cached</span></div>
      {clips.data.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-[13px]">
            <thead className="text-[12px] text-muted"><tr className="border-b border-line">{["Keywords", "Source", "Licence", "Length", "Uses", "Last used"].map((h) => <th key={h} className={cx("px-4 py-2.5 font-normal", ["Length", "Uses"].includes(h) && "text-right")}>{h}</th>)}</tr></thead>
            <tbody>
              {clips.data.map((c) => (
                <tr key={c.source + c.id} className="border-b border-line last:border-0 hover:bg-raised/50">
                  <td className="px-4 py-3"><div className="flex items-center gap-3"><span className="h-10 w-16 shrink-0 overflow-hidden rounded-[8px] bg-[#0b0d14]"><Scene seed={c.keywords.join(" ")} w={640} h={400} className="size-full" /></span><span className="font-medium">{c.keywords.join(", ")}</span></div></td>
                  <td className="px-4 py-3 text-text-2 capitalize">{c.source} <span className="font-mono text-[12px] text-muted">#{c.id}</span></td>
                  <td className="px-4 py-3 text-text-2">{c.license}</td>
                  <td className="px-4 py-3 text-right font-mono num">{fmtDur(c.duration)}</td>
                  <td className="px-4 py-3 text-right font-mono num">{c.uses}</td>
                  <td className="px-4 py-3 text-text-2">{fmtDay(c.last_used)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="p-5"><Empty title="No footage cached yet">Clips are kept for 90 days after a video uses them</Empty></div>}
    </Card>
  );
}
