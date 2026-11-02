import { Pencil, Share } from "lucide-react";
import { useId, type ReactNode } from "react";
import { ActionButton } from "./ActionButton";
import { AvatarStack, type AvatarData } from "./AvatarStack";
import { Card, CardHeader } from "./Card";
import { SegmentedToggle, type SegmentItem } from "./SegmentedToggle";
import { TaskRow, type TaskData } from "./TaskRow";
import { ICON } from "./tokens";

/** TodayTasksCard: header = title + AvatarStack (24 px, accent "+"), right side = icon SegmentedToggle,
 *  Edit and Share ghost actions. Body = TaskRows, 0 gap (rows carry their own 12 px padding);
 *  the first (current) row is raised. */
export function TodayTasksCard<V extends string>({ title, people, onAddPerson, addPersonLabel, view, views, onView, viewLabel, editLabel, shareLabel, onEdit, onShare, tasks, raisedId, empty, className }: {
  title: string; people?: AvatarData[]; onAddPerson?: () => void; addPersonLabel?: string;
  view?: V; views?: SegmentItem<V>[]; onView?: (v: V) => void; viewLabel?: string;
  editLabel?: string; shareLabel?: string; onEdit?: () => void; onShare?: () => void;
  tasks: TaskData[]; raisedId?: string; empty?: ReactNode; className?: string;
}) {
  const id = useId();
  return (
    <Card labelledBy={id} className={className}>
      <CardHeader id={id} title={title}
        extra={people && <AvatarStack people={people} onMore={onAddPerson} moreLabel={addPersonLabel} />}
        action={<>
          {views && view && onView && <SegmentedToggle variant="icons" label={viewLabel ?? title} value={view} onChange={onView} items={views} className="mr-2" />}
          {editLabel && <ActionButton variant="ghost" size="sm" onClick={onEdit} icon={<Pencil size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />}>{editLabel}</ActionButton>}
          {shareLabel && <ActionButton variant="ghost" size="sm" onClick={onShare} icon={<Share size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />}>{shareLabel}</ActionButton>}
        </>} />
      {tasks.length ? (
        <ul className="-mx-2 mt-4 flex flex-col gap-1">
          {tasks.map((t) => <TaskRow key={t.id} task={t} raised={t.id === raisedId} />)}
        </ul>
      ) : empty}
    </Card>
  );
}
