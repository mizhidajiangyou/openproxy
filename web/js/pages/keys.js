/* ==========================================================================
 * 密钥：签发 / 停用 / 改配额 / 删除 + 一次性明文展示
 *
 * 明文只在创建响应里出现一次。弹窗里必须**强制用户确认已经保存**才允许关闭，
 * 否则「随手点掉弹窗」= 密钥永久丢失，而之后没有任何接口能再取回它。
 * ========================================================================== */

import { api } from '../api.js';
import { copy, esc, int, stamp, toast } from '../ui.js';
import { refreshShellState } from '../app.js';

function keyRows(keys, requireKey) {
  const window = Number(keys.window_days) || 90;
  if (!keys.length) {
    return `<div class="empty">
      <div class="empty-title">还没有签发任何密钥</div>
      <div class="empty-hint">
        当前中转站<b>免鉴权</b>，任何本地客户端填任意密钥都能用。签发密钥后才能
        「按客户归集用量」与「设日配额」。
      </div>
    </div>`;
  }
  return `
    <div class="table-wrap">
      <table class="table">
        <thead><tr>
          <th>名称</th><th>前缀</th><th>状态</th><th class="num">日配额</th>
          <th class="num">近 ${esc(window)} 天请求</th><th class="num">Token</th>
          <th>创建</th><th>最近使用</th><th class="col-actions">操作</th>
        </tr></thead>
        <tbody>
          ${keys
            .map(
              (key) => `
            <tr>
              <td>
                <div class="strong">${esc(key.name)}</div>
                ${key.note ? `<div class="faint" style="font-size:12px">${esc(key.note)}</div>` : ''}
              </td>
              <td class="mono">${esc(key.prefix)}…</td>
              <td>${key.disabled
                ? '<span class="tag">已停用</span>'
                : '<span class="tag tag-ok">生效中</span>'}</td>
              <td class="num">${key.daily_token_quota
                ? esc(int(key.daily_token_quota))
                : '<span class="faint">不限</span>'}</td>
              <td class="num">${esc(int(key.requests))}</td>
              <td class="num">${esc(int(key.total_tokens))}</td>
              <td class="nowrap muted">${esc(stamp(key.created_at))}</td>
              <td class="nowrap muted">${key.last_used_at ? esc(stamp(key.last_used_at)) : '—'}</td>
              <td class="col-actions">
                <button class="btn btn-sm" type="button" data-act="quota" data-id="${esc(key.id)}">配额</button>
                <button class="btn btn-sm" type="button" data-act="toggle" data-id="${esc(key.id)}"
                  data-disabled="${key.disabled ? '1' : '0'}">${key.disabled ? '启用' : '停用'}</button>
                <button class="btn btn-sm btn-danger" type="button" data-act="del" data-id="${esc(key.id)}"
                  data-name="${esc(key.name)}">删除</button>
              </td>
            </tr>`
            )
            .join('')}
        </tbody>
      </table>
    </div>`;
}

function modal(title, body, foot, opts = {}) {
  return `
    <div class="modal-scrim${opts.open ? ' is-open' : ''}" id="keyModal">
      <div class="modal" role="dialog" aria-modal="true" tabindex="-1" aria-label="${esc(title)}">
        <div class="modal-head">
          <div class="modal-title">${esc(title)}</div>
          ${opts.locked ? '' : '<button class="btn btn-ghost btn-icon" type="button" data-act="close">✕</button>'}
        </div>
        <div class="modal-body">${body}</div>
        <div class="modal-foot">${foot}</div>
      </div>
    </div>`;
}

const CREATE_BODY = `
  <div class="stack gap-4">
    <div class="field">
      <label for="kName">名称</label>
      <input class="input" id="kName" placeholder="例如：ChatBox 桌面端" maxlength="64" />
    </div>
    <div class="field">
      <label for="kNote">备注（可选）</label>
      <input class="input" id="kNote" placeholder="谁在用、装在哪台机器" maxlength="200" />
    </div>
    <div class="field">
      <label for="kQuota">日配额 Token（留空 = 不限）</label>
      <input class="input" id="kQuota" type="number" min="1" step="1000" placeholder="例如 2000000" />
    </div>
  </div>`;

export async function renderKeys(view) {
  const [keys, settings] = await Promise.all([api.listKeys(), api.settings()]);

  view.innerHTML = `
    <h1 class="sr-only">密钥 —— 为下游客户端签发并按密钥归集用量</h1>
    ${keys.require_key
      ? `<div class="notice notice-info" style="margin-bottom:16px">
           <div>本站当前<b>强制校验密钥</b>：没有有效密钥的请求会收到
           <code>401 missing_api_key</code>。</div>
         </div>`
      : `<div class="notice notice-warn" style="margin-bottom:16px">
           <div>本站当前<b>免鉴权</b>：未署名调用会混进总览与调用记录。
           想按客户归集用量并限制配额，请在「设置」里开启密钥校验。</div>
         </div>`}

    <section class="card card-framed">
      <div class="card-head">
        <div>
          <div class="card-title">已签发密钥</div>
          <div class="faint" style="font-size:12px">
            明文只在签发时显示一次，之后只能看到前缀。删除密钥不会删除历史用量。
          </div>
        </div>
        <button class="btn btn-primary btn-sm" type="button" data-act="create">签发新密钥</button>
      </div>
      <div class="card-body card-body-flush">${keyRows(keys.items || [], settings.require_key)}</div>
    </section>`;

  async function reload() {
    const fresh = await api.listKeys();
    const box = view.querySelector('.card-body-flush');
    if (box) box.innerHTML = keyRows(fresh.items || [], fresh.require_key);
    refreshShellState();
  }

  const onClick = async (event) => {
    const node = event.target.closest('[data-act]');
    if (!node) return;
    const act = node.dataset.act;
    const id = node.dataset.id;

    try {
      if (act === 'create') {
        openCreate();
      } else if (act === 'close') {
        closeModal();
      } else if (act === 'toggle') {
        await api.patchKey(id, { disabled: node.dataset.disabled === '1' });
        toast('已更新密钥状态', 'ok');
        await reload();
      } else if (act === 'quota') {
        openQuota(id);
      } else if (act === 'del') {
        if (!confirm(`删除密钥「${node.dataset.name}」？\n历史用量会保留，但该客户端将立即无法通过校验。`)) return;
        const result = await api.deleteKey(id);
        toast(`已删除，保留 ${result.kept_usage_records} 条历史用量`, 'ok');
        await reload();
      } else if (act === 'copy-secret') {
        const ok = await copy(node.dataset.secret);
        toast(ok ? '已复制到剪贴板' : '复制失败，请手动选中', ok ? 'ok' : 'danger');
      } else if (act === 'ack-secret') {
        closeModal();
      }
    } catch (cause) {
      toast(cause.message, 'danger', 4200);
    }
  };

  // --------------------------------------------------------- 弹窗 ---

  // 弹窗必须**真的**是模态的：声明了 aria-modal="true" 却不把焦点放进去、
  // 也不拦 Tab，读屏与键盘用户拿到的语义和实际行为对不上。
  //
  // 三件事，缺一不可：① 打开时记住来源元素并把焦点移进弹窗；② Tab 在弹窗内循环；
  // ③ 关闭时把焦点还给来源元素。没有 ①③ 的话，关闭弹窗后焦点掉回 body，
  // 用户要从文档开头重新 Tab 整个侧边栏 —— 而这一刻正是「明文密钥只显示这一次」。
  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
    ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  let opener = null;

  function mount(html) {
    opener = document.activeElement;
    document.body.insertAdjacentHTML('beforeend', html);
    const scrim = document.getElementById('keyModal');
    scrim.addEventListener('click', (event) => {
      if (event.target === scrim && !scrim.querySelector('[data-locked]')) closeModal();
    });
    scrim.addEventListener('keydown', onTab);
    document.addEventListener('keydown', onEsc);
    // 焦点进弹窗：优先第一个输入框，没有就落在弹窗容器上
    const first = scrim.querySelector('input, textarea, select, button');
    if (first) first.focus();
    else scrim.focus();
    return scrim;
  }

  function onTab(event) {
    if (event.key !== 'Tab') return;
    const scrim = document.getElementById('keyModal');
    if (!scrim) return;
    const items = [...scrim.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function closeModal() {
    const scrim = document.getElementById('keyModal');
    if (scrim) scrim.remove();
    document.removeEventListener('keydown', onEsc);
    // 焦点还给打开它的那个按钮（它可能已经随重画消失，那就什么都不做，
    // 让浏览器按默认规则落到 body —— 强 focus 一个已脱离文档的节点会报错）
    const back = opener;
    opener = null;
    if (back && document.contains(back) && typeof back.focus === 'function') back.focus();
  }

  function onEsc(event) {
    if (event.key !== 'Escape') return;
    // 「已保存」弹窗带 locked 标记：Esc 不能关它 —— 明文只出现这一次，
    // 关掉就再也拿不回来，而用户很可能只是想关窗口。
    if (document.querySelector('#keyModal [data-locked]')) {
      toast('明文密钥只显示这一次，请先复制保存再关闭', 'warn', 4000);
      return;
    }
    closeModal();
  }

  function openCreate() {
    mount(
      modal(
        '签发新密钥',
        CREATE_BODY,
        `<button class="btn" type="button" data-act="close">取消</button>
         <button class="btn btn-primary" type="button" data-act="submit-create">签发</button>`,
        { open: true }
      )
    );
    const quota = document.getElementById('kQuota');
    const submit = document.querySelector('[data-act="submit-create"]');
    submit.addEventListener('click', async () => {
      const raw = quota.value.trim();
      submit.disabled = true;
      try {
        const payload = { name: document.getElementById('kName').value.trim() };
        const note = document.getElementById('kNote').value.trim();
        if (note) payload.note = note;
        if (raw) payload.daily_token_quota = Number(raw);
        const created = await api.createKey(payload);
        closeModal();
        await reload();
        showSecret(created);
      } catch (cause) {
        toast(cause.message, 'danger', 4200);
        submit.disabled = false;
      }
    });
    document.getElementById('kName').focus();
  }

  function showSecret(created) {
    mount(
      modal(
        '密钥已签发',
        `<div class="notice notice-danger">
           <div><b>${esc(created.hint)}</b></div>
         </div>
         <div class="secret-box">
           <div class="muted" style="font-size:12px">明文密钥</div>
           <code class="secret-value">${esc(created.secret)}</code>
           <button class="btn btn-sm" type="button" data-act="copy-secret"
             data-secret="${esc(created.secret)}">复制</button>
         </div>
         <div class="kv">
           <dt>名称</dt><dd>${esc(created.key.name)}</dd>
           <dt>前缀</dt><dd class="mono">${esc(created.key.prefix)}…</dd>
           <dt>日配额</dt><dd>${created.key.daily_token_quota
             ? esc(int(created.key.daily_token_quota))
             : '不限'}</dd>
         </div>`,
        '<button class="btn btn-primary" type="button" data-act="ack-secret" data-locked="1">我已保存</button>',
        { open: true, locked: true }
      )
    );
  }

  function openQuota(id) {
    const key = (keys.items || []).find((k) => k.id === id);
    if (!key) return;
    mount(
      modal(
        `调整配额 · ${key.name}`,
        `<div class="field">
           <label for="qQuota">日配额 Token（留空 = 不限）</label>
           <input class="input" id="qQuota" type="number" min="1" step="1000"
             value="${key.daily_token_quota || ''}" />
           <div class="faint" style="font-size:12px">
             配额按<b>本地自然日</b>结算，用满即返回 <code>429 daily_quota_exceeded</code>。
           </div>
         </div>`,
        `<button class="btn" type="button" data-act="close">取消</button>
         <button class="btn btn-primary" type="button" data-act="submit-quota">保存</button>`,
        { open: true }
      )
    );
    const submit = document.querySelector('[data-act="submit-quota"]');
    submit.addEventListener('click', async () => {
      const raw = document.getElementById('qQuota').value.trim();
      submit.disabled = true;
      try {
        await api.patchKey(id, { daily_token_quota: raw ? Number(raw) : 0 });
        closeModal();
        toast('已更新配额', 'ok');
        await reload();
      } catch (cause) {
        toast(cause.message, 'danger', 4200);
        submit.disabled = false;
      }
    });
  }

  document.addEventListener('click', onClick);
  return () => {
    document.removeEventListener('click', onClick);
    closeModal();
  };
}