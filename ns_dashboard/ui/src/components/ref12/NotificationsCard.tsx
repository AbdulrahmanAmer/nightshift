import { Trash2 } from "lucide-react";
import { useId, type ReactNode } from "react";
import { cx } from "../../ui/kit";
import { ActionButton } from "./ActionButton";
import { Card, CardHeader } from "./Card";
import { ICON } from "./tokens";

/** NotificationsCard: card with a "Clear" ghost action and a 16 px gap list. Overflow is visible so a
 *  FloatingEventCard can leave the card's edge, exactly like the reference. Pass rows as children. */
export function NotificationsCard({ title, clearLabel, onClear, children, className }: { title: string; clearLabel?: string; onClear?: () => void; children: ReactNode; className?: string }) {
  const id = useId();
  return (
    <Card labelledBy={id} className={cx("overflow-visible", className)}>
      <CardHeader id={id} title={title} action={clearLabel && <ActionButton variant="ghost" size="sm" onClick={onClear} icon={<Trash2 size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />}>{clearLabel}</ActionButton>} />
      <div className="mt-4 flex flex-col gap-4">{children}</div>
    </Card>
  );
}
