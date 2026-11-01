import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Copy, ExternalLink, FolderOpen, Image, MessageSquare, Play, Undo2 } from "lucide-react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtClock, fmtDay, fmtDur, fmtWhen, kindLabel, store } from "../lib/format";
import type { Output, OutputDetail } from "../lib/types";
import { Button, Card, cx, Empty, Keycap, KindTag, LinkButton, PageHeader, SegTabs } from "../ui/kit";
import { Scene } from "../visuals/Scene";

type Tab = "ready" | "posted" | "rejected";
const STATUS: Record<Tab, string> = { ready: "pending", posted: "approved,uploaded", rejected: "rejected" };

export function Poster({ o, className, portrait }: { o: Pick<Output, "title" | "kind" | "output_id">; className?: string; portrait?: boolean }) {
  const p = portrait ?? o.kind !== "long";
  return <Scene seed={o.title} kind={o.kind === "clip" ? "clip" : "story"} w={p ? 900 : 1600} h={p ? 1600 : 900} className={className} title={`Still for ${o.title}`} />;
}

export default function Queue() {
  const { route, status, refresh, channelName } = useApp();
  const [tab, setTab] = useState<Tab>((route.query.get("tab") as Tab) || "ready");
  const list = useLoad<Output[]>(() => api.get(`/outputs?status=${STATUS[tab]}`), [tab], []);
  const [current, setCurrent] = useState<string | null>(route.query.get("id"));
  const items = list.data;
  const o = items.find((x) => x.output_id === current) || items[0];
  const counts = status?.outbox;

  const newest = items.reduce<string | null>((m, x) => (!m || x.created_at > m ? x.created_at : m), null);
  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader
        title={tab === "ready" ? ((counts?.pending ?? items.length) ? `${counts?.pending ?? items.length} ready to review` : "Queue") : tab === "posted" ? "Approved and posted" : "Rejected"}
        sub={tab === "ready" ? (items.length ? <>Made overnight, newest at <span className="font-mono num">{fmtClock(newest)}</span>. Watch, approve, then post in this order</> : "Nothing waiting") : tab === "posted" ? "Approved videos wait in the Outbox folder until you post them" : "Kept for 7 days in the rejected folder, then deleted"}
        right={<SegTabs label="Queue" value={tab} onChange={(t) => { setTab(t); setCurrent(null); }} items={[{ id: "ready", label: "Ready", count: counts?.pending }, { id: "posted", label: "Approved & posted", count: counts?.approved }, { id: "rejected", label: "Rejected", count: counts?.rejected }]} />}
      />
      {list.loading && !items.length ? <div className="h-[460px] rounded-[20px] bg-card/60" /> :
        tab === "ready" ? (o ? <Review key={o.output_id} o={o} items={items} onPick={setCurrent} onChanged={async (next) => { await refresh(); list.reload(); setCurrent(next); }} channelName={channelName} /> :
          <Empty title="Nothing to review" action={<LinkButton href="#/create" variant="primary">Make something now</LinkButton>}>Everything made so far is approved or posted. New videos appear here after tonight's run.</Empty>) :
          <ListView items={items} tab={tab} onResume={(id) => { setTab("ready"); setCurrent(id); location.hash = `#/?id=${encodeURIComponent(id)}`; }} channelName={channelName} />}
    </div>
  );
}

function ListView({ items, tab, onResume, channelName }: { items: Output[]; tab: Tab; onResume: (id: string) => void; channelName: (s?: string) => string }) {
  if (!items.length) return <Empty title="Nothing here yet">{tab === "posted" ? "Approved videos show up here until you paste their YouTube link" : "Rejected videos and your reasons show up here"}</Empty>;
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-4">
      {items.map((x) => (
        <Card key={x.output_id} className="flex gap-4 p-3">
          <div className={cx("relative shrink-0 overflow-hidden rounded-[12px] bg-[#0b0d14]", x.kind === "long" ? "h-[72px] w-[128px]" : "h-[112px] w-[63px]")}><Poster o={x} className="size-full" /></div>
          <div className="flex min-w-0 flex-1 flex-col gap-1 py-1">
            <KindTag kind={x.kind} />
            <p className="line-clamp-2 text-[14px] leading-5 font-medium">{x.title}</p>
            <p className="text-[12px] text-muted">{channelName(x.channel)} · {fmtDay(x.created_at)}</p>
            <div className="mt-auto flex items-center gap-2 pt-1 text-[13px]">
              {x.status === "uploaded" && x.url ? <a href={x.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-text-2 hover:text-text"><ExternalLink size={14} strokeWidth={1.5} />Posted on YouTube</a>
                : x.status === "approved" ? <><span className="text-text-2">Not posted yet</span><Button size="sm" variant="secondary" className="ml-auto" onClick={() => onResume(x.output_id)}>Finish posting</Button></>
                : <span className="text-text-2">{x.reason ? `Reason: ${x.reason}` : "No reason given"}</span>}
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

type Prog = Record<string, boolean>;
function Review({ o, items, onPick, onChanged, channelName }: { o: Output; items: Output[]; onPick: (id: string) => void; onChanged: (nextId: string | null) => Promise<void>; channelName: (s?: string) => string }) {
  const { toast, confirm, ask, demo } = useApp();
  const detail = useLoad<OutputDetail | null>(() => api.get(`/outputs/${api.enc(o.output_id)}`), [o.output_id], null);
  const d = detail.data;
  const [prog, setProg] = useState<Prog>(() => store.get<Prog>("ns-post-" + o.output_id, {}));
  const [url, setUrl] = useState("");
  const [open, setOpen] = useState(false);
  const approved = o.status !== "pending" || !!prog.approved;
  const idx = items.findIndex((x) => x.output_id === o.output_id);
  const nextId = items[(idx + 1) % items.length]?.output_id ?? null;
  const portrait = o.kind !== "long";

  const mark = useCallback((k: string, v = true) => setProg((p) => { const n = { ...p, [k]: v }; store.set("ns-post-" + o.output_id, n); return n; }), [o.output_id]);
  const description = useMemo(() => {
    if (!d) return "";
    const ch = d.chapters?.length ? "\n\n" + d.chapters.map((c) => `${fmtDur(c.start_s)} ${c.title}`).join("\n") : "";
    return (d.description || "") + ch + (d.hashtags?.length ? "\n\n" + d.hashtags.join(" ") : "");
  }, [d]);
  const copy = async (text: string, label: string, key: string) => {
    try { await navigator.clipboard.writeText(text); toast(`${label} copied`, "ok"); mark(key); }
    catch { toast("Copy was blocked. Select the text and press Ctrl+C", "warn"); }
  };
  const approve = async () => { try { await api.post(`/outputs/${api.enc(o.output_id)}/approve`); mark("approved"); toast("Approved", "ok"); } catch (e: any) { toast(e.message, "bad"); } };
  const reject = async () => {
    const reason = await confirm({ title: "Reject this video?", body: "A short reason helps Nightshift do better next time", ok: "Reject", danger: true, input: { label: "Reason", placeholder: "The ending made no sense" } });
    if (reason === null) return;
    try { await api.post(`/outputs/${api.enc(o.output_id)}/reject`, { reason }); toast("Rejected", "warn"); await onChanged(items.filter((x) => x.output_id !== o.output_id)[Math.min(idx, items.length - 2)]?.output_id ?? null); } catch (e: any) { toast(e.message, "bad"); }
  };
  const done = async () => {
    if (!/^https?:\/\/\S+/.test(url.trim())) { toast("Paste the full YouTube link first", "warn"); return; }
    try { await api.post(`/outputs/${api.enc(o.output_id)}/uploaded`, { url: url.trim() }); toast("Posted. Nightshift will learn from this video", "ok"); await onChanged(items.filter((x) => x.output_id !== o.output_id)[Math.min(idx, items.length - 2)]?.output_id ?? null); } catch (e: any) { toast(e.message, "bad"); }
  };
  const openFolder = async () => {
    try { await api.post(`/outputs/${api.enc(o.output_id)}/open-folder`); mark("folder"); }
    catch { if (o.path) await copy(o.path, "File path", "folder"); }
  };

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input,textarea,select,[role=dialog]") || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "a" && !approved) { e.preventDefault(); approve(); }
      else if (k === "r" && !approved) { e.preventDefault(); reject(); }
      else if (e.key === "ArrowRight" && nextId) { e.preventDefault(); onPick(nextId); }
    };
    addEventListener("keydown", on); return () => removeEventListener("keydown", on);
  });

  type Step = { key: string; title: string; sub?: string; action?: JSX.Element };
  const steps: Step[] = [
    { key: "folder", title: "Open the video file", sub: o.path || "In the Outbox folder", action: <Button size="sm" variant="secondary" icon={<FolderOpen size={14} strokeWidth={1.5} />} onClick={openFolder}>Open</Button> },
    { key: "studio", title: "Open YouTube Studio", sub: "Start an upload and drag the file in", action: <a href="https://studio.youtube.com/" target="_blank" rel="noopener noreferrer" onClick={() => mark("studio")} className="inline-flex h-8 items-center gap-1.5 rounded-full border border-line-2 bg-card px-3 text-[13px] font-medium no-underline hover:bg-raised"><ExternalLink size={14} strokeWidth={1.5} />Studio</a> },
    { key: "title", title: "Title", sub: o.title, action: <Button size="sm" variant="secondary" icon={<Copy size={14} strokeWidth={1.5} />} onClick={() => copy(o.title, "Title", "title")}>Copy</Button> },
    { key: "description", title: "Description", sub: d?.chapters?.length ? `With ${d.chapters.length} chapters and ${d.hashtags?.length ?? 0} hashtags` : `With ${d?.hashtags?.length ?? 0} hashtags`, action: <Button size="sm" variant="secondary" icon={<Copy size={14} strokeWidth={1.5} />} onClick={() => copy(description, "Description", "description")} disabled={!d}>Copy</Button> },
    { key: "thumb", title: "Thumbnail", sub: o.kind === "long" ? "Three were made for this video" : "Pick a frame in Studio", action: o.kind === "long" && !demo ? <a href={api.thumbUrl(o.output_id)} target="_blank" rel="noopener noreferrer" onClick={() => mark("thumb")} className="inline-flex h-8 items-center gap-1.5 rounded-full border border-line-2 bg-card px-3 text-[13px] font-medium no-underline hover:bg-raised"><Image size={14} strokeWidth={1.5} />Open</a> : <Button size="sm" variant="ghost" onClick={() => mark("thumb")}>Done</Button> },
    { key: "audience", title: "Audience", sub: d?.made_for_kids ? "Yes, it's made for kids" : "No, it's not made for kids", action: <Button size="sm" variant="ghost" onClick={() => mark("audience")}>Done</Button> },
    { key: "altered", title: "Altered content", sub: d?.ai_disclosure === "yes" ? "Answer Yes" : "Answer No. The voice is production help, footage is real stock", action: <Button size="sm" variant="ghost" onClick={() => mark("altered")}>Done</Button> },
    { key: "tags", title: "Tags", sub: d?.tags?.length ? `${d.tags.length} tags, ${d.tags.slice(0, 3).join(", ")}` : "No tags", action: <Button size="sm" variant="secondary" icon={<Copy size={14} strokeWidth={1.5} />} onClick={() => copy((d?.tags || []).join(", "), "Tags", "tags")} disabled={!d}>Copy</Button> },
    { key: "schedule", title: "Schedule and related video", sub: [d?.publish_after ? `Publish ${fmtWhen(d.publish_after)}` : "Any time", o.kind !== "long" && d?.related_video_instruction].filter(Boolean).join(". "), action: <Button size="sm" variant="ghost" onClick={() => mark("schedule")}>Done</Button> },
  ];
  const doneCount = (approved ? 1 : 0) + steps.filter((s) => prog[s.key]).length;
  const total = steps.length + 2;

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
      <div className="flex min-w-0 flex-col gap-4">
        {/* hero player */}
        <div className="relative overflow-hidden rounded-[24px] bg-[#0b0d14] shadow-card">
          <div className={cx("relative", portrait ? "aspect-[16/10]" : "aspect-video")}>
            {portrait && <div aria-hidden className="absolute inset-0 scale-110 opacity-50 blur-2xl"><Poster o={o} portrait={false} className="size-full" /></div>}
            <div className={cx("absolute inset-0", portrait && "flex justify-center py-5")}>
              {demo ? (
                <div className={cx("relative overflow-hidden", portrait ? "aspect-[9/16] h-full rounded-[16px] shadow-float" : "size-full")}>
                  <Poster o={o} className="size-full" />
                  <span className="absolute inset-0 flex items-center justify-center"><span className="glass-chip flex size-16 items-center justify-center rounded-full shadow-float" aria-hidden><Play size={24} strokeWidth={1.5} className="translate-x-0.5" fill="currentColor" /></span></span>
                  <span className="sr-only">Video preview. With demo data there is no video file to play</span>
                </div>
              ) : (
                <video key={o.output_id} controls preload="metadata" src={api.mediaUrl(o.output_id)} poster={api.thumbUrl(o.output_id)} className={cx("bg-black", portrait ? "aspect-[9/16] h-full rounded-[16px]" : "size-full")} />
              )}
            </div>
            <div className="pointer-events-none absolute top-4 left-4 flex flex-wrap gap-2">
              <span className="glass-chip inline-flex h-8 items-center rounded-full px-3 text-[13px] font-medium">{kindLabel(o.kind)}{o.k ? ` ${o.k}` : ""}</span>
              <span className="glass-chip inline-flex h-8 items-center rounded-full px-3 font-mono text-[13px] num">{fmtDur(o.duration_s)}</span>
              <span className="glass-chip inline-flex h-8 items-center rounded-full px-3 text-[13px]">{channelName(o.channel)}</span>
            </div>
            <span className="glass-chip pointer-events-none absolute top-4 right-4 inline-flex h-8 items-center rounded-full px-3 font-mono text-[13px] num">{idx + 1} of {items.length}</span>
          </div>
        </div>
        <div className="flex flex-wrap items-start justify-between gap-3 px-1">
          <div className="min-w-0">
            <h2 className="text-[22px] leading-7 font-medium">{o.title}</h2>
            <p className="mt-1 text-[13px] text-muted">Made <span className="font-mono num">{fmtWhen(o.created_at)}</span>{d?.critic_avg ? <> · critic score <span className="font-mono num">{d.critic_avg.toFixed(1)}</span></> : null}{d?.words ? <> · <span className="font-mono num">{d.words.toLocaleString("en-US")}</span> words</> : null}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" icon={<MessageSquare size={14} strokeWidth={1.5} />} onClick={() => ask({ page: "queue", output_id: o.output_id }, `Tell me about "${o.title}"`)}>Ask about it</Button>
            {items.length > 1 && <Button variant="secondary" size="sm" onClick={() => nextId && onPick(nextId)}>Next video <Keycap>→</Keycap></Button>}
          </div>
        </div>

        <Card className="overflow-hidden">
          <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left hover:bg-raised/50">
            <span className="text-[15px] font-medium">The story and why it passed checks</span>
            <ChevronDown size={18} strokeWidth={1.5} className={cx("text-muted transition-transform duration-200", open && "rotate-180")} />
          </button>
          {open && (
            <div className="fade-in grid gap-5 border-t border-line px-5 py-4 md:grid-cols-[1.4fr_1fr]">
              <p className="text-[15px] leading-6 text-text-2">{d?.story_excerpt || "No excerpt saved for this video"}</p>
              <ul className="flex flex-col gap-2">
                {(d?.policy_notes || []).map((n) => <li key={n} className="flex gap-2 text-[13px]"><Check size={16} strokeWidth={1.75} className="mt-0.5 shrink-0 text-ok" />{n}</li>)}
              </ul>
            </div>
          )}
        </Card>

        {items.length > 1 && (
          <section aria-label="Other ready videos">
            <div className="mb-3 flex items-baseline justify-between px-1"><h3 className="text-[15px] font-medium">Also ready</h3><span className="text-[12px] text-muted">{items.length - 1} more</span></div>
            <div className="flex gap-3 overflow-x-auto pb-2 scroll-thin">
              {items.filter((x) => x.output_id !== o.output_id).map((x) => (
                <button key={x.output_id} type="button" onClick={() => onPick(x.output_id)} className="group flex w-[220px] shrink-0 flex-col gap-2 rounded-[16px] border border-line bg-card p-2 text-left shadow-sm transition-colors hover:border-line-2">
                  <div className="relative aspect-video overflow-hidden rounded-[10px] bg-[#0b0d14]">
                    <Poster o={x} portrait={false} className="size-full transition-transform duration-300 group-hover:scale-[1.02]" />
                    <span className="glass-chip absolute right-2 bottom-2 rounded-full px-2 py-0.5 font-mono text-[11px] num">{fmtDur(x.duration_s)}</span>
                  </div>
                  <div className="px-1 pb-1"><KindTag kind={x.kind} /><p className="mt-0.5 line-clamp-2 text-[13px] leading-[18px] font-medium">{x.title}</p></div>
                </button>
              ))}
            </div>
          </section>
        )}
      </div>

      {/* posting checklist */}
      <aside aria-label="Posting checklist" className="xl:sticky xl:top-4 xl:self-start">
        <Card className="flex flex-col">
          <div className="flex items-center justify-between px-5 pt-5 pb-3">
            <h2 className="text-[17px] font-medium">Post it</h2>
            <span className="font-mono text-[12px] text-muted num">{doneCount} of {total} done</span>
          </div>
          <ol className="flex flex-col px-3 pb-3">
            <li className={cx("mx-0 mb-1 rounded-[14px] p-3", approved ? "" : "bg-raised")}>
              <div className="flex items-start gap-3">
                <StepNum n={1} done={approved} />
                <div className="min-w-0 flex-1">
                  <p className="text-[14px] font-medium">{approved ? "Approved" : "Watch it, then decide"}</p>
                  {approved ? <p className="text-[12px] text-muted">Ready to post</p> : (
                    <>
                      <div className="mt-2.5 flex gap-2"><Button variant="accent" className="flex-1" onClick={approve}>Approve</Button><Button variant="danger" onClick={reject}>Reject</Button></div>
                      <p className="mt-2.5 flex flex-wrap items-center gap-1.5 text-[12px] text-muted"><Keycap>A</Keycap> approve <Keycap>R</Keycap> reject <Keycap>→</Keycap> next</p>
                    </>
                  )}
                </div>
                {approved && o.status === "pending" && <Button size="sm" variant="ghost" icon={<Undo2 size={14} strokeWidth={1.5} />} onClick={() => mark("approved", false)}>Undo</Button>}
              </div>
            </li>
            {steps.map((s, i) => (
              <li key={s.key} className={cx("flex items-center gap-3 rounded-[14px] px-3 py-2.5 transition-opacity", !approved && "opacity-45")} aria-disabled={!approved}>
                <StepNum n={i + 2} done={!!prog[s.key]} />
                <div className="min-w-0 flex-1"><p className="text-[14px] leading-5">{s.title}</p>{s.sub && <p className="truncate text-[12px] leading-4 text-muted" title={s.sub}>{s.sub}</p>}</div>
                <fieldset disabled={!approved} className="contents">{s.action}</fieldset>
              </li>
            ))}
            <li className={cx("mt-1 rounded-[14px] border border-line p-3", !approved && "opacity-45")}>
              <div className="flex items-start gap-3">
                <StepNum n={steps.length + 2} />
                <div className="min-w-0 flex-1">
                  <label htmlFor="yt-url" className="text-[14px]">Paste the YouTube link</label>
                  <div className="mt-2 flex gap-2"><input id="yt-url" className="field-input h-9" placeholder="https://youtu.be/" value={url} onChange={(e) => setUrl(e.target.value)} disabled={!approved} onKeyDown={(e) => e.key === "Enter" && done()} /><Button variant="primary" size="sm" className="h-9" onClick={done} disabled={!approved}>Done</Button></div>
                  <p className="mt-2 text-[12px] text-muted">The link lets Nightshift learn from this video's results</p>
                </div>
              </div>
            </li>
          </ol>
        </Card>
        <p className="mt-3 px-2 text-[12px] text-muted">Ticked steps are remembered for each video</p>
      </aside>
    </div>
  );
}

function StepNum({ n, done }: { n: number; done?: boolean }) {
  return <span className={cx("flex size-7 shrink-0 items-center justify-center rounded-full font-mono text-[12px] num transition-colors", done ? "bg-fill text-on-fill" : "border border-line-2 text-text-2")}>{done ? <Check size={14} strokeWidth={2} aria-label="Done" /> : n}</span>;
}
