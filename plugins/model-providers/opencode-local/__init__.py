"""OpenCode CLI local-run provider profile (api_mode ``opencode_cli``).

Unlike copilot-acp, this is NOT an ACP stdio client: each Hermes turn spawns a fresh
``opencode run --model <model> --format=json`` subprocess (see ``agent/opencode_runtime.py``).
The profile describes how to locate the CLI so auth.py's external-process machinery can report
``configured``/resolve a command hint; the runtime does the actual spawn itself.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class OpenCodeLocalProfile(ProviderProfile):
    """OpenCode CLI — local per-turn subprocess, no REST models endpoint."""

    def create_client(self, **client_kwargs: Any) -> Any:
        """No HTTP/ACP client: the runtime spawns ``opencode run`` itself."""
        return None

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """No live model listing: curated fallback_models drive the picker."""
        return None


opencode_local = OpenCodeLocalProfile(
    name="opencode-local", aliases=("opencode-cli-local", "opencode-cli"),
    api_mode="opencode_cli",
    display_name="OpenCode CLI (local run)",
    description="Run OpenCode models (e.g. big-pickle) via the local opencode CLI",
    env_vars=(),
    base_url="opencode://local",  # internal scheme; the runtime ignores it
    auth_type="external_process",
    # How to launch the CLI.
    process_command="opencode",
    process_args=("run", "--format=json"),
    process_command_env_vars=("HERMES_OPENCODE_COMMAND", "OPENCODE_CLI_PATH"),
    process_args_env_var="HERMES_OPENCODE_ARGS",
    # Single canonical id (the runtime normalizes "big-pickle" -> "opencode/big-pickle for
    # the subprocess); listing both made the picker offer the same model twice.
    fallback_models=("big-pickle",),
    supports_model_listing=False,
    supports_health_check=False,
)

register_provider(opencode_local)