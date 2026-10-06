/* ==========================================================================
 * 总览：统计卡 + Token 趋势 + 模型分布 + 最近调用
 *
 * 所有数字都直接来自后端 /overview，**前端不做二次聚合**。原因（R21）：
 * 一旦前端自己再算一遍「今日合计」，它和「调用记录」页的分页总数就可能对不上，
 * 而两个都「看起来对」的数字互相矛盾是最难查的一类问题。
 * ========================================================================== */

import { api } from '../api.js';
import { donutChart, lineChart, legend, sparkline } from '../charts.js';
import { ago, compact, duration, esc, int, percent, stamp } from '../ui.js';

const SERIES = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)',
  'var(--series-5)', 'var(--series-6)'];

function statCard(spec) {
  const { label, value, unit = '', meta = '', accent = 'var(--series-1)', spark = null } = spec;
  return `
    <div class="stat" style="--accent:${esc(accent)}">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value">${esc(value)}${unit ? `<span class="stat-unit">${esc(unit)}</span>` : ''}</div>
      ${meta ? `<div class="stat-meta">${meta}</div>` : ''}
      ${spark ? `<div class="stat-spark">${spark}</div>` : ''}
    </div>`;
}

function statusTag(row) {
  if (row.ok) return '<span class="tag tag-ok">成功</span>';
  return `<span class="tag tag-danger">${esc(row.status)}</span>`;
}

function recentRows(items) {
  if (!items.length) {
    return `<div class="empty">
      <div class="empty-mark">${emptyMark()}</div>
      <div class="empty-title">还没有任何调用</div>
      <div class="empty-hint">把客户端的接口地址指向本页的 <code>/v1</code> 即可开始</div>
    </div>`;
  }
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr>
          <th>时间</th><th>模型</th><th>客户端</th><th class="num">Token</th>
          <th class="num">耗时</th><th>结果</th>
        </tr></thead>
        <tbody>
          ${items
            .map(
              (row) => `
            <tr>
              <td class="nowrap muted">${esc(stamp(row.ts))}</td>
              <td class="mono truncate" style="max-width:200px">${esc(row.model)}</td>
              <td class="truncate" style="max-width:140px">${esc(row.key_label || '未署名')}</td>
              <td class="num">${row.usage_known ? esc(int(row.total_tokens)) : '<span class="faint">未知</span>'}</td>
              <td class="num muted">${esc(duration(row.latency_ms))}</td>
              <td>${statusTag(row)}</td>
            </tr>`
            )
            .join('')}
        </tbody>
      </table>
    </div>`;
}

function emptyMark() {
  return (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2">' +
    '<rect x="3" y="5" width="18" height="14" rx="1"/>' +
    '<path d="M3 10h18M8 5v14"/></svg>'
  );
}

export async function renderOverview(view) {
  const data = await api.overview({ trend_days: 14 });
  const trend = data.trend || [];
  const models = data.models || [];
  const recent = data.recent || [];
  const today = data.today || {};
  const week = data.week || {};
  const total = data.total || {};

  const totalSeries = trend.map((p) => p.total_tokens);
  const reqSeries = trend.map((p) => p.requests);

  const donutItems = models.slice(0, 6).map((m, i) => ({
    label: m.model,
    value: m.total_tokens,
    color: SERIES[i % SERIES.length],
  }));

  const unknownNotice =
    (total.unknown_usage || 0) > 0
      ? `<div class="notice notice-warn">
           <div>有 <b>${esc(int(total.unknown_usage))}</b> 次调用没拿到上游用量，
           总计里的 Token 因此偏低。这是统计口径的一部分，不是错误。</div>
         </div>`
      : '';

  view.innerHTML = `
    <h1 class="sr-only">总览 —— 中转站实时概览</h1>
    ${unknownNotice}
    <div class="grid grid-stats" style="margin-top:${unknownNotice ? '16px' : '0'}">
      ${statCard({
        label: '今日请求', value: int(today.requests), accent: SERIES[0],
        meta: `<span>本周 ${esc(int(week.requests))}</span>`,
        spark: sparkline(reqSeries, { color: SERIES[0], height: 28 }),
      })}
      ${statCard({
        label: '今日 Token', value: compact(today.total_tokens), accent: SERIES[1],
        meta: `<span>入 ${esc(compact(today.prompt_tokens))} · 出 ${esc(compact(today.completion_tokens))}</span>`,
        spark: sparkline(totalSeries, { color: SERIES[1], height: 28 }),
      })}
      ${statCard({
        label: '平均耗时', value: duration(today.avg_latency_ms), accent: SERIES[2],
        meta: `<span>实时 ${esc(int(total.rpm))} 次/分 · ${esc(compact(total.tpm))} Token/分</span>`,
      })}
      ${statCard({
        label: '累计 Token', value: compact(total.total_tokens), accent: SERIES[3],
        meta: `<span>累计 ${esc(int(total.requests))} 次调用</span>`,
      })}
      ${statCard({
        label: '失败率', value: percent(total.error_rate), accent: SERIES[4],
        meta: `<span>${esc(int(total.errors))} 次未成功</span>`,
      })}
      ${statCard({
        label: '缓存命中', value: percent(today.total_tokens ? today.cached_tokens / today.total_tokens : 0),
        accent: SERIES[5],
        meta: `<span>${esc(compact(today.cached_tokens))} Token 来自缓存</span>`,
      })}
    </div>

    <div class="grid grid-2" style="margin-top:16px">
      <section class="card card-framed">
        <div class="card-head">
          <div>
            <div class="card-title">Token 走势</div>
            <div class="faint" style="font-size:12px">最近 14 天 · 缺失的日期按 0 补齐</div>
          </div>
          <span class="tag">共 ${esc(int(total.total_tokens))} Token</span>
        </div>
        <div class="card-body">
          ${lineChart({
            labels: trend.map((p) => p.bucket.slice(5)),
            series: [
              { name: 'Token 总量', values: totalSeries, color: 'var(--series-1)' },
              { name: '请求数', values: reqSeries, color: 'var(--series-2)' },
            ],
            width: 700, height: 250,
            // 提示框里给出**完整数值**而不是轴上的缩写：轴上写「12.3K」是为了省地方，
            // 悬停是用户主动问「到底多少」，这里给 12,345
            formatValue: (v) => int(v),
            label: 'Token 与请求数走势',
          })}
          <div class="chart-legend">
            ${legend([{ label: 'Token 总量', color: 'var(--series-1)' },
                      { label: '请求数', color: 'var(--series-2)' }], 'line')}
          </div>
        </div>
      </section>

      <section class="card card-framed">
        <div class="card-head">
          <div class="card-title">今日模型分布</div>
          <span class="tag">${esc(int(models.length))} 个模型</span>
        </div>
        <div class="card-body">
          ${models.length
            ? `<div class="donut-wrap">
                 ${donutChart({
                   items: donutItems, size: 186,
                   centerValue: compact(today.total_tokens), centerLabel: '今日 Token',
                 })}
                 <div class="grow" style="min-width:180px">
                   <div class="chart-legend" style="flex-direction:column;gap:8px">
                     ${donutItems
                       .map(
                         (it) => `
                       <div class="chart-legend-item" style="width:100%">
                         <span class="chart-swatch" style="background:${esc(it.color)}"></span>
                         <span class="truncate grow">${esc(it.label)}</span>
                         <span class="num muted">${esc(compact(it.value))}</span>
                       </div>`
                       )
                       .join('')}
                   </div>
                 </div>
               </div>`
            : `<div class="chart-empty" style="height:180px;display:flex;align-items:center;justify-content:center">今日还没有调用</div>`}
        </div>
      </section>
    </div>

    <section class="card" style="margin-top:16px">
      <div class="card-head">
        <div class="card-title">最近调用</div>
        <span class="faint" style="font-size:12px">${esc(ago(recent[0]?.ts || 0))}</span>
      </div>
      <div class="card-body card-body-flush">${recentRows(recent)}</div>
    </section>`;

  return null;
}

/** 供单测导入的内部符号。 */
export const _internal = { SERIES, statCard, recentRows };