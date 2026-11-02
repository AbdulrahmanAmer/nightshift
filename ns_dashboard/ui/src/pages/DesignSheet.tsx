/* #/design: the ref12 component sheet. Every component from src/components/ref12 rendered with
   Nightshift sample content, labelled with its file and key measurements, light and dark side by side.
   Measurements and the full map: docs/design/ref12-map.md. */
import { useEffect, useState, type ReactNode } from "react";
import { Bell, Moon, Settings, Sun } from "lucide-react";
import { useApp } from "../lib/app";
import {
  ActionButton, AgendaTimeline, AssignmentItem, AssignmentsCard, AvatarStack, BoardMeetingCard, CalendarCard, Chip, ClipsIllustration, CommandSearch,
  DashedAddButton, DashedAddCard, ExportButton, FeatureCard, FloatingEventCard, HeroBadge, HeroGreeting, ICON, IconButton, KbdChip, MessageRow,
  NotificationsCard, PremiumCard, ProgressBar, RingProgress, SegmentedToggle, SeriesIllustration, StatRingCard, StoryIllustration, TaskRow,
  TodayTasksCard, TopBar, TopNav, VoiceIllustration, WeekStrip,
} from "../components/ref12";
import { EVENT, EVENT_ACTIONS, FEATURES, HERO, MESSAGE, PEOPLE, PLAN, PREMIUM, RENDERS, SCHEDULE, SLOT, STATS, TABS, THEME_ITEMS, VIEW_ITEMS, WEEK } from "./design/sample";

const ILLOS = [<StoryIllustration key="s" />, <ClipsIllustration key="c" />, <SeriesIllustration key="r" />];
const ic = (I: typeof Sun) => <I size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />;

/** One specimen: name, file and spec, then the same render in a light and a dark panel. */
function Specimen({ name, file, spec, children, wide }: { name: string; file: string; spec: string; children: () => ReactNode; wide?: boolean }) {
  return (
    <section aria-labelledby={`sp-${slug(name)}`} className="min-w-0 scroll-mt-4" id={slug(name)}>
      <header className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={`sp-${slug(name)}`} className="text-[16px] leading-6 font-semibold text-r12-ink">{name}</h2>
        <code className="font-mono text-[12px] text-r12-muted">{file}</code>
        <p className="w-full text-[13px] leading-5 text-r12-text-2">{spec}</p>
      </header>
      <div className={wide ? "grid gap-3" : "grid gap-3 lg:grid-cols-2"}>
        {(["light", "dark"] as const).map((t) => (
          <div key={t} className={`r12-${t} min-w-0 rounded-[24px] bg-r12-window p-4 ring-1 ring-r12-line sm:p-6`}>
            <p className="mb-3 font-mono text-[11px] text-r12-muted">{t === "light" ? "Light" : "Dark"}</p>
            {children()}
          </div>
        ))}
      </div>
    </section>
  );
}

type Section = { name: string; file: string; spec: string; wide?: boolean; render: () => ReactNode };
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");


function useSections() {
  const { theme, setTheme } = useApp();
  const [tray, setTray] = useState<"light" | "dark">("light");
  const [view, setView] = useState<"list" | "grid" | "board">("grid");
  const [day, setDay] = useState<number | string>(2);
  const SECTIONS: Section[] = [];
  const add = (s: Section) => { SECTIONS.push(s); };
  add({ name: "TopBar", file: "TopNav.tsx", wide: true, spec: "Tabs 14 medium, 16 px icons, 24 px apart, active = 2 px ink underline. Search 212×36, r 12. Actions 36 px tall, 10 px apart. Wraps instead of squeezing.",
    render: () => <TopBar nav={<TopNav label="Sections" tabs={TABS} active="tonight" />} search={<CommandSearch label="Search" placeholder="Search or command" />}
      actions={<>
        <SegmentedToggle label="Theme" value={theme} onChange={setTheme} items={THEME_ITEMS(ic(Sun), ic(Moon))} />
        <span className="flex"><IconButton label="Notifications"><Bell size={ICON.lg} strokeWidth={ICON.stroke} aria-hidden /></IconButton><IconButton label="Settings"><Settings size={ICON.lg} strokeWidth={ICON.stroke} aria-hidden /></IconButton></span>
        <ExportButton label="Export results" chip=".csv" />
        <ActionButton variant="ink">New story</ActionButton>
      </>} /> });
  add({ name: "Controls", file: "SegmentedToggle.tsx · ActionButton.tsx · ExportButton.tsx · KbdChip.tsx · IconButton.tsx · CommandSearch.tsx", spec: "Tray 36 px (tint-2, 3 px inset, 30 px items, r 8). Buttons md 36 / sm 28, r 10 / 8. KbdChip 22 px, r 6.",
    render: () => (
      <div className="flex flex-wrap items-center gap-3">
        <SegmentedToggle label="Theme preview" value={tray} onChange={setTray} items={THEME_ITEMS(ic(Sun), ic(Moon))} />
        <SegmentedToggle variant="icons" label="Layout" value={view} onChange={setView} items={VIEW_ITEMS} />
        <ActionButton variant="ink">New story</ActionButton>
        <ActionButton variant="accent">Open in Queue</ActionButton>
        <ActionButton variant="soft">Reschedule</ActionButton>
        <ActionButton variant="ghost" size="sm">Edit</ActionButton>
        <ActionButton variant="accent" size="sm">Check</ActionButton>
        <ExportButton label="Export results" chip=".csv" />
        <KbdChip>.csv</KbdChip><KbdChip tone="plain">A</KbdChip>
        <IconButton label="Notifications"><Bell size={ICON.lg} strokeWidth={ICON.stroke} aria-hidden /></IconButton>
        <CommandSearch label="Search" placeholder="Search or command" />
      </div>
    ) });
  add({ name: "HeroGreeting", file: "HeroGreeting.tsx · AvatarStack.tsx", spec: "36/44 semibold over 16/22 text-2, 16 px apart; 28/36 under 640 px. Badges 28 px, overlapping 6 px. AvatarStack 24 px, -8 px overlap, accent plus.",
    render: () => (
      <div className="flex flex-col gap-4">
        <HeroGreeting as="h2" greeting={HERO.greeting} question={HERO.question} body={HERO.body}
          badges={<><HeroBadge label="Night run is on"><Moon size={ICON.md} strokeWidth={ICON.stroke} /></HeroBadge><HeroBadge label="3 videos ready"><Bell size={ICON.md} strokeWidth={ICON.stroke} /></HeroBadge></>} />
        <AvatarStack people={PEOPLE} onMore={() => undefined} moreLabel="Add a channel" />
      </div>
    ) });
  add({ name: "DashedAddCard + FeatureCard", file: "DashedAddCard.tsx · FeatureCard.tsx · Illustrations.tsx", spec: "Square tiles, 184 px in the 1440 frame, 18 px apart, r 24. Feature tile: raised shadow, 16 px padding, 15 px semibold title, 12 px faint subtitle, lifts 2 px on hover.",
    render: () => (
      <div className="grid grid-cols-2 gap-[18px] sm:grid-cols-4">
        <DashedAddCard label="Start something new" />
        {FEATURES.map((f, n) => <FeatureCard key={f.title} {...f} illustration={ILLOS[n]} />)}
      </div>
    ) });
  add({ name: "NotificationsCard", file: "NotificationsCard.tsx · FloatingEventCard.tsx · MessageRow.tsx", spec: "Card r 28, 24 px padding, overflow visible. FloatingEventCard = a swiped row: slides 44 px left, float shadow, tint action tray behind. MessageRow r 16 with a 24 px preview well.",
    render: () => (
      <div className="max-w-[400px] sm:pl-6">
        <NotificationsCard title="Job events" clearLabel="Clear">
          <FloatingEventCard {...EVENT} actions={EVENT_ACTIONS} moreLabel="More actions for this event" />
          <MessageRow {...MESSAGE} moreLabel="More actions for this message" className="sm:ml-5" />
        </NotificationsCard>
      </div>
    ) });
  add({ name: "AssignmentsCard", file: "AssignmentsCard.tsx · Chip.tsx · DashedAddCard.tsx", spec: "Inner item r 16, 16 px padding. Tags 12 medium (first in accent). Title 15/20 semibold, two lines max. Soft chip 24 px; solid tag chip 28 px; dashed add bar 56 px.",
    render: () => <div className="max-w-[400px]"><AssignmentsCard title="Tonight's plan" editLabel="Edit" items={PLAN} moreLabel="More actions" addLabel="Add to tonight's plan" /></div> });
  add({ name: "CalendarCard", file: "CalendarCard.tsx · AgendaTimeline.tsx", spec: "Week strip of 7 equal columns, active day = 32 px accent circle. Agenda slot label 14 semibold + dotted rule; event row 48 px with a 32 px tint icon tile and a vertical kebab.",
    render: () => <div className="max-w-[400px]"><CalendarCard month="November 2026" prevLabel="Previous week" nextLabel="Next week" days={WEEK} active={day} onPick={(d) => setDay(d.day)} slots={SCHEDULE} moreLabel="More actions" /></div> });
  add({ name: "WeekStrip + AgendaTimeline", file: "CalendarCard.tsx · AgendaTimeline.tsx", spec: "The two halves of CalendarCard, usable on their own (Channels schedule, Settings run time).",
    render: () => <div className="flex flex-col gap-4"><WeekStrip days={WEEK} active={day} onPick={(d) => setDay(d.day)} /><AgendaTimeline slots={SCHEDULE} moreLabel="More actions" /></div> });
  add({ name: "TodayTasksCard", file: "TodayTasksCard.tsx · TaskRow.tsx · ProgressBar.tsx", wide: true, spec: "Rows 64 px pitch, r 16; the current row is raised. Columns at 448 px and wider: name | metric | progress | counts; narrower rows wrap to two lines. Progress track 4 px, 72 px.",
    render: () => <TodayTasksCard title="Render jobs" people={PEOPLE} onAddPerson={() => undefined} addPersonLabel="Add a channel" view={view} views={VIEW_ITEMS} onView={setView} viewLabel="Layout" editLabel="Edit" shareLabel="Share" tasks={RENDERS} raisedId="r1" /> });
  add({ name: "TaskRow, ProgressBar, RingProgress, Chip", file: "TaskRow.tsx · ProgressBar.tsx · RingProgress.tsx · Chip.tsx", spec: "Building blocks. Ring 48 px, 4 px stroke, round cap from 12 o'clock. Chips 24 px, r 8: soft (tinted) and solid.",
    render: () => (
      <div className="flex flex-col gap-4">
        <ul><TaskRow task={RENDERS[1]!} raised /></ul>
        <div className="flex flex-wrap items-center gap-4">
          <ProgressBar value={72} label="Render progress" />
          <ProgressBar value={40} label="Upload progress" tone="green" />
          <RingProgress value={90} label="Shorts QC" tone="green" />
          <RingProgress value={65} label="Retention" tone="red" />
          <RingProgress value={30} label="Disk used" tone="accent" />
        </div>
        <div className="flex flex-wrap gap-2"><Chip tone="red">First</Chip><Chip tone="accent">Short</Chip><Chip tone="green" variant="solid">Slow burn</Chip><Chip tone="muted">Paused</Chip></div>
      </div>
    ) });
  add({ name: "PremiumCard", file: "PremiumCard.tsx · Illustrations.tsx", spec: "The one saturated surface. Accent fill, r 28, centred column: 96 px illustration, 20/28 white title, 13/18 white 85% body, navy 36 px button.",
    render: () => <div className="max-w-[200px]"><PremiumCard title={PREMIUM.title} body={PREMIUM.body} actionLabel={PREMIUM.action} illustration={<VoiceIllustration />} /></div> });
  add({ name: "StatRingCard", file: "StatRingCard.tsx", spec: "Compact card, 16 px padding. Ring + eyebrow (tone) + 15 px title; two muted meta lines; optional small accent button.",
    render: () => <div className="grid max-w-[400px] grid-cols-2 gap-4">{STATS.map((s) => <StatRingCard key={s.title} {...s} actionLabel={s.action} />)}</div> });
  add({ name: "BoardMeetingCard", file: "BoardMeetingCard.tsx", spec: "22/28 title + Edit, light-accent dot date line, two-line description left, soft + accent 32 px buttons right; buttons wrap below under 480 px.",
    render: () => <div className="max-w-[400px]"><BoardMeetingCard title={SLOT.title} date={SLOT.date} description={SLOT.description} editLabel="Edit" secondaryLabel={SLOT.secondary} primaryLabel={SLOT.primary} /></div> });
  add({ name: "AssignmentItem + DashedAddButton", file: "AssignmentsCard.tsx · DashedAddCard.tsx", spec: "Standalone row and add bar, for Library ideas and the Queue checklist.",
    render: () => <div className="flex max-w-[400px] flex-col gap-4"><AssignmentItem data={PLAN[0]!} moreLabel="More actions" /><DashedAddButton label="Add an idea" /></div> });
  return SECTIONS;
}

export default function DesignSheet() {
  const { theme, setTheme } = useApp();
  const sections = useSections();
  useEffect(() => { document.title = "ref12 component sheet · Nightshift"; }, []);
  return (
    <div className="min-h-dvh bg-r12-page text-r12-ink">
      <div className="mx-auto max-w-[1440px] px-4 py-6 sm:px-8 sm:py-10">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="font-mono text-[12px] text-r12-muted">src/components/ref12</p>
            <h1 className="mt-1 text-[28px] leading-9 font-semibold">ref12 component sheet</h1>
            <p className="mt-1 max-w-[640px] text-[14px] leading-5 text-r12-text-2">Every component from the reference, with Nightshift content, in light and dark. Sizes are for the 1440 design frame. The full map is in docs/design/ref12-map.md.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <a href="#/design/ref12" className="r12-focus inline-flex h-9 items-center rounded-r12-control bg-r12-accent px-4 text-[13px] font-medium text-r12-on-accent no-underline">Whole screen</a>
            <a href="#/" className="r12-focus inline-flex h-9 items-center rounded-r12-control bg-r12-tint-2 px-4 text-[13px] font-medium text-r12-ink no-underline">Back to Queue</a>
            <SegmentedToggle label="Page theme" value={theme} onChange={setTheme} items={THEME_ITEMS(ic(Sun), ic(Moon))} />
          </div>
        </header>
        <nav aria-label="Components" className="mt-6 flex flex-wrap gap-2">
          {sections.map((s) => <a key={s.name} href={`#/design?c=${encodeURIComponent(s.name)}`} onClick={(e) => { e.preventDefault(); document.getElementById(slug(s.name))?.scrollIntoView({ behavior: "smooth" }); }} className="r12-focus rounded-full bg-r12-card px-3 py-1 text-[12px] text-r12-text-2 no-underline hover:text-r12-ink">{s.name}</a>)}
        </nav>
        <div className="mt-8 flex flex-col gap-12">
          {sections.map((s) => <Specimen key={s.name} name={s.name} file={s.file} spec={s.spec} wide={s.wide}>{s.render}</Specimen>)}
        </div>
      </div>
    </div>
  );
}
