from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "server"))

from filesystem_ops import YurichError, read_file, save_file  # noqa: E402
from search_ops import search_project  # noqa: E402
from state_ops import load_state, save_state  # noqa: E402


class FilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "hello.py").write_text(
            "before\nprint('needle')\nafter\n", encoding="utf-8", newline=""
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_search_read_and_save(self) -> None:
        found = search_project({"root": str(self.root), "query": "needle"})
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["results"][0]["path"], "src/hello.py")
        opened = read_file({"root": str(self.root), "path": "src/hello.py"})
        saved = save_file({
            "root": str(self.root),
            "path": "src/hello.py",
            "content": opened["content"].replace("needle", "changed"),
            "expectedVersion": opened["metadata"],
        })
        self.assertEqual(saved["status"], "success")
        self.assertIn("changed", (self.root / "src" / "hello.py").read_text(encoding="utf-8"))

    def test_conflict_is_not_overwritten(self) -> None:
        opened = read_file({"root": str(self.root), "path": "src/hello.py"})
        (self.root / "src" / "hello.py").write_text("external\n", encoding="utf-8")
        saved = save_file({
            "root": str(self.root),
            "path": "src/hello.py",
            "content": "my edit\n",
            "expectedVersion": opened["metadata"],
        })
        self.assertEqual(saved["status"], "conflict")
        self.assertEqual((self.root / "src" / "hello.py").read_text(encoding="utf-8"), "external\n")

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(YurichError):
            read_file({"root": str(self.root), "path": "../outside.txt"})


class McpTests(unittest.TestCase):
    def test_server_lists_tools_and_ui(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://yurich/main-v4.html"}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ui://yurich/main-v3.html"}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "ui://yurich/main-v2.html"}},
            {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "ui://yurich/main-v1.html"}},
            {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": "ui://yurich/missing.html"}},
        ]
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "server" / "yurich_mcp.py")],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        names = {tool["name"] for tool in replies[1]["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "open_yurich", "validate_root", "choose_folder", "search_project",
                "load_state", "save_state", "read_file", "save_file",
            },
        )
        content = replies[2]["result"]["contents"][0]
        self.assertEqual(content["uri"], "ui://yurich/main-v4.html")
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertIn('method:"tools/call"', content["text"])
        self.assertIn('requestDisplayMode("fullscreen")', content["text"])
        self.assertIn('id="settings"', content["text"])
        self.assertIn('id="fontSize"', content["text"])
        self.assertNotIn('requestDisplayMode("pip")', content["text"])
        compatibility_content = replies[3]["result"]["contents"][0]
        self.assertEqual(compatibility_content["uri"], "ui://yurich/main-v3.html")
        self.assertEqual(compatibility_content["text"], content["text"])
        older_content = replies[4]["result"]["contents"][0]
        self.assertEqual(older_content["uri"], "ui://yurich/main-v2.html")
        self.assertEqual(older_content["text"], content["text"])
        legacy_content = replies[5]["result"]["contents"][0]
        self.assertEqual(legacy_content["uri"], "ui://yurich/main-v1.html")
        self.assertEqual(legacy_content["text"], content["text"])
        self.assertNotIn("result", replies[6])
        self.assertEqual(replies[6]["error"]["code"], -32000)


class StateTests(unittest.TestCase):
    def test_preferences_survive_a_new_ui_instance(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            previous = os.environ.get("YURICH_DATA_DIR")
            os.environ["YURICH_DATA_DIR"] = data_dir
            try:
                saved = {
                    "root": r"C:\Projects\Video",
                    "query": ".hero-text",
                    "caseSensitive": True,
                    "wholeWord": False,
                    "regex": False,
                    "include": "*.css",
                    "exclude": "*.min.css",
                    "fontSize": 17,
                }
                self.assertEqual(save_state({"state": saved})["status"], "success")
                self.assertEqual(load_state({})["state"], saved)
            finally:
                if previous is None:
                    os.environ.pop("YURICH_DATA_DIR", None)
                else:
                    os.environ["YURICH_DATA_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
