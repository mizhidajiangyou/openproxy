/* ==========================================================================
 * 渠道：上游可达性、实时速率、每日成功/失败分布、失败原因拆解
 *
 * 这一页回答运维问题：**现在能不能用？**以及**不能用的时候为什么？**
 * 所以「探测」按钮在这里是最重要的控件，而不是一个附带功能。
 * ========================================================================== */

import { api } from '../api.js';
import { barChart, legend } from '../charts.js';
import { ago, bindOnce, compact, duration, esc, int, percent, toast } from '../ui.js';

const ERROR_LABELS = {
  client_disconnect: '客户端断开',
  upstream_timeout: '上游超时',
  upstream_unreachable: '上游不可达',
  upstream_status: '上游返回错误',
  request_too_large: '请求体过大',
  bad_request: '请求不合法',
  not_json: '请求体不是 JSON',
  auth_failed: '鉴权失败',
  quota_exceeded: '超出配额',
  model_not_allowed: '模型不在清单',
  internal: '站内错误',
};

function healthBanner(probe, summary) {
  if (!probe) {
    return `<div class="notice">
      <div>还没有探测过上游。点右上角「立即探测」发一次 <code>GET /v1/models</code> ——
      这是幂等请求，失败会自动重试一次。</div>
    </div>`;
  }
  if (probe.ok) {
    const rate = summary.requests ? summary.errors / summary.requests : 0;
    return `<div class="notice ${rate > 0.1 ? 'notice-warn' : 'notice-ok'}">
      <div>上游<b>可达</b>，往返 <b>${esc(int(probe.latency_ms))}ms</b>，
      探测于 ${esc(ago(probe.checked_at))}。窗口内失败率
      <b>${esc(percent(rate))}</b>。</div>
    </div>`;
  }
  return `<div class="notice notice-danger">
    <div><b>上游不可达</b>：${esc(probe.detail || '未知原因')}，
    探测于 ${esc(ago(probe.checked_at))}。转发请求会得到 <code>502</code>。</div>
  </div>`;
}

function errorTable(kinds) {
  const entries = Object.entries(kinds || {});
  if (!entries.length) {
    return `<div class="empty" style="padding:32px 12px">
      <div class="empty-title">窗口内没有失败</div>
      <div class="empty-hint">一切正常</div>
    </div>`;
  }
  const max = Math.max(...entries.map(([, count]) => count));
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>失败原因</th><th class="num">次数</th><th style="width:44%">占比</th></tr></thead>
        <tbody>
          ${entries
            .map(
              ([kind, count]) => `
            <tr>
              <td>${esc(ERROR_LABELS[kind] || kind)}</td>
              <td class="num">${esc(int(count))}</td>
              <td>
                <div class="gauge" style="--accent:var(--series-1)">
                  <div class="gauge-fill" style="width:${(count / max) * 100}%"></div>
                </div>
              </td>
            </tr>`
            )
            .join('')}
        </tbody>
      </table>
    </div>`;
}

export async function renderChannel(view) {
  const data = await api.channel({ days: 14 });
  const summary = data.summary || {};
  const statusTrend = data.status_trend || [];

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="page-heading">渠道</h1>
        <p class="page-lede">
          出站一律覆写 <code>User-Agent</code> —— 上游的 Cloudflare 会以
          <code>403 error code: 1010</code> 拒绝 <code>Python-urllib/*</code> 与缺失 UA 的请求，
          透传客户端 UA 会让一部分合法客户端直接挂掉。
        </p>
      </div>
      <button class="btn btn-primary btn-sm" type="button" data-act="probe">立即探测</button>
    </div>

    <div style="margin-bottom:16px">${healthBanner(data.probe, summary)}</div>

    <div class="grid grid-stats">
      <div class="stat" style="--accent:var(--series-1)">
        <div class="stat-label">上游地址</div>
        <div class="stat-value mono" style="font-size:15px;word-break:break-all">
          ${esc(data.upstream_base || '-')}
        </div>
        <div class="stat-meta">出站 UA ${esc('<已强制覆写>')}</div>
      </div>
      <div class="stat" style="--accent:var(--series-2)">
        <div class="stat-label">实时速率</div>
        <div class="stat-value">${esc(int(summary.rpm))}<span class="stat-unit">次/分</span></div>
        <div class="stat-meta">${esc(compact(summary.tpm))} Token/分（滚动 1 分钟）</div>
      </div>
      <div class="stat" style="--accent:var(--series-3)">
        <div class="stat-label">窗口请求</div>
        <div class="stat-value">${esc(int(summary.requests))}</div>
        <div class="stat-meta">近 ${esc(int(data.window_days))} 天</div>
      </div>
      <div class="stat" style="--accent:var(--series-4)">
        <div class="stat-label">平均耗时</div>
        <div class="stat-value">${esc(duration(summary.avg_latency_ms))}</div>
        <div class="stat-meta">失败 ${esc(int(summary.errors))} 次</div>
      </div>
    </div>

    <section class="card card-framed" style="margin-top:16px">
      <div class="card-head">
        <div>
          <div class="card-title">成功 / 失败</div>
          <div class="faint" style="font-size:12px">按本地自然日；缺失的日期补 0</div>
        </div>
        <div class="chart-legend">
          ${legend([{ label: '成功', color: 'var(--series-5)' },
                    { label: '失败', color: 'var(--series-1)' }])}
        </div>
      </div>
      <div class="card-body">
        ${barChart({
          labels: statusTrend.map((p) => p.bucket.slice(5)),
          series: [
            { name: '成功', values: statusTrend.map((p) => p.ok), color: 'var(--series-5)' },
            { name: '失败', values: statusTrend.map((p) => p.error), color: 'var(--series-1)' },
          ],
          width: 900, height: 220,
          formatValue: (v) => int(v),
          label: '每日成功与失败次数',
        })}
      </div>
    </section>

    <div class="grid grid-2" style="margin-top:16px">
      <section class="card card-framed">
        <div class="card-head"><div class="card-title">失败原因拆解</div></div>
        <div class="card-body card-body-flush">${errorTable(data.error_kinds)}</div>
      </section>

      <section class="card card-framed">
        <div class="card-head"><div class="card-title">这一页的几个已知边界</div></div>
        <div class="card-body">
          <ul style="margin:0;padding-left:20px;color:var(--ink-soft)">
            <li><b>不自动重试</b>：<code>/v1/chat/completions</code> 是有副作用的 POST，
                重试会白白消耗上游额度。只有幂等的 <code>/v1/models</code> 探测重试一次。</li>
            <li><b>上游非 2xx 原样透传</b>：包括未知模型返回的
                <code>401 ModelError</code>。开启「仅转发免费模型」时本站会先拦成
                <code>400</code>，避免客户端误判成密钥问题。</li>
            <li><b>统计是尽力而为</b>：写入走有界队列，队列满时丢弃并计数，
                优雅退出会排空。<code>SIGKILL</code> 会丢掉队列里的几条。</li>
            <li><b>客户端 IP 取自 TCP 连接</b>，不读 <code>X-Forwarded-For</code> ——
                本站默认绑 127.0.0.1，那个头是本机客户端自报的。</li>
          </ul>
        </div>
      </section>
    </div>`;

  const onClick = async (event) => {
    const node = event.target.closest('[data-act="probe"]');
    if (!node) return;
    node.disabled = true;
    try {
      const result = await api.probe();
      toast(result.ok ? `上游可达（${result.latency_ms}ms）` : `探测失败：${result.detail}`,
        result.ok ? 'ok' : 'danger', 3600);
      await renderChannel(view);
    } catch (cause) {
      toast(cause.message, 'danger', 3600);
    } finally {
      // 必须在 finally 里放开：放在 catch 里的话，catch 里再抛一次（toast 未定义
      // 之类）按钮就永久停在禁用态 —— 切走再回来才恢复，看起来像「功能坏了」。
      // 重画之后 node 已经不在文档里，这一句对它自己无害。
      node.disabled = false;
    }
  };
  bindOnce(view, 'click', onClick);
  return () => view.removeEventListener('click', onClick);
}