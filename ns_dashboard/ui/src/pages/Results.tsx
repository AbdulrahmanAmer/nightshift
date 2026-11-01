import { ArrowDown, ArrowUp } from "lucide-react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtNum, fmtWhen, POOL_LABELS } from "../lib/format";
import type { Learn, LearnRow } from "../lib/types";
import { Card, CardHead, cx, Dropzone, Empty, KindTag, PageHeader } from "../ui/kit";
import { ViewsChart } from "../visuals/charts";

const optionLabel = (pool: string, opt: string) => POOL_LABELS[pool]?.opts?.[opt] || (pool === "length_buckets" ? `${opt}-second Shorts` : opt.replace(/_/g, " "));
const poolLabel = (pool: string) => POOL_LABELS[pool]?.label || (pool === "length_buckets" ? "Short length" : pool.replace(/_/g, " "));

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
  const drop = <Dropzone title="Drop your YouTube Studio analytics export" sub="In YouTube Studio open Analytics, then Content, then Export current view as CSV. Do it once for Shorts and once for Videos" accept=".csv,text/csv" onFile={send} />;

  if (!sum.loading && !s) return <div className="enter flex flex-col gap-5"><PageHeader title="Results" sub="How your videos are doing, and what Nightshift will change" /><Empty title="No results yet">After your first videos have been up for a week, export your analytics and drop the file below</Empty>{drop}</div>;
  if (!s) return null;
  const m = s.milestone; const go = m ? m.current >= m.target && m.weeks >= m.weeks_needed : false;
  const maxViews = Math.max(1, ...rows.map((r) => r.views));
  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader title="Results" sub={<>From the YouTube Studio export imported <span className="font-mono num">{fmtWhen(s.imported_at)}</span>{s.matched != null ? `. ${s.matched} videos matched, ${s.unmatched} not matched` : ""}</>} />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-3">
        <Figure label="Views, last 14 days" value={total ? fmtNum(total) : "–"} />
        <Figure label="From Shorts" value={shortsShare != null ? `${shortsShare}%` : "–"} />
        <Figure label="Watched to the end, on average" value={avgWatched != null ? `${avgWatched}%` : "–"} />
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
        <Card className="p-5">
          <CardHead title="Views over the last 14 days" right={<div className="flex items-center gap-4 text-[12px] text-text-2"><span className="flex items-center gap-1.5"><span className="h-0.5 w-4 rounded-full bg-accent" />Shorts</span><span className="flex items-center gap-1.5"><span className="h-0.5 w-4 rounded-full bg-text" />Full videos</span></div>} />
          <div className="mt-4">{days.length ? <ViewsChart days={days} height={300} /> : <p className="py-12 text-center text-[13px] text-muted">Daily views appear after the next import</p>}</div>
        </Card>
        {m && (
          <Card className="flex flex-col p-5">
            <div className="flex items-start justify-between gap-3"><div><p className="text-[12px] text-muted">Next step</p><h2 className="mt-0.5 text-[17px] leading-6 font-medium">{m.label}</h2></div>
              <span className={cx("inline-flex h-8 shrink-0 items-center rounded-full px-3.5 text-[13px] font-medium", go ? "bg-ok-soft text-ok" : "bg-accent-soft text-accent-ink")}>{go ? "Go" : "Not yet"}</span></div>
            <p className="mt-2 text-[13px] text-text-2">{m.rule}</p>
            <div className="mt-auto pt-6">
              <div className="flex items-baseline gap-2"><span className="font-mono text-[40px] leading-none num">{m.current}%</span><span className="text-[13px] text-muted">watched now, {m.target}% needed</span></div>
              <div className="mt-5 flex gap-2" aria-label={`${m.weeks} of ${m.weeks_needed} weeks passed`}>{Array.from({ length: m.weeks_needed }, (_, i) => <div key={i} className="flex-1"><div className={cx("h-2 rounded-full", i < m.weeks ? "bg-fill" : "bg-sunken")} /><p className="mt-1.5 text-[11px] text-muted">Week {i + 1}</p></div>)}</div>
              <p className="mt-4 text-[13px] text-text-2">{go ? "Nightshift will raise it at the next run" : `${m.weeks_needed - m.weeks} more week${m.weeks_needed - m.weeks === 1 ? "" : "s"} at this level and it goes ahead on its own`}</p>
            </div>
          </Card>
        )}
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card className="p-5">
          <CardHead title="Watched to the end" sub="Per video, most watched first" />
          <ul className="mt-4 flex flex-col gap-3.5">
            {[...rows].sort((a, b) => b.viewed_pct - a.viewed_pct).map((r) => (
              <li key={r.title}>
                <div className="flex items-baseline justify-between gap-3 text-[13px]"><span className="truncate">{r.title}</span><span className="font-mono num">{r.viewed_pct}%</span></div>
                <div className="mt-1.5 h-1.5 rounded-full bg-sunken"><div className={cx("h-full rounded-full", r.viewed_pct >= 55 ? "bg-fill" : "bg-bad")} style={{ width: `${r.viewed_pct}%` }} /></div>
              </li>
            ))}
          </ul>
          <p className="mt-4 text-[12px] text-muted">Below 55% counts against the style that made it</p>
        </Card>
        <Card className="overflow-hidden">
          <div className="p-5 pb-3"><CardHead title="Views per video" /></div>
          <table className="w-full text-[13px]">
            <thead className="text-[12px] text-muted"><tr className="border-y border-line"><th className="px-5 py-2 text-left font-normal">Video</th><th className="px-5 py-2 text-right font-normal">Views</th></tr></thead>
            <tbody>{[...rows].sort((a, b) => b.views - a.views).map((r) => (
              <tr key={r.title} className="border-b border-line last:border-0"><td className="px-5 py-2.5"><div className="flex min-w-0 flex-col"><span className="truncate">{r.title}</span>{r.kind && <KindTag kind={r.kind} className="text-muted" />}</div></td>
                <td className="px-5 py-2.5 text-right"><div className="flex items-center justify-end gap-3"><span className="hidden h-1 w-20 overflow-hidden rounded-full bg-sunken sm:block"><span className="block h-full rounded-full bg-text-2" style={{ width: `${(r.views / maxViews) * 100}%` }} /></span><span className="w-14 font-mono num">{fmtNum(r.views)}</span></div></td></tr>
            ))}</tbody>
          </table>
        </Card>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card className="p-5">
          <CardHead title="What Nightshift learned" />
          <ul className="mt-4 flex flex-col gap-3">{(s.summary || []).map((l) => <li key={l} className="flex gap-3 text-[15px] leading-6"><span className="mt-2.5 size-1.5 shrink-0 rounded-full bg-accent" />{l}</li>)}</ul>
          {!s.summary?.length && <p className="mt-3 text-[13px] text-muted">Nothing yet</p>}
        </Card>
        <Card className="p-5">
          <CardHead title="What changes next" sub="Applied from tonight's run" />
          <ul className="mt-3 flex flex-col divide-y divide-line">{(s.changes || []).map((c) => (
            <li key={c.pool + c.option} className="flex items-center justify-between gap-3 py-2.5"><div className="min-w-0"><p className="truncate text-[14px]">{optionLabel(c.pool, c.option)}</p><p className="text-[12px] text-muted">{poolLabel(c.pool)}</p></div>
              <span className={cx("inline-flex shrink-0 items-center gap-1 text-[13px]", c.delta >= 0 ? "text-ok" : "text-text-2")}>{c.delta >= 0 ? <ArrowUp size={14} strokeWidth={1.5} /> : <ArrowDown size={14} strokeWidth={1.5} />}{c.delta >= 0 ? "More often" : "Less often"}</span></li>
          ))}</ul>
          {!s.changes?.length && <p className="mt-3 text-[13px] text-muted">No changes yet</p>}
        </Card>
      </div>
      {drop}
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return <Card className="p-5"><p className="text-[13px] text-text-2">{label}</p><p className="mt-2 font-mono text-[30px] leading-9 num">{value}</p></Card>;
}
