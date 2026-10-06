/**
 * 前端格式化与 API 客户端单测。
 *
 * 这里能测的是**纯逻辑**：格式化函数的空值/负数/极端值行为、query 拼装、
 * FastAPI 两种错误体的解析。DOM 交互靠真实浏览器验证（见 README 的验证一节）。
 */

import assert from 'node:assert/strict';
import test, { describe } from 'node:test';

import { ApiError, qs } from '../../web/js/api.js';
import {
  ago, bindOnce, bytes, clientBaseUrl, compact, debounce, duration, esc, int, percent,
  shortDay, stamp, unbindAll,
} from '../../web/js/ui.js';

describe('int', () => {
  test('千分位', () => {
    assert.equal(int(0), '0');
    assert.equal(int(7665), '7,665');
    assert.equal(int(1234567), '1,234,567');
  });

  test('空值与非法值显示横杠而不是 0', () => {
    // 「没有数据」和「真的是 0」必须能区分，否则报表会把缺失当成没有用量
    for (const bad of [null, undefined, NaN, Infinity, 'x']) {
      assert.equal(int(bad), '-', `输入 ${String(bad)}`);
    }
  });

  test('小数四舍五入', () => {
    assert.equal(int(3.6), '4');
    assert.equal(int(-3.6), '-4');
  });
});

describe('compact', () => {
  test('按量级缩写', () => {
    assert.equal(compact(999), '999');
    assert.equal(compact(26_100_000), '26.1M');
    assert.equal(compact(172_900), '172.9K');
    assert.equal(compact(235_800), '235.8K');
  });

  test('空值显示横杠', () => {
    assert.equal(compact(null), '-');
    assert.equal(compact(NaN), '-');
  });

  test('负数可用', () => {
    assert.equal(compact(-26_100_000), '-26.1M');
  });
});

describe('duration', () => {
  test('毫秒与秒切换', () => {
    assert.equal(duration(860), '860ms');
    assert.equal(duration(999), '999ms');
    assert.equal(duration(1000), '1.00s');
    assert.equal(duration(1500), '1.50s');
    assert.equal(duration(24_540), '24.54s');
  });

  test('空值', () => {
    assert.equal(duration(null), '-');
    assert.equal(duration(NaN), '-');
  });
});

describe('bytes', () => {
  test('三级单位', () => {
    assert.equal(bytes(512), '512 B');
    assert.equal(bytes(1536), '1.5 KB');
    assert.equal(bytes(5 * 1024 * 1024), '5.00 MB');
  });

  test('每一级的边界都落在正确的一侧', () => {
    // 阈值差一个字节就会显示成上一级，看起来像整整差 1000 倍
    assert.equal(bytes(1023), '1023 B');
    assert.equal(bytes(1024), '1.0 KB');
    assert.equal(bytes(1024 * 1024 - 1), '1024.0 KB');
    assert.equal(bytes(1024 * 1024), '1.00 MB');
    assert.equal(bytes(0), '0 B');
  });

  test('空值', () => {
    assert.equal(bytes(null), '-');
    assert.equal(bytes(NaN), '-');
  });
});

describe('percent', () => {
  test('0 与非法值给 0.0% 而不是 NaN%', () => {
    // 失败率的分母在「今天没人调用」时是 0，算出来必须是 0.0% 而不是 NaN%
    assert.equal(percent(0), '0.0%');
    assert.equal(percent(null), '0.0%');
    assert.equal(percent(NaN), '0.0%');
  });

  test('正常换算', () => {
    assert.equal(percent(0.1234), '12.3%');
    assert.equal(percent(1), '100.0%');
    assert.equal(percent(0.1234, 2), '12.34%');
  });
});

describe('stamp / shortDay', () => {
  test('补零的日期时间', () => {
    const d = new Date(2026, 9, 3, 8, 5, 0);
    assert.equal(stamp(d.getTime()), '2026-10-03 08:05');
  });

  test('空值', () => {
    assert.equal(stamp(0), '-');
    assert.equal(stamp(null), '-');
  });

  test('shortDay 截短到 MM-DD', () => {
    assert.equal(shortDay('2026-10-03'), '10-03');
    assert.equal(shortDay(''), '');
  });
});

describe('ago', () => {
  test('相对时间分档', () => {
    const now = Date.now();
    assert.equal(ago(now - 5_000), '刚刚');
    assert.equal(ago(now - 5 * 60_000), '5 分钟前');
    assert.equal(ago(now - 3 * 3_600_000), '3 小时前');
    assert.equal(ago(now - 2 * 86_400_000), '2 天前');
  });

  test('超过 30 天回落到绝对时间', () => {
    assert.match(ago(Date.now() - 40 * 86_400_000), /^\d{4}-\d{2}-\d{2} /);
  });

  test('未来时间不显示「负 X 前」', () => {
    // 时钟漂移会让 ts 落在未来；显示「-3 天前」比显示绝对时间更难懂
    const future = Date.now() + 3 * 86_400_000;
    assert.match(ago(future), /^\d{4}-\d{2}-\d{2} /);
  });

  test('空值', () => {
    assert.equal(ago(0), '-');
  });
});

describe('esc', () => {
  test('转义会破坏渲染的字符', () => {
    assert.equal(esc('<b>'), '&lt;b&gt;');
    assert.equal(esc('a & b'), 'a &amp; b');
    assert.equal(esc("it's"), 'it&#39;s');
  });

  test('转义引号 —— 它有 14 处被用在属性位置', () => {
    // charts.esc 测过引号，ui.esc 没测：两个实现是各自一份，必须都钉住
    assert.equal(esc('"'), '&quot;');
    assert.ok(!esc('" onload="alert(1)').includes('"'));
  });

  test('挡不住脚本注入', () => {
    // 用户可命名的字段（密钥名、备注）会进 innerHTML
    assert.ok(!esc('<img src=x onerror=alert(1)>').includes('<img'));
  });
});

describe('clientBaseUrl', () => {
  const loc = (over = {}) => ({
    origin: 'http://127.0.0.1:8787', protocol: 'http:',
    hostname: '127.0.0.1', port: '8787', ...over,
  });

  test('后端已经给绝对地址，直接用（这就是那个 bug）', () => {
    // 曾经把 location.origin 又拼上去，指南页把两段地址粘在一起显示，填进客户端立刻连不上
    assert.equal(
      clientBaseUrl('http://127.0.0.1:8787/v1', loc()),
      'http://127.0.0.1:8787/v1',
    );
    assert.equal(
      clientBaseUrl('http://192.168.1.7:9000/v1', loc()),
      'http://192.168.1.7:9000/v1',
    );
  });

  test('通配绑定换成可达地址，且端口跟着 location 走', () => {
    // 0.0.0.0 填进客户端是连不上的，必须换成浏览器实际用的那个 host
    assert.equal(
      clientBaseUrl('http://0.0.0.0:8787/v1', loc()),
      'http://127.0.0.1:8787/v1',
    );
    assert.equal(
      clientBaseUrl('http://[::]:8787/v1', loc()),
      'http://127.0.0.1:8787/v1',
    );
    // 反代在 443 上时 location.port 是空的：不能把内网端口漏出去，
    // 协议也必须跟着页面走 —— hint 说的是 http，客户端要用 https
    assert.equal(
      clientBaseUrl('http://0.0.0.0:8787/v1', loc({
        origin: 'https://gw.example.com', protocol: 'https:',
        hostname: 'gw.example.com', port: '',
      })),
      'https://gw.example.com/v1',
    );
    // hint 没给协议时退回自己的（纯 http 部署）
    assert.equal(
      clientBaseUrl('http://0.0.0.0:8787/v1', loc({
        origin: 'http://10.0.0.9:8787', hostname: '10.0.0.9', port: '8787',
      })),
      'http://10.0.0.9:8787/v1',
    );
  });

  test('末尾多余的斜杠与空白都清掉', () => {
    assert.equal(clientBaseUrl('  http://h:1/v1/  ', loc()), 'http://h:1/v1');
    assert.equal(clientBaseUrl('http://h:1/v1//', loc()), 'http://h:1/v1');
  });

  test('缺 host 或非法值退回当前 origin（宁可保守也不能给废地址）', () => {
    for (const bad of [null, undefined, '', '   ', '127.0.0.1:8787/v1', '/v1', 'not a url']) {
      assert.equal(clientBaseUrl(bad, loc()), 'http://127.0.0.1:8787/v1', `输入 ${String(bad)}`);
    }
  });
});

describe('API query 拼装', () => {
  test('跳过空值', () => {
    assert.equal(qs({}), '');
    assert.equal(qs({ a: '', b: null, c: undefined }), '');
  });

  test('保留 0 与 false（它们是有意义的值）', () => {
    // 之前用 if (!value) 过滤会把 page=0 / anonymous_only=false 悄悄丢掉
    assert.equal(qs({ page: 0 }), '?page=0');
    assert.equal(qs({ anon: false }), '?anon=false');
  });

  test('多参数', () => {
    const out = qs({ page: 2, model: 'space-bunny-free' });
    assert.ok(out.startsWith('?'));
    assert.ok(out.includes('page=2'));
    assert.ok(out.includes('model=space-bunny-free'));
  });

  test('值里的特殊字符被编码', () => {
    const out = qs({ q: 'a&b=c' });
    assert.ok(out.includes('q=a%26b%3Dc'));
  });
});

describe('ApiError', () => {
  test('保留状态码与路径', () => {
    const err = new ApiError(401, '需要令牌', '/settings');
    assert.equal(err.status, 401);
    assert.equal(err.path, '/settings');
    assert.match(err.message, /需要令牌/);
  });

  test('status 0 表示连不上（与后端报错是两回事）', () => {
    const err = new ApiError(0, undefined, '/overview');
    assert.equal(err.status, 0);
    assert.match(err.message, /HTTP 0/);
  });
});

describe('bindOnce', () => {
  // #view 在整站生命周期里从不替换，所以页面往它上面挂监听必须自带去重 ——
  // 页面在自己的回调里再 render 一次时，上一批的 cleanup 没人调用（app.js 只保留
  // 最后一次 render 的返回值），监听器于是 1→2→4→8 累积：点第 3 次「探测上游」
  // 会发出 8 次请求，离页后还永久残留 7 个闭包。
  const fakeRoot = () => {
    const handlers = new Map();
    return {
      addEventListener(type, fn) {
        if (!handlers.has(type)) handlers.set(type, []);
        handlers.get(type).push(fn);
      },
      removeEventListener(type, fn) {
        const list = handlers.get(type) || [];
        const i = list.indexOf(fn);
        if (i >= 0) list.splice(i, 1);
      },
      count: (type) => (handlers.get(type) || []).length,
      fire: (type, event) => {
        let n = 0;
        for (const fn of [...(handlers.get(type) || [])]) {
          fn(event);
          n += 1;
        }
        return n;
      },
    };
  };

  test('同一元素同一类型只保留最后一个 handler', () => {
    const root = fakeRoot();
    bindOnce(root, 'click', () => {});
    bindOnce(root, 'click', () => {});
    bindOnce(root, 'click', () => {});
    assert.equal(root.count('click'), 1);
  });

  test('重画后只有最新的 handler 会跑（一次点击 = 一次请求）', () => {
    const root = fakeRoot();
    const calls = [];
    bindOnce(root, 'click', () => calls.push('old'));
    bindOnce(root, 'click', () => calls.push('new'));
    assert.equal(root.fire('click', {}), 1);
    assert.deepEqual(calls, ['new']);
  });

  test('不同事件类型互不影响', () => {
    const root = fakeRoot();
    bindOnce(root, 'click', () => {});
    bindOnce(root, 'change', () => {});
    bindOnce(root, 'click', () => {});
    assert.equal(root.count('click'), 1);
    assert.equal(root.count('change'), 1);
  });

  test('返回的取消函数只摘自己那一个', () => {
    const root = fakeRoot();
    const cancel = bindOnce(root, 'click', () => {});
    bindOnce(root, 'click', () => {});
    cancel();
    assert.equal(root.count('click'), 1, '旧 handler 被摘掉后不该动到新的那个');
  });

  test('unbindAll 摘干净', () => {
    const root = fakeRoot();
    bindOnce(root, 'click', () => {});
    bindOnce(root, 'change', () => {});
    bindOnce(root, 'input', () => {});
    unbindAll(root);
    assert.equal(root.count('click'), 0);
    assert.equal(root.count('change'), 0);
    assert.equal(root.count('input'), 0);
  });

  test('不同容器互不影响', () => {
    const a = fakeRoot();
    const b = fakeRoot();
    bindOnce(a, 'click', () => {});
    bindOnce(b, 'click', () => {});
    bindOnce(a, 'click', () => {});
    assert.equal(a.count('click'), 1);
    assert.equal(b.count('click'), 1, '不该把别的容器的 handler 摘掉');
  });
});

describe('debounce', () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  test('连着调只触发一次', async () => {
    const seen = [];
    const fn = debounce((v) => seen.push(v), 30);
    for (const ch of 'abcdef') {
      fn(ch);
      await sleep(5);
    }
    await sleep(80);
    assert.deepEqual(seen, ['f']);
  });

  test('连着调不会周期性触发（这正是少了 clearTimeout 的症状）', async () => {
    // 之前那版每 320ms 就触发一次、带着**当时的中间值**：调用记录页每 320ms
    // 整页重画一次，输入框被反复销毁。这里用「总共只该触发一次」钉住它。
    const seen = [];
    const fn = debounce(() => seen.push(1), 30);
    for (let i = 0; i < 20; i += 1) {
      fn();
      await sleep(10);
    }
    await sleep(80);
    assert.equal(seen.length, 1, `实际触发了 ${seen.length} 次`);
  });

  test('参数透传给最后一次调用', async () => {
    const seen = [];
    const fn = debounce((...args) => seen.push(args), 20);
    fn(1, 'a');
    await sleep(5);
    fn(2, 'b');
    await sleep(60);
    assert.deepEqual(seen, [[2, 'b']]);
  });

  test('cancel 之后不再触发', async () => {
    const seen = [];
    const fn = debounce(() => seen.push(1), 20);
    fn();
    assert.equal(fn.pending(), true);
    fn.cancel();
    assert.equal(fn.pending(), false);
    await sleep(60);
    assert.deepEqual(seen, []);
  });

  test('触发后 pending 归零', async () => {
    const fn = debounce(() => {}, 10);
    assert.equal(fn.pending(), false);
    fn();
    assert.equal(fn.pending(), true);
    await sleep(40);
    assert.equal(fn.pending(), false);
  });

  test('cancel 之后再调还能重新排程', async () => {
    const seen = [];
    const fn = debounce(() => seen.push(1), 20);
    fn();
    fn.cancel();
    fn();
    await sleep(60);
    assert.deepEqual(seen, [1]);
  });
});