"""运行期配置：环境变量基线 + 持久化覆盖层。

分三层，每层职责单一：

``Settings``
    进程启动时从环境变量读一次，不可变。是**基线**。
``Overlays``
    控制台「设置」页写入数据库的可变覆盖项，不可变快照。
``RuntimeConfig``
    前两者合成后的**有效配置**。所有运行期组件只读它，不直接读环境变量。

**为什么不让组件直接读环境变量**：R23 要求覆盖只通过公开 setter 落到 DI 解析出的
单例上；如果 `ProxyService` 自己去 `os.environ.get`，那么控制台改了设置就必须重建
服务实例，而重建会静默丢掉注入的 recorder / store。这里用一个显式的合成点代替重建。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Final

from openproxy.domain import VALID_REASONING_EFFORTS

ENV_PREFIX: Final = "OPENPROXY_"

logger = logging.getLogger(__name__)

DEFAULT_UPSTREAM_BASE: Final = "https://opencode.ai/zen"
DEFAULT_UPSTREAM_USER_AGENT: Final = "openproxy/1.0 (+https://github.com/local/openproxy)"
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8787

#: 思考级别的合法取值。定义在领域层（``config`` 反向依赖 ``domain`` 是允许的方向，
#: 反过来会让依赖图成环），这里只做本地别名。
VALID_EFFORTS: Final = VALID_REASONING_EFFORTS

#: 「站外可达性」每日探测的默认开关。定义在这里而不是 ``container``：配置层不能
#: 反向依赖装配层。
PROBE_REACHABILITY_DEFAULT: Final = True
DEFAULT_OPENCODE_BASE: Final = "http://127.0.0.1:4096"
"""本机 opencode 服务的默认地址。

`opencode serve --port N` 的默认端口是 4096。**注意桌面版的 Bun 子进程用的是
随机端口**（实测 49374），所以桌面版用户必须显式设这个值或者重启服务固定端口 ——
这是「走 opencode 服务」这条路最大的脆弱点，写在这里是为了让看默认值的人
立刻知道，而不是等到 502 才发现。
"""
DEFAULT_OPENCODE_TIMEOUT: Final = 120.0
"""等 opencode 回完的默认秒数。实测最小问题也要 22~25 秒，所以不能沿用
read_timeout（默认 30 秒会在最普通的问题上就超时）。"""

#: 出站请求体上限。LLM 的 messages + tools 很容易到几百 KB，1 MiB 会误杀长会话。
DEFAULT_MAX_BODY_BYTES: Final = 16 * 1024 * 1024


class ConfigError(ValueError):
    """配置值非法。消息里必须包含出错的键名和收到的值，方便排障。"""


@dataclass(frozen=True, slots=True)
class Settings:
    """环境变量基线。字段全部有安全默认值，`load_settings()` 从不抛异常以外的错。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    upstream_base: str = DEFAULT_UPSTREAM_BASE
    # 三个密钥字段一律``repr=False``。
    #
    # dataclass 默认会进 ``repr``，而 ``repr`` 会出现在**断言失败信息**、
    # ``logger.exception``、调试器的变量展开里 —— 实测 2026-10-05 做变异测试时，
    # 一个失败的 pytest 断言把本机``.env`` 里的真实 opencode 密码
    # 原样打印进了 CI 输出。「加遮罩」只保护了 ``--print-config`` 与
    # ``public_dict()`` 两条路，而 ``repr`` 是**最容易被漏掉的那条**。
    upstream_key: str | None = field(default=None, repr=False)
    upstream_user_agent: str = DEFAULT_UPSTREAM_USER_AGENT
    require_key: bool = False
    admin_token: str | None = field(default=None, repr=False)
    db_path: str = "data/openproxy.db"
    connect_timeout: float = 15.0
    read_timeout: float = 600.0
    retain_days: int = 90
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    free_models_only: bool = True
    inject_stream_usage: bool = True
    reasoning_effort: str | None = None
    """强制写进出站请求的思考级别。``None`` = 不动客户端的 body。

    值必须在 :data:`openproxy.service.usage_extract.REASONING_EFFORTS` 里（由
    ``__post_init__`` 校验），``None`` 之外的每个值都会真的改写上游请求。
    """
    daily_token_quota: int = 0
    """全局兜底配额（所有密钥合计的每日 token 上限）。0 表示不限制。"""
    probe_reachability: bool = PROBE_REACHABILITY_DEFAULT
    """是否跑「站外可达性」每日探测。关掉后启动与凌晨 2 点都不发那10 个请求。"""
    opencode_models: tuple[str, ...] | None = None
    """这些模型改走本机opencode 服务转发，其余仍直通。

    这是**环境变量基线**；控制台改的是同名的覆盖层（优先级更高）。
    默认 ``None`` 而非空元组：``None`` = 「环境里没设，回退到覆盖层」，
    ``()`` = 「明确设成空，即便覆盖层有值也全部直通」。合成时
    ``eff.opencode_models or ()`` 把两者都归成「空 = 全直通」，
    所以这个区分只影响「有没有回退」，不影响最终路由。
    """
    opencode_base: str = DEFAULT_OPENCODE_BASE
    """本机 opencode 服务地址（``opencode serve --port N``）。

    默认值是 ``127.0.0.1``，但**刻意允许任意主机** —— 有的人会把 opencode
    跑在另一台机器上（Docker 容器、局域网另一台）。所以这里只校验协议，
    不校验主机名（校验主机名会把那类部署直接挡掉）。
    协议校验是必须的：没有它``file:///etc/passwd`` 能过，
    而 httpx 会在发请求时抛 ``UnsupportedProtocol``，
    被 ``except Exception`` 归成「服务不可达」—— 报的错误与真正原因不符。
    """
    opencode_password: str | None = field(default=None, repr=False)
    """opencode 服务密码。``None`` = 自动读 ``~/.config/opencode/service.json``。

    ``repr=False``：这个值 ``None`` 时也会被自动填上（见
    :func:`~openproxy.service.opencode_client.discover_password`），
    所以它**经常是已设置的**，进 ``repr`` 就等于经常泄漏。"""
    opencode_directory: str | None = None
    """opencode 工作区目录。``None`` = 用它服务默认的。"""
    opencode_timeout: float = DEFAULT_OPENCODE_TIMEOUT
    """等 opencode 回完的秒数上限（实测最小问题就要 22~25 秒）。"""

    def __post_init__(self) -> None:
        _check("host", self.host, lambda v: bool(v.strip()))
        _check_range("port", self.port, 1, 65535)
        _check(
            "upstream_base",
            self.upstream_base,
            lambda v: v.startswith(("http://", "https://")),
        )
        _check_range("connect_timeout", self.connect_timeout, 0.001, 3600.0)
        _check_range("read_timeout", self.read_timeout, 0.001, 7200.0)
        # 与 upstream_base 同一条约束。见 opencode_base 的 docstring：
        # 不校验的话 file:// 与 gopher:// 能过，然后在 httpx 里炸成
        # UnsupportedProtocol，被报成「服务不可达」。
        _check(
            "opencode_base",
            self.opencode_base,
            lambda v: v.startswith(("http://", "https://")),
        )
        _check_range("opencode_timeout", self.opencode_timeout, 1.0, 7200.0)
        _check_range("retain_days", self.retain_days, 1, 3650)
        _check_range("max_body_bytes", self.max_body_bytes, 1024, 1 << 30)
        _check_range("daily_token_quota", self.daily_token_quota, 0, 1 << 40)
        if self.reasoning_effort is not None:
            _check("reasoning_effort", self.reasoning_effort, lambda v: v in VALID_EFFORTS)
        if self.upstream_user_agent.strip() == "":
            raise ConfigError("upstream_user_agent 不能为空：上游会拒绝空 UA（Cloudflare 1010）")


@dataclass(frozen=True, slots=True)
class Overlays:
    """控制台可改、且需要跨重启保留的覆盖项。``None`` = 不覆盖基线。

    **这里的每一个字段都必须同时出现在 ``/api/admin/settings`` 的 PATCH 模型里**，
    否则控制台上的开关会静默地不工作：Pydantic 丢弃未知字段 → 请求体被判定为空 →
    ``400 没有需要修改的字段``。这个契约由
    ``tests/test_api_admin.py::TestSettingsContract`` 钉住。
    """

    require_key: bool | None = None
    upstream_base: str | None = None
    retain_days: int | None = None
    daily_token_quota: int | None = None
    free_models_only: bool | None = None
    inject_stream_usage: bool | None = None
    reasoning_effort: str | None = None
    """``None`` 有两个含义：没设过这个覆盖（回环境变量基线），或者「显式取消强制」。
    两者对合成后的结果都是「不注入」，所以不需要区分。"""

    opencode_models: tuple[str, ...] | None = None
    """这些模型**改走本机 opencode 服务**转发，其余仍直通 ``opencode.ai/zen``。

    为什么需要它：实测（2026-10-04）10 个免费模型里有 9 个从站外直连会被上游回
    ``403 FreeTierError``，只有 ``space-bunny-free`` 能直连；而同一个模型经由
    opencode 本地服务能调通。所以「配一个上游 Key」与「让某些模型走 opencode」
    是**两条互补的路**，不是二选一 —— 前者要账号，后者不要。

    存成元组而不是 ``dict[str, bool]``：只有「在列表里」这一个状态，
    用集合语义比布尔字典更难写错（比如``{"a": False}`` 这种）。
    空元组 = 全部直通（默认，也是无 opencode 环境下的正确行为）。
    """

    _FIELD_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "require_key",
            "upstream_base",
            "retain_days",
            "daily_token_quota",
            "free_models_only",
            "inject_stream_usage",
            "reasoning_effort",
            "opencode_models",
        }
    )

    def patch(self, **changes: Any) -> Overlays:
        """返回一个只改了给定字段的新快照。未提及的字段保持不变。"""
        unknown = set(changes) - self._FIELD_NAMES
        if unknown:
            raise ConfigError(f"未知的覆盖项: {sorted(unknown)}")
        return replace(self, **changes)

    def validated(self) -> Overlays:
        """校验覆盖值本身合法。``upstream_base`` 走与基线相同的 URL 约束。

        **空白 = 取消这个覆盖**（回到环境变量的基线）。控制台上明写着「留空则用
        环境变量的值」，而在此之前 ``""`` 与 ``"   "`` 都会被 URL 校验拒成 422 ——
        于是覆盖层一旦设上就**再也没有任何一个值能清掉它**，唯一出路是「恢复默认」
        把六个字段一起清空。

        ``strip()`` 也和环境变量那条路对齐（``load_settings.text()`` 会 strip）：
        不对齐的话 ``"http://x/zen  "`` 能存进库，之后每次出站都打到
        ``/zen%20%20/v1/...``，症状是「上游突然 404」，极难联想到是这个空格。

        **累积式清洗，最后一次性replace**：早前每个字段各自「需要规范化就
        ``return replace(...)``」，于是**后面的字段永远洗不到** ——
        实测同时动``upstream_base`` 与 ``opencode_models`` 时，
        ``opencode_models`` 的 strip 被跳过，存下带空格的模型名，
        症状是「开关显示已勾选但请求永远不走 opencode」，而重启后
        ``decode_overlays`` 反而会 strip 自愈 —— 「重启前不生效、重启后生效」
        这种现象极难排查。
        """
        # 先把每个字段的「清洗后」值算出来，最后统一 replace。
        changes: dict[str, Any] = {}

        base = self.upstream_base
        if base is not None:
            trimmed = base.strip()
            if not trimmed:
                changes["upstream_base"] = None
            else:
                _check(
                    "upstream_base", trimmed,
                    lambda v: v.startswith(("http://", "https://")),
                )
                if trimmed != base:
                    changes["upstream_base"] = trimmed
        effort = self.reasoning_effort
        if effort is not None:
            # **空串是「显式取消强制」，不能归一成 ``None``**。
            # ``None`` 在合成时的含义是「没设过，回环境变量基线」——
            # 两者归 together 的话，「在界面关掉开关」会被环境变量复活：
            # 实测 ``OPENPROXY_REASONING_EFFORT=high`` 时提交空串，
            # 生效值仍是 ``high``，而界面显示已关闭、且没有任何提示。
            #
            # 与 ``opencode_models`` 同一套三态：
            # ``None`` = 没设过（回基线）、``""`` = 显式关闭（永不注入）。
            trimmed_effort = effort.strip()
            if not trimmed_effort:
                changes["reasoning_effort"] = ""
            else:
                _check("reasoning_effort", trimmed_effort, lambda v: v in VALID_EFFORTS)
                if trimmed_effort != effort:
                    changes["reasoning_effort"] = trimmed_effort
        if self.retain_days is not None:
            _check_range("retain_days", self.retain_days, 1, 3650)
        if self.daily_token_quota is not None:
            _check_range("daily_token_quota", self.daily_token_quota, 0, 1 << 40)
        if self.opencode_models is not None:
            # 逐项 strip 并丢掉空串：前端把「取消勾选」提交成 `["a", ""]` 时，
            # 不清理就会存下一个永远匹配不到的空模型名。
            cleaned = tuple(
                m.strip() for m in self.opencode_models if m and m.strip()
            )
            # 去重但**保持顺序**：顺序就是控制台上的展示顺序，
            # 而 set 会把它变成不可预测的顺序（PYTHONHASHSEED 影响 str hash）。
            deduped = tuple(dict.fromkeys(cleaned))
            if deduped != self.opencode_models:
                changes["opencode_models"] = deduped

        return replace(self, **changes) if changes else self


#: 控制台允许改的字段。「允许改」这件事本身也是契约的一部分 ——
#: 界面只渲染这里的开关，避免再出现「开关点不动」的情况。
EDITABLE_FIELDS: Final = (
    "require_key",
    "free_models_only",
    "inject_stream_usage",
    "reasoning_effort",
    "daily_token_quota",
    "retain_days",
    "upstream_base",
    "opencode_models",
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """合成后的有效配置。所有服务层组件的唯一配置来源。"""

    host: str
    port: int
    upstream_base: str
    upstream_key: str | None
    upstream_user_agent: str
    require_key: bool
    admin_token: str | None
    db_path: str
    connect_timeout: float
    read_timeout: float
    retain_days: int
    max_body_bytes: int
    free_models_only: bool
    inject_stream_usage: bool
    reasoning_effort: str | None
    daily_token_quota: int
    opencode_models: frozenset[str] = frozenset()
    """走 opencode 服务的模型 id 集合。**空= 全部直通**（默认行为）。

    存成 frozenset 是因为它只用来做「在不在里面」的判断，
    而 :meth:`ProxyService._forward` 每转发一个请求都要问一次 ——
    list 的线性查找在热门模型上会白给几十微秒，frozenset 是 O(1)。
    顺序信息不在这层（那是覆盖层的事），所以丢在这里无害。

    **不可变 + 有默认值**，所以整个 opencode 那一组字段都能带默认 ——
    否则 dataclass 会因为「``overlays``（无默认）排在有默认字段之后」而拒绝建类
    （``TypeError: non-default argument follows default argument``）。
    """

    opencode_base: str = "http://127.0.0.1:4096"
    """本机 opencode 服务地址。

    默认 4096 是 opencode 桌面版默认监听的端口（实测桌面版的 Bun 子进程
    在 127.0.0.1:49374，端口随机；``opencode serve`` 默认 4096）。
    **端口随机这件事是这套方案最大的脆弱点** —— 见 :class:`OpencodeSettings`。
    """

    opencode_password: str | None = None
    """opencode 服务的 Basic 认证密码。``None`` = 自动从
    ``~/.config/opencode/service.json`` 读（推荐，见
    :func:`openproxy.service.opencode_client.discover_password`）。"""

    opencode_directory: str | None = None
    """opencode 的工作区目录。``None`` = 用它服务默认的。"""

    opencode_timeout: float = DEFAULT_OPENCODE_TIMEOUT
    """等 opencode 回完的秒数上限。

    比 ``read_timeout`` 大一个量级：opencode 是「投递后异步生成」，
    实测最小问题也要 22~25 秒（含首 token），复杂问题几分钟很常见。
    """

    # ``overlays`` 也给了默认值（``None`` → 空覆盖层），否则 dataclass 会拒绝建类：
    # 它是唯一一个没默认值的字段，却排在``opencode_*`` 那组（有默认值）之后，
    # 报``TypeError: non-default argument 'overlays' follows default argument``。
    # 给了默认值之后「忘记传」不再是错误 —— :meth:`compose` 里
    # ``overlays or Overlays()`` 会把它归一成空覆盖层，语义上也对。
    overlays: Overlays = field(default_factory=Overlays)

    def uses_opencode(self, model: str) -> bool:
        """这个模型是否走 opencode 服务。

        单独一个方法而不是让调用方直接 ``model in config.opencode_models``：
        以后要加「按前缀通配」「按供应商批量」之类的规则时，
        只有这一处需要改，而不会散落在代理层的分支里。
        """
        return model in self.opencode_models

    @classmethod
    def compose(cls, settings: Settings, overlays: Overlays | None = None) -> RuntimeConfig:
        eff = (overlays or Overlays()).validated()
        return cls(
            host=settings.host,
            port=settings.port,
            upstream_base=(eff.upstream_base or settings.upstream_base).rstrip("/"),
            upstream_key=settings.upstream_key,
            upstream_user_agent=settings.upstream_user_agent,
            require_key=settings.require_key if eff.require_key is None else eff.require_key,
            free_models_only=(
                settings.free_models_only
                if eff.free_models_only is None
                else eff.free_models_only
            ),
            inject_stream_usage=(
                settings.inject_stream_usage
                if eff.inject_stream_usage is None
                else eff.inject_stream_usage
            ),
            # 三态合成（与 ``opencode_models`` 同一套）：
            # ``None`` = 没设过 -> 回环境变量基线；
            # ``""``   = 显式关闭 -> **永不注入**，环境变量设了也不管；
            # 其它合法档位= 用它。
            # 不能写成 ``eff.reasoning_effort or settings.reasoning_effort`` ——
            # 那会把 ``""`` 也当成「没设过」，于是关不掉。
            reasoning_effort=(
                None
                if eff.reasoning_effort == ""
                else (
                    settings.reasoning_effort
                    if eff.reasoning_effort is None
                    else eff.reasoning_effort
                )
            ),
            admin_token=settings.admin_token,
            db_path=settings.db_path,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
            retain_days=settings.retain_days if eff.retain_days is None else eff.retain_days,
            max_body_bytes=settings.max_body_bytes,
            daily_token_quota=(
                settings.daily_token_quota
                if eff.daily_token_quota is None
                else eff.daily_token_quota
            ),
            # 三态合成：覆盖层显式设了（哪怕是空列表）就用它，否则回环境变量基线。
            # 写成 `eff.opencode_models or ()` 会把「覆盖层设成空列表」与「覆盖层
            # 没设」混成同一件事 —— 于是用户在控制台清空所有勾选后，环境变量里
            # 设的 OPENPROXY_OPENCODE_MODELS 会悄悄复活。
            opencode_models=frozenset(
                eff.opencode_models
                if eff.opencode_models is not None
                else (settings.opencode_models or ())
            ),
            opencode_base=settings.opencode_base,
            opencode_password=settings.opencode_password,
            opencode_directory=settings.opencode_directory,
            opencode_timeout=settings.opencode_timeout,
            overlays=eff,
        )

    def public_dict(self) -> dict[str, Any]:
        """给控制台看的快照。**绝不包含** ``upstream_key`` / ``admin_token`` 的值。"""
        return {
            "host": self.host,
            "port": self.port,
            "upstream_base": self.upstream_base,
            "upstream_authenticated": bool(self.upstream_key),
            "upstream_user_agent": self.upstream_user_agent,
            "require_key": self.require_key,
            "admin_protected": bool(self.admin_token),
            "retain_days": self.retain_days,
            "max_body_bytes": self.max_body_bytes,
            "free_models_only": self.free_models_only,
            "inject_stream_usage": self.inject_stream_usage,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_efforts": list(VALID_EFFORTS),
            "daily_token_quota": self.daily_token_quota,
            # 按**有序列表**回而不是 frozenset：前端要用它渲染勾选框，
            # 而集合的迭代顺序不可预测 —— 那样每次刷新勾选框顺序可能不一样。
            # 排序用「先按清单顺序、再按字典序兜底」，所以既稳定又与
            # 模型页的展示顺序一致。
            "opencode_models": sorted(self.opencode_models),
            "opencode_base": self.opencode_base,
            "opencode_timeout": self.opencode_timeout,
            "overlays": {
                "require_key": self.overlays.require_key,
                "upstream_base": self.overlays.upstream_base,
                "retain_days": self.overlays.retain_days,
                "daily_token_quota": self.overlays.daily_token_quota,
                "free_models_only": self.overlays.free_models_only,
                "inject_stream_usage": self.overlays.inject_stream_usage,
                "reasoning_effort": self.overlays.reasoning_effort,
                #覆盖层里的原始值（可能是 ``None`` = 回环境变量基线）。
                # 控制台用它区分「这一项被显式设过」与「正在用基线」——
                # 两者合成后的结果可能相同，但含义不同，清空的方式也不同。
                "opencode_models": (
                    None if self.overlays.opencode_models is None
                    else list(self.overlays.opencode_models)
                ),
            },
            "editable": EDITABLE_FIELDS,
        }


# --------------------------------------------------------------------- 解析 ---


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """读 ``.env`` 文件，返回它解析出的键值。

    ## 优先级：**真实环境变量 > ``.env`` 文件**

    这与python-dotenv 的默认相反，而**必须如此**：``.env`` 是给人手写的基线，
    而 ``docker run -e OPENPROXY_PORT=9000`` 这类显式传参是「这一次的意图」，
    应当压过文件里的值。反过来的话，容器编排怎么传参都没用。

    ## 格式（刻意只支持这一种）

    ``#`` 注释、空行忽略、``KEY=VALUE``、可选的成对引号（``"…"`` / ``'…'``）。
    **不支持** ``export`` 前缀、多行值、变量插值 —— 那些是 shell 的语法，
    而这里是「一个键一个值」的配置文件，多支持一种就多一种能写错的写法。

    解析失败**不抛异常**：``.env`` 写错了不该让服务起不来（那会让一个
    笔误变成停机）。坏行**跳过**，并由 :func:`load_settings` 打一条
    ``warning`` 说「有 N 行没看懂」—— 静默与不致命是两件事：
    服务起来了但用户永远不知道自己少配了一项，那是更坏的失败。

    ## 为什么不在 import 期调用

    R15：import 期读文件会污染测试（本机的 ``.env`` 会影响默认值断言）。
    所以只在 :func:`load_settings` 里显式调用，测试可以传入 ``env=``绕开。
    """
    target = Path(path) if path is not None else _default_dotenv_path()
    parsed: dict[str, str] = {}
    bad: list[str] = []
    try:
        # **必须是 ``utf-8-sig`` 而不是 ``utf-8``**（实测 2026-10-05）。
        # Windows 记事本与 VS Code 的「以 UTF-8 with BOM 保存」是**默认行为**，
        # 而 ``utf-8`` 不会剥 BOM —— 于是第一个键变成 ``\ufeffOPENPROXY_PORT``，
        # 一个永远匹配不上任何 ``ENV_PREFIX + key`` 的垃圾键。
        #
        # 症状是**完全静默**的：``.env`` 第一行明明写着 10490，
        # ``--print-config`` 却打印 8787，没有任何警告。而模板的第一项安全配置
        # 恰好是 ``OPENPROXY_ADMIN_TOKEN=`` —— 它失效意味着
        # 「绑 0.0.0.0 又留空 = 管理接口裸奔」这条防线被无声拆掉。
        #
        # ``utf-8-sig`` 对无 BOM 文件是**恒等变换**，所以没有副作用。
        raw = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        # 文件不存在是最常见情况（首次运行），不是错误。
        _warn_bad_lines(bad)
        return parsed
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            # 空值行（只有键没有 ``=``）是坏行；注释与空行不是。
            bad.append(stripped)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            # 支持 `export KEY=VALUE` 但去掉前缀 —— 它是常见的复制粘贴产物
            key = key[len("export "):].strip()
        if not key:
            bad.append(stripped)
            continue
        value = value.strip()
        # 去掉成对引号；**不成对的引号保留原样**（那更可能是值的一部分，
        # 比如路径末尾的 "）。``len>= 2`` 是必需的：单字符的 ``"`` 不是成对的，
        # 而 ``value[1:-1]`` 对它切片会得到空串 —— **静默吞掉那个字符**
        # （review 的 A-7 实测）。
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[key] = value
    _warn_bad_lines(bad)
    return parsed


def _warn_bad_lines(bad: list[str]) -> None:
    """坏行要说一声 —— 但**只说行数与内容，不中止启动**。

    「不致命」与「静默」是两件事：服务起来了却没人知道自己少配了一项，
    那比直接失败更难查（实测review 的 B-5：``OPENPROXY ADMIN_TOKEN=x``
    这种带空格的中键会静默丢弃，管理令牌就没设上）。

    刻意**不指出行号** —— 我们是按行 split 的，行号只在把 ``.env`` 当代码读
    的时候才有用；而用户要的是「哪几行没看懂」这个事实本身。
    """
    if not bad:
        return
    preview = "；".join(bad[:3])
    more = f"（另有 {len(bad) - 3} 行）" if len(bad) > 3 else ""
    logger.warning(
        ".env 里有 %d 行没看懂，已跳过：%s%s。"
        "每行应该是 KEY=VALUE（等号两边不要有空格）。",
        len(bad), preview, more,
    )


def _default_dotenv_path() -> Path:
    """``.env`` 的默认位置：当前工作目录。

    刻意用 CWD 而不是包目录 —— ``.env`` 是**部署**配置，属于「这个服务跑在
    哪儿」，跟代码装在哪儿无关。
    """
    return Path(".env")


def merged_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """把 ``.env`` 与真实环境变量合成，**真实环境变量优先**。

    合成后的结果里，``.env`` 独有的键会被补进来，而真实环境里同名的键
    **保持真实值**。这样 :func:`load_settings` 拿到的映射与「直接 export 了
    全部变量」等价，不需要在下游区分变量来自哪儿。
    """
    merged = load_dotenv()
    merged.update(os.environ if env is None else env)
    return merged


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """从环境变量构造基线配置。

    来源优先级：``os.environ`` > **``.env`` 文件** > 内置默认值。

    ``env`` 参数用于测试：显式传入映射就完全不碰真实环境与 ``.env`` 文件
    （R15：不在 import 期调用 ``load_dotenv``，否则默认值断言会被开发者本机
    的 ``.env`` 污染）。
    """
    source = merged_env(env) if env is None else env
    defaults = Settings()

    def text(key: str, fallback: str) -> str:
        """字符串项。**空白等于未设置** —— 容器编排里 `PORT=""` 这种写法很常见，
        按字面解释会得到「监听空地址」这种启动后才崩的坏配置。"""
        raw = source.get(ENV_PREFIX + key)
        if raw is None:
            return fallback
        return raw.strip() or fallback

    def optional(key: str, fallback: str | None) -> str | None:
        raw = source.get(ENV_PREFIX + key)
        if raw is None:
            return fallback
        raw = raw.strip()
        return raw or None

    def number(key: str, fallback: float) -> float:
        raw = source.get(ENV_PREFIX + key)
        if raw is None or not raw.strip():
            return fallback
        try:
            return float(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{key} 需要一个数字，收到 {raw.strip()!r}") from exc

    def integer(key: str, fallback: int) -> int:
        raw = source.get(ENV_PREFIX + key)
        if raw is None or not raw.strip():
            return fallback
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{key} 需要一个整数，收到 {raw.strip()!r}") from exc

    def flag(key: str, fallback: bool) -> bool:
        raw = source.get(ENV_PREFIX + key)
        if raw is None or not raw.strip():
            return fallback
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{ENV_PREFIX}{key} 需要布尔值(true/false)，收到 {raw.strip()!r}")

    def csv(key: str, fallback: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """逗号分隔的列表。空串= 未设（回``fallback``），不是「空列表」。

        刻意区分这两者：``OPENPROXY_OPENCODE_MODELS=""`` 表示「我环境里没有这个变量，
        请回退到数据库里的覆盖层」，而 ``= ","`` 表示「明确要一个空列表」。
        不过 :meth:`Overlays.validated` 之后两者会归一（空列表 ≡ 全部直通），
        所以这个区分只影响「有没有回退」，不影响最终行为。
        """
        raw = source.get(ENV_PREFIX + key)
        if raw is None or not raw.strip():
            return fallback
        return tuple(m.strip() for m in raw.split(",") if m.strip())

    return Settings(
        host=text("HOST", defaults.host),
        port=integer("PORT", defaults.port),
        upstream_base=text("UPSTREAM_BASE", defaults.upstream_base),
        upstream_key=optional("UPSTREAM_KEY", None),
        upstream_user_agent=text("UPSTREAM_USER_AGENT", defaults.upstream_user_agent),
        require_key=flag("REQUIRE_KEY", defaults.require_key),
        admin_token=optional("ADMIN_TOKEN", None),
        db_path=text("DB_PATH", defaults.db_path),
        connect_timeout=number("CONNECT_TIMEOUT", defaults.connect_timeout),
        read_timeout=number("READ_TIMEOUT", defaults.read_timeout),
        retain_days=integer("RETAIN_DAYS", defaults.retain_days),
        max_body_bytes=integer("MAX_BODY_BYTES", defaults.max_body_bytes),
        free_models_only=flag("FREE_MODELS_ONLY", defaults.free_models_only),
        inject_stream_usage=flag("INJECT_STREAM_USAGE", defaults.inject_stream_usage),
        reasoning_effort=optional("REASONING_EFFORT", None),
        daily_token_quota=integer("DAILY_TOKEN_QUOTA", defaults.daily_token_quota),
        probe_reachability=flag(
            "PROBE_REACHABILITY", PROBE_REACHABILITY_DEFAULT
        ),
        opencode_models=csv("OPENCODE_MODELS", None),
        opencode_base=text("OPENCODE_BASE", defaults.opencode_base),
        opencode_password=optional("OPENCODE_PASSWORD", None),
        opencode_directory=optional("OPENCODE_DIRECTORY", None),
        opencode_timeout=number("OPENCODE_TIMEOUT", defaults.opencode_timeout),
    )


# ----------------------------------------------------------------- 内部工具 ---


def _check(name: str, value: str, predicate: Any) -> None:
    if not predicate(value):
        raise ConfigError(f"{name} 非法: {value!r}")


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not (low <= value <= high):
        raise ConfigError(f"{name} 必须在 [{low}, {high}] 内，收到 {value!r}")
