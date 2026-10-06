/**
 * 「走 opencode / 直通」开关的**交互逻辑**单测。
 *
 * ## 为什么单独一个文件
 *
 * `models.test.mjs` 只测纯函数（token 格式化、可达性三态）。
 * 而第三轮 review 用变异测试发现：并发保护的修复**零覆盖** ——
 * 去掉`if (busy.saving) return`、改成只禁被点的那个开关、去掉顺序归一，
 * 三个变异都**没有任何测试失败**。原因是逻辑埋在 DOM 事件回调里，
 * 而 `node --test` 没有 DOM。
 *
 * 所以把「算下一份配置」抽成纯函数 `nextViaOpencode`（它本来就该是纯的），
 * 在这里直接断言。剩下的「busy 守卫」用**读源码**的方式钉住 ——
 * 不是什么好办法，但比零覆盖强，而且变异测试能验证它有效。
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test, { describe } from 'node:test';

import { nextViaOpencode } from '../../web/js/pages/models.js';

const SRC = readFileSync(
  fileURLToPath(new URL('../../web/js/pages/models.js', import.meta.url)),
  'utf8',
);

const ORDER = ['big-pickle', 'fledge-alpha-free', 'space-bunny-free'];

describe('nextViaOpencode（下一份配置怎么算）', () => {
  test('勾上：加入该模型', () => {
    assert.deepEqual(
      nextViaOpencode(['fledge-alpha-free'], ORDER, 'big-pickle', true),
      ['big-pickle', 'fledge-alpha-free'],
    );
  });

  test('取消：移除该模型', () => {
    assert.deepEqual(
      nextViaOpencode(['big-pickle', 'fledge-alpha-free'], ORDER, 'big-pickle', false),
      ['fledge-alpha-free'],
    );
  });

  test('取消一个不在集合里的模型：结果不变', () => {
    assert.deepEqual(
      nextViaOpencode(['big-pickle'], ORDER, 'space-bunny-free', false),
      ['big-pickle'],
    );
  });

  test('勾一个已经在集合里的：幂等', () => {
    assert.deepEqual(
      nextViaOpencode(['big-pickle'], ORDER, 'big-pickle', true),
      ['big-pickle'],
    );
  });

  test('**顺序按模型清单，不是点击顺序**', () => {
    // 先勾 space-bunny-free 再勾 big-pickle：清单顺序是
    // [big-pickle, fledge-alpha-free, space-bunny-free]，所以结果是
    // [big-pickle, space-bunny-free] —— 与点击顺序相反。
    const step1 = nextViaOpencode([], ORDER, 'space-bunny-free', true);
    const step2 = nextViaOpencode(step1, ORDER, 'big-pickle', true);
    assert.deepEqual(step2, ['big-pickle', 'space-bunny-free']);
    // 反向验证：真的不是点击顺序
    assert.notDeepEqual(step2, ['space-bunny-free', 'big-pickle']);
  });

  test('清单之外的模型 id 不会进结果', () => {
    // 用户点了某个不在清单里的模型（理论上不会，但数据结构上允许）——
    // 它不该出现在提交里，否则后端会存下一个永远匹配不到的名字。
    const out = nextViaOpencode([], ORDER, 'not-in-catalog', true);
    assert.deepEqual(out, []);
  });

  test('空快照 + 取消 = 空', () => {
    assert.deepEqual(nextViaOpencode([], ORDER, 'big-pickle', false), []);
  });

  test('**返回值总是新数组**（不能是快照的引用）', () => {
    // 若是同一引用，调用方后续改动会污染渲染快照。
    const snapshot = ['big-pickle'];
    const out = nextViaOpencode(snapshot, ORDER, 'fledge-alpha-free', true);
    assert.notEqual(out, snapshot);
    snapshot.push('space-bunny-free');
    assert.deepEqual(out, ['big-pickle', 'fledge-alpha-free']);
  });

  test('输入是 Set 也能工作', () => {
    // 渲染时用的是 `new Set(...)`，所以要接受可迭代对象。
    assert.deepEqual(
      nextViaOpencode(new Set(['big-pickle']), ORDER, 'space-bunny-free', true),
      ['big-pickle', 'space-bunny-free'],
    );
  });
});

describe('并发保护（用源码断言，因为逻辑在 DOM 回调里）', () => {
  test('点击处理器里有 busy 守卫', () => {
    assert.match(
      SRC,
      /if \(busy\.saving\) return;/,
      '并发守卫不见了 —— 两个点击会各自从同一份快照算配置，后到的覆盖先到的',
    );
  });

  test('保存期间禁用**全部**开关，不只是被点的那个', () => {
    // 只禁被点的那个的话，用户能接着点第二张卡，而那次点击被守卫静默忽略 ——
    // 界面毫无反应，用户只会以为坏了。
    assert.match(
      SRC,
      /view\.querySelectorAll\('\[data-act="via-opencode"\]'\)/,
      '没有禁用全部开关 —— 用户能点第二个，而它会被静默忽略',
    );
  });

  test('finally 里重置 busy 标志', () => {
    // 不重置的话，一次失败之后整个页面的开关就永久点不动了。
    assert.match(SRC, /finally \{[\s\S]{0,400}?busy\.saving = false;/,
      'finally 里没有重置 busy —— 一次失败后开关会永久失效');
  });

  test('用抽出的纯函数而不是内联算配置', () => {
    assert.match(SRC, /nextViaOpencode\(/,
      '点击处理器没有用 nextViaOpencode —— 内联算的话排序与并发规则无法单测');
  });
});

describe('切页保护（第三轮 review 的 B-3）', () => {
  test('重绘前检查令牌是否仍有效', () => {
    assert.match(SRC, /if \(isCurrent\(token\)\) await renderModels\(view\);/,
      '重绘没有令牌守卫 —— PATCH 飞行中切页会把新页面覆盖成模型页');
  });

  test('cleanup 里作废令牌', () => {
    assert.match(SRC, /return \(\) => \{[\s\S]{0,300}?pageToken \+= 1;/,
      'cleanup 没有作废令牌 —— 切页后本页的异步重绘仍会执行');
  });
});
