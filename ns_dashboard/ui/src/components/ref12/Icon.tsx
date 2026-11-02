import {
  ArrowDown, AudioLines, Bell, Briefcase, Calendar, CalendarDays, ChevronLeft, ChevronRight, Clapperboard, Clock, Columns3, Download,
  Ellipsis, EllipsisVertical, FileText, Film, LayoutGrid, LayoutList, Layers, Link2, Mail, Megaphone, MessageCircle, Mic, Moon,
  PenLine, PieChart, Plus, Scissors, Search, Settings, Share2, SquarePen, Sun, Target, Trash2, Truck, Tv, Upload, User, Users, Video,
  type LucideIcon,
} from "lucide-react";

/** One icon registry for the ref12 sheets (Planify's Icon.tsx plus the Nightshift icons).
 *  Components name icons, so the set can be swapped in this one file. */
export const icons = {
  dashboard: LayoutGrid, workflows: Truck, integrations: Layers, search: Search, sun: Sun, moon: Moon, bell: Bell,
  settings: Settings, download: Download, arrowDown: ArrowDown, plus: Plus, user: User, users: Users, chart: PieChart,
  calendar: Calendar, calendarDays: CalendarDays, mail: Mail, notes: SquarePen, chat: MessageCircle, trash: Trash2,
  edit: PenLine, more: Ellipsis, moreVertical: EllipsisVertical, target: Target, clock: Clock, chevronLeft: ChevronLeft,
  chevronRight: ChevronRight, attachment: Link2, file: FileText, viewList: LayoutList, viewGrid: LayoutGrid, viewBoard: Columns3,
  share: Share2, video: Video, briefcase: Briefcase,
  // Nightshift
  film: Film, scissors: Scissors, scenes: Clapperboard, upload: Upload, megaphone: Megaphone, channels: Tv, mic: Mic, voice: AudioLines,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof icons;

/** Icon: 18 px, 1.75 stroke by default. Hidden from assistive tech unless it carries meaning (label). */
export function Icon({ name, size = 18, strokeWidth = 1.75, className, label }: { name: IconName; size?: number; strokeWidth?: number; className?: string; label?: string }) {
  const Cmp = icons[name];
  return <Cmp size={size} strokeWidth={strokeWidth} className={className} aria-hidden={label ? undefined : true} aria-label={label} role={label ? "img" : undefined} />;
}
