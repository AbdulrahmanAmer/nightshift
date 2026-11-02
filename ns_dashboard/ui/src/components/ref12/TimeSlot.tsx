import { AgendaItem, type AgendaEvent } from "./AgendaItem";
import { DottedRule } from "./DottedRule";

export interface AgendaSlot { time: string; events: AgendaEvent[] }

/** TimeSlot (Planify): md/600 time range followed by a DottedRule to the edge, then its AgendaItems 12 px below. */
export function TimeSlot({ slot, moreLabel, onMore }: { slot: AgendaSlot; moreLabel: string; onMore?: (id: string) => void }) {
  return (
    <section>
      <h3 className="flex items-center gap-2 text-ds-md font-semibold ds-num"><span className="shrink-0">{slot.time}</span><DottedRule /></h3>
      <ul className="mt-3 list-none p-0">{slot.events.map((e) => <AgendaItem key={e.id} event={e} moreLabel={moreLabel} onMore={onMore} />)}</ul>
    </section>
  );
}
