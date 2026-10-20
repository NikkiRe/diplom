"""MCP source persistence is config only, not discovery, RPC or credential storage."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neurobot.ui import catalog, source_state


class SourceStateTest(unittest.TestCase):
    def test_private_roundtrip_only_url_label_no_rpc(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(catalog.Client, "rpc") as rpc:
            state = source_state.SourceState(directory)
            self.assertEqual(state.load(), [])
            snapshot = {"sources": [{"url": "http://localhost:19000/mcp", "label": "Sensors",
                                      "server_info": {"secret": "not persisted"}, "status": "ready"}],
                        "devices": [{"sensitive": "not persisted"}]}
            self.assertIsNone(state.save(snapshot))
            self.assertEqual(source_state.SourceState(directory).load(), [{"url": "http://localhost:19000/mcp", "label": "Sensors"}])
            raw = state.path.read_text()
            self.assertNotIn("secret", raw)
            self.assertNotIn("sensitive", raw)
            self.assertNotIn("status", raw)
            self.assertEqual(os.stat(state.path).st_mode & 0o777, 0o600)
            rpc.assert_not_called()

    def test_corruption_is_explicit_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            state = source_state.SourceState(directory)
            state.path.write_text("broken JSON")
            self.assertEqual(state.load(), [])
            self.assertTrue(state.error)
            self.assertTrue(state.save({"sources": []}))
            self.assertEqual(state.path.read_text(), "broken JSON")

    def test_invalid_endpoints_limits_and_atomic_error_preserve_old_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = source_state.SourceState(directory)
            valid = {"sources": [{"url": "http://127.0.0.1:19000/mcp", "label": ""}]}
            self.assertIsNone(state.save(valid))
            previous = state.path.read_bytes()
            for url in ("http://192.168.1.1/mcp", "https://127.0.0.1/mcp", "http://user:password@localhost/mcp"):
                self.assertTrue(state.save({"sources": [{"url": url}]}))
                self.assertEqual(state.path.read_bytes(), previous)
            self.assertTrue(state.save({"sources": [{"url": "http://localhost:%d/mcp" % n} for n in range(19000, 19009)]}))
            self.assertEqual(state.path.read_bytes(), previous)
            with mock.patch.object(source_state.os, "replace", side_effect=PermissionError("denied")):
                self.assertTrue(state.save({"sources": []}))
            self.assertEqual(state.path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).glob(".mcp-sources-*")), [])

    def test_no_state_dir_is_noop(self):
        state = source_state.SourceState()
        self.assertEqual(state.load(), [])
        self.assertIsNone(state.save({"not": "persisted"}))
        self.assertIsNone(state.error)

    def test_git_path_is_rejected_and_existing_data_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: external")
            state = source_state.SourceState(root / "private")
            self.assertTrue(state.save({"sources": []}))
            self.assertIn("Git", state.error)
            self.assertFalse(state.path.exists())

    def test_auto_url_label_and_duplicate_or_oversized_saved_config(self):
        with tempfile.TemporaryDirectory() as directory:
            state = source_state.SourceState(directory)
            url = "http://localhost:19000/mcp"
            self.assertIsNone(state.save({"sources": [{"url": url, "label": url}]}))
            self.assertEqual(state.load(), [{"url": url, "label": ""}])
            duplicate = {"format": source_state.FORMAT, "sources": [{"url": url}, {"url": url}]}
            state.path.write_text(json.dumps(duplicate))
            self.assertEqual(state.load(), [])
            self.assertTrue(state.error)
            state.path.write_bytes(b"x" * (source_state.MAX_BYTES + 1))
            self.assertEqual(state.load(), [])
            self.assertIn("64 KiB", state.error)


if __name__ == "__main__":
    unittest.main()
