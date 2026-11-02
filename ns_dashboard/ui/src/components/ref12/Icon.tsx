import {
  ArrowDown, ArrowUp, AudioLines, Bell, Blend, BookOpen, Briefcase, Calendar, CalendarDays, Captions, Check, ChevronDown, ChevronLeft, ChevronRight,
  Clapperboard, Clock, Columns3, Copy, Cpu, Download, Ellipsis, EllipsisVertical, ExternalLink, FileArchive, FileSearch, FileText, FileVideo, Film,
  FolderOpen, Github, HardDrive, Image, KeyRound, LayoutGrid, LayoutList, Layers, Lightbulb, Link2, ListChecks, Lock, LockOpen, Mail, Megaphone,
  MessageCircle, Mic, MonitorPlay, Moon, Pause, PenLine, PieChart, Play, Plus, Minus, Power, RefreshCw, RotateCcw, Scissors, Search, Settings, Share2,
  SkipBack, SkipForward, Smartphone, SquarePen, Sun, Target, Trash2, TriangleAlert, Truck, Tv, Undo2, Upload, User, Users, Video, Wand2, X,
  type LucideIcon,
} from "lucide-react";

/** One icon registry for the ref12 components and pages (Planify's Icon.tsx plus the Nightshift icons).
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
  // pages and ported widgets
  play: Play, pause: Pause, skipBack: SkipBack, skipForward: SkipForward, minus: Minus, check: Check, x: X, blend: Blend, chevronDown: ChevronDown,
  cpu: Cpu, disk: HardDrive, folder: FolderOpen, copy: Copy, external: ExternalLink, image: Image, undo: Undo2, arrowUp: ArrowUp,
  lightbulb: Lightbulb, fileSearch: FileSearch, key: KeyRound, lock: Lock, lockOpen: LockOpen, github: Github, refresh: RefreshCw,
  archive: FileArchive, restore: RotateCcw, monitor: MonitorPlay, phone: Smartphone, fileVideo: FileVideo, alert: TriangleAlert,
  book: BookOpen, checklist: ListChecks, captions: Captions, wand: Wand2, power: Power,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof icons;

/** Icon: 18 px, 1.75 stroke by default. Hidden from assistive tech unless it carries meaning (label).
 *  `filled` paints silhouette glyphs (play, pause, skip) solid, as Planify's transport does. */
export function Icon({ name, size = 18, strokeWidth = 1.75, className, label, filled }: { name: IconName; size?: number; strokeWidth?: number; className?: string; label?: string; filled?: boolean }) {
  const Cmp = icons[name];
  return <Cmp size={size} strokeWidth={strokeWidth} fill={filled ? "currentColor" : "none"} className={className} aria-hidden={label ? undefined : true} aria-label={label} role={label ? "img" : undefined} />;
}
