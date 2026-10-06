"""多轮 / 多轮轮询的消息合并（第三、四、五轮 review 的 B-1 / B-3-1 / B-1）。

## 为什么替身是这个文件最重要的部分

五轮review 里出了**三个 P0**，每一个的根因都是「替身语义与真实世界不符」：

| 轮次     | 替身错在哪                                  | 掩盖了什么|
|----------|-------------------------------------------|---------------------------|
| 第三轮   | 每次返回**不同批次**（假设只返回新增）       | 「重复累加」               |
| 第四轮   | 修了「历史只增不减」，没修**「消息原地增长」** | 「空 parts 窗口 → 答案丢失」 |
| 第五轮   | ——（本轮才发现前两个都没被断言守护）——      |                           |

第三条最要紧：**那两个维度本身没有任何断言在守护**。docstring 记了教训，
下一个人照样可能把替身改回错误形状。所以这里把真实语义写成
:class:`FakeOpencode` 的**时间线模型** + :class:`TestFakeMatchesReality`
的断言（``_assert_frames_grow``）。

## opencode 的真实语义（都实测过）

| 事实 | 出处 |
|---|---|
| ``GET /message`` 返回**全部**历史，无 limit/since | ``ListMessagesBySession`` |
| assistant 消息**先以空 parts 入库，再原地增长**（同一 id） | ``agent.go:326`` / ``UpdateMessage`` |
| token 在 ``EventComplete`` 时才写 | ``agent.go:507`` ``TrackUsage`` |
| 工具轮的 ``tokens`` 是 ``{}`` | 实测 2026-10-05 跑「读 /etc/hosts」 |
| 一轮 prompt 产生**多条** assistant 消息 | 工具调用一轮一条，tool_result 后再一条 |
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest

from openproxy.domain import ErrorKind
from openproxy.service.opencode_client import (
    OpencodeError,
    OpencodeSettings,
    _merge_usage,
    complete,
)

# --------------------------------------------------------------- 构造器-----


def assistant(
    *,
    mid: str = "a1",
    text: str = "",
    reasoning: str = "",
    tool: bool = False,
    tokens: dict[str, int] | None = None,
    model: str = "fledge-alpha-free",
) -> dict[str, Any]:
    """一条 assistant 消息。``text`` 与 ``reasoning`` 都为空即「空窗」形态。"""
    content: list[dict[str, Any]] = []
    if reasoning:
        content.append({"type": "reasoning", "text": reasoning})
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool", "tool": "read", "state": {}})
    return {
        "id": mid, "type": "assistant", "model": {"id": model},
        "content": content, "tokens": tokens or {},
    }


def idle(mid: str = "i1") -> dict[str, Any]:
    return {"id": mid, "type": "idle", "outcome": "succeeded"}


def failed(kind: str = "error", message: str = "上游 500") -> dict[str, Any]:
    return {"id": "e1", "type": kind, "error": {"message": message}}


def no_id_message(text: str, tokens: dict[str, int] | None = None) -> dict[str, Any]:
    """**没有 id** 的 assistant 消息（测指纹兜底用）。"""
    return {
        "type": "assistant", "model": {"id": "m"},
        "content": [{"type": "text", "text": text}],
        "tokens": tokens or {"input": 10, "output": 1},
    }


# --------------------------------------------------------------- 替身-------


class FakeOpencode:
    """按**时间线**回放 ``GET /message``。

    ## 三条真实语义

    1. **历史只增不减**：第 n 次轮询返回 ``frames[n]``，而每个 frame 必须是
       **到目前为止的全部**消息（不是新增的那几条）。超出预设就停在最后一帧。
    2. **消息原地增长**：同一条消息（``id`` 不变）从 ``content=[]`` 长到有内容
       —— 用「同一个 ``mid`` 在不同 frame 里内容不同」表达。
    3. **tokens 最后才写**：前几帧 ``tokens={}``。

    用法::

        fake = FakeOpencode([
            [assistant(mid="a1")],                      # 空窗
            [assistant(mid="a1")],                      # 还是空窗
            [assistant(mid="a1", text="答案",
                       tokens={"input": 150, "output": 30}), idle()],
        ])
        reply = await run(fake)
    """

    def __init__(self, frames: list[list[dict[str, Any]]]) -> None:
        if not frames:
            raise ValueError("frames 不能为空 —— 至少要有一次轮询的视图")
        _assert_frames_grow(frames)
        self.frames = frames
        self.polls = 0
        self.session_id = "s"
        self.deleted: list[str] = []
        #: 每个请求的 ``(方法, 路径, body)``。用来断言「模型传给了哪个端点」——
        #: 那件事从「最终是谁在答」是**看不出来**的（两者可以同时成立或同时不成立）。
        self.calls: list[tuple[str, str, Any]] = []

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        path = url.split("/", 3)[-1]
        self.calls.append((method, path, kw.get("json")))
        if path == "api/session" and method == "POST":
            return httpx.Response(200, json={"data": {"id": self.session_id}})
        if path.endswith("/prompt"):
            return httpx.Response(200, json={})
        if path.endswith("/message"):
            i = min(self.polls, len(self.frames) - 1)
            self.polls += 1
            return httpx.Response(200, json={
                "data": [dict(m) for m in self.frames[i]]
            })
        if path == f"api/session/{self.session_id}":
            self.deleted.append(self.session_id)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})


def _assert_frames_grow(frames: list[list[dict[str, Any]]]) -> None:
    """断言 frames 满足「历史只增不减」（真实语义）。

    规则：第 n+1 帧必须包含第 n 帧的**全部消息 id**（同 id 的内容可以被
    替换成更完整的版本 —— 那正是「原地增长」）。

    **在构造时就查**，而不是等某个测试间接失败 —— 那样报错信息会指向
    错误的测试，而真正的问题在替身。
    """
    for i in range(len(frames) - 1):
        before = {m.get("id") for m in frames[i]}
        after = {m.get("id") for m in frames[i + 1]}
        missing = before - after
        assert not missing, (
            f"第 {i + 1} 帧丢了第 {i} 帧已有的消息 {missing} —— "
            "历史只增不减（真实语义：GET /message 返回全部历史）"
        )


async def run(fake: FakeOpencode, *, timeout: float = 5.0,
              prompt: str = "问题", model: str = "big-pickle") -> Any:
    return await complete(
        fake,
        OpencodeSettings(password="pw", poll_interval=0.0, poll_timeout=timeout),
        prompt, model=model,
    )


def simple(text: str = "答案", *, reasoning: str = "",
           tokens: dict[str, int] | None = None) -> FakeOpencode:
    """最简单的场景：一帧、一条完整 assistant 消息 + idle。"""
    return FakeOpencode([[
        assistant(
            mid="a1", text=text, reasoning=reasoning,
            tokens=tokens if tokens is not None else {"input": 3, "output": 4},
        ),
        idle(),
    ]])


# ------------------------------------------- 守护替身本身（第五轮教训）-----


class TestFakeMatchesReality:
    """**守护替身的三条真实语义**（它们是三个 P0 的共同根因）。"""

    def test_empty_frames_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            FakeOpencode([])

    def test_shrinking_history_rejected(self) -> None:
        """历史只增不减 —— 违反就在**构造时**报错，不等到运行时。"""
        with pytest.raises(AssertionError, match="只增不减"):
            FakeOpencode([
                [assistant(mid="a1", text="先")],
                [],  # 第二帧空了
            ])

    def test_in_place_growth_is_allowed(self) -> None:
        """同 id 内容变完整是**合法**的（那正是「原地增长」）。"""
        FakeOpencode([
            [assistant(mid="a1")],
            [assistant(mid="a1", text="答案", tokens={"input": 9})],
            [assistant(mid="a1", text="答案", tokens={"input": 9}), idle()],
        ])  # 不抛即通过

    def test_new_messages_may_be_appended(self) -> None:
        FakeOpencode([
            [assistant(mid="a1", text="一")],
            [assistant(mid="a1", text="一"), assistant(mid="a2", text="二")],
        ])

    @pytest.mark.asyncio
    async def test_polls_stop_at_the_last_frame(self) -> None:
        """超出预设后停在最后一帧 —— 模拟「模型不再变化」。

        具体表现：第一帧就带 ``idle`` 时，一次轮询就结束 ——
        而真实轮询会持续到看见 ``idle`` 为止（这里已经看见了）。
        """
        fake = simple("答案")
        await run(fake)
        assert fake.polls == 1, "第一帧已有idle，不该再轮询"

    @pytest.mark.asyncio
    async def test_extra_polls_reuse_the_last_frame(self) -> None:
        """末帧没带 idle 时会反复读它 —— 所以会话可能被读多次。"""
        ready = assistant(mid="a1", text="答案", tokens={"input": 9, "output": 1})
        fake = FakeOpencode([[ready]])  # 永远没有 idle -> 只能靠超时
        with pytest.raises(OpencodeError, match="仍未回复"):
            await run(fake, timeout=0.2)
        assert fake.polls >= 2, f"应该轮询多次，实际 {fake.polls}"


# ---------------------------------------- 第五轮 P0：空 parts 窗口 -------


class TestEmptyPartsWindow:
    """**第五轮 review 的 B-1（P0）**：空 ``parts`` 窗口导致答案 100% 丢失。

    第四轮把「首次见到」等同于「已完整处理」，但 opencode 的 assistant 消息是
    **先以空 parts 入库、再原地增长**的：

    - ``agent.go:326``：``messages.Create(..., Parts: []message.ContentPart{})``
    - 每个流式 delta 都 ``messages.Update(...)``，而 ``UpdateMessage`` 是
      ``UPDATE messages SET parts = ? WHERE id = ?`` —— **同一 id 原地覆盖**

    症状：第一次轮询看到 ``content=[] tokens={}`` -> 被标记成已处理 ->
    之后内容增长也不再采 -> **抛「没有回复内容」**，而真值是模型已经答了。

    按默认 ``poll_interval=0.35s``、实测最小问题 22~25 秒、首token 之前的
    推理时间通常远超 350ms —— 这个空窗**是常态而非边缘情况**。
    """

    @pytest.mark.asyncio
    async def test_answer_survives_the_empty_window(self) -> None:
        fake = FakeOpencode([
            [assistant(mid="a1")],                             # 空窗
            [assistant(mid="a1")],                             # 还是空窗
            [assistant(mid="a1", text="这是最终答案：42",
                       tokens={"input": 150, "output": 30}), idle()],
        ])
        reply = await run(fake)
        assert reply.text == "这是最终答案：42", (
            f"答案在空 parts 窗口里丢了：{reply.text!r}"
        )
        assert reply.usage == {"prompt_tokens": 150, "completion_tokens": 30}, (
            f"tokens 也一起丢了：{reply.usage}"
        )

    @pytest.mark.asyncio
    async def test_long_empty_window(self) -> None:
        """空窗很久（模型思考 5 秒，按 0.35s 间隔 ≈ 14 轮空）。"""
        frames = [[assistant(mid="a1")] for _ in range(14)]
        frames.append([
            assistant(mid="a1", text="答案",
                      tokens={"input": 9, "output": 1}),
            idle(),
        ])
        reply = await run(FakeOpencode(frames))
        assert reply.text == "答案"

    @pytest.mark.asyncio
    async def test_empty_window_with_reasoning_then_text(self) -> None:
        fake = FakeOpencode([
            [assistant(mid="a1")],
            [assistant(mid="a1")],
            [assistant(mid="a1")],
            [assistant(mid="a1", text="正文", reasoning="思考过程",
                       tokens={"input": 150, "output": 30}), idle()],
        ])
        reply = await run(fake)
        assert reply.text == "正文"
        assert reply.reasoning == "思考过程"

    @pytest.mark.asyncio
    async def test_growing_content_is_not_double_counted(self) -> None:
        """内容增长**不会被重复累加**。

        这是 B-3-1 与 B-1 的一对：前者要求「同一条消息只算一次」，
        后者要求「空窗之后还能再采」。两个必须同时成立。
        """
        fake = FakeOpencode([
            [assistant(mid="a1")],
            [assistant(mid="a1")],
            [assistant(mid="a1")],
            [assistant(mid="a1", text="答案",
                       tokens={"input": 9, "output": 1}), idle()],
        ])
        reply = await run(fake)
        assert reply.text.count("答案") == 1, (
            f"内容被重复累加 {reply.text.count('答案')} 次：{reply.text!r}"
        )


# ------------------------------------------ 第四轮 P0：历史重复累加---------


class TestHistoryIsNotReAccumulated:
    """**第四轮 review 的 B-3-1（P0）**：轮询历史被重复累加。

    第三轮把 ``text or best.text`` 改成拼接，方向对，但建立在
    **「每次轮询只返回新增消息」** 这个错误前提上。真实是**全部历史**。

    实测：3 次轮询 -> 「我先读一下配置。」出现 **3 次**；按生产参数
    （``poll_interval=0.35s``、实测最小问题 22~25 秒 ≈ 60~70 次轮询）
    会到**几十次** —— 而 HTTP 200、没有任何报错。
    """

    @pytest.mark.asyncio
    async def test_same_message_counted_once_across_polls(self) -> None:
        ready = assistant(mid="a1", text="答案", tokens={"input": 9, "output": 1})
        fake = FakeOpencode([[ready], [ready], [ready, idle()]])
        reply = await run(fake)
        assert reply.text == "答案", f"被重复累加：{reply.text!r}"
        assert reply.text.count("答案") == 1

    @pytest.mark.asyncio
    async def test_tool_round_then_final_answer(self) -> None:
        """实测形状：工具轮 ``tokens={}``，最终轮有完整计数。"""
        fake = FakeOpencode([[
            assistant(mid="a1", text="我先读一下配置。",
                      reasoning="我应该先读文件", tool=True),
            assistant(mid="a2", text="最终答案：42",
                      tokens={"input": 250, "output": 30}),
            idle(),
        ]])
        reply = await run(fake)
        assert "我先读一下配置。" in reply.text
        assert "最终答案：42" in reply.text
        assert "我应该先读文件" in reply.reasoning

    @pytest.mark.asyncio
    async def test_id_less_messages_fall_back_to_fingerprint(self) -> None:
        """没有 id 时退回内容指纹去重。

        宁可「内容相同就当重复」（模型真连续说两遍一模一样的话会被合并），
        也不要退化成重复累加 —— 后者就是这个 P0 的本质。
        """
        m = no_id_message("无 id 的消息")
        fake = FakeOpencode([[m], [m], [m, idle()]])
        reply = await run(fake)
        assert reply.text == "无 id 的消息", f"无 id 时重复累加了：{reply.text!r}"

    @pytest.mark.asyncio
    async def test_same_content_different_ids_are_kept(self) -> None:
        """**内容相同但 id 不同 = 两条真实消息**，都要保留。

        这是指纹兜底的代价：指纹只用于「没有 id」的情况。
        """
        fake = FakeOpencode([[
            assistant(mid="a1", text="好", tokens={"input": 1, "output": 1}),
            assistant(mid="a2", text="好", tokens={"input": 2, "output": 1}),
            idle(),
        ]])
        reply = await run(fake)
        assert reply.text == "好好", f"同 id 去重误伤了真实重复：{reply.text!r}"


# ------------------------------------------------- 第三轮 P0：多轮截断------


class TestMultiTurnMerge:
    @pytest.mark.asyncio
    async def test_text_and_reasoning_are_concatenated(self) -> None:
        fake = FakeOpencode([[
            assistant(mid="a1", text="我先读一下配置。",
                      reasoning="我应该先读文件", tool=True,
                      tokens={"input": 150, "output": 12, "reasoning": 8}),
            assistant(mid="a2", text="最终答案：42",
                      tokens={"input": 250, "output": 30}),
            idle(),
        ]])
        reply = await run(fake)
        assert "我先读一下配置。" in reply.text
        assert "最终答案：42" in reply.text
        assert "我应该先读文件" in reply.reasoning

    @pytest.mark.asyncio
    async def test_empty_tokens_does_not_erase_statistics(self) -> None:
        """工具轮那条 ``tokens`` 是 ``{}``，不能把已有统计清空。

        实测（2026-10-05，真实 opencode 跑「读 /etc/hosts」）：
        工具轮 assistant 的 ``tokens`` 就是空对象。
        """
        tool_round = assistant(mid="a1", text="我先读一下配置。", tool=True,
                              tokens={"input": 150, "output": 12})
        final = assistant(mid="a2", text="最终答案", tokens={})
        fake = FakeOpencode([
            [tool_round],
            [tool_round, final],
            [tool_round, final, idle()],
        ])
        reply = await run(fake)
        assert reply.usage.get("prompt_tokens") == 150, (
            f"统计被空 tokens 清空了：{reply.usage}"
        )
        assert reply.usage.get("completion_tokens") == 12

    @pytest.mark.asyncio
    async def test_usage_takes_max_not_sum(self) -> None:
        """``input`` 是**累计值**（每轮含之前所有内容），相加会重复计。

        取 max 在「累计」假设下正确；万一 opencode 改成报增量，max 会偏小
        （少算）而不是偏大（多算）。
        """
        r1 = assistant(mid="a1", text="一", tokens={"input": 10, "output": 1})
        r2 = assistant(mid="a2", text="二", tokens={"input": 20, "output": 2})
        r3 = assistant(mid="a3", text="三", tokens={"input": 35, "output": 3})
        fake = FakeOpencode([
            [r1],
            [r1, r2],
            [r1, r2, r3, idle()],
        ])
        reply = await run(fake)
        assert reply.usage["prompt_tokens"] == 35, "应取累计最大值，不是相加"
        assert reply.usage["completion_tokens"] == 3

    @pytest.mark.asyncio
    async def test_three_rounds(self) -> None:
        fake = FakeOpencode([[
            assistant(mid="a1", text="一", tokens={"input": 10, "output": 1}),
            assistant(mid="a2", text="二", tokens={"input": 20, "output": 2}),
            assistant(mid="a3", text="三", tokens={"input": 35, "output": 3}),
            idle(),
        ]])
        reply = await run(fake)
        assert reply.text == "一二三"

    @pytest.mark.asyncio
    async def test_single_round_still_works(self) -> None:
        """回归：最常见的单轮路径不受影响。"""
        reply = await run(simple("答案"))
        assert reply.text == "答案"
        assert reply.usage == {"prompt_tokens": 3, "completion_tokens": 4}

    @pytest.mark.asyncio
    async def test_tool_only_round_keeps_its_text(self) -> None:
        """实测形状：``content=[reasoning, text, tool]``。"""
        fake = FakeOpencode([[
            assistant(mid="a1", text="我先读一下配置。",
                      reasoning="先读文件", tool=True),
            assistant(mid="a2", text="文件里有 localhost",
                      tokens={"input": 200, "output": 20}),
            idle(),
        ]])
        reply = await run(fake)
        assert "先读文件" in reply.reasoning
        assert "我先读一下配置。" in reply.text
        assert "文件里有 localhost" in reply.text
        assert reply.usage["prompt_tokens"] == 200


# ------------------------------------------------------ 只思考没正文---------


class TestReasoningOnlyRound:
    """**第四轮 review 的 B-3-4**：全轮次只有 reasoning 时被误报为失败。

    模型可能整轮只在思考而没出正文。初版只检查 ``if best.text``，
    于是抛「没有回复内容」—— 而真因是「只思考没答」，思考内容**已经被采到了**，
    丢掉它等于白花上游额度。
    """

    @pytest.mark.asyncio
    async def test_reasoning_only_is_not_a_failure(self) -> None:
        fake = FakeOpencode([[
            assistant(mid="a1", reasoning="让我想想...",
                      tokens={"input": 10, "reasoning": 5}),
            idle(),
        ]])
        reply = await run(fake)
        assert reply.reasoning == "让我想想..."
        assert reply.text == ""

    @pytest.mark.asyncio
    async def test_reasoning_only_then_text(self) -> None:
        """先只思考、后出正文 —— 两者都要保留。"""
        fake = FakeOpencode([[
            assistant(mid="a1", reasoning="先想",
                      tokens={"input": 10, "reasoning": 5}),
            assistant(mid="a2", text="后答", tokens={"input": 20, "output": 3}),
            idle(),
        ]])
        reply = await run(fake)
        assert reply.reasoning == "先想"
        assert reply.text == "后答"

    @pytest.mark.asyncio
    async def test_truly_empty_still_raises(self) -> None:
        """回归：真的什么都没有仍然要报错（守住 ``or best.reasoning`` 的另一半）。"""
        with pytest.raises(OpencodeError, match="没有回复内容"):
            await run(FakeOpencode([[idle()]]))


class TestIdleOrderIsNotTrusted:
    """**第六轮发现的第六个问题**：真实环境下 **100% 触发**。

    实测（2026-10-05，真实 opencode 服务跑「7*8 是几」）返回的消息顺序是::

        [user, **idle**, assistant[reasoning+text], user]

    ——``idle`` 排在 assistant **之前**。而初版逐条遍历时一遇到 idle 就检查
    ``best``，此时 assistant还没被处理 -> 误判「没有回复内容」。

    那个 case 在真实环境下**每次都触发**（实测表现为 180 秒超时）。
    纯替身测试全绿 —— 因为它们都把 ``idle`` 放在最后（那是「看起来对」的顺序）。

    修法：**先扫完整轮再判断是否结束**（error/aborted 仍立刻抛）。
    """

    @pytest.mark.asyncio
    async def test_idle_before_assistant(self) -> None:
        """实测形状：``[user, idle, assistant]``。"""
        fake = FakeOpencode([
            [assistant(mid="a1")],# 空窗
            [
                {"id": "u1", "type": "user", "payload": {"text": "q"}},
                idle("i1"),
                assistant(mid="a1", text="56", reasoning="7*8=56",
                          tokens={"input": 12760, "output": 347}),
                {"id": "u2", "type": "user", "payload": {"text": "…"}},
            ],
        ])
        reply = await run(fake)
        assert reply.text == "56", (
            f"idle 在 assistant 之前时答案丢了：{reply.text!r}"
        )
        assert reply.reasoning == "7*8=56"
        assert reply.usage["prompt_tokens"] == 12760

    @pytest.mark.asyncio
    async def test_idle_in_the_middle(self) -> None:
        """idle 之后还有别的消息 —— 也不能被idle 提前打断。"""
        fake = FakeOpencode([[
            {"id": "u1", "type": "user", "payload": {"text": "q"}},
            idle("i1"),
            assistant(mid="a1", text="第一段"),
            assistant(mid="a2", text="第二段"),
        ]])
        reply = await run(fake)
        assert "第一段" in reply.text
        assert "第二段" in reply.text

    @pytest.mark.asyncio
    async def test_idle_last_still_works(self) -> None:
        """回归：``idle`` 在最后（替身里最常见的顺序）仍然正常。"""
        reply = await run(simple("答案"))
        assert reply.text == "答案"

    @pytest.mark.asyncio
    async def test_error_still_raises_immediately(self) -> None:
        """顺序不保证，但 **error 必须立刻抛** —— 不能因为它在列表中间就跳过。

        这是「先扫完再判断」的例外：``error``/``aborted`` 是真正的失败，
        等扫完等于让一个已经失败的请求继续等下一轮。
        """
        partial = assistant(mid="a1", text="半截",
                           tokens={"input": 5, "output": 1})
        fake = FakeOpencode([
            [partial, failed()],
        ])
        with pytest.raises(OpencodeError, match="上游 500"):
            await run(fake)


class TestModelIsSetWhenCreatingSession:
    """模型**建会话时**指定，且指定**生效**。

    ## 这里曾经有一个把结论带反的 bug

    初版把模型放在 ``POST /prompt`` 的 body 里（``{"text":…, "modelID":…}``），
    看到返回 ``200 {"delivery":"steer"}`` 就以为参数被接受了 —— 并据此在
    README、代码注释与 ``task.md`` 里写下「opencode 会忽略 modelID、
    一律走默认模型」，进而判定这条路是死路。

    **那是个观测错误**：``delivery: steer`` 与「模型对不对」毫无关系。
    真实形状是``POST /api/session`` 带
    ``{"model": {"id":…, "providerID": "opencode", "modelID": …}}``。

    这组测试守两件事：
    1. 模型确实放在**建会话**那一步（形状精确到字段名）
    2. ``/prompt`` **不带**任何 model 参数（带了会被静默忽略）
    """

    @pytest.mark.asyncio
    async def test_model_goes_into_the_session_call(self) -> None:
        fake = simple("答案")
        await run(fake)
        create = [c for c in fake.calls if c[0] == "POST"
                  and c[1] == "api/session"]
        assert create, f"没有建会话调用：{fake.calls}"
        body = create[0][2]
        assert isinstance(body, dict), f"建会话的 body不是对象：{body!r}"
        model = body.get("model")
        assert isinstance(model, dict), (
            f"建会话时没有传model 对象：{body!r}—— 模型必须在这一步指定"
        )
        assert model.get("id") == "big-pickle", (
            f"model.id 缺失或不对：{model!r}"
            "（只给 providerID/modelID 会被 opencode 拒成"
            " 400 Missing key at [\"model\"][\"id\"]）"
        )
        assert model.get("modelID") == "big-pickle"
        assert model.get("providerID") == "opencode"

    @pytest.mark.asyncio
    async def test_prompt_carries_no_model_parameter(self) -> None:
        """``/prompt`` 里的 model 参数会被**静默忽略**（返回 200 但没用）。

        所以不能靠「顺手也传一份」来「保险」—— 那样会让真模型被悄悄换掉，
        而响应仍然是 200。
        """
        fake = simple("答案")
        await run(fake)
        posts = [c for c in fake.calls if c[1].endswith("/prompt")]
        assert posts, f"没有投递调用：{fake.calls}"
        body = posts[0][2]
        assert isinstance(body, dict)
        for key in ("model", "modelID", "providerID"):
            assert key not in body, (
                f"/prompt 不该带 {key}（会被静默忽略）：{body!r}"
            )
        assert body.get("text") == "问题"

    @pytest.mark.asyncio
    async def test_reply_model_is_reported_truthfully(self) -> None:
        """返回值里的 ``model`` 是**实际服务的**，不是请求的。

        实测 opencode 可能在重试时回退到别的模型；回显请求值会掩盖
        「实际走了另一个模型」这个事实，而落库也跟着错。
        """
        fake = FakeOpencode([[
            assistant(mid="a1", text="答案", model="另一个模型",
                      tokens={"input": 5, "output": 1}),
            idle(),
        ]])
        reply = await run(fake, model="big-pickle")
        assert reply.model == "另一个模型", (
            f"回显了请求的模型而不是实际的：{reply.model!r}"
        )


class TestAssistantFailureFields:
    """assistant 消息自带 ``finish="error"`` / ``retry`` 两个失败字段。

    实测 2026-10-05：多个模型调不通时，消息的 ``type`` 仍是 ``assistant``、
    ``content`` 是 ``[]``，真正的失败写在这两个字段里。不读它们就会把
    **上游端点故障**误报成「本轮结束但没有回复内容」—— 而真因只有那里有。
    """

    @pytest.mark.asyncio
    async def test_finish_error_is_surfaced(self) -> None:
        fake = FakeOpencode([[
            {"id": "a1", "type": "assistant",
             "model": {"id": "ling-3.0-flash-fin-free"},
             "content": [], "finish": "error",
             "error": {"type": "provider.invalid-request",
                       "message": "Upstream request failed: "
                                  "Endpoint is unavailable"}},
            idle(),
        ]])
        with pytest.raises(OpencodeError) as exc:
            await run(fake, model="ling-3.0-flash-fin-free")
        msg = str(exc.value)
        assert "ling-3.0-flash-fin-free" in msg, "错误信息要带模型名"
        assert "Endpoint is unavailable" in msg, (
            f"错误信息里没有上游原因：{msg}"
        )

    @pytest.mark.asyncio
    async def test_retry_error_is_surfaced(self) -> None:
        fake = FakeOpencode([[
            {"id": "a1", "type": "assistant",
             "model": {"id": "nemotron-3-ultra-free"},
             "content": [], "completed": False,
             "retry": {"attempt": 2,
                       "error": {"type": "provider.rate-limit",
                                 "message": "Streaming response failed: [503]"}}},
        ]])
        with pytest.raises(OpencodeError) as exc:
            await run(fake, model="nemotron-3-ultra-free")
        msg = str(exc.value)
        assert "重试" in msg and "503" in msg, f"重试信息没转达：{msg}"

    def test_pure_helper(self) -> None:
        from openproxy.service.opencode_client import _assistant_failure

        assert _assistant_failure({
            "finish": "error", "error": {"message": "boom"},
        }) == "boom"
        assert "第 3 次" in _assistant_failure({
            "retry": {"attempt": 3, "error": {"message": "rate-limit"}},
        })
        # 正常消息（有 content、无 error）不该被误判
        assert _assistant_failure({
            "content": [{"type": "text", "text": "ok"}],
        }) == ""
        # 有 content 但也 finish=error -> 失败优先（实测这种消息 content 为空，
        # 但不依赖那个前提）
        assert _assistant_failure({
            "content": [{"type": "text", "text": "partial"}],
            "finish": "error", "error": {"message": "boom"},
        }) == "boom"


class TestErrorKindClassification:
    """**每种失败都要归到正确的 ``error_kind``**（实测 2026-10-06）。

    归错类的代价很具体：控制台「渠道」页按 ``error_kind`` 分组，
    「上游不可达」会把方向带偏成查地址/端口/代理，而真因是
    「opencode 侧那个模型的上游端点挂了」，处置该是「换个模型」。

    实测过一次真实的归错：等满``opencode_timeout``（120s）后超时，
    因为没显式给 ``kind``，落成了默认的 ``unreachable`` ——
    耗时明明是 120410ms（正好等于超时预算），却显示「上游不可达」。
    """

    @pytest.mark.asyncio
    async def test_poll_timeout_is_upstream_timeout(self) -> None:
        """轮询到预算耗尽 -> ``upstream_timeout``，不是 unreachable。"""
        ready = assistant(mid="a1", text="答案", tokens={"input": 9, "output": 1})
        fake = FakeOpencode([[ready]])  # 永远没有 idle -> 只能靠超时
        with pytest.raises(OpencodeError) as exc:
            await run(fake, timeout=0.2)
        assert exc.value.kind is ErrorKind.UPSTREAM_TIMEOUT, (
            f"超时应归upstream_timeout，实际是 {exc.value.kind}"
        )

    @pytest.mark.asyncio
    async def test_idle_without_content_is_upstream_status(self) -> None:
        """``idle`` 但无assistant -> 上游模型挂了，不是「连不上」。"""
        with pytest.raises(OpencodeError) as exc:
            await run(FakeOpencode([[idle()]]))
        assert exc.value.kind is ErrorKind.UPSTREAM_STATUS, (
            f"实际是 {exc.value.kind}"
        )

    @pytest.mark.asyncio
    async def test_missing_session_id_is_upstream_status(self) -> None:
        """建会话响应里没有 ``data.id`` -> 上游返回了预期外的东西。"""

        class NoIdClient(FakeOpencode):
            async def request(
                self, method: str, url: str, **kw: Any
            ) -> httpx.Response:
                path = url.split("/", 3)[-1]
                self.calls.append((method, path, kw.get("json")))
                if path == "api/session":
                    return httpx.Response(200, json={"data": {}})
                return await super().request(method, url, **kw)

        fake = NoIdClient([[idle()]])
        with pytest.raises(OpencodeError) as exc:
            await run(fake)
        assert exc.value.kind is ErrorKind.UPSTREAM_STATUS, (
            f"实际是 {exc.value.kind}"
        )


class TestErrorAndSessionCleanup:
    @pytest.mark.asyncio
    async def test_error_after_content_is_not_swallowed(self) -> None:
        """有过正文之后再出现 error，必须报错而不是「返回已有内容」。"""
        partial = assistant(mid="a1", text="部分",
                           tokens={"input": 5, "output": 1})
        fake = FakeOpencode([[partial], [partial, failed()]])
        with pytest.raises(OpencodeError, match="上游 500"):
            await run(fake)

    @pytest.mark.asyncio
    async def test_aborted_is_immediate(self) -> None:
        with pytest.raises(OpencodeError, match="被用户打断"):
            await run(FakeOpencode([[failed("aborted", "被用户打断")]]))

    @pytest.mark.asyncio
    async def test_session_is_always_recycled(self) -> None:
        """会话必须回收 —— 否则 opencode 里会堆一堆空会话。

        覆盖三条路径：正常成功、``idle`` 但无内容（抛错）、``error``（抛错）。
        """
        for fake in (simple("答案"),
                     FakeOpencode([[idle()]]),
                     FakeOpencode([[failed()]])):
            with contextlib.suppress(OpencodeError):
                await run(fake)
            assert fake.deleted == [fake.session_id], (
                f"会话没被回收：{fake.deleted}"
            )


# ------------------------------------------------------- _merge_usage -------


class TestMergeUsagePure:
    def test_empty_new_keeps_previous(self) -> None:
        assert _merge_usage({"prompt_tokens": 5}, {}) == {"prompt_tokens": 5}

    def test_empty_prev_takes_new(self) -> None:
        assert _merge_usage({}, {"prompt_tokens": 5}) == {"prompt_tokens": 5}

    def test_both_empty(self) -> None:
        assert _merge_usage({}, {}) == {}

    def test_max_per_key(self) -> None:
        assert _merge_usage(
            {"prompt_tokens": 10, "output_tokens": 5},
            {"prompt_tokens": 20, "cached_tokens": 3},
        ) == {"prompt_tokens": 20, "output_tokens": 5, "cached_tokens": 3}

    def test_union_of_keys(self) -> None:
        """两侧独有的键都要保留 —— 某轮报 cached 而下一轮没报时不能丢。"""
        out = _merge_usage({"cached_tokens": 7}, {"reasoning_tokens": 2})
        assert out == {"cached_tokens": 7, "reasoning_tokens": 2}

    def test_is_idempotent(self) -> None:
        once = _merge_usage({"a": 1}, {"a": 3})
        assert _merge_usage(once, {}) == once

    def test_cached_never_exceeds_prompt(self) -> None:
        """max逐项独立取，天然保持「cached <= prompt」的子集关系。"""
        out = _merge_usage(
            {"prompt_tokens": 100, "cached_tokens": 90},
            {"prompt_tokens": 20, "cached_tokens": 15},
        )
        assert out["cached_tokens"] <= out["prompt_tokens"]
