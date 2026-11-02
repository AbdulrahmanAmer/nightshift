/* Results: stat cards on top, the views chart as the one main chart with the go/no-go milestone beside it, then the
   published videos as a TaskList (watched to the end as the progress, views as the figure) next to a MeterGroup of
   the last seven days, what Nightshift learned and what changes, and the CSV drop zone. Same calls as before:
   GET /learn/summary, POST /learn/import. */
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtCompact, fmtNum, fmtWhen, kindLabel, POOL_LABELS } from "../lib/format";
import type { Learn, LearnRow } from "../lib/types";
import { cx } from "../ui/kit";
import { ViewsChart } from "../visuals/charts";
import {
  Badge, Card, CardHeader, DashGrid, DropTile, EmptyState, Figure, Icon, MeterGroup, ProgressRing, SPAN, StatCard, TaskList,
  type Task,
} from "../components/ref12";

const optionLabel = (pool: string, opt: string) => POOL_LABELS[pool]?.opts?.[opt] || (pool === "length_buckets" ? `${opt}-second Shorts` : opt.replace(/_/g, " "));
const poolLabel = (pool: string) => POOL_LABELS[pool]?.label || (pool === "length_buckets" ? "Short length" : pool.replace(/_/g, " "));
const TARGET = 55;

export default function Results() {
  const { toast } = useApp();
  const sum = useLoad<Learn | null>(() => api.get("/learn/summary"), [], null);
  const s = sum.data;
  const send = async (f: File) => {
    const fd = new FormData(); fd.append("file", f, f.name);
    try { const r = await api.post("/learn/import", fd); sum.setData(r); toast(`Imported. ${r?.matched ?? 0} videos matched`, "ok"); } catch (e: any) { toast(e.message, "bad"); }
  };
  const rows: LearnRow[] = [...(s?.top || []), ...(s?.bottom || [])];
  const days = s?.daily || [];
  const total = days.reduce((a, d) => a + d.shorts + d.long, 0);
  const shortsShare = total ? Math.round((days.reduce((a, d) => a + d.shorts, 0) / total) * 100) : null;
  const avgWatched = rows.length ? Math.round(rows.reduce((a, r) => a + r.viewed_pct * r.views, 0) / Math.max(1, rows.reduce((a, r) => a + r.views, 0))) : null;
  const drop = <DropTile title="Drop your YouTube Studio analytics export" sub="In YouTube Studio open Analytics, then Content, then Export current view as CSV. Do it once for Shorts and once for Videos" accept=".csv,text/csv" onFile={send} />;

  if (!sum.loading && !s) return <div className="enter flex flex-col gap-5"><EmptyState icon="chart" title="No results yet">After your first videos have been up for a week, export your analytics and drop the file below</EmptyState>{drop}</div>;
  if (!s) return null;
  const m = s.milestone; const go = m ? m.current >= m.target && m.weeks >= m.weeks_needed : false;
  const half = Math.floor(days.length / 2);
  const firstHalf = days.slice(0, half).reduce((a, d) => a + d.shorts + d.long, 0); const secondHalf = days.slice(half).reduce((a, d) => a + d.shorts + d.long, 0);
  const growth = firstHalf ? Math.round(((secondHalf - firstHalf) / firstHalf) * 100) : null;
  const week = days.slice(-7); const weekMax = Math.max(1, ...week.map((d) => d.shorts + d.long));
  const published: Task[] = [...rows].sort((a, b) => b.views - a.views).map((r, i) => ({
    id: `${i}-${r.title}`, title: r.title, date: r.kind ? kindLabel(r.kind) : "Video", metricLabel: "Views", metric: fmtNum(r.views),
    progress: r.viewed_pct, progressLabel: `${r.title}, watched to the end`, tone: r.viewed_pct >= TARGET ? "success" : "danger", meta: [],
  }));

  return (
    <div className="enter flex flex-col gap-5">
      <p className="text-ds-md text-ds-text-2">From the YouTube Studio export imported <span className="ds-num">{fmtWhen(s.imported_at)}</span>{s.matched != null ? `. ${s.matched} videos matched, ${s.unmatched} not matched` : ""}</p>
      <DashGrid label="Results">
        <Figure className={cx("self-stretch", SPAN["2/4"])} label="Views, last 14 days" value={total ? fmtNum(total) : "–"} detail={growth != null ? `${growth >= 0 ? "+" : ""}${growth}% last 7 days vs the 7 before` : undefined} />
        <StatCard className={cx("self-stretch", SPAN["2/2"])} tone="accent" value={shortsShare ?? 0} eyebrow="Mix" title="Views from Shorts" caption={`${shortsShare ?? "–"}% Shorts, ${shortsShare != null ? 100 - shortsShare : "–"}% full videos`} detail="Last 14 days" />
        <StatCard className={cx("self-stretch", SPAN["2/2"])} tone={(avgWatched ?? 0) >= TARGET ? "success" : "danger"} value={avgWatched ?? 0} eyebrow="Retention" title="Watched to the end" caption={`Average, weighted by views`} detail={`Target ${TARGET}%`} />

        <Card as="section" aria-labelledby="r-views" className={SPAN["4/4"]}>
          <CardHeader id="r-views" title="Views over the last 14 days" actions={<div className="mr-2 flex items-center gap-4 text-ds-2xs text-ds-text-2"><span className="flex items-center gap-1.5"><span className="h-0.5 w-4 rounded-full bg-ds-accent" />Shorts</span><span className="flex items-center gap-1.5"><span className="h-0.5 w-4 rounded-full bg-ds-text" />Full videos</span></div>} />
          {days.length ? <ViewsChart days={days} height={300} /> : <p className="py-12 text-center text-ds-sm text-ds-muted">Daily views appear after the next import</p>}
        </Card>
        {m && (
          <Card as="section" aria-labelledby="r-next" className={cx("flex flex-col self-stretch", SPAN["2/4"])}>
            <CardHeader id="r-next" title="Next step" className="!mb-2" actions={<Badge tone={go ? "success" : "accent"} size="md" className="mr-2">{go ? "Go" : "Not yet"}</Badge>} />
            <h3 className="text-ds-md leading-ds-snug font-semibold">{m.label}</h3>
            <p className="mt-1 text-ds-sm text-ds-text-2">{m.rule}</p>
            <div className="mt-auto flex items-center gap-4 pt-6">
              <ProgressRing value={m.current} size={72} thickness={6} tone={m.current >= m.target ? "success" : "accent"} label="Watched now" />
              <p className="text-ds-sm text-ds-text-2">watched now, <span className="font-semibold text-ds-text ds-num">{m.target}%</span> needed</p>
            </div>
            <div className="mt-5 flex gap-2" role="group" aria-label={`${m.weeks} of ${m.weeks_needed} weeks passed`}>
              {Array.from({ length: m.weeks_needed }, (_, i) => <div key={i} className="flex-1"><div className={cx("h-2 rounded-full", i < m.weeks ? "bg-ds-accent" : "bg-ds-track shadow-ds-inset-sm")} /><p className="mt-1.5 text-ds-2xs text-ds-muted">Week {i + 1}</p></div>)}
            </div>
            <p className="mt-4 text-ds-xs text-ds-text-2">{go ? "Nightshift will raise it at the next run" : `${m.weeks_needed - m.weeks} more week${m.weeks_needed - m.weeks === 1 ? "" : "s"} at this level and it goes ahead on its own`}</p>
          </Card>
        )}

        <Card as="section" aria-labelledby="r-pub" className={cx("self-stretch", SPAN["4/4"])}>
          <CardHeader id="r-pub" title="Published videos" actions={<span className="mr-2 text-ds-2xs text-ds-muted">Bar = watched to the end. Below {TARGET}% counts against the style that made it</span>} />
          {published.length ? <TaskList tasks={published} /> : <EmptyState icon="film" title="No videos matched yet" />}
        </Card>
        <MeterGroup className={cx("self-stretch", SPAN["2/4"])} title="Views, last 7 days"
          meters={week.map((d) => { const v = d.shorts + d.long; return { label: new Date(d.date + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short" }), value: (v / weekMax) * 100, display: fmtCompact(v), source: `${fmtNum(v)} views on ${d.date}` }; })}
          caption="Each well is that day's views against the best day of the week" />

        <Card as="section" aria-labelledby="r-learn" className={cx("self-stretch", SPAN["3/4"])}>
          <CardHeader id="r-learn" title="What Nightshift learned" />
          <ul className="flex flex-col gap-3">{(s.summary || []).map((l) => <li key={l} className="flex gap-3 text-ds-md leading-ds-normal"><span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-ds-accent" />{l}</li>)}</ul>
          {!s.summary?.length && <p className="text-ds-sm text-ds-muted">Nothing yet</p>}
        </Card>
        <Card as="section" aria-labelledby="r-change" className={cx("self-stretch", SPAN["3/4"])}>
          <CardHeader id="r-change" title="What changes next" actions={<span className="mr-2 text-ds-2xs text-ds-muted">Applied from tonight's run</span>} />
          <ul className="flex flex-col">{(s.changes || []).map((c) => (
            <li key={c.pool + c.option} className="flex items-center justify-between gap-3 border-b border-ds-border-subtle py-3 last:border-0">
              <div className="min-w-0"><p className="truncate text-ds-sm font-semibold">{optionLabel(c.pool, c.option)}</p><p className="text-ds-2xs text-ds-muted">{poolLabel(c.pool)}</p></div>
              <span className={cx("inline-flex shrink-0 items-center gap-1 text-ds-xs font-semibold", c.delta >= 0 ? "text-ds-success-ink" : "text-ds-text-2")}><Icon name={c.delta >= 0 ? "arrowUp" : "arrowDown"} size={14} />{c.delta >= 0 ? "More often" : "Less often"}</span>
            </li>
          ))}</ul>
          {!s.changes?.length && <p className="text-ds-sm text-ds-muted">No changes yet</p>}
        </Card>
        <div className={SPAN["6/4"]}>{drop}</div>
      </DashGrid>
    </div>
  );
}
