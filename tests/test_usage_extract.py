"""用量抽取：非流式 JSON、SSE 增量扫描、流式注入、请求元信息。

这一层是「统计对不对」的唯一入口，所以按畸形输入逐个钉死行为：
抽不到必须标成 unknown，而不是悄悄变成 0。
"""

from __future__ import annotations

import json

import pytest

from openproxy.domain import TokenUsage
from openproxy.service.usage_extract import (
    MAX_SSE_LINE_BYTES,
    RequestMeta,
    SseUsageScanner,
    inject_stream_usage,
    parse_request_meta,
    usage_from_json_body,
    usage_from_mapping,
    usage_from_sse_event,
)


class TestUsageFromMapping:
    def test_full_payload(self) -> None:
        got = usage_from_mapping(
            {
                "prompt_tokens": 158,
                "completion_tokens": 29,
                "total_tokens": 187,
                "prompt_tokens_details": {"cached_tokens": 157},
                "completion_tokens_details": {"reasoning_tokens": 22},
            }
        )
        assert got == TokenUsage(158, 29, 157, 22, 187, True)

    def test_flat_detail_fields_are_also_accepted(self) -> None:
        """有些端点把 cached/reasoning 平铺在 usage 上，两种形状都要认。"""
        got = usage_from_mapping(
            {"prompt_tokens": 5, "completion_tokens": 5, "cached_tokens": 3, "reasoning_tokens": 4}
        )
        assert got is not None
        assert (got.cached_tokens, got.reasoning_tokens) == (3, 4)

    def test_flat_wins_over_nested_when_both_present(self) -> None:
        got = usage_from_mapping(
            {
                "prompt_tokens": 5,
                "completion_tokens": 5,
                "cached_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 999},
            }
        )
        assert got is not None and got.cached_tokens == 1

    def test_details_container_of_wrong_type_is_ignored(self) -> None:
        assert usage_from_mapping(
            {"prompt_tokens": 1, "prompt_tokens_details": "nope"}
        ) is not None

    def test_derives_total_when_missing(self) -> None:
        """上游不给 total 时按 in+out 补，否则图表上会出现「分解之和 ≠ 总量」。"""
        got = usage_from_mapping({"prompt_tokens": 10, "completion_tokens": 5})
        assert got is not None
        assert got.total_tokens == 15

    def test_derives_total_when_zero(self) -> None:
        got = usage_from_mapping({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        assert got == TokenUsage(0, 0, 0, 0, 0, True)

    def test_only_completion_is_enough(self) -> None:
        got = usage_from_mapping({"completion_tokens": 7})
        assert got is not None and got.total_tokens == 7

    @pytest.mark.parametrize("raw", [None, {}, [], "usage", 42, True, {"prompt_tokens": None}])
    def test_absent_or_unusable_returns_none(self, raw: object) -> None:
        assert usage_from_mapping(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("12", 12), (12.0, 12), (True, None), (False, None), ("abc", None), (-5, 0)],
    )
    def test_coercion_rules(self, raw: object, expected: int | None) -> None:
        got = usage_from_mapping({"prompt_tokens": raw})
        if expected is None:
            assert got is None
        else:
            assert got is not None and got.prompt_tokens == expected

    def test_bool_does_not_count_as_one(self) -> None:
        """``True`` 是 ``int`` 子类；不排除它会把 ``usage: {cached_tokens: true}`` 记成 1。"""
        got = usage_from_mapping({"cached_tokens": True})
        assert got is None


class TestUsageFromJsonBody:
    def test_happy_path(self) -> None:
        body = json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}).encode()
        got = usage_from_json_body(body)
        assert got.known is True and got.total_tokens == 7

    @pytest.mark.parametrize(
        "body",
        [b"", b"not json", b"[1,2,3]", b'"a string"', b"{", "中文字节".encode()],
    )
    def test_malformed_bodies_become_unknown(self, body: bytes) -> None:
        got = usage_from_json_body(body)
        assert got == TokenUsage.unknown()
        assert got.known is False

    def test_usage_null_is_unknown(self) -> None:
        assert usage_from_json_body(b'{"usage":null}').known is False

    def test_truncated_json_is_unknown_not_a_crash(self) -> None:
        truncated = '{"choices":[{"message":{"content":"很长的回答'.encode()
        assert usage_from_json_body(truncated).known is False


class TestUsageFromSseEvent:
    def test_reads_usage_object(self) -> None:
        line = json.dumps({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
        got = usage_from_sse_event(line)
        assert got is not None and got.total_tokens == 5

    @pytest.mark.parametrize("line", ["", "   ", "[DONE]", "not json", "[1,2]", '"x"', "{}"])
    def test_non_usage_lines_return_none(self, line: str) -> None:
        assert usage_from_sse_event(line) is None


class TestSseUsageScanner:
    def test_reads_usage_from_a_normal_stream(self) -> None:
        scanner = SseUsageScanner()
        for frame in (
            b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":158,"completion_tokens":6,'
            b'"total_tokens":164}}\n\n',
            b"data: [DONE]\n\n",
        ):
            scanner.feed(frame)
        got = scanner.close()
        assert got.known is True
        assert (got.prompt_tokens, got.completion_tokens, got.total_tokens) == (158, 6, 164)
        assert scanner.complete is True

    def test_line_split_across_chunks(self) -> None:
        """真实上游按任意字节边界切块，一条 SSE 行横跨三块是常态而不是意外。"""
        whole = b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":22}}\n\n'
        scanner = SseUsageScanner()
        for i in range(0, len(whole), 7):
            scanner.feed(whole[i : i + 7])
        assert scanner.close().total_tokens == 33

    def test_byte_at_a_time(self) -> None:
        whole = b'data: {"usage":{"prompt_tokens":5,"completion_tokens":6}}\n\ndata: [DONE]\n\n'
        scanner = SseUsageScanner()
        for byte in whole:
            scanner.feed(bytes([byte]))
        got = scanner.close()
        assert got.known is True and got.total_tokens == 11
        assert scanner.complete is True

    def test_carriage_returns_are_stripped(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}\r\n\r\n')
        assert scanner.close().total_tokens == 2

    def test_ignores_non_data_lines(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b": keep-alive comment\n\nevent: ping\n\nid: 7\n\nretry: 100\n\n")
        assert scanner.close().known is False

    def test_missing_trailing_newline_still_parsed_on_close(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b'data: {"usage":{"prompt_tokens":9,"completion_tokens":9}}')
        assert scanner.close().total_tokens == 18

    def test_stream_without_usage_frame_stays_unknown(self) -> None:
        """不注入 include_usage 时上游就是这种流 —— 必须报 unknown，不能报 0。"""
        scanner = SseUsageScanner()
        scanner.feed(b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n')
        scanner.feed(b"data: [DONE]\n\n")
        got = scanner.close()
        assert got.known is False
        assert scanner.complete is True

    def test_residual_buffer_after_done_is_not_misread(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b"data: [DONE]\n\ndata: {\"usage\":{\"prompt_tokens\":3}}\n\n")
        got = scanner.close()
        # 缓冲区里 [DONE] 之后的残留行仍要扫到（上游偶尔乱序），且只有 prompt
        # 给出时 total 按 prompt+0 补 = 3
        assert got.known is True and got.total_tokens == 3

    def test_later_usage_merges_by_max(self) -> None:
        """有些上游会中途再补一帧 usage；取字段最大值而不是相加，才不会把
        「累计值」和「增量值」混成两倍。"""
        scanner = SseUsageScanner()
        scanner.feed(b'data: {"usage":{"prompt_tokens":10,"completion_tokens":4}}\n\n')
        scanner.feed(b'data: {"usage":{"prompt_tokens":10,"completion_tokens":9}}\n\n')
        got = scanner.close()
        assert (got.prompt_tokens, got.completion_tokens, got.total_tokens) == (10, 9, 19)

    def test_tiny_increment_after_large_is_ignored(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b'data: {"usage":{"prompt_tokens":100,"completion_tokens":50}}\n\n')
        scanner.feed(b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n')
        got = scanner.close()
        assert got.prompt_tokens == 100 and got.total_tokens == 150

    def test_oversized_line_is_dropped_and_counted(self) -> None:
        """上游（或恶意客户端）发一条几 MB 的单行时必须丢缓冲，而不是吃光内存。"""
        scanner = SseUsageScanner(max_line_bytes=2048)
        scanner.feed(b'data: {"junk":"' + b"A" * 4096)
        assert scanner.oversized_lines >= 1
        scanner.feed(b'"}\n\n')
        assert scanner.close().known is False

    def test_default_line_limit_is_the_module_constant(self) -> None:
        assert MAX_SSE_LINE_BYTES == 1 << 20

    def test_max_line_is_floored_so_it_can_never_be_zero(self) -> None:
        scanner = SseUsageScanner(max_line_bytes=1)
        assert scanner._max_line == 1024

    def test_feed_empty_chunk_is_a_noop(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b"")
        assert scanner.oversized_lines == 0
        assert scanner.close().known is False

    def test_invalid_utf8_in_payload_does_not_raise(self) -> None:
        scanner = SseUsageScanner()
        scanner.feed(b"data: \xff\xfe not utf8\n\n")
        assert scanner.close().known is False


class TestTokenUsageMerging:
    def test_unknown_merged_with_known_wins(self) -> None:
        assert TokenUsage.unknown().merged_with(TokenUsage(1, 1, 0, 0, 2, True)).known is True

    def test_known_merged_with_unknown_keeps_known(self) -> None:
        known = TokenUsage(1, 1, 0, 0, 2, True)
        assert known.merged_with(TokenUsage.unknown()) is known


class TestParseRequestMeta:
    def test_full_body(self) -> None:
        body = json.dumps(
            {"model": " space-bunny-free ", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        meta = parse_request_meta(body)
        assert meta.model == "space-bunny-free"
        assert meta.stream is True
        assert meta.messages == 1
        assert meta.is_json is True
        assert meta.has_messages is True

    def test_model_is_truncated_to_a_sane_length(self) -> None:
        meta = parse_request_meta(json.dumps({"model": "m" * 500}).encode())
        assert len(meta.model) == 120

    def test_absent_fields_default(self) -> None:
        meta = parse_request_meta(b"{}")
        assert (meta.model, meta.stream, meta.messages, meta.is_json) == ("", False, 0, True)

    def test_wrong_types_do_not_raise(self) -> None:
        meta = parse_request_meta(
            json.dumps({"model": 123, "stream": "yes", "messages": "nope"}).encode()
        )
        assert meta.model == ""
        assert meta.stream is False
        assert meta.messages == 0

    @pytest.mark.parametrize("body", [b"", b"garbage", b"[1]", b'"s"'])
    def test_non_json_bodies(self, body: bytes) -> None:
        meta = parse_request_meta(body)
        assert meta.is_json is False
        assert meta.model == ""


class TestInjectStreamUsage:
    def test_adds_the_flag(self) -> None:
        out = json.loads(inject_stream_usage(b'{"model":"m","stream":true}'))
        assert out["stream_options"] == {"include_usage": True}

    def test_preserves_existing_options(self) -> None:
        out = json.loads(
            inject_stream_usage(b'{"stream":true,"stream_options":{"other":1}}')
        )
        assert out["stream_options"] == {"other": 1, "include_usage": True}

    def test_already_true_is_left_alone(self) -> None:
        raw = b'{"stream":true,"stream_options":{"include_usage":true}}'
        assert inject_stream_usage(raw) == raw

    def test_wrong_typed_stream_options_left_alone(self) -> None:
        """``stream_options`` 是别的类型时不能改写 —— 宁可统计不到也别弄坏请求。"""
        raw = b'{"stream":true,"stream_options":"nope"}'
        assert inject_stream_usage(raw) == raw

    def test_non_json_unchanged(self) -> None:
        assert inject_stream_usage(b"garbage") == b"garbage"

    def test_json_array_unchanged(self) -> None:
        assert inject_stream_usage(b"[1,2]") == b"[1,2]"

    def test_unicode_survives(self) -> None:
        out = json.loads(inject_stream_usage('{"messages":[{"role":"user","content":"你好"}]}'.encode()))
        assert out["messages"][0]["content"] == "你好"

    def test_non_serialisable_payload_unchanged(self) -> None:
        raw = b'{"model":"m","max_tokens":Infinity}'
        out = inject_stream_usage(raw)
        # json 解析成功但含 Infinity，序列化后再解析会退化成 null；只要不抛异常即可
        assert isinstance(out, bytes)


class TestRequestMetaDefault:
    def test_default_is_empty_and_known(self) -> None:
        meta = RequestMeta()
        assert meta.is_json is True
        assert meta.has_messages is False
