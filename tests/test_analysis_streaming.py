"""JYI 流式分析的请求契约：默认模型直答，续写与降级沿用同一套参数。

这组用例出身于"隐藏推理把输出预算吃光、正文一个字都没出来"那次故障。
现在的设计是默认文本模型**关掉**隐藏推理（先出正文，不让商户干等），
但契约没变：正文能流式拿到、推理内容不许混进结果、续写与非流式降级
必须带着同一套请求参数、图片拒绝要与本地解密失败区分开。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.client_login import GatewayError  # noqa: E402
from wechat_ai_assistant.jyi_client import JYIClient  # noqa: E402
from wechat_ai_assistant.summary_task import (  # noqa: E402
    _complete_jyi_chunk, _complete_summary_prompt, _jyi_model_limits,
    _run_summary_jyi,
)


def completion(text: str, *, finish: str = "stop", reasoning: str = "") -> bytes:
    events = []
    if reasoning:
        events.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
    events.extend([
        {"model": "deepseek-v4.1-flash", "choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": finish}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                   "completion_tokens_details": {"reasoning_tokens": 0}}},
    ])
    return ("".join("data: " + json.dumps(event) + "\n\n" for event in events)
            + "data: [DONE]\n\n").encode()


class Response(io.BytesIO):
    status = 200
    headers = {"x-jyi-request-id": "gwr_test_diagnostics"}


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra paid request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Response(item)

    def bodies(self):
        return [json.loads(request.data) for request in self.requests]


def rejecting(code: str = "unsupported_model_parameter", status: int = 400,
              message: str = "unsupported parameter: thinking"):
    """网关对"这个模型不吃这个参数"回的 400。"""
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    import urllib.error

    return urllib.error.HTTPError("https://example.invalid", status, "err", {},
                                 io.BytesIO(body))


class AnalysisStreamingTests(unittest.TestCase):
    def setUp(self):
        # 模块级"这个模型不吃 thinking"要清掉，否则用例之间互相影响。
        from wechat_ai_assistant import summary_task

        summary_task._THINKING_UNSUPPORTED.clear()

    def tearDown(self):
        from wechat_ai_assistant import summary_task

        summary_task._THINKING_UNSUPPORTED.clear()

    def test_a_model_that_rejects_thinking_is_retried_without_it(self):
        """模型不接受 thinking 参数时：退一步重试，并且记住别再发。

        反转记录：给 ``jyi-default`` 打开推理开关后，网关回 400
        ``unsupported_model_parameter``，整个分析失败，商户看到的是
        「所选分析模型暂不可用」—— 明明只是"这个模型不吃这个参数"。
        400 不产生费用，所以这次重试不额外花钱。
        """
        opener = Opener(rejecting(), completion("分析正文"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "苹果两箱"}],
                on_reasoning=lambda _text: None)
        self.assertEqual(report, "分析正文")
        self.assertEqual(opener.bodies()[0]["thinking"], {"type": "enabled"})
        self.assertNotIn("thinking", opener.bodies()[1], "降级后不许再发这个参数")

    def test_a_rejected_model_is_remembered_for_the_next_call(self):
        """记住之后，下一批连试都不试 —— 否则每批都白花一次 400 往返。"""
        from wechat_ai_assistant import summary_task

        opener = Opener(rejecting(), completion("第一批"), completion("第二批"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "苹果两箱"}],
                on_reasoning=lambda _text: None)
            second = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "梨一箱"}],
                on_reasoning=lambda _text: None)
        self.assertEqual(second, "第二批")
        self.assertIn("jyi-default", summary_task._THINKING_UNSUPPORTED)
        self.assertNotIn("thinking", opener.bodies()[2],
                         "记住之后连试都不试，直接不带这个参数")

    def test_reasoning_text_reaches_the_interface_separately(self):
        """推理过程要给界面看，但**不能**混进报告正文。

        模型常常先想几十秒（实测最长 48 秒），这段时间界面上能显示的只有推理。
        以前只把推理的**字数**交出去，页面就只能写一句「正在推理」—— 商户看不到
        任何在动的东西。所以这里钉两件事：推理正文确实到了 `on_reasoning`，
        而且正文流里一个字都没有它。
        """
        opener = Opener(completion("分析正文", reasoning="先看这两句是不是订货"))
        reasoning, chunks = [], []
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "苹果两箱"}],
                on_stream=chunks.append, on_reasoning=reasoning.append)
        self.assertEqual(report, "分析正文")
        self.assertEqual("".join(reasoning), "先看这两句是不是订货")
        self.assertEqual("".join(chunks), "分析正文", "推理不许混进正文")
        # 有人在看推理才打开它：没人看仍然直答，别为看不见的 token 付钱。
        self.assertEqual(opener.bodies()[0]["thinking"], {"type": "enabled"})

    def test_final_content_is_streamed_without_exposing_reasoning(self):
        opener = Opener(completion("订单分析正文", reasoning="内部推理"))
        chunks = []
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "苹果两箱"}],
                on_stream=chunks.append)
        self.assertEqual(report, "订单分析正文")
        self.assertEqual("".join(chunks), report)
        body = opener.bodies()[0]
        # 默认文本模型直答：显式关掉 thinking，也不带 reasoning_effort。
        # 见 _complete_summary_prompt 里 DEFAULT_TEXT_MODEL 那个分支。
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["max_tokens"], 131072)

    def test_images_use_the_same_endpoint_model_and_request_settings(self):
        from PIL import Image

        opener = Opener(completion("文字分析"), completion("图片分析"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "order.png"
            Image.new("RGB", (100, 100), "white").save(path)
            with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
                client = JYIClient()
                for message, images in [
                    ({"kind": "text", "content": "苹果两箱"}, False),
                    ({"kind": "image", "content": "图片", "attachments": [str(path)]}, True),
                ]:
                    _complete_jyi_chunk(client, "jyi-default", "order", [message],
                                        include_images=images)
        self.assertEqual({r.full_url for r in opener.requests},
                         {"https://gateway.jyisolutions.com/v1/chat/completions"})
        for body in opener.bodies():
            self.assertEqual(body["model"], "jyi-default")
            self.assertEqual(body["thinking"], {"type": "disabled"})
        parts = opener.bodies()[1]["messages"][-1]["content"]
        image = next(part for part in parts if part["type"] == "image_url")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/webp;base64,"))

    def test_continuations_keep_the_same_request_settings(self):
        opener = Opener(completion("第一部分", finish="length"), completion("第二部分"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "待分析消息"}])
        self.assertIn("第一部分", report)
        self.assertIn("第二部分", report)
        self.assertEqual(len(opener.requests), 2)
        for body in opener.bodies():
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", body)

    def test_non_streaming_fallback_keeps_the_same_request_settings(self):
        body = {"choices": [{"message": {"content": "完整正文"}, "finish_reason": "stop"}]}
        opener = Opener(completion(""), json.dumps(body).encode())
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            report = _complete_summary_prompt(
                JYIClient(), "jyi-default", [{"role": "user", "content": "待分析消息"}])
        self.assertEqual(report, "完整正文")
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(opener.bodies()[0]["stream"])
        self.assertNotIn("stream", opener.bodies()[1])
        self.assertEqual(opener.bodies()[1]["thinking"], {"type": "disabled"})

    def test_reasoning_only_length_failure_does_not_retry_or_split(self):
        opener = Opener(completion("", finish="length", reasoning="持续推理"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "推理过程"):
                _complete_jyi_chunk(JYIClient(), "jyi-default", "order", [
                    {"kind": "text", "content": "苹果两箱"},
                    {"kind": "text", "content": "梨一箱"},
                ])
        self.assertEqual(len(opener.requests), 1)

    def test_other_models_do_not_receive_deepseek_specific_settings(self):
        opener = Opener(completion("分析正文"))
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            _complete_summary_prompt(JYIClient(), "another-model", [
                {"role": "user", "content": "待分析消息"}])
        self.assertNotIn("thinking", opener.bodies()[0])
        self.assertNotIn("reasoning_effort", opener.bodies()[0])

    def test_sse_error_is_reported_without_a_second_request(self):
        error = {"error": {"code": "upstream_error", "message": "Provider unavailable"}}
        opener = Opener(("data: " + json.dumps(error) + "\n\ndata: [DONE]\n\n").encode())
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            with self.assertRaises(GatewayError) as raised:
                _complete_summary_prompt(JYIClient(), "jyi-default", [
                    {"role": "user", "content": "待分析消息"}])
        self.assertEqual(raised.exception.code, "upstream_error")
        self.assertEqual(len(opener.requests), 1)

    def test_diagnostics_record_actual_request_budget_and_reasoning_usage(self):
        opener = Opener(completion("正文"))
        stats = []
        with patch("wechat_ai_assistant.client_login.build_opener", return_value=opener):
            JYIClient().chat_completions_stream(
                "jyi-default", [{"role": "user", "content": "待分析消息"}],
                max_tokens=384000, thinking={"type": "enabled"}, reasoning_effort="max",
                on_diagnostics=stats.append)
        self.assertEqual(stats[0]["http_status"], 200)
        self.assertEqual(stats[0]["request_max_tokens"], 384000)
        self.assertEqual(stats[0]["request_thinking"], "enabled")
        self.assertEqual(stats[0]["usage_reasoning_tokens"], 0)
        self.assertEqual(stats[0]["response_model"], "deepseek-v4.1-flash")
        self.assertEqual(stats[0]["gateway_request_id"], "gwr_test_diagnostics")

    def test_model_capacity_clips_the_output_and_input_budgets(self):
        from wechat_ai_assistant.jyi_parse import SUMMARY_MAX_OUTPUT_TOKENS
        from wechat_ai_assistant.summary_task import SUMMARY_CHUNK_TARGET_TOKENS

        app = types.SimpleNamespace(model_catalog={"models": [{
            "alias": "jyi-default", "context_window": 1_000_000,
            "max_output_tokens": 384000,
        }]})
        output, input_budget = _jyi_model_limits(app, "jyi-default")
        # 上游允许的输出上限再大也不越过本地常量；输入预算封顶在分批目标值。
        self.assertEqual(output, SUMMARY_MAX_OUTPUT_TOKENS)
        self.assertEqual(input_budget, SUMMARY_CHUNK_TARGET_TOKENS)
        self.assertLess(output + input_budget, 1_000_000, "两者相加不能超过上下文窗口")

        # 上下文小的模型：输出与输入都必须被余量裁掉，不能只信上游声明。
        small = types.SimpleNamespace(model_catalog={"models": [{
            "alias": "jyi-default", "context_window": 65536,
            "max_output_tokens": 8192,
        }]})
        output, input_budget = _jyi_model_limits(small, "jyi-default")
        self.assertEqual(output, 8192, "上游的输出上限小于本地常量时听上游的")
        self.assertIsNotNone(input_budget)
        self.assertLessEqual(output + int(input_budget), 65536)

    def test_gateway_image_rejection_is_distinct_from_decryption_failure(self):
        from PIL import Image

        client = JYIClient()

        def reply(model, messages, **kwargs):
            if isinstance(messages[-1]["content"], list):
                raise GatewayError("unsupported_content", "Image inputs are not supported", 422)
            return {"choices": [{"message": {"content": "文字订单分析"}}]}

        client.chat_completions = reply
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "order.png"
            Image.new("RGB", (100, 100), "white").save(path)
            image = {"kind": "image", "content": "图片", "attachments": [str(path)]}
            with self.assertLogs("wechat_ai_assistant.summary", level="ERROR"):
                outcome = _run_summary_jyi(client, [
                    {"kind": "text", "content": "苹果两箱"}, image,
                ], role_id="order")
        self.assertIn("文字订单分析", outcome["summary"])
        self.assertEqual(outcome["message_count"], 1)
        self.assertIn("已解密", outcome["partial_error"])
        self.assertIn("网关", outcome["partial_error"])
        self.assertIn("已解密", image["media_status"])


if __name__ == "__main__":
    unittest.main()
