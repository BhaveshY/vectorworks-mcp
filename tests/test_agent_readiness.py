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
        self.assertEqual(server["env"]["VW_MCP_PREFLIGHT_CACHE_MS"], "750")
        self.assertNotIn(":-", (ROOT / ".mcp.json").read_text(encoding="utf-8"))

    def test_agent_instruction_files_exist(self):
        self.assertTrue((ROOT / "AGENTS.md").exists())
        self.assertTrue((ROOT / "CLAUDE.md").exists())
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_companion_contract_marker_exists(self):
        marker = json.loads((ROOT / ".vectorworks-mcp-contract.json").read_text(encoding="utf-8"))

        self.assertEqual(marker["name"], "vectorworks-mcp")
        self.assertGreaterEqual(marker["contractVersion"], 5)
        for feature in (
            "stable-loader",
            "loader-clipboard-copy",
            "native-bridge-scaffold",
            "native-bridge-scaffold-copy",
            "native-doctor-next-command",
        ):
            self.assertIn(feature, marker["requiredFeatures"])

    def test_bootstrap_scripts_exist(self):
        for relative_path in (
            "scripts/bootstrap-agent.ps1",
            "scripts/bootstrap-claude-code.ps1",
            "scripts/bootstrap-native-bridge.ps1",
            "scripts/build-native-bridge.ps1",
            "scripts/check-bundled-plugin-contract.ps1",
            "scripts/check-native-bridge-prereqs.ps1",
            "scripts/copy-vectorworks-loader.ps1",
            "scripts/copy-native-bridge-scaffold.ps1",
            "scripts/doctor-vectorworks-mcp.ps1",
            "scripts/doctor-native-bridge.ps1",
            "scripts/prepare-native-bridge-source.ps1",
            "scripts/register-claude-code.ps1",
            "scripts/run-mcp-server.ps1",
            "scripts/smoke-native-bridge.ps1",
            "scripts/test-native-bridge-scaffold.ps1",
            "scripts/verify-no-vectorworks.ps1",
            ".github/workflows/verify.yml",
        ):
            self.assertTrue((ROOT / relative_path).exists(), relative_path)

    def test_connector_ci_checks_standalone_plugin_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")

        self.assertIn("repository: BhaveshY/vectorworks-claude-plugin", workflow)
        self.assertIn("check-companion-contract.ps1", workflow)
        self.assertIn("Standalone plugin companion contract", workflow)

    def test_generated_launcher_uses_dialog_agent_session_listener(self):
        register_script = (ROOT / "scripts/register-claude-code.ps1").read_text(encoding="utf-8")
        self.assertIn('os.environ["VW_MCP_MODE"] = "dialog"', register_script)
        self.assertIn('os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"', register_script)
        self.assertIn("New-VectorworksLoader", register_script)
        self.assertIn("vw_load_listener_2024.py", register_script)
        self.assertIn('VW_MCP_PREFLIGHT_CACHE_MS = "750"', register_script)
        self.assertIn("CopyLoaderToClipboard", register_script)
        self.assertIn("copy-vectorworks-loader.ps1", register_script)
        self.assertIn("VW_MCP_LOADER_METADATA", register_script)
        self.assertIn("requiredFeatures", register_script)

        launcher_path = ROOT / "vw_start_listener_2024.py"
        if launcher_path.exists():
            launcher_text = launcher_path.read_text(encoding="utf-8")
            self.assertIn('os.environ["VW_MCP_MODE"] = "dialog"', launcher_text)
            self.assertIn('os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"', launcher_text)

        loader_path = ROOT / "vw_load_listener_2024.py"
        if loader_path.exists():
            loader_text = loader_path.read_text(encoding="utf-8")
            self.assertIn("runpy.run_path", loader_text)
            self.assertIn("vw_start_listener_2024.py", loader_text)

    def test_copy_loader_helper_regenerates_and_captures_loader_text(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the loader clipboard helper")
        if not os.environ.get("USERPROFILE"):
            self.skipTest("USERPROFILE is required for the generated Windows launcher")

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher_path = Path(temp_dir) / "vw_start_listener_2024.py"
            loader_path = Path(temp_dir) / "vw_load_listener_2024.py"
            script = str(ROOT / "scripts/copy-vectorworks-loader.ps1").replace("'", "''")
            launcher = str(launcher_path).replace("'", "''")
            loader = str(loader_path).replace("'", "''")
            command = (
                "$global:VW_TEST_CLIPBOARD = ''; "
                "function Set-Clipboard { param([string]$Value) $global:VW_TEST_CLIPBOARD = $Value }; "
                f"& '{script}' -LauncherPath '{launcher}' -LoaderPath '{loader}' -Regenerate -Print; "
                "if (-not $global:VW_TEST_CLIPBOARD.Contains('runpy.run_path')) { throw 'clipboard capture missing loader text' }"
            )
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertTrue(loader_path.exists())
            self.assertIn("runpy.run_path", result.stdout)
            self.assertIn(str(launcher_path).replace("\\", "\\\\"), loader_path.read_text(encoding="utf-8"))

    def test_no_vectorworks_verifier_generates_fresh_launcher_by_default(self):
        verifier = (ROOT / "scripts/verify-no-vectorworks.ps1").read_text(encoding="utf-8")

        self.assertIn("[System.IO.Path]::GetTempPath()", verifier)
        self.assertIn("$FreshLauncher = $true", verifier)
        self.assertIn("$FreshLoader = $true", verifier)
        self.assertIn("$FreshLauncher -or $FreshLoader -or -not (Test-Path $LauncherPath)", verifier)
        self.assertIn("Remove-Item -LiteralPath $LauncherPath", verifier)
        self.assertIn("Remove-Item -LiteralPath $LoaderPath", verifier)
        self.assertIn("Generated Vectorworks loader", verifier)
        self.assertIn("test-native-bridge-scaffold.ps1", verifier)
        self.assertIn("native bridge scaffold compile smoke", verifier)
        self.assertIn("doctor-native-bridge.ps1", verifier)
        self.assertIn("native bridge doctor next command", verifier)
        self.assertIn("nextCommandReason", verifier)

    def test_native_bridge_scaffold_compile_smoke_script_exercises_protocol(self):
        smoke = (ROOT / "scripts/test-native-bridge-scaffold.ps1").read_text(encoding="utf-8")
        harness = (ROOT / "native_bridge/tests/native_scaffold_smoke.cpp").read_text(encoding="utf-8")

        self.assertIn("cl.exe", smoke)
        self.assertIn("clang++.exe", smoke)
        self.assertIn("g++.exe", smoke)
        self.assertIn("c++.exe", smoke)
        self.assertIn("BridgeProtocol.cpp", smoke)
        self.assertIn("VectorworksMCPBridge.cpp", smoke)
        self.assertIn("BridgeDispatcher.hpp", smoke)
        self.assertIn("CadRequestQueue.hpp", smoke)
        self.assertIn("native_scaffold_smoke.cpp", smoke)
        self.assertIn("RequireCompiler", smoke)
        self.assertIn("No C++ compiler found", smoke)
        self.assertIn("ParseRequestEnvelope", harness)
        self.assertIn("SerializeResponseEnvelope", harness)
        self.assertIn("FindActionSpec", harness)
        self.assertIn("RequiresCadMainContext", harness)
        self.assertIn("CadRequestQueue", harness)
        self.assertIn("DispatchFromSocketWorker", harness)
        self.assertIn("missing params should default to object", harness)
        self.assertIn("array params should fail", harness)
        self.assertIn("success without result should fail", harness)
        self.assertIn("failure without error should fail", harness)
        self.assertIn("phase-0 CAD request should fail immediately", harness)

    def test_register_script_generates_dialog_agent_session_launcher(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the Windows launcher generator")
        if not os.environ.get("USERPROFILE"):
            self.skipTest("USERPROFILE is required for the generated Windows launcher")

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher_path = Path(temp_dir) / "vw_start_listener_2024.py"
            loader_path = Path(temp_dir) / "vw_load_listener_2024.py"
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
                    "-LoaderPath",
                    str(loader_path),
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

            loader_text = loader_path.read_text(encoding="utf-8")
            self.assertIn("runpy.run_path", loader_text)
            self.assertIn(str(launcher_path).replace("\\", "\\\\"), loader_text)
            self.assertIn("VW_MCP_LOADER_METADATA", loader_text)
            self.assertIn('"contractVersion": 6', loader_text)
            self.assertIn('"native-bridge-scaffold-copy"', loader_text)
            self.assertIn('"native-doctor-next-command"', loader_text)

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
            "native_bridge/src/BridgeProtocol.hpp",
            "native_bridge/src/BridgeProtocol.cpp",
            "native_bridge/src/BridgeDispatcher.hpp",
            "native_bridge/src/CadRequestQueue.hpp",
            "native_bridge/src/VectorworksMCPBridge.cpp",
            "native_bridge/tests/native_scaffold_smoke.cpp",
        )
        for relative_path in expected_files:
            self.assertTrue((ROOT / relative_path).exists(), relative_path)

        native_readme = (ROOT / "native_bridge/README.md").read_text(encoding="utf-8")
        self.assertIn("native Vectorworks SDK plug-in bridge", native_readme)
        self.assertIn("marshaled back onto the Vectorworks main/plugin event context", native_readme)
        self.assertIn("Revit-style connector", native_readme)
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("not compiled or installed by default", root_readme)
        self.assertIn("Why this is not as simple as a Revit-style setup yet", root_readme)
        self.assertIn("bridge_kind=python_dialog_agent_session", root_readme)
        self.assertIn("Raw socket reachability is not enough", root_readme)

        protocol = (ROOT / "native_bridge/PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("4-byte big-endian", protocol)
        self.assertIn("must not call", protocol)
        self.assertIn("Vectorworks document APIs directly", protocol)

        matrix = (ROOT / "native_bridge/HANDLER_MATRIX.md").read_text(encoding="utf-8")
        self.assertIn("Native phase", matrix)
        self.assertIn("main/plugin event context", matrix)

    def test_native_bridge_source_scaffold_encodes_threading_contract(self):
        src = ROOT / "native_bridge" / "src"
        scaffold_files = (
            "BridgeProtocol.hpp",
            "BridgeProtocol.cpp",
            "BridgeDispatcher.hpp",
            "CadRequestQueue.hpp",
            "VectorworksMCPBridge.cpp",
        )
        combined = "\n".join((src / name).read_text(encoding="utf-8") for name in scaffold_files)

        for action in (
            "ping",
            "stop",
            "get_document_info",
            "get_layers",
            "get_objects",
            "selection",
            "create_object",
        ):
            self.assertIn(action, combined)

        self.assertIn("kMaxFrameBytes", combined)
        self.assertIn("EncodeFrameHeader", combined)
        self.assertIn("DecodeFrameHeader", combined)
        self.assertIn("ParseRequestEnvelope", combined)
        self.assertIn("SerializeResponseEnvelope", combined)
        self.assertIn("FindActionSpec", combined)
        self.assertIn("request params must be a JSON object", combined)
        self.assertIn("duplicate request id field", combined)
        self.assertIn("success response result is required", combined)
        self.assertIn("failure response error is required", combined)
        self.assertIn("native bridge scaffold only supports ASCII request strings", combined)
        self.assertIn("CadRequestQueue", combined)
        self.assertIn("Socket worker thread must not call Vectorworks document", combined)
        self.assertIn("VectorworksMainPluginContext", combined)
        self.assertIn("TryDequeueOnVectorworksMainContext", combined)
        self.assertIn("CompleteFromVectorworksMainContext", combined)
        self.assertIn("std::atomic_bool", combined)
        self.assertIn("WaitForResponseOnSocketThread", combined)
        self.assertIn("wait_for", combined)
        self.assertIn("kCadRequestTimeout", combined)
        self.assertIn("CancelAll", combined)
        self.assertIn("ResetCancellation", combined)
        self.assertIn("kDefaultMaxPendingCadRequests", combined)
        self.assertIn("maxPendingRequests_", combined)
        self.assertIn("duplicate native bridge request id", combined)
        self.assertIn("native bridge CAD request queue is full", combined)
        self.assertIn("PendingCountForDiagnostics", combined)
        self.assertIn("InFlightCountForDiagnostics", combined)
        self.assertIn("kCadHandlersImplemented", combined)
        self.assertIn("native bridge phase 0 CAD handlers are not implemented", combined)
        self.assertIn("native bridge timed out waiting for Vectorworks main/plugin context", combined)
        self.assertIn('"cad_api_safe":false', combined)
        self.assertIn('"transport_only":true', combined)
        self.assertIn('"native_phase":0', combined)
        self.assertIn('"implemented_actions":["ping","stop"]', combined)

        for name in scaffold_files:
            text = (src / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r'#include\s+[<"].*(Vectorworks|VWFC|MiniCad|SDK).*?[>"]', name)

        readme = (src / "README.md").read_text(encoding="utf-8")
        self.assertIn("not a standalone build system", readme)
        self.assertIn("copy-native-bridge-scaffold.ps1", readme)
        self.assertIn("strict request envelope parsing", readme)

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
        self.assertIn("vw_load_listener_2024.py", gitignore)

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
        self.assertIn("copy-native-bridge-scaffold.ps1", prepare)
        self.assertIn("SDKExamples", prepare)
        self.assertIn("VectorworksMCPBridge", prepare)
        self.assertIn("[string]$SdkDir", prepare)

        self.assertIn("check-native-bridge-prereqs.ps1", build)
        self.assertIn("MSBuild", build)
        self.assertIn("*$VectorworksVersion.sln", build)
        self.assertIn("/p:Platform=x64", build)
        self.assertIn("[string]$SdkDir", build)
        self.assertIn('"-SdkDir", $SdkDir', build)
        self.assertIn("Microsoft.VisualStudio.2022.BuildTools", bootstrap)
        self.assertIn("Microsoft.VisualStudio.Workload.VCTools", bootstrap)
        self.assertIn("[switch]$PrepareSource", bootstrap)
        self.assertIn("[switch]$Build", bootstrap)
        self.assertIn("[string]$WorktreeRoot", bootstrap)
        self.assertIn('"-SdkDir", $SdkDir', bootstrap)
        self.assertIn('"-WorktreeRoot", $WorktreeRoot', bootstrap)
        self.assertIn('"-SourceDir", $WorktreeRoot', bootstrap)
        self.assertIn("[string]$WorktreeRoot", prepare)
        self.assertIn("third_party\\VectorworksSDKExamples\\VectorworksSDK\\SDK$Version", checker)
        self.assertIn("native_bridge/worktree/", gitignore)

    def test_copy_native_bridge_scaffold_script_copies_reviewed_sources(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise native scaffold copy")

        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir) / "SDKExamples"
            source_dir = worktree / "Examples2024" / "VectorworksMCPBridge" / "Source"
            source_dir.mkdir(parents=True)

            copy_result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/copy-native-bridge-scaffold.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            destination = source_dir / "VectorworksMCPBridge"
            self.assertIn("smoke-native-bridge.ps1 -Phase 0 -Stop -Json", copy_result.stdout)
            self.assertTrue((destination / "BridgeProtocol.hpp").exists())
            self.assertTrue((destination / "CadRequestQueue.hpp").exists())
            self.assertTrue((destination / "VectorworksMCPBridge.cpp").exists())
            self.assertIn("ParseRequestEnvelope", (destination / "BridgeProtocol.hpp").read_text(encoding="utf-8"))
            self.assertIn("SerializeResponseEnvelope", (destination / "BridgeProtocol.cpp").read_text(encoding="utf-8"))
            self.assertIn("native_sdk_bridge_scaffold", (destination / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8"))
            self.assertIn("CancelAll", (destination / "CadRequestQueue.hpp").read_text(encoding="utf-8"))
            self.assertIn("duplicate native bridge request id", (destination / "CadRequestQueue.hpp").read_text(encoding="utf-8"))

            refusal = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/copy-native-bridge-scaffold.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertNotEqual(refusal.returncode, 0)
            self.assertIn("Refusing to overwrite", refusal.stderr + refusal.stdout)

            (destination / "CadRequestQueue.hpp").write_text("stale scaffold\n", encoding="utf-8")
            subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/copy-native-bridge-scaffold.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                    "-Force",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIn("CancelAll", (destination / "CadRequestQueue.hpp").read_text(encoding="utf-8"))

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

    def test_prepare_native_bridge_source_accepts_sdk_dir(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise native source preparation")

        worktree = ROOT / "native_bridge" / "worktree"
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk_root = Path(temp_dir) / "ExtractedSDK"
            examples = sdk_root / "SDKExamples"
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
                        "-SdkDir",
                        str(sdk_root),
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
                self.assertTrue((root / "VectorworksSDK" / "SDK2024" / "SDKLib").exists())
            finally:
                if worktree.exists():
                    shutil.rmtree(worktree)

    def test_prepare_native_bridge_source_accepts_custom_worktree_root(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise native source preparation")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            examples = temp_root / "SDKExamples Source"
            source = examples / "Examples2024" / "ObjectExample"
            custom_worktree = temp_root / "Custom SDKExamples"
            (source / "Source").mkdir(parents=True)
            (examples / "VectorworksSDK" / "SDK2024" / "SDKLib").mkdir(parents=True)
            (examples / "ThirdPartySource" / "libcurl").mkdir(parents=True)
            (source / "ObjectExample2024.sln").write_text("fake solution\n", encoding="utf-8")

            result = subprocess.run(
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
                    "-WorktreeRoot",
                    str(custom_worktree),
                    "-Force",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            bridge = custom_worktree / "Examples2024" / "VectorworksMCPBridge"
            self.assertTrue((bridge / "ObjectExample2024.sln").exists())
            self.assertTrue((bridge / "VECTORWORKS_MCP_BRIDGE_NOTES.md").exists())
            self.assertIn(str(custom_worktree), result.stdout)
            self.assertIn("-SourceDir", result.stdout)
            self.assertIn(str(custom_worktree), (bridge / "VECTORWORKS_MCP_BRIDGE_NOTES.md").read_text(encoding="utf-8"))

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
        raw_ping = (ROOT / "scripts/test-vectorworks-listener.ps1").read_text(encoding="utf-8")

        self.assertIn("overall", doctor)
        self.assertIn("nextActions", doctor)
        self.assertIn("cad_api_safe", doctor)
        self.assertIn("transport_only", doctor)
        self.assertIn("check-native-bridge-prereqs.ps1", doctor)
        self.assertIn("LoaderStatus", doctor)
        self.assertIn("copy-vectorworks-loader.ps1", doctor)
        self.assertIn("stable loader", doctor)
        self.assertIn("connector contract", doctor)
        self.assertIn("VW_MCP_LOADER_METADATA", doctor)
        self.assertIn("Write-RecoverySteps", raw_ping)
        self.assertIn("vw_load_listener_2024.py", raw_ping)
        self.assertIn("foreground/background/win_timer", raw_ping)

    def test_native_smoke_script_is_documented(self):
        smoke_script = (ROOT / "scripts/smoke-native-bridge.ps1").read_text(encoding="utf-8")
        acceptance = (ROOT / "native_bridge/ACCEPTANCE.md").read_text(encoding="utf-8")
        native_readme = (ROOT / "native_bridge/README.md").read_text(encoding="utf-8")
        protocol = (ROOT / "native_bridge/PROTOCOL.md").read_text(encoding="utf-8")
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("native_bridge\\smoke.py", smoke_script)
        self.assertIn("--ping-count", smoke_script)
        self.assertIn("--read-count", smoke_script)
        self.assertIn("--max-ping-ms", smoke_script)
        self.assertIn("--max-read-ms", smoke_script)
        self.assertIn("--phase", smoke_script)
        self.assertIn("--allow-write-fixture", smoke_script)
        self.assertIn("smoke-native-bridge.ps1", acceptance)
        self.assertIn("schema failures", acceptance)
        self.assertIn("smoke-native-bridge.ps1", native_readme)
        self.assertIn("minimum response", native_readme)
        self.assertIn("Phase 0 accepts", native_readme)
        self.assertIn("cad_api_safe: false", native_readme)
        self.assertIn("Phase-0 Smoke Schema", protocol)
        self.assertIn("Phase-1 Smoke Schemas", protocol)
        self.assertIn("success` must be boolean", protocol)
        self.assertIn('dispatch_mode: "native_sdk"', protocol)
        self.assertIn("cross-checks", native_readme)
        self.assertIn("implemented_actions", native_readme)
        self.assertIn("selection.get", native_readme)
        self.assertIn("native_phase >= 1", native_readme)
        self.assertIn("bounded backpressure", native_readme)
        self.assertIn("phase 0 CAD handlers are not implemented", native_readme)
        self.assertIn("get_document_info", protocol)
        self.assertIn("get_objects", protocol)
        self.assertIn("implemented_actions", protocol)
        self.assertIn("selection` with `action=get`", protocol)
        self.assertIn(
            "powershell -ExecutionPolicy Bypass -File .\\scripts\\doctor-native-bridge.ps1 -BuiltArtifact C:\\path\\to\\VectorworksMCPBridge.vwlibrary -Install -WhatIf",
            root_readme,
        )
        self.assertIn(
            "powershell -ExecutionPolicy Bypass -File .\\scripts\\doctor-native-bridge.ps1 -BuiltArtifact C:\\path\\to\\VectorworksMCPBridge.vwlibrary -Install\n# Restart Vectorworks",
            root_readme,
        )
        self.assertIn("enable/load the native VectorworksMCPBridge plug-in", root_readme)
        self.assertIn("smoke-native-bridge.ps1 -Phase 0 -Stop -Json", root_readme)
        self.assertIn("nextCommand", root_readme)
        self.assertIn("nextCommandReason", root_readme)
        self.assertIn(
            "doctor-native-bridge.ps1 -BuiltArtifact C:\\path\\to\\VectorworksMCPBridge.vwlibrary -Install -WhatIf",
            agents,
        )
        self.assertIn(
            "doctor-native-bridge.ps1 -BuiltArtifact C:\\path\\to\\VectorworksMCPBridge.vwlibrary -Install",
            agents,
        )
        self.assertIn("smoke-native-bridge.ps1 -Phase 0 -Stop -Json", agents)
        self.assertIn("nextCommand", agents)
        self.assertIn("nextCommandReason", agents)
        self.assertIn("Do not run the default native smoke against the copied", agents)

    def test_native_doctor_exposes_stage_aware_next_command(self):
        doctor = (ROOT / "scripts/doctor-native-bridge.ps1").read_text(encoding="utf-8")

        self.assertIn("nextCommand", doctor)
        self.assertIn("nextCommandReason", doctor)
        self.assertIn("nextActions", doctor)
        self.assertIn("bootstrap-native-bridge.ps1", doctor)
        self.assertIn("-InstallVisualStudioBuildTools", doctor)
        self.assertIn("-DownloadSdk", doctor)
        self.assertIn("-PrepareSource", doctor)
        self.assertIn("prepare-native-bridge-source.ps1", doctor)
        self.assertIn("build-native-bridge.ps1", doctor)
        self.assertIn("copy-native-bridge-scaffold.ps1", doctor)
        self.assertIn("without -WhatIf", doctor)
        self.assertIn("smoke-native-bridge.ps1 -Phase 0 -Stop -Json", doctor)

    def test_native_doctor_reports_one_primary_next_command_for_empty_worktree(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the native doctor")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            worktree = temp_root / "SDKExamples With Spaces"
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/doctor-native-bridge.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                    "-InstallDir",
                    str(temp_root / "Plug-ins"),
                    "-Json",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            report = json.loads(result.stdout)
            self.assertIsInstance(report["nextActions"], list)
            self.assertTrue(report["nextCommand"])
            self.assertTrue(report["nextCommandReason"])
            self.assertIn(str(worktree), report["nextCommand"])
            self.assertIn("-WorktreeRoot", report["nextCommand"])
            self.assertIn(str(ROOT / "scripts"), report["nextCommand"])
            self.assertNotIn("-File .\\scripts", report["nextCommand"])
            if report["prereqsReady"]:
                self.assertIn("prepare-native-bridge-source.ps1", report["nextCommand"])
            else:
                self.assertIn("bootstrap-native-bridge.ps1", report["nextCommand"])
                self.assertIn("-InstallVisualStudioBuildTools", report["nextCommand"])
                self.assertIn("-DownloadSdk", report["nextCommand"])

    def test_native_doctor_can_plan_and_install_explicit_artifact(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the native doctor")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            artifact = temp_root / "VectorworksMCPBridge.vwlibrary"
            install_dir = temp_root / "Plug-ins With Spaces"
            artifact.write_text("fake native bridge artifact\n", encoding="utf-8")

            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/doctor-native-bridge.ps1"),
                    "-VectorworksVersion",
                    "2025",
                    "-BuiltArtifact",
                    str(artifact),
                    "-InstallDir",
                    str(install_dir),
                    "-Install",
                    "-Json",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            report = json.loads(result.stdout)
            installed_path = Path(report["installedPath"])
            self.assertEqual(installed_path, install_dir / artifact.name)
            self.assertTrue(installed_path.exists())
            self.assertEqual(report["builtArtifact"], str(artifact))
            self.assertEqual(report["installDestination"], str(install_dir / artifact.name))
            self.assertTrue(report["installPerformed"])
            self.assertFalse(report["installWhatIf"])
            self.assertIn("smoke-native-bridge.ps1 -Phase 0 -Stop -Json", "\n".join(report["nextActions"]))
            self.assertIn("nextCommand", report)
            self.assertIn("nextCommandReason", report)
            self.assertIn("smoke-native-bridge.ps1 -Phase 0 -Stop -Json", report["nextCommand"])
            self.assertIn("Restart Vectorworks", report["nextCommandReason"])

    def test_native_doctor_whatif_install_is_non_mutating(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the native doctor")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            artifact = temp_root / "VectorworksMCPBridge.vwlibrary"
            install_dir = temp_root / "Plug-ins With Spaces"
            artifact.write_text("fake native bridge artifact\n", encoding="utf-8")

            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/doctor-native-bridge.ps1"),
                    "-VectorworksVersion",
                    "2025",
                    "-BuiltArtifact",
                    str(artifact),
                    "-InstallDir",
                    str(install_dir),
                    "-Install",
                    "-WhatIf",
                    "-Json",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            report = json.loads(result.stdout)
            destination = install_dir / artifact.name
            self.assertFalse(destination.exists())
            self.assertFalse(install_dir.exists())
            self.assertTrue(report["installRequested"])
            self.assertTrue(report["installWhatIf"])
            self.assertFalse(report["installPerformed"])
            self.assertEqual(report["installDestination"], str(destination))
            self.assertEqual(report["installedPath"], "")
            self.assertNotIn("Restart Vectorworks", "\n".join(report["nextActions"]))
            self.assertIn("without -WhatIf", "\n".join(report["nextActions"]))
            self.assertIn("doctor-native-bridge.ps1", report["nextCommand"])
            self.assertIn("-VectorworksVersion 2025", report["nextCommand"])
            self.assertIn(str(artifact), report["nextCommand"])
            self.assertIn(str(install_dir), report["nextCommand"])
            self.assertIn("-Install", report["nextCommand"])
            self.assertNotIn("-WhatIf", report["nextCommand"])
            self.assertIn("without -WhatIf", report["nextCommandReason"])

    def test_native_doctor_reports_auto_discovered_artifact_as_candidate_only(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the native doctor")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            worktree = temp_root / "SDKExamples"
            bridge_source = worktree / "Examples2024" / "VectorworksMCPBridge"
            artifact = bridge_source / "Build" / "VectorworksMCPBridge.vwlibrary"
            install_dir = temp_root / "Plug-ins With Spaces"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("fake native bridge artifact\n", encoding="utf-8")

            plan_result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/doctor-native-bridge.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                    "-InstallDir",
                    str(install_dir),
                    "-Json",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            report = json.loads(plan_result.stdout)
            self.assertEqual(report["builtArtifact"], "")
            self.assertFalse(report["builtArtifactWasExplicit"])
            self.assertEqual(report["builtArtifactCandidate"], str(artifact))
            self.assertIn(str(artifact), "\n".join(report["nextActions"]))
            self.assertIn(str(artifact), report["nextCommand"])
            self.assertIn(str(install_dir), report["nextCommand"])
            self.assertIn("-Install -WhatIf", report["nextCommand"])
            self.assertIn("Dry-run", report["nextCommandReason"])

            install_result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/doctor-native-bridge.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                    "-InstallDir",
                    str(install_dir),
                    "-Install",
                    "-Json",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(install_result.returncode, 0)
            self.assertIn("explicit -BuiltArtifact", install_result.stderr + install_result.stdout)
            self.assertFalse((install_dir / artifact.name).exists())
            self.assertFalse(install_dir.exists())

    def test_native_doctor_detects_partial_scaffold_copy(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is required to exercise the native doctor")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            worktree = temp_root / "SDKExamples"
            bridge_source = worktree / "Examples2024" / "VectorworksMCPBridge"
            scaffold_dir = bridge_source / "Source" / "VectorworksMCPBridge"
            scaffold_dir.mkdir(parents=True)
            (bridge_source / "VectorworksMCPBridge2024.sln").write_text("fake solution\n", encoding="utf-8")
            (scaffold_dir / "BridgeProtocol.hpp").write_text("partial scaffold\n", encoding="utf-8")

            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts/doctor-native-bridge.ps1"),
                    "-WorktreeRoot",
                    str(worktree),
                    "-InstallDir",
                    str(temp_root / "Plug-ins"),
                    "-Json",
                ],
                cwd=str(ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            report = json.loads(result.stdout)
            self.assertFalse(report["scaffoldCopied"])
            self.assertEqual(report["worktreeRoot"], str(worktree))
            self.assertIn("CadRequestQueue.hpp", report["missingScaffoldFiles"])
            self.assertIn("VectorworksMCPBridge.cpp", report["missingScaffoldFiles"])
            self.assertIn("copy-native-bridge-scaffold.ps1 -VectorworksVersion 2024 -Force", "\n".join(report["nextActions"]))
            self.assertIn("copy-native-bridge-scaffold.ps1 -VectorworksVersion 2024 -Force", report["nextCommand"])
            self.assertIn(str(worktree), report["nextCommand"])
            self.assertIn("-WorktreeRoot", report["nextCommand"])
            self.assertIn("partially copied", report["nextCommandReason"])

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
