"""Regression test: the auxiliary OpenAI-wire factory applies per-provider
``extra_headers`` (matched by base_url) to the constructed client, mirroring the
main agent client paths (agent_init / client_lifecycle).

Reproduces the reported bug: a custom gateway that authenticates via
``extra_headers`` (e.g. an APIM subscription key) worked for main chat but 401'd
on auxiliary tasks (title generation, compression, vision) because
``_create_openai_client`` never applied those headers.

Runnable under pytest AND standalone (python tests/test_aux_extra_headers.py).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.auxiliary_client as ac

_APIM = {"Ocp-Apim-Subscription-Key": "sub-key-123", "Host": "gateway.example.com"}
_GATEWAY = "https://gateway.example.com:8443/openai"
_PUBLIC = "https://api.openai.com/v1"


def _fake_apply(mapping):
    """Stand-in for apply_custom_provider_extra_headers_to_client_kwargs: merges a
    surface's extra_headers onto client_kwargs['default_headers'] when base_url matches."""
    def _apply(client_kwargs, base_url, *a, **k):
        for pref, hdrs in mapping.items():
            if str(base_url).rstrip("/").lower().startswith(pref.rstrip("/").lower()):
                merged = dict(client_kwargs.get("default_headers") or {})
                merged.update(hdrs)
                client_kwargs["default_headers"] = merged
                return
    return _apply


class AuxExtraHeadersTest(unittest.TestCase):
    def _run_factory(self, base_url, mapping, caller_headers=None):
        captured = {}

        class _FakeOpenAI:
            def __init__(self, **kw):
                captured.update(kw)

        kwargs = {}
        if caller_headers is not None:
            kwargs["default_headers"] = dict(caller_headers)
        with mock.patch(
            "hermes_cli.config.apply_custom_provider_extra_headers_to_client_kwargs",
            side_effect=_fake_apply(mapping),
        ), mock.patch.object(ac, "OpenAI", _FakeOpenAI), \
                mock.patch.object(ac, "_openai_http_client_kwargs", return_value={}), \
                mock.patch.object(ac, "_aux_probe_active", return_value=False):
            ac._create_openai_client(api_key="dummy", base_url=base_url, **kwargs)
        return captured.get("default_headers") or {}

    def test_gateway_surface_gets_extra_headers(self):
        dh = self._run_factory(_GATEWAY, {_GATEWAY: _APIM})
        self.assertEqual(dh.get("Ocp-Apim-Subscription-Key"), "sub-key-123")
        self.assertEqual(dh.get("Host"), "gateway.example.com")

    def test_any_new_surface_generic_by_base_url(self):
        surfaces = {
            "https://gateway.example.com:8443/openai": _APIM,
            "https://gateway.example.com:8443/OnPrem": {"Ocp-Apim-Subscription-Key": "k2"},
        }
        for base, expected in surfaces.items():
            dh = self._run_factory(base, surfaces)
            for k, v in expected.items():
                self.assertEqual(dh.get(k), v, f"{base} missing {k}")

    def test_public_endpoint_untouched(self):
        dh = self._run_factory(_PUBLIC, {_GATEWAY: _APIM})
        self.assertNotIn("Ocp-Apim-Subscription-Key", dh)

    def test_config_failure_never_strips_caller_headers(self):
        def _boom(client_kwargs, base_url, *a, **k):
            raise RuntimeError("config unreadable")

        captured = {}

        class _FakeOpenAI:
            def __init__(self, **kw):
                captured.update(kw)

        with mock.patch(
            "hermes_cli.config.apply_custom_provider_extra_headers_to_client_kwargs",
            side_effect=_boom,
        ), mock.patch.object(ac, "OpenAI", _FakeOpenAI), \
                mock.patch.object(ac, "_openai_http_client_kwargs", return_value={}), \
                mock.patch.object(ac, "_aux_probe_active", return_value=False):
            ac._create_openai_client(
                api_key="dummy", base_url=_GATEWAY, default_headers={"X-Keep": "1"})
        self.assertEqual((captured.get("default_headers") or {}).get("X-Keep"), "1")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(AuxExtraHeadersTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
