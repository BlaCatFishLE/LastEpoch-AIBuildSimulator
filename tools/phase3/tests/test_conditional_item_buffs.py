"""Focused current-source/stack boundary checks; no historic case reproduction."""
import copy
import json
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/phase3/src'))
from le_calc.conditional_node_buffs import bind
from le_calc.effect_binding import bind_profile_effects
from le_calc.definitions_v3 import _validate_control_value,DefinitionBindingError
from le_calc.plan_handlers import implementation_fingerprint

class ConditionalItemBuffTests(unittest.TestCase):
    def make_calc(self):
        item={'instance_id':'idols.current','definition_entity_id':'item:letools:uniqueList.uniques:211','mods':[]}
        for index,text in [(5,'20 Maximum Stacks of Ambition'),(2,'2% more Cold Damage per stack of Ambition'),(1,'2% more Fire Damage per stack of Ambition')]:
            item['mods'].append({'mod_id':f'idols.current#unique_description#{index}','item_instance_id':item['instance_id'],'kind':'description','readable_text':text})
        return {'items':[item],'mods':copy.deepcopy(item['mods'])}

    def definition(self,calc):
        d={'controls':[],'effects':[],'conditional_buff_library':'tools/phase3/rules/shared-conditional-node-buffs-v1.json'}
        context=SimpleNamespace(root=ROOT,calc=calc,input_refs=[])
        bind(d,context);d['controls_by_key']={c['key']:c for c in d['controls']}
        return d,context

    def test_current_identity_stack_values_and_scopes(self):
        calc=self.make_calc();d,context=self.definition(calc)
        self.assertEqual([c['key'] for c in d['controls']],['ambition'])
        self.assertEqual(d['controls'][0]['max'],20)
        # Real mod indexes, not source array positions; no fixed historic idol slot.
        self.assertEqual([e['source']['description_index'] for e in d['effects']],[1,2])
        for stacks,value in [(0,0),(20,.4)]:
            effects=bind_profile_effects(calc,None,d,states={'ambition':stacks},target={'boss':False},pool_lookup=lambda *a:0)
            self.assertEqual(len(effects),2)
            self.assertEqual([e.contribution.damage_tags for e in effects],[['Fire'],['Cold']])
            for effect in effects:self.assertAlmostEqual(effect.contribution.value,value)
        for invalid in (-1,21,True,1.5):
            with self.assertRaises(DefinitionBindingError):_validate_control_value(d['controls'][0],invalid,label='test')
        self.assertIn('tools/phase3/rules/shared-conditional-item-buffs-v1.json',[r['path'] for r in context.input_refs])
        self.assertIn('tools/phase3/src/le_calc/conditional_item_buffs.py',implementation_fingerprint()['compiler_files'])

    def test_absent_mismatched_and_duplicate_sources(self):
        calc=self.make_calc();calc['items'][0]['definition_entity_id']='item:other'
        d,_=self.definition(calc);self.assertEqual(d['effects'],[]);self.assertEqual(d['controls'],[])
        calc=self.make_calc();calc['items'][0]['mods'][0]['readable_text']='Unknown maximum'
        with self.assertRaises(ValueError):self.definition(calc)
        calc=self.make_calc();duplicate=copy.deepcopy(calc['items'][0]);duplicate['instance_id']='idols.other';calc['items'].append(duplicate)
        d,context=self.definition(calc);self.assertEqual(len(d['effects']),2)
        bind(d,context);self.assertEqual(len(d['effects']),2);self.assertEqual(len(d['controls']),1)
        self.assertEqual(d['_bound_conditional_item_buffs'][0]['available_instances'],['idols.current','idols.other'])

    def test_declared_effect_reaches_current_dot_execution(self):
        # Explicit current input supplied by the caller; never select a historic case.
        ref=os.environ.get('LE_EDITOR_EVALUATION')
        if not ref:self.skipTest('Set LE_EDITOR_EVALUATION to the current evaluation directory')
        path=ROOT/ref
        plan=json.loads((path/'calculation-plan.json').read_text(encoding='utf-8'))
        result=json.loads((path/'result.json').read_text(encoding='utf-8'))
        from le_calc.plan_v4 import execute_plan_v4
        effects=[e for e in plan['execution']['effects'] if str(e.get('rule_id','')).startswith('item.throne_of_ambition.')]
        self.assertEqual(len(effects),2,'Declared effects must reach the actual execution plan')
        self.assertTrue(all(e['value_mode']=='stack_scale' for e in effects))
        scene=copy.deepcopy(result['scenario']);scene['states']['ambition']=0
        zero=execute_plan_v4(plan,scene)
        twenty_scene=copy.deepcopy(scene);twenty_scene['states']['ambition']=20
        twenty=execute_plan_v4(plan,twenty_scene)
        self.assertTrue(math.isclose(twenty['totals']['horizon_dps']/zero['totals']['horizon_dps'],1.4,rel_tol=1e-10))
        self.assertTrue(math.isclose(twenty['totals']['horizon_dps'],result['totals']['horizon_dps'],rel_tol=1e-10))
        self.assertTrue(any('点燃继承' in str(item) for item in result.get('assumptions',[])))
        ledger=[c for c in result['contributions'] if 'Throne of Ambition' in str(c.get('source_label')) and c['stat_key']=='damage' and c['operation']=='more']
        self.assertEqual(len(ledger),2)
        self.assertTrue(all(math.isclose(c['value'],.4) for c in ledger))
        self.assertTrue(all(c['application']['components'] for c in ledger))
        self.assertIn('tools/phase3/rules/shared-conditional-item-buffs-v1.json',[r['path'] for r in plan['input_refs']])
        binding=json.loads((path/'report-binding.json').read_text(encoding='utf-8'))
        self.assertEqual(binding['editor_snapshot']['conditions']['states']['ambition'],20)
        self.assertEqual(binding['scenario_binding']['scenario']['states']['ambition'],20)
        self.assertTrue(json.loads((path/'validation-result.json').read_text(encoding='utf-8'))['passed'])
        print('Current execution: Ambition 0 → 20 ratio = 1.40, DPS =',twenty['totals']['horizon_dps'])

if __name__=='__main__':unittest.main()
