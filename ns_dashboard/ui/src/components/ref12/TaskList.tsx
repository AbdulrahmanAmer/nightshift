import { cx } from "../../ui/kit";
import { TaskRow, type Task } from "./TaskRow";

/** TaskList (Planify): TaskRows 8 px apart, pulled 8 px past the card padding on both sides. */
export function TaskList({ tasks, selectedId, onSelect, className }: { tasks: Task[]; selectedId?: string; onSelect?: (id: string) => void; className?: string }) {
  return <ul className={cx("-mx-2 flex list-none flex-col gap-2 p-0", className)}>{tasks.map((t) => <TaskRow key={t.id} task={t} highlighted={t.id === selectedId} onSelect={onSelect} />)}</ul>;
}
