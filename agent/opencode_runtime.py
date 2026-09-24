"""OpenCode CLI runtime (api_mode = ``opencode_cli``).

The ``opencode_cli`` API mode hands each Hermes turn to the LOCAL ``opencode`` CLI:
one ``opencode run --model <model> --format=json`` subprocess per turn (stateless,
no persistent session), the Hermes transcript passed via ``stdin``, and the CLI's
NDJSON events bridged into Hermes: ``type:text`` into stream deltas, ``type:tool_use``
into the tool-progress callbacks, ``type:reasoning`` into the reasoning channel plus
the persisted message, ``type:step_finish`` into token accounting, and ``type:error``
(in either the part-shaped or the top-level failure shape) into the turn error.
Big-pickle and friends talk OpenCode's *free tier*, so with this
mode big-pickle runs through opencode-ai itself (a real ``opencode run`` subprocess
instead of a forged HTTP bearer against the zen endpoint, which the free tier rejects).

Design notes
------------
- Stateless per turn: spawning a fresh ``opencode run`` per turn means the CLI's own
  tools/config never persist a session we might desync. Prompt caching still works
  because the transcript is passed whole every turn (same prefix → warm cache).
- Isolated XDG: each Hermes session gets its own ``XDG_*`` root under the OS temp dir,
  so the CLI never touches the user's real opencode DB/auth and a Hermes turn cannot
  corrupt another Hermes session's opencode data.
- The Hermes tool loop is bypassed (the CLI owns tools inside its run), exactly like
  ``codex_app_server``. Memory/review/skill bookkeeping is reused through
  ``_finish_codex_turn``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from agent.codex_runtime import (
    _call_guarded,
    _consume_user_interrupt,
    _finish_codex_turn,
    _persist_projected_messages,
    _turn_result,
)

logger = logging.getLogger(__name__)

_OPCODE_FAMILY = "opencode"
_BINARY_NAMES = ("opencode",)


def _sanitize_part(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in "-_.")[:80] or "none"


def _model_cli_id(model: str) -> str:
    """CLI ``--model`` id: qualify provider-less ids with ``opencode/`` (the opencode
    provider inside the CLI), pass ``provider/model`` ids through untouched."""
    model = (model or "").strip()
    if not model or "/" in model:
        return model or f"{_OPCODE_FAMILY}/big-pickle"
    return f"{_OPCODE_FAMILY}/{model}"


def _resolve_opencode_executable() -> str:
    """Locate the real ``opencode`` executable, not the npm ``.cmd``/``.ps1`` shim.

    On Windows ``shutil.which("opencode")`` resolves the npm launcher shim
    (*.cmd /*.ps1), which ``CreateProcess`` cannot spawn directly as a program. The
    actual binary lives next to the shim under ``node_modules/opencode-ai/bin/``.
    Env overrides take precedence, then PATH, then known install roots."""
    for var in ("HERMES_OPENCODE_COMMAND", "OPENCODE_CLI_PATH"):
        candidate = (os.getenv(var) or "").strip()
        if candidate:
            if shutil.which(candidate):
                return shutil.which(candidate)
            if os.path.isfile(candidate):
                return candidate
    resolved = shutil.which("opencode")  # may be a .cmd/.ps1 shim on Windows
    if resolved:
        if resolved.lower().endswith(".exe"):
            return resolved
        shim_dir = os.path.dirname(os.path.abspath(resolved))
        for candidate in (
            os.path.join(shim_dir, "node_modules", "opencode-ai", "bin", "opencode.exe"),
            os.path.join(shim_dir, "..", "node_modules", "opencode-ai", "bin", "opencode.exe"),
        ):
            normalized = os.path.normpath(candidate)
            if os.path.isfile(normalized):
                return normalized
        return resolved
    if os.name == "nt":
        for root in (
            os.path.join(os.environ.get("APPDATA", ""), "npm"),
            os.path.expanduser("~/.opencode/bin"),
            os.path.expanduser("~/AppData/Roaming/npm"),
        ):
            candidate = os.path.join(root, "node_modules", "opencode-ai", "bin", "opencode.exe")
            try:
                if os.path.isfile(candidate):
                    return candidate
            except OSError:
                continue
    return "opencode"


def _xdg_root(agent) -> Path:
    """Per-session isolated XDG root so the CLI cannot touch the user's real opencode state."""
    ident = _sanitize_part(str(getattr(agent, "session_id", None) or getattr(agent, "provider", "opencode-local")))
    import tempfile
    return Path(tempfile.gettempdir()) / f"hermes-opencode-xdg-{ident}"


def _opencode_env(root: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items()}
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        env.pop(name, None)
    subdirs = {"XDG_DATA_HOME": "data", "XDG_CONFIG_HOME": "config", "XDG_CACHE_HOME": "cache", "XDG_STATE_HOME": "state"}
    for var, sub in subdirs.items():
        env[var] = str(root / sub)
    # Suppress auto-update / telemetry noise in a hermetic subprocess.
    env.setdefault("OPENCODE_DISABLE_AUTOUPDATER", "1")
    env.setdefault("OPENCODE_NO_TELEMETRY", "1")
    env.pop("OPENCODE", None)  # we are not an opencode sub-agent
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env.pop("OPENCODE_CONFIG_PATH", None)
    return env


def _flatten_content(content: Any) -> str:
    """Flatten a message ``content`` (string or multipart list) to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for part in content:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            part_type = part.get("type", "")
            if part_type in ("text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _transcript_to_prompt(messages: List[Dict[str, Any]]) -> str:
    """Serialize the Hermes transcript into the single prompt handed to ``opencode run``."""
    blocks: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        text = _flatten_content(message.get("content", ""))
        if text:
            blocks.append(f"[{role}]\n{text}")
    return "\n\n".join(blocks)


def _opencode_tokens_to_usage(tokens: Any) -> Optional[Dict[str, int]]:
    """Translate the CLI's ``step_finish.part.tokens`` shape into the codex-shaped
    ``token_usage_last`` dict ``_record_codex_app_server_usage`` understands."""
    if not isinstance(tokens, dict) or not tokens:
        return None
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    usage = {
        "inputTokens": int(tokens.get("input") or 0),
        "outputTokens": int(tokens.get("output") or 0),
        "cachedInputTokens": int(cache.get("read") or 0),
        "totalTokens": int(tokens.get("total") or 0),
    }
    return usage if any(usage.values()) else None


def _extract_error(event: Dict[str, Any]) -> Optional[str]:
    # Top-level failure shape: {"type":"error","error":{"name":...,"data":{"message":...}}}.
    # The provider surfaces transport/rate-limit failures this way (stderr stays empty),
    # so missing it degrades to a bare "exited with code 1" with no actionable detail.
    err = event.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        message: Optional[str] = None
        status: Optional[int] = None
        if isinstance(data, dict):
            raw_message = data.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
            if isinstance(data.get("statusCode"), int):
                status = data["statusCode"]
        if message is None:
            raw_message = err.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
        if message:
            name = err.get("name")
            label = f"{name}: " if isinstance(name, str) and name else ""
            suffix = f" (status {status})" if status is not None else ""
            return f"{label}{message}{suffix}"
    part = event.get("part")
    if not isinstance(part, dict):
        return None
    part_type = part.get("type", "")
    if part_type == "error" or part.get("error"):
        text = part.get("text") or part.get("error")
        if isinstance(text, str) and text.strip():
            return text.strip()
        if isinstance(part.get("error"), dict):
            message = (part["error"] or {}).get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return None


_PREVIEW_INPUT_KEYS = ("pattern", "filePath", "path", "command", "url", "query")

_TERMINAL_TOOL_STATUSES = {"completed", "success", "failed", "error"}


def _opencode_tool_preview(tool_name: str, state: Dict[str, Any]) -> Optional[str]:
    """Short preview for the tool.started bubble; mirrors _codex_item_to_preview."""
    if not isinstance(state, dict):
        return None
    title = state.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:120]
    inputs = state.get("input")
    if not isinstance(inputs, dict):
        return None
    for key in _PREVIEW_INPUT_KEYS:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    for value in inputs.values():
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return None


def _opencode_tool_result(state: Dict[str, Any]) -> tuple[str, bool]:
    """(result_text, is_error) for a completed tool part — display-facing, capped."""
    if not isinstance(state, dict):
        return "", False
    status = str(state.get("status") or "")
    is_error = status not in {"completed", "success"} or "error" in state
    output = state.get("output", "")
    if isinstance(output, dict):
        try:
            text = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(output)
    else:
        text = output if isinstance(output, str) else ""
    return text[:4000], is_error


def _bridge_opencode_tool(agent, part: Dict[str, Any], started: Dict[str, Any],
                          finished: set[str]) -> int:
    """Project one ``tool_use`` part into the display callbacks (codex-bridge shapes).

    Fires tool.started once per callID and tool.completed on a terminal status;
    returns 1 when a tool completed (0 otherwise) for turn accounting. Every display
    callback is guarded: a buggy hook must never tear down the turn.
    """
    name = str(part.get("tool") or "unknown")
    call_id = str(part.get("callID") or "")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = str(state.get("status") or "")
    if call_id and call_id not in started and call_id not in finished:
        args = state.get("input") if isinstance(state.get("input"), dict) else {}
        started[call_id] = (name, args, time.monotonic())
        preview = _opencode_tool_preview(name, state)
        _call_guarded(getattr(agent, "tool_progress_callback", None),
                      "tool_progress_callback raised on tool.started for %s", name,
                      args=("tool.started", name, preview, args))
        _call_guarded(getattr(agent, "tool_start_callback", None),
                      "tool_start_callback raised for %s", name,
                      args=(call_id, name, args))
    if status not in _TERMINAL_TOOL_STATUSES and "error" not in state:
        return 0
    if call_id:
        if call_id in finished:
            return 0
        finished.add(call_id)
    prior = started.pop(call_id, None) if call_id else None
    result, is_error = _opencode_tool_result(state)
    duration: Optional[float] = None
    timing = state.get("time") if isinstance(state.get("time"), dict) else {}
    start_ms, end_ms = timing.get("start"), timing.get("end")
    if (isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float))
            and end_ms >= start_ms):
        duration = (end_ms - start_ms) / 1000.0
    elif prior is not None:
        duration = time.monotonic() - prior[2]
    _call_guarded(getattr(agent, "tool_progress_callback", None),
                  "tool_progress_callback raised on tool.completed for %s", name,
                  args=("tool.completed", name, None, None),
                  kwargs={"duration": duration, "is_error": is_error, "result": result})
    args = prior[1] if prior is not None else (
        state.get("input") if isinstance(state.get("input"), dict) else {})
    _call_guarded(getattr(agent, "tool_complete_callback", None),
                  "tool_complete_callback raised for %s", name,
                  args=(call_id or name, name, args, result))
    return 1


def run_opencode_cli_turn(agent, *, user_message: str, original_user_message: Any,
                          messages: List[Dict[str, Any]], effective_task_id: str,
                          should_review_memory: bool = False) -> Dict[str, Any]:
    """Hand the turn to a local ``opencode run`` subprocess; bridge its NDJSON events into
    Hermes streaming/accounting. Returns the chat_completions result shape. The user
    message is ALREADY in ``messages`` — never append it again."""
    executable = _resolve_opencode_executable()
    model_id = _model_cli_id(getattr(agent, "model", "") or "")
    xdg_root = _xdg_root(agent)
    try:
        for sub in ("data", "config", "cache", "state"):
            (xdg_root / sub).mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - exotic FS
        logger.warning("opencode_cli: could not prepare XDG root %s: %s", xdg_root, exc)

    prompt = _transcript_to_prompt(messages)
    command = [executable, "run", "--model", model_id, "--format=json", "--thinking"]
    logger.info("opencode_cli turn: model=%s xdg=%s", model_id, xdg_root)

    timeout_seconds = int(getattr(agent, "opencode_task_timeout", 0) or 1800)
    proc = None
    final_text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_started: Dict[str, Any] = {}
    tool_finished: set[str] = set()
    tool_completed = 0
    usage_last: Optional[Dict[str, int]] = None
    error: Optional[str] = None
    interrupted = False
    watchdog_expired = False

    def _watchdog(process: "subprocess.Popen") -> None:
        time.sleep(timeout_seconds)
        if process.poll() is None:
            logger.warning("opencode_cli run timed out after %ds; terminating", timeout_seconds)
            try:
                process.terminate()
            except OSError:
                pass

    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=getattr(agent, "session_cwd", None) or os.getcwd(), env=_opencode_env(xdg_root),
        )
    except OSError as exc:
        logger.exception("opencode_cli spawn failed (executable=%r)", executable)
        return _turn_result(
            _consume_user_interrupt(agent), messages, api_calls=0, completed=False, error=str(exc),
            final_response=f"OpenCode CLI turn failed to start ({executable}): {exc}. "
                           "Install the opencode CLI or set HERMES_OPENCODE_COMMAND to its path.",
        )

    watchdog = threading.Thread(target=_watchdog, args=(proc,), daemon=True)
    watchdog.start()
    try:
        try:
            proc.stdin.write(prompt.encode("utf-8", errors="replace"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        proc.stdin.close()

        for raw_line in proc.stdout:
            if getattr(agent, "_interrupt_requested", False):
                interrupted = True
                try:
                    proc.terminate()
                except OSError:
                    pass
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type", "")
            if event_type == "text":
                part = event.get("part")
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        final_text_parts.append(text)
                        try:
                            agent._fire_stream_delta(text)
                        except Exception:  # display hooks must never tear down the turn
                            logger.debug("opencode_cli stream delta hook raised", exc_info=True)
            elif event_type == "step_finish":
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("tokens"), dict):
                    usage_last = _opencode_tokens_to_usage(part["tokens"]) or usage_last
            elif event_type == "tool_use":
                part = event.get("part")
                if isinstance(part, dict):
                    tool_completed += _bridge_opencode_tool(agent, part, tool_started, tool_finished)
            elif event_type == "reasoning":
                part = event.get("part")
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        reasoning_parts.append(text)
                        _call_guarded(getattr(agent, "_fire_reasoning_delta", None),
                                      "_fire_reasoning_delta raised", args=(text,))
            elif event_type == "error":
                message = _extract_error(event)
                if message:
                    error = message
                finalized = event.get("finalized")
                if isinstance(finalized, dict):
                    final_text_parts.append(str(finalized.get("text") or ""))
            elif event_type == "done":
                break
        proc.wait(timeout=timeout_seconds + 30)
        if proc.poll() != 0 and not error:
            stderr = b""
            try:
                stderr = proc.stderr.read()
            except OSError:
                pass
            tail = stderr.decode("utf-8", errors="replace").strip()[-1200:]
            error = f"opencode run exited with code {proc.returncode}" + (f": {tail}" if tail else "")
    except subprocess.TimeoutExpired:
        watchdog_expired = True
        try:
            proc.kill()
        except OSError:
            pass
        error = f"opencode run exceeded {timeout_seconds}s and was terminated"
    finally:
        proc.stdout.close()
        try:
            proc.stderr.close()
        except OSError:
            pass

    interrupt = _consume_user_interrupt(agent, interrupted)
    final_text = "\n".join(p for p in final_text_parts if p).strip()
    if watchdog_expired:
        final_text = final_text or ""
    if error and not watchdog_expired:
        logger.warning("opencode_cli turn error: %s", error)

    # Assemble the assistant message and persist (agent_persisted=True skips the gateway rewrite).
    # Reasoning rides the canonical assistant_msg["reasoning"] store, rendered wherever the
    # surfaces show thinking; tool activity was already streamed live and stays out of history.
    assistant_message: Dict[str, Any] = {"role": "assistant", "content": final_text or ""}
    reasoning_text = "\n".join(p for p in reasoning_parts if p).strip()
    if reasoning_text:
        assistant_message["reasoning"] = reasoning_text
    turn = SimpleNamespace(
        projected_messages=[assistant_message],
        submitted_user_text=None,  # assistant row — never stripped by the turn-start-dedup
        final_text=final_text, error=(error or None), interrupted=interrupted,
        tool_iterations=tool_completed, token_usage_last=usage_last, compacted=False,
        model_context_window=None,
    )
    _persist_projected_messages(agent, turn, messages)
    usage_result = _finish_codex_turn(
        agent, turn, messages, original_user_message=original_user_message, should_review_memory=should_review_memory,
    )
    completed = not interrupted and not watchdog_expired and error is None
    return _turn_result(
        interrupt, messages, api_calls=1, completed=completed, error=error, final_response=final_text,
        agent_persisted=True, **usage_result,
    )