/* ==========================================================================
 * 后端 API 客户端
 *
 * 两条硬约定：
 * 1. **任何非 2xx 都抛 ApiError**，绝不让「后端 500」变成页面上一个空表格 ——
 *    看起来像「今天没人调用」，实际是服务挂了。
 * 2. **管理令牌存在内存而不是 localStorage**：本页只绑定 127.0.0.1，但把令牌
 *    落到磁盘会让任何一个同源脚本都能读到它。
 * ========================================================================== */

const BASE = '/api/admin';

export class ApiError extends Error {
  constructor(status, detail, path) {
    super(detail || `请求失败（HTTP ${status}）`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.path = path;
  }
}

/** 内存令牌。刷新页面即丢失 —— 代价是多输一次，换来不落盘。 */
let adminToken = '';

export function setAdminToken(token) {
  adminToken = token || '';
}

export function getAdminToken() {
  return adminToken;
}

function authHeaders() {
  return adminToken ? { 'X-Admin-Token': adminToken } : {};
}

/**
 * FastAPI 的错误体有两种形状：`{"detail": "..."}` 和
 * `{"detail": [{loc, msg, type}]}`（422 校验失败）。都要能读出人话。
 */
function detailOf(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload.detail === 'string') return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((item) => `${(item.loc || []).slice(1).join('.')}: ${item.msg}`)
      .join('；');
  }
  if (payload.error) {
    return payload.error.message || payload.error.type || fallback;
  }
  return fallback;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...authHeaders(),
        ...(options.headers || {}),
      },
    });
  } catch (cause) {
    // fetch 本身失败 = 服务没起来。这是与「后端返回错误」完全不同的一类。
    throw new ApiError(0, `无法连接中转站服务：${cause.message}`, path);
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      detailOf(payload, `请求失败（HTTP ${response.status}）`),
      path,
    );
  }
  return payload;
}

/** 把查询对象拼成 URLSearchParams，跳过空值。 */
export function qs(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  overview: (params) => request(`/overview${qs(params)}`),
  usage: (params) => request(`/usage${qs(params)}`),
  models: (params) => request(`/models${qs(params)}`),
  channel: (params) => request(`/channel${qs(params)}`),
  probe: (reachability = false) =>
    request(`/channel/probe${qs({ reachability: reachability ? 1 : '' })}`, { method: 'POST' }),
  settings: () => request('/settings'),
  patchSettings: (patch) => request('/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  resetSettings: () => request('/settings/reset', { method: 'POST' }),
  clientContext: () => request('/client-context'),
  listKeys: () => request('/keys'),
  createKey: (body) => request('/keys', { method: 'POST', body: JSON.stringify(body) }),
  patchKey: (id, body) => request(`/keys/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  }),
  deleteKey: (id) => request(`/keys/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  prune: () => request('/maintenance/prune', { method: 'POST' }),
  flush: () => request('/maintenance/flush', { method: 'POST' }),
  siteHealth: () => request('../health'),
};