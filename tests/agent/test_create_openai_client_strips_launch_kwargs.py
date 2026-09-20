"""Guardrail: external-process launch kwargs must never reach the OpenAI SDK.

``_explicit_client_kwargs`` attaches ``command``/``args`` for every external-process
provider (copilot-acp, opencode-local, ...). Those keys are consumed by
``ProviderProfile.create_client``; a profile that returns None (opencode-local: the
runtime spawns the CLI itself, there is no HTTP/ACP client) falls through to the
plain ``OpenAI(**client_kwargs)`` construction, which rejects them with
``TypeError: OpenAI.__init__() got an unexpected keyword argument 'command'`` and
surfaces as ``Failed to initialize OpenAI client`` — blocking every model while the
provider stays pinned to the external one.
"""
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _agent(**overrides):
    params = {
        "api_key": "test-key",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openrouter/deepseek/deepseek-v4-flash-0731:free",
        "provider": "openrouter",
        "quiet_mode": True,
        "skip_context_files": True,
        "skip_memory": True,
    }
    params.update(overrides)
    return AIAgent(**params)


@patch("agent.process_bootstrap.OpenAI")
def test_openai_constructor_never_receives_launch_kwargs(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = _agent()
    kwargs = {
        "api_key": "test-key",
        "base_url": "https://openrouter.ai/api/v1",
        "command": "opencode",
        "args": ["run", "--format=json"],
    }
    snapshot = dict(kwargs)

    agent._create_openai_client(kwargs, reason="test", shared=False)

    assert kwargs == snapshot
    _, constructed = mock_openai.call_args
    assert "command" not in constructed and "args" not in constructed


@patch("agent.process_bootstrap.OpenAI")
def test_opencode_local_explicit_init_builds_client(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = _agent(
        provider="opencode-local",
        acp_command="opencode",
        acp_args=["run", "--format=json"],
    )

    assert mock_openai.called
    _, constructed = mock_openai.call_args
    assert "command" not in constructed and "args" not in constructed
    assert constructed["base_url"] == "https://openrouter.ai/api/v1"
