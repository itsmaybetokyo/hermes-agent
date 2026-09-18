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


if __name__ == "__main__":
    unittest.main()