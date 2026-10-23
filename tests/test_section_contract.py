"""Host validation: malformed section requests must fail before reaching a live document."""
import unittest

from vectorworks_mcp.builders import compile_edit
from vectorworks_mcp.errors import VWError


class SectionContract(unittest.TestCase):
    def setUp(self):
        self.change = {"op":"section_heights", "target":"name:Section",
                       "expected_geometry":{"name:Section":"0"*64},
                       "bands":[{"bottom":0,"top":3.5,"delta":.5}],
                       "labels":[{"target":"name:Level","elevation":3.88,"datum":110.69}],
                       "stairs":[{"target":"name:Flight","bottom":0,"top":3.88}]}

    def compile(self, change=None):
        return compile_edit([change or self.change], 1000, schemas={}, descriptors={})

    def test_unit_conversion_and_single_transaction(self):
        plan = self.compile()
        self.assertFalse(plan.ops)
        self.assertEqual(len(plan.script_ops), 1)
        item = plan.script_ops[0]
        self.assertEqual(item["bands"], [{"bottom":0,"top":3500,"delta":500}])
        self.assertEqual(item["labels"][0]["datum"], 110690)
        self.assertEqual(item["stairs"][0]["top"], 3880)

    def test_reject_malformed_before_host_binding(self):
        cases = [({"bands":[]}), {"bands":[{"bottom":0,"top":0,"delta":1}]},
                 {"bands":[{"bottom":0,"top":2,"delta":-2}]},
                 {"bands":[{"bottom":0,"top":2,"delta":1},{"bottom":1,"top":3,"delta":1}]},
                 {"bands":[{"bottom":0,"top":float("inf"),"delta":1}]},
                 {"expected_geometry":{}}, {"labels":["bad"]}, {"fixed_points":[42]},
                 {"stairs":[{"target":"name:Flight","bottom":1,"top":0}]}]
        for override in cases:
            with self.subTest(override=override), self.assertRaises(VWError):
                self.compile(dict(self.change, **override))

    def test_forbid_mixed_native_script_transactions(self):
        with self.assertRaises(VWError):
            compile_edit([self.change,{"op":"move","target":"name:Other","by":[1,0]}],1,schemas={},descriptors={})

    def test_reshape_points_units_and_finiteness(self):
        plan = self.compile({"op":"reshape","target":"name:Polygon","points":[[0,0],[2,0],[2,3]]})
        self.assertEqual(plan.script_ops[0]["points"],[[0,0],[2000,0],[2000,3000]])
        with self.assertRaises(VWError):
            self.compile({"op":"reshape","target":"name:Polygon","points":[[0,0],[float("nan"),0]]})

    def test_shifted_origin_localized_labels_and_rigid_units(self):
        self.change['labels'][0].update(origin=-60.86,decimal_separator=',')
        self.change['rigid']=[{'target':'name:Marker','elevation':-56.98}]
        item=self.compile().script_ops[0]
        self.assertEqual(item['labels'][0]['origin'],-60860)
        self.assertEqual(item['labels'][0]['decimal_separator'],',')
        self.assertEqual(item['rigid'][0]['elevation'],-56980)
        self.change['labels'][0]['decimal_separator']=';'
        with self.assertRaises(VWError): self.compile()

    def test_zero_sign(self):
        self.assertEqual(self.compile().script_ops[0]['labels'][0]['zero_sign'],'+')
        self.change['labels'][0]['zero_sign']='±'
        self.assertEqual(self.compile().script_ops[0]['labels'][0]['zero_sign'],'±')
        self.change['labels'][0]['zero_sign']='0'
        with self.assertRaises(VWError): self.compile()


if __name__ == "__main__":
    unittest.main()
