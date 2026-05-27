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
            "scripts/build-native-bridge.ps1",
            "scripts/check-native-bridge-prereqs.ps1",
            "scripts/doctor-vectorworks-mcp.ps1",
            "scripts/prepare-native-bridge-source.ps1",
            "scripts/register-claude-code.ps1",
            "scripts/run-mcp-server.ps1",
            "scripts/smoke-native-bridge.ps1",
            "scripts/verify-no-vectorworks.ps1",
            ".github/workflows/verify.yml",
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
            "native_bridge/HANDLER_MATRIX.md",
            "native_bridge/SDK_REQUIREMENTS.json",
            "native_bridge/mock/mock_bridge.py",
            "native_bridge/smoke.py",
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

        matrix = (ROOT / "native_bridge/HANDLER_MATRIX.md").read_text(encoding="utf-8")
        self.assertIn("Native phase", matrix)
        self.assertIn("main/plugin event context", matrix)

    def test_native_bridge_scripts_point_to_official_sdk_and_ignore_downloads(self):
        requirements = json.loads((ROOT / "native_bridge/SDK_REQUIREMENTS.json").read_text(encoding="utf-8"))
        checker = (ROOT / "scripts/check-native-bridge-prereqs.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts/bootstrap-native-bridge.ps1").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertEqual(requirements["officialSdkPage"], "https://www.vectorworks.net/en-US/support/custom/sdk/sdkdown")
        self.assertEqual(requirements["officialSdkExamples"], "https://github.com/VectorworksDeveloper/SDKExamples")
        self.assertIn("2024-NNA-eng-win-SDK", requirements["versions"]["2024"]["winSdkDownload"])
        self.assertEqual(requirements["versions"]["2024"]["visualStudioMinimumVersion"], "17.6.3")
        self.assertEqual(requirements["versions"]["2024"]["toolset"], "v143")
        self.assertIn("SDK_REQUIREMENTS.json", checker)
        self.assertIn("SDK_REQUIREMENTS.json", bootstrap)
        self.assertIn("Invoke-WebRequest", bootstrap)
        self.assertIn("-DownloadSdk", bootstrap)
        self.assertIn(".cache/", gitignore)
        self.assertIn("third_party/", gitignore)

        for version, data in requirements["versions"].items():
            self.assertRegex(version, r"^20\d{2}$")
            self.assertTrue(data["winSdkDownload"].startswith("https://"))
            self.assertRegex(data["visualStudioMinimumVersion"], r"^\d+\.\d+")
            self.assertRegex(data["toolset"], r"^v\d+")

        self.assertNotIn("[ValidateSet(\"2024\", \"2025\", \"2026\")]", checker)
        self.assertNotIn("[ValidateSet(\"2024\", \"2025\", \"2026\")]", bootstrap)
        self.assertNotIn("2025-NNA-eng-win-SDK.zip", checker)
        self.assertNotIn("2026-NNA-eng-win-SDK.zip", checker)
        self.assertIn("Supported versions", checker)
        self.assertIn("Supported versions", bootstrap)

    def test_native_bridge_prepare_and_build_scripts_use_official_sdk_examples(self):
        prepare = (ROOT / "scripts/prepare-native-bridge-source.ps1").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build-native-bridge.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts/bootstrap-native-bridge.ps1").read_text(encoding="utf-8")
        checker = (ROOT / "scripts/check-native-bridge-prereqs.ps1").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("officialSdkExamples", prepare)
        self.assertIn("Examples$VectorworksVersion\\ObjectExample", prepare)
        self.assertIn("VectorworksSDK\\SDK$Version\\SDKLib", prepare)
        self.assertIn("git clone --depth 1", prepare)
        self.assertIn("native_bridge\\worktree", prepare)
        self.assertIn("SDKExamples", prepare)
        self.assertIn("VectorworksMCPBridge", prepare)

        self.assertIn("check-native-bridge-prereqs.ps1", build)
        self.assertIn("MSBuild", build)
        self.assertIn("*$VectorworksVersion.sln", build)
        self.assertIn("/p:Platform=x64", build)
        self.assertIn("Microsoft.VisualStudio.2022.BuildTools", bootstrap)
        self.assertIn("Microsoft.VisualStudio.Workload.VCTools", bootstrap)
        self.assertIn("[switch]$PrepareSource", bootstrap)
        self.assertIn("[switch]$Build", bootstrap)
        self.assertIn("third_party\\VectorworksSDKExamples\\VectorworksSDK\\SDK$Version", checker)
        self.assertIn("native_bridge/worktree/", gitignore)

    def test_prepare_native_bridge_source_preserves_sdk_example_layout(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise native source preparation")

        worktree = ROOT / "native_bridge" / "worktree"
        with tempfile.TemporaryDirectory() as temp_dir:
            examples = Path(temp_dir) / "SDKExamples"
            source = examples / "Examples2024" / "ObjectExample"
            (source / "Source").mkdir(parents=True)
            (examples / "VectorworksSDK" / "SDK2024" / "SDKLib").mkdir(parents=True)
            (examples / "ThirdPartySource" / "libcurl").mkdir(parents=True)
            (source / "ObjectExample2024.sln").write_text("fake solution\n", encoding="utf-8")

            try:
                subprocess.run(
                    [
                        powershell,
                        "-NoLogo",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ROOT / "scripts/prepare-native-bridge-source.ps1"),
                        "-SdkExamplesDir",
                        str(examples),
                        "-Force",
                    ],
                    cwd=str(ROOT),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                root = worktree / "SDKExamples"
                bridge = root / "Examples2024" / "VectorworksMCPBridge"
                self.assertTrue((bridge / "ObjectExample2024.sln").exists())
                self.assertTrue((bridge / "VECTORWORKS_MCP_BRIDGE_NOTES.md").exists())
                self.assertTrue((root / "VectorworksSDK" / "SDK2024" / "SDKLib").exists())
                self.assertTrue((root / "ThirdPartySource" / "libcurl").exists())
            finally:
                if worktree.exists():
                    shutil.rmtree(worktree)

    def test_powershell_scripts_parse(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to parse scripts")

        scripts = sorted((ROOT / "scripts").glob("*.ps1"))
        self.assertGreater(len(scripts), 0)
        for script in scripts:
            with self.subTest(script=script.name):
                script_literal = str(script).replace("'", "''")
                parser = (
                    "$errors=$null; "
                    "[System.Management.Automation.Language.Parser]::ParseFile("
                    f"'{script_literal}', [ref]$null, [ref]$errors) > $null; "
                    "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
                )
                subprocess.run(
                    [
                        powershell,
                        "-NoLogo",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        parser,
                    ],
                    cwd=str(ROOT),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

    def test_doctor_script_reports_next_actions_and_cad_safety(self):
        doctor = (ROOT / "scripts/doctor-vectorworks-mcp.ps1").read_text(encoding="utf-8")

        self.assertIn("overall", doctor)
        self.assertIn("nextActions", doctor)
        self.assertIn("cad_api_safe", doctor)
        self.assertIn("transport_only", doctor)
        self.assertIn("check-native-bridge-prereqs.ps1", doctor)

    def test_native_smoke_script_is_documented(self):
        smoke_script = (ROOT / "scripts/smoke-native-bridge.ps1").read_text(encoding="utf-8")
        acceptance = (ROOT / "native_bridge/ACCEPTANCE.md").read_text(encoding="utf-8")
        native_readme = (ROOT / "native_bridge/README.md").read_text(encoding="utf-8")

        self.assertIn("native_bridge\\smoke.py", smoke_script)
        self.assertIn("--ping-count", smoke_script)
        self.assertIn("--read-count", smoke_script)
        self.assertIn("smoke-native-bridge.ps1", acceptance)
        self.assertIn("smoke-native-bridge.ps1", native_readme)

    def test_native_prereq_checker_reports_supported_versions_for_unknown_version(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the native prerequisite checker")

        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts/check-native-bridge-prereqs.ps1"),
                "-VectorworksVersion",
                "2099",
                "-Advisory",
                "-Json",
            ],
            cwd=str(ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        report = json.loads(result.stdout)
        self.assertFalse(report["ready"])
        self.assertIn("Supported versions", report["error"])
        self.assertIn("2024", report["supportedVersions"])


if __name__ == "__main__":
    unittest.main()
