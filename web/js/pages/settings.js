/* ==========================================================================
 * 设置：运行期开关（落库、跨重启生效）+ 维护操作 + 接入信息
 *
 * 这里的每一个开关都对应后端的一个 `Overlays` 字段。改完立即生效、无需重启，
 * 靠的是「改覆盖层 → 重合成配置快照」这条路径，而不是重建服务对象 ——
 * 重建会静默丢掉注入的 recorder / store。
 * ========================================================================== */

import { api } from '../api.js';
import { bindOnce, clientBaseUrl, compact, copy, esc, int, toast } from '../ui.js';
import { refreshShellState } from '../app.js';

function row(label, hint, control) {
  return `
    <div class="row gap-4 wrap" style="padding:14px 0;border-bottom:1px solid var(--rule-hairline)">
      <div class="grow" style="min-width:220px">
        <div class="strong">${esc(label)}</div>
        <div class="faint" style="font-size:12px">${hint}</div>
      </div>
      <div class="shrink-0">${control}</div>
    </div>`;
}

/** 一个开关。
 *
 * ``aria-label`` 是必需的：可见文案在**兄弟节点**里（``row()`` 那个 .strong），
 * 包住 input 的 label 里没有任何文字，所以没有可访问名 —— 读屏用户只会听到
 * 「复选框，未选中」。
 */
function toggle(name, checked, label) {
  return (
    '<label class="switch">' +
    `<input type="checkbox" data-act="${esc(name)}" aria-label="${esc(label)}"` +
    ` ${checked ? 'checked' : ''} />` +
    '<span class="switch-track"></span></label>'
  );
}

export async function renderSettings(view) {
  const [settings, context] = await Promise.all([api.settings(), api.clientContext()]);
  const busy = { flag: false };
  // 后端给的已经是绝对地址，别再拼 location.origin（详见 ui.js clientBaseUrl）。
  const baseUrl = clientBaseUrl(settings.base_url_hint, location);

  /**
   * 打开开关时用哪个档位。取当前下拉框的值；「不注入」那一档意味着用户还没选过，
   * 这时默认给 `low` —— 静默挑一个中档（medium/high/max）会直接改变用户的账单与延迟，
   * 而 low 是最不容易后悔的默认值。
   *
   * 白名单**必须与后端 VALID_REASONING_EFFORTS 一致**：这里手写一份而不同步，
   * 症状是「下拉框里能选 max，点开关后静默变成 low」—— 用户选了拉满，拿到的是最低档，
   * 而且没有任何报错。所以这里从后端给的 `settings.reasoning_efforts` 取，
   * 而不是硬编码（后端是唯一事实来源，见 models.py 的 VALID_REASONING_EFFORTS）。
   */
  function effortPreset() {
    const picked = view.querySelector('[data-act="effort"]');
    const value = picked && picked.value ? picked.value : '';
    const allowed = Array.isArray(settings.reasoning_efforts) && settings.reasoning_efforts.length
      ? settings.reasoning_efforts
      : ['low', 'medium', 'high', 'max'];
    if (value && !allowed.includes(value)) return 'low';
    return value || 'low';
  }

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="page-heading">设置</h1>
        <p class="page-lede">
          这里的改动写进数据库、跨重启保留，会覆盖同名环境变量。
          点「恢复默认」清空覆盖层，回到 <code>OPENPROXY_*</code> 的基线值。
        </p>
      </div>
      <button class="btn btn-sm" type="button" data-act="reset">恢复默认</button>
    </div>

    <div class="grid grid-2">
      <section class="card card-framed">
        <div class="card-head"><div class="card-title">转发</div></div>
        <div class="card-body" style="padding-top:0;padding-bottom:0">
          ${row(
            '要求下游密钥',
            '关：任何本地客户端填任意密钥都能用，用量记在「未署名」。开：必须用本站签发的 <code>sk-op-</code> 密钥。',
            toggle('require_key', settings.require_key, '要求下游密钥')
          )}
          ${row(
            '仅转发免费模型',
            '开：清单外的模型在本站就被拒成 <code>400 model_not_allowed</code>。关：退化为纯反向代理。',
            toggle('free_models_only', settings.free_models_only, '仅转发免费模型')
          )}
          ${row(
            '流式注入用量统计',
            '开：出站请求加上 <code>stream_options.include_usage=true</code>。关：流式响应里每一帧 <code>usage</code> 都是 <code>null</code>，流式用量会记为「未知」。',
            toggle('inject_stream_usage', settings.inject_stream_usage, '流式注入用量统计')
          )}
          ${row(
            '强制思考级别',
            `开：出站请求一律改写成 <code>reasoning_effort</code>（<b>${esc(
              settings.reasoning_effort || ''
            )}</b>），客户端自己写的值会被覆盖。关：完全不动客户端的 body，由客户端自己决定。`,
            `<div class="row gap-2">
              <select class="select" data-act="effort" aria-label="强制思考级别"
                aria-describedby="effort-hint">
                <option value=""${settings.reasoning_effort ? '' : ' selected'}>不注入</option>
                ${(settings.reasoning_efforts || []).map(
                  (v) =>
                    `<option value="${esc(v)}"${
                      settings.reasoning_effort === v ? ' selected' : ''
                    }>${esc(v)}</option>`
                ).join('')}
              </select>
              <label class="switch">
                <input type="checkbox" data-act="effort-on"
                  aria-label="启用强制思考级别"
                  ${settings.reasoning_effort ? 'checked' : ''} />
                <span class="switch-track"></span>
              </label>
            </div>
            <div class="faint" id="effort-hint" style="font-size:11px;margin-top:4px">
              开关关着时也可以先在这里挑档位，挑完再开开关就按这个档位生效。
            </div>`
          )}          ${row(
            '全站日配额 Token',
            `当前合计 <b>${esc(compact(context.global_today_tokens))}</b> Token/天。填 0 表示不限；超出会返回 <code>429</code>。`,
            `<input class="input" style="width:150px" type="number" min="0" step="100000"
              aria-label="全站日配额 Token"
              data-act="quota" value="${esc(settings.daily_token_quota || 0)}" />`
          )}
        </div>
      </section>

      <section class="card card-framed">
        <div class="card-head"><div class="card-title">留存与上游</div></div>
        <div class="card-body" style="padding-top:0;padding-bottom:0">
          ${row(
            '用量保留天数',
            '超过这个天数的记录会被清掉（启动时清一次，之后每 6 小时清一次）。',
            `<input class="input" style="width:110px" type="number" min="1" max="3650"
              aria-label="用量保留天数"
              data-act="retain" value="${esc(settings.retain_days)}" />`
          )}
          ${row(
            '上游地址',
            '改地址会让下一次转发立刻指向新上游。留空则用环境变量的值。',
            `<input class="input mono" style="width:280px" data-act="upstream"
              aria-label="上游地址"
              value="${esc(settings.overlays.upstream_base || settings.upstream_base)}" />`
          )}
          <dl class="kv" style="margin-top:18px">
            <dt>监听</dt><dd class="mono">${esc(settings.host)}:${esc(settings.port)}</dd>
            <dt>出站 UA</dt><dd class="mono">${esc(settings.upstream_user_agent)}</dd>
            <dt>上游认证</dt><dd>${settings.upstream_authenticated
              ? '<span class="tag tag-info">已配置上游 key</span>'
              : '<span class="tag">匿名访问上游</span>'}</dd>
            <dt>管理令牌</dt><dd>${settings.admin_protected
              ? '<span class="tag tag-warn">已启用</span>'
              : '<span class="tag tag-danger">未启用</span>'}</dd>
            <dt>请求体上限</dt><dd>${esc(compact(settings.max_body_bytes))} 字节</dd>
          </dl>
          ${
            settings.admin_protected
              ? ''
              : `<div class="notice notice-warn" style="margin-top:14px">
                   <div>管理端<b>没有令牌</b>。本站默认只绑 127.0.0.1 所以尚可，
                   但只要改成 <code>0.0.0.0</code>，同网段任何人都能读用量、改设置。
                   请设置 <code>OPENPROXY_ADMIN_TOKEN</code>。</div>
                 </div>`
          }
        </div>
      </section>
    </div>

    <div class="grid grid-2" style="margin-top:16px">
      <section class="card card-framed">
        <div class="card-head"><div class="card-title">接入</div></div>
        <div class="card-body">
          <div class="field" style="margin-bottom:12px">
            <label for="baseUrl">接口地址（Base URL）</label>
            <div class="row gap-2">
              <input class="input mono" id="baseUrl" readonly value="${esc(baseUrl)}" />
              <button class="btn btn-sm" type="button" data-act="copy-url"
                data-value="${esc(baseUrl)}">复制</button>
            </div>
          </div>
          <div class="notice">
            <div>
              <div><b>API Key 随便填</b> —— 本站会把它丢掉，绝不会转发给上游
              （上游收到占位凭证会回 <code>401 AuthError</code>）。</div>
              <div style="margin-top:6px">
                免鉴权模式下客户端必须填非空密钥的，填什么都能过；
                开启密钥校验后才需要本站签发的 <code>sk-op-</code> 值。
              </div>
            </div>
          </div>
          <div class="kv" style="margin-top:16px">
            <dt>模型名</dt><dd class="mono">${esc((settings.free_model_ids || []).slice(0, 3).join('、'))} …</dd>
            <dt>免鉴权</dt><dd>${settings.require_key
              ? '<span class="tag tag-danger">已关闭</span>'
              : '<span class="tag tag-ok">已开启</span>'}</dd>
          </div>
        </div>
      </section>

      <section class="card card-framed">
        <div class="card-head"><div class="card-title">维护</div></div>
        <div class="card-body">
          <div class="row gap-2 wrap">
            <button class="btn btn-sm" type="button" data-act="flush">排空统计队列</button>
            <button class="btn btn-sm" type="button" data-act="prune">立即清理过期记录</button>
            <button class="btn btn-sm" type="button" data-act="probe">探测上游</button>
          </div>
          <p class="faint" style="font-size:12px;margin-top:14px">
            写入走有界队列（默认 1 万条）。「排空」会等到队列清空为止；
            队列满时新记录会被丢弃并计数，丢弃数可在
            <code>GET /api/health</code> 的 <code>recorder_dropped</code> 里看到。
          </p>
        </div>
      </section>
    </div>`;

  async function save(patch, okMessage) {
    if (busy.flag) return;
    busy.flag = true;
    try {
      await api.patchSettings(patch);
      toast(okMessage, 'ok');
      await refreshShellState();
      // 成功也必须重画。纯开关不需要（它自己就是状态），但**成对出现的控件**
      // 需要：「思考级别」是一个下拉框 + 一个开关，开关决定下拉框是否 enabled，
      // 而 enabled 不在 PATCH 的影响范围内 —— 不重画就会停在一个「服务端已经是
      // high、界面还是 disabled」的状态，下拉框点不动，看起来像开关坏了。
      await renderSettings(view);
    } catch (cause) {
      toast(cause.message, 'danger', 4600);
      // 失败后重画，把控件状态回滚到服务端的真实值
      await renderSettings(view);
    } finally {
      busy.flag = false;
    }
  }

  const onClick = async (event) => {
    const node = event.target.closest('[data-act]');
    if (!node) return;
    const act = node.dataset.act;
    node.disabled = true;
    try {
      if (act === 'reset') {
        if (!confirm('恢复默认会清空所有运行期覆盖，回到环境变量的值。继续？')) return;
        await api.resetSettings();
        toast('已恢复默认', 'ok');
        await refreshShellState();
        await renderSettings(view);
      } else if (act === 'flush') {
        const result = await api.flush();
        toast(
          `已排空：写入 ${result.written} 条，丢弃 ${result.dropped} 条，失败 ${result.failed} 条`,
          result.failed ? 'danger' : 'ok',
          3600
        );
      } else if (act === 'prune') {
        const result = await api.prune();
        toast(`清理了 ${result.removed} 条（保留 ${result.retain_days} 天）`, 'ok', 3600);
      } else if (act === 'probe') {
        const result = await api.probe();
        toast(result.ok ? `上游可达（${result.latency_ms}ms）` : `探测失败：${result.detail}`,
          result.ok ? 'ok' : 'danger', 3600);
      } else if (act === 'copy-url') {
        const ok = await copy(node.dataset.value);
        toast(ok ? '已复制接口地址' : '复制失败，请手动选中', ok ? 'ok' : 'danger');
      }
    } catch (cause) {
      toast(cause.message, 'danger', 4600);
    } finally {
      node.disabled = false;
    }
  };

  const onChange = async (event) => {
    const act = event.target.dataset.act;
    if (act === 'require_key') {
      if (event.target.checked) {
        const confirmed = confirm(
          '开启后，没有本站签发密钥的请求会全部收到 401。\n' +
          '确认你的客户端已经改用 sk-op- 密钥了吗？'
        );
        if (!confirmed) {
          event.target.checked = false;
          return;
        }
      }
      await save({ require_key: event.target.checked }, '已更新鉴权模式');
    } else if (act === 'free_models_only') {
      await save({ free_models_only: event.target.checked }, '已更新模型白名单');
    } else if (act === 'inject_stream_usage') {
      await save({ inject_stream_usage: event.target.checked }, '已更新流式用量采集');
    } else if (act === 'effort-on') {
      // 关掉开关 = 「显式取消强制」。传空串而不是 null：null 在 PATCH 里
      // 表示「不修改」，那样开关关了但配置还留着。
      await save(
        { reasoning_effort: event.target.checked ? effortPreset() : '' },
        event.target.checked ? '已启用强制思考级别' : '已取消强制思考级别'
      );
    } else if (act === 'effort') {
      await save({ reasoning_effort: event.target.value }, '已更新思考级别');
    } else if (act === 'quota') {
      await save({ daily_token_quota: Number(event.target.value) || 0 }, '已更新全站配额');
    } else if (act === 'retain') {
      const days = Number(event.target.value);
      if (!Number.isFinite(days) || days < 1) {
        toast('保留天数必须是 1–3650 的整数', 'danger');
        return;
      }
      await save({ retain_days: days }, '已更新保留天数');
    } else if (act === 'upstream') {
      await save({ upstream_base: event.target.value.trim() }, '已更新上游地址');
    }
  };

  bindOnce(view, 'click', onClick);
  bindOnce(view, 'change', onChange);
  return () => {
    view.removeEventListener('click', onClick);
    view.removeEventListener('change', onChange);
  };
}

/** 数字格式化的再导出，方便其它页面统一引用。 */
export const _fmt = { int };