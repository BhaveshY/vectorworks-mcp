"""Container boundaries and deformation policy, independent of the native bridge."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


class SectionContainers(unittest.TestCase):
    def setUp(self):
        self.states={
            'chain':{'type_id':86,'parent_uuid':'layer','plugin':'NNA_ChainDim'},
            'source':{'type_id':11,'parent_uuid':'chain'},
            'measure':{'type_id':63,'parent_uuid':'source','points':[[0,0],[0,3500]],'dimension_class':0,'show_value':True},
            'display':{'type_id':63,'parent_uuid':'chain','points':[[0,0],[0,3500]],'dimension_class':0,'show_value':True},
            'panel':{'type_id':15,'parent_uuid':'layer','points':[[100,4000]],'bounds':[[0,4100],[200,3900]],'symbol':'Solar','symbol_type':0},
        }
        for uid,s in self.states.items(): s.update(uuid=uid,type=str(s['type_id']))
        self.writes=[]
        api=SimpleNamespace(
            GetTypeN=lambda h:self.states.get(h,{}).get('type_id',0),
            FInGroup=lambda h:{'chain':'source','source':'measure'}.get(h),
            NextObj=lambda h:{'source':'display'}.get(h),
            GetParent=lambda h:self.states[h]['parent_uuid'],
            GetObjectVariableBoolean=lambda h,n:False,
            ResetObject=lambda h:None,
        )
        ns={'vs':api,'uuid_of':lambda h:h,'_nil':lambda h:h is None,
            'plugin_of':lambda h:self.states[h].get('plugin',''),
            'resolve':lambda h:h}
        code=(Path(__file__).resolve().parents[1]/'vectorworks_mcp/scripts_lib/_section.py').read_text()
        exec(compile(code,'_section.py','exec'),ns)
        ns['section_state']=lambda h:copy.deepcopy(self.states[h])
        ns['section_plane']=lambda h:None
        def set_points(h,state,points):
            self.writes.append(h); self.states[h]['points']=copy.deepcopy(points)
            return copy.deepcopy(self.states[h])
        ns['section_set_points']=set_points
        self.ns=ns

    def edit(self, **overrides):
        item={'expected_geometry':{'panel':self.ns['section_revision'](['panel'])},
              'bands':[{'bottom':0,'top':3500,'delta':500}],
              'stairs':[],'fixed_points':[],'labels':[],'preserve_text':[],**overrides}
        self.ns['section_edit'](item,['panel'],[])

    def test_chain_walk_uses_persistent_source_not_generated_display(self):
        self.assertEqual(self.ns['section_walk'](['chain']),['chain','source','measure'])
        revision=self.ns['section_revision'](self.ns['section_walk'](['chain']))
        self.states['display']['points'][1][1]=9999
        self.assertEqual(revision,self.ns['section_revision'](self.ns['section_walk'](['chain'])))
        self.states['measure']['points'][1][1]=4000
        self.assertNotEqual(revision,self.ns['section_revision'](self.ns['section_walk'](['chain'])))

    def test_unknown_chain_source_layout_fails(self):
        self.states['source']['type_id']=63
        with self.assertRaisesRegex(ValueError,'source layout'):
            self.ns['section_walk'](['chain'])

    def test_display_dimension_is_not_an_editable_source(self):
        with self.assertRaisesRegex(ValueError,'generated chain display'):
            self.ns['section_check']('display',self.states['display'])

    def test_roof_symbol_moves_rigidly(self):
        self.edit()
        self.assertEqual(self.states['panel']['points'],[[100,4500]])
        self.assertEqual(self.writes,['panel'])

    def test_cross_band_symbol_requires_explicit_rigid_binding(self):
        self.states['panel'].update(points=[[100,3400]],bounds=[[0,3600],[200,3200]])
        with self.assertRaisesRegex(ValueError,'crosses a deformation band'): self.edit()
        self.assertEqual(self.writes,[])
        self.edit(rigid=[{'target':'panel','elevation':3500}])
        self.assertEqual(self.states['panel']['points'],[[100,3900]])

    def test_hybrid_symbol_rejected_before_mutation(self):
        self.states['panel']['symbol_type']=2
        with self.assertRaisesRegex(ValueError,'hybrid/3D'): self.edit()
        self.assertEqual(self.writes,[])

    def test_opposing_bands_do_not_hide_interior_symbol_deformation(self):
        self.states['panel'].update(points=[[100,-100]],bounds=[[0,4100],[200,-100]])
        with self.assertRaisesRegex(ValueError,'crosses a deformation band'):
            self.edit(bands=[{'bottom':0,'top':1000,'delta':500},{'bottom':2000,'top':3000,'delta':-500}])
        self.assertEqual(self.writes,[])

    def test_conflicting_rigid_and_fixed_bindings_rejected(self):
        with self.assertRaisesRegex(ValueError,'cannot overlap'):
            self.edit(rigid=[{'target':'panel','elevation':3500}],fixed_points=[{'target':'panel','vertex':1}])
        self.assertEqual(self.writes,[])


if __name__=='__main__': unittest.main()
