"""Targeted semantic checks; no historical DPS runs or full regression."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from service import Workspace, ROOT

class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT/'.scratch').mkdir(exist_ok=True)
        cls.tmp=tempfile.TemporaryDirectory(prefix='bd-editor-',dir=ROOT/'.scratch')
        cls.w=Workspace(storage=cls.tmp.name)
    @classmethod
    def tearDownClass(cls):
        cls.w.c.close();cls.w.a.close();cls.tmp.cleanup()
    def new(self):
        return self.w.save({'name':'测试','build_spec':self.w.new_spec(1,1,100)},True)
    def test_persist_isolation_and_conflict(self):
        a=self.new();b=copy.deepcopy(a);b.pop('id');b.pop('version');b=self.w.save(b,True)
        b['build_spec']['character']['level']=90;b=self.w.save(b)
        self.assertEqual(self.w.get(a['id'])['build_spec']['character']['level'],100)
        stale=copy.deepcopy(b);b=self.w.save(b)
        with self.assertRaisesRegex(ValueError,'另一窗口'):self.w.save(stale)
    def test_export_import_preserves_unknown_data(self):
        a=self.new();a['build_spec']['extensions']['future']={'unknown':[1,2,3]};a=self.w.save(a,True)
        b=json.loads(json.dumps(a));b.pop('id');b.pop('version');b=self.w.save(b,True)
        self.assertEqual(b['build_spec']['extensions']['future'],{'unknown':[1,2,3]})
        self.assertTrue(list((self.w.config_path(a['id'])/'revisions').glob('*/config.json')))
    def test_all_nodes_and_edges_exposed(self):
        t=self.w.tree('tree:letools:LESkillTrees:bh2')
        self.assertEqual(len(t['nodes']),t['node_count']);self.assertGreater(len(t['edges']),0)
        ids={n['entity_id'] for n in t['nodes']}
        self.assertTrue(all(e['from_node_entity_id'] in ids and e['to_node_entity_id'] in ids for e in t['edges']))
    def test_affix_ranges_and_invalid_input(self):
        item=next(x for x in self.w.item_list('head',1,1,100,kind='base') if not x['unique'])
        opts=self.w.affixes(item['base_type']);a=next(x for x in opts if x['special']==0)
        req={'definition':item['id'],'slot':'head','class_id':1,'mastery':1,'level':100,'ir':[128]*3,'ur':[],'affixes':[{'id':a['id'],'tier':a['ranges'][0]['tier'],'roll':255,'channel':'normal'}]}
        it=self.w.make_item(req)
        self.assertTrue(it['affixes']['normal'][0]['value']['values'])
        req['affixes'][0]['roll']=256
        with self.assertRaises(ValueError):self.w.make_item(req)
        req['affixes'][0]['roll']=255;req['affixes'].append(copy.deepcopy(req['affixes'][0]))
        with self.assertRaisesRegex(ValueError,'重复'):self.w.make_item(req)
    def test_blessings_timeline_filter(self):
        rows=self.w.item_list('blessing',1,1,100,timeline='1');self.assertGreater(len(rows),0)
        wrong=next((x for x in self.w.item_list('blessing',1,1,100) if x['id'] not in {v['id'] for v in rows}),None)
        self.assertIsNotNone(wrong)
        with self.assertRaises(ValueError):self.w.make_item({'definition':wrong['id'],'slot':'blessing','level':100,'class_id':1,'mastery':1,'position':{'timeline':'1'}})
    def test_illegal_node_points_are_not_hidden(self):
        a=self.new();t=self.w.tree(a['build_spec']['passive_tree']['tree']['entity_id']);n=next(n for n in t['nodes'] if n['max_points'])
        a['build_spec']['passive_tree']['nodes']=[{'ref':{'entity_id':n['entity_id']},'points':n['max_points']+1}]
        self.assertTrue(any(p['severity']=='error' and '越界' in p['message'] for p in self.w.validate(a['build_spec'])))
    def test_path_and_noninteger_rejected(self):
        with self.assertRaises(ValueError):self.w.get('../outside')
        with self.assertRaises(ValueError):self.w.new_spec(True,0,100)
    def test_unchanged_save_reuses_snapshot(self):
        a=self.new();b=self.w.save(copy.deepcopy(a),True)
        self.assertEqual(a['revision'],b['revision'])
        self.assertEqual(a['build_spec']['meta']['revision'],b['build_spec']['meta']['revision'])
    def test_calculation_export_uses_edited_values(self):
        from le_build.calculation import build_calculation_input
        a=self.new();item=next(x for x in self.w.item_list('head',1,1,100,kind='base') if not x['unique'])
        inst=self.w.make_item({'definition':item['id'],'slot':'head','class_id':1,'mastery':1,'level':100,'ir':[255]*3,'ur':[],'affixes':[]})
        a['build_spec']['equipment']['slots']=[{'slot':'head','instance':inst}];a=self.w.save(a,True)
        calc=build_calculation_input(a['build_spec'])
        self.assertEqual(calc['build_id'],'editor:'+a['id'])
        self.assertTrue(calc['mods']);self.assertTrue(any(m['value_numeric'] is not None for m in calc['mods']))
        self.assertNotIn('head',a['build_spec']['equipment']['absent_slots'])
        self.assertEqual(a['build_spec']['readable_summary']['items'],1)

if __name__=='__main__':unittest.main()
