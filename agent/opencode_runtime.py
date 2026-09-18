"""OpenCode CLI runtime (api_mode = ``opencode_cli``).

The ``opencode_cli`` API mode hands each Hermes turn to the LOCAL ``opencode`` CLI:
one ``opencode run --model <model> --format=json`` subprocess per turn (stateless,
no persistent session), the Hermes transcript passed via ``stdin``, and the CLI's
NDJSON ``type:text`` / ``type:step_finish`` events bridged into Hermes' stream deltas
and token accounting. Big-pickle and friends talk OpenCode's *free tier*, so with this
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
    command = [executable, "run", "--model", model_id, "--format=json"]
    logger.info("opencode_cli turn: model=%s xdg=%s", model_id, xdg_root)

    timeout_seconds = int(getattr(agent, "opencode_task_timeout", 0) or 1800)
    proc = None
    final_text_parts: List[str] = []
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
    assistant_message: Dict[str, Any] = {"role": "assistant", "content": final_text or ""}
    turn = SimpleNamespace(
        projected_messages=[assistant_message],
        submitted_user_text=None,  # assistant row — never stripped by the turn-start-dedup
        final_text=final_text, error=(error or None), interrupted=interrupted,
        tool_iterations=0, token_usage_last=usage_last, compacted=False, model_context_window=None,
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