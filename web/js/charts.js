/* ==========================================================================
 * 自研 SVG 图表 —— 折线 / 面积 / 柱 / 环形 / 迷你走势
 *
 * 为什么不引图表库（决策 Q3 的落地）：
 * 1. 零依赖，页面零外链，可以 file:// 直接跑；
 * 2. 样式 100% 自有，能把配色/字体/线条粗细调成古风，而不是覆盖第三方默认主题；
 * 3. **纯函数**（数据 → SVG 字符串），所以能在 node 里直接单测坐标缩放、
 *    空数据降级、除零保护这些最容易出错的地方；第三方图表的内部算不了。
 *
 * 所有函数都遵守三条硬约定：
 *   - 数据为空或全 0 时画「空态」而不是崩掉；
 *   - 坐标反转型（折线的 y 轴）要防除零，避免全平数据把线画到画布外；
 *   - 输出是纯 SVG 字符串，不含 script / 外链 / data: URI。
 * ========================================================================== */

const NS = 'http://www.w3.org/2000/svg';

/* ------------------------------------------------------------------ 工具 --- */

/** 数字转字符串并去掉浮点尾巴（2.0000000000000004 → 2）。 */
export function fmt(value) {
  if (!Number.isFinite(value)) return '0';
  return String(Math.round(value * 1000) / 1000);
}

/** XML 文本转义。用户可控的模型名/密钥名必须过这一层。 */
export function esc(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 取「好看」的刻度上限。
 *
 * 关键点：`niceMax(0)` 必须返回一个**正数**，否则分母为 0 → 全平数据时
 * 所有点的 y 都会算出 NaN，图上什么都没有。1 是这里的哨兵值。
 */
export function niceMax(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const frac = value / base;
  let nice;
  if (frac <= 1) nice = 1;
  else if (frac <= 2) nice = 2;
  else if (frac <= 2.5) nice = 2.5;
  else if (frac <= 5) nice = 5;
  else nice = 10;
  return nice * base;
}

/** 把 max 切成 count 段，并去掉浮点尾巴。 */
export function ticks(max, count = 4) {
  const step = max / count;
  const out = [];
  for (let i = 0; i <= count; i += 1) out.push(Number((step * i).toFixed(6)));
  return out;
}

/**
 * 序列号 → 横向坐标。
 * 单点时居中，否则只除以 (n-1)。除以 n-1 而不是 n，才能让首末点贴住左右边缘。
 */
export function scaleX(index, length, left, width) {
  if (length <= 1) return left + width / 2;
  return left + (index / (length - 1)) * width;
}

/** 值 → 纵向坐标（已反转：越大越靠上）。
 *
 *  非有限值一律当 0。不能只靠 ``Math.min`` 夹区间：``Math.min(NaN, max)`` 仍是
 *  NaN，于是**一个脏数据点就会让整条折线消失**（图上什么都不画，但代码不报错）。
 */
export function scaleY(value, max, top, height) {
  if (!(max > 0)) return top + height;
  const v = Number.isFinite(value) ? value : 0;
  const clamped = Math.max(0, Math.min(v, max));
  return top + height - (clamped / max) * height;
}

/** 全平数据（max===min）时把线画在纵向中点，避免贴在某个边上像贴顶。 */
export function midY(max, top, height) {
  return top + height / 2;
}

/** 数据里最大的一组数值。 */
export function maxOf(series) {
  let max = 0;
  for (const row of series) {
    for (const value of row) {
      if (Number.isFinite(value) && value > max) max = value;
    }
  }
  return max;
}

function emptySvg(width, height, message) {
  return (
    `<svg class="chart" viewBox="0 0 ${fmt(width)} ${fmt(height)}" width="${fmt(width)}" ` +
    `height="${fmt(height)}" role="img" aria-label="${esc(message)}">` +
    `<rect x="0" y="0" width="${fmt(width)}" height="${fmt(height)}" fill="none" />` +
    `<text class="chart-empty" x="${fmt(width / 2)}" y="${fmt(height / 2)}" ` +
    `text-anchor="middle" dominant-baseline="middle">${esc(message)}</text>` +
    `</svg>`
  );
}

/* --------------------------------------------------------------- 悬停提示 --- */

/**
 * 估算一段文本的像素宽度。
 *
 * **为什么需要估算**：本模块的硬约定是「纯函数，数据进 SVG 字符串出」，所以拿不到
 * DOM，也就无法用 `getComputedTextLength()`。而提示框不定宽就会被内容撑破或留一大
 * 条空白。CJK 按 2 个单位、其余按 1 个单位算 —— 对「10-04 · Token 总量 12.3K」这类
 * 实际内容，误差在几个像素内，而提示框本身有 8px 内边距吸收它。
 */
export function estimateTextWidth(text, fontSize = 11) {
  // 数字 0 也要当「没有文本」：``String(0)`` 是 "0"，一个字宽 6.16px ——
  // 而调用方传0 的意思通常是「这个字段没值」，不是「要显示一个零」。
  if (text === null || text === undefined || text === '' || text === 0) return 0;
  let units = 0;
  for (const ch of String(text)) {
    units += /[\u2E80-\u9FFF\uFF00-\uFFEF]/.test(ch) ? 2 : 1;
  }
  return units * fontSize * 0.56;
}

/**
 * 一条悬停提示的盒子。
 *
 * 位置**必须夹在画布内**：折线图的提示框挂在数据点右侧，而最后一个点紧贴右边缘 ——
 * 不夹的话它有一半在 viewBox 外面。`.chart` 虽然是 `overflow: visible`，但那样会
 * 压到卡片边框上，看起来像布局坏了。
 *
 * @param {number} x锚点x（通常是被悬停的数据点）
 * @param {number} y 锚点 y
 * @param {string} title 标题（通常是日期）
 * @param {Array<{label:string, value:string, color?:string}>} rows
 * @param {{left:number, right:number, top:number, bottom:number}} bounds 可用范围
 */
export function tooltipBox(x, y, title, rows, bounds) {
  const padX = 9;
  const padY = 7;
  const lineH = 15;
  const titleH = 16;
  const swatchW = rows.some((r) => r.color) ? 12 : 0;

  const titleW = estimateTextWidth(title, 11.5) + padX * 2;
  const rowW = rows.reduce(
    (max, r) => Math.max(max, estimateTextWidth(`${r.label} ${r.value}`, 11) + padX * 2 + swatchW),
    0
  );
  const w = Math.max(titleW, rowW, 60);
  const h = padY * 2 + titleH + rows.length * lineH;

  // 先试着放右边，放不下就翻到左边；再夹进画布。
  let tx = x + 14;
  if (tx + w > bounds.right) tx = x - w - 14;
  if (tx < bounds.left) tx = bounds.left;
  if (tx + w > bounds.right) tx = Math.max(bounds.left, bounds.right - w);

  let ty = y - h - 12;
  if (ty < bounds.top) ty = y + 12;
  if (ty + h > bounds.bottom) ty = Math.max(bounds.top, bounds.bottom - h);

  const parts = [
    `<g class="hv-tip" transform="translate(${fmt(tx)} ${fmt(ty)})" pointer-events="none">`,
    `<rect class="hv-tip-bg" x="0" y="0" width="${fmt(w)}" height="${fmt(h)}" rx="6" ` +
      `fill="var(--tip-bg)" stroke="var(--tip-line)" stroke-width="1" />`,
    `<text x="${fmt(padX)}" y="${fmt(padY + 11)}" font-size="11.5" ` +
      `fill="var(--tip-title)">${esc(title)}</text>`,
  ];
  rows.forEach((r, i) => {
    const ly = padY + titleH + i * lineH + 10;
    if (r.color) {
      parts.push(
        `<rect x="${fmt(padX)}" y="${fmt(ly - 7)}" width="8" height="8" rx="2" ` +
          `fill="${esc(r.color)}" />`
      );
    }
    parts.push(
      `<text x="${fmt(padX + swatchW)}" y="${fmt(ly)}" font-size="11" fill="var(--tip-text)">` +
        `${esc(r.label)}</text>`,
      // 数值右对齐：不同位数的数字列成一条竖线，比「标签+空格+数字」好读得多
      `<text x="${fmt(w - padX)}" y="${fmt(ly)}" font-size="11" text-anchor="end" ` +
        `fill="var(--tip-value)" font-weight="600">${esc(r.value)}</text>`
    );
  });
  parts.push('</g>');
  return parts.join('');
}

/**
 * 一层悬停命中区。
 *
 * 每列是**一个透明矩形 + 提示框 + 竖直参考线**，纯 CSS 显隐（``.chart`` 里的
 * `.hv:hover .hv-tip { opacity: 1 }`）。不用 JS 监听 mousemove 的理由：
 * 本模块的纯函数约定一旦破掉，坐标缩放 / 空数据降级 / 除零保护这些最容易错的
 * 地方就没法在 node 里单测了（现在charts.test.mjs 有 60+ 项）。
 *
 * 命中区用 ``fill="transparent"`` + ``pointer-events="all"``：前者让它不可见但
 * 可命中（``fill="none"`` 的区域收不到鼠标事件），后者确保即使落在折线的空隙里
 * 也能触发。
 *
 * @param {number} n 数据点数
 * @param {(i:number)=>{x:number, y:number, label:string, rows:Array}} describe
 */
export function hoverLayer(n, describe, bounds, opts = {}) {
  const { slotWidth, guide = true, ariaLabel = '' } = opts;
  const parts = [];
  for (let i = 0; i < n; i += 1) {
    const d = describe(i);
    if (!d) continue;
    // 命中列以数据点为中心；首末列可能超出绘图区，所以反向多给半个 slot 的宽容度，
    // 否则贴边的那个点有一半点不到。
    const half = Math.max(slotWidth / 2, 6);
    const hx = Math.max(bounds.left - half, Math.min(d.x, bounds.right + half) - half);
    const hw = Math.max(12, Math.min(bounds.right + half, d.x + half) - hx);
    const screen = n > 1 ? d.rows.map((r) => `${r.label} ${r.value}`).join('，') : '';
    parts.push(
      `<g class="hv">`,
      guide
        ? `<line class="hv-guide" x1="${fmt(d.x)}" y1="${fmt(bounds.top)}" ` +
          `x2="${fmt(d.x)}" y2="${fmt(bounds.bottom)}" />`
        : '',
      `<rect class="hv-hit" x="${fmt(hx)}" y="${fmt(bounds.top)}" width="${fmt(hw)}" ` +
        `height="${fmt(bounds.bottom - bounds.top)}" fill="transparent" ` +
        `pointer-events="all" tabindex="0" ` +
        `aria-label="${esc(`${d.label}${screen ? '：' + screen : ''}`)}">` +
        // <title> 是原生提示，也是**键盘与读屏**用户的唯一入口 —— 不能只靠 hover。
        `<title>${esc(d.label)}${screen ? '\n' + esc(screen) : ''}</title>` +
        `</rect>`,
      tooltipBox(d.x, d.y, d.label, d.rows, bounds),
      `</g>`
    );
  }
  return parts.join('');
}

/* ------------------------------------------------------------ 折线 / 面积 --- */

/**
 * 多序列折线图（可叠面积）。
 *
 * @param {object} spec
 * @param {string[]} spec.labels      X 轴标签，长度 = 数据点数
 * @param {Array<{name:string, values:number[], color:string, area?:boolean}>} spec.series
 * @param {number} [spec.width]
 * @param {number} [spec.height]
 * @param {boolean} [spec.area]       是否给第一条序列填面积
 * @param {number} [spec.labelEvery]  X 轴每隔几个点显示一次标签
 * @param {string} [spec.emptyText]
 * @param {boolean} [spec.hover]      是否叠加悬停提示层（默认 true）
 * @param {(v:number)=>string} [spec.formatValue] 提示框里数值的格式化
 */
export function lineChart(spec) {
  const {
    labels = [],
    series = [],
    width = 720,
    height = 260,
    area = true,
    labelEvery = 0,
    emptyText = '暂无数据',
    hover = true,
    formatValue = null,
  } = spec;

  const points = labels.length;
  const usable = series.filter((s) => Array.isArray(s.values) && s.values.length);
  if (points === 0 || usable.length === 0) return emptySvg(width, height, emptyText);

  const pad = { top: 16, right: 16, bottom: 28, left: 52 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  const max = niceMax(maxOf(usable.map((s) => s.values)));
  const step = labelEvery > 0 ? labelEvery : Math.max(1, Math.ceil(points / 8));

  const parts = [];
  parts.push(
    `<defs>${usable
      .map(
        (s, i) =>
          `<linearGradient id="lc-fill-${i}" x1="0" y1="0" x2="0" y2="1">` +
          `<stop offset="0%" stop-color="${esc(s.color)}" stop-opacity="0.22" />` +
          `<stop offset="100%" stop-color="${esc(s.color)}" stop-opacity="0.02" />` +
          `</linearGradient>`
      )
      .join('')}</defs>`
  );

  // 横向网格线 + Y 轴刻度
  for (const value of ticks(max, 4)) {
    const y = scaleY(value, max, pad.top, plotH);
    parts.push(
      `<line x1="${fmt(pad.left)}" y1="${fmt(y)}" x2="${fmt(pad.left + plotW)}" y2="${fmt(y)}" ` +
        `stroke="var(--grid-line)" stroke-width="1" />`
    );
    parts.push(
      `<text x="${fmt(pad.left - 8)}" y="${fmt(y)}" text-anchor="end" dominant-baseline="middle" ` +
        `font-size="10" fill="var(--axis-text)">${esc(shortNum(value))}</text>`
    );
  }

  // X 轴标签
  labels.forEach((label, i) => {
    if (i % step !== 0 && i !== points - 1) return;
    const x = scaleX(i, points, pad.left, plotW);
    parts.push(
      `<text x="${fmt(x)}" y="${fmt(pad.top + plotH + 18)}" text-anchor="middle" ` +
        `font-size="10" fill="var(--axis-text)">${esc(label)}</text>`
    );
  });

  // 先记下每条序列每个点的真实 y，供悬停层用。**必须在画线之前算**。
  // 数据全平（max===min）时折线画在纵向中点，悬停点要跟线走而不是贴在底边。
  const ysBySeries = usable.map((s) => {
    const flat = s.values.every((v) => v === s.values[0]);
    return s.values.map((v, i) => scaleY(flat ? 0 : v, max, pad.top, plotH));
  });
  const flatAny = usable.map((s) => s.values.every((v) => v === s.values[0]));
  const yAt = (si, i) =>
    flatAny[si] ? midY(max, pad.top, plotH) : ysBySeries[si][i];

  usable.forEach((s, si) => {
    const values = s.values;
    const coords = values.map((v, i) => [scaleX(i, points, pad.left, plotW), ysBySeries[si][i]]);
    const flat = flatAny[si];
    const line = coords
      .map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${fmt(x)} ${fmt(flat ? midY(max, pad.top, plotH) : y)}`)
      .join(' ');

    if (area && si === 0 && points > 1) {
      const baseline = pad.top + plotH;
      const first = coords[0][0];
      const last = coords[coords.length - 1][0];
      const areaPath = `${line} L${fmt(last)} ${fmt(baseline)} L${fmt(first)} ${fmt(baseline)} Z`;
      parts.push(`<path d="${areaPath}" fill="url(#lc-fill-${si})" />`);
    }
    parts.push(
      `<path d="${line}" fill="none" stroke="${esc(s.color)}" stroke-width="1.8" ` +
        `stroke-linejoin="round" stroke-linecap="round" />`
    );
    // 只在数据点不太密时画点，否则会糊成一片
    if (points <= 32) {
      for (const [x, y] of coords) {
        parts.push(
          `<circle cx="${fmt(x)}" cy="${fmt(flat ? midY(max, pad.top, plotH) : y)}" ` +
            `r="2.4" fill="var(--paper-raised)" stroke="${esc(s.color)}" stroke-width="1.4" />`
        );
      }
    }
  });

  // 悬停层放在最后：命中矩形要盖在折线之上，否则点被压在下面收不到事件。
  if (hover) {
    const bounds = { left: pad.left, right: pad.left + plotW, top: pad.top, bottom: pad.top + plotH };
    parts.push(
      hoverLayer(
        points,
        (i) => ({
          x: scaleX(i, points, pad.left, plotW),
          // 锚在**最高**的那个点：提示框往上弹不会盖住它自己
          y: Math.min(...usable.map((_, si) => yAt(si, i))),
          label: labels[i],
          rows: usable.map((s) => ({
            label: s.name,
            value: formatValue ? formatValue(s.values[i]) : shortNum(s.values[i]),
            color: s.color,
          })),
        }),
        bounds,
        { slotWidth: plotW / Math.max(1, points - 1 || 1), ariaLabel: spec.label || '' }
      )
    );
  }

  return wrap(width, height, parts.join(''), spec.label || `折线图：${labels.length} 个数据点`);
}

/* ------------------------------------------------------------------- 柱 --- */

/**
 * 分组柱状图。
 *
 * @param {object} spec
 * @param {string[]} spec.labels
 * @param {Array<{name:string, values:number[], color:string}>} spec.series
 * @param {boolean} [spec.hover]      是否叠加悬停提示层（默认 true）
 * @param {(v:number)=>string} [spec.formatValue] 提示框里数值的格式化
 */
export function barChart(spec) {
  const {
    labels = [],
    series = [],
    width = 720,
    height = 240,
    emptyText = '暂无数据',
    labelEvery = 0,
    label = '',
    hover = true,
    formatValue = null,
  } = spec;

  const usable = series.filter((s) => Array.isArray(s.values) && s.values.length);
  const groups = usable.length ? Math.max(...usable.map((s) => s.values.length)) : 0;
  if (!labels.length || !usable.length || !groups) return emptySvg(width, height, emptyText);
  // 全 0 时每根柱的高度都是 0，一张图上什么都不会画出来 —— 那样只剩网格线，
  // 看起来像坏了而不是像「没数据」。直接给空态。
  if (maxOf(usable.map((s) => s.values)) <= 0) return emptySvg(width, height, emptyText);

  const pad = { top: 14, right: 16, bottom: 28, left: 48 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  const max = niceMax(maxOf(usable.map((s) => s.values)));
  const step = labelEvery > 0 ? labelEvery : Math.max(1, Math.ceil(labels.length / 10));

  const slot = plotW / labels.length;
  const gap = Math.min(4, slot * 0.18);
  const barW = Math.max(1, (slot - gap) / usable.length);
  const baseline = pad.top + plotH;
  const parts = [];

  for (const value of ticks(max, 3)) {
    const y = scaleY(value, max, pad.top, plotH);
    parts.push(
      `<line x1="${fmt(pad.left)}" y1="${fmt(y)}" x2="${fmt(pad.left + plotW)}" y2="${fmt(y)}" ` +
        `stroke="var(--grid-line)" stroke-width="1" />`
    );
    parts.push(
      `<text x="${fmt(pad.left - 8)}" y="${fmt(y)}" text-anchor="end" dominant-baseline="middle" ` +
        `font-size="10" fill="var(--axis-text)">${esc(shortNum(value))}</text>`
    );
  }

  labels.forEach((label, gi) => {
    usable.forEach((s, si) => {
      const value = s.values[gi] ?? 0;
      const x = pad.left + gi * slot + gap / 2 + si * barW;
      const y = scaleY(value, max, pad.top, plotH);
      const h = Math.max(0, baseline - y);
      if (h > 0) {
        parts.push(
          `<rect class="bar" x="${fmt(x)}" y="${fmt(y)}" width="${fmt(barW)}" height="${fmt(h)}" ` +
            `fill="${esc(s.color)}" rx="1" />`
        );
      }
    });
    if (gi % step === 0 || gi === labels.length - 1) {
      parts.push(
        `<text x="${fmt(pad.left + gi * slot + slot / 2)}" y="${fmt(baseline + 18)}" ` +
          `text-anchor="middle" font-size="10" fill="var(--axis-text)">${esc(label)}</text>`
      );
    }
  });

  // 悬停层：命中区按**分组**（一个日期一个），提示框里列该分组所有序列的值。
  // 放在最后 —— 命中矩形要盖在柱子之上，否则柱子收不到事件。
  if (hover) {
    const bounds = { left: pad.left, right: pad.left + plotW, top: pad.top, bottom: baseline };
    parts.push(
      hoverLayer(
        labels.length,
        (gi) => {
          const rows = usable.map((s) => ({
            label: s.name,
            value: formatValue ? formatValue(s.values[gi] ?? 0) : shortNum(s.values[gi] ?? 0),
            color: s.color,
          }));
          // 锚在最高那根柱的顶端；全 0 时锚在基线，提示框仍弹在图内
          const topY = Math.min(
            ...usable.map((s) => scaleY(s.values[gi] ?? 0, max, pad.top, plotH))
          );
          return {
            x: pad.left + gi * slot + slot / 2,
            y: topY >= baseline ? baseline : topY,
            label: labels[gi],
            rows,
          };
        },
        bounds,
        { slotWidth: slot }
      )
    );
  }

  return wrap(width, height, parts.join(''), label || `柱状图：${labels.length} 个分组`);
}

/* ------------------------------------------------------------------ 环形 --- */

/**
 * 环形图。切片用 stroke-dasharray 画，而不是 path 弧 —— 弧长计算在负角度、
 * 单切片 100%、以及总和为 0 这三个边界上都容易出错，dasharray 天然免疫。
 *
 * @param {object} spec
 * @param {Array<{label:string, value:number, color:string}>} spec.items
 * @param {number} [spec.size]
 * @param {string} [spec.centerLabel]
 * @param {string} [spec.emptyText]
 */
export function donutChart(spec) {
  const {
    items = [],
    size = 190,
    thickness = 26,
    centerValue = '',
    centerLabel = '',
    emptyText = '暂无用量',
  } = spec;

  const usable = items.filter((it) => Number.isFinite(it.value) && it.value > 0);
  const total = usable.reduce((sum, it) => sum + it.value, 0);
  if (!usable.length || total <= 0) return emptySvg(size, size, emptyText);

  const radius = size / 2 - thickness / 2 - 2;
  const circumference = 2 * Math.PI * radius;
  const cx = size / 2;
  const cy = size / 2;
  const parts = [];

  // 底环：让「未用满」的部分也有形
  parts.push(
    `<circle cx="${fmt(cx)}" cy="${fmt(cy)}" r="${fmt(radius)}" fill="none" ` +
      `stroke="var(--paper-sunken)" stroke-width="${fmt(thickness)}" />`
  );

  let offset = 0;
  usable.forEach((item, i) => {
    const fraction = item.value / total;
    const length = fraction * circumference;
    // 扇形本身是 ``stroke-dasharray`` 画的，命中区就是这一段圆环。
    // ``pointer-events: stroke`` 让只有那条环收得到鼠标 —— 环内的空洞不该触发。
    parts.push(
      `<circle class="hv hv-donut" cx="${fmt(cx)}" cy="${fmt(cy)}" r="${fmt(radius)}" fill="none" ` +
        `stroke="${esc(item.color)}" stroke-width="${fmt(thickness)}" ` +
        `stroke-dasharray="${fmt(length)} ${fmt(circumference - length)}" ` +
        `stroke-dashoffset="${fmt(-offset)}" ` +
        `transform="rotate(-90 ${fmt(cx)} ${fmt(cy)})" ` +
        `stroke-linecap="butt" pointer-events="stroke" tabindex="0" ` +
        `aria-label="${esc(`${item.label}：${shortNum(item.value)}，占 ${(fraction * 100).toFixed(1)}%`)}">` +
        // 原生 <title> 保持**只有名称 + 数值**：它出现在浏览器的悬浮提示里，
        // 空间小、且会被读屏念出来，多加百分比会让那句变成一长串数字。
        // 占比放在下面的富提示框里—— 那里才是「想看细节」的地方。
        `<title>${esc(item.label)} ${esc(shortNum(item.value))}</title>` +
        tooltipBox(
          // 锚在扇形中点：角度 0 在 12 点方向，顺着圆周走 ``-90`` 已由transform 处理
          cx + Math.cos((offset / circumference) * 2 * Math.PI - Math.PI / 2) * radius,
          cy + Math.sin((offset / circumference) * 2 * Math.PI - Math.PI / 2) * radius,
          item.label,
          [
            { label: '数值', value: shortNum(item.value), color: item.color },
            { label: '占比', value: `${(fraction * 100).toFixed(1)}%` },
            { label: '合计', value: shortNum(total) },
          ],
          { left: 0, right: size, top: 0, bottom: size }
        ) +
        `</circle>`
    );
    offset += length;
    void i;
  });

  if (centerValue !== '' || centerLabel !== '') {
    parts.push(
      `<text class="donut-center-value" x="${fmt(cx)}" y="${fmt(cy - 2)}" text-anchor="middle" ` +
        `dominant-baseline="middle" font-size="20">${esc(centerValue)}</text>`
    );
    parts.push(
      `<text class="donut-center-label" x="${fmt(cx)}" y="${fmt(cy + 18)}" text-anchor="middle" ` +
        `font-size="11">${esc(centerLabel)}</text>`
    );
  }

  const summary = usable.map((it) => `${it.label} ${shortNum(it.value)}`).join('，');
  return wrap(size, size, parts.join(''), spec.label || `环形图：${summary}`);
}

/* -------------------------------------------------------------- 迷你走势 --- */

/**
 * 卡片里的迷你走势。单序列、无坐标轴，用极简的面积折线。
 * 数据全平时画一条中线而不是贴底 —— 那才是「没变化」的正确视觉。
 */
export function sparkline(values, options = {}) {
  const {
    width = 200, height = 30, color = 'var(--series-1)', fill = true, label = '',
  } = options;
  const points = Array.isArray(values) ? values.filter((v) => Number.isFinite(v)) : [];
  if (points.length === 0) return emptySvg(width, height, '');

  const max = Math.max(...points);
  const min = Math.min(...points);
  const span = max - min;
  const pad = 3;
  const usableH = Math.max(1, height - pad * 2);
  // span===0（全平）时画在纵向中点。早期版本给 min 兜了个 0，导致全平数据的
  // span 变成非零，线被贴到顶部 —— 「没变化」看起来像「一路飙升」。
  const y = (v) => pad + (span === 0 ? usableH / 2 : usableH - ((v - min) / span) * usableH);
  const x = (i) => (points.length === 1 ? width / 2 : (i / (points.length - 1)) * width);

  const line = points.map((v, i) => `${i === 0 ? 'M' : 'L'}${fmt(x(i))} ${fmt(y(v))}`).join(' ');
  const areaPath = `${line} L${fmt(width)} ${fmt(height)} L0 ${fmt(height)} Z`;
  // **颜色必须一起进 hash**：同屏两张迷你图（总览的「今日请求」与「今日 Token」）
  // 数据完全可能相同而颜色不同，只按几何路径算 id 会让两张图共用一个
  // ``url(#id)`` —— SVG 的 id 是文档级全局的，两边都解析到**第一个**渐变，
  // 于是「Token」卡的面积被涂成「请求」卡的颜色。
  const id = `sp-${Math.abs(hash(`${color}|${line}`)).toString(36)}`;
  const parts = [];

  if (fill) {
    parts.push(
      `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">` +
        `<stop offset="0%" stop-color="${esc(color)}" stop-opacity="0.28" />` +
        `<stop offset="100%" stop-color="${esc(color)}" stop-opacity="0" />` +
        `</linearGradient></defs>`,
      `<path d="${areaPath}" fill="url(#${id})" />`
    );
  }
  parts.push(
    `<path d="${line}" fill="none" stroke="${esc(color)}" stroke-width="1.4" ` +
      `stroke-linejoin="round" stroke-linecap="round" />`,
    `<circle cx="${fmt(x(points.length - 1))}" cy="${fmt(y(points[points.length - 1]))}" ` +
      `r="2" fill="${esc(color)}" />`
  );
  return wrap(width, height, parts.join(''), label || '迷你走势图');
}

function hash(text) {
  let h = 0;
  for (let i = 0; i < text.length; i += 1) {
    h = (h * 31 + text.charCodeAt(i)) | 0;
  }
  return h;
}

/* ---------------------------------------------------------------- 辅助 --- */

/** 轴上的短数字：12345 → 12.3K。
 *
 *  阈值定在 1e4 而不是 1e3：1500 就该写「1500」，写成「1.5K」是丢精度。
 *  小数位固定 1 位 —— 走通用 :func:`fmt` 会得到 ``99.999K`` 这种三小数。
 */
export function shortNum(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '0';
  const abs = Math.abs(n);
  const one = (x) => Math.round(x * 10) / 10;
  if (abs >= 1e9) return `${one(n / 1e9)}B`;
  if (abs >= 1e6) return `${one(n / 1e6)}M`;
  if (abs >= 1e4) return `${one(n / 1e3)}K`;
  return fmt(n);
}

/** 包一层 <svg>。
 *
 * ``role="img"`` 必须配可访问名，否则读屏只会念「图像」—— 环形图里的
 * ``<title>`` 是鼠标提示，不是 ``role="img"`` 的命名来源。所以每个调用方都要
 * 自己传一句人话。
 */
function wrap(width, height, inner, label = '') {
  return (
    `<svg class="chart" xmlns="${NS}" viewBox="0 0 ${fmt(width)} ${fmt(height)}" ` +
    `width="100%" height="${fmt(height)}" preserveAspectRatio="xMidYMid meet" ` +
    `role="img" aria-label="${esc(label)}">${inner}</svg>`
  );
}

/** 图例 HTML（不是 SVG），便于点选联动。 */
export function legend(items, kind = 'box') {
  return items
    .map(
      (it) =>
        `<span class="chart-legend-item">` +
        `<span class="${kind === 'line' ? 'chart-swatch-line' : 'chart-swatch'}" ` +
        `style="background:${esc(it.color)}"></span>` +
        `<span>${esc(it.label)}</span>` +
        // value 为 null 与「没给 value」一样按「无数值」处理：``Number(null)`` 是 0，
        // 直接丢给 shortNum 会在图例上凭空多出一个「0」
        (it.value !== undefined && it.value !== null
          ? `<span class="muted num">${esc(shortNum(it.value))}</span>`
          : '') +
        `</span>`
    )
    .join('');
}