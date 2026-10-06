"""命令行入口：``openproxy`` / ``python -m openproxy``。

参数优先级：**命令行 > 环境变量 > 内置默认值**。命令行是「这一次启动」的临时覆盖，
不会写进数据库的覆盖层，所以退出后不残留。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from typing import Any

import uvicorn

from openproxy.app import configure_logging, create_app
from openproxy.config import ConfigError, Settings, load_settings
from openproxy.service.opencode_supervisor import (
    Health,
    OpencodeUnavailable,
    Started,
    ensure_opencode,
    local_endpoint,
    resolve_password,
)

logger = logging.getLogger(__name__)

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openproxy",
        description="转发到 opencode 免费模型的中转站，带用量统计与控制台",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="监听端口（默认 8787）")
    parser.add_argument("--upstream", help="上游 base url（默认 https://opencode.ai/zen）")
    parser.add_argument("--upstream-key", help="转发这个上游 key；不填则匿名访问上游")
    parser.add_argument("--db", help="SQLite 文件路径（默认 data/openproxy.db）")
    parser.add_argument("--require-key", action="store_true", help="强制下游必须带本站签发的密钥")
    parser.add_argument("--admin-token", help="控制台管理令牌；不填则不校验")
    parser.add_argument("--retain-days", type=int, help="用量保留天数（默认 90）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument(
        "--print-config", action="store_true", help="只打印生效配置（不启动服务）后退出"
    )
    parser.add_argument(
        "--no-opencode-autostart",
        action="store_true",
        help="不要在 opencode_base 上没有进程时自动起一个（默认会起）",
    )
    parser.add_argument(
        "--no-opencode-check",
        action="store_true",
        help="完全跳过 opencode 可用性检查（调障用；opencode 不可用时本站会每个请求都 504）",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """命令行覆盖 → 最终 Settings。"""
    base = load_settings()
    overrides: dict[str, Any] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.upstream:
        overrides["upstream_base"] = args.upstream
    if args.upstream_key:
        overrides["upstream_key"] = args.upstream_key
    if args.db:
        overrides["db_path"] = args.db
    if args.require_key:
        overrides["require_key"] = True
    if args.admin_token:
        overrides["admin_token"] = args.admin_token
    if args.retain_days is not None:
        overrides["retain_days"] = args.retain_days
    return dataclasses.replace(base, **overrides) if overrides else base


def ensure_opencode_ready(
    settings: Settings, args: argparse.Namespace
) -> Settings:
    """启动前确认 opencode **真的可用**，不可用就退出。

    ## 为什么不自动启动而是退出

    一个转发站的上游没起来时，启动它没有意义 —— 而「起来但每个请求都 504」
    比「起不来」更难排查。所以这里宁可拒绝启动。

    ## 为什么密码默认自动读

    ``opencode serve`` 每次启动生成的密码**都不同**（实测多次确认），
    而那个密码写在 ``~/.config/opencode/service.json`` 里。所以显式配置的
    优先级高于自动读，但**留空时自动读**才是长期可用的做法 ——
    手填的必然会在某次重启后过期。

    用户自己接的外部 opencode（``opencode_base`` 不指向本机）**只检查不托管**。
    """
    if args.no_opencode_check:
        logger.warning(
            "已跳过 opencode 可用性检查 —— 它不可用时本站每个转发请求都会 504"
        )
        return settings
    base = settings.opencode_base
    password = resolve_password(settings.opencode_password)
    try:
        result = asyncio.run(
            ensure_opencode(
                base, password, auto_start=not args.no_opencode_autostart,
            )
        )
    except OpencodeUnavailable as exc:
        print(f"opencode 不可用: {exc}", file=sys.stderr)
        if exc.detail:
            print(f"  → {exc.detail}", file=sys.stderr)
        print(
            "  → 只想跳过这步检查加 --no-opencode-check"
            "（opencode 不可用时转发会全部失败）",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    if isinstance(result, Started):
        # **必须把密码回写到 settings** —— 实测 2026-10-06踩过：
        # 本站自己起的 opencode 密码只在它的 stdout 上，而代理层走的是
        # ``RuntimeConfig.opencode_password``（那份Settings 的快照）。
        # 不回写的话健康检查用的是新密码（通过）、转发用的是
        # ``service.json`` 里的旧密码（401）—— 启动日志一切正常，
        # 第一个用户请求就失败。
        settings = dataclasses.replace(
            settings, opencode_password=result.password,
        )
        # 不报模型数：真的 opencode 的 /api/model 返回空列表，
        # 报「0 个模型可用」会让人以为它没模型可用。
        logger.info("已自动启动 opencode（%s）", result.base)
    elif isinstance(result, Health) and result.ok and not _is_local(base):
        logger.info("opencode（%s）可用", base)
    return settings


def _is_local(base: str) -> bool:
    return local_endpoint(base) is not None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        settings = settings_from_args(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if args.print_config:
        print(_safe_config_dump(settings))
        return 0

    settings = ensure_opencode_ready(settings, args)

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level.lower())
    return 0


def _safe_config_dump(settings: Settings) -> str:
    """把配置打成可粘贴的文本，**密钥只显示长度**。

    ``opencode_password`` **必须**在遮罩列表里（实测 2026-10-05）：它的
    ``None`` 会被 :func:`~openproxy.service.opencode_client.discover_password`
    自动填上 ``~/.config/opencode/service.json`` 里的值，所以**即使 ``.env``
    里没写密码，这个字段也经常是已设置状态** —— 也就是说这个命令的输出
    在绝大多数情况下都会泄漏它。

    而这个命令的用途恰恰是「贴给别人看」：贴给 AI 排查问题、丢进 issue、
    ``--print-config > cfg.log``。所以漏掉一个遮罩就等于凭据外流。
    """
    data = dataclasses.asdict(settings)
    for secret in ("upstream_key", "admin_token", "opencode_password"):
        value = data.get(secret)
        data[secret] = f"<已设置，长度 {len(value)}>" if value else "<未设置>"
    width = max(len(k) for k in data)
    return "\n".join(f"  {k.ljust(width)} = {v}" for k, v in sorted(data.items()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
