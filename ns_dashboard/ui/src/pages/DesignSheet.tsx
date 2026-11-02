/* #/design: the ref12 component sheet. Every component in src/components/ref12 (the Planify library ported onto
   our Tailwind tokens) rendered with Nightshift sample content, labelled with its file, origin and spec,
   in a light and a dark panel side by side. Full map: docs/design/ref12-map.md. */
import { useEffect, useState, type ReactNode } from "react";
import { useApp } from "../lib/app";
import {
  AssignmentCard, Avatar, AvatarGroup, Badge, Button, CalendarCard, CalendarIllustration, Card, CardHeader, ClipsIllustration, CollaborateIllustration,
  CreateTile, DottedRule, FeatureCard, GiftIllustration, Greeting, Icon, IconButton, MeetingCard, MetaItem, NavTabs, NotesIllustration, NotificationItem,
  ProgressBar, ProgressRing, PromoCard, SearchInput, SegmentedControl, SeriesIllustration, StatCard, StatusDot, StoryIllustration, SwipeActions,
  TaskList, TextField, TimeSlot, VoiceIllustration, WeekStrip, WorkspaceTokens,
} from "../components/ref12";
import { EVENT, FEATURES, HERO, MESSAGE, PEOPLE, PLAN, PROMO, RENDERS, SCHEDULE, SLOT, STATS, TABS, THEME_OPTIONS, VIEW_OPTIONS, WEEK } from "./design/sample";

type Origin = "Planify" | "Planify + draft" | "Draft";
type Section = { name: string; file: string; origin: Origin; spec: string; wide?: boolean; render: () => ReactNode };
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

function Specimen({ s }: { s: Section }) {
  const id = slug(s.name);
  return (
    <section aria-labelledby={`sp-${id}`} id={id} className="min-w-0 scroll-mt-4">
      <header className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={`sp-${id}`} className="text-ds-lg font-semibold">{s.name}</h2>
        <code className="font-mono text-[12px] text-ds-muted">{s.file}</code>
        <Badge tone={s.origin === "Planify" ? "accent" : s.origin === "Draft" ? "neutral" : "success"} size="sm">{s.origin}</Badge>
        <p className="w-full text-ds-sm leading-ds-snug text-ds-text-2">{s.spec}</p>
      </header>
      <div className={s.wide ? "grid gap-3" : "grid gap-3 lg:grid-cols-2"}>
        {(["light", "dark"] as const).map((t) => (
          <div key={t} data-theme={t} className="ds-root min-w-0 rounded-ds-xl bg-ds-app p-4 shadow-ds-0 sm:p-6">
            <p className="mb-3 font-mono text-[11px] text-ds-muted">{t === "light" ? "Light" : "Dark"}</p>
            {s.render()}
          </div>
        ))}
      </div>
    </section>
  );
}

function useSections(): Section[] {
  const { theme, setTheme } = useApp();
  const [seg, setSeg] = useState<"light" | "dark">("light");
  const [view, setView] = useState<"list" | "grid" | "board">("grid");
  const [day, setDay] = useState(WEEK[0]!.iso);
  const [task, setTask] = useState("r1");
  const [open, setOpen] = useState(true);
  return [
    { name: "NavTabs + SearchInput", file: "NavTabs.tsx · SearchInput.tsx", origin: "Planify", wide: true,
      spec: "Tabs: 15 px icon + sm/600, 24 apart, inactive 78% opacity, active = 2 px currentColor underline. Search 36 px, radius md, default border, xs text, focus ring.",
      render: () => <div className="flex flex-wrap items-center gap-x-8 gap-y-3"><NavTabs label="Sections" tabs={TABS} active="tonight" /><SearchInput label="Search" placeholder="Search or run a command" shortcut="Ctrl K" className="w-full sm:w-[240px]" /></div> },
    { name: "Button", file: "Button.tsx", origin: "Planify",
      spec: "primary · inverse · soft · ghost · dashed; sm 28 / md 36 / lg 40, radius sm/md, 600 weight; leading/trailing icon and a trailing Badge slot. Press nudges 1 px down.",
      render: () => (
        <div className="flex flex-wrap items-center gap-2">
          <Button>Review</Button><Button variant="inverse">New story</Button><Button variant="soft">Reschedule</Button>
          <Button variant="ghost" size="sm" leadingIcon="edit">Edit</Button><Button variant="dashed" leadingIcon="plus">Add an idea</Button>
          <Button variant="soft" leadingIcon="download" trailing={<Badge tone="accent" variant="solid" size="xs">csv</Badge>}>Export results</Button>
          <Button size="sm">Check</Button><Button size="lg" variant="inverse">Open Voices</Button><Button disabled>Disabled</Button>
        </div>
      ) },
    { name: "IconButton + SegmentedControl", file: "IconButton.tsx · SegmentedControl.tsx", origin: "Planify",
      spec: "IconButton ghost/soft/surface/accent, sm 24 / md 36 / lg 44, optional amber dot; label required. SegmentedControl: radio group with arrow keys; filled track (theme) or plain (view switch).",
      render: () => (
        <div className="flex flex-wrap items-center gap-3">
          <IconButton icon="bell" label="Notifications" dot /><IconButton icon="settings" label="Settings" /><IconButton icon="chevronLeft" label="Previous" variant="soft" size="sm" round />
          <IconButton icon="plus" label="Add" variant="accent" /><IconButton icon="more" label="More" variant="surface" />
          <SegmentedControl label="Theme" value={seg} onChange={setSeg} options={THEME_OPTIONS} />
          <SegmentedControl label="View" variant="plain" size="sm" value={view} onChange={setView} options={VIEW_OPTIONS} />
          <SegmentedControl label="App theme" value={theme} onChange={setTheme} options={THEME_OPTIONS} size="sm" />
        </div>
      ) },
    { name: "Badge, StatusDot, Avatar, AvatarGroup", file: "Badge.tsx · StatusDot.tsx · Avatar.tsx · AvatarGroup.tsx", origin: "Planify",
      spec: "Badge solid/soft × accent/success/danger/neutral, xs 16 · sm 22 · md 28. StatusDot 7 px (ring option). Avatar 18/24/32 with initials fills; group overlaps 7 px with a + button.",
      render: () => (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="accent">After Dark Stories</Badge><Badge tone="danger" size="md">First</Badge><Badge tone="success" variant="solid" size="md">Slow burn</Badge>
            <Badge tone="neutral">Paused</Badge><Badge tone="accent" variant="solid" size="xs">NEW</Badge>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <StatusDot tone="online" label="Online" /><StatusDot tone="attention" /><StatusDot tone="accent" /><StatusDot tone="danger" />
            <Avatar name="Voice Ryan" size="xs" /><Avatar name="Tiny Mysteries" size="sm" /><Avatar name="After Dark" size="md" />
            <AvatarGroup people={PEOPLE} max={3} onAdd={() => undefined} addLabel="Add a channel" />
          </div>
        </div>
      ) },
    { name: "MetaItem, DottedRule, TextField", file: "MetaItem.tsx · DottedRule.tsx · TextField.tsx", origin: "Planify",
      spec: "MetaItem 13 px muted icon + 2xs caption. DottedRule 2 px dotted border-default. TextField 30 px accent-subtle quick-reply field.",
      render: () => (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-x-6 gap-y-2"><MetaItem icon="calendarDays">Mon, 2 Nov</MetaItem><MetaItem icon="clock">03:41 – 03:52</MetaItem><MetaItem icon="mail">Message from the clips stage</MetaItem></div>
          <div className="flex items-center gap-2 text-ds-md font-semibold"><span>18:00–21:00</span><DottedRule /></div>
          <TextField label="Reply" placeholder={MESSAGE.reply} />
        </div>
      ) },
    { name: "Card + CardHeader", file: "Card.tsx · CardHeader.tsx", origin: "Planify",
      spec: "elevation 0 flat · 1 resting · 2 raised · 3 floating; padding sm/md/lg; radius lg 16 / xl 24 / 2xl 32; tone surface/sunken/accent; interactive lifts 2 px.",
      render: () => (
        <div className="grid grid-cols-2 gap-4">
          {([0, 1, 2, 3] as const).map((e) => <Card key={e} elevation={e} padding="md"><CardHeader level={3} title={`Elevation ${e}`} className="!mb-1" /><p className="text-ds-xs text-ds-muted">shadow-ds-{e}</p></Card>)}
          <Card tone="sunken" padding="md"><p className="text-ds-sm font-semibold">Sunken</p></Card>
          <Card tone="accent" padding="md" elevation={2}><p className="text-ds-sm font-semibold">Accent</p></Card>
        </div>
      ) },
    { name: "ProgressBar + ProgressRing", file: "ProgressBar.tsx · ProgressRing.tsx", origin: "Planify + draft",
      spec: "Planify API and sizes (4 px track, 48/56 rings, sm/700 value); fills animate transform / stroke-dashoffset instead of width (from the draft).",
      render: () => (
        <div className="flex flex-wrap items-center gap-5">
          <ProgressBar value={90} label="Render" className="w-40" /><ProgressBar value={50} tone="success" label="Upload" className="w-40" />
          <ProgressRing value={90} tone="success" size={48} label="Shorts QC" /><ProgressRing value={65} tone="danger" size={48} label="Retention" /><ProgressRing value={30} label="Disk" />
        </div>
      ) },
    { name: "Greeting + WorkspaceTokens", file: "Greeting.tsx · WorkspaceTokens.tsx", origin: "Planify + draft",
      spec: "display/600 (40 px), tight leading, -0.02em, balanced wrap; md secondary description max 34ch, 20 px below. Copy is generic (salutation, question) instead of Planify's fixed \"Hi, {name}!\".",
      render: () => <Greeting as="h2" salutation={HERO.salutation} question={HERO.question} description={HERO.description} adornment={<WorkspaceTokens tokens={HERO.tokens} />} /> },
    { name: "CreateTile + FeatureCard", file: "CreateTile.tsx · FeatureCard.tsx · Illustrations.tsx", origin: "Planify",
      spec: "CreateTile square (min 160, radius xl) and row (56, radius lg): dashed border-dashed on accent-subtle, glowing plus. FeatureCard: elevation 2, radius xl, padding 16/20, art max 120 px, md/600 title, 2xs muted 2-line description.",
      render: () => (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <CreateTile label="Start something new" />
            {FEATURES.map((f, i) => <FeatureCard key={f.title} {...f} illustration={[<StoryIllustration key="a" />, <ClipsIllustration key="b" />, <SeriesIllustration key="c" />][i]} />)}
          </div>
          <CreateTile layout="row" label="Add to tonight's plan" />
        </div>
      ) },
    { name: "NotificationItem + SwipeActions", file: "NotificationItem.tsx · SwipeActions.tsx", origin: "Planify",
      spec: "Item radius lg, padding 12/12/12/16; resting = hairline + elevation 1, raised = elevation 3. SwipeActions: sunken tray, content slides 52 px left when open (toggle below).",
      render: () => (
        <div className="flex max-w-[420px] flex-col gap-4 pl-2">
          <SwipeActions open={open} actions={[{ icon: "trash", label: "Delete notification" }, { icon: "edit", label: "Open in Queue" }]}>
            <NotificationItem state={open ? "raised" : "resting"} title={EVENT.title} titleAdornment={<StatusDot tone="online" label="Done" />} subtitle={EVENT.subtitle} moreLabel="More options"
              meta={<><MetaItem icon="calendarDays">{EVENT.date}</MetaItem><MetaItem icon="clock">{EVENT.time}</MetaItem></>} />
          </SwipeActions>
          <NotificationItem className="ml-4" moreLabel="More options" title={<>{MESSAGE.title} <span className="font-normal text-ds-muted">|</span> {MESSAGE.context}</>} meta={<MetaItem icon="mail">{MESSAGE.from}</MetaItem>}>
            <TextField label="Reply" placeholder={MESSAGE.reply} />
          </NotificationItem>
          <Button variant="soft" size="sm" onClick={() => setOpen((o) => !o)} className="self-start">{open ? "Close swipe" : "Open swipe"}</Button>
        </div>
      ) },
    { name: "AssignmentCard", file: "AssignmentCard.tsx", origin: "Planify + draft",
      spec: "Planify layout (soft accent discipline badge, md/600 title, soft priority badge, solid success tag, 18 px assignee). Priority label and tone come in as props instead of a fixed High/Medium/Low enum.",
      render: () => <div className="max-w-[360px]"><AssignmentCard {...PLAN} moreLabel="Plan options" /></div> },
    { name: "CalendarCard", file: "CalendarCard.tsx · WeekStrip.tsx · TimeSlot.tsx · AgendaItem.tsx", origin: "Planify",
      spec: "WeekStrip 7 columns, 30 px date circles, selected = accent with a 4 px soft halo + glow. TimeSlot md/600 + DottedRule. AgendaItem 34 px sunken icon tile, sm/600 title, 2xs meta with a 5 px tone dot.",
      render: () => (
        <div className="flex flex-col gap-4">
          <div className="max-w-[380px]"><CalendarCard monthLabel="November 2026" days={WEEK} selected={day} onSelect={setDay} slots={SCHEDULE} prevLabel="Previous week" nextLabel="Next week" weekLabel="Week" moreLabel="Options for this upload" /></div>
          <WeekStrip days={WEEK} selected={day} onSelect={setDay} label="Week" />
          <TimeSlot slot={SCHEDULE[1]!} moreLabel="Options" />
        </div>
      ) },
    { name: "TaskList + TaskRow", file: "TaskList.tsx · TaskRow.tsx", origin: "Planify + draft", wide: true,
      spec: "Planify grid (1.3/0.8/1/auto, 24 gaps), highlighted row = surface + elevation 2 pulled 8 px left. Columns are generic (metric label/value, meta list) instead of fixed Duration/attachments/due; wraps by container width.",
      render: () => (
        <Card as="section" aria-label="Render jobs">
          <CardHeader title="Render jobs" adornment={<AvatarGroup people={PEOPLE} onAdd={() => undefined} addLabel="Add a channel" />}
            actions={<><SegmentedControl label="View" variant="plain" size="sm" value={view} onChange={setView} options={VIEW_OPTIONS} /><Button variant="ghost" size="sm" leadingIcon="edit">Edit</Button><Button variant="ghost" size="sm" leadingIcon="share">Share</Button></>} />
          <TaskList tasks={RENDERS} selectedId={task} onSelect={setTask} />
        </Card>
      ) },
    { name: "PromoCard + StatCard + MeetingCard", file: "PromoCard.tsx · StatCard.tsx · MeetingCard.tsx", origin: "Planify",
      spec: "Promo: accent, radius 2xl, accent glow, navy CTA in both themes. Stat: 48 px ring, 2xs uppercase tone eyebrow, md/600 title, 2xs captions, sm primary. Meeting: md padding, 8 px accent dot, soft + primary pair (labels via props).",
      render: () => (
        <div className="grid gap-4 sm:grid-cols-[200px_minmax(0,1fr)]">
          <PromoCard illustration={<VoiceIllustration />} title={PROMO.title} description={PROMO.description} ctaLabel={PROMO.cta} />
          <div className="grid content-start gap-4 sm:grid-cols-2">
            {STATS.map((s) => <StatCard key={s.title} {...s} />)}
            <MeetingCard className="sm:col-span-2" title={SLOT.title} when={SLOT.when} description={SLOT.description} editLabel="Edit" secondaryLabel={SLOT.secondary} primaryLabel={SLOT.primary} />
          </div>
        </div>
      ) },
    { name: "Illustrations", file: "Illustrations.tsx", origin: "Planify + draft",
      spec: "One Spot system: 120×100, 2 px currentColor strokes, fills from --ds-illus-paper / --ds-illus-accent. Calendar, Notes, Collaborate, Gift from Planify; Story, Clips, Series, Voice are the Nightshift spots redrawn on it.",
      render: () => (
        <div className="grid grid-cols-4 gap-4">
          {[<CalendarIllustration key="1" />, <NotesIllustration key="2" />, <CollaborateIllustration key="3" />, <div key="4" className="ds-illus-on-accent rounded-ds-lg bg-ds-accent p-2"><GiftIllustration /></div>,
            <StoryIllustration key="5" />, <ClipsIllustration key="6" />, <SeriesIllustration key="7" />, <div key="8" className="ds-illus-on-accent rounded-ds-lg bg-ds-accent p-2"><VoiceIllustration /></div>]}
        </div>
      ) },
    { name: "Icon registry", file: "Icon.tsx", origin: "Planify + draft",
      spec: "lucide through one name registry (Planify's names plus film, scissors, scenes, upload, megaphone, channels, mic, voice). 18 px, 1.75 stroke by default.",
      render: () => <div className="flex flex-wrap gap-3 text-ds-text-2">{(["dashboard", "search", "bell", "settings", "calendarDays", "clock", "mail", "trash", "edit", "film", "scissors", "scenes", "upload", "megaphone", "channels", "voice"] as const).map((n) => <Icon key={n} name={n} label={n} />)}</div> },
  ];
}

export default function DesignSheet() {
  const { theme, setTheme } = useApp();
  const sections = useSections();
  useEffect(() => { document.title = "ref12 component sheet · Nightshift"; }, []);
  return (
    <div className="ds-root min-h-dvh bg-ds-canvas">
      <div className="mx-auto max-w-[1440px] px-4 py-6 sm:px-8 sm:py-10">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="font-mono text-[12px] text-ds-muted">src/components/ref12</p>
            <h1 className="mt-1 text-ds-2xl leading-ds-tight font-semibold">ref12 component sheet</h1>
            <p className="mt-2 max-w-[640px] text-ds-md leading-ds-normal text-ds-text-2">The Planify library ported onto our Tailwind tokens, with Nightshift content, light and dark side by side. Each block says where it came from. The full map is in docs/design/ref12-map.md.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <a href="#/design/ref12" className="ds-focus inline-flex h-9 items-center rounded-ds-md bg-ds-accent px-4 text-ds-sm font-semibold text-ds-on-accent no-underline">Whole screen</a>
            <a href="#/" className="ds-focus inline-flex h-9 items-center rounded-ds-md bg-ds-accent-soft px-4 text-ds-sm font-semibold text-ds-text no-underline">Back to Queue</a>
            <SegmentedControl label="Page theme" value={theme} onChange={setTheme} options={THEME_OPTIONS} />
          </div>
        </header>
        <nav aria-label="Components" className="mt-6 flex flex-wrap gap-2">
          {sections.map((s) => <a key={s.name} href={`#/design`} onClick={(e) => { e.preventDefault(); document.getElementById(slug(s.name))?.scrollIntoView({ behavior: "smooth" }); }} className="ds-focus rounded-full bg-ds-surface px-3 py-1 text-ds-xs text-ds-text-2 no-underline shadow-ds-0 hover:text-ds-text">{s.name}</a>)}
        </nav>
        <div className="mt-8 flex flex-col gap-12">{sections.map((s) => <Specimen key={s.name} s={s} />)}</div>
      </div>
    </div>
  );
}
