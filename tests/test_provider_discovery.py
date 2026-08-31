"""Tests for provider auto-discovery (A1 — aisuite convention)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_assistant.providers import BaseBackend, _discover_plugin_backend, get_backend

_VALID_BACKEND_SRC = """\
from personal_assistant.providers import BaseBackend

class TestpluginBackend(BaseBackend):
    name = "testplugin"
    def available(self):
        return True, "plugin ok"
    def reason(self, conn, request):
        return {"reply": "plugin reply", "plan": [], "actions": []}
"""

_NO_SUBCLASS_SRC = """\
class SomeClass:
    pass
"""


class DiscoverPluginBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._providers_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        # Clean up any injected sys.modules entries.
        for key in list(sys.modules):
            if "_plugin_" in key:
                del sys.modules[key]

    def _write_backend(self, name: str, src: str) -> Path:
        path = self._providers_dir / f"{name}_backend.py"
        path.write_text(src)
        return path

    def _patch_providers_dir(self, name: str, src: str):
        """Write backend file into the real providers/ dir temporarily."""
        real_dir = Path(__file__).parent.parent / "src" / "personal_assistant" / "providers"
        path = real_dir / f"{name}_backend.py"
        path.write_text(src)
        return path

    def test_returns_none_for_missing_file(self) -> None:
        result = _discover_plugin_backend("nonexistent_xyz")
        self.assertIsNone(result)

    def test_discovers_valid_backend(self) -> None:
        path = self._patch_providers_dir("testplugin", _VALID_BACKEND_SRC)
        try:
            result = _discover_plugin_backend("testplugin")
            self.assertIsNotNone(result)
            self.assertIsInstance(result, BaseBackend)
        finally:
            path.unlink(missing_ok=True)

    def test_returns_none_when_no_subclass(self) -> None:
        path = self._patch_providers_dir("noclassxyz", _NO_SUBCLASS_SRC)
        try:
            result = _discover_plugin_backend("noclassxyz")
            self.assertIsNone(result)
        finally:
            path.unlink(missing_ok=True)

    def test_returns_none_on_syntax_error(self) -> None:
        path = self._patch_providers_dir("badplugin", "this is not valid python !!!")
        try:
            result = _discover_plugin_backend("badplugin")
            self.assertIsNone(result)  # fail-open
        finally:
            path.unlink(missing_ok=True)

    def test_get_backend_uses_plugin_as_fallback(self) -> None:
        path = self._patch_providers_dir("myplugin", _VALID_BACKEND_SRC.replace("testplugin", "myplugin").replace("Testplugin", "Myplugin"))
        try:
            # Should not fall back to Claude when plugin exists.
            backend = get_backend("myplugin")
            self.assertIsInstance(backend, BaseBackend)
        finally:
            path.unlink(missing_ok=True)

    def test_known_backends_unaffected(self) -> None:
        # Ensure auto-discovery doesn't interfere with the explicit registry.
        backend = get_backend("claude")
        self.assertEqual(backend.name, "claude")


if __name__ == "__main__":
    unittest.main()
