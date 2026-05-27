import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AgentReadinessTests(unittest.TestCase):
    def test_project_mcp_config_uses_bootstrap_runner(self):
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["vectorworks"]

        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "powershell.exe")
        self.assertIn("scripts/run-mcp-server.ps1", "/".join(server["args"]).replace("\\", "/"))
        self.assertEqual(server["env"]["VW_MCP_HOST"], "127.0.0.1")
        self.assertEqual(server["env"]["VW_MCP_PORT"], "9877")

    def test_agent_instruction_files_exist(self):
        self.assertTrue((ROOT / "AGENTS.md").exists())
        self.assertTrue((ROOT / "CLAUDE.md").exists())
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_bootstrap_scripts_exist(self):
        for relative_path in (
            "scripts/bootstrap-agent.ps1",
            "scripts/bootstrap-claude-code.ps1",
            "scripts/bootstrap-native-bridge.ps1",
            "scripts/check-native-bridge-prereqs.ps1",
            "scripts/doctor-vectorworks-mcp.ps1",
            "scripts/register-claude-code.ps1",
            "scripts/run-mcp-server.ps1",
            "scripts/verify-no-vectorworks.ps1",
        ):
            self.assertTrue((ROOT / relative_path).exists(), relative_path)

    def test_generated_launcher_uses_dialog_agent_session_listener(self):
        register_script = (ROOT / "scripts/register-claude-code.ps1").read_text(encoding="utf-8")
        self.assertIn('os.environ["VW_MCP_MODE"] = "dialog"', register_script)
        self.assertIn('os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"', register_script)

        launcher_path = ROOT / "vw_start_listener_2024.py"
        if launcher_path.exists():
            launcher_text = launcher_path.read_text(encoding="utf-8")
            self.assertIn('os.environ["VW_MCP_MODE"] = "dialog"', launcher_text)
            self.assertIn('os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"', launcher_text)

    def test_register_script_generates_dialog_agent_session_launcher(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the Windows launcher generator")
        if not os.environ.get("USERPROFILE"):
            self.skipTest("USERPROFILE is required for the generated Windows launcher")

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher_path = Path(temp_dir) / "vw_start_listener_2024.py"
            subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/register-claude-code.ps1"),
                    "-SkipInstall",
                    "-NoClaudeConfig",
                    "-LauncherPath",
                    str(launcher_path),
                    "-Port",
                    "19877",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            launcher_text = launcher_path.read_text(encoding="utf-8")
            self.assertIn('os.environ["VW_MCP_HOST"] = "127.0.0.1"', launcher_text)
            self.assertIn('os.environ["VW_MCP_PORT"] = "19877"', launcher_text)
            self.assertIn('os.environ["VW_MCP_MODE"] = "dialog"', launcher_text)
            self.assertIn('os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"', launcher_text)

    def test_native_bridge_scaffold_is_explicitly_not_default(self):
        expected_files = (
            "native_bridge/README.md",
            "native_bridge/PROTOCOL.md",
            "native_bridge/ACCEPTANCE.md",
            "native_bridge/src/README.md",
        )
        for relative_path in expected_files:
            self.assertTrue((ROOT / relative_path).exists(), relative_path)

        native_readme = (ROOT / "native_bridge/README.md").read_text(encoding="utf-8")
        self.assertIn("native Vectorworks SDK plug-in bridge", native_readme)
        self.assertIn("marshaled back onto the Vectorworks main/plugin event context", native_readme)
        self.assertIn("not compiled or installed by default", (ROOT / "README.md").read_text(encoding="utf-8"))

        protocol = (ROOT / "native_bridge/PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("4-byte big-endian", protocol)
        self.assertIn("must not call", protocol)
        self.assertIn("Vectorworks document APIs directly", protocol)

    def test_native_bridge_scripts_point_to_official_sdk_and_ignore_downloads(self):
        checker = (ROOT / "scripts/check-native-bridge-prereqs.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts/bootstrap-native-bridge.ps1").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("https://www.vectorworks.net/en-US/support/custom/sdk/sdkdown", checker)
        self.assertIn("https://github.com/VectorworksDeveloper/SDKExamples", checker)
        self.assertIn("2024-NNA-eng-win-SDK", checker)
        self.assertIn("17.6.3", checker)
        self.assertIn("v143", checker)
        self.assertIn("Invoke-WebRequest", bootstrap)
        self.assertIn("-DownloadSdk", bootstrap)
        self.assertIn(".cache/", gitignore)
        self.assertIn("third_party/", gitignore)

    def test_doctor_script_reports_next_actions_and_cad_safety(self):
        doctor = (ROOT / "scripts/doctor-vectorworks-mcp.ps1").read_text(encoding="utf-8")

        self.assertIn("overall", doctor)
        self.assertIn("nextActions", doctor)
        self.assertIn("cad_api_safe", doctor)
        self.assertIn("transport_only", doctor)
        self.assertIn("check-native-bridge-prereqs.ps1", doctor)


if __name__ == "__main__":
    unittest.main()
