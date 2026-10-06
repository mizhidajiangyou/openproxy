/* ==========================================================================
 * 使用指南：怎么把客户端指过来
 *
 * 这一页的内容全部是**可验证的事实**：地址来自后端 <code>/settings</code>，
 * 端点清单来自 <code>FREE_MODELS</code>，不是写死的文案。改了配置这页跟着变。
 * ========================================================================== */

import { api } from '../api.js';
import { clientBaseUrl, esc, int } from '../ui.js';

const ENDPOINTS = [
  { method: 'POST', path: '/v1/chat/completions', note: '对话补全（支持流式 SSE）' },
  { method: 'GET', path: '/v1/models', note: '模型清单，直接透传给上游' },
  { method: 'GET', path: '/v1/__health', note: '本站自检，不打上游' },
  { method: 'GET', path: '/api/docs', note: 'OpenAPI 文档' },
];

function endpointRow(item) {
  return `
    <div class="endpoint">
      <span class="endpoint-method">${esc(item.method)}</span>
      <span class="endpoint-path grow">${esc(item.path)}</span>
      <span class="muted" style="font-size:12px">${esc(item.note)}</span>
    </div>`;
}

export async function renderGuide(view) {
  const settings = await api.settings();
  const base = clientBaseUrl(settings.base_url_hint, location);

  const curlSample =
    `curl ${base}/chat/completions \\\n` +
    `  -H "Content-Type: application/json" \\\n` +
    `  -H "Authorization: Bearer sk-op-你的密钥" \\\n` +
    `  -d '{\n` +
    `    "model": "${(settings.free_model_ids || [])[0] || 'space-bunny-free'}",\n` +
    `    "messages": [{"role": "user", "content": "你好"}],\n` +
    `    "stream": true\n` +
    `  }'`;

  const clientSample =
    `{\n` +
    `  "baseURL": "${base}",\n` +
    `  "apiKey": "随便填，非空即可",\n` +
    `  "model": "${(settings.free_model_ids || [])[0] || 'space-bunny-free'}"\n` +
    `}`;

  view.innerHTML = `
    <section class="card card-framed">
      <div class="card-body">
        <div class="doc">
          <h1 class="sr-only">使用指南 —— 把客户端指到这个地址即可</h1>
          <h2>三步接上</h2>
          <ol class="steps">
            <li>
              <div><b>把客户端的接口地址指向本站</b></div>
              <div class="muted">多数客户端叫「Base URL」「接口地址」「API 地址」。填：</div>
              <div class="secret-box" style="border-style:solid;border-color:var(--rule)">
                <code>${esc(base)}</code>
              </div>
            </li>
            <li>
              <div><b>API Key 随便填个非空值</b></div>
              <div class="muted">
                有些客户端不允许密钥留空，这是它们的要求，不是上游的。
                本站会把客户端带来的任何凭证丢掉再转发，绝不会让它碰到上游 ——
                上游收到占位凭证会返回 <code>401 AuthError</code>。
              </div>
            </li>
            <li>
              <div><b>模型名填清单里的任意一个</b></div>
              <div class="muted">当前清单（${esc(int(settingCount(settings)))} 个）：</div>
              <div class="row wrap gap-2" style="margin-top:8px">
                ${(settings.free_model_ids || [])
                  .map((id) => `<code>${esc(id)}</code>`)
                  .join('')}
              </div>
            </li>
          </ol>
        </div>
      </div>
    </section>

    <div class="grid grid-2" style="margin-top:16px">
      <section class="card card-framed">
        <div class="card-head"><div class="card-title">curl 验证</div></div>
        <div class="card-body">
          <pre><code>${esc(curlSample)}</code></pre>
          <p class="faint" style="font-size:12px;margin-top:12px">
            开启密钥校验后，把 <code>Authorization</code> 换成「密钥」页签发的
            <code>sk-op-…</code> 值；免鉴权时这一行可以照抄。
          </p>
        </div>
      </section>

      <section class="card card-framed">
        <div class="card-head"><div class="card-title">客户端配置片段</div></div>
        <div class="card-body">
          <pre><code>${esc(clientSample)}</code></pre>
          <div class="notice" style="margin-top:12px">
            <div>接口是 OpenAI 兼容的：<code>/v1/chat/completions</code>、
            <code>stream</code>、SSE 事件格式都按 OpenAI 的来，
            所以绝大多数客户端改个地址就能用。</div>
          </div>
        </div>
      </section>
    </div>

    <section class="card" style="margin-top:16px">
      <div class="card-head"><div class="card-title">可用端点</div></div>
      <div class="card-body">
        ${ENDPOINTS.map(endpointRow).join('')}
        <div class="muted" style="font-size:12.5px;margin-top:12px">
          <code>/v1/*</code> 下的其它路径也会原样透传给上游，所以上游新增端点时
          本站不用改代码。
        </div>
      </div>
    </section>

    <section class="card card-framed" style="margin-top:16px">
      <div class="card-head"><div class="card-title">行为约定</div></div>
      <div class="card-body">
        <div class="doc">
          <h3>鉴权</h3>
          <p>
            默认<b>免鉴权</b>：未带密钥的调用会归到「未署名」桶，仍会完整计数。
            在「设置」里开启密钥校验后，客户端必须带本站签发的
            <code>sk-op-</code> 密钥，否则返回 <code>401 missing_api_key</code>；
            密钥被停用返回 <code>401 api_key_disabled</code>。
            超出日配额返回 <code>429 daily_quota_exceeded</code>。
          </p>

          <h3>模型白名单</h3>
          <p>
            开启「仅转发免费模型」时，清单外的模型在本站就被拒成
            <code>400 model_not_allowed</code>。这么做是因为上游对未知模型返回的是
            <code>401 ModelError</code> —— 字面意思是「凭证无效」，
            透传过去会让客户端误判成密钥问题。关掉该开关则完全透传。
          </p>

          <h3>用量统计</h3>
          <p>
            <b>只存计数，绝不存提示词与回答。</b> 表里有 token 数、耗时、字节数、
            模型名、状态码、客户端标签，没有正文。密钥明文也只在签发那一刻返回一次，
            库里存的是 sha256 的前 32 位。
          </p>
          <p>
            流式调用依赖出站注入 <code>stream_options.include_usage=true</code>。
            注入失败或被关掉时，流式响应里每一帧的 <code>usage</code> 都是
            <code>null</code>，那次调用会被记成「用量未知」——
            总览页会显式提示有多少条未知，而不是把它当成 0 混进总量。
          </p>

          <h3>重试</h3>
          <p>
            不自动重试。<code>/v1/chat/completions</code> 是有副作用的 POST，
            重试会白白消耗上游额度并可能重复计费。
            只有幂等的 <code>GET /v1/models</code> 探测会重试一次。
            上游超时回 <code>504</code>，连不上回 <code>502</code>。
          </p>

          <h3>管理端</h3>
          <p>
            <code>/api/admin/*</code>${
              settings.admin_protected
                ? '已启用令牌校验，请在页面右上角填入 <code>OPENPROXY_ADMIN_TOKEN</code>。'
                : '<b>未启用令牌校验</b>。本站默认只绑 127.0.0.1；一旦改绑 0.0.0.0，同网段任何人都能读用量、改设置，请务必设置 <code>OPENPROXY_ADMIN_TOKEN</code>。'
            }
          </p>
        </div>
      </div>
    </section>`;

  return null;
}

function settingCount(settings) {
  return (settings.free_model_ids || []).length;
}

/** 供测试断言「指南页没有写死数字」用。 */
export const _internal = { ENDPOINTS, settingCount };