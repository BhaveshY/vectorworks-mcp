import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def _all_broken_resource_errors(exc: BaseException) -> bool:
    if isinstance(exc, anyio.BrokenResourceError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return all(_all_broken_resource_errors(item) for item in exc.exceptions)
    return False


class McpStdioContractTests(unittest.TestCase):
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
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                            initialized = await session.initialize()
                            self.assertEqual(initialized.serverInfo.name, "Vectorworks 2024/2025")

                            tools = await session.list_tools()
                            by_name = {tool.name: tool for tool in tools.tools}
                            self.assertGreaterEqual(len(by_name), 25)
                            for name in (
                                "vw_ping",
                                "vw_preflight_for_cad",
                                "vw_create_object",
                                "vw_batch_create_objects",
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

                            get_objects_schema = by_name["vw_get_objects"].inputSchema
                            self.assertEqual(get_objects_schema["additionalProperties"], False)
                            self.assertEqual(get_objects_schema["properties"]["limit"]["minimum"], 1)
                            self.assertEqual(get_objects_schema["properties"]["limit"]["maximum"], 1000)

                            drawing_summary_schema = by_name["vw_drawing_summary"].inputSchema
                            self.assertIn("include_examples", drawing_summary_schema["properties"])
                            self.assertEqual(drawing_summary_schema["properties"]["example_limit"]["minimum"], 0)
                            self.assertEqual(drawing_summary_schema["properties"]["example_limit"]["maximum"], 100)
                            self.assertEqual(drawing_summary_schema["properties"]["scan_limit"]["maximum"], 100000)

                            lookup_schema = by_name["vw_lookup_objects"].inputSchema
                            self.assertIn("detail", lookup_schema["properties"])
                            self.assertIn("fields", lookup_schema["properties"])
                            self.assertEqual(lookup_schema["properties"]["limit"]["maximum"], 1000)

                            batch_property_schema = by_name["vw_batch_set_object_properties"].inputSchema
                            self.assertEqual(batch_property_schema["properties"]["edits"]["minItems"], 1)
                            self.assertEqual(batch_property_schema["properties"]["edits"]["maxItems"], 100)
                            self.assertIn("items", batch_property_schema["properties"]["edits"])
                            self.assertEqual(batch_property_schema["properties"]["edits"]["items"]["required"], ["ref", "properties"])
                            self.assertFalse(batch_property_schema["properties"]["edits"]["items"]["additionalProperties"])
                            self.assertIn("fillColor", batch_property_schema["properties"]["edits"]["items"]["properties"]["properties"]["properties"])
                            self.assertEqual(batch_property_schema["properties"]["lookup_limit"]["maximum"], 1000)

                            agent_context_schema = by_name["vw_agent_context"].inputSchema
                            self.assertIn("profile", agent_context_schema["properties"])
                            self.assertIn("include_examples", agent_context_schema["properties"])

                            worksheet_schema = by_name["vw_worksheet"].inputSchema
                            self.assertEqual(worksheet_schema["properties"]["row"]["minimum"], 1)
                            self.assertEqual(worksheet_schema["properties"]["col"]["minimum"], 1)
                            self.assertEqual(worksheet_schema["properties"]["num_rows"]["maximum"], 500)

                            slab_points_schema = by_name["vw_create_slab"].inputSchema["properties"]["points"]
                            self.assertEqual(slab_points_schema["minItems"], 3)
                            self.assertEqual(slab_points_schema["items"]["minItems"], 2)
                            self.assertEqual(slab_points_schema["items"]["maxItems"], 2)

                            batch_objects_schema = by_name["vw_batch_create_objects"].inputSchema["properties"]["objects"]
                            self.assertEqual(batch_objects_schema["minItems"], 1)
                            self.assertEqual(batch_objects_schema["maxItems"], 250)
                            self.assertIn("atomic", by_name["vw_batch_create_objects"].inputSchema["properties"])

                            create_object_type = by_name["vw_create_object"].inputSchema["properties"]["object_type"]
                            self.assertIn("rectangle", create_object_type["enum"])
                            self.assertIn("box", create_object_type["enum"])

                            floor_plan_rooms_schema = by_name["vw_create_schematic_floor_plan"].inputSchema["properties"]["rooms"]
                            self.assertEqual(floor_plan_rooms_schema["minItems"], 1)
                            self.assertEqual(floor_plan_rooms_schema["maxItems"], 100)
                            self.assertIn("atomic", by_name["vw_create_schematic_floor_plan"].inputSchema["properties"])

                            bim_floor_plan_schema = by_name["vw_create_bim_floor_plan"].inputSchema
                            self.assertIn("wall_height", bim_floor_plan_schema["properties"])
                            self.assertIn("dimension_rooms", bim_floor_plan_schema["properties"])
                            self.assertIn("rooms", bim_floor_plan_schema["properties"])
                            self.assertIn("walls", bim_floor_plan_schema["properties"])
                            self.assertNotIn("rooms", bim_floor_plan_schema.get("required", []))

                            dimension_schema = by_name["vw_create_linear_dimension"].inputSchema["properties"]["dimension_type"]
                            self.assertEqual(dimension_schema["minimum"], 0)
                            self.assertEqual(dimension_schema["maximum"], 2)

                            contract_checked = True
                            ping = await session.call_tool("vw_ping", {})
                            self.assertFalse(ping.isError)
                            ping_text = ping.content[0].text
                            self.assertIn("Connection error:", ping_text)
                            self.assertIn("127.0.0.1:1", ping_text)
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
