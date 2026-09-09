"""Regression tests for the generic custom-gateway header injection on the
OpenAI-wire auxiliary path (Penelope carried patch).

Proves the ONE requirement Shailen asked for: any custom_providers entry's
subscription headers reach the wire with ZERO per-provider code, matched purely
by base_url -- the same way the Anthropic path already works.

Runnable standalone (python tests/test_penelope_aux_generic_headers.py) AND
under pytest. Penelope's hermes venv has no pytest, so keep it stdlib-only.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.auxiliary_client as ac

_APIM = {"Ocp-Apim-Subscription-Key": "sub-key-123", "Host": "llm-api.amd.com"}


class MergeCustomGatewayHeadersTest(unittest.TestCase):
    def _patch_resolver(self, mapping):
        def _fake(base_url):
            for pref, hdrs in mapping.items():
                if str(base_url).rstrip("/").lower().startswith(pref.rstrip("/").lower()):
                    return dict(hdrs)
            return {}
        return mock.patch(
            "hermes_cli.config.get_custom_provider_extra_headers",
            side_effect=_fake,
        )

    def test_generic_across_every_surface(self):
        """openai AND OnPrem AND any future surface all get their headers from the
        single resolver, no surface-specific branch."""
        surfaces = {
            "https://localhost:9443/openai": _APIM,
            "https://localhost:9443/OnPrem": _APIM,
            "https://localhost:9443/BrandNewSurface": {"Ocp-Apim-Subscription-Key": "k2"},
        }
        with self._patch_resolver(surfaces):
            for base, expected in surfaces.items():
                merged = ac._merge_custom_gateway_headers(base, None)
                for k, v in expected.items():
                    self.assertEqual(merged.get(k), v, f"{base} missing {k}")

    def test_caller_headers_win(self):
        with self._patch_resolver({"https://localhost:9443/openai": _APIM}):
            merged = ac._merge_custom_gateway_headers(
                "https://localhost:9443/openai", {"Host": "override.example"})
            self.assertEqual(merged["Ocp-Apim-Subscription-Key"], "sub-key-123")
            self.assertEqual(merged["Host"], "override.example")  # caller wins

    def test_non_gateway_url_untouched(self):
        """A public endpoint (no matching custom_providers entry) is never given
        gateway headers -- isolation guard."""
        with self._patch_resolver({"https://localhost:9443/openai": _APIM}):
            self.assertIsNone(ac._merge_custom_gateway_headers("https://api.openai.com/v1", None))
            existing = {"X-Foo": "bar"}
            self.assertEqual(
                ac._merge_custom_gateway_headers("https://api.openai.com/v1", existing), existing)

    def test_resolver_failure_never_strips_existing(self):
        def _boom(base_url):
            raise RuntimeError("config unreadable")
        with mock.patch(
            "hermes_cli.config.get_custom_provider_extra_headers", side_effect=_boom):
            existing = {"Authorization": "Bearer x"}
            self.assertEqual(
                ac._merge_custom_gateway_headers("https://localhost:9443/openai", existing), existing)
            self.assertIsNone(ac._merge_custom_gateway_headers("https://localhost:9443/openai", None))

    def test_create_openai_client_attaches_gateway_headers(self):
        """End to end through the real factory: the constructed OpenAI client is
        given default_headers carrying the subscription key."""
        captured = {}

        class _FakeOpenAI:
            def __init__(self, **kw):
                captured.update(kw)

        with self._patch_resolver({"https://localhost:9443/openai": _APIM}), \
                mock.patch.object(ac, "OpenAI", _FakeOpenAI), \
                mock.patch.object(ac, "_openai_http_client_kwargs", return_value={}), \
                mock.patch.object(ac, "_aux_probe_active", return_value=False):
            ac._create_openai_client(api_key="dummy", base_url="https://localhost:9443/openai")
        dh = captured.get("default_headers") or {}
        self.assertEqual(dh.get("Ocp-Apim-Subscription-Key"), "sub-key-123")
        self.assertEqual(dh.get("Host"), "llm-api.amd.com")

    def test_create_openai_client_public_endpoint_no_gateway_headers(self):
        captured = {}

        class _FakeOpenAI:
            def __init__(self, **kw):
                captured.update(kw)

        with self._patch_resolver({"https://localhost:9443/openai": _APIM}), \
                mock.patch.object(ac, "OpenAI", _FakeOpenAI), \
                mock.patch.object(ac, "_openai_http_client_kwargs", return_value={}), \
                mock.patch.object(ac, "_aux_probe_active", return_value=False):
            ac._create_openai_client(api_key="dummy", base_url="https://api.openai.com/v1")
        dh = captured.get("default_headers") or {}
        self.assertNotIn("Ocp-Apim-Subscription-Key", dh)


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(MergeCustomGatewayHeadersTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
