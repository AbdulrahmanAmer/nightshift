/* #/design/ref12: the whole reference screen, rebuilt from the ref12 components with Nightshift copy.
   The composition is Planify's demo/Dashboard.tsx: a 6-column grid with named areas
     greeting greeting create f1     f2   f3
     notif    notif    assign assign cal  cal
     tasks    tasks    tasks  promo  side side
   reflowing to 4 columns and to 1 (container queries, see .ds-dash in styles.css).
   At 1024 px and wider the window is laid out on the 1440x1080 design frame and scaled to fit, so at
   1366x768 it renders at the reference's own 1024x768 size. Narrower, it renders fluid.
   The left column is a reserved slot: the side rail is built on PR #25 and drops in via `sideTab`. */
import { useEffect, useState, type ReactNode } from "react";
import { useApp } from "../lib/app";
import { cx } from "../ui/kit";
import {
  AppWindow, AssignmentCard, AvatarGroup, Badge, Button, CalendarCard, Card, CardHeader, ClipsIllustration, CreateTile, FeatureCard,
  Greeting, IconButton, MeetingCard, MetaItem, NavTabs, NotificationItem, PageBackdrop, PromoCard, SearchInput, SegmentedControl,
  SeriesIllustration, StatCard, StatusDot, StoryIllustration, SwipeActions, TaskList, TextField, VoiceIllustration, WorkspaceTokens,
} from "../components/ref12";
import { EVENT, FEATURES, HERO, MESSAGE, PEOPLE, PLAN, PROMO, RENDERS, SCHEDULE, SLOT, STATS, TABS, THEME_OPTIONS, VIEW_OPTIONS, WEEK } from "./design/sample";

const FRAME = { w: 1440, h: 1080 } as const;
const ILLOS = [<StoryIllustration key="s" />, <ClipsIllustration key="c" />, <SeriesIllustration key="r" />];
const AREA = ["[grid-area:f1]", "[grid-area:f2]", "[grid-area:f3]"];

function useViewport() {
  const read = () => ({ w: window.innerWidth, h: window.innerHeight });
  const [v, setV] = useState(read);
  useEffect(() => { const on = () => setV(read()); addEventListener("resize", on); return () => removeEventListener("resize", on); }, []);
  return v;
}

/** The dashboard body (Planify's Dashboard.tsx composition) with Nightshift content. */
export function Ref12Dashboard() {
  const [view, setView] = useState<"list" | "grid" | "board">("grid");
  const [day, setDay] = useState(WEEK[0]!.iso);
  const [taskId, setTaskId] = useState("r1");
  return (
    <div className="ds-dash-wrap">
      <div className="ds-dash">
        <Greeting size="lg" className="self-center [grid-area:greeting]" salutation={HERO.salutation} question={HERO.question} description={HERO.description} adornment={<WorkspaceTokens tokens={HERO.tokens} />} />
        <CreateTile className="self-stretch [grid-area:create]" label="Start something new" />
        {FEATURES.map((f, i) => <FeatureCard key={f.title} className={cx("self-stretch", AREA[i])} illustration={ILLOS[i]} title={f.title} description={f.description} href={f.href} />)}

        <Card as="section" aria-label="Job events" className="self-stretch overflow-visible [grid-area:notif]">
          <CardHeader title="Job events" actions={<Button variant="ghost" size="sm" leadingIcon="trash">Clear</Button>} />
          <div className="flex flex-col gap-4">
            <SwipeActions open actions={[{ icon: "trash", label: "Delete notification" }, { icon: "edit", label: "Open in Queue" }]}>
              <NotificationItem state="raised" title={EVENT.title} titleAdornment={<StatusDot tone="online" label="Done" />} subtitle={EVENT.subtitle} moreLabel="More options"
                meta={<><MetaItem icon="calendarDays">{EVENT.date}</MetaItem><MetaItem icon="clock">{EVENT.time}</MetaItem></>} />
            </SwipeActions>
            <NotificationItem className="ml-4" moreLabel="More options"
              title={<>{MESSAGE.title} <span className="font-normal text-ds-muted">|</span> {MESSAGE.context}</>}
              meta={<MetaItem icon="mail">{MESSAGE.from}</MetaItem>}>
              <TextField label="Reply" placeholder={MESSAGE.reply} />
            </NotificationItem>
          </div>
        </Card>

        <Card as="section" aria-label="Tonight's plan" className="self-stretch [grid-area:assign]">
          <CardHeader title="Tonight's plan" actions={<Button variant="ghost" size="sm" leadingIcon="edit">Edit</Button>} />
          <div className="flex flex-col gap-4">
            <AssignmentCard {...PLAN} moreLabel="Plan options" />
            <CreateTile layout="row" label="Add to tonight's plan" />
          </div>
        </Card>

        <CalendarCard className="self-stretch [grid-area:cal]" monthLabel="November 2026" days={WEEK} selected={day} onSelect={setDay} slots={SCHEDULE}
          prevLabel="Previous week" nextLabel="Next week" weekLabel="Week" moreLabel="Options for this upload" />

        <Card as="section" aria-label="Render jobs" className="self-stretch [grid-area:tasks]">
          <CardHeader title="Render jobs" adornment={<AvatarGroup people={PEOPLE} max={3} onAdd={() => undefined} addLabel="Add a channel" />}
            actions={<>
              <SegmentedControl label="View" variant="plain" size="sm" value={view} onChange={setView} options={VIEW_OPTIONS} />
              <Button variant="ghost" size="sm" leadingIcon="edit">Edit</Button>
              <Button variant="ghost" size="sm" leadingIcon="share">Share</Button>
            </>} />
          <TaskList tasks={RENDERS} selectedId={taskId} onSelect={setTaskId} />
        </Card>

        <PromoCard className="self-stretch [grid-area:promo]" illustration={<VoiceIllustration />} title={PROMO.title} description={PROMO.description} ctaLabel={PROMO.cta} />

        <div className="grid grid-cols-1 gap-4 [grid-area:side] @min-[600px]:grid-cols-2">
          {STATS.map((s) => <StatCard key={s.title} {...s} />)}
          <MeetingCard className="@min-[600px]:col-span-2" title={SLOT.title} when={SLOT.when} description={SLOT.description} editLabel="Edit" secondaryLabel={SLOT.secondary} primaryLabel={SLOT.primary} />
        </div>
      </div>
    </div>
  );
}

/** The top bar row (navigation, search, actions). Not Planify's TopBar component: that and the shell stay out of scope. */
function Ref12TopRow() {
  const { theme, setTheme } = useApp();
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("tonight");
  return (
    <div className="flex min-h-14 flex-wrap items-center gap-x-8 gap-y-3">
      <NavTabs label="Sections" tabs={TABS} active={tab} onChange={setTab} />
      <SearchInput label="Search" placeholder="Search or run a command" className="min-w-[140px] flex-[0_1_220px] max-md:order-3 max-md:basis-full" />
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <SegmentedControl label="Theme" value={theme} onChange={setTheme} options={THEME_OPTIONS} />
        <IconButton icon="bell" label="Notifications" dot />
        <IconButton icon="settings" label="Settings" />
        <Button variant="soft" leadingIcon="download" className="!font-medium !text-ds-text-2" trailing={<Badge tone="accent" variant="solid" size="xs">csv</Badge>}>Export results</Button>
        <Button variant="inverse">New story</Button>
      </div>
    </div>
  );
}

export function Ref12Screen({ sideTab, fit }: { sideTab?: ReactNode; fit: boolean }) {
  const slot = sideTab ?? (
    <div role="note" aria-label="Side rail slot" className="m-5 flex h-[calc(100%-40px)] items-center justify-center rounded-ds-xl border-[1.5px] border-dashed border-ds-dashed text-ds-2xs text-ds-muted [writing-mode:vertical-rl]">
      Side rail slot, built on PR #25
    </div>
  );
  if (!fit) {
    return (
      <AppWindow className="m-4 px-4 pt-2 pb-6">
        <Ref12TopRow />
        <div className="mt-4"><Ref12Dashboard /></div>
      </AppWindow>
    );
  }
  return (
    <AppWindow style={{ position: "absolute", left: 62, top: 34, width: 1316, height: 1014 }}>
      <div className="absolute inset-y-0 left-0 w-[140px]">{slot}</div>
      <div className="absolute top-5 right-12 left-[140px]">
        <Ref12TopRow />
        <div className="mt-3"><Ref12Dashboard /></div>
      </div>
    </AppWindow>
  );
}

export default function DesignRef12() {
  const v = useViewport();
  const fit = v.w >= 1024;
  const s = Math.min(v.w / FRAME.w, v.h / FRAME.h);
  useEffect(() => { document.title = "ref12 recreation · Nightshift"; }, []);
  if (!fit) return <div className="ds-root min-h-dvh bg-ds-canvas py-2"><Ref12Screen fit={false} /></div>;
  return (
    <div className="ds-root flex h-dvh w-full items-center justify-center overflow-hidden bg-ds-canvas">
      <div style={{ width: FRAME.w * s, height: FRAME.h * s }} className="relative shrink-0">
        <div className="absolute top-0 left-0 origin-top-left" style={{ width: FRAME.w, height: FRAME.h, transform: `scale(${s})` }}>
          <PageBackdrop />
          <Ref12Screen fit />
        </div>
      </div>
    </div>
  );
}
