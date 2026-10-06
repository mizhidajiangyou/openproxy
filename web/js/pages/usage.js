/* ==========================================================================
 * 调用记录：过滤 + 分页 + 明细
 *
 * 分页状态放在 hash query 里（`#/usage?page=2&model=…`），所以「筛选后把链接
 * 发给同事」能复现同一屏；而 hash 变化会触发路由重渲染，因此读写都走
 * ``#/usage?...`` 这一条路径，不另开一套状态。
 * ========================================================================== */

import { api } from '../api.js';
import { ago, bindOnce, bytes, debounce, duration, esc, int, stamp } from '../ui.js';

function parseQuery() {
  const raw = location.hash.split('?')[1] || '';
  const params = new URLSearchParams(raw);
  const num = (key, fallback) => {
    const value = Number(params.get(key));
    return Number.isFinite(value) && value > 0 ? value : fallback;
  };
  return {
    page: num('page', 1),
    page_size: num('size', 20),
    model: params.get('model') || '',
    status: params.get('status') || '',
    search: params.get('q') || '',
    anonymous_only: params.get('anon') === '1',
  };
}

/** 与后端 ``search: Query("", max_length=200)`` 必须一致。
 *
 * 少了它：粘一段 201 字 → 防抖 → ``GET /api/admin/usage?search=<201字>`` 回 422
 * → 整个 ``#view`` 被换成「页面载入失败」，输入框连同用户正在敲的内容一起消失，
 * 而界面上**没有任何途径**清掉这个条件 —— 别人分享来的一个长链接就能让这一屏
 * 永久打不开。（后端那条边界早有测试，前端漏了遵守契约的这一半。）
 */
const SEARCH_MAX = 200;

function writeQuery(next) {
  const merged = { ...parseQuery(), ...next };
  if (typeof merged.search === 'string' && merged.search.length > SEARCH_MAX) {
    merged.search = merged.search.slice(0, SEARCH_MAX);
  }
  const params = new URLSearchParams();
  if (merged.page > 1) params.set('page', String(merged.page));
  if (merged.page_size !== 20) params.set('size', String(merged.page_size));
  if (merged.model) params.set('model', merged.model);
  if (merged.status) params.set('status', merged.status);
  if (merged.search) params.set('q', merged.search);
  if (merged.anonymous_only) params.set('anon', '1');
  location.hash = `#/usage${params.toString() ? `?${params}` : ''}`;
}

const ERROR_LABELS = {
  client_disconnect: '客户端断开',
  upstream_timeout: '上游超时',
  upstream_unreachable: '上游不可达',
  upstream_status: '上游报错',
  request_too_large: '请求过大',
  bad_request: '请求不合法',
  not_json: '请求体不是 JSON',
  auth_failed: '鉴权失败',
  quota_exceeded: '超出配额',
  model_not_allowed: '模型不在清单',
  internal: '站内错误',
};

function errorLabel(kind) {
  return ERROR_LABELS[kind] || kind || '成功';
}

function rowClass(row) {
  return row.ok ? '' : ' style="background:var(--danger-bg)"';
}

function rows(items) {
  if (!items.length) {
    return `<div class="empty">
      <div class="empty-title">没有符合条件的记录</div>
      <div class="empty-hint">试着放宽筛选条件，或换一个时间范围</div>
    </div>`;
  }
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr>
          <th>时间</th><th>模型</th><th>客户端</th><th>方式</th>
          <th class="num">入</th><th class="num">出</th><th class="num">合计</th>
          <th class="num">耗时</th><th class="num">下行</th><th>结果</th>
        </tr></thead>
        <tbody>
          ${items
            .map(
              (row) => `
            <tr${rowClass(row)}>
              <td class="nowrap muted">${esc(stamp(row.ts))}</td>
              <td class="mono truncate" style="max-width:180px" title="${esc(row.model)}">${esc(row.model)}</td>
              <td class="truncate" style="max-width:130px" title="${esc(row.key_label || '')}">
                ${row.anonymous
                  ? '<span class="tag">未署名</span>'
                  : esc(row.key_label || '已删除的密钥')}
              </td>
              <td>${row.stream ? '<span class="tag tag-info">流式</span>' : '<span class="tag">普通</span>'}</td>
              <td class="num">${row.usage_known ? esc(int(row.prompt_tokens)) : '<span class="faint">—</span>'}</td>
              <td class="num">${row.usage_known ? esc(int(row.completion_tokens)) : '<span class="faint">—</span>'}</td>
              <td class="num strong">${row.usage_known ? esc(int(row.total_tokens)) : '<span class="faint">未知</span>'}</td>
              <td class="num muted">${esc(duration(row.latency_ms))}</td>
              <td class="num muted">${esc(bytes(row.bytes_out))}</td>
              <td class="nowrap">
                ${row.ok
                  ? '<span class="tag tag-ok">成功</span>'
                  : `<span class="tag tag-danger" title="${esc(row.error_kind)}">${esc(row.status)} ${esc(errorLabel(row.error_kind))}</span>`}
              </td>
            </tr>`
            )
            .join('')}
        </tbody>
      </table>
    </div>`;
}

function pager(payload) {
  const page = payload.page;
  const pages = payload.pages;
  // 越界时（别人发来的链接、清理后页数变少）不要打印「第 19961–585 条」这种
  // 自相矛盾的区间 —— 直接说没数据，并把页码夹回最后一页。
  const current = Math.min(page, pages);
  const from = payload.total === 0 || current < pages ? 0 : (current - 1) * payload.page_size + 1;
  const to = payload.total === 0 ? 0 : Math.min(payload.total, current * payload.page_size);
  return `
    <div class="pager">
      <div>第 <span class="num">${esc(int(from))}</span>–<span class="num">${esc(int(to))}</span> 条 ·
        共 <span class="num">${esc(int(payload.total))}</span> 条</div>
      <div class="pager-buttons">
        <button class="btn btn-sm" type="button" data-act="page" data-page="${current - 1}"
          ${current <= 1 ? 'disabled' : ''}>上一页</button>
        <span class="pager-page">${esc(current)} / ${esc(pages)}</span>
        <button class="btn btn-sm" type="button" data-act="page" data-page="${current + 1}"
          ${current >= pages ? 'disabled' : ''}>下一页</button>
      </div>
    </div>`;
}

/* 搜索框的焦点问题：**每次输入都会改 hash → hashchange → 整块 view.innerHTML
 * 被换掉**，正在输入的那个框随之消失，焦点掉回 body。
 *
 * 试过「去掉防抖」，结果更糟：一次输入触发一次异步重绘（还要等一次接口），
 * 而用户已经敲下一个字符进了旧的（已脱离文档的）input，于是只剩第一个字母。
 *
 * 正确的组合是**防抖 + 焦点还原**：防抖把连续输入压成一次重绘；重绘之后把
 * 焦点与光标位置还回同一个控件。两样缺一不可。
 *
 * 而这两份状态必须放在**模块作用域**，不能放在 renderUsage() 的函数体里：
 * 每改一次 hash，app.js 都会重新调用一次 renderUsage()（先跑上一页的 cleanup），
 * 函数体里的闭包连同 focusMemory 一起被丢掉 —— 新一次渲染里 act 恒为 ''，
 * 于是「记下了却没人还」的焦点永远还不回去。模块作用域在多次调用之间是同一份。
 */
const focusMemory = { act: '', start: 0, end: 0, hash: '' };

/** 停手 320ms 之后才把关键词写进 hash，从而只触发一次重绘。 */
const onSearchSettled = debounce((node) => {
  focusMemory.act = node.dataset.act || 'search';
  focusMemory.start = node.selectionStart ?? node.value.length;
  focusMemory.end = node.selectionEnd ?? node.value.length;
  writeQuery({ search: node.value, page: 1 });
  // 记住**将要渲染的那一屏**长什么样：还原焦点时若 hash 已经不是它，
  // 说明这一次重绘另有原因（比如顺手改了模型下拉），就别抢焦点了
  focusMemory.hash = location.hash;
}, 320);

export async function renderUsage(view) {
  const query = parseQuery();
  const data = await api.usage(query);

  const modelOptions = ['<option value="">全部模型</option>']
    .concat(
      (data.models || []).map(
        (m) =>
          `<option value="${esc(m)}"${m === query.model ? ' selected' : ''}>${esc(m)}</option>`
      )
    )
    .join('');

  view.innerHTML = `
    <h1 class="sr-only">调用记录 —— 每一次转发都留痕</h1>
    <section class="card card-framed">
      <div class="card-head">
        <div class="card-title">筛选</div>
        <button class="btn btn-sm" type="button" data-act="reset">重置</button>
      </div>
      <div class="card-body">
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
          <div class="field">
            <label for="fModel">模型</label>
            <select class="select" id="fModel" data-act="model">${modelOptions}</select>
          </div>
          <div class="field">
            <label for="fStatus">结果</label>
            <select class="select" id="fStatus" data-act="status">
              <option value=""${query.status === '' ? ' selected' : ''}>全部</option>
              <option value="ok"${query.status === 'ok' ? ' selected' : ''}>仅成功</option>
              <option value="error"${query.status === 'error' ? ' selected' : ''}>仅失败</option>
            </select>
          </div>
          <div class="field">
            <label for="fSearch">关键词</label>
            <input class="input" id="fSearch" data-act="search" placeholder="模型 / 路径 / 密钥名"
              maxlength="${SEARCH_MAX}" value="${esc(query.search)}" />
          </div>
          <div class="field">
            <label for="fSize">每页</label>
            <select class="select" id="fSize" data-act="size">
              ${[20, 50, 100, 200]
                .map(
                  (n) =>
                    `<option value="${n}"${n === query.page_size ? ' selected' : ''}>${n} 条</option>`
                )
                .join('')}
            </select>
          </div>
        </div>
        <div class="row gap-3 wrap" style="margin-top:12px">
          <label class="switch">
            <input type="checkbox" data-act="anon" ${query.anonymous_only ? 'checked' : ''} />
            <span class="switch-track"></span>
            <span>只看未署名调用</span>
          </label>
          <span class="faint" style="font-size:12px">
            关键词只匹配模型名、路径、错误类别与密钥名 —— 表格只存计数，从不存提示词与回答
          </span>
        </div>
      </div>
    </section>

    <section class="card" style="margin-top:16px">
      <div class="card-head">
        <div class="card-title">调用明细</div>
        <span class="faint" style="font-size:12px">最近更新 ${esc(ago(data.items[0]?.ts || 0))}</span>
      </div>
      <div class="card-body card-body-flush">
        ${rows(data.items || [])}
        ${pager(data)}
      </div>
    </section>`;

  const onClick = (event) => {
    const node = event.target.closest('[data-act]');
    if (!node) return;
    const act = node.dataset.act;
    if (act === 'page') writeQuery({ page: Number(node.dataset.page) });
    if (act === 'reset') location.hash = '#/usage';
  };
  const onChange = (event) => {
    const act = event.target.dataset.act;
    if (act === 'model') writeQuery({ model: event.target.value, page: 1 });
    if (act === 'status') writeQuery({ status: event.target.value, page: 1 });
    if (act === 'size') writeQuery({ page_size: Number(event.target.value), page: 1 });
    if (act === 'anon') writeQuery({ anonymous_only: event.target.checked, page: 1 });
  };
  const onInput = (event) => {
    const act = event.target.dataset.act;
    if (act !== 'search') return;
    // 防抖在 ui.js 里：每次调用都先 clearTimeout，少了那一步就会退化成
    // 「每 320ms 重绘一次」—— 输入框被反复销毁，光标永远追不上手速
    onSearchSettled(event.target);
  };

  bindOnce(view, 'click', onClick);
  bindOnce(view, 'change', onChange);
  bindOnce(view, 'input', onInput);

  // 重绘**完成**之后把焦点还给刚才那个控件（监听 app.js 派的 rendered 事件 ——
  // 紧随 innerHTML 赋值的微任务跑得太早，那时页面内容还不存在）
  const restoreFocus = () => {
    const memory = { ...focusMemory };
    focusMemory.act = '';
    if (!memory.act) return;
    if (memory.hash && memory.hash !== location.hash) return;
    const node = view.querySelector(`[data-act="${memory.act}"]`);
    if (!node) return;
    node.focus();
    if (typeof node.setSelectionRange === 'function') {
      const max = (node.value || '').length;
      node.setSelectionRange(Math.min(memory.start, max), Math.min(memory.end, max));
    }
  };
  bindOnce(view, 'rendered', restoreFocus);

  return () => {
    onSearchSettled.cancel();
    view.removeEventListener('click', onClick);
    view.removeEventListener('change', onChange);
    view.removeEventListener('input', onInput);
    view.removeEventListener('rendered', restoreFocus);
  };
}