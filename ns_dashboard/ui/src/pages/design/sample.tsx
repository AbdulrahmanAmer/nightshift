/* Sample Nightshift content for the ref12 design sheet (#/design) and the recreation (#/design/ref12).
   Same channels and titles as lib/demo.ts so the sheet reads like the real product. */
import type { AgendaSlot, NavTab, Person, SegmentOption, Task, WeekDay } from "../../components/ref12";

export const OWNER = "Abdo";

export const TABS: NavTab<"tonight" | "queue" | "channels">[] = [
  { id: "tonight", label: "Tonight", icon: "dashboard", href: "#/design/ref12" },
  { id: "queue", label: "Queue", icon: "film", href: "#/" },
  { id: "channels", label: "Channels", icon: "channels", href: "#/channels" },
];

export const THEME_OPTIONS: SegmentOption<"light" | "dark">[] = [
  { value: "light", label: "Light", icon: "sun" },
  { value: "dark", label: "Dark", icon: "moon" },
];

export const VIEW_OPTIONS: SegmentOption<"list" | "grid" | "board">[] = [
  { value: "list", icon: "viewList", ariaLabel: "List view" },
  { value: "grid", icon: "viewGrid", ariaLabel: "Grid view" },
  { value: "board", icon: "viewBoard", ariaLabel: "Board view" },
];

export const HERO = {
  salutation: `Good evening, ${OWNER}`,
  question: "What are we making tonight?",
  description: "Two stories written, one Short rendering. All of it in the Queue by breakfast.",
  tokens: [{ icon: "moon" as const, label: "Night run is on" }, { icon: "film" as const, label: "3 videos ready" }],
};

export const FEATURES = [
  { title: "Write a story", description: "Narrated while you sleep", href: "#/create" },
  { title: "Cut clips", description: "From videos you own", href: "#/create?mode=clip" },
  { title: "Build a series", description: "Plan the episodes with the Assistant", href: "#/assistant" },
];

export const EVENT = { title: "Render finished", subtitle: "After Dark Stories | Length 12:11", date: "Mon, 2 Nov", time: "03:41 – 03:52" };
export const MESSAGE = { title: "Job failed", context: "Tiny Mysteries", from: "Message from the clips stage", reply: "No footage matched three scenes. Add a Pexels key or widen the look" };

export const PLAN = {
  categories: ["After Dark Stories", "Full video"],
  title: "Write and narrate a 12-minute story about a doorbell camera",
  priority: { label: "First", tone: "danger" as const },
  tag: "Slow burn",
  assignee: { name: "Voice Ryan" },
};

export const WEEK: WeekDay[] = [
  { weekday: "Mon", date: 2, iso: "2026-11-02", label: "Monday 2 November" },
  { weekday: "Tue", date: 3, iso: "2026-11-03", label: "Tuesday 3 November" },
  { weekday: "Wed", date: 4, iso: "2026-11-04", label: "Wednesday 4 November" },
  { weekday: "Thu", date: 5, iso: "2026-11-05", label: "Thursday 5 November" },
  { weekday: "Fri", date: 6, iso: "2026-11-06", label: "Friday 6 November" },
  { weekday: "Sat", date: 7, iso: "2026-11-07", label: "Saturday 7 November" },
  { weekday: "Sun", date: 8, iso: "2026-11-08", label: "Sunday 8 November" },
];

export const SCHEDULE: AgendaSlot[] = [
  { time: "18:00–21:00", events: [{ id: "p1", icon: "upload", title: "The Doorbell Camera That Recorded Tomorrow", time: "18:30", tag: "Full video", tagTone: "success" }] },
  { time: "20:00–22:00", events: [{ id: "p2", icon: "megaphone", title: "My doorbell camera records someone every night", time: "20:15", tag: "Short", tagTone: "danger" }] },
];

export const PEOPLE: Person[] = [{ name: "After Dark Stories" }, { name: "Tiny Mysteries" }, { name: "Night Desk" }, { name: "Late Shift" }];

export const RENDERS: Task[] = [
  { id: "r1", title: "Doorbell Camera", date: "Started 03:00", metricLabel: "Length", metric: "12 m 11 s", progress: 90, progressLabel: "Render progress",
    meta: [{ icon: "scenes", value: 16, label: "scenes" }, { icon: "scissors", value: 4, label: "Shorts" }] },
  { id: "r2", title: "Bakery Receipts", date: "Started 03:42", metricLabel: "Length", metric: "14 m 06 s", progress: 50, progressLabel: "Render progress",
    meta: [{ icon: "scenes", value: 19, label: "scenes" }, { icon: "scissors", value: 3, label: "Shorts" }] },
  { id: "r3", title: "Stream, 31 October", date: "Queued 04:10", metricLabel: "Source", metric: "01 h 30 m", progress: 10, progressLabel: "Clip progress",
    meta: [{ icon: "scissors", value: 6, label: "clips" }] },
];

export const PROMO = { title: "Train a voice", description: "Clone a narrator from a 30-second sample on this PC", cta: "Open Voices" };

export const STATS = [
  { value: 90, tone: "success" as const, eyebrow: "Pass rate", title: "Shorts QC", caption: "18 of 20 passed", detail: "This week" },
  { value: 65, tone: "danger" as const, eyebrow: "Target 70%", title: "Retention", caption: "Viewed 65%", detail: "5 to go", action: { label: "Check" } },
];

export const SLOT = { title: "Next publish slot", when: "Today at 18:30", description: "Doorbell Camera, long cut", secondary: "Reschedule", primary: "Review" };
