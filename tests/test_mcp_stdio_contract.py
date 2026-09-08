import json
import os
import shutil
import sys
import unittest
from pathlib import Path

import anyio
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def _all_broken_resource_errors(exc: BaseException) -> bool:
    if isinstance(exc, anyio.BrokenResourceError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return all(_all_broken_resource_errors(item) for item in exc.exceptions)
    return False


class McpStdioContractTests(unittest.TestCase):
    def test_modern_discovery_and_legacy_clients_share_tool_contract(self):
        async def run_contract():
            env = {**os.environ, "VW_MCP_HOST": "127.0.0.1", "VW_MCP_PORT": "1",
                   "VW_MCP_TIMEOUT": "0.5", "VW_MCP_HEALTH_TIMEOUT": "0.2",
                   "VW_MCP_INSECURE_NO_AUTH": "1", "VW_MCP_TOOL_PROFILE": "fast-native"}
            params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=ROOT, env=env)
            for mode, expected in [("legacy", "2025-11-25"), ("auto", "2026-07-28"), ("2026-07-28", "2026-07-28")]:
                with self.subTest(mode=mode):
                    async with Client(params, mode=mode, read_timeout_seconds=10) as client:
                        self.assertEqual(client.protocol_version, expected)
                        listed = await client.session.list_tools()
                        self.assertEqual({tool.name for tool in listed.tools}, set(__import__("server").FAST_NATIVE_TOOL_NAMES))
                        result = await client.session.call_tool("vw_tool_safety", {})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content, json.loads(result.content[0].text))
                        ping = await client.session.call_tool("vw_status", {"action": "health"})
                        self.assertTrue(ping.is_error)
                        self.assertFalse(ping.structured_content["ok"])

        anyio.run(run_contract)

    def test_bundled_codex_wrapper_starts_when_spawned_by_python(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("Windows PowerShell is required to exercise the Codex plugin wrapper")

        async def run_contract():
            env = os.environ.copy()
            env.update(
                {
                    "VW_MCP_REPO": str(ROOT),
                    "VW_MCP_HOST": "127.0.0.1",
                    "VW_MCP_PORT": "1",
                    "VW_MCP_TIMEOUT": "0.5",
                    "VW_MCP_HEALTH_TIMEOUT": "0.2",
                    "VW_MCP_INSECURE_NO_AUTH": "1",
                    "VW_MCP_TOOL_PROFILE": "fast-native",
                }
            )
            params = StdioServerParameters(
                command=powershell,
                args=[
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "plugins" / "vectorworks" / "scripts" / "run-vectorworks-mcp.ps1"),
                ],
                cwd=ROOT,
                env=env,
            )
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=15) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.server_info.version, "0.7.0")
                        tools = await session.list_tools()
                        self.assertEqual({tool.name for tool in tools.tools}, set(__import__("server").FAST_NATIVE_TOOL_NAMES))

        anyio.run(run_contract)

    def test_fast_native_profile_exposes_only_curated_native_tools(self):
        async def run_contract():
            env = os.environ.copy()
            env.update(
                {
                    "VW_MCP_HOST": "127.0.0.1",
                    "VW_MCP_PORT": "1",
                    "VW_MCP_TIMEOUT": "0.5",
                    "VW_MCP_HEALTH_TIMEOUT": "0.2",
                    "VW_MCP_INSECURE_NO_AUTH": "1",
                    "VW_MCP_TOOL_PROFILE": "fast-native",
                }
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["server.py"],
                cwd=ROOT,
                env=env,
            )
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=5) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.server_info.version, "0.7.0")
                        self.assertIn("fast-native phase-4", initialized.instructions[:512])
                        self.assertIn("capability revision 4 or newer", initialized.instructions[:512])
                        self.assertIn("one atomic vw_apply or vw_execute_operations", initialized.instructions[:512])
                        self.assertIn("Never use modal Python", initialized.instructions[:512])
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        by_name = {tool.name: tool for tool in tools.tools}
                        self.assertEqual(names, set(__import__("server").FAST_NATIVE_TOOL_NAMES))
                        self.assertIn("vw_execute_operations", names)
                        self.assertNotIn("vw_run_script", names)
                        self.assertNotIn("vw_create_object", names)
                        self.assertNotIn("vw_insert_door", names)
                        self.assertEqual(
                            by_name["vw_catalog"].input_schema["properties"]["action"]["enum"],
                            ["capabilities", "classes", "symbols", "parametric_schemas", "worksheets", "resources"],
                        )
                        self.assertNotIn("idempotency_key", by_name["vw_io"].input_schema["properties"])
                        self.assertNotIn("idempotency_key", by_name["vw_document"].input_schema["properties"])
                        for tool in tools.tools:
                            self.assertEqual(tool.output_schema, {"type": "object", "additionalProperties": True})

                        safety_result = await session.call_tool("vw_tool_safety")
                        self.assertFalse(safety_result.is_error)
                        self.assertEqual(
                            safety_result.structured_content,
                            json.loads(safety_result.content[0].text),
                        )

                        self.assertEqual(
                            safety_result.structured_content["vw_document"]["actions"]["open"]["retryPolicy"],
                            "never_after_send",
                        )

        anyio.run(run_contract)

    def test_server_starts_over_stdio_and_exposes_expected_contract(self):
        contract_checked = False

        async def run_contract():
            nonlocal contract_checked
            env = os.environ.copy()
            env.update(
                {
                    "VW_MCP_HOST": "127.0.0.1",
                    "VW_MCP_PORT": "1",
                    "VW_MCP_TIMEOUT": "0.5",
                    "VW_MCP_HEALTH_TIMEOUT": "0.2",
                    "VW_MCP_INSECURE_NO_AUTH": "1",
                    "VW_MCP_TOOL_PROFILE": "compat",
                }
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["server.py"],
                cwd=ROOT,
                env=env,
            )

            with open(os.devnull, "w", encoding="utf-8") as errlog:
                try:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=5) as session:
                            initialized = await session.initialize()
                            self.assertEqual(initialized.server_info.name, "Vectorworks 2024/2025")
                            self.assertEqual(initialized.server_info.version, "0.7.0")
                            self.assertIn("fast-native phase-4", initialized.instructions[:512])

                            tools = await session.list_tools()
                            by_name = {tool.name: tool for tool in tools.tools}
                            self.assertGreaterEqual(len(by_name), 25)
                            self.assertIn(
                                "move",
                                by_name["vw_selection"].input_schema["properties"]["action"]["enum"],
                            )
                            self.assertIn(
                                "duplicate",
                                by_name["vw_selection"].input_schema["properties"]["action"]["enum"],
                            )
                            for name in (
                                "vw_ping",
                                "vw_preflight_for_cad",
                                "vw_create_object",
                                "vw_batch_create_objects",
                                "vw_execute_operations",
                                "vw_plan_schematic_floor_plan",
                                "vw_create_schematic_floor_plan",
                                "vw_create_bim_floor_plan",
                                "vw_create_text",
                                "vw_create_linear_dimension",
                                "vw_drawing_summary",
                                "vw_lookup_objects",
                                "vw_batch_set_object_properties",
                                "vw_agent_context",
                                "vw_capabilities",
                                "vw_get_layers",
                            ):
                                self.assertIn(name, by_name)

                            get_objects_schema = by_name["vw_get_objects"].input_schema
                            self.assertEqual(get_objects_schema["additionalProperties"], False)
                            self.assertEqual(get_objects_schema["properties"]["limit"]["minimum"], 1)
                            self.assertEqual(get_objects_schema["properties"]["limit"]["maximum"], 1000)

                            drawing_summary_schema = by_name["vw_drawing_summary"].input_schema
                            self.assertIn("include_examples", drawing_summary_schema["properties"])
                            self.assertEqual(drawing_summary_schema["properties"]["example_limit"]["minimum"], 0)
                            self.assertEqual(drawing_summary_schema["properties"]["example_limit"]["maximum"], 100)
                            self.assertEqual(drawing_summary_schema["properties"]["scan_limit"]["maximum"], 100000)

                            lookup_schema = by_name["vw_lookup_objects"].input_schema
                            self.assertIn("detail", lookup_schema["properties"])
                            self.assertIn("fields", lookup_schema["properties"])
                            self.assertEqual(lookup_schema["properties"]["limit"]["maximum"], 1000)

                            batch_property_schema = by_name["vw_batch_set_object_properties"].input_schema
                            self.assertEqual(batch_property_schema["properties"]["edits"]["minItems"], 1)
                            self.assertEqual(batch_property_schema["properties"]["edits"]["maxItems"], 100)
                            self.assertIn("items", batch_property_schema["properties"]["edits"])
                            self.assertEqual(batch_property_schema["properties"]["edits"]["items"]["required"], ["ref", "properties"])
                            self.assertFalse(batch_property_schema["properties"]["edits"]["items"]["additionalProperties"])
                            self.assertIn("fillColor", batch_property_schema["properties"]["edits"]["items"]["properties"]["properties"]["properties"])
                            self.assertEqual(batch_property_schema["properties"]["lookup_limit"]["maximum"], 1000)

                            agent_context_schema = by_name["vw_agent_context"].input_schema
                            self.assertIn("profile", agent_context_schema["properties"])
                            self.assertIn("include_examples", agent_context_schema["properties"])

                            worksheet_schema = by_name["vw_worksheet"].input_schema
                            self.assertEqual(worksheet_schema["properties"]["row"]["minimum"], 1)
                            self.assertEqual(worksheet_schema["properties"]["col"]["minimum"], 1)
                            self.assertEqual(worksheet_schema["properties"]["num_rows"]["maximum"], 500)

                            slab_points_schema = by_name["vw_create_slab"].input_schema["properties"]["points"]
                            self.assertEqual(slab_points_schema["minItems"], 3)
                            self.assertEqual(slab_points_schema["items"]["minItems"], 2)
                            self.assertEqual(slab_points_schema["items"]["maxItems"], 2)

                            batch_objects_schema = by_name["vw_batch_create_objects"].input_schema["properties"]["objects"]
                            self.assertEqual(batch_objects_schema["minItems"], 1)
                            self.assertEqual(batch_objects_schema["maxItems"], 250)
                            self.assertIn("atomic", by_name["vw_batch_create_objects"].input_schema["properties"])

                            execute_schema = by_name["vw_execute_operations"].input_schema
                            self.assertEqual(execute_schema["properties"]["operations"]["minItems"], 1)
                            self.assertEqual(execute_schema["properties"]["operations"]["maxItems"], 250)
                            self.assertIn("create", execute_schema["properties"]["operations"]["items"]["properties"]["type"]["enum"])
                            self.assertIn(
                                "set_properties",
                                execute_schema["properties"]["operations"]["items"]["properties"]["type"]["enum"],
                            )
                            self.assertIn("idempotency_key", execute_schema["required"])
                            self.assertEqual(execute_schema["properties"]["idempotency_key"]["maxLength"], 128)
                            self.assertIn("pattern", execute_schema["properties"]["idempotency_key"])

                            create_object_type = by_name["vw_create_object"].input_schema["properties"]["object_type"]
                            self.assertIn("rectangle", create_object_type["enum"])
                            self.assertIn("box", create_object_type["enum"])

                            floor_plan_rooms_schema = by_name["vw_create_schematic_floor_plan"].input_schema["properties"]["rooms"]
                            self.assertEqual(floor_plan_rooms_schema["minItems"], 1)
                            self.assertEqual(floor_plan_rooms_schema["maxItems"], 100)
                            self.assertIn("atomic", by_name["vw_create_schematic_floor_plan"].input_schema["properties"])

                            bim_floor_plan_schema = by_name["vw_create_bim_floor_plan"].input_schema
                            self.assertIn("wall_height", bim_floor_plan_schema["properties"])
                            self.assertIn("dimension_rooms", bim_floor_plan_schema["properties"])
                            self.assertIn("rooms", bim_floor_plan_schema["properties"])
                            self.assertIn("walls", bim_floor_plan_schema["properties"])
                            self.assertNotIn("rooms", bim_floor_plan_schema.get("required", []))

                            dimension_schema = by_name["vw_create_linear_dimension"].input_schema["properties"]["dimension_type"]
                            self.assertEqual(dimension_schema["minimum"], 0)
                            self.assertEqual(dimension_schema["maximum"], 2)

                            contract_checked = True
                            ping = await session.call_tool("vw_ping", {})
                            self.assertTrue(ping.is_error)
                            ping_text = ping.content[0].text
                            self.assertIn("Connection error:", ping_text)
                            self.assertIn("127.0.0.1:1", ping_text)
                            self.assertEqual(ping.structured_content, {"result": ping_text})
                except BaseExceptionGroup as exc:
                    if not _all_broken_resource_errors(exc):
                        raise

        try:
            anyio.run(run_contract)
        except BaseExceptionGroup as exc:
            if not contract_checked or not _all_broken_resource_errors(exc):
                raise


if __name__ == "__main__":
    unittest.main()
