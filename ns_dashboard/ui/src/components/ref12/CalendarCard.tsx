import { useId } from "react";
import { Card } from "./Card";
import { CardHeader } from "./CardHeader";
import { IconButton } from "./IconButton";
import { TimeSlot, type AgendaSlot } from "./TimeSlot";
import { WeekStrip, type WeekDay } from "./WeekStrip";

/** CalendarCard (Planify): CardHeader with round soft pager buttons, a WeekStrip, then TimeSlots
 *  (20 px below the strip, 16 px between slots). */
export function CalendarCard({ monthLabel, days, selected, slots, onSelect, onPrev, onNext, onEventMore, prevLabel, nextLabel, weekLabel, moreLabel, className }: {
  monthLabel: string; days: WeekDay[]; selected: string; slots: AgendaSlot[]; onSelect?: (iso: string) => void; onPrev?: () => void; onNext?: () => void; onEventMore?: (id: string) => void;
  prevLabel: string; nextLabel: string; weekLabel: string; moreLabel: string; className?: string;
}) {
  const id = useId();
  return (
    <Card as="section" aria-labelledby={id} className={className}>
      <CardHeader id={id} title={monthLabel} actions={<>
        <IconButton icon="chevronLeft" label={prevLabel} size="sm" variant="soft" round onClick={onPrev} />
        <IconButton icon="chevronRight" label={nextLabel} size="sm" variant="soft" round onClick={onNext} />
      </>} />
      <WeekStrip days={days} selected={selected} onSelect={onSelect} label={weekLabel} />
      <div className="mt-5 flex flex-col gap-4">{slots.map((s) => <TimeSlot key={s.time} slot={s} moreLabel={moreLabel} onMore={onEventMore} />)}</div>
    </Card>
  );
}
