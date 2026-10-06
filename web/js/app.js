/* ==========================================================================
 * 壳层：hash 路由、侧边栏、顶栏、主题
 *
 * 用 hash 路由而不是 history API，是为了让后端**不需要** SPA fallback 路由。
 * 刷新任何一个页面都只会命中 `/`，静态站也就只有一个入口文件。
 * 未知地址一律回落到总览，不给用户一个 404。
 * ========================================================================== */

import { api, setAdminToken } from './api.js';
import { el, esc, icon, toast, unbindAll } from './ui.js';
import { renderOverview } from './pages/overview.js';
import { renderKeys } from './pages/keys.js';
import { renderUsage } from './pages/usage.js';
import { renderModels } from './pages/models.js';
import { renderChannel } from './pages/channel.js';
import { renderGuide } from './pages/guide.js';
import { renderSettings } from './pages/settings.js';

export const ROUTES = [
  { id: 'overview', label: '总览', icon: 'overview', group: '观览',
    title: '总览', sub: '中转站的实时概览', render: renderOverview },
  { id: 'usage', label: '调用记录', icon: 'usage', group: '观览',
    title: '调用记录', sub: '每一次转发都留痕，只存计数不存正文', render: renderUsage },
  { id: 'models', label: '模型', icon: 'models', group: '观览',
    title: '模型', sub: '可转发的免费模型与各自的用量', render: renderModels },
  { id: 'channel', label: '渠道', icon: 'channel', group: '运维',
    title: '渠道', sub: '上游可达性、延迟与失败分布', render: renderChannel },
  { id: 'keys', label: '密钥', icon: 'keys', group: '运维',
    title: '密钥', sub: '为下游客户端签发，并按密钥归集用量', render: renderKeys },
  { id: 'settings', label: '设置', icon: 'settings', group: '运维',
    title: '设置', sub: '运行期开关，保存后立即生效', render: renderSettings },
  { id: 'guide', label: '使用指南', icon: 'guide', group: '帮助',
    title: '使用指南', sub: '把客户端指到这个地址即可', render: renderGuide },
];

const DEFAULT_ROUTE = 'overview';

const store = {
  collapsed: localStorage.getItem('opx.collapsed') === '1',
  theme: localStorage.getItem('opx.theme') || 'light',
};

let cleanup = null;

/** 单调递增的渲染令牌。
 *
 * ``renderRoute`` 是 async，而 ``hashchange`` 会在上一次还在飞的时候就再触发一次
 * （控制台读接口实测要占 ~300ms，点两下「下一页」就重叠了）。没有令牌的话两次
 * 渲染并发推进，先完成的那次会**盖掉后完成的**内容，并且把 ``cleanup`` 覆盖成
 * 自己那份 —— 于是另一页刚挂上的监听再也没人摘，每次这样的导航都往 ``#view`` 上
 * 永久多留一套。过期的那次直接丢弃：不写 innerHTML、不碰 cleanup。
 */
let renderSeq = 0;

/* --------------------------------------------------------------- 路由 --- */

export function currentRoute() {
  const raw = location.hash.replace(/^#\/?/, '').split('?')[0].trim();
  const found = ROUTES.find((r) => r.id === raw);
  if (found) return found;
  return ROUTES.find((r) => r.id === DEFAULT_ROUTE);
}

export function navigate(id) {
  if (currentRoute().id === id) return;
  location.hash = `#/${id}`;
}

async function renderRoute() {
  const route = currentRoute();
  const view = el('view');
  const seq = (renderSeq += 1);

  // 上一页的定时器/监听必须先拆掉，否则切页后仍在往已卸载的 DOM 里写。
  // unbindAll 兜住「cleanup 没被调用」的情况（并发渲染、页面自己重画）。
  if (typeof cleanup === 'function') {
    try {
      cleanup();
    } catch (cause) {
      console.error('页面清理失败', cause);
    }
  }
  unbindAll(view);
  cleanup = null;

  el('pageTitle').textContent = route.title;
  el('pageSubtitle').textContent = route.sub || '';
  document.title = `${route.title} · 云笺中转站`;
  for (const node of document.querySelectorAll('.nav-link')) {
    node.classList.toggle('is-active', node.dataset.route === route.id);
  }
  closeDrawer();

  view.innerHTML = '<div class="empty"><div class="empty-title">载入中…</div></div>';
  try {
    const teardown = await route.render(view);
    // 渲染期间又发生了一次导航：这次的结果整个作废。
    if (seq !== renderSeq) {
      if (typeof teardown === 'function') teardown();
      return;
    }
    cleanup = teardown || null;
    // 渲染**完成**后派发一次事件：页面若需要恢复焦点/滚动位置等「现场」，
    // 在这里做才靠得住 —— render 是 async 的，中间隔着一次接口，
    // 紧随 innerHTML 赋值的微任务跑得太早，内容还不存在。
    view.dispatchEvent(new CustomEvent('rendered', { detail: { route: route.id } }));
  } catch (cause) {
    if (seq !== renderSeq) return;
    view.innerHTML = `
      <div class="notice notice-danger">
        <div>
          <div class="strong">页面载入失败</div>
          <div class="mono faint" style="margin-top:4px">${esc(cause.message || String(cause))}</div>
        </div>
      </div>`;
  }
}

/* --------------------------------------------------------------- 壳层 --- */

function renderNav() {
  const groups = [];
  for (const route of ROUTES) {
    let group = groups.find((g) => g.name === route.group);
    if (!group) {
      group = { name: route.group, items: [] };
      groups.push(group);
    }
    group.items.push(route);
  }
  el('navList').innerHTML = groups
    .map(
      (group) => `
      <div class="nav-section">
        <div class="nav-section-label">${esc(group.name)}</div>
        ${group.items
          .map(
            (route) => `
          <button class="nav-link" type="button" data-act="nav" data-route="${esc(route.id)}">
            ${icon(route.icon, 'nav-icon')}
            <span class="nav-label">${esc(route.label)}</span>
          </button>`
          )
          .join('')}
      </div>`
    )
    .join('');
}

function applyTheme() {
  document.documentElement.dataset.theme = store.theme;
  // 必须带 nav-icon 类：icon() 生成的 svg 没有默认尺寸，漏掉会让它撑满整个按钮
  el('btnTheme').innerHTML =
    (store.theme === 'dark' ? icon('sun', 'nav-icon') : icon('moon', 'nav-icon')) +
    `<span class="foot-label">${store.theme === 'dark' ? '日间' : '夜间'}</span>`;
}

function toggleCollapse() {
  store.collapsed = !store.collapsed;
  localStorage.setItem('opx.collapsed', store.collapsed ? '1' : '0');
  applyShell();
}

function applyShell() {
  el('shell').classList.toggle('is-collapsed', store.collapsed);
}

function openDrawer() {
  el('shell').classList.add('drawer-open');
  el('drawerScrim').classList.add('is-open');
}

function closeDrawer() {
  el('shell').classList.remove('drawer-open');
  el('drawerScrim').classList.remove('is-open');
}

/** 顶栏的鉴权模式印章：免鉴权/强制鉴权是这个站最要紧的一个状态，必须常驻可见。 */
function paintModeSeal(requireKey, adminProtected) {
  const seal = el('modeSeal');
  const parts = [];
  if (adminProtected) parts.push('<span class="tag tag-info">已设管理令牌</span>');
  parts.push(
    requireKey
      ? '<span class="seal mode-seal mode-locked">验密钥</span>'
      : '<span class="seal mode-seal mode-open">免验</span>'
  );
  seal.innerHTML = parts.join('');
}

export async function refreshShellState() {
  try {
    const settings = await api.settings();
    paintModeSeal(settings.require_key, settings.admin_protected);
    const badge = el('navKeysBadge');
    const keys = await api.listKeys();
    const active = keys.items.filter((k) => !k.disabled).length;
    badge.textContent = keys.total ? String(active) : '';
    badge.classList.toggle('hidden', !keys.total);
  } catch (cause) {
    // 令牌不对 / 服务不可达：把状态标成「未知」，不要假装正常
    if (cause.status === 401) {
      el('modeSeal').innerHTML = '<span class="tag tag-danger">需要管理令牌</span>';
    }
    el('navKeysBadge').classList.add('hidden');
  }
}

/* --------------------------------------------------------------- 启动 --- */

function bindShell() {
  el('btnTheme').addEventListener('click', () => {
    store.theme = store.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('opx.theme', store.theme);
    applyTheme();
  });
  el('btnCollapse').addEventListener('click', toggleCollapse);
  el('btnMenu').addEventListener('click', openDrawer);
  el('drawerScrim').addEventListener('click', closeDrawer);
  el('navList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-route]');
    if (button) navigate(button.dataset.route);
  });
  el('btnToken').addEventListener('click', () => {
    const current = localStorage.getItem('opx.token') || '';
    const next = prompt('输入 OPENPROXY_ADMIN_TOKEN（留空清除）', current);
    if (next === null) return;
    localStorage.setItem('opx.token', next);
    setAdminToken(next);
    toast(next ? '已设置管理令牌' : '已清除管理令牌', 'ok');
    refreshShellState();
  });
  window.addEventListener('hashchange', renderRoute);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDrawer();
  });
}

export async function boot() {
  setAdminToken(localStorage.getItem('opx.token') || '');
  renderNav();
  applyTheme();
  applyShell();
  bindShell();
  await renderRoute();
  refreshShellState();
}

boot();