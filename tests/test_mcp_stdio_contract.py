import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


class McpStdioContractTests(unittest.TestCase):
    def test_server_starts_over_stdio_and_exposes_expected_contract(self):
        contract_completed = False

        async def run_contract():
            nonlocal contract_completed
            env = os.environ.copy()
            env.update(
                {
                    "VW_MCP_HOST": "127.0.0.1",
                    "VW_MCP_PORT": "1",
                    "VW_MCP_TIMEOUT": "0.5",
                    "VW_MCP_HEALTH_TIMEOUT": "0.2",
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
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, "Vectorworks 2024/2025")

                        tools = await session.list_tools()
                        by_name = {tool.name: tool for tool in tools.tools}
                        self.assertGreaterEqual(len(by_name), 25)
                        for name in ("vw_ping", "vw_preflight_for_cad", "vw_create_object", "vw_get_layers"):
                            self.assertIn(name, by_name)

                        get_objects_schema = by_name["vw_get_objects"].inputSchema
                        self.assertEqual(get_objects_schema["additionalProperties"], False)
                        self.assertEqual(get_objects_schema["properties"]["limit"]["minimum"], 1)
                        self.assertEqual(get_objects_schema["properties"]["limit"]["maximum"], 1000)

                        worksheet_schema = by_name["vw_worksheet"].inputSchema
                        self.assertEqual(worksheet_schema["properties"]["row"]["minimum"], 1)
                        self.assertEqual(worksheet_schema["properties"]["col"]["minimum"], 1)
                        self.assertEqual(worksheet_schema["properties"]["num_rows"]["maximum"], 500)

                        slab_points_schema = by_name["vw_create_slab"].inputSchema["properties"]["points"]
                        self.assertEqual(slab_points_schema["minItems"], 3)
                        self.assertEqual(slab_points_schema["items"]["minItems"], 2)
                        self.assertEqual(slab_points_schema["items"]["maxItems"], 2)

                        ping = await session.call_tool("vw_ping", {})
                        self.assertFalse(ping.isError)
                        ping_text = ping.content[0].text
                        self.assertIn("Connection error:", ping_text)
                        self.assertIn("127.0.0.1:1", ping_text)

                        contract_completed = True

        try:
            anyio.run(run_contract)
        except* anyio.BrokenResourceError:
            if not contract_completed:
                raise


if __name__ == "__main__":
    unittest.main()
