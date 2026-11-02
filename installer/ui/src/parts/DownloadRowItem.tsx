// DownloadRow (docs/design/installer-ui.md section 2.4): 48 px, 64 px while active (it gains a second line).
import { Button, ProgressBar, StatusDot } from "@ref12";
import { cx } from "@dash/ui/kit";
import type { DownloadRow } from "../api/types";
import { download as t } from "../copy";
import { gbNum, mbps, minutesLeft, pct } from "../format";
import { Ring } from "./RowItems";

const LIVE = new Set<DownloadRow["status"]>(["active", "copying", "verifying", "extracting"]);

function sizeText(r: DownloadRow): string {
  if (r.bytesTotal == null) return "";
  if (r.status === "done") return r.bytesTotal < 1e8 ? `${Math.round(r.bytesTotal / 1e6)} MB` : `${gbNum(r.bytesTotal)} GB`;
  return r.bytesTotal < 1e8 ? `${Math.round(r.bytesDone / 1e6)}/${Math.round(r.bytesTotal / 1e6)} MB` : `${gbNum(r.bytesDone)}/${gbNum(r.bytesTotal)} GB`;
}

export function DownloadRowItem({ row, onRetry }: { row: DownloadRow; onRetry: () => void }) {
  const live = LIVE.has(row.status);
  const noSize = row.via === "winget" || row.via === "uv";
  const failed = row.status === "failed";
  const done = row.status === "done" || row.status === "skipped";
  const percent = pct(row.bytesDone, row.bytesTotal);
  const second = failed
    ? null
    : live && !noSize && row.speedBps
      ? `${mbps(row.speedBps)}${row.etaS ? ` · ${minutesLeft(row.etaS)}` : ""}`
      : row.status === "paused" ? t.rowStatus.paused : null;
  const showTwo = failed || !!second;
  const mark = failed ? <StatusDot tone="danger" size={8} /> : done ? <StatusDot tone="success" size={8} /> : live ? <StatusDot tone="online" size={8} className="shadow-[0_0_0_3px_var(--ds-bg-success-soft)]" /> : <Ring />;
  const right = noSize
    ? <span className="w-[170px] shrink-0 text-right text-ds-xs text-ds-text-2">{done ? t.rowStatus.done : live ? t.installing : row.status === "paused" ? t.rowStatus.paused : failed ? "" : t.rowStatus.queued}</span>
    : done
      ? <span className="w-[170px] shrink-0 text-right text-ds-xs text-ds-text-2 ds-num">{t.rowStatus.done}</span>
      : failed
        ? <span className="w-[170px] shrink-0" />
        : row.status === "queued" && percent === 0
          ? <span className="w-[170px] shrink-0 text-right text-ds-xs text-ds-text-2">{t.rowStatus.queued}</span>
          : <ProgressBar value={percent} label={`${row.label} progress`} maxTrack={120} tone="accent" className="w-[170px] shrink-0 justify-end" />;
  return (
    <li className={cx("flex flex-col justify-center rounded-ds-md px-3 transition-colors duration-[120ms] hover:bg-ds-hover", showTwo ? "min-h-16 py-2" : "min-h-12 py-2")}>
      <div className="flex items-center gap-4">
        <span className="grid size-2 shrink-0 place-items-center">{mark}</span>
        <span className="w-[200px] shrink-0 truncate text-ds-sm font-medium">{row.label}</span>
        <span className={cx("min-w-0 flex-1 truncate text-ds-xs text-ds-muted", row.note ? "" : "font-mono")}>{row.note ?? row.file ?? ""}</span>
        <span className="w-[84px] shrink-0 text-right text-ds-xs ds-num">{sizeText(row)}</span>
        {right}
      </div>
      {showTwo && (
        <div className="mt-1 flex items-center gap-4 pl-6">
          {failed
            ? (
              <>
                <span className="min-w-0 flex-1 text-ds-2xs text-ds-danger"><span className="font-mono">{row.error?.code}</span> {row.error?.message}</span>
                <Button variant="ghost" size="sm" leadingIcon="restore" onClick={onRetry}>{t.retry}</Button>
              </>
            )
            : <span className="text-ds-2xs text-ds-text-2 ds-num">{second}</span>}
        </div>
      )}
    </li>
  );
}
