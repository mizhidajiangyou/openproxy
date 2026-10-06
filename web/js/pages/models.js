/* ==========================================================================
 * 模型：免费清单 + 上游在线状态 + 每个模型的用量
 *
 * 页面刻意分两栏：左栏是**策略**（静态清单，后端 `FREE_MODELS` 决定哪些模型
 * 允许被转发），右栏是**观测**（上游当前是否还提供它、各自用了多少）。
 * 两栏混在一起会让人以为「不在清单里」等于「不可用」。
 * ========================================================================== */

import { api } from '../api.js';
import { donutChart, legend } from '../charts.js';
import { ago, bindOnce, compact, esc, int, percent, toast } from '../ui.js';

const SERIES = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)',
  'var(--series-5)', 'var(--series-6)', 'var(--series-7)', 'var(--series-8)'];

/**
 * 把「某模型该走哪条路」算成一份完整的待提交列表。
 *
 * ## 为什么要抽成纯函数
 *
 * 因为``opencode_models`` 是**整份替换**语义，而每次点击的起点是渲染时的
 * 快照 —— 这两件事凑在一起就是丢更新的配方。把它变成纯函数后：
 *
 * - 「顺序按模型清单而不是点击顺序」这条规则可以被直接断言；
 * - 并发保护（``busy.saving``）能独立测试 —— 第三轮 review 用变异测试发现
 *   那个修复**零覆盖**（去掉守卫、只禁被点的开关、去掉顺序归一，三个变异
 *   全都没人失败），就是因为逻辑埋在 DOM 事件里没法测。
 *
 * @param {Iterable<string>} snapshot 渲染时的集合（``viaOpencode``）
 * @param {string[]} order 模型清单的 id 顺序
 * @param {string} modelId 被点的模型
 * @param {boolean} checked 开关的新状态
 * @returns {string[]} 按 ``order`` 归一后的完整列表
 */
export function nextViaOpencode(snapshot, order, modelId, checked) {
  const current = new Set(snapshot);
  if (checked) current.add(modelId);
  else current.delete(modelId);
  // 按清单顺序写回，避免每次勾选都让顺序变成点击顺序 ——
  // 那样同一个配置在库里会有多种写法，而 diff 看不出它们等价。
  return order.filter((id) => current.has(id));
}

/**
 * 本页当前是否仍是「正在显示的那一页」。
 *
 * ## 为什么需要它
 *
 * ``app.js`` 的 ``renderRoute`` 有 ``renderSeq`` 守卫 —— 渲染期间又发生导航，
 * 那次结果整个作废。但**页面内部**的重渲染（点开关后PATCH 回来了要重画）
 * 没有这层保护，而 ``view`` 是全局唯一的 ``#view``元素。
 *
 * 症状（第三轮 review 的 B-3）：用户在模型页勾选开关 → PATCH 飞行中切到
 * 「设置」页 → PATCH 返回 → 本页的 ``await renderModels(view)`` 执行 →
 * **设置页被整个替换成模型页**，而用户以为自己在设置页。
 *
 * ## 怎么实现的
 *
 * 用一个模块级计数器 +「本页渲染时记下当前值」的比较法，
 * 而不是去改 ``app.js`` 导出一个 ``isCurrent()``：那样要动公共入口，
 * 而这里的规则是**页面自己的重绘只该在「自己还在被显示」时生效**。
 * ``app.js`` 切页时会调上一页返回的 cleanup，我们就在那里把令牌作废。
 */
let pageToken = 0;

function isCurrent(token) {
  return token === pageToken;
}

/**
 * token 数 → 200K / 1M。
 *
 * 「未知」一律显示横杠，**包括 0 与负数** —— 后端用 0 表示「没填」，而聚合里
 * 缺数据时也可能落下负值；显示成 `0` 会被读成「这个模型没有上下文」，
 * 显示成 `-1` 更是直接把内部哨兵值漏给了用户。
 */
export function tokens(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '—';
  if (v >= 1e6) return `${trimZero(v / 1e6)}M`;
  if (v >= 1e3) return `${trimZero(v / 1e3)}K`;
  return String(Math.round(v));
}

function trimZero(v) {
  const r = Math.round(v * 10) / 10;
  return String(r);
}

/** 最近一次探测的时间戳（毫秒）。没有探测过返回 0。 */
export function newestReachabilityAt(items) {
  return items.reduce(
    (max, m) => Math.max(max, (m.reachability && m.reachability.checked_at) || 0),
    0
  );
}

/**
 * 站外可达性的三种呈现。
 *
 * **这一项与「上游在线」是两件事**：`/v1/models` 照常返回 id，但 10 个免费模型里
 * 有 9 个真调会回 403 FreeTierError（实测 2026-10-04）。只显示「上游在线」会让人
 * 以为点得通，于是把时间浪费在一个必然失败的请求上。
 */
export function reachabilityTag(model) {
  const r = model.reachability;
  if (!r || r.status === 'unknown') {
    return '<span class="tag" title="尚未探测站外可达性">可达性未探测</span>';
  }
  if (r.status === 'ok') {
    return `<span class="tag tag-ok" title="${esc(r.detail || '实测站外可调')}">站外可调</span>`;
  }
  return `<span class="tag tag-danger" title="${esc(r.detail || '上游拒绝站外调用')}">站外被拒</span>`;
}

export function reasoningTag(model) {
  return model.reasoning
    ? '<span class="tag" title="接受 reasoning_effort：low / medium / high">支持思考级别</span>'
    : '<span class="tag" title="不接受 reasoning_effort">不支持思考</span>';
}

function availability(model) {
  if (model.available === true) return '<span class="tag tag-ok"><span class="dot dot-ok"></span>上游在线</span>';
  if (model.available === false) return '<span class="tag tag-danger"><span class="dot dot-bad"></span>上游未见</span>';
  return '<span class="tag"><span class="dot dot-unknown"></span>未探测</span>';
}

function card(model, viaOpencode) {
  const on = viaOpencode.has(model.id);
  return `
    <div class="model-card">
      <div class="row gap-2" style="align-items:flex-start">
        <div class="grow">
          <div class="model-name">${esc(model.name)}</div>
          <div class="model-id">${esc(model.id)}</div>
        </div>
        ${availability(model)}
      </div>
      ${model.note ? `<div class="faint" style="font-size:12px;margin-top:6px">${esc(model.note)}</div>` : ''}
      <div class="row wrap gap-2" style="margin-top:8px">
        ${reachabilityTag(model)}
        ${reasoningTag(model)}
      </div>
      <dl class="kv model-caps">
        <div><dt>上下文上限</dt><dd>${esc(tokens(model.context_window))}</dd></div>
        <div><dt>最大输出</dt><dd>${esc(tokens(model.max_output_tokens))}</dd></div>
      </dl>
      <div class="model-route">
        <label class="switch" title="走本机 opencode 服务转发，而不是直接打上游">
          <input type="checkbox" data-act="via-opencode" data-model="${esc(model.id)}"
            aria-label="让 ${esc(model.name)} 走本机 opencode 服务"
            ${on ? 'checked' : ''} />
          <span class="switch-track"></span>
        </label>
        <span class="model-route-text">${on ? '走 opencode' : '直通上游'}</span>
      </div>
      <div class="model-stats">
        <div><b>${esc(int(model.requests))}</b>请求</div>
        <div><b>${esc(compact(model.total_tokens))}</b>Token</div>
        <div><b>${model.avg_latency_ms ? esc(`${model.avg_latency_ms}ms`) : '—'}</b>均耗时</div>
      </div>
    </div>`;
}

export async function renderModels(view) {
  // 每次进入本页领一个新令牌；cleanup 时作废它。这样本页的异步重绘
  // 就能判断「我还是当前页吗」（见 pageToken 的说明）。
  const token = (pageToken += 1);
  // 设置与模型清单**并行**取：两者互不依赖，串行会让首屏多等一个往返。
  const [data, settings] = await Promise.all([
    api.models({ days: 30 }),
    api.settings(),  ]);
  const items = data.items || [];
  const unlisted = data.unlisted || [];
  const probe = data.probe;
  // 「清单为空」要当成**加载失败**处理，不能当成「用户想清空」。
  //
  // ``opencode_models`` 是整份替换语义，而 ``nextViaOpencode`` 靠
  // ``items`` 的 id 顺序来算下一份配置 —— 若 ``items`` 为空，任何一次点击
  // 都会算出一个空列表并提交，**把用户已有的配置清掉**。
  // 而触发条件很容易发生：``/api/admin/models`` 返回体缺 ``items``、
  // 或者被网关截断，都会让 ``data.items`` 是undefined。
  //
  // 所以清单为空时直接**不渲染任何开关**，并给一个说明 —— 那样用户点不到，
  // 也就不会误清配置。页面其余部分照常渲染。
  const catalogMissing = items.length === 0;
  // 「走 opencode」的模型集合。**每次重渲染都重新取** —— 改完要立刻反映到
  // 其它卡的开关状态上，而这里重画的就是整页。
  const viaOpencode = new Set(settings.opencode_models || []);
  // 「正在保存」标志。
  //
  // 为什么必需：``opencode_models`` 是**整份替换**语义（PATCH 一个字段、
  // 覆盖整份列表），而每次点击的起点是**渲染时的快照** ``viaOpencode``。
  // 两个点击各自从同一份快照算出一份完整列表并发提交，后到的会把先到的
  // 整个顶掉—— 症状是「勾了两个，刷新后只剩一个」，而界面上两个都还亮着。
  // ``node.disabled = true`` 挡不住这个：它只锁住被点的那个开关，
  // 用户可以接着点另一张卡。实测两次 PATCH 分别发
  // ``["big-pickle"]`` 与 ``["fledge-alpha-free"]``，后者覆盖前者。
  const busy = { saving: false };
  const used = items.filter((m) => m.requests > 0);
  const share = used.map((m, i) => ({
    label: m.id, value: m.total_tokens, color: SERIES[i % SERIES.length],
  }));

  const unlistedNotice = unlisted.length
    ? `<div class="notice notice-warn">
         <div>记录里出现了 <b>${esc(int(unlisted.length))}</b> 个不在免费清单内的模型
         （${esc(unlisted.map((m) => m.model).join('、'))}）。
         通常是历史数据，或「仅转发免费模型」开关被关掉时产生的。</div>
       </div>`
    : '';

  // 「上游在线」与「站外可调」是两件事，必须分开说：/v1/models 照常返回 id，
  // 但真调可能回 403。只显示前者会让人以为点得通。
  const probed = items.filter((m) => m.reachability && m.reachability.status !== 'unknown');
  const blocked = probed.filter((m) => m.reachability.status === 'blocked');
  const callable = probed.filter((m) => m.reachability.status === 'ok');
  const blockedNotice = blocked.length
    ? `<div class="notice notice-danger" style="margin-bottom:16px">
         <div>
           <b>${esc(int(blocked.length))} 个模型站外调不通</b>（实测于
           ${esc(ago(newestReachabilityAt(probed)))}）—— 上游回
           <code>403 FreeTierError</code>：<code>OpenCode's free tier can only be
           used from within OpenCode</code>。它们在 <code>/v1/models</code> 里仍然在线，
           所以「上游在线」不等于「你能调」。
           ${
             callable.length
               ? `清单内站外可调的是：<b>${esc(callable.map((m) => m.id).join('、'))}</b>。`
               : '本轮探测没有任何模型站外可调。'
           }
         </div>
       </div>`
    : '';

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="page-heading">模型</h1>
        <p class="page-lede">
          清单由服务端固定：只有这 ${esc(int(items.length))} 个模型会被转发，
          其余请求会在本站就被拒成 <code>400 model_not_allowed</code>，
          而不是转上去换一个含义错误的 <code>401 ModelError</code>。
        </p>
      </div>
      <div class="row gap-2">
        <button class="btn btn-sm" type="button" data-act="probe-reach"
          title="逐个模型发一次最小请求，验站外能不能调通（会消耗上游额度）">${probeIcon()} 探测站外可达性</button>
        <button class="btn btn-sm" type="button" data-act="probe">${probeIcon()} 探测上游</button>
      </div>
    </div>

    ${unlistedNotice ? `<div style="margin-bottom:16px">${unlistedNotice}</div>` : ''}

    ${blockedNotice}

    <div class="grid grid-2">
      <section class="card card-framed">
        <div class="card-head">
          <div>
            <div class="card-title">近 ${esc(int(data.window_days))} 天分布</div>
            <div class="faint" style="font-size:12px">只统计有调用的模型</div>
          </div>
        </div>
        <div class="card-body">
          ${share.length
            ? `<div class="donut-wrap">
                 ${donutChart({
                   items: share, size: 186,
                   centerValue: String(used.length), centerLabel: '个模型',
                 })}
                 <div class="grow" style="min-width:170px">
                   <div class="chart-legend" style="flex-direction:column;gap:8px">
                     ${share
                       .map(
                         (it) => `
                       <div class="chart-legend-item" style="width:100%">
                         <span class="chart-swatch" style="background:${esc(it.color)}"></span>
                         <span class="mono truncate grow">${esc(it.label)}</span>
                         <span class="num muted">${esc(compact(it.value))}</span>
                       </div>`
                       )
                       .join('')}
                   </div>
                 </div>
               </div>`
            : `<div class="chart-empty" style="height:170px;display:flex;align-items:center;justify-content:center">
                 近 ${esc(int(data.window_days))} 天还没有任何调用
               </div>`}
        </div>
      </section>

      <section class="card card-framed">
        <div class="card-head"><div class="card-title">上游探测</div></div>
        <div class="card-body">
          ${
            probe
              ? `<dl class="kv">
                   <dt>结果</dt><dd>${probe.ok
                     ? '<span class="tag tag-ok">可达</span>'
                     : `<span class="tag tag-danger">${esc(probe.detail || '不可达')}</span>`}</dd>
                   <dt>往返延迟</dt><dd class="num">${probe.latency_ms ? esc(`${probe.latency_ms}ms`) : '—'}</dd>
                   <dt>探测时间</dt><dd>${esc(ago(probe.checked_at))}</dd>
                   <dt>上游模型总数</dt><dd class="num">${esc(int(probe.upstream_count))}</dd>
                   <dt>清单命中率</dt><dd>${esc(
                     percent(
                       items.length
                         ? items.filter((m) => m.available).length / items.length
                         : 0
                     )
                   )}</dd>
                 </dl>`
              : `<div class="empty" style="padding:32px 12px">
                   <div class="empty-title">尚未探测</div>
                   <div class="empty-hint">点右上角「探测上游」，本站会 GET 一次
                     <code>/v1/models</code></div>
                 </div>`
          }
        </div>
      </section>
    </div>

    <section class="card" style="margin-top:16px">
      <div class="card-head">
        <div>
          <div class="card-title">免费模型清单</div>
          <div class="faint" style="font-size:12px">
            用量统计窗口 ${esc(int(data.window_days))} 天 · 共 ${esc(int(items.length))} 个
          </div>
        </div>
        <div class="chart-legend">
          ${legend([{ label: '有调用', color: 'var(--series-1)' }])}
        </div>
      </div>
      <div class="card-body">
        ${catalogMissing
          ? `<div class="empty">
               <div class="empty-title">模型清单加载失败</div>
               <p class="faint" style="font-size:12px;margin:6px 0 0">
                 为避免误清「走 opencode」配置，此处不提供开关。刷新页面重试。
               </p>
             </div>`
          : `<div class="model-grid">${items.map((m) => card(m, viaOpencode)).join('')}</div>`}
      </div>
    </section>`;

  const onClick = async (event) => {
    const node = event.target.closest('[data-act]');
    if (!node) return;
    const act = node.dataset.act;

    // ---- 单模型切换「走 opencode / 直通」 ----
    if (act === 'via-opencode') {
      // 清单为空时不能提交 —— 否则 ``nextViaOpencode`` 会算出一个空列表，
      // 把用户已有的配置清掉。界面上此时没有开关（见 catalogMissing），
      // 这一层是纵深防御：万一真有事件带着 data-model 漏进来，也拦得住。
      if (catalogMissing) return;
      // **已有 PATCH 在飞就忽略这次点击**（见 busy 的注释）。这里必须是
      // 「忽略」而不是「排队」：排队要维护一个待提交队列，而两次点击的
      // 语义合并起来很容易搞错顺序；忽略掉更简单，而且用户看到的是
      // 「点不动」—— 那是诚实的反馈（真丢更新才是骗人）。
      if (busy.saving) return;
      const modelId = node.dataset.model;
      if (!modelId) return;
      busy.saving = true;
      //禁用**全部**开关而不是只禁被点的那个：否则用户能接着点第二张卡，
      // 而那次点击会被上面的 busy 静默忽略 —— 界面毫无反应，
      // 不如一开始就让他点不动。
      view.querySelectorAll('[data-act="via-opencode"]').forEach((el) => {
        el.disabled = true;
      });
      try {
        // 读当前集合 → 增删该模型 → 整份写回。
        // 为什么不是「只提交这一个模型」：后端字段是**整份列表**
        //（PATCH 语义是替换而不是并集），提交单元素会把其它模型的设置全清掉。
        // 纯函数化的版本见 nextViaOpencode —— 它让并发与排序规则可测。
        const ordered = nextViaOpencode(
          viaOpencode, items.map((m) => m.id), modelId, node.checked,
        );
        await api.patchSettings({ opencode_models: ordered });
        toast(node.checked
          ? `${modelId} 改走本机 opencode 服务`
          : `${modelId} 改回直通上游`, 'ok', 3000);
        // 切过页就**不要**重画 —— 否则会把当前页面覆盖成模型页（pageToken 说明）
        if (isCurrent(token)) await renderModels(view);
      } catch (cause) {
        toast(cause.message, 'danger', 3600);
        // 回滚开关：不重画的话它会停在与数据库不一致的状态上，
        // 而用户刚才那次点击「看起来生效了」。
        if (isCurrent(token)) await renderModels(view);
      } finally {
        // **只重置标志，不去解禁那些 input** —— 它们已经随着
        // ``renderModels`` 的整页重画被替换掉了，此刻的 node 是个
        // 脱离文档的旧元素，对它做什么都没有意义。
        busy.saving = false;
      }
      return;
    }

    if (act !== 'probe' && act !== 'probe-reach') return;
    node.disabled = true;
    try {
      const result = await api.probe(act === 'probe-reach');
      if (act === 'probe') {
        toast(result.ok ? `上游可达（${result.latency_ms}ms）` : `探测失败：${result.detail}`,
          result.ok ? 'ok' : 'danger', 3600);
      } else {
        // 可达性探测要发 10 次真实请求（串行 + 间隔），可能十几秒。
        toast('正在逐个验证站外可达性，约十几秒…', 'info', 6000);
        const reach = result.reachability || {};
        const okCount = Object.values(reach).filter((r) => r.status === 'ok').length;
        toast(`站外可调 ${okCount}/${Object.keys(reach).length} 个模型`,
          okCount ? 'ok' : 'danger', 6000);
      }
      // 可达性探测要十几秒，这期间切页的概率不低 —— 所以也要守卫
      if (isCurrent(token)) await renderModels(view);
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
  return () => {
    view.removeEventListener('click', onClick);
    // 作废令牌：切页后本页尚未完成的异步重绘会因此被跳过，
    // 不会把新页面覆盖成模型页（见 pageToken 的说明）。
    pageToken += 1;
  };
}

/** 探测图标。**不要**过 esc()：那会把 SVG 标记转义成一长串字面文本，
 *  把按钮撑到一千多像素宽、把整页顶出横向滚动条。 */
function probeIcon() {
  return (
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
    'stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8"/>' +
    '<circle cx="12" cy="12" r="3"/></svg>'
  );
}