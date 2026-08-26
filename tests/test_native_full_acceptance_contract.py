import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-native-full-acceptance.py"


def load_acceptance_module():
    spec = importlib.util.spec_from_file_location("native_full_acceptance", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load native full acceptance module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeFullAcceptanceContractTests(unittest.TestCase):
    def test_fixture_prefix_leaves_room_for_longest_vectorworks_object_name(self):
        module = load_acceptance_module()
        prefix = module.fixture_prefix("20260821-163719-49e31e9b")
        full_name = prefix + module.LONGEST_FIXTURE_OBJECT_SUFFIX

        self.assertLessEqual(len(full_name), module.VECTORWORKS_OBJECT_NAME_LIMIT)
        self.assertTrue(prefix.startswith("VW_MCP_P4_"))

    def test_generic_parametric_fixture_uses_a_freestanding_plugin(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('"query": "Stake Object"', source)
        self.assertIn('"plugin_name": "Stake Object"', source)

    def test_document_open_explicitly_accepts_disposable_fixture_replacement(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertGreaterEqual(
            source.count('"replace_dirty_confirmation": "REPLACE_DIRTY_DOCUMENT"'),
            3,
        )


if __name__ == "__main__":
    unittest.main()
