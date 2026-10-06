"""pytest 共享夹具。

**不在这里调用 ``load_dotenv()``**（R15）：那会把开发者本机的 ``.env`` 注入整个
pytest 会话的 ``os.environ``，让「默认值」断言在别人机器上、在某个 import 顺序下
突然失败。测试需要的每个环境变量都用 ``monkeypatch.setenv`` 显式设置。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from openproxy.app import create_app
from openproxy.config import Settings
from openproxy.container import Container
from tests.support.upstream import FakeUpstream, standard_upstream

# 钉死时区偏移，让「今天」的边界在测试里是确定的（默认 +480 = UTC+8）
FIXED_TZ_OFFSET = 480


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """清掉所有 ``OPENPROXY_*`` 环境变量，保证测试之间互不影响。

    **只清环境变量，不 chdir。** ``load_settings()`` 也会读 ``./.env``
    （真实功能），而本机那个 ``.env`` 里写着 ``OPENPROXY_REASONING_EFFORT=max`
    与 opencode 密码 —— 实测（review 的 A-3）它会泄漏进测试，且一次失败的
    断言会把**真实密码**打印进 CI 输出。

    曾经在这里加 ``monkeypatch.chdir(tmp_path)`` 来彻底隔离，实测让
    ``test_probe_schedule`` 从 1.5 秒涨到 **10 秒**（慢 7 倍）——
    CWD 变成临时目录后某些相对路径解析失败会走重试/超时分支。
    代价大于收益，所以撤回；``.env`` 的隔离由 ``TestDotenv`` 内部的
    ``monkeypatch.chdir`` 就地完成（自己 chdir、自己复原）。

    真正的根治是 review 的 B-3：三个密钥字段已设``repr=False``，
    所以即使泄漏也不会打印出凭据。
    """
    for name in [k for k in os.environ if k.startswith("OPENPROXY_")]:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=str(tmp_path / "openproxy.db"))


@pytest.fixture
def container(settings: Settings, upstream: FakeUpstream) -> Iterator[Container]:
    """构造容器但**不启动 pruner 循环**（测试里不需要后台任务）。"""
    c = Container.build(settings, transport=upstream, tz_offset_minutes=FIXED_TZ_OFFSET)
    try:
        yield c
    finally:
        c.recorder.close()
        c.database.close()


@pytest.fixture
def upstream() -> FakeUpstream:
    return standard_upstream()


@pytest.fixture
def client(settings: Settings, upstream: FakeUpstream) -> Iterator[TestClient]:
    app = create_app(
        settings,
        transport=upstream,
        start_pruner=False,
        start_prober=False,
        tz_offset_minutes=FIXED_TZ_OFFSET,
    )
    with TestClient(app) as c:
        yield c


def flush(client: TestClient) -> dict[str, object]:
    """排空异步写入队列，让断言立刻看到落库结果。"""
    response = client.post("/api/admin/maintenance/flush")
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json())
