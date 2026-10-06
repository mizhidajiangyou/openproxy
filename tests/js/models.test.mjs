/**
 * 模型页的展示逻辑单测。
 *
 * 只测纯函数：token 格式化、可达性三态、思考标记。渲染出来的 DOM 与真实
 * 后端字段的对应关系由「字段契约」那条静态测试 + 真浏览器验证覆盖。
 */

import assert from 'node:assert/strict';
import test, { describe } from 'node:test';

import {
  newestReachabilityAt, reachabilityTag, reasoningTag, tokens,
} from '../../web/js/pages/models.js';

describe('tokens（上下文 / 输出上限的显示）', () => {
  test('按量级缩写', () => {
    // 这四个数字是 models.dev 给的真实值，全部来自本项目的静态清单
    assert.equal(tokens(1_048_576), '1M');
    assert.equal(tokens(1_000_000), '1M');
    assert.equal(tokens(262_144), '262.1K');
    assert.equal(tokens(200_000), '200K');
    assert.equal(tokens(32_768), '32.8K');
  });

  test('小数只留一位', () => {
    assert.equal(tokens(1_048_576), '1M', '不该显示成 1.0M');
    assert.equal(tokens(131_072), '131.1K');
  });

  test('未知值显示横杠而不是 0', () => {
    // 「0」会被读成「这个模型没有上下文」，与「数据缺失」是两件事
    for (const bad of [0, null, undefined, NaN, -1, 'x']) {
      assert.equal(tokens(bad), '—', `输入 ${String(bad)}`);
    }
  });

  test('小于 1000 的原样显示', () => {
    assert.equal(tokens(512), '512');
  });
});

describe('可达性标记', () => {
  test('三种状态各说各话', () => {
    // 「上游在线」和「站外可调」是两件事：/v1/models 返回 id 不代表调得通
    assert.match(reachabilityTag({ reachability: { status: 'ok' } }), /站外可调/);
    assert.match(reachabilityTag({ reachability: { status: 'blocked' } }), /站外被拒/);
    assert.match(reachabilityTag({ reachability: { status: 'unknown' } }), /可达性未探测/);
  });

  test('没探测过（字段为 null）也要有话说', () => {
    assert.match(reachabilityTag({ reachability: null }), /可达性未探测/);
    assert.match(reachabilityTag({}), /可达性未探测/);
  });

  test('blocked 与 ok 的样式不同', () => {
    // 两者都带 tag-danger 就会被当成同一件事 —— 而它们的处置完全相反
    assert.match(reachabilityTag({ reachability: { status: 'blocked' } }), /tag-danger/);
    assert.match(reachabilityTag({ reachability: { status: 'ok' } }), /tag-ok/);
  });

  test('上游的 detail 被转义后进 title', () => {
    const html = reachabilityTag({
      reachability: { status: 'blocked', detail: '"><script>alert(1)</script>' },
    });
    assert.ok(!html.includes('<script'), 'detail 必须转义：它来自上游响应');
  });
});

describe('思考级别标记', () => {
  test('支持与不支持要能一眼区分', () => {
    assert.match(reasoningTag({ reasoning: true }), /支持思考级别/);
    assert.match(reasoningTag({ reasoning: false }), /不支持思考/);
    // 缺字段时按「不支持」处理：宁可说少了，不要让人以为能调
    assert.match(reasoningTag({}), /不支持思考/);
  });
});

describe('newestReachabilityAt', () => {
  test('取最新的那个时间戳', () => {
    const items = [
      { reachability: { checked_at: 100 } },
      { reachability: { checked_at: 900 } },
      { reachability: { checked_at: 300 } },
    ];
    assert.equal(newestReachabilityAt(items), 900);
  });

  test('没探测过返回 0（而不是 undefined）', () => {
    // 调用方要拿它算「多久之前」，undefined 会变成 NaN
    assert.equal(newestReachabilityAt([{ reachability: null }, {}]), 0);
    assert.equal(newestReachabilityAt([]), 0);
  });
});