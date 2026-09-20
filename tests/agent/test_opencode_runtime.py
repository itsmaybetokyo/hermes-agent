"""Unit tests for the opencode_cli runtime (agent/opencode_runtime.py).

These tests exercise the pure helpers and the NDJSON bridge with a fake subprocess;
they do NOT invoke the real `opencode` CLI (gated elsewhere as a live test).
"""

from __future__ import annotations

import builtins
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent import opencode_runtime as ort


def build_agent_stub(model: str = "big-pickle", session_cwd: str = None):
    """Minimal agent surface the codex_runtime bookkeeping touches (no session DB)."""
    agent = SimpleNamespace(
        model=model,
        provider="opencode-local",
        base_url="opencode://local",
        api_key="",
        session_cwd=session_cwd or os.getcwd(),
        session_id="test-session",
        _session_db=None,
        _session_db_created=False,
        session_api_calls=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status=None,
        session_cost_source=None,
        context_compressor=None,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _interrupt_requested=False,
        _interrupt_message=None,
        opencode_task_timeout=600,
        _opencode_cli_deltas=[],
    )
    agent._fire_stream_delta = lambda text: agent._opencode_cli_deltas.append(text)
    agent._ensure_db_session = lambda: None
    agent._sync_external_memory_for_turn = lambda **kwargs: None
    agent._spawn_background_review = lambda **kwargs: None
    return agent


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __iter__(self):
        for line in self._lines:
            yield line.encode("utf-8")

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self.stdin = io.BytesIO()
        self.stdout = _FakeStream(lines)
        self.stderr = io.BytesIO(stderr.encode("utf-8"))
        self.returncode = returncode
        self._wait = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: int = 0) -> int:
        return self.returncode

    def terminate(self) -> None:
        self._wait = True

    def kill(self) -> None:
        self._wait = True


class TestHelpers(unittest.TestCase):
    def test_model_cli_id_qualifies_bare_ids(self) -> None:
        self.assertEqual(ort._model_cli_id("big-pickle"), "opencode/big-pickle")
        self.assertEqual(ort._model_cli_id("opencode/big-pickle"), "opencode/big-pickle")
        self.assertEqual(ort._model_cli_id(""), "opencode/big-pickle")

    def test_flatten_content_str_and_parts(self) -> None:
        self.assertEqual(ort._flatten_content("hi"), "hi")
        self.assertEqual(
            ort._flatten_content([{"type": "text", "text": "a"}, {"type": "image_url", "url": "x"}]),
            "a",
        )
        self.assertEqual(ort._flatten_content(None), "")

    def test_transcript_to_prompt_builds_role_blocks(self) -> None:
        messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
        prompt = ort._transcript_to_prompt(messages)
        self.assertIn("[user]", prompt)
        self.assertIn("[assistant]", prompt)
        self.assertIn("one", prompt)

    def test_opencode_tokens_to_usage_maps_to_codex_shape(self) -> None:
        usage = ort._opencode_tokens_to_usage(
            {"total": 100, "input": 40, "output": 60, "cache": {"read": 30}}
        )
        self.assertEqual(
            usage,
            {"inputTokens": 40, "outputTokens": 60, "cachedInputTokens": 30, "totalTokens": 100},
        )
        self.assertIsNone(ort._opencode_tokens_to_usage(None))
        self.assertIsNone(ort._opencode_tokens_to_usage({}))

    def test_extract_error_nested_and_flat(self) -> None:
        err = {"type": "error", "part": {"type": "error", "text": "boom"}}
        self.assertEqual(ort._extract_error(err), "boom")
        err2 = {"type": "error", "part": {"type": "error", "error": {"message": "nested"}}}
        self.assertEqual(ort._extract_error(err2), "nested")
        self.assertIsNone(ort._extract_error({"type": "text"}))

    def test_extract_error_top_level_shape(self) -> None:
        err = {"type": "error", "timestamp": 1, "sessionID": "s",
               "error": {"name": "APIError",
                         "data": {"message": "Internal server error", "statusCode": 500}}}
        self.assertEqual(ort._extract_error(err), "APIError: Internal server error (status 500)")

    def test_tool_preview_prefers_title_then_known_keys(self) -> None:
        self.assertEqual(ort._opencode_tool_preview("glob", {"title": "  Files  "}), "Files")
        self.assertEqual(
            ort._opencode_tool_preview("read", {"input": {"filePath": "a/b.py"}}), "a/b.py")
        self.assertIsNone(ort._opencode_tool_preview("x", {}))

    def test_tool_result_error_status(self) -> None:
        text, is_error = ort._opencode_tool_result({"status": "completed", "output": "ok"})
        self.assertEqual((text, is_error), ("ok", False))
        text, is_error = ort._opencode_tool_result({"status": "failed", "output": "nope"})
        self.assertEqual((text, is_error), ("nope", True))


def _patched_cost():
    """Hermetic cost accounting: opencode-local has no pricing entry, so force the
    generic result instead of hitting the network during tests."""
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    from decimal import Decimal
    from agent.usage_pricing import CostResult
    return patch("agent.usage_pricing.estimate_usage_cost",
                 return_value=CostResult(amount_usd=Decimal("0"), status="unknown", source="none",
                                         label="unknown", fetched_at=now))


class TestRunTurn(unittest.TestCase):
    def _fake_popen(self, lines: list[str], returncode: int = 0):
        proc = _FakeProc(lines, returncode=returncode)

        def _fake_popen_cmd(cmd, **kwargs):
            self.assertEqual(cmd[1], "run")
            self.assertIn("--format=json", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "opencode/big-pickle")
            return proc

        return _fake_popen_cmd, proc

    def test_happy_path_streams_and_persists(self) -> None:
        agent = build_agent_stub(model="big-pickle", session_cwd=str(Path(os.getcwd())))
        fake, _ = self._fake_popen([
                json.dumps({"type": "step_start"}),
                json.dumps({"type": "text", "part": {"type": "text", "text": "Hel"}}),
                json.dumps({"type": "text", "part": {"type": "text", "text": "lo"}}),
                json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {
                    "total": 12, "input": 5, "output": 7, "cache": {"read": 4}}}}),
                json.dumps({"type": "done"}),
            ])
        with patch.object(ort.subprocess, "Popen", side_effect=fake), _patched_cost():
            result = ort.run_opencode_cli_turn(
                agent,
                user_message="hi",
                original_user_message="hi",
                messages=[{"role": "user", "content": "hi"}],
                effective_task_id="t-1",
            )
        self.assertEqual(result["final_response"], "Hel\nlo")
        self.assertEqual(agent._opencode_cli_deltas, ["Hel", "lo"])
        self.assertTrue(result["completed"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["api_calls"], 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["messages"][-1]["role"], "assistant")
        # Streamed output is persisted; the gateway must skip its own DB write.
        self.assertTrue(result["agent_persisted"])
        self.assertEqual(agent.session_api_calls, 1)

    def test_error_event_surfaces(self) -> None:
        agent = build_agent_stub(model="big-pickle")
        fake, _ = self._fake_popen(
            [
                json.dumps({"type": "error", "part": {"type": "error", "text": "failed: boom"}}),
            ],
        )
        with patch.object(ort.subprocess, "Popen", side_effect=fake):
            result = ort.run_opencode_cli_turn(
                agent, user_message="x", original_user_message="x",
                messages=[{"role": "user", "content": "x"}], effective_task_id="t-1",
            )
        self.assertFalse(result["completed"])
        self.assertTrue(result["partial"])
        self.assertIn("boom", str(result["error"]))

    def test_nonzero_exit_surfaces_error(self) -> None:
        agent = build_agent_stub(model="big-pickle")
        fake, _ = self._fake_popen([], returncode=2)
        with patch.object(ort.subprocess, "Popen", side_effect=fake):
            result = ort.run_opencode_cli_turn(
                agent, user_message="x", original_user_message="x",
                messages=[{"role": "user", "content": "x"}], effective_task_id="t-1",
            )
        self.assertFalse(result["completed"])
        self.assertIn("exit code 2", str(result["error"]).replace("exited with code", "exit code"))

    def test_spawn_failure_returns_typed_result(self) -> None:
        agent = build_agent_stub(model="big-pickle")

        def _raise(cmd, **kwargs):
            raise FileNotFoundError("no such binary")

        with patch.object(ort.subprocess, "Popen", side_effect=_raise):
            result = ort.run_opencode_cli_turn(
                agent, user_message="x", original_user_message="x",
                messages=[{"role": "user", "content": "x"}], effective_task_id="t-1",
            )
        self.assertFalse(result["completed"])
        self.assertIn("failed to start", result["final_response"])
        self.assertIn("no such binary", str(result["error"]))

    def test_xdg_root_is_per_session(self) -> None:
        class A:
            session_id = "abc-123"

        class B:
            session_id = "xyz-999"

        self.assertNotEqual(ort._xdg_root(A()), ort._xdg_root(B()))

    def test_env_is_isolated_and_sanitized(self) -> None:
        import tempfile
        root = Path(tempfile.gettempdir()) / "ort-xdg-test-xyz"
        env = ort._opencode_env(root)
        self.assertEqual(env["XDG_DATA_HOME"], str(root / "data"))
        self.assertNotIn("OPENCODE", env)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", env)
        self.assertNotIn("OPENCODE_CONFIG_PATH", env)
        self.assertEqual(env.get("OPENCODE_DISABLE_AUTOUPDATER"), "1")


class TestEventBridge(unittest.TestCase):
    """tool_use/reasoning/top-level-error events reach the Hermes display channels."""

    def _run(self, agent, lines, returncode=0):
        proc = _FakeProc(lines, returncode=returncode)

        def _fake(cmd, **kwargs):
            return proc

        with patch.object(ort.subprocess, "Popen", side_effect=_fake), _patched_cost():
            return ort.run_opencode_cli_turn(
                agent, user_message="x", original_user_message="x",
                messages=[{"role": "user", "content": "x"}], effective_task_id="t-1",
            )

    def test_tool_use_fires_progress_callbacks_and_counts(self) -> None:
        agent = build_agent_stub(model="big-pickle")
        progress, started, completed = [], [], []
        agent.tool_progress_callback = lambda *a, **k: progress.append((a, k))
        agent.tool_start_callback = lambda *a: started.append(a)
        agent.tool_complete_callback = lambda *a: completed.append(a)
        result = self._run(agent, [
            json.dumps({"type": "tool_use", "part": {
                "type": "tool", "tool": "glob", "callID": "call_1",
                "state": {"status": "completed", "input": {"pattern": "AGENTS.md"},
                          "output": "agent/AGENTS.md",
                          "time": {"start": 1000, "end": 1500}}}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "DONE"}}),
            json.dumps({"type": "done"}),
        ])
        self.assertTrue(result["completed"])
        kinds = [call[0][0] for call in progress]
        self.assertEqual(kinds, ["tool.started", "tool.completed"])
        self.assertEqual(progress[0][0][1:], ("glob", "AGENTS.md", {"pattern": "AGENTS.md"}))
        self.assertFalse(progress[1][1]["is_error"])
        self.assertAlmostEqual(progress[1][1]["duration"], 0.5)
        self.assertEqual(progress[1][1]["result"], "agent/AGENTS.md")
        self.assertEqual(started, [("call_1", "glob", {"pattern": "AGENTS.md"})])
        self.assertEqual(completed, [("call_1", "glob", {"pattern": "AGENTS.md"}, "agent/AGENTS.md")])
        # Turn accounting sees the CLI-owned tool call (was hardcoded 0).
        self.assertEqual(agent._iters_since_skill, 1)

    def test_reasoning_streams_and_persists_without_polluting_answer(self) -> None:
        agent = build_agent_stub(model="big-pickle")
        deltas = []
        agent._fire_reasoning_delta = deltas.append
        result = self._run(agent, [
            json.dumps({"type": "reasoning", "part": {"type": "reasoning", "text": "Let me think"}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "DONE"}}),
            json.dumps({"type": "done"}),
        ])
        self.assertTrue(result["completed"])
        self.assertEqual(deltas, ["Let me think"])
        self.assertEqual(result["final_response"], "DONE")
        self.assertEqual(result["messages"][-1]["content"], "DONE")
        self.assertEqual(result["messages"][-1]["reasoning"], "Let me think")

    def test_top_level_error_shape_fails_the_turn(self) -> None:
        agent = build_agent_stub(model="big-pickle")
        result = self._run(agent, [
            json.dumps({"type": "error", "timestamp": 1, "sessionID": "s",
                        "error": {"name": "APIError",
                                  "data": {"message": "Internal server error",
                                           "statusCode": 500}}}),
        ])
        self.assertFalse(result["completed"])
        self.assertTrue(result["partial"])
        self.assertIn("Internal server error", str(result["error"]))


if __name__ == "__main__":
    unittest.main()