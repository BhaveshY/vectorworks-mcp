"""Level label formatting and chain reshape readback, independent of the native bridge."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


class SectionLabels(unittest.TestCase):
    def setUp(self):
        # Ground floor (zero level) is drawn at Y=3000, the basement floor at Y=0.
        self.states={
            'gf':{'type_id':10,'parent_uuid':'layer','points':[[0,3000]],'text':'+0.00'},
            'ug':{'type_id':10,'parent_uuid':'layer','points':[[0,0]],'text':'-3.00'},
            'roof':{'type_id':10,'parent_uuid':'layer','points':[[0,9000]],'text':'+6.00'},
            'chain':{'type_id':86,'parent_uuid':'layer','plugin':'NNA_ChainDim'},
            'source':{'type_id':11,'parent_uuid':'chain'},
            'measure':{'type_id':63,'parent_uuid':'source','points':[[0,0],[0,3500]],'dimension_class':0,'show_value':True},
        }
        for uid,s in self.states.items(): s.update(uuid=uid,type=str(s['type_id']))
        self.regenerate=None
        def reset(h):
            if h=='chain' and self.regenerate: self.regenerate()
        api=SimpleNamespace(
            GetTypeN=lambda h:self.states.get(h,{}).get('type_id',0),
            FInGroup=lambda h:{'chain':'source','source':'measure'}.get(h),
            NextObj=lambda h:None,
            GetParent=lambda h:self.states[h]['parent_uuid'],
            GetObjectVariableBoolean=lambda h,n:False,
            ResetObject=reset,
            SetText=lambda h,t:self.states[h].__setitem__('text',t),
            GetText=lambda h:self.states[h]['text'],
        )
        ns={'vs':api,'uuid_of':lambda h:h,'_nil':lambda h:h is None,
            'plugin_of':lambda h:self.states.get(h,{}).get('plugin',''),
            'resolve':lambda h:h}
        code=(Path(__file__).resolve().parents[1]/'vectorworks_mcp/scripts_lib/_section.py').read_text(encoding='utf-8')
        exec(compile(code,'_section.py','exec'),ns)
        ns['section_state']=lambda h:copy.deepcopy(self.states[h])
        ns['section_plane']=lambda h:None
        def set_points(h,state,points):
            self.states[h]['points']=copy.deepcopy(points)
            return copy.deepcopy(self.states[h])
        ns['section_set_points']=set_points
        self.ns=ns

    def label(self, uid, elevation, **extra):
        return {'target':uid,'elevation':elevation,'datum':0,'origin':0,'decimal_separator':'.',
                'zero_sign':'+','template':'{level}','precision':2,**extra}

    def edit(self, bands, labels):
        roots=['gf','ug','roof']
        item={'expected_geometry':{h:self.ns['section_revision']([h]) for h in roots},'bands':bands,
              'stairs':[],'fixed_points':[],'rigid':[],'labels':labels,'preserve_text':[]}
        self.ns['section_edit'](item,roots,[])
        return {h:self.states[h]['text'] for h in roots}

    def test_zero_level_stays_reference_when_band_below_changes(self):
        texts=self.edit([{'bottom':0,'top':3000,'delta':500}],
                        [self.label(h,e,origin=3000,decimal_separator=',',zero_sign='±') for h,e in (('gf',3000),('ug',0),('roof',9000))])
        self.assertEqual(texts,{'gf':'±0,00','ug':'-3,50','roof':'+6,00'})

    def test_absolute_keeps_zero_level_height(self):
        labels=[self.label(h,e,origin=3000,datum=100000,template='{level} = {absolute} m NN') for h,e in (('gf',3000),('ug',0),('roof',9000))]
        texts=self.edit([{'bottom':0,'top':3000,'delta':500}],labels)
        self.assertEqual(texts,{'gf':'+0.00 = 103.00 m NN','ug':'-3.50 = 99.50 m NN','roof':'+6.00 = 109.00 m NN'})

    def test_band_above_origin_matches_unmapped_formula(self):
        labels=[self.label(h,e,datum=110690,template='{level} = {absolute} m NN') for h,e in (('gf',3000),('ug',0),('roof',9000))]
        texts=self.edit([{'bottom':3000,'top':6000,'delta':500}],labels)
        self.assertEqual(texts,{'gf':'+3.00 = 113.69 m NN','ug':'+0.00 = 110.69 m NN','roof':'+9.50 = 120.19 m NN'})

    def test_negative_zero_is_formatted_as_zero(self):
        labels=[self.label('gf',3000,origin=3000.001,zero_sign='±'),self.label('ug',0),self.label('roof',9000)]
        self.assertEqual(self.edit([{'bottom':6000,'top':9000,'delta':500}],labels)['gf'],'±0.00')

    def test_reshape_chain_source_is_verified_after_chain_reset(self):
        def revert(): self.states['measure']['points']=[[0,0],[0,3500]]
        self.regenerate=revert
        with self.assertRaisesRegex(ValueError,'chain regeneration changed the reshaped source'):
            self.ns['reshape_geometry']({'start':[0,0],'end':[0,4000]},['measure'],[])
        self.regenerate=None
        done=[]
        self.ns['reshape_geometry']({'start':[0,0],'end':[0,4000]},['measure'],done)
        self.assertEqual(self.states['measure']['points'],[[0,0],[0,4000]])
        self.assertTrue(done[0]['verified'])


if __name__=='__main__': unittest.main()
