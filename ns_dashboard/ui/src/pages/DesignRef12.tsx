/* #/design/ref12: the whole reference screen rebuilt from the ref12 components with Nightshift copy.
   The composition is laid out on the 1440x1080 design frame (reference x1.406) and scaled to fit the
   viewport, so at 1366x768 it renders at the reference's own 1024x768 size, centred on the page colour.
   The left tab is a reserved slot: SideTab is built on feat/ui-sidetab and drops in here. */
import { useEffect, useState, type ReactNode } from "react";
import { Bell, Film, Moon, Settings, Sun } from "lucide-react";
import { useApp } from "../lib/app";
import {
  ActionButton, AppWindow, AssignmentsCard, BoardMeetingCard, CalendarCard, ClipsIllustration, CommandSearch, DashedAddCard,
  ExportButton, FeatureCard, FloatingEventCard, HeroBadge, HeroGreeting, ICON, IconButton, MessageRow, NotificationsCard,
  PageBackdrop, PremiumCard, SegmentedToggle, SeriesIllustration, StatRingCard, StoryIllustration, TodayTasksCard, TopBar, TopNav, VoiceIllustration,
} from "../components/ref12";
import { EVENT, EVENT_ACTIONS, FEATURES, HERO, MESSAGE, PEOPLE, PLAN, PREMIUM, RENDERS, SCHEDULE, SLOT, STATS, TABS, THEME_ITEMS, VIEW_ITEMS, WEEK } from "./design/sample";

const FRAME = { w: 1440, h: 1080 } as const;

function useFitScale() {
  const calc = () => Math.min(window.innerWidth / FRAME.w, window.innerHeight / FRAME.h);
  const [s, setS] = useState(calc);
  useEffect(() => { const on = () => setS(calc()); addEventListener("resize", on); return () => removeEventListener("resize", on); }, []);
  return s;
}

/** Fixed-size cell so the recreation keeps the reference's row heights; the components inside stay fluid. */
const Box = ({ h, children, className }: { h: number; children: ReactNode; className?: string }) => <div className={className} style={{ height: h }}>{children}</div>;

export function Ref12Screen({ sideTab }: { sideTab?: ReactNode }) {
  const { theme, setTheme } = useApp();
  const [view, setView] = useState<"list" | "grid" | "board">("grid");
  const [day, setDay] = useState<number | string>(2);
  const ic = (I: typeof Sun) => <I size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />;
  return (
    <AppWindow style={{ position: "absolute", left: 62, top: 48, width: 1316, height: 983 }}>
      {/* Layer 2: side tab slot (140 px column). */}
      <div className="absolute inset-y-0 left-0 w-[140px]">
        {sideTab ?? (
          <div aria-label="Side tab slot" role="note" className="m-5 flex h-[calc(100%-40px)] items-center justify-center rounded-[20px] border-[1.5px] border-dashed border-r12-dash text-[11px] text-r12-muted [writing-mode:vertical-rl]">
            SideTab slot, built on feat/ui-sidetab
          </div>
        )}
      </div>

      {/* Layer 3: content, inset 140 left, 40 top, 48 right. */}
      <div className="absolute top-10 right-12 left-[140px]">
        <TopBar className="!gap-x-6"
          nav={<TopNav label="Sections" tabs={TABS} active="tonight" />}
          search={<CommandSearch label="Search" placeholder="Search or command" />}
          actions={<>
            <SegmentedToggle label="Theme" value={theme} onChange={setTheme} items={THEME_ITEMS(ic(Sun), ic(Moon))} />
            <span className="flex">
              <IconButton label="Notifications"><Bell size={ICON.lg} strokeWidth={ICON.stroke} aria-hidden /></IconButton>
              <IconButton label="Settings"><Settings size={ICON.lg} strokeWidth={ICON.stroke} aria-hidden /></IconButton>
            </span>
            <ExportButton label="Export results" chip=".csv" />
            <ActionButton variant="ink">New story</ActionButton>
          </>} />

        {/* Row A: hero 360 | add tile 184 | three feature tiles 184, 18 px apart; the last tile crosses the window edge. */}
        <div className="mt-9 flex h-[184px] items-start gap-[18px]">
          <HeroGreeting className="mr-[-18px] w-[404px] shrink-0 -mt-1" greeting={HERO.greeting} question={HERO.question} body={HERO.body}
            badges={<><HeroBadge label="Night run is on"><Moon size={ICON.md} strokeWidth={ICON.stroke} /></HeroBadge><HeroBadge label="3 videos ready"><Film size={ICON.md} strokeWidth={ICON.stroke} /></HeroBadge></>} />
          <div className="w-[184px] shrink-0"><DashedAddCard label="Start something new" /></div>
          {FEATURES.map((f, n) => (
            <div key={f.title} className="w-[184px] shrink-0">
              <FeatureCard {...f} illustration={[<StoryIllustration key="s" />, <ClipsIllustration key="c" />, <SeriesIllustration key="r" />][n]} />
            </div>
          ))}
        </div>

        {/* Body: 358 | 358 | 1fr (366), 22 px gutters. */}
        <div className="mt-12 grid gap-x-[22px]" style={{ gridTemplateColumns: "358px 358px minmax(0, 1fr)" }}>
          <div className="col-span-2 flex flex-col gap-5">
            <div className="grid grid-cols-2 gap-x-[22px]" style={{ height: 287 }}>
              <NotificationsCard title="Job events" clearLabel="Clear" className="h-full">
                <FloatingEventCard {...EVENT} actions={EVENT_ACTIONS} moreLabel="More actions for this event" className="mt-[-4px]" />
                <MessageRow {...MESSAGE} moreLabel="More actions for this message" className="ml-5 py-3" />
              </NotificationsCard>
              <AssignmentsCard title="Tonight's plan" editLabel="Edit" items={PLAN} moreLabel="More actions" addLabel="Add to tonight's plan" className="h-full" />
            </div>
            <div className="grid gap-[23px]" style={{ gridTemplateColumns: "534px minmax(0, 1fr)", height: 290 }}>
              <TodayTasksCard className="mt-1 h-[286px]" title="Render jobs" people={PEOPLE} onAddPerson={() => undefined} addPersonLabel="Add a channel"
                view={view} views={VIEW_ITEMS} onView={setView} viewLabel="Layout" editLabel="Edit" shareLabel="Share" tasks={RENDERS} raisedId="r1" />
              <PremiumCard className="h-full" title={PREMIUM.title} body={PREMIUM.body} actionLabel={PREMIUM.action} illustration={<VoiceIllustration />} />
            </div>
          </div>
          <div className="flex flex-col gap-6">
            <Box h={302}><CalendarCard className="h-full" month="November 2026" prevLabel="Previous week" nextLabel="Next week" days={WEEK} active={day} onPick={(d) => setDay(d.day)} slots={SCHEDULE} moreLabel="More actions" /></Box>
            <Box h={120} className="grid grid-cols-2 gap-[17px]">
              {STATS.map((s) => <StatRingCard key={s.title} {...s} actionLabel={s.action} />)}
            </Box>
            <Box h={128}><BoardMeetingCard className="h-full !p-5" title={SLOT.title} date={SLOT.date} description={SLOT.description} editLabel="Edit" secondaryLabel={SLOT.secondary} primaryLabel={SLOT.primary} /></Box>
          </div>
        </div>
      </div>
    </AppWindow>
  );
}

export default function DesignRef12() {
  const s = useFitScale();
  useEffect(() => { document.title = "ref12 recreation · Nightshift"; }, []);
  return (
    <div className="flex h-dvh w-full items-center justify-center overflow-hidden bg-r12-page">
      <div style={{ width: FRAME.w * s, height: FRAME.h * s }} className="relative shrink-0">
        <div className="absolute top-0 left-0 origin-top-left" style={{ width: FRAME.w, height: FRAME.h, transform: `scale(${s})` }}>
          <PageBackdrop />
          <Ref12Screen />
        </div>
      </div>
    </div>
  );
}
