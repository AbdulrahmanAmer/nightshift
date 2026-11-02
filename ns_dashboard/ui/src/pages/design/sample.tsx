/* Sample Nightshift content for the ref12 design sheet and the #/design/ref12 recreation.
   Same channels and titles as the demo data in lib/demo.ts so the sheet reads like the real product. */
import { AudioLines, Bell, Clapperboard, Download, FileText, Film, LayoutGrid, Library, List, Megaphone, Pencil, Rows3, Scissors, Settings, Sparkles, Trash2, Tv, Upload, Users } from "lucide-react";
import type { AgendaSlot, AssignmentData, AvatarData, SegmentItem, SwipeAction, TaskData, TopNavTab, WeekDay } from "../../components/ref12";
import { ICON } from "../../components/ref12";

const i = (I: typeof Bell, size: number = ICON.md) => <I size={size} strokeWidth={ICON.stroke} aria-hidden />;

export const OWNER = "Abdo";

export const TABS: TopNavTab[] = [
  { id: "tonight", label: "Tonight", href: "#/design/ref12", icon: i(LayoutGrid) },
  { id: "queue", label: "Queue", href: "#/", icon: i(Film) },
  { id: "channels", label: "Channels", href: "#/channels", icon: i(Tv) },
];

export const THEME_ITEMS = (sun: JSX.Element, moon: JSX.Element): SegmentItem<"light" | "dark">[] => [
  { id: "light", label: "Light", icon: sun },
  { id: "dark", label: "Dark", icon: moon },
];

export const VIEW_ITEMS: SegmentItem<"list" | "grid" | "board">[] = [
  { id: "list", label: "List view", icon: i(List, 14), iconOnly: true },
  { id: "grid", label: "Grid view", icon: i(LayoutGrid, 14), iconOnly: true },
  { id: "board", label: "Board view", icon: i(Rows3, 14), iconOnly: true },
];

export const HERO = {
  greeting: `Good evening, ${OWNER}`,
  question: "What are we making tonight?",
  body: "Two stories written, one Short rendering. All of it in the Queue by breakfast.",
};

export const FEATURES = [
  { title: "Write a story", subtitle: "Narrated while you sleep", href: "#/create" },
  { title: "Cut clips", subtitle: "From videos you own", href: "#/create?mode=clip" },
  { title: "Build a series", subtitle: "Plan the episodes", href: "#/assistant" },
];

export const EVENT = {
  title: "Render finished", statusLabel: "Done",
  meta: ["After Dark Stories", "Length 12:11"],
  date: "Mon, 2 Nov", time: "03:41 – 03:52",
};
export const EVENT_ACTIONS: SwipeAction[] = [
  { label: "Delete notification", icon: i(Trash2) },
  { label: "Open in Queue", icon: i(Pencil) },
];

export const MESSAGE = {
  title: "Job failed", context: "Tiny Mysteries", from: "Message from the clips stage",
  preview: "No footage matched three scenes. Add a Pexels key in Settings or widen the footage look",
};

export const PLAN: AssignmentData[] = [
  {
    tags: ["After Dark Stories", "Full video"],
    title: "Write and narrate a 12-minute story about a doorbell camera",
    priority: { label: "First", tone: "red" },
    tag: { label: "Slow burn", tone: "green" },
    assignee: { name: "Voice: Ryan", hue: 210 },
  },
];

export const WEEK: WeekDay[] = [
  { weekday: "Mon", day: 2, label: "Monday 2 November", marked: true },
  { weekday: "Tue", day: 3, label: "Tuesday 3 November" },
  { weekday: "Wed", day: 4, label: "Wednesday 4 November", marked: true },
  { weekday: "Thu", day: 5, label: "Thursday 5 November" },
  { weekday: "Fri", day: 6, label: "Friday 6 November" },
  { weekday: "Sat", day: 7, label: "Saturday 7 November", marked: true },
  { weekday: "Sun", day: 8, label: "Sunday 8 November" },
];

export const SCHEDULE: AgendaSlot[] = [
  { label: "18:00–21:00", events: [{ title: "The Doorbell Camera That Recorded Tomorrow", icon: i(Upload), time: "18:30", note: "Full video", tone: "green" }] },
  { label: "20:00–22:00", events: [{ title: "My doorbell camera records someone every night", icon: i(Megaphone), time: "20:15", note: "Short", tone: "red" }] },
];

export const PEOPLE: AvatarData[] = [
  { name: "After Dark Stories", hue: 250 },
  { name: "Tiny Mysteries", hue: 20 },
  { name: "Night Desk", hue: 160 },
];

const counts = (scenes: number, shorts: number) => [
  { icon: i(Clapperboard, 14), value: scenes, label: "scenes" },
  { icon: i(Scissors, 14), value: shorts, label: "Shorts" },
];
export const RENDERS: TaskData[] = [
  { id: "r1", name: "Doorbell Camera", when: "Started 03:00", metricLabel: "Length", metricValue: "12 m 11 s", progress: 90, progressLabel: "Render progress", counts: counts(16, 4) },
  { id: "r2", name: "Bakery Receipts", when: "Started 03:42", metricLabel: "Length", metricValue: "14 m 06 s", progress: 50, progressLabel: "Render progress", counts: counts(19, 3) },
  { id: "r3", name: "Stream, 31 October", when: "Queued 04:10", metricLabel: "Source", metricValue: "01 h 30 m", progress: 10, progressLabel: "Clip progress", counts: [{ icon: i(Scissors, 14), value: 6, label: "clips" }] },
];

export const PREMIUM = { title: "Train a voice", body: "Clone a narrator from a 30-second sample, right on this PC", action: "Open Voices" };

export const STATS = [
  { value: 90, tone: "green" as const, eyebrow: "Pass rate", title: "Shorts QC", meta: ["18 of 20 passed", "This week"] as [string, string] },
  { value: 65, tone: "red" as const, eyebrow: "Target 70%", title: "Retention", meta: ["Viewed 65%", "5 to go"] as [string, string], action: "Check" },
];

export const SLOT = {
  title: "Next publish slot", date: "Today at 18:30",
  description: "The Doorbell Camera, full video",
  secondary: "Reschedule", primary: "Review",
};

export const ICONS = { i, Bell, Settings, Download, FileText, Sparkles, Users, Library, AudioLines };
