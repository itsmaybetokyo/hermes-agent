"""Tests for opencode-local model validation.

``opencode-local`` runs a local ``opencode`` CLI: there is no HTTP ``/v1/models``
endpoint to probe, so validation goes through the static-catalog branch
(``opencode models`` output). These tests pin that branch: catalog membership
accepts, unknown ids soft-accept, and the branch never crashes on the provider's
label lookup.
"""

from unittest.mock import patch

from hermes_cli.models_validate import validate_requested_model

_CATALOG = [
    "opencode/big-pickle",
    "opencode/ling-3.0-flash-fin-free",
    "openrouter/openrouter/free",
    "openrouter/openrouter/auto",
]


def _validate(model: str):
    with patch("hermes_cli.models_validate._static_catalog", return_value=_CATALOG):
        return validate_requested_model(model, "opencode-local")


def test_opencode_local_known_model_accepted():
    result = _validate("opencode/big-pickle")
    assert result["accepted"] is True
    assert result["persist"] is True
    assert result["recognized"] is True
    assert result["message"] is None


def test_opencode_local_nested_openrouter_entry_accepted():
    result = _validate("openrouter/openrouter/free")
    assert result["accepted"] is True
    assert result["persist"] is True
    assert result["recognized"] is True
    assert result["message"] is None


def test_opencode_local_unknown_model_soft_accepts():
    result = _validate("some-brand-new-model")
    assert result["accepted"] is True
    assert result["persist"] is True
    assert result["recognized"] is False
    assert "OpenCode CLI" in result["message"]


def test_opencode_local_base_url_does_not_probe_live_listing():
    # The validator must not fall into the live /v1/models probe: the local CLI
    # has no HTTP listing, so a reachable-looking base_url must not hard-reject
    # a catalog member (regression: KeyError on the label lookup).
    with patch("hermes_cli.models_validate._static_catalog", return_value=_CATALOG):
        result = validate_requested_model(
            "openrouter/openrouter/free", "opencode-local",
            base_url="http://localhost:9999/v1", api_mode="opencode_cli")
    assert result["accepted"] is True


def test_opencode_local_is_known_provider_name():
    # opencode-local must be a KNOWN provider so the main-slot save path never
    # treats it as an unknown vendor prefix. Regression: the Settings Apply
    # (_normalize_main_model_assignment) remapped opencode-local → openrouter,
    # then hard-rejected opencode-local models via the openrouter listing.
    from hermes_cli.models import _KNOWN_PROVIDER_NAMES

    assert "opencode-local" in _KNOWN_PROVIDER_NAMES


def test_main_assignment_keeps_opencode_local_provider():
    # The desktop Settings "Apply" goes through _prepare_main_assignment →
    # _normalize_main_model_assignment + switch_model. It must keep the explicit
    # opencode-local provider (not remap to openrouter) and accept the ids.
    from hermes_cli.web_server_config import _normalize_main_model_assignment, _prepare_main_assignment

    for model in ("opencode/big-pickle", "openrouter/openrouter/free"):
        provider, kept = _normalize_main_model_assignment("opencode-local", model)
        assert provider == "opencode-local", (provider, kept)
        assert kept == model
        _, result = _prepare_main_assignment({}, "opencode-local", model, "opencode://local", "")
        assert result.success is True
        assert result.target_provider == "opencode-local"
        assert result.new_model == model