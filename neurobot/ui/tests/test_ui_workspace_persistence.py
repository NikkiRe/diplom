"""HTTP workspace persistence across real local server restarts, without a model."""
import http.client
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from neurobot.ui import server
from neurobot.ui.tests.test_ui_boards import minimal_report
from neurobot.ui.tests.test_ui_catalog import FakeMCP, wait_for


class WorkspacePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="edgelab-http-state-")
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        self.fake = FakeMCP()
        self.addCleanup(self.fake.close)
        self.srv = None
        self.addCleanup(self.stop)

    def start(self, durable=True):
        self.stop()
        self.srv = server.make_server(None, 0, state_dir=str(self.state_dir) if durable else None)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.thread.join(timeout=2)
            self.srv = None

    def request(self, path, method="GET", body=None):
        host = "127.0.0.1:%d" % self.srv.server_port
        headers = {"Host": host, "Origin": "http://" + host, "Content-Type": "application/json"}
        connection = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_sources_passports_and_chat_journal_survive_restart(self):
        self.start()
        code, connected = self.request("/api/catalog/sources", "POST", {"url": self.fake.url, "label": "Explicit local sensors"})
        self.assertEqual(code, 200, connected)
        self.assertEqual(connected["persistence"], {"durable": True, "error": None})
        source_id = connected["source_id"]
        wait_for(lambda: any(s["status"] == "ready" for s in self.request("/api/catalog")[1]["sources"]))
        report = minimal_report()
        report["identity"].update(model="Imported experimental ARM board", kind="linux_device")
        report["arbitrary_extension"] = {"untouched": False}
        code, imported = self.request("/api/boards/import", "POST", {"report": report, "label": "Lab board"})
        self.assertEqual(code, 201, imported)
        board_id = imported["board"]["id"]
        code, created = self.request("/api/assistant/sessions", "POST", {"title": "Persistent operator journal"})
        self.assertEqual(code, 201, created)
        session_id = created["session"]["id"]
        state_file = self.state_dir / "mcp-sources.json"
        self.assertEqual(json.loads(state_file.read_text())["sources"], [{"url": self.fake.url, "label": "Explicit local sensors"}])
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)

        self.start()
        wait_for(lambda: any(s["status"] == "ready" for s in self.request("/api/catalog")[1]["sources"]))
        code, catalog = self.request("/api/catalog")
        self.assertEqual(code, 200)
        self.assertEqual(catalog["sources"][0]["id"], source_id)
        self.assertEqual(catalog["sources"][0]["label"], "Explicit local sensors")
        self.assertTrue(catalog["devices"])
        self.assertEqual(catalog["persistence"], {"durable": True, "error": None})
        self.assertEqual(self.request("/api/boards/" + board_id)[1]["board"]["report"], report)
        self.assertEqual(self.request("/api/assistant/sessions/" + session_id)[1]["session"]["title"], "Persistent operator journal")
        self.assertFalse(any(call["method"] == "tools/call" for call in self.fake.calls))

        code, removed = self.request("/api/catalog/disconnect", "POST", {"id": source_id})
        self.assertEqual(code, 200, removed)
        self.assertEqual(json.loads(state_file.read_text())["sources"], [])
        self.start()
        self.assertEqual(self.request("/api/catalog")[1]["sources"], [])

    def test_corrupt_saved_sources_warn_without_overwrite_or_registry_loss(self):
        self.state_dir.mkdir()
        path = self.state_dir / "mcp-sources.json"
        original = b'{"format":"broken", "sources":'
        path.write_bytes(original)
        self.start()
        code, initial = self.request("/api/catalog")
        self.assertEqual(code, 200)
        self.assertEqual(initial["sources"], [])
        self.assertTrue(initial["persistence"]["error"])
        code, connected = self.request("/api/catalog/sources", "POST", {"url": self.fake.url})
        self.assertEqual(code, 200, connected)
        self.assertTrue(connected["persistence"]["error"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.request("/api/catalog")[1]["sources"]), 1)
        self.start()
        restored = self.request("/api/catalog")[1]
        self.assertEqual(restored["sources"], [])
        self.assertTrue(restored["persistence"]["error"])
        self.assertEqual(path.read_bytes(), original)

    def test_ephemeral_constructor_does_not_persist_sources(self):
        self.start(durable=False)
        code, connected = self.request("/api/catalog/sources", "POST", {"url": self.fake.url})
        self.assertEqual(code, 200)
        self.assertEqual(connected["persistence"], {"durable": False, "error": None})
        self.start(durable=False)
        self.assertEqual(self.request("/api/catalog")[1]["sources"], [])
        self.assertFalse(self.state_dir.exists())

    def test_concurrent_source_additions_cannot_overwrite_a_newer_configuration(self):
        self.start()
        first_save_entered = threading.Event()
        release_first_save = threading.Event()
        second_finished = threading.Event()
        original_save = self.srv.source_state.save
        calls, results = [], []

        def delay_first(snapshot):
            calls.append(snapshot)
            if len(calls) == 1:
                first_save_entered.set()
                if not release_first_save.wait(3):
                    raise AssertionError("first persistence call was not released")
            return original_save(snapshot)

        def connect(url, done=None):
            try:
                results.append(self.request("/api/catalog/sources", "POST", {"url": url}))
            except Exception as exc:
                results.append(exc)
            finally:
                if done is not None:
                    done.set()

        second_url = self.fake.url + "?explicit-source=2"
        with mock.patch.object(self.srv.source_state, "save", side_effect=delay_first):
            first = threading.Thread(target=connect, args=(self.fake.url,))
            second = threading.Thread(target=connect, args=(second_url, second_finished))
            first.start()
            try:
                self.assertTrue(first_save_entered.wait(2))
                second.start()
                # Without serialization, POST 2 completes here, then the old
                # delayed snapshot from POST 1 overwrites its saved endpoint.
                second_finished.wait(.3)
            finally:
                release_first_save.set()
                first.join(timeout=4)
                if second.ident is not None:
                    second.join(timeout=4)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual(len(results), 2, results)
        self.assertTrue(all(isinstance(result, tuple) and result[0] == 200 for result in results), results)
        saved = json.loads((self.state_dir / "mcp-sources.json").read_text())
        self.assertEqual({row["url"] for row in saved["sources"]}, {self.fake.url, second_url})
        self.assertEqual({row["url"] for row in self.request("/api/catalog")[1]["sources"]}, {self.fake.url, second_url})

    def test_default_state_path_uses_state_home_outside_checkout(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name}):
            location = Path(server.default_state_dir()).resolve()
        self.assertEqual(location, Path(self.temp.name).resolve() / "edgelab")
        self.assertFalse(any((parent / ".git").exists() for parent in location.parents))


if __name__ == "__main__":
    unittest.main()
