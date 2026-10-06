# 云笺中转站 · openproxy

把流量转发到 **opencode Zen 的免费模型**，顺带把用量记清楚。

零成本、零外链、零构建：后端一个 Python 进程，前端是原生 ES module + 手写 SVG。

**需要 Python 3.14**（`.python-version` 与 `requires-python` 都钉在这里）。
不是「新的更好」，而是 3.14 会给未关闭的 SQLite 连接发 `ResourceWarning` ——
本项目 `filterwarnings = error`，所以它把「连接泄漏」变成编译期就能看见的失败。
3.12 也能跑通（实测 884 项全绿），但那些泄漏会被藏起来。

```bash
uv sync                                    # 装依赖（约 40MB，仅首次）
uv run openproxy                           # 起服务，控制台在 http://127.0.0.1:8787
```

<details>
<summary>没有 uv？以及各平台的替代命令</summary>

```bash
# 有 uv（推荐）：上面那两条就够了
uv sync && uv run openproxy

# 只有 pip：建一个虚拟环境，装本项目（.python-version 钉的是 3.14）
python3.14 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
openproxy                          # 之后每条 uv run xxx 都可以直接写成 xxx
```

启动之后：

| 地址 | 是什么 |
|---|---|
| `http://127.0.0.1:8787` | 控制台（七个页面，零构建零外链） |
| `http://127.0.0.1:8787/v1` | 给下游客户端用的 OpenAI 兼容端点 |
| `http://127.0.0.1:8787/api/health` | 健康检查（会报上游地址与丢弃计数） |
| `http://127.0.0.1:8787/api/docs` | 自动生成的 OpenAPI 文档 |
| `http://127.0.0.1:8787/api/openapi.json` | 同一份规范的 JSON |

（文档挂在 `/api/` 下而不是 FastAPI 默认的 `/docs`：默认路径会和 `/v1/{path:path}`
的透传路由打架，实测默认的 `/docs` 与 `/openapi.json` 都是 404。）

`Ctrl-C` 优雅退出：写统计的线程会先把队列排空再关数据库，进程日志里能看到
`Application shutdown complete`。

</details>

把客户端的接口地址指向 `http://127.0.0.1:8787/v1`，API Key **随便填个非空值**，模型名从
`http://127.0.0.1:8787/api/admin/settings` 的 `free_model_ids` 里挑一个，就能用了。

---

## 目录

- [为什么要有这个东西](#为什么要有这个东西)
- [三分钟上手](#三分钟上手)
- [架构](#架构)
- [页面](#页面)
- [接口](#接口)
- [配置](#配置)
- [隐私立场](#隐私立场)
- [测试与验证](#测试与验证)
- [与参考实现的三处关键差异](#与参考实现的三处关键差异)
- [已知边界](#已知边界)

---

## 为什么要有这个东西

opencode Zen 的免费模型分两种，**获取凭证的方式完全不同**：

| 走的路 | 免费模型 | 条件 |
|---|---|---|
| **站外直连** | 只有默认那一个 | **不需要 API Key** |
| **经 opencode 转发** | 清单里**其余全部** | 需要本机跑着 opencode，**不需要 Key** |

第二类是关键：**它们不接受站外直连**（直连会被上游按「只能从 opencode 内部调用」
拒掉），但**经本机的 opencode 转发就能用** —— 而 opencode 自己不需要任何凭证。

换句话说，**本站的用途不是「配一把 Key 让所有人都能用」，而是「把那些只能经
opencode 调用的模型也接进来」**。两种路都通：

| 客户端配置 | 走哪条路 |
|---|---|
| Key 留空 | 直连默认那个免费模型 |
| Key 填本站签发的 `sk-op-…` | 转发其余全部免费模型 |

### 为什么还需要一个 Key 占位

有些第三方聊天客户端**不允许 API Key 留空**，保存配置时直接拒绝。而免费模型**根本不需要
凭证**就能调用 —— 空 key 也能过，唯独一个**错的** key 会被上游拒掉：

| 出站 `Authorization` | 上游响应 |
|---|---|
| 不带头 | `200` |
| `Bearer `（空） | `200` |
| `Bearer sk-fake`（假值） | `401 {"type":"error","error":{"type":"AuthError"}}` |

于是客户端那个「必填」的占位密钥，必须有人替它吞掉。这个中转站就是那个人：把流量
转到免费模型上（直连或经 opencode 转发都行），顺带按调用、按模型、按密钥把用量统计
出来。

## 三分钟上手

### 0 · 环境要求

- Python **3.12**（`.python-version` 已钉死；本项目用了 `StrEnum` 与 PEP 695 泛型语法，低版本装不上）
- [`uv`](https://docs.astral.sh/uv/)（可选，只是更省事）
- 首次启动要能访问 `https://opencode.ai`，之后除上游外全程离线

### 1 · 启动

```bash
uv sync            # 仅首次：装依赖
uv run openproxy   # 每次：起服务
```

看到这三行就成了：

```
INFO  openproxy | openproxy 已启动 | 转发 http://127.0.0.1:8787/v1 -> https://opencode.ai/zen | 免鉴权=True
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
```

**想先看看会用什么配置再启动**（不会起服务，也不会碰数据库）：

```bash
uv run openproxy --print-config
```

```
  host                = 127.0.0.1
  port                = 8787
  db_path             = data/openproxy.db
  upstream_base       = https://opencode.ai/zen
  upstream_key        = <未设置>
  admin_token         = <未设置>
  require_key         = False
  free_models_only    = True
  inject_stream_usage = True
  retain_days         = 90
  max_body_bytes      = 16777216
  daily_token_quota   = 0
  connect_timeout     = 15.0
  read_timeout        = 600.0
  upstream_user_agent = openproxy/1.0 (+https://github.com/local/openproxy)
```

`upstream_key` 与 `admin_token` 永远只打印「有没有设、多长」，绝不打印内容。

### 2 · 常用启动参数

参数优先级 **命令行 > 环境变量 > 内置默认值**。命令行只影响这一次启动，不写进数据库。

```bash
# 换端口 / 换库文件（多个实例并存时最常用）
uv run openproxy --port 9000 --db /tmp/a.db

# 对外提供服务：必须同时设管理令牌，见「暴露到局域网之前」
uv run openproxy --host 0.0.0.0 --admin-token "$(openssl rand -hex 24)"

# 让下游必须带本站签发的密钥（默认免鉴权）
uv run openproxy --require-key

# 转自己另外一套 OpenAI 兼容端点，顺带统计
uv run openproxy --upstream https://my-relay.example.com/v1 --upstream-key sk-xxxx

uv run openproxy --help          # 全部参数
```

### 3 · 验证确实通了

```bash
# 健康检查
curl -s http://127.0.0.1:8787/api/health

# 真调一次上游（非流式）
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer 随便填' \
  -d '{"model":"space-bunny-free","messages":[{"role":"user","content":"你好"}]}'
```

`Authorization` **随便填个非空值**即可 —— 本站会把客户端的凭证头剥掉，绝不转发给上游
（转过去就是 `401 AuthError`）。回答里带 `usage.total_tokens`。

拿到回答的同时刷新控制台，「调用记录」页已经有这一条：Token 数、耗时、字节数都在。

### 4 · 接进客户端

- **OpenAI 兼容客户端**：Base URL 填 `http://127.0.0.1:8787/v1`，API Key 填任意非空字符串，
  模型名从 `http://127.0.0.1:8787/api/admin/settings` 的 `free_model_ids` 里挑
  （就是控制台「模型」页列的那些；清单会随上游变动，以接口返回为准）。
- **流式**：`"stream": true` 即可。本站会自动注入 `stream_options.include_usage=true`
  —— 上游默认每一帧的 `usage` 都是 `null`，不注入就完全统计不到流式用量。
- 现成的配置片段在控制台「使用指南」页，可直接复制。

---

## 架构

四层，依赖只朝一个方向。**往回依赖会在 import 期形成环**，所以这条线是硬约束：

```
api/          路由与依赖          →  service/
service/      转发 / 鉴权 / 统计  →  store/
store/        SQLite 仓储         →  domain/
domain/       纯数据结构与错误    →  （谁都不依赖）
```

| 目录 | 职责 | 关键文件 |
|---|---|---|
| `domain/` | 数据类、错误类型、免费模型清单。不 import 任何 stdlib 之外的东西 | `models.py` |
| `store/` | SQLite schema、迁移、线程安全、用量与密钥的聚合查询 | `db.py` `usage_store.py` `key_store.py` |
| `service/` | 上游转发、鉴权配额、用量抽取、写入通道、运行期配置、仪表盘组装 | `proxy.py` `auth.py` `usage_extract.py` |
| `api/` | `/v1/*` 透传与 `/api/admin/*` 控制台 | `routes_relay.py` `routes_admin.py` |
| `container.py` | 依赖装配与生命周期。生产与测试**用同一个构造函数** | |
| `app.py` | ASGI 装配、静态站挂载、错误处理 | |
| `web/` | 零构建原生 ES module + 手写 SVG 图表 | |

几个刻意的选择，理由都写在对应文件的 docstring 里：

- **用量写入走有界队列 + 单个 daemon 写线程**。记账发生在流式生成器的 `finally` 里，
  那条路径随时可能在处理 `GeneratorExit`（客户端断开），在那里 `await` 会把「关流」和
  「等 IO」耦合在一起；而 sqlite 写入只有零点几毫秒，放进事件循环反而简单。
  代价是队列满时**丢弃并计数**，所以它是「尽力而为」的统计，不是账务系统。
- **免费模型清单是静态的**。上游 `/v1/models` 只返回 id、不带价格，用它判断「免费」等于把
  策略外包给上游 —— 上游改一次，本站策略就悄悄变了。清单变更走一次 code review。
  同一份清单还带**上下文上限 / 最大输出 / 是否支持思考级别**（数据取自
  [models.dev](https://models.dev)，即 opencode 自己的模型元数据源；`/v1/models`
  不提供这些字段，第三方目录站对同一模型的数字互相矛盾，所以不采信）。
- **「上游在线」与「站外可调」是两件事**。`/v1/models` 照常返回 id，但真调可能失败：
  实测 2026-10-04，10 个免费模型里有 **9 个**回 `403 FreeTierError`
  （`OpenCode's free tier can only be used from within OpenCode`）—— 清单里
  **只有默认那一个**能站外直连，其余全部必须经 opencode。所以本站另做一次
  **逐模型最小请求**的可达性探测，
  结果落库，启动时跑一次（仅当结果超过一天）+ **每天凌晨 2 点**再跑一遍；
  控制台上两者分开展示。可用 `OPENPROXY_PROBE_REACHABILITY=false` 关掉 —— 它每轮要发
  10 次真实请求，在共享额度下这是需要被 consciously 关掉的事。

  **为什么绕不过**（`bash tests/probe_free_tier.sh` 可复现，全部实测）：

  | 尝试 | 结果 |
  |---|---|
  | 裸请求 | `403 FreeTierError` |
  | `User-Agent: opencode/1.0` / `OpenCode/1.0` | 同上 —— UA 不参与判定 |
  | `Referer` / `Origin: https://opencode.ai` | 同上 |
  | `X-OpenCode-Client: 1`，或以上全带 | 同上 |
  | `Authorization: Bearer sk-fake` | **`401 AuthError`** |

  **判据是「有没有 Zen API key」**（官方文档 `opencode.ai/docs/zen` 明确写的是标准
  Bearer：`Authorization: Bearer $OPENCODE_API_KEY`，拿 Key 的流程是登录
  `opencode.ai/auth` → `/connect`）。但**这不是唯一解法** —— 上面那 9 个模型
  经本机 opencode转发**不需要任何 Key**（见「为什么要有这个东西」）。两条路
  各有取舍：

  | 做法 | 覆盖 | 代价 |
  |---|---|---|
  | 配上游 Key（`OPENPROXY_UPSTREAM_KEY`） | 站外直连全部 | 要注册、Key 会过期 |
  | 经 opencode 转发（本项目默认） | 同样覆盖全部，**不要 Key** | 依赖本机 opencode 在跑 |

  顺带两种别的拒绝，别和「站外被拒」混为一谈：`401 ModelError`（模型名不存在或已下线）
  与 `403 RegionError`（模型在所在地区不可用 —— 实测清单里**有**这一种，
  不是理论情况）。三者的处置完全不同，探测结果里靠 `detail` 区分。

  **为什么抓不到 opencode 自己的请求头**（排查记录，避免后人重走）：
  opencode 的LLM 请求**不是 Electron 发的**，而是 Bun 子进程
  `…/cli/<版本>/opencode-cli serve --service`。而 Bun **不读系统代理、不认
  `HTTPS_PROXY`/`ALL_PROXY`、不支持 `SSLKEYLOGFILE`**（均实测；同一时刻
  Chrome / 钉钉 / QQ 都正常走 mitmproxy，只有它不）。所以 mitmproxy、透明模式、
  网卡抓包三条路都拿不到明文，而剩下手段（root +改 `pf.conf` 防火墙、
  替换 180MB 第三方二进制）风险与收益不成比例。

  **一个曾经把结论带偏的推理**（记下来防止重犯）：「带假 key 回 401 说明服务端在查凭证，
  所以判据是凭证」—— 这个推理**不成立**。服务端对**任何格式错误**的凭证都回 401，
  它可能只是在区分「提供了但无效」与「压根没提供」，真正的放行条件是另一套机制。
  真正的确认来自官方文档，不是这个观测。

- **思考级别只有四个，是实测出来的而不是抄来的**。设置页可切
  `low` / `medium` / `high` / `max`，**外加一个开关**决定是否强制注入
  （关掉时本站一个字都不改客户端的 body）。

  | 档位 | 实测 `completion_tokens`（`27*43`，`max_tokens=40`） |
  |---|---|
  | `minimal` / `low` | 3 —— 上游返回 200 但 usage 空，等于**静默关掉思考** |
  | `medium` | 16 |
  | `high` | 29 / 33 |
  | **`max`** | **33 / 34 / 33**（连跑三次，逼近上限） |

  `none` 与 `minimal` **刻意不提供**：它们不是「更低的档位」，而是「不思考」——
  上游照常返回 200，而 `usage` 是空的。列成合法档位只会让人以为思考还开着，
  而实际上一分钱推理预算都没花。`max` 则是实测真的把预算拉满（33/34/33 稳定复现），
  不是被静默忽略的别名。`xhigh` 上游也接受，但没列 —— 只给用户实测确认过的值。

  > 档位表是**后端的唯一事实来源**（`VALID_REASONING_EFFORTS`），前端下拉框与
  > 「开开关时用哪档」的默认值都从 `/api/admin/settings` 取。曾经前端手写过一份
  > 白名单，加了 `max` 之后忘了同步 —— 症状是「选了 max，点开关，静默变成 low」，
  > **且没有任何报错**。现在有测试守着这件事。

- **统计是「一个数据集、多处渲染」**。总览的「今日 Token」和调用记录的分页总数来自同一次
  聚合，前端不做任何二次计算；两个都「看起来对」却互相矛盾的数字是最难查的一类问题。
- **按天聚合用固定 UTC 偏移**（默认取本机，可钉死）。于是「今天」对用户是本地日，
  而测试可以把它钉成确定值。

### 前端

```
web/
├── index.html          壳（侧边栏 + 顶栏 + <div id="view">）
├── css/
│   ├── tokens.css      设计令牌：宣纸 / 墨 / 朱砂 / 靛青，日夜双主题
│   ├── base.css        重置、排版、通用原子件
│   ├── layout.css      侧边栏、顶栏、抽屉
│   └── components.css  统计卡、表格、分页、弹层
├── js/
│   ├── app.js          hash 路由、侧边栏、主题
│   ├── api.js          后端客户端（非 2xx 一律抛 ApiError）
│   ├── ui.js           格式化、DOM 辅助、图标、吐司
│   ├── charts.js       自研 SVG：折线 / 柱 / 环形 / 迷你走势
│   └── pages/          七个页面，各自一个模块
└── assets/favicon.svg
```

**为什么自己写图表**：零依赖零外链、`file://` 也能跑；配色/线条/字体能调成古风而不是覆盖
第三方默认主题；最重要的是它们是**纯函数**（数据 → SVG 字符串），所以坐标缩放、空数据降级、
除零保护这些最容易出错的地方能在 `node --test` 里直接验证 —— 第三方图表的内部算不了。

**悬停提示为什么也不用 JS**：折线 / 柱 / 环形都是「每根柱子一个透明命中矩形 + 一个提示框 +
一条参考线」，显隐交给 CSS 的 `:hover` / `:focus-visible`。一旦改成监听 `mousemove`
去算「鼠标落在第几个点上」，上面的纯函数约定就破了 —— 而那正是这些图最容易错的地方
（缩放、除零、空数据）。命中区用 `fill="transparent"` 而不是 `fill="none"`
（后者收不到鼠标事件），提示框用 `opacity` + `visibility` 而不是 `display`
（`visibility` 隐藏时不再接收事件，于是「鼠标移出提示框」不会卡住不消失）。
键盘可达是顺带的：命中矩形带 `tabindex="0"` 与 `aria-label`，焦点态用 `stroke` 画
（`outline` 在 SVG 子元素上跨浏览器不一致）。提示框宽度靠**估算文本像素宽**
（CJK 记 2 个单位）——因为拿不到 DOM 就用不了 `getComputedTextLength()`。

**为什么用 hash 路由**：后端因此**不需要** SPA fallback 路由，静态站只有一个入口文件，
也不存在「catch-all 把真正的 404 吞掉」的问题。

### 视觉

中式古风，取色有出处：宣纸 `#f4efe3`、松烟墨 `#26221d`、印泥朱砂 `#9e3b32`、靛青 `#2f5d7c`、
赭黄 `#b8873f`、松花绿 `#5c7a63`。标题走宋体族，正文数字走等宽表格数字，分隔用**双线界栏**
而非阴影。夜间主题不是把颜色反过来，而是换更冷的墨底 + **提亮**的朱砂 —— 暗底上未提亮的朱砂
会糊成一团。

---

## 页面

| 地址 | 页面 | 数据来源 |
|---|---|---|
| `#/overview` | 总览 | `/api/admin/overview` |
| `#/usage` | 调用记录 | `/api/admin/usage` |
| `#/models` | 模型 | `/api/admin/models` |
| `#/channel` | 渠道 | `/api/admin/channel` + `/channel/probe` |
| `#/keys` | 密钥 | `/api/admin/keys` |
| `#/settings` | 设置 | `/api/admin/settings` |
| `#/guide` | 使用指南 | `/api/admin/settings` |

筛选状态写进 hash（`#/usage?page=2&model=…`），所以「筛选后把链接发给同事」能复现同一屏；
未知地址一律回落到总览，不给 404。

---

## 接口

### 透传（OpenAI 兼容）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/chat/completions` | 对话补全，支持流式 SSE |
| `GET` | `/v1/models` | 原样透传给上游 |
| `*` | `/v1/{任意路径}` | 原样透传。上游新增端点时本站不用改代码 |
| `GET` | `/v1/__health` | **本站**自检，不打上游 |

`/v1/__health` 单独存在，是因为上游对未知模型会回 `401 ModelError`，透传给健康检查会被
误读成「本站挂了」。

### 控制台

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 本站状态（含 `recorder_dropped`） |
| `GET` | `/api/admin/overview` | 总览数据 |
| `GET` | `/api/admin/usage` | 调用记录，支持 `model` / `status` / `search` / `anonymous_only` / `since` / `until` / `page` / `page_size`（`status` ∈ `ok`\|`error`） |
| `GET` | `/api/admin/models` | 免费清单 + 在线状态 + 用量 |
| `GET` | `/api/admin/channel` | 上游信息、成功/失败趋势、失败原因拆解 |
| `POST` | `/api/admin/channel/probe` | 立即探测上游 |
| `GET/POST` | `/api/admin/keys` | 列出 / 签发密钥 |
| `PATCH/DELETE` | `/api/admin/keys/{id}` | 改（改名、配额、停用）/ 删 |
| `GET/PATCH` | `/api/admin/settings` | 读 / 改运行期设置 |
| `POST` | `/api/admin/settings/reset` | 回到环境变量基线 |
| `GET` | `/api/admin/client-context` | 管理端自身的配额视角 |
| `POST` | `/api/admin/maintenance/prune` | 立即清理过期记录 |
| `POST` | `/api/admin/maintenance/flush` | 排空统计写入队列 |
| `GET` | `/api/docs` | OpenAPI 文档 |

---

## 配置

**优先级：命令行 > 真实环境变量 > `.env` 文件 > 内置默认值。**

### `.env` 文件

```bash
cp .env.template .env      # 模板入库，.env 不入库
$EDITOR .env
python -m openproxy        # 自动读 ./.env
```

- **真实环境变量压过 `.env`** —— 与 python-dotenv 的默认**相反**，且必须如此：
  `.env` 是人手写的基线，而 `docker run -e OPENPROXY_PORT=9000` 是「这一次的
  意图」，应当压过文件。反过来的话容器编排怎么传参都没用。
- 格式只支持 `#` 注释、空行、`KEY=VALUE`、可选的成对引号。**写错的行会被
  跳过并打一条 warning** —— 不让一个笔误变成停机，但也不静默（服务起来了
  却没人知道自己少配了一项，那是更坏的失败）。
- **支持 UTF-8 BOM**（`utf-8-sig`）。这是刻意加的：Windows 记事本与 VS Code
  「以 UTF-8 with BOM 保存」是默认行为，而 BOM 不剥会让**第一个键**变成
  `\ufeffOPENPROXY_PORT` —— 症状是完全静默的（`.env` 写着 10490，
  `--print-config` 打印 8787）。模板第一项安全配置恰是 `ADMIN_TOKEN=`，
  它失效意味着「绑 0.0.0.0 又留空 = 管理接口裸奔」这条防线被无声拆掉。
- `.env` 已在 `.gitignore` 里；`.env.template` **要入库**。
- 密码含首尾空格时**必须用引号**（`KEY="  pad  "`）：裸值写法会被 strip。
- opencode 的密码**每次重启都变**，而 `OPENPROXY_OPENCODE_PASSWORD` 留空时
  会自动读 `~/.config/opencode/service.json` —— **留空比手填更省事**。

### 环境变量

命令行是「这一次启动」的临时覆盖，不写库，退出后不残留。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `OPENPROXY_HOST` | `127.0.0.1` | 监听地址 |
| `OPENPROXY_PORT` | `8787` | 监听端口 |
| `OPENPROXY_UPSTREAM_BASE` | `https://opencode.ai/zen` | 上游地址 |
| `OPENPROXY_UPSTREAM_KEY` | 空 | 转发这个 key 给上游；不填则匿名 |
| `OPENPROXY_UPSTREAM_USER_AGENT` | `openproxy/1.0 (+…)` | **出站 UA，勿设为空** |
| `OPENPROXY_REQUIRE_KEY` | `false` | 是否强制下游密钥 |
| `OPENPROXY_ADMIN_TOKEN` | 空 | 控制台令牌；不填则不校验 |
| `OPENPROXY_DB_PATH` | `data/openproxy.db` | SQLite 路径 |
| `OPENPROXY_RETAIN_DAYS` | `90` | 用量保留天数 |
| `OPENPROXY_MAX_BODY_BYTES` | `16777216` | 请求体上限 |
| `OPENPROXY_FREE_MODELS_ONLY` | `true` | 是否只转发免费模型 |
| `OPENPROXY_INJECT_STREAM_USAGE` | `true` | 流式是否注入 `include_usage` |
| `OPENPROXY_REASONING_EFFORT` | 空 | 强制写出的思考级别（`low`/`medium`/`high`/`max`）；空 = 不动客户端的 body。控制台关掉开关是**三态里的「显式关闭」**，压得过这个环境变量 |
| `OPENPROXY_PROBE_REACHABILITY` | `true` | 是否跑每日「站外可达性」探测（会消耗上游额度） |
| `OPENPROXY_DAILY_TOKEN_QUOTA` | `0` | 全站日配额，0 = 不限 |
| `OPENPROXY_CONNECT_TIMEOUT` / `_READ_TIMEOUT` | `15` / `600` | 上游超时（秒） |
| `OPENPROXY_OPENCODE_MODELS` | 空 | 逗号分隔的模型 id，这些改走本机 opencode 服务；空 = 全部直通 |
| `OPENPROXY_OPENCODE_BASE` | `http://127.0.0.1:4096` | 本机 opencode 服务地址 |
| `OPENPROXY_OPENCODE_PASSWORD` | 空 | opencode 服务密码；空 = 自动读 `~/.config/opencode/service.json` |
| `OPENPROXY_OPENCODE_DIRECTORY` | 空 | opencode 工作区目录 |
| `OPENPROXY_OPENCODE_TIMEOUT` | `120` | 等 opencode 回完的秒数（实测最小问题就要 22~25 秒） |

其中 `require_key`、`upstream_base`、`retain_days`、`daily_token_quota`、
`free_models_only`、`inject_stream_usage`、`reasoning_effort`、`opencode_models`
八项可以在控制台改：`opencode_models` 在**模型页**逐个勾选（每张模型卡一个开关），
其余在「设置」页。全部**落库、跨重启保留、立即生效**。生效路径是
「改覆盖层 → 重合成配置快照」，**不是重建服务对象** —— 重建会静默丢掉
注入的 recorder / store。

### 两条上游路径：直通与 opencode 服务

`opencode_models` 里的模型走本机 opencode 服务转发，其余仍直通
`opencode.ai/zen`。**默认全部直通** —— 没有 opencode 也能跑，行为与之前完全一致。

为什么需要第二条路：实测 10 个免费模型里有 9 个从站外直连会被上游回
`403 FreeTierError`，而同一个模型经由 opencode 本地服务能调通。所以
「配一个上游 Key」与「让某些模型走 opencode」是**两条互补的路**。

**四个必须知道的限制**（都实测过）：

1. **模型指定不了。** `/api/session/<id>/prompt` 的三种写法（`model` 对象 /
   `providerID`+`modelID` / 配置文件里的 `model` 字段）全部被忽略，一律走
   opencode 自己的默认模型。所以本站**如实回传实际服务的模型名**，而不是回显
   请求里的那个 —— 落库的统计也跟着用实际那个，否则「按模型统计」会系统性归错类。
2. **流式变非流式。** opencode 是「投递 → 事后查」：投递立刻返回
   `{"delivery":"steer"}`，回复要靠轮询 `GET /api/session/<id>/message` 才拿得到
   （实测 22~25 秒）。客户端要 `stream:true` 时本站会**补一层 SSE 包装**，
   让响应形状与直通一致 —— 宁可少一个「首 token 早到几百毫秒」的特性，
   也不能让客户端因形状变了而失败。SSE 里思考内容单独一帧
   （`delta.reasoning_content`）放在正文之前，顺序与真实流式一致。
3. **只支持 `chat/completions` 形状。** `/v1/responses`、`/v1/messages`
   这类协议转不过去（opencode 的 `/prompt` 只吃一个字符串），会明确回 400
   而不是发一个含义错误的请求。
4. **强依赖外部进程。** opencode 没起就 502，**且不自动回退直通** ——
   自动回退会造成同一模型在两条路上静默切换，客户端看到的价格、上下文、
   模型回答全都不一致，而日志里只有一行 warning。

**超时是三层的**（各自解决不同的问题，别只调一个）：

| 层 | 配置 | 管什么 |
|---|---|---|
| 单次 HTTP 连接 | `connect_timeout` | 本机端口被占时多久放弃 |
| 单次 HTTP 读取 | `read_timeout` | 建会话 / 投递 / 回收这些一次性请求 |
| **轮询总预算** | `opencode_timeout` | 从投递到拿到回复的总时长 |

`opencode_timeout`会**同时压住单次轮询请求**：每次轮询的 timeout 取
「剩余预算」与 `read_timeout` 的较小值。否则它只在循环头部检查 deadline，
管不住「正在飞的那一次 GET」—— 而那正是「opencode 进程挂起但TCP 不断」时的
等待来源（实测 `poll_timeout=0.3s` 而单次 GET 卡 2s → 实际 2.00s）。

**一轮prompt 会产生多条 assistant 消息，而轮询返回的是全部历史。**

这两件事一起决定了实现方式：

- **多条消息**：opencode 是 agent，工具调用一轮产生一条（文本 + `tool_call`），
  拿到 `tool_result` 后再产生一条。所以各条 assistant 的**正文与思考要拼接**，
  不能用「后一条覆盖前一条」—— 那会让用户拿到一个**语义不完整但看起来正常**
  的答案。
- **全部历史**：`GET /api/session/<id>/message` 每次返回**到目前为止的全部**
  （opencode 侧 `ListMessagesBySession` 没有 `limit`、没有 `since`）。
  而轮询会调十几次到几十次（默认 `poll_interval=0.35s`，实测最小问题
  22~25 秒 ≈ 60~70 次）。所以**必须按消息 id 去重**，否则早期消息会被
  累加几十次—— 同样是 HTTP 200、没有任何报错。

`usage` 逐项取 **max** 而非相加：`input` 在多轮里是**累计值**（每轮含之前所有
内容），相加会重复计。**已知偏差**：opencode 的摘要器压缩历史后会把 `PromptTokens`
清零，此时 max 会取到压缩前的更大值 → **多算**。这是刻意选的保守方向 ——
高估只会让用户多留意，低估会让账单看起来比实际便宜。

**模型是在「建会话时」指定的，指定是生效的。** 形状是：

```http
POST /api/session
{"model": {"id": "<modelID>", "providerID": "opencode", "modelID": "<modelID>"}}
```

`id` 与 `modelID` **都要给** —— 只给 `{providerID, modelID}` 会被拒成
`400 Missing key at ["model"]["id"]`。**建好会话之后 `/prompt` 不接受任何
model 参数**：顶层 `{providerID, modelID}`、嵌套 `{"model": {...}}`、
`{"model": "字符串"}` 三种形状实测**全部被静默忽略** —— 都返回 200、都
`delivery: "steer"`，但实际服务的还是默认模型。

> **「返回 200」不等于「参数被接受」。** 本站曾据此得出「opencode 改不了模型」
> 的结论，还写进了本文与代码注释，并据此判定这条路是死路 —— 那是**错的**。
> 唯一可信的判据是去 `GET /message` 里读 assistant 消息的 `model.id`。
> 复现命令：`python3 scripts/probe-opencode-models.py`。

**响应与落库都用 `reply.model`（实际服务的那个）而不是请求里的值** ——
opencode 可能在重试时回退到别的模型，而回显请求值会掩盖「实际走了另一个模型」
这个事实。

**思考内容是尽力而为的额外字段。** 走 opencode 时思考过程放在
`message.reasoning_content`（流式时是独立的 `delta.reasoning_content` 帧）。
**它不是 OpenAI 官方 Responses API 的字段名**（官方用 `reasoning.summary`），
主流客户端（openai-python / openai-node 等基于 pydantic / zod 的）遇到未声明
字段的默认策略是**静默丢弃** —— 不会报错，但也拿不到。所以：

- 直连本站的客户端**可能**能看到思考内容，取决于它的实现；
- 本站**控制台**显示 `reasoning_tokens` 计数；**思考正文不落库**（「只存计数，
  绝不存正文」是本项目的硬约定）。

### 实测可用性（2026-10-05，真 `opencode serve` + 真模型）

`scripts/probe-opencode-models.py` 的输出，两个数字要分开看：

```
模型指定生效 8~10/10  请求的模型 == 实际服务的模型
真的可用     5~6/10指定生效 且 这次调用没报错（三次实测 5、6、6/10）
```

**「真的可用」这三次采样逐个模型都不同** —— 前一轮某个模型成功过、
另一个失败过，下一轮反过来了。所以别把某个模型的「上次能用」当承诺。

**具体哪些模型能用，见控制台「模型」页**（那里是实时数据，不会有本文档这种
过期风险）。实测的三种状态：

| 状态 | 特征 | 处置 |
|---|---|---|
| 完全可用 | 正常返回 | — |
| 指定生效但调用失败 | 上游模型自己的问题（`provider.invalid-request` / `Endpoint is unavailable` / 上游 503） | **换个模型名**即可 |
| 完全没调通 | 1.5 秒内 `idle` 且无 assistant | 上游已下线该模型 |

**两个必须知道的事实**：

1. **可用性会漂移。** 同一个模型今天 4 秒返回、明天 150 秒超时
   （实测某个模型一次 4.7 秒、另一次 113 秒）。所以
   「控制台上次能用」不代表现在能用 —— 升级 opencode 后跑一次上面的脚本。
2. **失败原因写在 assistant 消息自己的 `finish` / `retry` 字段里**，
   而它的 `type` 仍是 `assistant`、`content` 是 `[]`。只按 `type` 判断会把
   上游故障误报成「没有回复内容」。本站读这两个字段并原样转达，
   这样控制台上看到的是「`<模型名>`: Endpoint is unavailable」
   而不是一句无用的「没有回复内容」。

### 并发与切页保护

模型卡的开关是**逐模型**的，两个并发保护是刻意加的：

- **保存期间禁用全部开关**（不是只禁被点的那个）。`opencode_models` 是
  **整份替换**语义，而每次点击的起点是渲染时的快照—— 两个点击各自从同一份
  快照算配置并发提交，后到的会把先到的整个顶掉。症状是「勾了两个，刷新后
  只剩一个」，而界面上两个都还亮着。
- **切页后不重画**。PATCH 飞行中切到别的页，返回后本页的重绘会把那个页整个
  覆盖成模型页（`view` 是全局唯一的 `#view`）。所以每次进本页领一个令牌，
  cleanup 时作废它。

`opencode_models` 的三态与 `reasoning_effort` 同一套：
`None` = 没设过（回环境变量基线）、`()` = 明确要空（即便环境变量有值也全部直通）、
`""` 只在 `reasoning_effort` 上有意义（= 显式关闭强制）。

### 与真实 opencode 服务对接时实测到的四条语义

下面每一条都是**真跑 `opencode serve`观测到的**，不是从文档推断的。它们每一条
都曾导致一个 P0，所以写在这里 —— 换opencode 版本时请先重测这四条。

| 事实 | 后果 |
|---|---|
| `GET /message` 返回**全部**历史（无 limit/since） | 不去重就会把早期消息累加几十次 |
| assistant 消息**先以空 parts 入库、再原地增长**（同一 id） | 把「首次见到」当成「已完整处理」会**丢答案** |
| **token 在 `EventComplete` 时才写**；工具轮 `tokens` 是 `{}` | 无脑覆盖会让统计变成「未知」 |
| **`idle` 排在 assistant 之前**（实测返回 `[user, idle, assistant]`） | 遍历时遇idle 就返回 -> **每次都失败** |

最后一条尤其反直觉：所有替身测试都会把 `idle` 放在最后（那是「看起来对」的顺序），
所以纯单测全绿而真实调用 100% 失败。这也是为什么
`tests/test_opencode_multiturn.py` 里有一个 `TestFakeMatchesReality` ——
它断言替身本身的语义，而不只是替身的产出。

### 真实验证记录（2026-10-05）

真 `opencode serve` + 真模型（清单里随便挑一个）跑通两层：

```
直接调 opencode_client: text='56'  usage={prompt:12760, completion:52}
                        reasoning='The user asks 回答 exactly: 7*8…'
走完整代理层:      HTTP 200  content='42'  usage known=True
落库 bytes_out=720 == 实际发送 720   落库 model = 请求的那个（不是默认值）
```

实测耗时 15~18 秒（其中首token 之前约 4 秒是空窗，`poll_interval=0.35s`
时会经历十余次空轮询 —— 也就是「空parts 窗口」是常态）。

**端口是这套方案最大的脆弱点**：`opencode serve --port N` 默认 4096，
但**桌面版的 Bun 子进程用的是随机端口**（实测 49374）。用桌面版时必须显式设
`OPENPROXY_OPENCODE_BASE`。

### 暴露到局域网之前

本站默认只绑 `127.0.0.1`，且**默认不校验控制台令牌**。要改绑 `0.0.0.0`，**必须**同时设置：

```bash
OPENPROXY_HOST=0.0.0.0
OPENPROXY_ADMIN_TOKEN=<一串足够长的随机串>
```

「设置」页在未设令牌时会显式警告这一点，而不是假装默认就安全。

---

## 隐私立场

- **只存计数，绝不存正文**（指**本站的库**）。`usage_records` 表里没有
  prompt / completion 字段，只有 token 数、耗时、字节数、模型名、状态码、
  错误类别、客户端标签。实测全库搜不到一句 prompt 原文。
  **走 opencode 转发时例外** —— 那条链路上正文落在 opencode 自己的库里，
  见下一节。
- **密钥明文只在签发那一刻返回一次。** 库里存的是 `sha256(key)` 的前 32 位十六进制；
  之后任何接口都只能拿到 `sk-op-xxxxx…` 这样的前缀。
- **访问日志只记方法、路径、状态、耗时、字节数、模型名** —— 正文永远不进日志。
- **但走 opencode 转发时，正文会落在 opencode 那边**（不是落在这里）。见下。

### 走 opencode 时你的对话存在哪

**这一条必须说清楚**，因为「只存计数」只对**本站的库**成立：

| | 位置 | 里面有什么 |
|---|---|---|
| **openproxy** | `data/openproxy.db` | **只有计数**（token / 耗时 / 字节 / 模型名 / 状态码）。实测全库搜不到一句prompt 原文 |
| **opencode** | `~/.local/share/opencode/opencode.db` | **完整对话**：你的 prompt、模型的回答、思考过程、工具调用 —— 一条不落 |

opencode 那边的表是 `session_v2`（会话）与 `session_message`（消息，
`data` 列是整条消息的 JSON）。实测本机 48 个会话 / 5358 条消息 / 64 MB。

三个查看方式：

```bash
# 1) 桌面 App —— 图形界面，最方便
# 2) TUI：跑 opencode，在会话列表里选
# 3) 直接查库
sqlite3 ~/.local/share/opencode/opencode.db \
  "SELECT id, title FROM session_v2 ORDER BY time_created DESC LIMIT 10;"
```

**本站转发时会主动删掉这些会话**（`DELETE /api/session/<id>`，用 `finally`
保证异常时也删），所以正常情况下那些会话只活到本次转发结束。**但删的是
opencode 的会话记录，不是 opencode 那个库本身** —— 它的 schema 版本、设置、
凭据等都在同一个库里。

想彻底不留在 opencode 那边，本项目**做不到**（那是它的设计：会话是它的核心
功能）。能做到的是**缩短存活时间**（本站转发完就删），以及**不主动请求它把
对话写进项目目录**（`opencode_directory` 指向一个空目录即可 —— 实测对
全局 `AGENTS.md` 无效，那些指令是全局加载的）。
- **客户端 IP 取自 TCP 连接，不读 `X-Forwarded-For`**。本站绑 127.0.0.1，那个头是本机
  客户端自报的，把它当真会让「按 IP 统计」变成一个谁都能伪造的字段。
- **不自动重试。** `/v1/chat/completions` 是有副作用的 POST，重试会白白消耗上游额度。
  只有幂等的 `GET /v1/models` 探测会重试一次。

---

## 测试与验证

```bash
uv run pytest -m "not network"        # 884 项，不联网，约 15 秒
uv run pytest                          # 891 项（含 7 项真实上游冒烟），约 38 秒
uv run ruff check src tests            # 0 error
uv run mypy                            # strict，53 个文件 0 error
node --test "tests/js/*.test.mjs"      # 180 项前端单测
```

最近一次实测：**pytest 891 passed**（其中 884 不联网、7 项真打 `opencode.ai`）、
**ruff 0 error**、**mypy strict 0 error**、**node 180 passed**。整套件在
**Python 3.14 与 3.12 上都是 884/884 全绿**。

> 3.14 值得单独说一句：它给未关闭的 SQLite 连接发 `ResourceWarning`，在本项目
> `filterwarnings = error` 下直接是失败。升上去之后**真的炸出了三个缺陷** ——
> `Database.close()` 只关自己那条连接、写线程/线程池的连接没人负责、
> 以及测试里 `with sqlite3.connect(...)` 只管事务不关连接（这条是 3.12 藏起来的）。
> 换句话说 3.12 的「全绿」并不代表没有泄漏。

| 层 | 文件 | 覆盖的东西 |
|---|---|---|
| 配置 | `test_config.py` | 环境解析、覆盖层合成、非法值（含 `retain_days=0` 会清空历史）、秘密不外泄 |
| 存储 | `test_store_usage.py` | 写入、分页、五种聚合、时区边界、8 线程并发、清理、WAL 与 `BEGIN IMMEDIATE`、幂等迁移 |
| 存储 | `test_store_keys.py` | 签发、解析、停用、**删密钥后用量留存**、备注入库截断 |
| 容器 | `test_container.py` | 启动清理、**后台清理循环真跑**（含异常后继续下一拍）、关停关掉全部资源 |
| 抽取 | `test_usage_extract.py` | 非流式 JSON、SSE 增量扫描（跨块 / 逐字节 / 超长行）、注入 |
| 鉴权 | `test_auth_quota.py` | 令牌提取、三种密钥态、自然日配额、边界值 |
| 转发 | `test_proxy.py` | 出站头、分帧、SSE 透传、客户端中断、上游异常、**落库状态码等于上游真实码**、原始路径、**重复响应头到客户端**、配额判定不在事件循环上、model 归一化 |
| 写入 | `test_usage_recorder.py` | 队列、批量、背压丢弃、写线程存活、关闭排空、乱序批次的 `last_used_at` |
| 服务 | `test_services.py` | 模型目录、探测重试、覆盖层持久化、**跨页数字一致**、时间窗口边界 |
| HTTP | `test_api_relay.py` `test_api_admin.py` | 两个 HTTP 层的完整行为、`/maintenance/flush` 的真实计数 |
| 前端站 | `test_web_static.py` | 路由覆盖、零外链、**导入的符号必须真的被导出**、**用到的 helper 必须真的被导入**、类名必须有样式、**文字对比度达 AA**、**源码里不许有 U+FFFD 乱码** |
| 前端 | `tests/js/charts.test.mjs` | 坐标缩放、除零保护、空数据降级、SVG 注入面、可访问名、防抖、**悬停提示框必须完整落在绘图区内**、命中区可命中/可聚焦、**不含 script 与事件属性** |
| 前端 | `tests/js/ui.test.mjs` | 格式化函数的空值 / 负数 / 极端值、query 拼装、**防抖只触发一次**、**监听去重** |
| 前端 | `tests/js/models.test.mjs` | token 格式化（0 / 负数 / 未知）、可达性三态、**detail 必须转义** |
| 探测 | `test_reachability.py` | `403FreeTierError` 判blocked、**5xx 不误判成拒绝**、网络失败归 `unknown`、单个失败不废整轮、**串行 + 间隔真的 sleep**、落库往返、损坏 JSON 退化 |
| 调度 | `test_probe_schedule.py` | 对齐到 02:00（而非「每 N 小时」）、启动那一次只在过期时跑、循环自转、**一败不杀循环**、关停不拖超时 |
| 配置 | `test_overlay_codec.py` | **编解码字段集 == dataclass 字段集**（漏一个字段 = 重启即失效）、类型不对的值退化成「没设」、校验失败不落库 |
| 注入 | `test_reasoning_effort.py` | 纯函数、配置合成、**出站 wire 上真的注入**、与 `inject_stream_usage` / model 归一化叠加、GET 不受影响、`null` ≠ 空串 |
| 真上游 | `test_smoke_real.py` | 清单里的模型还在线、真实 usage、UA 过 Cloudflare、流式注入 |

### 为什么假上游不能替代真上游

整个套件用的是 `tests/support/upstream.py` 里的假 transport，它证明了转发链路的分帧、
用量抽取、错误透传、记账调用是对的。**它证明不了真实上游还能用** —— Cloudflare 规则、上游
改协议、免费模型下线，只有真跑一次才知道。所以 `test_smoke_real.py` 单独存在，默认不跑，
只做只读或幂等的调用。

（顺便：`httpx.MockTransport` **测不了流式转发** —— 它返回的响应带同步字节流，
`client.send(..., stream=True)` 会直接在 httpx 内部炸掉。所以假上游是自己实现的
`AsyncByteStream`，SSE 分块、跨块、逐字节、中途断流都能真实驱动。）

### 真实浏览器验证记录

在 `http://127.0.0.1:8899` / `:8904` 上逐页跑过（Playwright + 真 Chromium）：

- 七个页面全部渲染成功，**console 零 error**
- 每页 `scrollWidth == clientWidth`（无横向溢出；宽表格在自己的 `.table-wrap` 里滚动）
- 渲染出的 DOM 里没有 `undefined` / `NaN` / `[object Object]`
- 未知 hash 回落总览、筛选写进 query、分页翻页正常
- 主题切换、侧边栏折叠、窄屏抽屉可用
- **调用记录页关键词框**：模拟 70ms/字敲一个模型名，`rendered` 事件恰好触发 **1 次**
  （不是每 320ms 一次），hash 里带上那个关键词，
  `document.activeElement === 搜索框` 且 `selectionStart === 16`
- **设置页开关**：点「仅转发免费模型」→ 服务端 `overlays.free_models_only` 立刻变 `false`、
  吐司提示成功（此前是两个死开关：点击报错并弹回）
- **对比度**：所有承载文字的令牌在纸底与夜间两套主题下都达 WCAG AA 4.5:1
  （由 `test_web_static.py::test_text_colours_meets_wcag_aa` 逐个断言）
- **可访问名**：每页一个 `sr-only` 的 `<h1>`；所有图表 `role="img"` 且带 `aria-label`；
  设置页每个开关都有可访问名

---

## 与参考实现的三处关键差异

行为刻意与既有的 keyless 转发脚本保持一致的部分（上游非 2xx 原样透传、hop-by-hop 头双向
剥离、凭证不转发、127.0.0.1 绑定、正文不进日志）不重复。修掉的三个**实测存在的**缺陷：

| # | 参考实现的行为 | 本站的做法 | 为什么 |
|---|---|---|---|
| 1 | 原样透传客户端 `User-Agent` | **无条件覆写出站 UA** | 实测上游 Cloudflare 对 `Python-urllib/*` 和**缺失 UA** 的请求回 `403 error code: 1010`。用 urllib 的客户端（或任何不发 UA 的）会直接挂 |
| 2 | 不注入 `stream_options` | 默认注入 `include_usage: true` | 实测流式响应里每帧 `usage` 都是 `null` —— 不注入则**流式调用完全统计不到用量** |
| 3 | 逐条记录后转发 | **边转发边抽**，不缓冲响应 | 内存占用与响应长度解耦，completion 正文不会落进任何日志或库 |

页面功能参考另一套同类中转站项目，但**版式、配色、组件、图表全部重写**，
并且按中转站的语义重新映射了页面集合：充值 / 订阅 / 兑换 / 返利 / 订单对免费中转站无意义，
换成 密钥 / 调用记录 / 模型 / 渠道。

---

## 启动时会替你拉起 opencode

`opencode_base` 指向本机时，启动流程是：

| 情况 | 做什么 |
|---|---|
| 地址不是本机（你自己接的外部服务） | **只做健康检查**，不接管它的生命周期 |
| 本机已有进程在听 | 直接用，不重复起 |
| 本机没人监听 | **自动起一个** `opencode serve --port <同一个端口>` |
| 起不来 / 起了但 15 秒内不健康 | **退出码 2**，并说清接下来该做什么 |

起完还会**轮询等健康** —— `serve` 打印 `server listening` 只说明 socket 绑好了，
不说明它能应答（实测有「端口在听但 `/api/model` 一直 502」的窗口期）。等不到就
把它杀掉：留一个半死不活的进程比没有更糟，它会占着端口让下次也起不来。

调障开关：`--no-opencode-check`（完全跳过检查）、
`--no-opencode-autostart`（不自动起，但仍然检查）。

### 密码：为什么建议留空，以及自动读**只对桌面版**成立

`opencode serve` **每次启动生成的密码都不同**。而
`~/.config/opencode/service.json` 是**桌面版独占**的（只有它写那个文件）——
实测同一个密码：桌面版 200、CLI 起的服务 401。

所以：

| 连的是 | 密码从哪来 |
|---|---|
| **自己起的 CLI** | 它的 stdout（本站启动时直接抓，写进运行时配置） |
| **桌面版** | 留空 → 自动读 `service.json` |
| 手填 `OPENPROXY_OPENCODE_PASSWORD` | 会在某次重启后失效，不建议 |

### 端口被一个「密码拿不到」的服务占着时会**自动接管**

实测场景：10490 上有个之前手工起的 opencode，而本站读到的密码对它无效
（它属于另一类进程），而它自己的密码**只存在于它的 stdout、早就随进程退出
消失了** —— 也就是说**那个密码在物理上已经拿不到了**。

此时「请手工处理」是句空话（用户能做的只有杀进程或放弃这条路），所以本站
**自动接管**：杀掉那个进程（**只杀命令行里有 opencode 的**，不按端口无差别
kill ——那会误伤恰好用着这个端口的别的服务），再起一个自己的。

关掉这个行为：`--no-opencode-autostart`。

## 经 opencode 转发的已知限制

**它只适合纯问答，不能用工具。** opencode 是 agent，每个新会话都会读全局
`AGENTS.md`（本机那份写着「必须先读取 xxx」），于是它会先调 `read` 工具——
而**读文件必须先要人批准**，通过 HTTP 投递时没人批，那个工具就永远停在
`{"executed": false, "status": "running"}`，一直等到本站超时。

实测对照（同一服务、同两个模型、同一 prompt，各 3 次；模型名见控制台「模型」页）：

| 被测模型 | 不加约束 | 加约束 |
|---|---|---|
| 甲 | 2/3 卡住 | **0/3** |
| 乙 | 3/3 卡住 | **0/3** |

所以 openproxy 转发时会在 prompt **末尾**追一句
「不要使用任何工具，不要读取任何文件，不要遵循任何项目指令文件，直接回答」。
放末尾是因为实测放前面时模型有时读不到。

**这条链路的代价**：问题本身就是「读一下这个文件」时它答不了 ——
而那种场景本来也不适合走转发。想要模型真的动工具，就直连
（`OPENPROXY_UPSTREAM_KEY`）或用 opencode 自己的 TUI。

## 已知边界

- **统计是尽力而为。** 写入走有界队列，队列满时丢弃并计数（`GET /api/health` 的
  `recorder_dropped` 可见）。优雅退出会排空队列；`SIGKILL` 会丢掉队列里的几条。
  要当账务系统用不合适。
- **未知用量是显式的。** 流式若拿不到 usage，会记成「用量未知」而不是 0，总览页会提示
  有多少条未知 —— 宁可少算也不要让总量悄悄对不上。
- **清单可能漂移。** 免费模型下线后本站不会自动加新模型，只会在「模型」页显示「上游未见」，
  并由 `test_smoke_real.py` 的第一条断言逼着人更新清单。
- **删除密钥不删历史用量。** 这是刻意的：报表不能因为一次误删而出现空洞。
- **管理端默认无令牌。** 只在绑 `127.0.0.1` 时可接受，见上文「暴露到局域网之前」。
- **走opencode 路径时，可用性由 opencode 与上游决定，本站控制不了。**
  实测「模型指定生效 8/10、真的可用 5~6/10」，且**同一个模型今天 4 秒返回、
  明天 150 秒超时**（实测某个模型一次 4.7 秒、另一次 113 秒）。
  所以「上次能用」不代表现在能用 —— 升级 opencode 或换机器后跑一次
  `scripts/probe-opencode-models.py`。控制台上的开关只决定「走哪条路」，
  不保证那条路上那个模型此刻可用。
- **本机直连不受 `http_proxy` 等环境变量影响**（`trust_env=False`）。
  这一条是**踩过的坑**：httpx 默认 `trust_env=True` 会读环境变量，于是
  连本机的 opencode（`127.0.0.1:4096`）都被转发给 `http_proxy` 指向的代理，
  代理连不上就回 `502 upstream connect failed` —— 本站记成
  `504 upstream_unreachable`，症状是「明明 opencode 在跑、端口也对，
  转发就是超时」，而排查方向会被完全带偏。上游（`opencode.ai`）要不要走代理
  由 `upstream_proxy` **显式**控制。

## 运维脚本

| 脚本 | 用途 |
|---|---|
| `scripts/probe-opencode-models.py` | 逐个探测经 opencode 转发的模型：**核对实际服务的模型名**（不看 HTTP 200），并区分「模型指定生效」与「真的可用」。零第三方依赖，密码默认读 `~/.config/opencode/service.json` |
| `tests/probe_free_tier.sh` | 复现「站外直连被拒」的现象（见上文「站外可调」） |

```bash
# 起 opencode（密码会打到 stdout）
opencode serve --port 5003 --hostname 127.0.0.1

# 逐模型探测
python3 scripts/probe-opencode-models.py --base http://127.0.0.1:5003
```

退出码 0 = 全部模型都真的可用；1 = 有任何一项不符。可用在升级 opencode 后的自检。

## 许可

MIT