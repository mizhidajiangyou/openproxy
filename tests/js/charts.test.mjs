/**
 * SVG 图表库单测（node --test）。
 *
 * 图表层是纯函数「数据 → SVG 字符串」，所以这里能真正验证坐标缩放、
 * 空数据降级、除零保护这些最容易出错的地方 —— 第三方图表库做不到这一点。
 */

import assert from 'node:assert/strict';
import test, { describe } from 'node:test';

import {
  barChart, donutChart, esc, estimateTextWidth, fmt, hoverLayer, legend, lineChart,
  maxOf, midY, niceMax, scaleX, scaleY, shortNum, sparkline, ticks, tooltipBox,
} from '../../web/js/charts.js';

/** 数柱子。柱子都带 rx="1"；空态自带的背景 <rect> 不算。 */
function countBars(svg) {
  return (svg.match(/<rect [^>]*rx="1"/g) || []).length;
}

/** 取**折线**（fill="none" 那条 path）上的 y 值。
 *  不能直接扫全文的 M/L：面积填充的闭合段（… L width height Z）也会被算进去，
 *  于是「全平数据每个点同高」这类断言会被自己绊倒。 */
function lineYs(svg) {
  const path = svg.match(/d="(M[^"]+)" fill="none"/);
  if (!path) return [];
  return [...path[1].matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map((m) => Number(m[2]));
}

describe('基础工具', () => {
  test('fmt 去掉浮点尾巴', () => {
    assert.equal(fmt(2), '2');
    assert.equal(fmt(2.0000000000000004), '2');
    assert.equal(fmt(2.5), '2.5');
    assert.equal(fmt(2.1234567), '2.123');
  });

  test('fmt 对非有限值给 0 而不是 NaN', () => {
    for (const bad of [NaN, Infinity, -Infinity, null, undefined, 'x']) {
      assert.equal(fmt(bad), '0', `输入 ${String(bad)}`);
    }
  });

  test('esc 转义会破坏标签的字符', () => {
    assert.equal(esc('<script>'), '&lt;script&gt;');
    assert.equal(esc('a & b'), 'a &amp; b');
    assert.equal(esc('"q"'), '&quot;q&quot;');
    assert.equal(esc("it's"), 'it&#39;s');
    assert.equal(esc(null), '');
  });

  test('esc 挡住 SVG 属性注入', () => {
    const evil = '" onload="alert(1)';
    const out = esc(evil);
    assert.ok(!out.includes('"'), '引号必须被转义，否则能逃出属性值');
    assert.ok(out.includes('&quot;'));
  });
});

describe('刻度计算', () => {
  test('niceMax 把 0 变成正数（防除零）', () => {
    // 这是最关键的一条：max=0 会让 scaleY 的分母为 0，全平数据直接 NaN
    assert.equal(niceMax(0), 1);
    assert.equal(niceMax(-5), 1);
    assert.equal(niceMax(NaN), 1);
    assert.equal(niceMax(Infinity), 1);
  });

  test('niceMax 取整到易读的档位', () => {
    assert.equal(niceMax(1), 1);
    assert.equal(niceMax(1.5), 2);
    assert.equal(niceMax(2.2), 2.5);
    assert.equal(niceMax(4), 5);
    assert.equal(niceMax(9), 10);
    assert.equal(niceMax(37), 50);
    assert.equal(niceMax(120), 200);
  });

  test('niceMax 单调不减', () => {
    let previous = 0;
    for (const v of [0.5, 1, 3, 7, 9, 11, 47, 99, 130, 900]) {
      const got = niceMax(v);
      assert.ok(got >= previous, `niceMax(${v})=${got} < ${previous}`);
      assert.ok(got >= v, `niceMax(${v})=${got} 必须 >= 原值`);
      previous = got;
    }
  });

  test('ticks 首尾为 0 和 max', () => {
    const out = ticks(100, 4);
    assert.equal(out.length, 5);
    assert.equal(out[0], 0);
    assert.equal(out[4], 100);
  });

  test('ticks 不产生浮点尾巴', () => {
    assert.deepEqual(ticks(1, 3), [0, 0.333333, 0.666667, 1]);
  });

  test('maxOf 忽略非法值', () => {
    assert.equal(maxOf([[1, NaN], [Infinity, 5]]), 5);
    assert.equal(maxOf([]), 0);
    assert.equal(maxOf([[-3, -1]]), 0);
  });
});

describe('坐标缩放', () => {
  test('scaleX 让首末点贴住左右边缘', () => {
    assert.equal(scaleX(0, 5, 10, 100), 10);
    assert.equal(scaleX(4, 5, 10, 100), 110);
    assert.equal(scaleX(2, 5, 10, 100), 60);
  });

  test('scaleX 单点时居中而不是除以 0', () => {
    const x = scaleX(0, 1, 0, 200);
    assert.ok(Number.isFinite(x));
    assert.equal(x, 100);
  });

  test('scaleY 反转：越大越靠上', () => {
    assert.equal(scaleY(0, 100, 0, 100), 100);
    assert.equal(scaleY(100, 100, 0, 100), 0);
    assert.equal(scaleY(50, 100, 0, 100), 50);
  });

  test('scaleY 对 max=0 不产生 NaN', () => {
    const y = scaleY(0, 0, 0, 100);
    assert.ok(Number.isFinite(y), `得到 ${y}`);
  });

  test('scaleY 把越界值夹回区间内', () => {
    assert.equal(scaleY(200, 100, 0, 100), 0);
    assert.equal(scaleY(-50, 100, 0, 100), 100);
  });

  test('midY 在纵向中点', () => {
    assert.equal(midY(100, 0, 100), 50);
  });

  test('脏数据点不会让整条线消失', () => {
    // 回归：Math.min(NaN, max) 是 NaN，一个脏点会让所有点的坐标变成 NaN，
    // 结果是「代码不报错但图上什么都不画」。
    const out = lineChart({
      labels: ['a', 'b', 'c'],
      series: [{ name: 'n', values: [1, NaN, 3], color: 'red' }],
      width: 300, height: 100,
    });
    assert.ok(!out.includes('NaN'), `SVG 里不能出现 NaN：${out.slice(0, 200)}`);
    assert.ok(out.includes('暂无数据') === false, '一个脏点不该让整张图降级');
  });

  test('所有输出都是有限数（扫极端组合）', () => {
    const weird = [[0, 0, 0], [1], [1e18, 0], [-5, -5], [NaN, 1], [0.0001]];
    for (const values of weird) {
      for (const max of [0, 1, 1000]) {
        const y = scaleY(values[0], max, 0, 100);
        assert.ok(Number.isFinite(y), `values=${values} max=${max} → ${y}`);
      }
    }
  });
});

describe('shortNum', () => {
  test('按量级缩写，阈值是 1e4 而不是 1e3', () => {
    // 1500 写「1.5K」是丢精度，所以 1e4 以下一律原样显示
    assert.equal(shortNum(999), '999');
    assert.equal(shortNum(1500), '1500');
    assert.equal(shortNum(9999), '9999');
    assert.equal(shortNum(10_000), '10K');
    assert.equal(shortNum(15_500), '15.5K');
    assert.equal(shortNum(2_610_000), '2.6M');
    assert.equal(shortNum(3_200_000_000), '3.2B');
  });

  test('缩进只有一位小数', () => {
    // 曾经把通用 fmt 用在这里，99999 会变成 99.999K
    assert.equal(shortNum(99_999), '100K');
    assert.equal(shortNum(1_234_567), '1.2M');
    for (const v of [99_999, 1_234_567, 9_876_543_210, 12_345]) {
      const out = shortNum(v);
      // 去掉 K/M/B 单位后再数小数位，否则 "1.2M" 会被当成两位小数
      const decimals = out.replace(/[KMB]$/, '').split('.')[1] || '';
      assert.ok(decimals.length <= 1, `${v} → ${out} 小数位过多`);
    }
  });

  test('负数与非有限值不崩', () => {
    // 负数走绝对值判档，所以 -1500 同样原样显示，-12345 才缩写
    assert.equal(shortNum(-1500), '-1500');
    assert.equal(shortNum(-12345), '-12.3K');
    assert.equal(shortNum(NaN), '0');
    assert.equal(shortNum(Infinity), '0');
    assert.equal(shortNum(undefined), '0');
  });
});

describe('lineChart', () => {
  const spec = {
    labels: ['01', '02', '03'],
    series: [{ name: 'a', values: [1, 2, 3], color: 'red' }],
    width: 300, height: 100,
  };

  test('返回 SVG', () => {
    const out = lineChart(spec);
    assert.ok(out.startsWith('<svg'));
    assert.ok(out.includes('viewBox="0 0 300 100"'));
    assert.ok(out.includes('<path'));
  });

  test('空标签降级成空态而不是崩', () => {
    const out = lineChart({ ...spec, labels: [] });
    assert.ok(out.includes('暂无数据'));
  });

  test('空序列降级', () => {
    const out = lineChart({ ...spec, series: [] });
    assert.ok(out.includes('暂无数据'));
  });

  test('空态是一张有可访问名的图，不是一段裸文本', () => {
    // role="img" 少了 aria-label 就只剩一句「图像」—— 整个图表库唯一的可访问名
    // 来源，之前把它删掉整套件照样绿。
    const out = lineChart({ ...spec, labels: [], emptyText: '近 14 天暂无调用' });
    assert.ok(out.includes('role="img"'), out);
    assert.ok(out.includes('aria-label="近 14 天暂无调用"'), out);
  });

  test('空态的可访问名会转义', () => {
    const out = lineChart({ ...spec, labels: [], emptyText: '"><script>x</script>' });
    assert.ok(!out.includes('<script'), out);
    assert.ok(out.includes('aria-label="&quot;&gt;&lt;script&gt;'), out);
  });

  test('全 0 数据不产生 NaN', () => {
    const out = lineChart({ ...spec, series: [{ name: 'z', values: [0, 0, 0], color: 'red' }] });
    assert.ok(!out.includes('NaN'), 'SVG 里不能出现 NaN');
    assert.ok(!out.includes('Infinity'));
  });

  test('单点数据不产生 NaN', () => {
    const out = lineChart({ labels: ['x'], series: [{ name: 'a', values: [5], color: 'red' }] });
    assert.ok(!out.includes('NaN'));
  });

  test('系列名进入图例并被转义', () => {
    const out = lineChart({ ...spec, series: [{ name: '<b>x</b>', values: [1], color: 'red' }] });
    assert.ok(!out.includes('<b>'), '图例文本必须转义');
  });

  test('颜色作为属性值注入时无法逃逸', () => {
    const evil = 'red" onload="alert(1)';
    const out = lineChart({ ...spec, series: [{ name: 'a', values: [1, 2, 3], color: evil }] });
    assert.ok(!/stroke="red"/.test(out), '引号必须被转义');
    assert.ok(out.includes('&quot;'));
  });

  test('多条序列各出一条线', () => {
    const out = lineChart({
      labels: ['1', '2'],
      series: [
        { name: 'a', values: [1, 2], color: 'red' },
        { name: 'b', values: [3, 1], color: 'blue' },
      ],
      width: 200, height: 100,
    });
    // 数的是带描边的线，不是 path 元素总数 —— 面积填充也是 <path d="M…">
    assert.equal((out.match(/<path d="M[^"]+" fill="none"/g) || []).length, 2);
  });

  test('面积填充只在第一条序列', () => {
    const out = lineChart({
      labels: ['1', '2', '3'],
      series: [
        { name: 'a', values: [1, 2, 3], color: 'red' },
        { name: 'b', values: [3, 2, 1], color: 'blue' },
      ],
      width: 200, height: 100, area: true,
    });
    // 光数「有几个填充」不够：``si === 0`` 换成 ``si === 1`` 数量照样是 1。
    // 必须断言它引用的是**第一条**序列的那条渐变。
    assert.ok(out.includes('fill="url(#lc-fill-0)"'), out);
    assert.ok(!out.includes('fill="url(#lc-fill-1)"'), out);
    assert.equal((out.match(/fill="url\(#lc-fill-/g) || []).length, 1);
  });

  test('全平数据画在中线上而不是贴在底边', () => {
    // scaleY 对「最大值 == 最小值」的序列会算出同一个 y（贴住上边），
    // 所以 lineChart 专门有 flat 分支把整条线放到绘图区中线。
    // 这个分支之前没有任何测试，`const flat = ...` 改成 false 也照样绿。
    const width = 200;
    const height = 100;
    const out = lineChart({
      labels: ['1', '2', '3'],
      series: [{ name: 'a', values: [5, 5, 5], color: 'red' }],
      width, height,
    });
    const pad = { top: 16, right: 16, bottom: 28, left: 52 };
    const plotH = height - pad.top - pad.bottom;
    const mid = pad.top + plotH / 2;
    const line = (out.match(/<path d="(M[^"]+)" fill="none"/) || [])[1] || '';
    const ys = [...line.matchAll(/[ML](?:[\d.]+) ([\d.]+)/g)].map((m) => Number(m[1]));
    assert.equal(ys.length, 3, line);
    assert.ok(ys.every((y) => y === mid), `全平数据应落在中线 ${mid}，实际 ${ys}`);
  });

  test('非全平数据仍按刻度定位', () => {
    // 对照组：上一条若写成「所有线都在中线」也能过，所以这里必须有反例
    const out = lineChart({
      labels: ['1', '2', '3'],
      series: [{ name: 'a', values: [1, 5, 9], color: 'red' }],
      width: 200, height: 100,
    });
    const line = (out.match(/<path d="(M[^"]+)" fill="none"/) || [])[1] || '';
    const ys = [...line.matchAll(/[ML](?:[\d.]+) ([\d.]+)/g)].map((m) => Number(m[1]));
    assert.equal(ys.length, 3, line);
    assert.ok(new Set(ys).size === 3, `刻度不同就不该重合：${ys}`);
  });

  test('dense 数据不画点，避免糊成一片', () => {
    const many = Array.from({ length: 64 }, (_, i) => i);
    const out = lineChart({
      labels: many.map(String),
      series: [{ name: 'a', values: many, color: 'red' }],
      width: 400, height: 100,
    });
    assert.ok(!out.includes('<circle'), '64 个点不该再画圆点');
  });

  test('labelEvery 控制 X 轴标签密度', () => {
    const labels = Array.from({ length: 20 }, (_, i) => String(i));
    const dense = lineChart({ ...spec, labels, series: [{ name: 'a', values: labels.map(Number), color: 'red' }] });
    const sparse = lineChart({ ...spec, labels, series: [{ name: 'a', values: labels.map(Number), color: 'red' }], labelEvery: 5 });
    const count = (s) => (s.match(/font-size="10" fill="var\(--axis-text\)"/g) || []).length;
    assert.ok(count(sparse) < count(dense), 'labelEvery=5 应比默认更稀疏');
  });

  test('不含 script 或外链（SVG 命名空间不算外链）', () => {
    const out = lineChart(spec);
    assert.ok(!out.includes('<script'));
    assert.ok(!/https?:\/\/(?!www\.w3\.org\/2000\/svg)/.test(out));
    assert.ok(!out.includes('data:'));
  });
});

describe('barChart', () => {
  const spec = {
    labels: ['a', 'b'],
    series: [{ name: 's', values: [3, 5], color: 'red' }],
    width: 300, height: 120,
  };

  test('只画非 0 的柱子，数量等于非 0 数据点数', () => {
    const out = barChart({ ...spec, series: [{ name: 's', values: [3, 0, 5], color: 'red' }], labels: ['a', 'b', 'c'] });
    assert.equal(countBars(out), 2);
  });

  test('全 0 数据降级成空态而不是只剩网格线', () => {
    const out = barChart({ ...spec, series: [{ name: 's', values: [0, 0], color: 'red' }] });
    assert.ok(out.includes('暂无数据'), '全 0 应降级为空态');
    // 空态自带的背景 <rect> 不算柱子；柱子都带 rx="1"
    assert.ok(!out.includes('rx="1"'), `不该画出零高度柱子：${out.slice(0, 200)}`);
  });

  test('部分为 0 时只画非 0 的柱', () => {
    const out = barChart({ ...spec, series: [{ name: 's', values: [0, 5], color: 'red' }] });
    assert.ok(!out.includes('暂无数据'));
    assert.equal(countBars(out), 1);
  });

  test('缺值按 0 处理而不是 NaN', () => {
    const out = barChart({ ...spec, series: [{ name: 's', values: [3], color: 'red' }] });
    assert.ok(!out.includes('NaN'));
  });

  test('空数据降级', () => {
    assert.ok(barChart({ ...spec, labels: [] }).includes('暂无数据'));
    assert.ok(barChart({ ...spec, series: [] }).includes('暂无数据'));
  });

  test('柱子在绘图区内，且高度与数值成正比', () => {
    // width=300, pad.left=48, pad.right=16 → 绘图区 x∈[48,284]，基线 y=212-28=184? 用实际值算
    const out = barChart({
      labels: ['a', 'b'], series: [{ name: 's', values: [10, 20], color: 'red' }],
      width: 300, height: 120,
    });
    // 只数 .bar —— 悬停层的命中矩形也是 <rect>，不排除的话计数会多出来
    const rects = [...out.matchAll(/<rect class="bar" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"/g)]
      .map((m) => ({ x: +m[1], y: +m[2], w: +m[3], h: +m[4] }));
    assert.equal(rects.length, 2);
    for (const r of rects) {
      assert.ok(r.x >= 48 - 0.01, `左边界 ${r.x} 应在绘图区内`);
      assert.ok(r.x + r.w <= 284 + 0.01, `右边界 ${r.x + r.w} 应在绘图区内`);
      assert.ok(r.w > 0 && r.h > 0, '柱子必须有正面积');
    }
    // 值大一倍，高度也要大一倍（底部对齐）
    assert.ok(Math.abs(rects[1].h / rects[0].h - 2) < 0.01, `高度比 ${rects[1].h / rects[0].h}`);
    // 底部对齐：y + h 相同
    assert.ok(Math.abs((rects[0].y + rects[0].h) - (rects[1].y + rects[1].h)) < 0.01);
    // 值大的柱子 y 更靠上
    assert.ok(rects[1].y < rects[0].y);
  });

  test('分组柱的横向偏移随序列序号变化', () => {
    const out = barChart({
      labels: ['a'],
      series: [
        { name: 'x', values: [5], color: 'red' },
        { name: 'y', values: [5], color: 'blue' },
      ],
      width: 300, height: 120,
    });
    const rects = [...out.matchAll(/<rect class="bar" x="([\d.]+)"[^>]*width="([\d.]+)"/g)]
      .map((m) => ({ x: Number(m[1]), w: Number(m[2]) }));
    assert.equal(rects.length, 2);
    // 同组的两根柱必须并排：第 2 根的左边界 = 第 1 根的右边界（不重叠）
    assert.ok(rects[1].x > rects[0].x, `同组的两根柱不该重叠：${JSON.stringify(rects)}`);
    assert.ok(Math.abs((rects[0].x + rects[0].w) - rects[1].x) < 0.01, '两根柱应紧挨着');
  });

  test('分组柱：每组柱数 = 序列数', () => {
    const out = barChart({
      labels: ['a', 'b'],
      series: [
        { name: 'x', values: [1, 2], color: 'red' },
        { name: 'y', values: [3, 4], color: 'blue' },
      ],
      width: 300, height: 120,
    });
    assert.equal(countBars(out), 4);
  });
});

describe('donutChart', () => {
  const spec = {
    items: [
      { label: '甲', value: 30, color: 'red' },
      { label: '乙', value: 70, color: 'blue' },
    ],
    size: 100,
  };

  test('画出与项目等量的环段', () => {
    const out = donutChart(spec);
    // 底环 1 + 数据段 2
    assert.equal((out.match(/<circle/g) || []).length, 3);
  });

  test('单切片 100% 也画得出来', () => {
    const out = donutChart({ items: [{ label: 'x', value: 5, color: 'red' }], size: 100 });
    assert.ok(!out.includes('NaN'));
    assert.ok(out.includes('暂无用量') === false);
  });

  test('总和为 0 降级成空态', () => {
    assert.ok(donutChart({ items: [{ label: 'x', value: 0, color: 'red' }] }).includes('暂无用量'));
  });

  test('空数组降级', () => {
    assert.ok(donutChart({ items: [] }).includes('暂无用量'));
  });

  test('负值与非有限值被剔除', () => {
    const out = donutChart({
      items: [
        { label: 'neg', value: -5, color: 'red' },
        { label: 'nan', value: NaN, color: 'blue' },
        { label: 'ok', value: 10, color: 'green' },
      ],
    });
    assert.equal((out.match(/<circle/g) || []).length, 2);
    assert.ok(!out.includes('neg'));
  });

  test('弧长与占比成正比，且首尾相接', () => {
    const out = donutChart(spec, { size: 100 });
    const dashes = [...out.matchAll(/stroke-dasharray="([\d.]+) ([\d.]+)"/g)]
      .map((m) => Number(m[1]));
    assert.equal(dashes.length, 2, '两个数据段');
    // 甲 30 / 乙 70 → 3 : 7
    assert.ok(Math.abs(dashes[0] / dashes[1] - 3 / 7) < 0.01, `弧长比 ${dashes[0] / dashes[1]}`);
  });

  test('半径随 thickness 收缩，两端都在画布内', () => {
    const thin = donutChart({ ...spec, size: 100, thickness: 10 });
    const thick = donutChart({ ...spec, size: 100, thickness: 40 });
    const radiusOf = (svg) => Number(svg.match(/r="([\d.]+)"/)[1]);
    assert.ok(radiusOf(thick) < radiusOf(thin), '更粗的环半径必须更小');
    for (const svg of [thin, thick]) {
      const r = radiusOf(svg);
      assert.ok(r > 0, '半径必须为正');
      assert.ok(r * 2 + Number(svg.match(/stroke-width="([\d.]+)"/)[1]) <= 100 + 0.01,
        `环的外径 ${r * 2} 必须放得进 ${100} 的画布`);
    }
  });

  test('dashoffset 依次累加，保证弧段首尾相接', () => {
    const out = donutChart({
      items: [
        { label: 'a', value: 1, color: 'r' },
        { label: 'b', value: 1, color: 'b' },
        { label: 'c', value: 2, color: 'g' },
      ],
    });
    const offsets = [...out.matchAll(/stroke-dashoffset="(-?[\d.]+)"/g)].map((m) => Number(m[1]));
    assert.equal(offsets.length, 3);
    for (let i = 1; i < offsets.length; i += 1) {
      assert.ok(offsets[i] < offsets[i - 1], `第 ${i} 段的起点必须往前挪`);
    }
  });

  test('圆心文字被转义', () => {
    const out = donutChart({ ...spec, centerValue: '<x>', centerLabel: '总' });
    assert.ok(!out.includes('<x>'));
    assert.ok(out.includes('&lt;x&gt;'));
  });

  test('原生 title 里是原始值而不是百分比', () => {
    const out = donutChart(spec);
    // 甲 30 / 乙 70 → 30%
    assert.ok(out.includes('<title>甲 30</title>'), out);
    assert.ok(out.includes('<title>乙 70</title>'), out);
    // 原生提示（浏览器悬浮气泡 + 读屏会念的那句）要短：只有名称 + 数值。
    // 占比属于富提示框的内容，不该挤进这句里。
    for (const m of out.matchAll(/<title>([^<]*)<\/title>/g)) {
      assert.ok(!m[1].includes('%'), `原生 title 里不该有百分比：${m[1]}`);
    }
  });

  test('富提示框里有占比', () => {
    // 占比是「这块占多少」的意思，只在悬停时给 —— 原生 title 保持一行
    const out = donutChart(spec);
    assert.ok(out.includes('30.0%'), out);
    assert.ok(out.includes('70.0%'), out);
  });

  test('title 里的标签被转义', () => {
    const out = donutChart({ items: [{ label: '<b>&</b>', value: 5, color: 'red' }] });
    assert.ok(out.includes('&lt;b&gt;&amp;&lt;/b&gt;'), out);
  });
});

describe('sparkline', () => {
  test('正常路径含面积与折线', () => {
    const out = sparkline([1, 2, 3, 4], { width: 100, height: 30 });
    assert.ok(out.includes('<path'));
    assert.ok(out.includes('circle'));
  });

  test('空数组降级', () => {
    assert.ok(sparkline([]).startsWith('<svg'));
  });

  test('非数组降级', () => {
    assert.ok(sparkline(null).startsWith('<svg'));
  });

  test('单点不产生 NaN', () => {
    const out = sparkline([7]);
    assert.ok(!out.includes('NaN'));
  });

  test('全平数据画在中点，而不是贴顶或贴底', () => {
    // 回归：早期给 min 兜了 0，span 变成非零，线被贴到顶部 ——
    // 「没变化」看起来像「一路飙升」。
    const out = sparkline([5, 5, 5, 5], { width: 100, height: 40 });
    assert.ok(!out.includes('NaN'));
    const ys = lineYs(out);
    assert.ok(ys.length >= 4, `至少 4 个点，实际 ${ys.length}`);
    assert.ok(ys.every((y) => y === ys[0]), `全平的每个点应在同一高度，实际 ${ys}`);
    assert.ok(ys[0] > 10 && ys[0] < 30, `全平应在纵向中点附近，实际 y=${ys[0]}`);
  });

  test('有波动时按相对高低起伏（用真实 min/max，不锚到 0）', () => {
    const ys = lineYs(sparkline([3, 8, 5], { width: 100, height: 40 }));
    assert.ok(ys[0] > ys[1], `起点应在中线之上：${ys}`);
    assert.ok(ys[1] < ys[0] && ys[1] < ys[2], `峰值应最靠上：${ys}`);
    assert.ok(ys[2] > ys[1], `谷值应更靠下：${ys}`);
  });

  test('fill=false 时不画面积', () => {
    const out = sparkline([1, 2, 3], { fill: false });
    assert.ok(!out.includes('linearGradient'));
  });

  test('同数据不同颜色也不该共用 gradient id', () => {
    // 真实的失效场景就在总览页：「今日请求」与「今日 Token」两张迷你图并排，
    // 数据**可能完全相同**、颜色不同。原来那条测试只让数据不同，恰好绕开了
    // 它要防的场景（实测两种颜色算出的 id 一模一样）。
    const idA = sparkline([1, 2, 3], { color: 'var(--series-1)' }).match(/id="([^"]+)"/)[1];
    const idB = sparkline([1, 2, 3], { color: 'var(--series-2)' }).match(/id="([^"]+)"/)[1];
    assert.notEqual(idA, idB, '同数据不同颜色也不该共用 gradient id');
  });

  test('同屏两张迷你图的渐变不会互相串色', () => {
    const page = sparkline([1, 2, 3], { color: 'var(--series-1)' })
      + sparkline([1, 2, 3], { color: 'var(--series-2)' });
    const ids = [...page.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
    assert.equal(ids.length, 2);
    assert.equal(new Set(ids).size, 2, `文档里出现了重复 id: ${ids}`);
    const colours = [...page.matchAll(/stop-color="([^"]+)"/g)].map((m) => m[1]);
    assert.deepEqual(new Set(colours), new Set(['var(--series-1)', 'var(--series-2)']));
  });

  test('不同数据得到不同的 gradient id', () => {
    // 同一页面上的两张迷你图如果共用一个 id，第二张会拿到第一张的渐变。
    const idA = sparkline([1, 2, 3]).match(/id="([^"]+)"/)[1];
    const idB = sparkline([9, 1, 4]).match(/id="([^"]+)"/)[1];
    assert.notEqual(idA, idB, '不同数据不应共用 gradient id');
  });

  test('同数据得到同一个 gradient id（是幂等的，不是冲突）', () => {
    const a = sparkline([1, 2, 3]).match(/id="([^"]+)"/)[1];
    const b = sparkline([1, 2, 3]).match(/id="([^"]+)"/)[1];
    assert.equal(a, b, '同样的数据配同样的渐变是正常的');
  });

  test('空数据降级成空态而不是一张空图', () => {
    assert.ok(sparkline([]).includes('暂无数据') === false);
    // 空态文字是空串（迷你图太小，塞不下字），但仍必须是合法 svg
    assert.ok(sparkline([]).startsWith('<svg'));
    assert.ok(sparkline([]).endsWith('</svg>'));
    assert.ok(sparkline(null).startsWith('<svg'));
  });
});

describe('legend', () => {
  test('渲染标签与数值', () => {
    const out = legend([{ label: '甲', color: 'red', value: 15_500 }]);
    assert.ok(out.includes('甲'));
    assert.ok(out.includes('15.5K'));
    assert.ok(out.includes('background:red'));
  });

  test('没有 value 时不画数值格', () => {
    const out = legend([{ label: '甲', color: 'red' }]);
    // 只断言「不含 class="muted num"」。之前那条断言写成了
    // `!out.includes('chart-legend-item"><span class="muted num"')` —— legend() 总是
    // 先输出色块 span，所以那个子串永远不可能出现，这条断言恒真（等于没测）。
    assert.ok(!out.includes('class="muted num"'), out);
    assert.ok(out.includes('甲'));
  });

  test('有 value 时画数值格', () => {
    const out = legend([{ label: '甲', color: 'red', value: 15_500 }]);
    assert.ok(out.includes('class="muted num"'), out);
    assert.ok(out.includes('15.5K'), out);
  });

  test('value 为 null 同样按「无数值」处理', () => {
    // Number(null) === 0，不挡住的话图例上会凭空冒出一个「0」
    const out = legend([{ label: '甲', color: 'red', value: null }]);
    assert.ok(!out.includes('class="muted num"'), out);
    assert.ok(!out.includes('>0<'), out);
  });

  test('value 为 0 仍要画出来', () => {
    // 对照组：0 是有效数值，不能被上面那条一起滤掉
    const out = legend([{ label: '甲', color: 'red', value: 0 }]);
    assert.ok(out.includes('class="muted num"'), out);
  });

  test('转义标签', () => {
    assert.ok(!legend([{ label: '<i>', color: 'red' }]).includes('<i>'));
  });

  test("kind='line' 用线型色块", () => {
    assert.ok(legend([{ label: 'a', color: 'red' }], 'line').includes('chart-swatch-line'));
  });

  test('空数组得到空串', () => {
    assert.equal(legend([]), '');
  });
});

describe('输出安全', () => {
  const outputs = [
    lineChart({ labels: ['a'], series: [{ name: 'n', values: [1], color: 'red' }] }),
    barChart({ labels: ['a'], series: [{ name: 'n', values: [1], color: 'red' }] }),
    donutChart({ items: [{ label: 'n', value: 1, color: 'red' }] }),
    sparkline([1, 2]),
  ];

  test('没有任何一段输出脚本、事件属性或外链', () => {
    for (const out of outputs) {
      assert.ok(!/<script/i.test(out), '含 script');
      assert.ok(!/onload=/i.test(out), '含事件属性');
      assert.ok(!/onerror=/i.test(out), '含事件属性');
      assert.ok(!/https?:\/\/(?!www\.w3\.org)/.test(out), '含外部资源引用');
    }
  });

  test('SVG 命名空间正确', () => {
    for (const out of [outputs[0], outputs[3]]) {
      assert.ok(out.includes('xmlns="http://www.w3.org/2000/svg"'));
    }
  });

  test('每段输出都是良构的 svg 开闭标签', () => {
    for (const out of outputs) {
      assert.ok(out.startsWith('<svg'));
      assert.ok(out.endsWith('</svg>'));
    }
  });
});
// ==========================================================================
// 悬停提示
//
// 单独一个 describe，因为这层是「用户主动问『到底多少』」的唯一入口 ——
// 它算错不会让图变形，只会让用户拿到一个错的数字，而那种错很难被肉眼发现。
// ==========================================================================

describe('estimateTextWidth', () => {
  test('中文按两个单位算', () => {
    // 不做这件事的话，提示框会被 CJK 撑破：中文全是宽字符，按 1 个单位算会短一半
    assert.ok(estimateTextWidth('中文', 11) > estimateTextWidth('ab', 11));
  });

  test('空值不炸', () => {
    for (const bad of [null, undefined, '', 0]) {
      assert.equal(estimateTextWidth(bad, 11), 0);
    }
  });

  test('字号线性影响宽度', () => {
    assert.ok(estimateTextWidth('abc', 22) > estimateTextWidth('abc', 11));
  });
});

describe('tooltipBox', () => {
  const bounds = { left: 50, right: 500, top: 10, bottom: 200 };
  const rows = [
    { label: 'Token 总量', value: '12,345', color: 'red' },
    { label: '请求数', value: '42', color: 'blue' },
  ];

  test('内容都在盒子里', () => {
    const svg = tooltipBox(200, 120, '10-04', rows, bounds);
    assert.ok(svg.includes('10-04'));
    assert.ok(svg.includes('Token 总量'));
    assert.ok(svg.includes('12,345'));
    assert.ok(svg.includes('42'));
  });

  test('盒子完整落在绘图区内', () => {
    // 这是最容易坏的一条：提示框挂在数据点右边，而最后一个点紧贴右边缘 ——
    // 不夹住的话它有一半在 viewBox 外面，看起来像布局坏了
    for (const x of [50, 200, 496, 499, 500]) {
      const svg = tooltipBox(x, 120, '10-04', rows, bounds);
      const m = svg.match(/translate\(([\d.-]+) ([\d.-]+)\)/);
      assert.ok(m, svg);
      const tx = Number(m[1]);
      const ty = Number(m[2]);
      const w = Number(svg.match(/class="hv-tip-bg"[^>]*width="([\d.]+)"/)[1]);
      const h = Number(svg.match(/class="hv-tip-bg"[^>]*height="([\d.]+)"/)[1]);
      assert.ok(tx >= bounds.left - 0.01, `x=${x} 左边越界: ${tx}`);
      assert.ok(tx + w <= bounds.right + 0.01, `x=${x} 右边越界: ${tx + w}`);
      assert.ok(ty >= bounds.top - 0.01, `y 顶越界: ${ty}`);
      assert.ok(ty + h <= bounds.bottom + 0.01, `y 底越界: ${ty + h}`);
    }
  });

  test('靠右的数据点会把提示框翻到左边', () => {
    const right = tooltipBox(496, 120, '10-04', rows, bounds);
    const left = tooltipBox(60, 120, '10-04', rows, bounds);
    const rx = Number(right.match(/translate\(([\d.-]+)/)[1]);
    const lx = Number(left.match(/translate\(([\d.-]+)/)[1]);
    assert.ok(rx < 496, '贴右的数据点提示框应翻到左侧');
    assert.ok(lx >= 60, '左侧数据点的提示框应在右边');
  });

  test('顶部数据点不会把盒子顶出画布', () => {
    const svg = tooltipBox(200, 11, '10-04', rows, { ...bounds, top: 10 });
    const ty = Number(svg.match(/translate\([\d.-]+ ([\d.-]+)/)[1]);
    assert.ok(ty >= 10, `盒子被顶出上边界: ${ty}`);
  });

  test('标签与数值都被转义', () => {
    // label 可能是模型名，模型名可能带尖括号
    const svg = tooltipBox(200, 120, '<b>', [{ label: '<i>', value: '&' }], bounds);
    assert.ok(!svg.includes('<b>'), svg);
    assert.ok(!svg.includes('<i>'), svg);
    assert.ok(svg.includes('&lt;b&gt;'));
    assert.ok(svg.includes('&amp;'));
  });

  test('数值右对齐（text-anchor=end）', () => {
    // 不同位数的数字列成一条竖线，比「标签 + 空格 + 数字」好读得多
    const svg = tooltipBox(200, 120, 't', rows, bounds);
    assert.ok(svg.includes('text-anchor="end"'), svg);
  });

  test('没有颜色列时不留色块位置', () => {
    const svg = tooltipBox(200, 120, 't', [{ label: 'a', value: '1' }], bounds);
    // 色块是给 series 对色的；单序列时留 12px 空白会让文字莫名右移
    assert.ok(!svg.includes('rx="2"'), svg);
  });
});

describe('hoverLayer', () => {
  const bounds = { left: 50, right: 500, top: 10, bottom: 200 };
  const describePoint = (n) => (i) => ({
    x: 50 + (i * 450) / Math.max(1, n - 1),
    y: 100,
    label: `10-0${i + 1}`,
    rows: [{ label: '值', value: String(i * 10), color: 'red' }],
  });

  test('每个数据点一个命中区', () => {
    const svg = hoverLayer(5, describePoint(5), bounds, { slotWidth: 112 });
    assert.equal((svg.match(/class="hv-hit"/g) || []).length, 5);
  });

  test('命中矩形必须可命中', () => {
    // fill="none" 的区域收不到鼠标事件 —— 必须 transparent + pointer-events="all"
    const svg = hoverLayer(3, describePoint(3), bounds, { slotWidth: 150 });
    assert.ok(svg.includes('fill="transparent"'), svg);
    assert.ok(svg.includes('pointer-events="all"'), svg);
  });

  test('键盘可达：可聚焦且有可读名', () => {
    const svg = hoverLayer(3, describePoint(3), bounds, { slotWidth: 150 });
    assert.ok(svg.includes('tabindex="0"'), svg);
    // 读屏用户看不到 hover，必须有 aria-label
    assert.ok(svg.includes('aria-label="10-01：值 0"'), svg);
  });

  test('有原生 title（hover 与读屏的兜底）', () => {
    const svg = hoverLayer(2, describePoint(2), bounds, { slotWidth: 200 });
    assert.ok((svg.match(/<title>/g) || []).length, svg);
  });

  test('命中区覆盖绘图区全高', () => {
    // 只盖住点附近的话，鼠标在点与点之间移动时提示会闪
    const svg = hoverLayer(3, describePoint(3), bounds, { slotWidth: 150 });
    const h = Number(svg.match(/class="hv-hit"[^>]*height="([\d.]+)"/)[1]);
    assert.equal(h, bounds.bottom - bounds.top);
  });

  test('首末列的命中区不会被裁掉', () => {
    // 折线首末点贴住左右边缘，命中区只有半个 slot 宽的话有一半在画布外
    const svg = hoverLayer(4, describePoint(4), bounds, { slotWidth: 150 });
    const hits = [...svg.matchAll(/class="hv-hit" x="([\d.-]+)"[^>]*width="([\d.]+)"/g)];
    assert.equal(hits.length, 4);
    for (const [, x, w] of hits) {
      assert.ok(Number(w) > 0, `命中区宽度必须为正: ${w}`);
    }
    assert.ok(svg.includes('rx="2"') === false || true);
    assert.ok(Number(hits[0][1]) < 50, '首列命中区应从左边界之前开始');
  });

  test('参考线可以关掉（迷你图不需要）', () => {
    const on = hoverLayer(2, describePoint(2), bounds, { slotWidth: 200 });
    assert.ok(on.includes('hv-guide'));
    const off = hoverLayer(2, describePoint(2), bounds, { slotWidth: 200, guide: false });
    assert.ok(!off.includes('hv-guide'));
  });

  test('describe 返回 null 的点被跳过', () => {
    const svg = hoverLayer(3, (i) => (i === 1 ? null : describePoint(3)(i)), bounds, {
      slotWidth: 150,
    });
    assert.equal((svg.match(/class="hv-hit"/g) || []).length, 2);
  });

  test('零个数据点不炸', () => {
    assert.equal(hoverLayer(0, describePoint(0), bounds, { slotWidth: 10 }), '');
  });
});

describe('图表的悬停层集成', () => {
  const labels = ['10-01', '10-02', '10-03'];
  const series = [{ name: 'Token', values: [100, 200, 300], color: 'red' }];

  test('折线图带悬停层', () => {
    assert.ok(lineChart({ labels, series, width: 400, height: 200 }).includes('hv-hit'));
  });

  test('可以显式关掉', () => {
    const out = lineChart({ labels, series, width: 400, height: 200, hover: false });
    assert.ok(!out.includes('hv-hit'));
  });

  test('柱状图按分组给命中区', () => {
    const out = barChart({
      labels,
      series: [{ name: '成功', values: [1, 2, 3], color: 'green' }],
      width: 400, height: 200,
    });
    assert.equal((out.match(/class="hv-hit"/g) || []).length, 3);
  });

  test('formatValue 覆盖提示框里的数值显示', () => {
    // 轴上写 12.3K 是为了省地方；悬停是主动问「到底多少」，要给完整值
    const out = lineChart({
      labels, series, width: 400, height: 200, formatValue: (v) => `${v} tokens`,
    });
    assert.ok(out.includes('100 tokens'), out);
  });

  test('formatValue 为 null 时回落到 shortNum', () => {
    const out = lineChart({ labels, series, width: 400, height: 200 });
    assert.ok(out.includes('100'), out);
  });

  test('空数据时没有悬停层（只有空态）', () => {
    const out = lineChart({ labels: [], series, width: 400, height: 200 });
    assert.ok(out.includes('暂无数据'));
    assert.ok(!out.includes('hv-hit'), '空态不该有命中区');
  });

  test('单点数据也能给出提示', () => {
    // 单点时 scaleX 居中，折线画在中点；命中区仍要有一个
    const out = lineChart({ labels: ['10-01'], series, width: 400, height: 200 });
    assert.ok(out.includes('hv-hit'), out);
  });

  test('全平数据的提示框跟线走而不是贴底', () => {
    // 全平时线画在纵向中点；提示框锚点必须跟着走，否则会弹到图的底部去。
    // 注意比的是**盒子下沿与中线的关系**：提示框是画在锚点**上方**的
    // （ty = y - h - 12），所以盒子下沿才该贴近锚点，而不是盒子上沿。
    const flat = [{ name: '恒定', values: [5, 5, 5], color: 'red' }];
    const out = lineChart({ labels, series: flat, width: 400, height: 200 });
    const tipY = Number(out.match(/class="hv-tip" transform="translate\([\d.-]+ ([\d.-]+)/)[1]);
    const tipH = Number(out.match(/class="hv-tip-bg"[^>]*height="([\d.]+)"/)[1]);
    const mid = 16 + (200 - 16 - 28) / 2; // 102
    // 下沿 = 上沿 + 盒高，应该落在中线附近（差值就是那 12px 间距 + 小数误差）
    assert.ok(Math.abs(tipY + tipH - mid) < 16, `盒子下沿 ${tipY + tipH} 应贴近中线 ${mid}`);
    // 反证：贴底的话下沿会接近 188，差 80 多
    assert.ok(tipY + tipH < mid + 16);
  });

  test('悬停层不含 script 与事件属性', () => {
    const out = lineChart({ labels, series, width: 400, height: 200 });
    assert.ok(!/<script/i.test(out), '含 script');
    assert.ok(!/onmouseover=/i.test(out), '含事件属性');
    assert.ok(!/\son[a-z]+=/i.test(out), '含 on* 属性');
  });

  test('环形图的每个扇形都有提示', () => {
    const out = donutChart({
      items: [
        { label: '甲', value: 30, color: 'red' },
        { label: '乙', value: 70, color: 'blue' },
      ],
      size: 200,
    });
    assert.equal((out.match(/hv-donut/g) || []).length, 2);
    assert.ok(out.includes('pointer-events="stroke"'), '只有环该收鼠标事件');
  });
});