"""User-reported regressions: names, raw metadata, scope, budgets, waist."""
import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from service import Workspace,ROOT,SLOTS
from skill_budget import budgets
from player_projection import player_text

class PlayerRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w=Workspace();cls.specs=[]
        for case in ('black-hole-beta33-native-20261111','black-hole-beta33-v4-20261112'):
            p=ROOT/'data/builds/letools'/case
            if not (p/'current.json').exists():continue
            current=json.loads((p/'current.json').read_text(encoding='utf-8'))
            cls.specs.append(json.loads((p/'revisions'/current['revision_id']/'build.json').read_text(encoding='utf-8')))
    @classmethod
    def tearDownClass(cls):cls.w.c.close();cls.w.a.close()
    def require_specs(self):
        if len(self.specs)!=2:self.skipTest('User-selected local BuildSpecs are not available')
    def test_scope_is_ability_identity_not_tag_mask(self):
        scope=self.w.catalog.skill_level_scope(15,1)
        self.assertEqual(scope['label'],'Black Hole');self.assertEqual(scope['kind'],'skill')
        self.assertEqual(self.w.catalog.skill_level_scope(0,5)['label'],'Level of Acolyte Skills')
        self.assertEqual(self.w.catalog.skill_level_scope(8,0)['label'],'Level of Fire Skills')
    def test_no_player_metadata_and_keeps_named_effect(self):
        self.assertEqual(player_text('{"value":1,"property":21}'),'')
        self.assertEqual(player_text("You gain '{'Haste'}'."),'You gain Haste.')
        t=self.w.tree('tree:letools:LECharTrees.trees:mg-1')
        self.assertFalse(any('{' in s or '}' in s for n in t['nodes'] for s in [n['description'],*n['effects']]))
        self.assertEqual(next(n for n in t['nodes'] if n['node_index']==0)['name'],'Arcanist')
    def test_real_build_budgets_and_relic_projection(self):
        self.require_specs()
        for spec in self.specs:
            with self.subTest(origin=spec['meta']['build_id']):
                original=copy.deepcopy(spec);view=self.w.decorate({'build_spec':spec})
                bh=view['skill_budgets']['tree:letools:LESkillTrees:bh2']
                self.assertEqual(bh['minimum'],37);self.assertEqual(bh['maximum'],37)
                self.assertIn('+6 to Black Hole',[l['text'] for l in view['presentation']['items']['equipment.relic']['lines']])
                self.assertFalse(any('技能点数预算' in p['message'] for p in view['validation']))
                self.assertEqual(original,spec,'Player projection must not mutate archived input')
    def test_removing_level_item_reveals_real_overallocation(self):
        self.require_specs();spec=copy.deepcopy(self.specs[0]);spec['equipment']['slots']=[x for x in spec['equipment']['slots'] if x['slot']!='relic']
        bh=budgets(spec,self.w.catalog)['tree:letools:LESkillTrees:bh2']
        self.assertEqual(bh['maximum'],31)
        self.assertTrue(any('Black Hole' in p['message'] and '技能点数预算' in p['message'] for p in self.w.validate(spec)))
    def test_waist_and_english_are_canonical(self):
        self.require_specs();self.assertIn('waist',SLOTS)
        for spec in self.specs:
            waist=next(e for e in spec['equipment']['slots'] if e['slot']=='waist')
            self.assertEqual(self.w.name(waist['instance']['base']['entity_id']),'Noble Sash')
        self.assertEqual(self.w.names['affix:letools:affixList.singleAffixes:13'],'Fire Resistance')

if __name__=='__main__':unittest.main()
