/* Queue (home): the ref12 recreation's composition with Nightshift's real data.
     6 columns  greeting greeting f1 f2 f3 countdown
                review   review   review review post post
                events   events   plan plan   cal  cal
                renders  renders  renders promo meters meters
   4 columns: greeting x4 / f1 f2 f3 countdown / review x4 / post x4 / events plan / cal meters / renders x3 promo.
   Behaviour is unchanged from the previous page: same API calls, posting checklist, remembered progress, keys A / R / →. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { dayInline, doneWords, MAKES_VIDEO, fmtClock, fmtDay, fmtDur, fmtWhen, kindLabel, nightComes, plural, stageWords, stoppedAt, store } from "../lib/format";
import type { ChannelSummary, InterruptedJob, Job, LeftForTime, MorningNote, Output, OutputDetail, Run } from "../lib/types";
import { cx } from "../ui/kit";
import { JobError } from "../ui/JobError";
import { Scene } from "../visuals/Scene";
import {
  AssignmentCard, AvatarGroup, Badge, Button, CalendarCard, Card, CardHeader, ClipsIllustration, CountdownCard, CreateTile, DashGrid, EmptyState,
  FeatureCard, Greeting, Icon, Input, Kbd, LinkButton, MeterGroup, MetaItem, NotificationItem, PromoCard, SegmentedControl, SeriesIllustration,
  SPAN, StatusDot, StepItem, StoryIllustration, TaskList, VoiceIllustration, WorkspaceTokens,
  type AgendaSlot, type IconName, type MenuItem, type MeterDatum, type Task, type WeekDay,
} from "../components/ref12";

type Tab = "ready" | "posted" | "rejected";
// Ready is what still needs the owner: a video to review, or an approved one that is not posted yet. An approved
// video used to leave this tab when the list reloaded (within about ten seconds), and its posting checklist
// (copy the title, the description and the tags, paste the YouTube link) could not be opened again (F-H143, F-H164).
const STATUS: Record<Tab, string> = { ready: "pending,approved", posted: "approved,uploaded", rejected: "rejected" };
// A test output can be approved but never posted, so it has no checklist to finish.
export const needsOwner = (x: Pick<Output, "status" | "profile">) => x.status === "pending" || (x.status === "approved" && !x.profile);
// The video on screen: the one the owner picked, else the first to review, else the first to finish posting.
export const pickOutput = <T extends Pick<Output, "output_id" | "status">>(items: T[], current: string | null) =>
  items.find((x) => x.output_id === current) || items.find((x) => x.status === "pending") || items[0];

export function Poster({ o, className, portrait }: { o: Pick<Output, "title" | "kind" | "output_id">; className?: string; portrait?: boolean }) {
  const p = portrait ?? o.kind !== "long";
  return <Scene seed={o.title} kind={o.kind === "clip" ? "clip" : "story"} w={p ? 900 : 1600} h={p ? 1600 : 900} className={className} title={`Still for ${o.title}`} />;
}

export function jobTitle(j: Job, channelName: (s?: string | null) => string) {
  if (j.kind === "apply_voice") return j.params?.name ? `Making the voice ${j.params.name}` : "Making the setup voice";
  if (j.kind === "clip") return (j.params?.title ?? j.title) ? `Clips from ${j.params?.title ?? j.title}` : `Clips for ${channelName(j.channel)}`;
  return (j.params?.title ?? j.title) || `Story video for ${channelName(j.channel)}`;
}
const kindIcon = (k: string): IconName => (k === "long" ? "monitor" : k === "short" ? "phone" : "scissors");
const partOfDay = () => { const h = new Date().getHours(); return h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; };

export default function Queue() {
  const { route, status, refresh, channelName, go, ask, ownerName } = useApp();
  const [tab, setTab] = useState<Tab>((route.query.get("tab") as Tab) || "ready");
  // reloaded when the outbox counts move (a job just finished): the first dev run showed "Nothing to review"
  // under a "Ready 2" tab because the list was loaded once and only the counts kept polling
  const ob = status?.outbox;
  const list = useLoad<Output[]>(() => api.get(`/outputs?status=${STATUS[tab]}`), [tab, ob?.pending, ob?.approved, ob?.rejected, ob?.uploaded], []);
  const [current, setCurrent] = useState<string | null>(route.query.get("id"));
  // Job history for the events and render cards: loaded again whenever the engine changes state or the running job moves on
  // (its progress or stage), so the bars follow the job instead of freezing at the value read when it started.
  const jobs = useLoad<Job[]>(() => api.get("/jobs"), [status?.engine?.state, status?.job?.job_id, status?.job?.progress, status?.job?.message, status?.queue?.queued], []);
  const items = tab === "ready" ? list.data.filter(needsOwner) : list.data;
  const o = pickOutput(items, current);
  const counts = status?.outbox;
  const chans = status?.channels || [];
  const newest = items.reduce<string | null>((m, x) => (!m || x.created_at > m ? x.created_at : m), null);
  const pending = counts?.pending ?? (tab === "ready" ? items.length : 0);
  const owner = chans.find((c) => c.status === "active")?.display_name || chans[0]?.display_name;
  // only an enabled nightly task has a next run (Automatic runs on); off shows no countdown and no time
  const scheduled = nightComes(status);
  const nextRun = scheduled ? new Date(status!.next_run!) : null;

  const series = () => { ask({ page: "queue" }, "Draft 3 story concepts for a series", false); go("#/assistant"); };
  const pick = (t: Tab) => { setTab(t); setCurrent(null); };
  const reviewDescription = (
    <>
      {owner && !ownerName ? `${partOfDay()}. ` : ""}{pending ? <>{pending === 1 ? "One video is" : `${pending} videos are`} ready to review{newest && tab === "ready" ? <>, the newest made at <span className="ds-num">{fmtClock(newest)}</span></> : null}.</> : "Nothing is waiting for review."}{" "}
      {scheduled ? <>Tonight's run starts at <span className="ds-num">{fmtClock(status!.next_run!)}</span>.</> : status?.schedule_registered ? "No channel is active, so nothing runs tonight." : "Automatic runs are off."}
    </>
  );

  return (
    <div className="enter">
      <DashGrid label="Queue">
        <Greeting as="h2" size="lg" className={cx("self-center", SPAN["2/4"])} salutation={ownerName ? `${partOfDay()}, ${ownerName}` : owner || partOfDay()} question="What are we making tonight?"
          description={reviewDescription} adornment={chans.length ? <WorkspaceTokens tokens={chans.slice(0, 3).map((c) => ({ icon: /clip|stream|podcast/i.test(c.niche || "") ? "scissors" : "channels", label: c.display_name }))} /> : undefined} />
        <FeatureCard className={cx("self-stretch", SPAN["1/1"])} illustration={<StoryIllustration />} title="Write a story" description="An original story, narrated, with footage and captions" href="#/create?kind=story" />
        <FeatureCard className={cx("self-stretch", SPAN["1/1"])} illustration={<ClipsIllustration />} title="Cut clips" description="Vertical clips from your own stream or podcast" href="#/create?kind=clips" />
        <FeatureCard className={cx("self-stretch", SPAN["1/1"])} illustration={<SeriesIllustration />} title="Build a series" description="Plan the parts with the Assistant, then make them" onClick={series} />
        <CountdownCard className={cx("self-stretch", SPAN["1/1"])} title="Next automatic run" target={nextRun} emptyText={status?.schedule_registered ? "No channel is active, so nothing runs tonight" : "Automatic runs are off. Switch them on in Settings"}
          caption={scheduled ? `Every night at ${status!.schedule_time}${status!.schedule_mode === "interactive_only" ? ", only while you are signed in" : ""}` : undefined} />

        {tab === "ready" && o && !list.loading ? (
          <Review key={o.output_id} o={o} items={items} tab={tab} onTab={pick} onPick={setCurrent} onChanged={async (next) => { await refresh(); list.reload(); setCurrent(next); }} channelName={channelName} />
        ) : (
          <Card as="section" aria-labelledby="q-list" className={SPAN["6/4"]}>
            <CardHeader id="q-list" title={tab === "ready" ? "Ready to review" : tab === "posted" ? "Approved and posted" : "Rejected"} actions={<QueueTabs tab={tab} onTab={pick} />} />
            {list.loading && !items.length ? <div aria-busy="true" className="h-[320px] rounded-ds-lg bg-ds-sunken" />
              : tab === "ready" ? <EmptyState icon="film" title="Nothing to review" action={<LinkButton href="#/create" variant="primary">Make something now</LinkButton>}>Everything made so far is posted or rejected. New videos appear here {nightComes(status) ? "after tonight's run" : "when you make them"}</EmptyState>
              : <ListView items={items} tab={tab} onResume={(id) => { setTab("ready"); setCurrent(id); location.hash = `#/?id=${encodeURIComponent(id)}`; }} channelName={channelName} />}
          </Card>
        )}

        <LastNight className={SPAN["6/4"]} pending={pending} onReview={() => pick("ready")} channelName={channelName} />
        <JobEvents jobs={jobs.data} loading={jobs.loading} channelName={channelName} className={SPAN["2/2"]} onReview={() => pick("ready")} />
        <TonightPlan chans={chans} status={status} className={SPAN["2/2"]} />
        <PostingCalendar chans={chans} ready={tab === "ready" ? items : []} className={SPAN["2/2"]} onOpen={(id) => { setTab("ready"); setCurrent(id); }} />
        <MachineMeters className={cx("self-stretch @min-[1020px]:order-last", SPAN["2/2"])} />
        <RenderJobs jobs={jobs.data} chans={chans} channelName={channelName} className={cx("self-stretch", SPAN["3/2"])} />
        <PromoCard className={cx("self-stretch", SPAN["1/2"])} illustration={<VoiceIllustration />} title="Train a voice" description="Design a narrator from a few words, or clone one from a short sample" ctaLabel="Open Voices" onCta={() => go("#/library?tab=voices")} />
      </DashGrid>
    </div>
  );
}

function QueueTabs({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const { status } = useApp(); const c = status?.outbox;
  const n = (v?: number) => (v ? ` ${v}` : "");
  return <SegmentedControl label="Queue" size="sm" value={tab} onChange={onTab} options={[{ value: "ready", label: `Ready${n(c?.pending)}` }, { value: "posted", label: `Approved and posted${n(c?.approved)}` }, { value: "rejected", label: `Rejected${n(c?.rejected)}` }]} />;
}

function ListView({ items, tab, onResume, channelName }: { items: Output[]; tab: Tab; onResume: (id: string) => void; channelName: (s?: string | null) => string }) {
  if (!items.length) return <EmptyState icon="film" title="Nothing here yet">{tab === "posted" ? "Approved videos show up here until you paste their YouTube link" : "Rejected videos and your reasons show up here"}</EmptyState>;
  return (
    <div className="@container">
      <ul className="grid grid-cols-1 gap-4 @min-[640px]:grid-cols-2 @min-[960px]:grid-cols-3">
        {items.map((x) => (
          <li key={x.output_id} className="flex gap-4 rounded-ds-lg bg-ds-surface p-3 shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)]">
            <div className={cx("relative shrink-0 overflow-hidden rounded-ds-sm bg-ds-media", x.kind === "long" ? "h-[72px] w-[128px]" : "h-[112px] w-[64px]")}><Poster o={x} className="size-full" /></div>
            <div className="flex min-w-0 flex-1 flex-col gap-1 py-1">
              <span className="flex flex-wrap items-center gap-2"><MetaItem icon={kindIcon(x.kind)}>{kindLabel(x.kind)}</MetaItem>{x.profile ? <Badge tone="danger" variant="solid" size="xs">Test output</Badge> : null}</span>
              <p className="line-clamp-2 text-ds-sm leading-ds-snug font-semibold">{x.title}</p>
              <p className="text-ds-2xs text-ds-muted">{channelName(x.channel)} · {fmtDay(x.created_at)}</p>
              <div className="mt-auto flex items-center gap-2 pt-1 text-ds-xs">
                {x.status === "uploaded" && x.url ? <LinkButton href={x.url} external variant="ghost" size="sm" leadingIcon="external" className="-ml-2">Posted on YouTube</LinkButton>
                  : x.status === "approved" && x.profile ? <span className="text-ds-text-2">Test output: it cannot be posted</span>
                  : x.status === "approved" ? <><span className="text-ds-text-2">Not posted yet</span><Button size="sm" variant="soft" className="ml-auto" onClick={() => onResume(x.output_id)}>Finish posting</Button></>
                  : <span className="text-ds-text-2">{x.reason ? `Reason: ${x.reason}` : "No reason given"}</span>}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

type Prog = Record<string, boolean>;
function Review({ o, items, tab, onTab, onPick, onChanged, channelName }: { o: Output; items: Output[]; tab: Tab; onTab: (t: Tab) => void; onPick: (id: string) => void; onChanged: (nextId: string | null) => Promise<void>; channelName: (s?: string | null) => string }) {
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
  // the approved video stays on screen for its checklist: without the pick, the reloaded list showed the next one to review
  const approve = async () => { try { await api.post(`/outputs/${api.enc(o.output_id)}/approve`); mark("approved"); onPick(o.output_id); toast("Approved", "ok"); } catch (e: any) { toast(e.message, "bad"); } };
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
    { key: "folder", title: "Open the video file", sub: o.path || "In the Outbox folder", action: <Button size="sm" variant="soft" leadingIcon="folder" onClick={openFolder}>Open</Button> },
    { key: "studio", title: "Open YouTube Studio", sub: "Start an upload and drag the file in", action: <LinkButton href="https://studio.youtube.com/" external size="sm" variant="soft" leadingIcon="external" onClick={() => mark("studio")}>Studio</LinkButton> },
    { key: "title", title: "Title", sub: o.title, action: <Button size="sm" variant="soft" leadingIcon="copy" onClick={() => copy(o.title, "Title", "title")}>Copy</Button> },
    { key: "description", title: "Description", sub: d?.chapters?.length ? `With ${d.chapters.length} chapters and ${d.hashtags?.length ?? 0} hashtags` : `With ${d?.hashtags?.length ?? 0} hashtags`, action: <Button size="sm" variant="soft" leadingIcon="copy" onClick={() => copy(description, "Description", "description")} disabled={!d}>Copy</Button> },
    { key: "thumb", title: "Thumbnail", sub: o.kind === "long" ? (d?.thumbnails?.length ? `Pick one of the ${d.thumbnails.length} made for this video` : "Open the one that was made") : "Pick a frame in Studio", action: o.kind === "long" && !demo ? <span className="flex gap-1">{(d?.thumbnails?.length ? d.thumbnails : ["thumb_1.jpg"]).map((t) => { const n = Number(/\d/.exec(t)?.[0] || 1); return <a key={t} href={api.thumbChoiceUrl(n, o.output_id)} target="_blank" rel="noreferrer" aria-label={`Open thumbnail ${n}`} onClick={() => mark("thumb")} className="ds-focus block overflow-hidden rounded-ds-sm shadow-ds-control"><img src={api.thumbChoiceUrl(n, o.output_id)} alt="" loading="lazy" className="h-9 w-16 object-cover" /></a>; })}</span> : <Button size="sm" variant="ghost" onClick={() => mark("thumb")}>Done</Button> },
    { key: "audience", title: "Audience", sub: d?.made_for_kids ? "Yes, it's made for kids" : "No, it's not made for kids", action: <Button size="sm" variant="ghost" onClick={() => mark("audience")}>Done</Button> },
    { key: "altered", title: "Altered content", sub: d?.ai_disclosure === "yes" ? "Answer Yes" : "Answer No. The voice is production help, footage is real stock", action: <Button size="sm" variant="ghost" onClick={() => mark("altered")}>Done</Button> },
    { key: "tags", title: "Tags", sub: d?.tags?.length ? `${d.tags.length} tags, ${d.tags.slice(0, 3).join(", ")}` : "No tags", action: <Button size="sm" variant="soft" leadingIcon="copy" onClick={() => copy((d?.tags || []).join(", "), "Tags", "tags")} disabled={!d}>Copy</Button> },
    { key: "schedule", title: "Schedule and related video", sub: [d?.publish_after ? `Publish ${fmtWhen(d.publish_after)}` : "Any time", o.kind !== "long" && d?.related_video_instruction].filter(Boolean).join(". "), action: <Button size="sm" variant="ghost" onClick={() => mark("schedule")}>Done</Button> },
  ];
  const doneCount = (approved ? 1 : 0) + steps.filter((s) => prog[s.key]).length;
  const total = steps.length + 2;
  const nextKey = !approved ? "approve" : steps.find((s) => !prog[s.key])?.key ?? "link";
  // The checklist scrolls inside its own card on wide screens; keep the next step in view as steps get ticked.
  const aside = useRef<HTMLElement>(null);
  useEffect(() => {
    const box = aside.current; if (!box || box.scrollHeight <= box.clientHeight + 1) return;
    const el = box.querySelector<HTMLElement>(".is-next"); if (!el) return;
    const top = el.offsetTop; const bottom = top + el.offsetHeight;
    if (top < box.scrollTop + 64 || bottom > box.scrollTop + box.clientHeight - 16) box.scrollTo({ top: Math.max(0, top - 80), behavior: "smooth" });
  }, [nextKey]);

  return (
    <>
      <Card as="section" aria-labelledby="q-review" className={cx("flex flex-col gap-5", SPAN["4/4"])}>
        <CardHeader id="q-review" title={o.status === "approved" ? "Finish posting" : "Ready to review"} className="!mb-0" adornment={<Badge tone="accent" className="ds-num">{idx + 1} of {items.length}</Badge>} actions={<QueueTabs tab={tab} onTab={onTab} />} />
        {/* the player: the one rich surface on the page */}
        <div className="relative overflow-hidden rounded-ds-lg bg-ds-media">
          <div className={cx("relative", portrait ? "aspect-[16/10]" : "aspect-video")}>
            <div className={cx("absolute inset-0", portrait && "flex justify-center py-5")}>
              {demo ? (
                <div className={cx("relative overflow-hidden", portrait ? "aspect-[9/16] h-full rounded-ds-md" : "size-full")}>
                  <Poster o={o} className="size-full" />
                  <span className="absolute inset-0 grid place-items-center"><span aria-hidden className="grid size-14 place-items-center rounded-full bg-[rgba(11,11,28,0.72)] text-white"><Icon name="play" size={22} filled className="translate-x-0.5" /></span></span>
                  <span className="sr-only">Video preview. With demo data there is no video file to play</span>
                </div>
              ) : (
                <video key={o.output_id} controls preload="metadata" src={api.mediaUrl(o.output_id)} poster={api.thumbUrl(o.output_id)} className={cx("bg-black", portrait ? "aspect-[9/16] h-full rounded-ds-md" : "size-full")} />
              )}
            </div>
            <div className="pointer-events-none absolute top-3 left-3 flex flex-wrap gap-2">
              <Badge tone="overlay" size="md">{kindLabel(o.kind)}{o.k ? ` ${o.k}` : ""}<span className="font-medium opacity-75 ds-num">{fmtDur(o.duration_s)}</span></Badge>
              {o.profile ? <Badge tone="danger" variant="solid" size="md">Test output</Badge> : null}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="text-ds-xl leading-ds-tight font-semibold tracking-[-0.01em]">{o.title}</h3>
            <p className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
              <MetaItem icon="channels">{channelName(o.channel)}</MetaItem>
              <MetaItem icon="clock">Made {fmtWhen(o.created_at)}</MetaItem>
              {d?.critic_avg ? <MetaItem icon="target">Critic score {d.critic_avg.toFixed(1)}</MetaItem> : null}
              {d?.words ? <MetaItem icon="file">{d.words.toLocaleString("en-US")} words</MetaItem> : null}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" leadingIcon="chat" onClick={() => ask({ page: "queue", output_id: o.output_id }, `Tell me about "${o.title}"`)}>Ask about it</Button>
            {items.length > 1 && <Button variant="soft" size="sm" onClick={() => nextId && onPick(nextId)} trailing={<Kbd>→</Kbd>}>Next video</Button>}
          </div>
        </div>

        <div className="overflow-hidden rounded-ds-lg bg-ds-sunken shadow-ds-inset-sm">
          <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="ds-focus flex w-full items-center justify-between gap-3 rounded-ds-lg px-4 py-3 text-left text-ds-sm font-semibold hover:bg-ds-hover">
            The story and why it passed checks
            <Icon name="chevronDown" size={18} className={cx("text-ds-muted transition-transform duration-200 ease-ds-out", open && "rotate-180")} />
          </button>
          {open && (
            <div className="fade-in grid gap-5 px-4 pt-1 pb-4 @container md:grid-cols-[1.4fr_1fr]">
              <p className="text-ds-md leading-ds-normal text-ds-text-2">{d?.story_excerpt || "No excerpt saved for this video"}</p>
              <ul className="flex flex-col gap-2">
                {(d?.policy_notes || []).map((n) => <li key={n} className="flex gap-2 text-ds-sm"><Icon name="check" size={16} className="mt-0.5 shrink-0 text-ds-success-ink" />{n}</li>)}
              </ul>
            </div>
          )}
        </div>

        {items.length > 1 && (
          <section aria-labelledby="q-also">
            <div className="mb-3 flex items-baseline justify-between"><h3 id="q-also" className="text-ds-md font-semibold">Also ready</h3><span className="text-ds-2xs text-ds-muted ds-num">{items.length - 1} more</span></div>
            <div className="-mx-1 flex gap-3 overflow-x-auto px-1 pt-1 pb-3 scroll-thin">
              {items.filter((x) => x.output_id !== o.output_id).map((x) => (
                <button key={x.output_id} type="button" onClick={() => onPick(x.output_id)} className="ds-focus flex w-[200px] shrink-0 flex-col gap-2 rounded-ds-lg bg-ds-surface p-2 text-left shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)] transition-[box-shadow,transform] duration-200 ease-ds-out hover:-translate-y-0.5 hover:shadow-ds-2">
                  <div className="relative aspect-video overflow-hidden rounded-ds-sm bg-ds-media">
                    <Poster o={x} portrait={false} className="size-full" />
                    <Badge tone="overlay" size="xs" className="absolute right-2 bottom-2 ds-num">{fmtDur(x.duration_s)}</Badge>
                  </div>
                  <div className="px-1 pb-1"><MetaItem icon={kindIcon(x.kind)}>{kindLabel(x.kind)}</MetaItem><p className="mt-1 line-clamp-2 text-ds-xs leading-ds-snug font-semibold">{x.title}</p></div>
                </button>
              ))}
            </div>
          </section>
        )}
      </Card>

      {/* posting checklist, in YouTube's upload order */}
      <Card ref={aside} as="aside" aria-labelledby="q-post" padding="md" className={cx("scroll-thin @min-[1020px]:sticky @min-[1020px]:top-4 @min-[1020px]:max-h-[calc(100dvh-40px)] @min-[1020px]:overflow-y-auto", SPAN["2/4"])}>
        <CardHeader id="q-post" title="Post it" className="!mb-3" actions={<span className="mr-2 text-ds-2xs text-ds-muted ds-num">{doneCount} of {total} done</span>} />
        <ol className="-mx-2 flex flex-col gap-1">
          <StepItem n={1} className={nextKey === "approve" ? "is-next" : undefined} done={approved} current={!approved} title={approved ? "Approved" : "Watch it, then decide"} sub={approved ? "Ready to post" : undefined}
            action={approved && o.status === "pending" ? <Button size="sm" variant="ghost" leadingIcon="undo" onClick={() => mark("approved", false)}>Undo</Button> : undefined}>
            {!approved && (
              <>
                <div className="flex gap-2"><Button className="flex-1" onClick={approve}>Approve</Button><Button variant="danger" onClick={reject}>Reject</Button></div>
                <p className="mt-3 flex flex-wrap items-center gap-1.5 text-ds-2xs text-ds-muted"><Kbd>A</Kbd> approve <Kbd>R</Kbd> reject <Kbd>→</Kbd> next</p>
              </>
            )}
          </StepItem>
          {steps.map((s, i) => <StepItem key={s.key} n={i + 2} className={nextKey === s.key ? "is-next" : undefined} done={!!prog[s.key]} dimmed={!approved} title={s.title} sub={prog[s.key] ? undefined : s.sub} action={s.action} />)}
          <StepItem n={steps.length + 2} className={nextKey === "link" ? "is-next" : undefined} dimmed={!approved} title={<label htmlFor="yt-url">Paste the YouTube link</label>}>
            <div className="flex gap-2">
              <Input id="yt-url" className="!h-9" placeholder="https://youtu.be/" value={url} onChange={(e) => setUrl(e.target.value)} disabled={!approved} onKeyDown={(e) => e.key === "Enter" && done()} />
              <Button onClick={done} disabled={!approved}>Done</Button>
            </div>
            <p className="mt-2 text-ds-2xs text-ds-muted">The link lets Nightshift learn from this video's results</p>
          </StepItem>
        </ol>
        <p className="mt-3 px-1 text-ds-2xs text-ds-muted">Ticked steps are remembered for each video</p>
      </Card>
    </>
  );
}

/* ---------------------------------------------------------------- job events */

function JobEvents({ jobs, loading, channelName, className, onReview }: { jobs: Job[]; loading: boolean; channelName: (s?: string | null) => string; className?: string; onReview: () => void }) {
  const { ask, go, toast } = useApp();
  // Dismissed events are hidden in this browser only (there is no server-side dismiss).
  const [hidden, setHidden] = useState<string[]>(() => store.get<string[]>("ns-dismissed-events", []));
  const dismiss = (id: string) => setHidden((h) => { const n = [...h, id].slice(-50); store.set("ns-dismissed-events", n); return n; });
  const events = jobs.filter((j) => !["queued", "running"].includes(j.status) && !hidden.includes(j.job_id)).slice(0, 3);
  return (
    <Card as="section" aria-labelledby="q-events" className={cx("self-stretch overflow-visible", className)}>
      <CardHeader id="q-events" title="Job events" actions={<Button variant="ghost" size="sm" leadingIcon="scenes" onClick={() => go("#/create")}>All jobs</Button>} />
      {events.length ? (
        <ul className="flex flex-col gap-3">
          {events.map((j) => {
            const failed = j.status === "failed"; const cancelled = j.status === "cancelled";
            const title = jobTitle(j, channelName);
            const copy = async () => { try { await navigator.clipboard.writeText(`${title}: ${j.status}${j.message ? `, ${j.message}` : ""} (job ${j.job_id})`); toast("Details copied", "ok"); } catch { toast("Copy was blocked", "warn"); } };
            const menu: MenuItem[] = [
              ...(failed ? [{ icon: "chat" as const, label: "Ask why it stopped", onSelect: () => ask({ page: "queue", job_id: j.job_id }, "Why did the last run stop?") }, { icon: "settings" as const, label: "Open Settings", onSelect: () => go("#/settings") }]
                : cancelled ? [] : [{ icon: "play" as const, label: "Review it", onSelect: onReview }, { icon: "chat" as const, label: "Ask about it", onSelect: () => ask({ page: "queue", job_id: j.job_id }) }]),
              { icon: "copy", label: "Copy details", onSelect: copy },
              { icon: "x", label: "Dismiss", onSelect: () => dismiss(j.job_id) },
            ];
            return (
              <li key={j.job_id}>
                <NotificationItem title={title} moreLabel={`Options for ${title}`} menu={menu}
                    titleAdornment={<StatusDot tone={failed ? "danger" : cancelled ? "attention" : "online"} label={failed ? "Stopped" : cancelled ? "Cancelled" : "Done"} />}
                    subtitle={failed ? `${stoppedAt(j.stage)}. ${j.message || ""}` : cancelled ? "Cancelled" : doneWords(j)}
                    meta={<><MetaItem icon="calendarDays">{fmtDay(j.created_at)}</MetaItem><MetaItem icon="clock">{fmtClock(j.created_at)}</MetaItem><MetaItem icon="channels">{channelName(j.channel)}</MetaItem></>} />
                {failed && <JobError j={j} />}
              </li>
            );
          })}
        </ul>
      ) : <EmptyState icon="bell" title={loading ? "Loading jobs" : "No job events yet"}>Finished and stopped jobs show up here with their reasons</EmptyState>}
    </Card>
  );
}

/* ---------------------------------------------------------------- tonight's plan */

/** "dashboard:preview-12" -> "a voice sample", "studio:..." -> "a video job", "code" -> "Nightshift Code". */
const ownerWords = (o: string) => (o.startsWith("studio:") ? "a video job" : o.startsWith("dashboard:preview") || o.startsWith("dashboard:voice") ? "a voice sample" : o.startsWith("dashboard") ? "the dashboard" : /code/i.test(o) ? "Nightshift Code" : o);
function TonightPlan({ chans, status, className }: { chans: ChannelSummary[]; status: ReturnType<typeof useApp>["status"]; className?: string }) {
  const { go, confirm, toast, refresh } = useApp();
  const pause = async (c: ChannelSummary) => {
    const ok = await confirm({ title: `Pause ${c.display_name}?`, body: "It is skipped from tonight until you resume it in Channels. Videos already made stay in the Queue", ok: "Pause" });
    if (ok !== true) return;
    try { await api.post(`/channels/${encodeURIComponent(c.slug)}/pause`); toast(`${c.display_name} is paused`, "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); }
  };
  // A video job that was stopped hard can leave the card reserved; the engine frees it by itself after 10 minutes, this is the way to do it now.
  const releaseGpu = async (owner: string) => {
    const ok = await confirm({ title: "Release the graphics card?", body: "This frees the card for the next job and stops the Nightshift video job that holds it. If another program holds it (for example opencode), only Nightshift's claim is released: that program may still be using the card", ok: "Release" });
    if (ok !== true) return;
    try { const r = await api.post<{ note?: string }>("/engine/release_gpu", { owner }); toast(r?.note || "The graphics card is free", "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const plan = chans.filter((c) => c.status === "active" && c.tonight !== false).slice(0, 2);
  const at = fmtClock(status?.next_run) || ""; // no time beside a plan that will not run by itself
  const voice = (c: ChannelSummary) => (typeof c.voice === "string" ? c.voice : c.voice?.name || c.voice?.speaker || "Built-in voice");
  return (
    <Card as="section" aria-labelledby="q-plan" className={cx("self-stretch", className)}>
      <CardHeader id="q-plan" title="Tonight's plan" actions={<Button variant="ghost" size="sm" leadingIcon="edit" onClick={() => go("#/channels")}>Edit</Button>} />
      {status?.engine?.gpu_wait ? <p role="status" className="mb-3 flex flex-wrap items-center gap-2 rounded-ds-md bg-ds-accent-subtle px-3 py-2 text-ds-xs"><StatusDot tone="attention" />{status.engine.gpu_wait.said}</p> : status?.engine?.vram_owner && <p role="status" className="mb-3 flex flex-wrap items-center gap-2 rounded-ds-md bg-ds-accent-subtle px-3 py-2 text-ds-xs"><StatusDot tone="accent" />GPU held by <strong className="font-semibold" title={status.engine.vram_owner}>{ownerWords(status.engine.vram_owner)}</strong>. {nightComes(status) ? "Tonight's run waits for it." : "Jobs that need it wait."}{!status.job && <Button size="sm" variant="soft" onClick={() => releaseGpu(status.engine.vram_owner!)}>Release</Button>}</p>}
      <div className="flex flex-col gap-4">
        {plan.length ? plan.map((c) => (
          <AssignmentCard key={c.slug} categories={[c.display_name]} moreLabel={`Options for ${c.display_name}`} menu={[{ icon: "edit", label: "Edit channel", onSelect: () => go(`#/channels?edit=${encodeURIComponent(c.slug)}`) }, { icon: "pause", label: "Skip tonight and pause", onSelect: () => pause(c) }]}
            title={typeof c.tonight === "string" ? c.tonight : c.format === "shorts_only" ? "Shorts only" : "Full video and Shorts"}
            priority={{ label: at || "Off", tone: "accent" }} tag={status?.next_run ? `Runs ${dayInline(status.next_run)}` : "Not scheduled"} assignee={{ name: voice(c) }} />
        )) : <EmptyState icon="moon" title="No channel runs tonight">Resume a channel or start something now</EmptyState>}
        <CreateTile layout="row" label="Make something now" onClick={() => go("#/create")} />
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- posting calendar */

const isoDay = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function PostingCalendar({ chans, ready, className, onOpen }: { chans: ChannelSummary[]; ready: Output[]; className?: string; onOpen: (id: string) => void }) {
  const { go, ask } = useApp();
  const [offset, setOffset] = useState(0);
  const today = new Date(); today.setHours(12, 0, 0, 0);
  const days: WeekDay[] = Array.from({ length: 7 }, (_, i) => {
    const d = new Date(today); d.setDate(today.getDate() + offset * 7 + i);
    return { iso: isoDay(d), date: d.getDate(), weekday: d.toLocaleDateString("en-GB", { weekday: "short" }), label: d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }) };
  });
  const [sel, setSel] = useState(isoDay(today));
  const selected = days.find((d) => d.iso === sel) ? sel : days[0]!.iso;
  const isToday = selected === isoDay(today);
  const month = new Date(selected + "T12:00:00").toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  const slots: AgendaSlot[] = chans.filter((c) => c.status === "active" && c.publish_window).slice(0, 2).map((c) => {
    const mine = isToday ? ready.filter((o) => o.channel === c.slug).slice(0, 2) : [];
    return {
      time: (c.publish_window || "").replace("-", " – "),
      events: mine.length
        ? mine.map((o) => ({ id: o.output_id, icon: kindIcon(o.kind), title: o.title, time: `${kindLabel(o.kind)} · ${fmtDur(o.duration_s)}${o.profile ? " · Test output, cannot be posted" : ""}`, tag: c.display_name, menu: [{ icon: "play" as const, label: "Open in the review card", onSelect: () => onOpen(o.output_id) }, { icon: "chat" as const, label: "Ask about it", onSelect: () => ask({ page: "queue", output_id: o.output_id }, `Tell me about "${o.title}"`) }] }))
        : [{ id: "window-" + c.slug, icon: "calendar" as IconName, title: "Posting window", time: `${c.masters_per_week ?? 1} full video${(c.masters_per_week ?? 1) === 1 ? "" : "s"} a week`, tag: c.display_name, tagTone: "success" as const, menu: [{ icon: "edit" as const, label: "Change the posting window", onSelect: () => go(`#/channels?edit=${encodeURIComponent(c.slug)}`) }] }],
    };
  });
  return (
    <CalendarCard className={cx("self-stretch", className)} monthLabel={`Posting · ${month}`} days={days} selected={selected} onSelect={setSel} slots={slots}
      onPrev={() => setOffset((x) => x - 1)} onNext={() => setOffset((x) => x + 1)}
      prevLabel="Previous week" nextLabel="Next week" weekLabel="Posting days" moreLabel="Options" />
  );
}

/* ---------------------------------------------------------------- render jobs */

function RenderJobs({ jobs, chans, channelName, className }: { jobs: Job[]; chans: ChannelSummary[]; channelName: (s?: string | null) => string; className?: string }) {
  const { go } = useApp();
  const live = jobs.filter((j) => ["queued", "running"].includes(j.status));
  // A scheduled job is queued with a future schedule_at: it shows its start time, not "Waiting", and sorts after the running ones.
  const held = (j: Job) => j.status === "queued" && !!j.schedule_at && new Date(j.schedule_at).getTime() > Date.now();
  const ordered = [...live.filter((j) => !held(j)), ...live.filter(held).sort((a, b) => +new Date(a.schedule_at!) - +new Date(b.schedule_at!))];
  const rows = [...ordered, ...jobs.filter((j) => !live.includes(j))].slice(0, 4);
  const tasks: Task[] = rows.map((j) => {
    const running = j.status === "running" || j.status === "queued"; const failed = j.status === "failed"; const sched = held(j);
    // held by its channel's pause (P47): the server's own words, with the videos the request still owes
    const paused = j.status === "queued" && (j.message || "").startsWith("Paused: ");
    const title = jobTitle(j, channelName);
    return {
      id: j.job_id, title, date: paused ? j.message.slice("Paused: ".length) : sched ? `Scheduled for ${fmtWhen(j.schedule_at)}` : running ? `Started ${fmtWhen(j.created_at)}` : fmtWhen(j.created_at),
      metricLabel: sched && !paused ? "Starts" : "Stage", metric: paused ? "Paused" : sched ? fmtWhen(j.schedule_at) : j.status === "queued" ? "Waiting" : running ? stageWords(j.stage) : failed ? "Stopped" : j.status === "cancelled" ? "Cancelled" : "Done",
      progress: j.progress ?? (j.status === "done" ? 100 : 0), progressLabel: `${title} progress`, tone: failed ? "danger" : j.status === "done" ? "success" : "accent",
      meta: [{ icon: "clock", value: j.step && j.steps ? `${j.step}/${j.steps}` : "–", label: "steps" }, { icon: "film", value: MAKES_VIDEO.includes(j.kind) ? (j.result?.output_ids?.length ?? 0) : "–", label: "videos" }],
    };
  });
  return (
    <Card as="section" aria-labelledby="q-renders" className={className}>
      <CardHeader id="q-renders" title="Render jobs" adornment={chans.length ? <AvatarGroup people={chans.map((c) => ({ name: c.display_name }))} max={3} onAdd={() => go("#/channels?edit=new")} addLabel="Add a channel" /> : undefined}
        actions={<Button variant="ghost" size="sm" leadingIcon="scenes" onClick={() => go("#/create")}>Open Create</Button>} />
      {tasks.length ? <TaskList tasks={tasks} selectedId={ordered[0]?.job_id} onSelect={() => go("#/create")} />
        : <EmptyState icon="scenes" title="No jobs yet">Story videos and clips you start show their progress here</EmptyState>}
    </Card>
  );
}

/* ---------------------------------------------------------------- machine meters */

function MachineMeters({ className }: { className?: string }) {
  const { status } = useApp();
  const disk = status?.disk;
  const gpu = status?.gpu;
  const load = gpu?.load_pct ?? null;
  const used = gpu?.vram_used_mb ?? null; const total = gpu?.vram_total_mb ?? null;
  const vramPct = used != null && total ? Math.round((used / total) * 100) : null;
  const gb = (mb: number) => (mb / 1024).toFixed(1);
  // Without a total (no GPU block, or nvidia-smi gave none) the VRAM well stays empty and shows only the size the services report.
  const vramMb = Object.values(status?.services || {}).reduce((a, s) => a + (s.vram_mb || 0), 0);
  const vramGb = status?.engine?.vram_gb ?? (vramMb ? Math.round(vramMb / 1024) : null);
  const temp = gpu?.temp_c;
  const meters: MeterDatum[] = [
    { label: "GPU", value: load == null ? null : Math.round(load), source: load == null ? "not reported by the engine yet" : `${gpu!.simulated ? "simulated: " : ""}${gpu!.name}, ${Math.round(load)}% busy` },
    vramPct != null
      ? { label: "VRAM", value: vramPct, display: `${gb(used!)} GB`, source: `${gb(used!)} of ${gb(total!)} GB in use` }
      : { label: "VRAM", value: null, display: used != null ? `${gb(used)} GB` : vramGb ? `${vramGb} GB` : undefined, source: used != null ? `${gb(used)} GB in use, total not reported` : vramGb ? `${vramGb} GB in use` : "no model loaded" },
    { label: "Disk", value: disk && disk.total_gb != null && disk.free_gb != null ? Math.round(((disk.total_gb - disk.free_gb) / Math.max(1, disk.total_gb)) * 100) : null, source: disk ? `${disk.free_gb} GB free of ${disk.total_gb} GB` : "unknown until the engine answers" },
  ];
  const diskLine = disk ? `${disk.free_gb} GB free on ${disk.home}` : null;
  const gpuLine = gpu ? `${gpu.simulated ? "Simulated card (fake_gpu): " : ""}${gpu.name}${temp != null ? ` · ${Math.round(temp)} °C` : ""}` : null;
  return <MeterGroup className={className} title="This PC" meters={meters}
    caption={gpuLine ? [gpuLine, diskLine].filter(Boolean).join(". ") : diskLine ? `${diskLine}. GPU load appears once the engine reports it` : "Readings appear once the engine answers"} />;
}

/** One line about a job the night left half made. The state is decided where the summary is served (`interrupted_jobs[].state`), so the words are true for every case: still open, rescued, stopped again, waiting. A waiting job is continued by the next night only while one is coming (`nightComes`); otherwise the line says why none is and the way that works now: the Studio shortcut's `run --resume <id>` for every job, and its card's Retry for a job that has one. */
function interruptedWords(u: InterruptedJob, status: ReturnType<typeof useApp>["status"]): string {
  const at = u.stage ? ` at ${stageWords(u.stage).toLowerCase()}` : "";
  switch (u.state ?? (u.resumed ? "resumed" : "open")) {
    case "resumed": return `interrupted${at}, then continued and finished`;
    case "failed_again": return `interrupted${at}, continued but stopped again`;
    case "waiting": {
      if (u.said) return `interrupted${at}, waiting for the graphics card's memory (${u.said})${nightComes(status) ? ": the next night continues it" : ""}`;
      if (nightComes(status)) return `interrupted${at}, waiting for the GPU: the next night continues it`;
      // No night is coming, or the status is not known yet (then no cause is named). The Studio shortcut puts `studio` in front of what is typed.
      const way = `once the GPU is free, open the Nightshift Studio shortcut and type run --resume ${u.job_id}${u.has_card === false ? "" : ", or press Retry from where it stopped on its card in Create, under Earlier, while it is listed there"}`;
      if (!status) return `interrupted${at}, waiting for the GPU: ${way}`;
      return `interrupted${at}, waiting for the GPU: ${status.schedule_registered ? "no channel is active, so no nightly run is coming" : "Automatic runs are off"}, so ${way}`;
    }
    case "open": return `interrupted${at}, not finished`;
    default: return `interrupted${at}, not finished`;
  }
}

/** A duration as the morning note says it: "5 h 40 min", "52 min", "40 s" (ns_studio/nightclock.py words). */
const hm = (s?: number | null) => { const n = Math.max(0, Math.round(Number(s) || 0)); if (n < 60) return `${n} s`; const m = Math.floor(n / 60); const h = Math.floor(m / 60); return h && m % 60 ? `${h} h ${m % 60} min` : h ? `${h} h` : `${m} min`; };
const FULL_PLANS = ["master_first", "master_from_script", "master_from_owner_script"];
/** One thing the night left for time (NS-W211), in the morning note's words (ns_studio/notifications.py). */
function leftWords(x: LeftForTime, channelName: (s?: string | null) => string, status: ReturnType<typeof useApp>["status"]): string {
  if (x.said) return x.said.startsWith("simulated (") ? x.said : x.said.charAt(0).toUpperCase() + x.said.slice(1); // the decision's own sentence: the note and this card never disagree (a simulated card's mark stays as written)
  const ch = channelName(x.channel); const was = x.left_s == null ? "" : `, ${hm(x.left_s)} ${x.left_s < 120 ? "was" : "were"} left`;
  if (x.reason === "stage_too_long") return `${ch}: ${x.job_id ? "a half-made video waits" : "not started"}: its ${x.stage || "longest"} stage takes longer than a whole night on this PC (make it from Create, where a run has no time limit)`;
  const way = (first: string) => nightComes(status) ? `the next nightly run continues it${first}` : "press Retry on its card to continue it";
  if (x.job_id && !x.resumed) return `${ch}: stopped at ${x.stage || "a stage"}; ${way("")}`;
  if (x.job_id) return `${ch}: its half-made video was not continued (${x.need_s == null ? "it needs more time" : `it needs about ${hm(x.need_s)} more`}${was}); ${way(" first")}`;
  const what = plural(x.jobs, FULL_PLANS.includes(x.fmt || "") ? "full video" : "Short");
  if (x.need_s == null) return `${ch}: ${what} not started`;
  const took = x.basis === "default" ? `Nightshift plans ${hm(x.need_s)} for one until it has timed one on this PC` : `${x.jobs === 1 ? "it takes" : "each takes"} about ${hm(x.need_s)} here`;
  return `${ch}: ${what} not started (${took}${was})`;
}

/** "Last night": what the newest nightly run made and what stopped, from GET /runs. Shown only after a run has happened. */
function LastNight({ className, pending, onReview, channelName }: { className?: string; pending: number; onReview: () => void; channelName: (s?: string | null) => string }) {
  const { status } = useApp();
  const runs = useLoad<Run[]>(() => api.get("/runs?limit=1"), [], []);
  const notes = useLoad<MorningNote[]>(() => api.get("/notes"), [], []);
  const r = runs.data[0];
  if (!r) return null;
  const made = r.summary?.jobs?.length ?? 0; const failed = r.summary?.failures ?? []; const skipped = r.summary?.skipped ?? [];
  // jobs the night did not start because the drive had no room for them (nothing was made half way): the sentence is the server's own, "needs about N MB; D: has M MB free"
  const noRoom = r.summary?.no_room ?? []; const notStarted = noRoom.reduce((n, x) => n + x.jobs, 0);
  // jobs the night did not start because their channel was paused (before or during the night, P47); a half-made one waits for the resume
  const held = r.summary?.paused ?? []; const heldJobs = held.reduce((n, x) => n + x.jobs, 0);
  // work the night left for the next night to end inside Windows' limit for the task, and how long it took of it (card night-fits; an older report has neither)
  const left = r.summary?.out_of_time ?? []; const leftJobs = left.filter((x) => x.reason !== "cannot_fit_new" && x.reason !== "stage_too_long").reduce((n, x) => n + x.jobs, 0); const t = r.summary?.timing;
  const ofLimit = t && t.limit_s && t.used_s != null ? ` ${t.simulated_card ? `${t.simulated_card}: took` : "Took"} ${hm(t.used_s)} of ${hm(t.limit_s)}${t.gpu_s && t.gpu_s >= 60 ? `, ${hm(t.gpu_s)} of it getting the graphics card` : ""}.` : "";
  // A run that was killed (power cut, crash) has a finish time written by the recovery, but it did not finish: say so, and name the jobs it left half made.
  const interrupted = r.state === "interrupted"; const live = r.state === "running"; const unfinished = r.summary?.interrupted_jobs ?? [];
  const took = r.duration_s ? (r.duration_s >= 3600 ? `${Math.floor(r.duration_s / 3600)} h ${Math.round((r.duration_s % 3600) / 60)} min` : `${Math.max(1, Math.round(r.duration_s / 60))} min`) : null;
  const when = r.mode === "daily" ? "Last night" : "Last run";
  return (
    <Card as="section" aria-labelledby="q-last" className={className}>
      <CardHeader id="q-last" title={when} adornment={failed.length ? <StatusDot tone="danger" label="Something stopped" /> : noRoom.length && !made ? <StatusDot tone="danger" label="Not started" /> : interrupted ? <StatusDot tone="attention" label="Interrupted" /> : live ? <StatusDot tone="accent" label="Running" /> : <StatusDot tone="online" label="Finished" />}
        actions={pending > 0 ? <Button size="sm" variant="soft" onClick={onReview}>Review {pending}</Button> : undefined} />
      <p className="text-ds-md leading-ds-normal">
        {made ? `${plural(made, "video")} made` : "Nothing was made"}{took ? `, in ${took}` : ""}, {fmtDay(r.started_at).toLowerCase()} at <span className="ds-num">{fmtClock(r.started_at)}</span>.
        {failed.length ? ` ${plural(failed.length, "job")} stopped.` : ""}
        {skipped.length ? ` ${plural(skipped.reduce((n, x) => n + x.jobs, 0), "job")} skipped.` : ""}
        {notStarted ? ` ${plural(notStarted, "job")} not started, not enough disk space.` : ""}
        {heldJobs ? ` ${plural(heldJobs, "job")} held, the channel${new Set(held.map((x) => x.channel)).size > 1 ? "s were" : " was"} paused.` : ""}
        {ofLimit}
        {leftJobs ? ` Left for time: ${plural(leftJobs, "video")}, to end inside Windows' limit for the nightly task.` : ""}
        {interrupted ? ` ${r.note || "Interrupted: it never finished."}` : ""}
      </p>
      {unfinished.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-text-2">{unfinished.map((u) => <li key={u.job_id}>{channelName(u.channel)}: {interruptedWords(u, status)}</li>)}</ul>}
      {notes.data.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-text-2">{notes.data.map((n, i) => <li key={i}>{n.message} <span className="text-ds-muted">({fmtClock(n.at)})</span></li>)}</ul>}
      {noRoom.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-danger">{noRoom.map((x, i) => <li key={i}>{channelName(x.channel)}: {plural(x.jobs, "job")} not started, not enough disk space ({x.short || x.message || "free some space"})</li>)}</ul>}
      {held.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-text-2">{held.map((x, i) => <li key={i}>{x.job_id ? `${channelName(x.channel)} is paused: its half-made job waits for the resume` : `${plural(x.jobs, "job")} not started: ${channelName(x.channel)} is paused`}</li>)}</ul>}
      {left.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-text-2">{left.map((x, i) => <li key={i}>{leftWords(x, channelName, status)}</li>)}</ul>}
      {skipped.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-text-2">{skipped.map((x) => <li key={x.channel}>{channelName(x.channel)}: {plural(x.jobs, "job")} skipped because {x.reason === "vram" ? "other programs held the graphics card's memory (NS-E208)" : "an earlier one stopped"}</li>)}</ul>}
      {failed.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ds-xs text-ds-danger">{failed.slice(0, 3).map((f) => <li key={f.job_id}>{channelName(f.channel)}: {f.code ? `${f.code}. ` : ""}{f.error || "stopped"}</li>)}</ul>}
    </Card>
  );
}
