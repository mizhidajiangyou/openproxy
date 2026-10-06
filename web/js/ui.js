/* ==========================================================================
 * 格式化与 DOM 辅助
 *
 * 全部是纯函数或无副作用的小工具，方便单测（tests/js 里直接 import 它们）。
 * ========================================================================== */

/* ------------------------------------------------------------- 格式化 --- */

const NUM = new Intl.NumberFormat('zh-CN');

/** 千分位整数。null/undefined/NaN 一律显示「-」而不是 0。 */
export function int(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  return NUM.format(Math.round(value));
}

/** 紧凑 token 数：26100000 → 26.1M。 */
export function compact(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  const n = value;
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${round(n / 1e9)}B`;
  if (abs >= 1e6) return `${round(n / 1e6)}M`;
  if (abs >= 1e4) return `${round(n / 1e3)}K`;
  return NUM.format(Math.round(n));
}

function round(n) {
  return Math.round(n * 10) / 10;
}

/** 延迟：860 → 860ms，1500 → 1.50s。 */
export function duration(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '-';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** 字节：1536 → 1.5 KB。 */
export function bytes(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return '-';
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/** 百分比。分母为 0 时给 0.0% 而不是 NaN%。 */
export function percent(fraction, digits = 1) {
  if (!Number.isFinite(fraction) || fraction === null || fraction === undefined) return '0.0%';
  return `${(fraction * 100).toFixed(digits)}%`;
}

/** epoch 毫秒 → 2026-10-03 20:15。 */
export function stamp(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '-';
  const p = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 2026-10-03 → 10-03（图上用，短）。 */
export function shortDay(label) {
  return String(label || '').slice(5);
}

/** 人类可读的相对时间。 */
export function ago(ts) {
  if (!ts) return '-';
  const diff = Date.now() - ts;
  if (diff < 0) return stamp(ts);
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return '刚刚';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  const day = Math.floor(hour / 24);
  if (day < 30) return `${day} 天前`;
  return stamp(ts);
}

/**
 * 客户端该填的 Base URL（`.../v1`）。
 *
 * **后端 `base_url_hint` 已经是绝对地址**（`routes_admin.py` 拼的是
 * `{scheme}://{host}:{port}/v1`），所以前端**不能再拼一次 `location.origin`** ——
 * 使用指南页与设置页的「复制」按钮都坏在这上面，页面上出现过两段地址粘在一起的
 * 废地址（截图里就是这个）。本文档里的示例也刻意不写成完整 URL 或裸主机名：
 * `test_no_external_urls_in_js` 会把 JS 里任何协议相对地址字面量当成外链资源。
 *
 * 唯一需要替换 host 的是**通配绑定**：host 为 `0.0.0.0` / `::` 时那个地址不可拨号
 * （把它填进客户端会连不上，回环上也不行），此时**协议、主机、端口三项全部**取自
 * `location` —— 只保留 hint 里的路径。协议尤其不能沿用 hint：反代在 443 上时
 * hint 说的是 http，而客户端必须用 https，否则一个 TLS 终止的入口会被降级。
 *
 * 纯函数（`loc` 由调用方传入）是为了能在 node 里直接测，见 tests/js/ui.test.mjs。
 */
export function clientBaseUrl(hint, loc) {
  const fallback = `${loc.origin}/v1`;
  const text = String(hint == null ? '' : hint).trim();
  if (!text) return fallback;
  let url;
  try {
    url = new URL(text);
  } catch {
    return fallback;
  }
  const host = url.hostname;
  const wildcard = host === '0.0.0.0' || host === '::' || host === '[::]';
  if (!wildcard) return text.replace(/\/+$/, '');
  // 手工拼而不是改 url.hostname：location.hostname 在 IPv6 下带方括号，
  // 交给 URL 的 host 解析器容易踩坑。
  const scheme = (loc.protocol || url.protocol).replace(':', '');
  // 反代在标准端口上时 location.port 是空的，别把内网端口漏出去。
  const reachable = loc.hostname || '127.0.0.1';
  const port = loc.port ? `:${loc.port}` : '';
  return `${scheme}://${reachable}${port}${url.pathname}`.replace(/\/+$/, '');
}

/* --------------------------------------------------------- DOM 辅助 --- */

export function esc(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function el(id) {
  return document.getElementById(id);
}

/**
 * 页面往**持久容器**（`#view`）上挂监听时统一走这里。
 *
 * 为什么不能直接 `view.addEventListener`：`#view` 这个 DOM 节点在整站生命周期里
 * **从不替换**（app.js 只改它的 innerHTML）。页面在自己的点击回调里再调一次
 * `renderXxx(view)` 重画时，新一批监听会挂上去，而**上一批的 cleanup 没人调用**
 * ——app.js 只保留 `route.render()` 最后一次返回的返回值。于是监听器只增不减：
 * 点第 3 次「探测上游」会发出 8 次请求，离页后还永久残留 7 个闭包。
 *
 * 同一元素同一事件类型**只保留最后一个** handler：重画的那个才是当前的。
 */
const boundHandlers = new WeakMap();

export function bindOnce(root, type, handler) {
  const list = boundHandlers.get(root) || [];
  const kept = [];
  for (const [t, h] of list) {
    if (t === type) root.removeEventListener(t, h);
    else kept.push([t, h]);
  }
  kept.push([type, handler]);
  boundHandlers.set(root, kept);
  root.addEventListener(type, handler);
  return () => {
    root.removeEventListener(type, handler);
    boundHandlers.set(root, (boundHandlers.get(root) || []).filter(([t, h]) => !(t === type && h === handler)));
  };
}

/** 摘掉某个元素上由 bindOnce 挂的全部监听（app.js 换页时调）。 */
export function unbindAll(root) {
  for (const [type, handler] of boundHandlers.get(root) || []) {
    root.removeEventListener(type, handler);
  }
  boundHandlers.set(root, []);
}

/** 事件委托：给容器绑一次，按 data-act 分发。 */
export function delegate(root, handlers) {
  root.addEventListener('click', (event) => {
    const target = event.target.closest('[data-act]');
    if (!target || !root.contains(target)) return;
    const handler = handlers[target.dataset.act];
    if (handler) handler(target, event);
  });
}

/**
 * 防抖。**每次调用都必须先 clearTimeout** —— 少了这一步它就退化成「每 ms 触发一次」，
 * 而不是「停手 ms 之后触发一次」：更早排上的定时器照样会带着当时的中间值跑掉。
 * 调用记录页的搜索框就是这么坏过一次：每 320ms 整页重画一次，输入框被反复销毁。
 *
 * 抽成纯函数就是为了能被 node 直接测（见 tests/js/ui.test.mjs）。
 */
export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, ms);
  };
  /** 页面卸载时必须调，否则定时器会往已卸载的 DOM 里写。 */
  wrapped.cancel = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
  wrapped.pending = () => timer !== null;
  return wrapped;
}

/** 复制到剪贴板，带降级：非安全上下文下 execCommand 兜底。 */
export async function copy(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 落到下面的兜底
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

/* --------------------------------------------------------------- 吐司 --- */

export function toast(message, kind = 'info', ms = 2600) {
  let host = el('toasts');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toasts';
    host.className = 'toasts';
    document.body.appendChild(host);
  }
  const node = document.createElement('div');
  node.className = `toast toast-${kind}`;
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => node.remove(), ms);
}

/* --------------------------------------------------------------- 图标 --- */

const ICONS = {
  overview: 'M3 12h4l2-7 4 14 2-7h6',
  keys: 'M15 7a4 4 0 1 0-4 4M11 11 4 18M7 15l2 2M9 13l2 2',
  usage: 'M4 19V5m0 14h16M8 15V9m4 6V7m4 8v-4',
  models: 'M12 3 3 8l9 5 9-5-9-5ZM3 13l9 5 9-5M3 18l9 5 9-5',
  channel: 'M4 12h4m3 0h4m3 0h2M6 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm6 0a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm6 0a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z',
  guide: 'M4 5h7v15H4zM13 5h7v15h-7zM6.5 9h2M15.5 9h2M6.5 13h2M15.5 13h2',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-4l-.3 2.6a7 7 0 0 0-1.7 1l-2.4-1-2 3.4L6 11a7 7 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 1.7 1l.3 2.6h4l.3-2.6a7 7 0 0 0 1.7-1l2.4 1 2-3.4-2-1.6c.1-.3.1-.7.1-1Z',
  sun: 'M12 4V2m0 20v-2m8-8h2M2 12h2m13.7-5.7 1.4-1.4M4.9 19.1l1.4-1.4m0-11.4L4.9 4.9m14.2 14.2-1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  moon: 'M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z',
  collapse: 'M15 5l-7 7 7 7M9 5l-7 7 7 7',
  menu: 'M4 7h16M4 12h16M4 17h16',
  refresh: 'M20 12a8 8 0 1 1-2.6-5.9M20 4v4h-4',
  probe: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-5a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
  copy: 'M8 8V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3M5 8h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2Z',
  trash: 'M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-8 0 1 13h8l1-13',
  check: 'M4 12l5 5L20 6',
  x: 'M6 6l12 12M18 6 6 18',
  plus: 'M12 5v14M5 12h14',
  external: 'M14 4h6v6M20 4 10 14M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
};

/** 内联 SVG 图标（24 网格，1.6 描边），无外链。 */
export function icon(name, cls = '') {
  const d = ICONS[name] || ICONS.overview;
  return (
    `<svg class="${esc(cls)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="${d}"/></svg>`
  );
}

export const ICON_NAMES = Object.keys(ICONS);