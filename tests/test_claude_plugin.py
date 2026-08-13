import json
import re
import shutil
import subprocess
import sys
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
    def test_codex_repo_marketplace_targets_bundled_plugin(self):
        marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))

        self.assertEqual(marketplace["name"], "vectorworks-mcp")
        self.assertEqual(marketplace["interface"], {"displayName": "Vectorworks MCP"})
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "vectorworks")
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/vectorworks"})
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Productivity")
        self.assertEqual((ROOT / entry["source"]["path"]).resolve(), PLUGIN.resolve())

    def test_client_manifests_declare_separate_mcp_configs(self):
        claude_manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        codex_manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((PLUGIN / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        root_marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

        self.assertEqual(claude_manifest["name"], "vectorworks")
        self.assertEqual(claude_manifest["version"], "0.5.0")
        self.assertEqual(claude_manifest["mcpServers"], "./.claude-plugin/mcp.json")
        self.assertIn("vectorworks_repo", claude_manifest["userConfig"])
        self.assertEqual(codex_manifest["name"], "vectorworks")
        self.assertEqual(codex_manifest["version"], "0.5.0")
        self.assertEqual(codex_manifest["skills"], "./skills/")
        self.assertEqual(codex_manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(codex_manifest["interface"]["displayName"], "Vectorworks MCP")
        self.assertEqual(codex_manifest["interface"]["category"], "Productivity")
        self.assertEqual(codex_manifest["interface"]["capabilities"], ["Interactive", "Write"])
        self.assertLessEqual(len(codex_manifest["interface"]["defaultPrompt"]), 3)
        self.assertEqual(marketplace["name"], "vectorworks-claude-plugin")
        self.assertEqual(marketplace["plugins"][0]["name"], "vectorworks")
        self.assertEqual(root_marketplace["name"], "vectorworks-mcp")
        self.assertEqual(root_marketplace["plugins"][0]["name"], "vectorworks")
        self.assertEqual(root_marketplace["plugins"][0]["source"], "./plugins/vectorworks")

    def test_client_mcp_configs_use_native_only_wrappers(self):
        codex_config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        claude_config = json.loads((PLUGIN / ".claude-plugin" / "mcp.json").read_text(encoding="utf-8"))
        codex_server = codex_config["mcpServers"]["vectorworks"]
        claude_server = claude_config["mcpServers"]["vectorworks"]

        for server in (codex_server, claude_server):
            self.assertEqual(server["type"], "stdio")
            self.assertEqual(server["command"], "powershell.exe")
            self.assertIn("scripts/run-vectorworks-mcp.ps1", "/".join(server["args"]).replace("\\", "/"))
            self.assertEqual(server["env"]["VW_MCP_HOST"], "127.0.0.1")
            self.assertEqual(server["env"]["VW_MCP_PORT"], "9877")
            self.assertEqual(server["env"]["VW_MCP_PREFLIGHT_CACHE_MS"], "5000")
            self.assertEqual(server["env"]["VW_MCP_TOOL_PROFILE"], "fast-native")
            serialized = json.dumps(server)
            self.assertNotIn("EnablePythonDialogFallback", serialized)
            self.assertNotIn("allow-python-fallback", serialized)

        self.assertIn("${PLUGIN_ROOT}/scripts/run-vectorworks-mcp.ps1", codex_server["args"])
        self.assertNotIn("VW_MCP_REPO", codex_server["env"])
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/run-vectorworks-mcp.ps1", claude_server["args"])
        self.assertEqual(claude_server["env"]["VW_MCP_REPO"], "${user_config.vectorworks_repo}")

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
            "bin/vectorworksctl",
            "bin/vectorworksctl.cmd",
            "bin/vectorworksctl.ps1",
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
            "scripts/invoke-native-bridge-next.ps1",
            "scripts/check-companion-contract.ps1",
            "scripts/bootstrap-native-bridge.ps1",
            "scripts/prepare-native-bridge-source.ps1",
            "scripts/build-native-bridge.ps1",
            "scripts/wire-native-bridge-project.ps1",
            "scripts/smoke-native-bridge.ps1",
            "scripts/start-vectorworks-native-smoke.ps1",
        ):
            self.assertTrue((PLUGIN / relative_path).exists(), relative_path)

        helper = (PLUGIN / "bin" / "vectorworksctl").read_text(encoding="utf-8")
        self.assertIn("setup-runtime", helper)
        self.assertIn("agent-install", helper)
        self.assertIn("Vectorworks MCP plugin", helper)
        self.assertIn("native-next", helper)
        self.assertIn("listener_doctor", helper)
        self.assertIn("native_plan", helper)
        self.assertIn("user_message", helper)
        self.assertIn("native_summary", helper)
        self.assertIn("setup_complete", helper)
        self.assertIn("install_complete", helper)
        self.assertIn("usable_now", helper)
        self.assertIn("command_ok", helper)
        self.assertIn("requires_action", helper)
        self.assertIn("native_setup_complete", helper)
        self.assertIn("native_requires_action", helper)
        self.assertIn("allow_python_fallback", helper)
        self.assertIn("phase_zero_only", helper)
        self.assertIn("live_native_ready", helper)
        self.assertIn("NATIVE_PRODUCTION_REQUIRED_ACTIONS", helper)
        self.assertIn('"apply_operations"', helper)
        self.assertIn("mcp_config_path", helper)
        self.assertIn("runner_path", helper)
        self.assertIn("launcher_path", helper)
        self.assertIn("loader_path", helper)
        self.assertIn("next_user_step", helper)
        self.assertIn("vectorworksctl", (PLUGIN / "bin" / "vectorworksctl.ps1").read_text(encoding="utf-8"))

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
        self.assertIn("vectorworksctl doctor --json", diagnose)
        self.assertIn("native-next", diagnose)
        self.assertIn("unknown commit state", diagnose)
        self.assertIn("nextCommandSpec", diagnose)
        self.assertIn("vectorworksctl agent-install --json", setup)
        self.assertIn("--allow-python-fallback", setup)
        self.assertIn("nextCommandSpec", setup)
        self.assertIn("vectorworksctl ping", ping)
        self.assertIn("cad_api_safe", ping)
        self.assertIn("transport_only", ping)
        self.assertIn("transport_only=false", work)
        self.assertIn("native-next", work)
        self.assertIn("fast-native` profile is mandatory", work)
        self.assertIn("internal CAD preflight", work)
        self.assertIn("self-verifying tool response", work)
        self.assertIn("substitute independent lines", work)
        self.assertIn("administrator-only diagnostic surface", work)
        self.assertIn("never routes to a legacy, decomposed, batch, or modal fallback", work)
        self.assertIn("vw_execute_operations", work)
        self.assertIn("idempotency_key", work)
        self.assertIn("create-and-property-edit plan", work)
        self.assertIn("`create` and `set_properties`", work)

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
            "## Workflow Profiles",
            "Fast native (mandatory)",
            "preflight internally",
            "self-verifying",
            "manual administrator diagnostic only",
            "no legacy, decomposed, batch, or modal fallback",
            "`vw_execute_operations`",
            "`apply_operations`",
            "create-and-property-edit plan",
            '"type":"set_properties"',
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
            "scripts/invoke-native-bridge-next.ps1",
            "scripts/test-vectorworks-listener.ps1",
            "scripts/bootstrap-native-bridge.ps1",
            "scripts/prepare-native-bridge-source.ps1",
            "scripts/build-native-bridge.ps1",
            "scripts/wire-native-bridge-project.ps1",
            "scripts/smoke-native-bridge.ps1",
            "scripts/start-vectorworks-native-smoke.ps1",
        ):
            text = (PLUGIN / relative_path).read_text(encoding="utf-8")
            self.assertIn("-RequireContract", text, relative_path)
            self.assertIn("Resolve-VectorworksMcpCompanionRepo", text, relative_path)
            self.assertIn("RepoPath", text, relative_path)

        bootstrap = (PLUGIN / "scripts" / "bootstrap-vectorworks-mcp.ps1").read_text(encoding="utf-8")
        resolver = (PLUGIN / "scripts" / "resolve-vectorworks-mcp-repo.ps1").read_text(encoding="utf-8")
        contract = (PLUGIN / "scripts" / "check-companion-contract.ps1").read_text(encoding="utf-8")
        smoke = (PLUGIN / "scripts" / "smoke-native-bridge.ps1").read_text(encoding="utf-8")
        bundled_contract = (ROOT / "scripts" / "check-bundled-plugin-contract.ps1").read_text(encoding="utf-8")
        self.assertIn("check-companion-contract.ps1", bootstrap)
        self.assertIn("-RepoPath", bootstrap)
        self.assertIn("vw_load_listener_2024.py", bootstrap)
        self.assertIn("-LoaderPath", bootstrap)
        self.assertIn("copy-vectorworks-loader.ps1", bootstrap)
        self.assertIn("SkipClipboard", bootstrap)
        self.assertIn("[int]$MinimumContractVersion", resolver)
        self.assertIn("$MinimumContractVersion = 16", resolver)
        self.assertIn("requiredFeatures", resolver)
        for feature in (
            "native-phase4-apply-operations",
            "fast-native-tool-profile",
            "structured-mcp-results",
            "codex-plugin-package",
        ):
            self.assertIn(feature, resolver)
        self.assertIn("contractVersion >= 16", contract)
        self.assertIn("native-bridge-scaffold-copy", contract)
        self.assertIn("native-doctor-next-command", contract)
        self.assertIn("native-doctor-command-spec", contract)
        self.assertIn("native-bridge-project-wire", contract)
        self.assertIn("native-doctor-next-runner", contract)
        self.assertIn("native-runner-spec-validation", contract)
        self.assertIn("native-sdk-archive-reuse", contract)
        self.assertIn("native-vectorworks-auto-smoke", contract)
        self.assertIn("native-phase0-transport", contract)
        self.assertIn("native-phase1-cad-handlers", contract)
        self.assertIn("native-phase2-cad-handlers", contract)
        self.assertIn("native-phase2-set-property", contract)
        self.assertIn("native-phase2-manage-classes", contract)
        self.assertIn("local-auth-token-required", contract)
        self.assertIn("client-neutral-project-mcp", contract)
        self.assertIn("wire-native-project", contract)
        self.assertIn("nextCommandReason", contract)
        self.assertIn("nextCommandSpec", contract)
        self.assertIn("status=plan_only", contract)
        self.assertIn("missingAllowFlags", contract)
        self.assertIn("validationErrors", contract)
        self.assertIn("safetyBlocks", contract)
        self.assertIn("probe -WorktreeRoot", contract)
        self.assertIn("Vectorworks SDK With Spaces", contract)
        self.assertIn("SDK Examples With Spaces", contract)
        self.assertIn("-Configuration Release", contract)
        self.assertIn("workingDirectory must be the companion repo root", contract)
        self.assertIn("test-native-bridge-scaffold.ps1", contract)
        self.assertIn("LoaderPath", contract)
        self.assertIn("CopyLoaderToClipboard", contract)
        self.assertIn("MaxPingMilliseconds", smoke)
        self.assertIn("MaxReadMilliseconds", smoke)
        self.assertIn("[ValidateRange(0, 2)]", smoke)
        for text in (contract, bundled_contract):
            self.assertIn(".venv\\Scripts\\python.exe", text)
            self.assertIn("Run scripts\\bootstrap-agent.ps1 first or install Python 3", text)

    def test_connector_ci_checks_bundled_plugin_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
        bundled_contract = (ROOT / "scripts" / "check-bundled-plugin-contract.ps1").read_text(encoding="utf-8")

        self.assertIn("check-bundled-plugin-contract.ps1", workflow)
        self.assertIn("Bundled plugin contract", workflow)
        self.assertIn("Get-Command claude", bundled_contract)
        self.assertIn("plugin validate", bundled_contract)
        self.assertIn("skipping official Claude bundled-plugin validation", bundled_contract)

    def test_server_tool_safety_imports_without_optional_host_dependencies(self):
        code = f"""
import importlib.abc
import json
import sys

class BlockOptionalHostDeps(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {{"fastmcp", "pydantic"}}:
            raise ModuleNotFoundError("blocked optional host dependency: " + fullname, name=fullname)
        return None

sys.meta_path.insert(0, BlockOptionalHostDeps())
sys.path.insert(0, {json.dumps(str(ROOT))})
import server
print(json.dumps({{"tool_count": len(server.TOOL_SAFETY), "vw_ping_read_only": server.TOOL_SAFETY["vw_ping"]["readOnlyHint"]}}, sort_keys=True))
"""

        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["tool_count"], 25)
        self.assertTrue(payload["vw_ping_read_only"])

    def test_readme_uses_canonical_repo_override_env_var(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("$env:VW_MCP_REPO", readme)
        self.assertIn("VECTORWORKS_MCP_REPO` remains supported as a backward-compatible alias", readme)
        self.assertNotIn("$env:VECTORWORKS_MCP_REPO", readme)


if __name__ == "__main__":
    unittest.main()
