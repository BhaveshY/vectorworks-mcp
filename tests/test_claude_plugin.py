import json
import re
import shutil
import subprocess
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "vectorworks"


def _server_tool_names():
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    return set(re.findall(r"def (vw_[a-zA-Z0-9_]+)\(", text))


def _tool_map_names():
    text = (PLUGIN / "references" / "tool-map.md").read_text(encoding="utf-8")
    return set(re.findall(r"`(vw_[a-zA-Z0-9_]+)`", text))


class ClaudePluginTests(unittest.TestCase):
    def test_plugin_manifest_declares_mcp_config(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "vectorworks")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertIn("vectorworks_repo", manifest["userConfig"])

    def test_plugin_mcp_config_uses_wrapper(self):
        config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["vectorworks"]

        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "powershell.exe")
        self.assertIn("scripts/run-vectorworks-mcp.ps1", "/".join(server["args"]).replace("\\", "/"))
        self.assertEqual(server["env"]["VW_MCP_HOST"], "127.0.0.1")
        self.assertEqual(server["env"]["VW_MCP_PORT"], "9877")

    def test_plugin_skills_exist(self):
        for name in ("setup", "ping", "diagnose", "work"):
            skill = PLUGIN / "skills" / name / "SKILL.md"
            text = skill.read_text(encoding="utf-8")

            self.assertTrue(skill.exists(), name)
            self.assertTrue(text.startswith("---\n"), name)
            self.assertIn(f"name: {name}", text)
            self.assertIn("description:", text)

    def test_plugin_scripts_exist(self):
        for relative_path in (
            "scripts/resolve-vectorworks-mcp-repo.ps1",
            "scripts/run-vectorworks-mcp.ps1",
            "scripts/bootstrap-vectorworks-mcp.ps1",
            "scripts/test-vectorworks-listener.ps1",
            "scripts/diagnose-vectorworks-mcp.ps1",
            "scripts/doctor-vectorworks-mcp.ps1",
        ):
            self.assertTrue((PLUGIN / relative_path).exists(), relative_path)

    def test_plugin_resolver_finds_this_repo(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the plugin resolver")

        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PLUGIN / "scripts" / "resolve-vectorworks-mcp-repo.ps1"),
                "-RepoPath",
                str(ROOT),
            ],
            cwd=str(ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(Path(result.stdout.strip()).resolve(), ROOT.resolve())

    def test_plugin_tool_map_covers_server_tools(self):
        self.assertEqual(_tool_map_names(), _server_tool_names())

    def test_plugin_skills_mention_host_side_blocked_guard(self):
        work = (PLUGIN / "skills" / "work" / "SKILL.md").read_text(encoding="utf-8")
        diagnose = (PLUGIN / "skills" / "diagnose" / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("blocked: true", work)
        self.assertIn("blocked: true", diagnose)
        self.assertIn("host-side safety guard", diagnose)


if __name__ == "__main__":
    unittest.main()
