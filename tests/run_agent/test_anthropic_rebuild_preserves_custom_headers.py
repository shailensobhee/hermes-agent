"""APIM-style ``custom_headers`` survive Anthropic client rebuilds.

When the user interrupts a running turn on a messaging-platform gateway
(Telegram, Discord, etc.), ``chat_completion_helpers.interruptible_api_call``
force-closes the in-flight Anthropic client and calls
``AIAgent._rebuild_anthropic_client()`` to seed the next retry.  Same path
fires for stale-call kills, credential rotations, and the auxiliary
fallback rebuild.

Pre-fix, every one of those rebuild sites called
``build_anthropic_client(api_key, base_url, timeout=...)`` and dropped
the ``default_headers`` (subscription key, Host override) and ``verify``
flag that the original ``init_agent`` path had resolved from
``custom_providers``.  APIM-style gateways (AMD LLM Gateway, Azure APIM,
Kong) then returned HTTP 401 on the next call, silently breaking the
session's main LLM connection.

These tests pin the regression — see
``hermes-agent`` skill ``references/custom-provider-headers-pitfall.md``
Problem 14 for the full diagnosis playbook.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


_APIM_BASE = "https://gateway.example.com:8443/Anthropic"
_APIM_HEADERS = {
    "Ocp-Apim-Subscription-Key": "test-sub-key-123",
    "Host": "gateway.example.com",
}


def _make_apim_cfg() -> dict:
    """Minimal config.yaml shape with one APIM-style custom_providers entry."""
    return {
        "custom_providers": [
            {
                "id": "test-apim-gateway",
                "name": "Test APIM Gateway",
                "api_mode": "anthropic_messages",
                "base_url": _APIM_BASE,
                "custom_headers": dict(_APIM_HEADERS),
                "verify": False,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Helper: _resolve_custom_provider_headers_for_base_url
# ---------------------------------------------------------------------------


def test_resolve_helper_returns_headers_for_matching_base_url():
    from run_agent import _resolve_custom_provider_headers_for_base_url

    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        headers, verify = _resolve_custom_provider_headers_for_base_url(_APIM_BASE)

    assert headers == _APIM_HEADERS
    assert verify is False


def test_resolve_helper_case_and_slash_insensitive():
    from run_agent import _resolve_custom_provider_headers_for_base_url

    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        headers, verify = _resolve_custom_provider_headers_for_base_url(
            _APIM_BASE.upper().rstrip("/") + "/"
        )

    assert headers == _APIM_HEADERS
    assert verify is False


def test_resolve_helper_returns_fallback_when_no_match():
    from run_agent import _resolve_custom_provider_headers_for_base_url

    fallback = {"X-Stashed": "1"}
    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        headers, verify = _resolve_custom_provider_headers_for_base_url(
            "https://different-host.example.com/v1",
            fallback_headers=fallback,
            fallback_verify=False,
        )

    assert headers == fallback
    assert verify is False


def test_resolve_helper_empty_base_url_returns_fallback():
    from run_agent import _resolve_custom_provider_headers_for_base_url

    headers, verify = _resolve_custom_provider_headers_for_base_url(
        "", fallback_headers={"X": "1"}, fallback_verify=False
    )
    assert headers == {"X": "1"}
    assert verify is False


def test_resolve_helper_swallows_config_load_failures():
    from run_agent import _resolve_custom_provider_headers_for_base_url

    with patch(
        "hermes_cli.config.load_config", side_effect=RuntimeError("config broken")
    ):
        headers, verify = _resolve_custom_provider_headers_for_base_url(
            _APIM_BASE,
            fallback_headers={"X-Fallback": "1"},
            fallback_verify=True,
        )

    assert headers == {"X-Fallback": "1"}
    assert verify is True


# ---------------------------------------------------------------------------
# Helper: init stash — agent_init.py populates _anthropic_default_headers
# ---------------------------------------------------------------------------


def test_init_stashes_apim_headers_on_agent():
    """Native anthropic_messages init must stash custom_headers + verify."""
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="test-key",
                base_url=_APIM_BASE,
                provider="custom:test-apim-gateway",
                api_mode="anthropic_messages",
                model="Claude-Sonnet-4.6",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

    # The first call carries headers/verify
    init_call = mock_build.call_args_list[0]
    assert init_call.kwargs.get("default_headers") == _APIM_HEADERS
    assert init_call.kwargs.get("verify") is False

    # Stash exposed for rebuild sites
    assert agent._anthropic_default_headers == _APIM_HEADERS
    assert agent._anthropic_verify is False


def test_init_defaults_when_no_custom_provider_match():
    """Non-APIM providers must NOT inherit stale headers (regression guard)."""
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config", return_value={"custom_providers": []}):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="test-key",
                base_url="https://api.anthropic.com",
                provider="anthropic",
                api_mode="anthropic_messages",
                model="claude-3-5-sonnet-20241022",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

    assert agent._anthropic_default_headers is None
    assert agent._anthropic_verify is True


# ---------------------------------------------------------------------------
# Rebuild: _rebuild_anthropic_client (interrupt + stale-call path)
# ---------------------------------------------------------------------------


def test_rebuild_anthropic_client_preserves_apim_headers():
    """The interrupt rebuild must re-attach the stashed APIM headers."""
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="test-key",
                base_url=_APIM_BASE,
                provider="custom:test-apim-gateway",
                api_mode="anthropic_messages",
                model="Claude-Sonnet-4.6",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            mock_build.reset_mock()

            # Simulate a Telegram interrupt → rebuild path
            agent._rebuild_anthropic_client()

    assert mock_build.called, "rebuild must construct a new Anthropic client"
    rebuild_call = mock_build.call_args
    assert rebuild_call.kwargs.get("default_headers") == _APIM_HEADERS
    assert rebuild_call.kwargs.get("verify") is False


def test_rebuild_anthropic_client_uses_stash_when_config_unavailable():
    """Even if config loading fails at rebuild time, the stash wins."""
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="test-key",
                base_url=_APIM_BASE,
                provider="custom:test-apim-gateway",
                api_mode="anthropic_messages",
                model="Claude-Sonnet-4.6",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            mock_build.reset_mock()

            # Rebuild while config load is broken — stash should still apply
            with patch(
                "hermes_cli.config.load_config",
                side_effect=RuntimeError("config gone"),
            ):
                agent._rebuild_anthropic_client()

    rebuild_call = mock_build.call_args
    assert rebuild_call.kwargs.get("default_headers") == _APIM_HEADERS
    assert rebuild_call.kwargs.get("verify") is False


def test_rebuild_non_apim_provider_does_not_inject_headers():
    """Non-APIM rebuilds must not invent default_headers / verify=False."""
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config", return_value={"custom_providers": []}):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="test-key",
                base_url="https://api.anthropic.com",
                provider="anthropic",
                api_mode="anthropic_messages",
                model="claude-3-5-sonnet-20241022",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            mock_build.reset_mock()

            agent._rebuild_anthropic_client()

    rebuild_call = mock_build.call_args
    assert rebuild_call.kwargs.get("default_headers") is None
    assert rebuild_call.kwargs.get("verify") is True


# ---------------------------------------------------------------------------
# Rebuild: _try_refresh_anthropic_client_credentials (Anthropic-OAuth refresh)
# ---------------------------------------------------------------------------


def test_oauth_refresh_preserves_apim_headers():
    """OAuth credential refresh must keep APIM headers attached too.

    This path is only ever exercised on the native ``anthropic`` provider,
    so we use a stashed-headers shape (e.g. an Anthropic-on-Foundry style
    deployment that needs an extra Azure header) to prove the rebuild
    forwards the stash even when the path is gated to provider==anthropic.
    """
    from run_agent import AIAgent

    # Native anthropic endpoint with a stashed extra header
    with patch("hermes_cli.config.load_config", return_value={"custom_providers": []}):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="sk-ant-old-key",
                base_url="https://api.anthropic.com",
                provider="anthropic",
                api_mode="anthropic_messages",
                model="claude-3-5-sonnet-20241022",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            # Pretend init resolved an extra header from elsewhere
            agent._anthropic_default_headers = {"X-Extra": "stashed"}
            agent._anthropic_verify = False
            mock_build.reset_mock()

            with patch(
                "agent.anthropic_adapter.resolve_anthropic_token",
                return_value="sk-ant-new-rotated-key",
            ):
                refreshed = agent._try_refresh_anthropic_client_credentials()

    assert refreshed is True
    rebuild_call = mock_build.call_args
    assert rebuild_call.kwargs.get("default_headers") == {"X-Extra": "stashed"}
    assert rebuild_call.kwargs.get("verify") is False


# ---------------------------------------------------------------------------
# Rebuild: _swap_credential (credential pool rotation)
# ---------------------------------------------------------------------------


def test_swap_credential_reresolves_headers_for_new_base_url():
    """Swapping to a new base_url must re-resolve from custom_providers."""
    from run_agent import AIAgent

    # Two APIM gateways with different subscription keys
    cfg_two_gws = {
        "custom_providers": [
            {
                "id": "gw-a",
                "name": "Gateway A",
                "api_mode": "anthropic_messages",
                "base_url": _APIM_BASE,
                "custom_headers": {"Ocp-Apim-Subscription-Key": "key-A"},
                "verify": False,
            },
            {
                "id": "gw-b",
                "name": "Gateway B",
                "api_mode": "anthropic_messages",
                "base_url": "https://other-gw.example.com/Anthropic",
                "custom_headers": {"Ocp-Apim-Subscription-Key": "key-B"},
                "verify": True,
            },
        ]
    }

    with patch("hermes_cli.config.load_config", return_value=cfg_two_gws):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="key-A",
                base_url=_APIM_BASE,
                provider="custom:gw-a",
                api_mode="anthropic_messages",
                model="Claude-Sonnet-4.6",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            mock_build.reset_mock()

            # Simulate credential pool rotating us to Gateway B
            swap_entry = MagicMock()
            swap_entry.runtime_api_key = "key-B"
            swap_entry.runtime_base_url = "https://other-gw.example.com/Anthropic"
            swap_entry.access_token = "key-B"
            swap_entry.base_url = "https://other-gw.example.com/Anthropic"
            agent._swap_credential(swap_entry)

    swap_call = mock_build.call_args
    assert swap_call.kwargs.get("default_headers") == {
        "Ocp-Apim-Subscription-Key": "key-B"
    }
    assert swap_call.kwargs.get("verify") is True
    # Stash also updated for any subsequent interrupt rebuild
    assert agent._anthropic_default_headers == {"Ocp-Apim-Subscription-Key": "key-B"}
    assert agent._anthropic_verify is True


def test_swap_credential_falls_back_to_stash_when_no_match():
    """Swap to an unrecognized base_url must keep existing stash, not None."""
    from run_agent import AIAgent

    with patch("hermes_cli.config.load_config", return_value=_make_apim_cfg()):
        with patch("agent.anthropic_adapter.build_anthropic_client") as mock_build:
            mock_build.return_value = MagicMock()
            agent = AIAgent(
                api_key="test-key",
                base_url=_APIM_BASE,
                provider="custom:test-apim-gateway",
                api_mode="anthropic_messages",
                model="Claude-Sonnet-4.6",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert agent._anthropic_default_headers == _APIM_HEADERS
            mock_build.reset_mock()

            # Swap to an endpoint NOT listed in custom_providers
            swap_entry = MagicMock()
            swap_entry.runtime_api_key = "other-key"
            swap_entry.runtime_base_url = "https://unknown.example.com/v1"
            swap_entry.access_token = "other-key"
            swap_entry.base_url = "https://unknown.example.com/v1"
            agent._swap_credential(swap_entry)

    swap_call = mock_build.call_args
    # No match → previously-stashed headers are preserved as the fallback
    assert swap_call.kwargs.get("default_headers") == _APIM_HEADERS
    assert swap_call.kwargs.get("verify") is False
