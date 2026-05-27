import json
import re
import shutil
import subprocess
import tempfile
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
        marketplace = json.loads((PLUGIN / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "vectorworks")
        self.assertEqual(manifest["version"], "0.3.0")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertIn("vectorworks_repo", manifest["userConfig"])
        self.assertEqual(marketplace["name"], "vectorworks-claude-plugin")
        self.assertEqual(marketplace["plugins"][0]["name"], "vectorworks")

    def test_plugin_mcp_config_uses_wrapper(self):
        config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["vectorworks"]

        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "powershell.exe")
        self.assertIn("scripts/run-vectorworks-mcp.ps1", "/".join(server["args"]).replace("\\", "/"))
        self.assertEqual(server["env"]["VW_MCP_HOST"], "127.0.0.1")
        self.assertEqual(server["env"]["VW_MCP_PORT"], "9877")
        self.assertEqual(server["env"]["VW_MCP_PREFLIGHT_CACHE_MS"], "750")

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
            "scripts/resolve-companion-repo.ps1",
            "scripts/resolve-vectorworks-mcp-repo.ps1",
            "scripts/run-vectorworks-mcp.ps1",
            "scripts/bootstrap-vectorworks-mcp.ps1",
            "scripts/copy-vectorworks-loader.ps1",
            "scripts/copy-native-bridge-scaffold.ps1",
            "scripts/test-vectorworks-listener.ps1",
            "scripts/diagnose-vectorworks-mcp.ps1",
            "scripts/doctor-vectorworks-mcp.ps1",
            "scripts/doctor-native-bridge.ps1",
            "scripts/check-companion-contract.ps1",
            "scripts/bootstrap-native-bridge.ps1",
            "scripts/prepare-native-bridge-source.ps1",
            "scripts/build-native-bridge.ps1",
            "scripts/smoke-native-bridge.ps1",
        ):
            self.assertTrue((PLUGIN / relative_path).exists(), relative_path)

        self.assertTrue((ROOT / "scripts" / "check-bundled-plugin-contract.ps1").exists())

    def test_plugin_diagnose_reports_identity_and_loader_metadata(self):
        diagnose = (PLUGIN / "scripts" / "diagnose-vectorworks-mcp.ps1").read_text(encoding="utf-8")
        doctor = (PLUGIN / "scripts" / "doctor-vectorworks-mcp.ps1").read_text(encoding="utf-8")
        diagnose_skill = (PLUGIN / "skills" / "diagnose" / "SKILL.md").read_text(encoding="utf-8")

        for text in (
            "Plugin root:",
            "Plugin version:",
            "Plugin marketplace:",
            "Connector contract:",
            "Connector git:",
            "Generated loader metadata:",
            "VW_MCP_LOADER_METADATA",
            "contractVersion",
            "requiredFeatures",
            "generatedAtUtc",
        ):
            self.assertIn(text, diagnose)

        for text in (
            "Plugin root:",
            "Plugin version:",
            "Plugin marketplace:",
            "Connector contract:",
            "Connector git:",
            "-RequireContract",
        ):
            self.assertIn(text, doctor)

        for text in ("Plugin version", "Connector git", "Generated loader metadata: stale"):
            self.assertIn(text, diagnose_skill)

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
                "-RequireContract",
            ],
            cwd=str(ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(Path(result.stdout.strip()).resolve(), ROOT.resolve())

    def test_plugin_resolver_rejects_stale_repo_when_contract_required(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the plugin resolver")

        with tempfile.TemporaryDirectory() as temp_dir:
            stale = Path(temp_dir) / "vectorworks-mcp"
            (stale / "scripts").mkdir(parents=True)
            (stale / "server.py").write_text("", encoding="utf-8")
            (stale / "vw_listener.py").write_text("", encoding="utf-8")
            (stale / "scripts" / "run-mcp-server.ps1").write_text("", encoding="utf-8")

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
                    str(stale),
                    "-RequireContract",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("companion contract", result.stderr + result.stdout)
        self.assertIn(".vectorworks-mcp-contract.json", result.stderr + result.stdout)

    def test_plugin_tool_map_covers_server_tools(self):
        self.assertEqual(_tool_map_names(), _server_tool_names())

    def test_plugin_skills_mention_host_side_blocked_guard(self):
        work = (PLUGIN / "skills" / "work" / "SKILL.md").read_text(encoding="utf-8")
        diagnose = (PLUGIN / "skills" / "diagnose" / "SKILL.md").read_text(encoding="utf-8")
        setup = (PLUGIN / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
        ping = (PLUGIN / "skills" / "ping" / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("blocked: true", work)
        self.assertIn("vw_tool_safety", work)
        self.assertIn("unknown commit state", work)
        self.assertIn("blocked: true", diagnose)
        self.assertIn("host-side safety guard", diagnose)
        self.assertIn("unknown commit state", diagnose)
        self.assertIn("vw_load_listener_2024.py", setup)
        self.assertIn("vw_load_listener_2024.py", ping)
        self.assertIn("bridge_kind=python_dialog_agent_session", setup)
        self.assertIn("bridge_kind=python_dialog_agent_session", ping)
        self.assertIn("transport-only ping is not enough", work)

    def test_plugin_tool_map_documents_safety_metadata_and_mixed_actions(self):
        tool_map = (PLUGIN / "references" / "tool-map.md").read_text(encoding="utf-8")

        for text in (
            "## Safety Metadata",
            "requires_cad_preflight",
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
            "## Mixed Tool Actions",
            "`vw_selection.get`",
            "`vw_selection.delete`",
            "`vw_manage_classes.list`",
            "`vw_manage_classes.delete`",
            "`vw_worksheet.read_range`",
            "`vw_worksheet.write`",
            "`vw_symbol.list`",
            "`vw_symbol.insert`",
        ):
            self.assertIn(text, tool_map)

    def test_bundled_wrappers_require_current_connector_contract(self):
        for relative_path in (
            "scripts/run-vectorworks-mcp.ps1",
            "scripts/bootstrap-vectorworks-mcp.ps1",
            "scripts/copy-vectorworks-loader.ps1",
            "scripts/copy-native-bridge-scaffold.ps1",
            "scripts/diagnose-vectorworks-mcp.ps1",
            "scripts/doctor-vectorworks-mcp.ps1",
            "scripts/doctor-native-bridge.ps1",
            "scripts/test-vectorworks-listener.ps1",
            "scripts/bootstrap-native-bridge.ps1",
            "scripts/prepare-native-bridge-source.ps1",
            "scripts/build-native-bridge.ps1",
            "scripts/smoke-native-bridge.ps1",
        ):
            text = (PLUGIN / relative_path).read_text(encoding="utf-8")
            self.assertIn("-RequireContract", text, relative_path)
            self.assertIn("Resolve-VectorworksMcpCompanionRepo", text, relative_path)

        bootstrap = (PLUGIN / "scripts" / "bootstrap-vectorworks-mcp.ps1").read_text(encoding="utf-8")
        resolver = (PLUGIN / "scripts" / "resolve-vectorworks-mcp-repo.ps1").read_text(encoding="utf-8")
        contract = (PLUGIN / "scripts" / "check-companion-contract.ps1").read_text(encoding="utf-8")
        smoke = (PLUGIN / "scripts" / "smoke-native-bridge.ps1").read_text(encoding="utf-8")
        self.assertIn("check-companion-contract.ps1", bootstrap)
        self.assertIn("-RepoPath", bootstrap)
        self.assertIn("vw_load_listener_2024.py", bootstrap)
        self.assertIn("-LoaderPath", bootstrap)
        self.assertIn("copy-vectorworks-loader.ps1", bootstrap)
        self.assertIn("SkipClipboard", bootstrap)
        self.assertIn("[int]$MinimumContractVersion = 5", resolver)
        self.assertIn("requiredFeatures", resolver)
        self.assertIn("contractVersion >= 5", contract)
        self.assertIn("native-bridge-scaffold-copy", contract)
        self.assertIn("test-native-bridge-scaffold.ps1", contract)
        self.assertIn("LoaderPath", contract)
        self.assertIn("CopyLoaderToClipboard", contract)
        self.assertIn("MaxPingMilliseconds", smoke)
        self.assertIn("MaxReadMilliseconds", smoke)

    def test_connector_ci_checks_bundled_plugin_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
        bundled_contract = (ROOT / "scripts" / "check-bundled-plugin-contract.ps1").read_text(encoding="utf-8")

        self.assertIn("check-bundled-plugin-contract.ps1", workflow)
        self.assertIn("Bundled plugin contract", workflow)
        self.assertIn("Get-Command claude", bundled_contract)
        self.assertIn("plugin validate", bundled_contract)
        self.assertIn("skipping official Claude bundled-plugin validation", bundled_contract)

    def test_readme_uses_canonical_repo_override_env_var(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("$env:VW_MCP_REPO", readme)
        self.assertIn("VECTORWORKS_MCP_REPO` remains supported as a backward-compatible alias", readme)
        self.assertNotIn("$env:VECTORWORKS_MCP_REPO", readme)


if __name__ == "__main__":
    unittest.main()
