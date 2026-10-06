#!/usr/bin/env python3
"""逐个探测「经 opencode 服务转发」的真实可用性。

## 为什么需要这个脚本

**opencode 侧的可用性会漂移**：同一个模型今天 4 秒返回、明天 150 秒超时；
今天能用、下周上游端点挂了（实测 2026-10-05：``ling-3.0-flash-fin-free``
与 ``mimo-v2.5-free`` 返回 ``Endpoint is unavailable``）。而控制台上看到的
「转发成功」只说明本站的转发链路是通的，**不代表那个模型真能用**。

所以要有一个能一键复测全部模型、并**逐个核对实际模型名**的脚本。

## 判定标准（关键）

**不看 HTTP 200** —— ``/prompt`` 传任何模型参数都返回 ``200 {"delivery":"steer"}``，
那个「steer」与「模型对不对」毫无关系（2026-10-05 曾据此得出「模型指定不了」
的错误结论）。

唯一可信的判据是 ``GET /api/session/<id>/message`` 里 assistant 消息的
``model.id`` —— 本脚本据此输出「请求哪个 / 实际哪个」两列。

## 用法

```bash
# 1. 起opencode（密码会打到 stdout）
opencode serve --port 5002 --hostname 127.0.0.1

# 2. 跑探测（把密码与端口填进去）
python3 scripts/probe-opencode-models.py --base http://127.0.0.1:5002 \\
    --password <上面打印的密码> [--timeout 150]
```

退出码：全部模型都指定成功 = 0；有任何一项不符= 1。可直接用在 CI 或升级后自检。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: 本项目的 free 模型清单（与 ``openproxy.service.model_catalog.FREE_MODELS`` 一致）。
#: 刻意写成字面量而不是 import —— 这个脚本要能在**没有装 openproxy 包**的环境里跑。
MODELS = [
    "big-pickle",
    "fledge-alpha-free",
    "ling-3.0-flash-fin-free",
    "ling-3.1-flash-free",
    "longcat-2.5-preview-free",
    "mimo-v2.5-free",
    "mimo-v2.6-flash-free",
    "musketeer-1",
    "nemotron-3-ultra-free",
    "space-bunny-free",
]


@dataclass
class Outcome:
    model: str
    actual: str | None
    seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        """「指定生效」**且**「真的答了」才算成功。

        曾经写成只比 ``actual == model`` —— 那会把「模型对但调用失败」也标成 OK：
        实测 8/10 里有 2 个带着 ``provider.invalid-request`` /
        ``retry 2 次仍失败`` 的错误信息，却因为模型名对而被算成成功。
        **可用**要求两件事都对。
        """
        return self.actual == self.model and not self.failed

    @property
    def failed(self) -> bool:
        """有上游错误信息 —— 模型被指定到了，但它没能完成这次调用。"""
        return bool(self.error)


class OpencodeClient:
    """最小HTTP 客户端（不用 httpx —— 让脚本零第三方依赖）。"""

    def __init__(self, base: str, password: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(
            f"opencode:{password}".encode()
        ).decode()
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": self.auth},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            return {"_http_error": e.code,
                    "_body": e.read().decode()[:200]}
        return json.loads(raw) if raw else {}

    def probe(self, model: str, poll_interval: float,
              wait_seconds: float) -> Outcome:
        """建会话（**模型在这里指定**）-> 投递 -> 轮询到结束。

        模型必须**建会话时**给，形状是
        ``{"model": {"id":…, "providerID": "opencode", "modelID": …}}``
        —— ``id`` 与 ``modelID`` 都要有，只给后两个会被拒成
        ``400 Missing key at ["model"]["id"]``（实测 2026-10-05）。
        """
        t0 = time.monotonic()
        created = self.call("POST", "/api/session", {"model": {
            "id": model, "providerID": "opencode", "modelID": model,
        }})
        sid = (created.get("data") or {}).get("id")
        if not sid:
            return Outcome(model, None, time.monotonic() - t0,
                           f"建会话失败：{json.dumps(created)[:120]}")

        try:
            # **不带任何 model 参数** —— 实测在这里传会被静默忽略
            self.call("POST", f"/api/session/{sid}/prompt",
                      {"text": "回答 exactly: 1+1=? 只答数字"})
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                msgs = self.call(
                    "GET", f"/api/session/{sid}/message").get("data") or []
                for msg in msgs:
                    if msg.get("type") == "assistant":
                        actual = (msg.get("model") or {}).get("id")
                        detail = _failure_of(msg)
                        return Outcome(
                            model, str(actual), time.monotonic() - t0,
                            detail or ("" if msg.get("content")
                                       else "无正文（content 为空）"),
                        )
                for msg in msgs:
                    if msg.get("type") in ("error", "aborted"):
                        return Outcome(
                            model, None, time.monotonic() - t0,
                            _detail_of(msg),
                        )
                if any(m.get("type") == "idle" for m in msgs):
                    return Outcome(model, None, time.monotonic() - t0,
                                   "本轮结束但没有回复内容")
            return Outcome(model, None, time.monotonic() - t0,
                           f"超过 {wait_seconds:.0f}s 仍未回复")
        finally:
            self.call("DELETE", f"/api/session/{sid}")


def _failure_of(msg: dict) -> str:
    """assistant 消息自带的失败信息（实测很多模型不可用时写在这里）。"""
    if msg.get("finish") == "error":
        err = msg.get("error")
        if isinstance(err, dict):
            return f"{err.get('type', '')}: {err.get('message', '')}".strip(": ")
        return str(err or "上游返回 error")
    retry = msg.get("retry")
    if isinstance(retry, dict) and retry.get("error"):
        err = retry["error"]
        msg_text = err.get("message", "") if isinstance(err, dict) else str(err)
        return (f"重试 {retry.get('attempt', '?')} 次仍失败：{msg_text}")
    return ""


def _detail_of(msg: dict) -> str:
    for key in ("error", "message", "reason", "detail"):
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:150]
        if isinstance(v, dict):
            inner = v.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()[:150]
    return json.dumps(msg, ensure_ascii=False)[:150]


async def main_async(args: argparse.Namespace) -> int:
    client = OpencodeClient(args.base, args.password, args.timeout)
    print("=" * 78)
    print("逐模型探测：经 opencode 服务转发")
    print(f"  服务 {args.base}   超时 {args.wait:.0f}s   轮询间隔 {args.interval}s")
    print("=" * 78)
    print("  判定标准：请求的模型 == 实际服务的模型（不看 HTTP 200）")
    print()

    results: list[Outcome] = []
    for model in args.models or MODELS:
        out = await asyncio.to_thread(
            client.probe, model, args.interval, args.wait,
        )
        results.append(out)
        # 三态而不是两态：「指定生效」与「真的可用」是两件事 ——
        # 模型名对但调用失败（上游端点故障）时，两者不一致，而那恰恰是
        # 最需要被看见的情况（只看「模型指定生效 8/10」会以为能用8 个）。
        if out.ok:
            mark = "OK  "
        elif out.actual == out.model:
            mark = "半可"      # 模型对，但这次调用失败
        else:
            mark = "FAIL"
        got = out.actual or "(未拿到模型名)"
        line = (f"  [{mark}] {model:<28} 实际={got:<28}"
                f" {out.seconds:>6.1f}s")
        if out.error:
            line += f"  {out.error[:56]}"
        print(line, flush=True)

    usable = [r for r in results if r.ok]
    named = [r for r in results if r.actual == r.model]
    broken = [r for r in results if r.actual is None]
    print()
    print("=" * 78)
    print(f"模型指定生效 {len(named)}/{len(results)}"
          f"（即「请求的模型就是实际服务的模型」）")
    print(f"真的可用     {len(usable)}/{len(results)}"
          f"（指定生效 **且** 这次调用没报错）")
    if named:
        print(f"  指定生效：{', '.join(r.model for r in named)}")
    if broken:
        print(f"  完全没调通：{', '.join(r.model for r in broken)}")
    degraded = [r for r in results if r.actual == r.model and r.failed]
    if degraded:
        print()
        print("  「指定生效但这次失败」—— 上游模型自己的问题，与本站无关：")
        for r in degraded:
            print(f"    {r.model}: {r.error[:70]}")
    print("=" * 78)
    return 0 if len(usable) == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="逐个探测经 opencode 服务的模型可用性",
    )
    ap.add_argument("--base", default="http://127.0.0.1:5002",
                    help="opencode serve 的地址")
    ap.add_argument("--password", default=None,
                    help="服务密码；不给则读 ~/.config/opencode/service.json")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="单次 HTTP 超时（秒）")
    ap.add_argument("--wait", type=float, default=150.0,
                    help="每个模型的总等待预算（秒）")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="轮询间隔（秒）")
    ap.add_argument("--models", nargs="*", help="只测这几个模型")
    args = ap.parse_args()

    if args.password is None:
        svc = Path.home() / ".config/opencode/service.json"
        try:
            args.password = json.loads(svc.read_text())["password"]
        except Exception as exc:
            print(f"读不到 {svc}：{exc}\n请用 --password 传。", file=sys.stderr)
            return 2

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
