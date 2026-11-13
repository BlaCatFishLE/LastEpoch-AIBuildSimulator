import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from service import Workspace,ROOT
from conditions import prepare

class ConditionsAndDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT/'.scratch').mkdir(exist_ok=True)
        cls.tmp=tempfile.TemporaryDirectory(prefix='editor-conditions-',dir=ROOT/'.scratch')
        cls.w=Workspace(storage=cls.tmp.name)
    @classmethod
    def tearDownClass(cls):cls.w.c.close();cls.w.a.close();cls.tmp.cleanup()
    def new(self):return self.w.save({'name':'条件测试','build_spec':self.w.new_spec(1,1,100)},True)
    def buff_spec(self):
        s=self.w.new_spec(1,1,100)
        tree='tree:letools:LESkillTrees:me27'
        s['specializations']=[{'slot_number':0,'tree':{'entity_id':tree,'name':'Meteor'},'tree_source_id':'me27','owner_skill':{'entity_id':'skill:letools:LEAbilities.abilityList:me27'},'source_level':20,'nodes':[{'node_id':'25','node_index':25,'points':1,'ref':{'entity_id':'node:letools:me27:25','name':'Craterborn'}}]}]
        return s
    def test_delete_hides_and_restore_keeps_snapshots(self):
        d=self.new();before=list((self.w.config_path(d['id'])/'revisions').glob('*/config.json'));data=[p.read_bytes() for p in before]
        removed=self.w.set_deleted(d['id'],d['version'])
        self.assertNotIn(d['id'],[b['id'] for b in self.w.list()]);self.assertIn(d['id'],[b['id'] for b in self.w.list(deleted=True)])
        with self.assertRaises(ValueError):self.w.get(d['id'])
        restored=self.w.set_deleted(d['id'],removed['version'],False)
        self.assertEqual(restored['build_spec'],d['build_spec']);self.assertEqual([p.read_bytes() for p in before],data)
    def test_deleted_save_cannot_resurrect_and_version_is_checked(self):
        d=self.new();self.w.set_deleted(d['id'],d['version'])
        with self.assertRaises(ValueError):self.w.save(d)
        with self.assertRaises(ValueError):self.w.set_deleted(d['id'],d['version'],False)
        with self.assertRaises(ValueError):self.w.set_deleted('../outside',1)
    def test_conditions_change_revision_without_changing_build(self):
        a=self.new();b=copy.deepcopy(a);b['conditions']['target']['kind']='boss';b['conditions']['target']['resistances']['Fire']=50;b=self.w.save(b,True)
        self.assertNotEqual(a['revision'],b['revision']);self.assertEqual(a['build_spec']['meta']['revision'],b['build_spec']['meta']['revision'])
        self.assertEqual(self.w.get(b['id'])['conditions']['target']['resistances']['Fire'],50)
    def test_copy_export_and_restore_keep_independent_conditions(self):
        a=self.new();b=json.loads(json.dumps(a));b.pop('id');b.pop('version');b['conditions']['horizon_s']=60;b=self.w.save(b,True)
        self.assertEqual(self.w.get(a['id'])['conditions']['horizon_s'],30)
        saved=json.loads((self.w.config_path(b['id'])/'revisions'/b['revision']/'config.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['conditions']['horizon_s'],60)
    def test_available_buff_comes_from_selected_nodes(self):
        spec=self.buff_spec();state,available=prepare(self.w,spec)
        self.assertIn('craterborn_active',[c['key'] for c in available['controls']])
        state['states']['craterborn_active']=True;state,_=prepare(self.w,spec,state)
        spec['specializations']=[];preserved,catalog=prepare(self.w,spec,state)
        self.assertTrue(preserved['states']['craterborn_active']);self.assertNotIn('craterborn_active',[c['key'] for c in catalog['controls']])
    def test_illegal_conditions_rejected_before_save(self):
        d=self.new();original=self.w.get(d['id'])
        for value in (True,0,101,float('nan')):
            changed=copy.deepcopy(d);changed['conditions']['target']['monster_level']=value
            with self.assertRaises(ValueError):self.w.save(changed)
        self.assertEqual(self.w.get(d['id'])['version'],original['version'])
        state,catalog=prepare(self.w,self.buff_spec());state['states']['craterborn_active']=1
        with self.assertRaises(ValueError):prepare(self.w,self.buff_spec(),state)
    def test_derived_conditions_do_not_trust_user_switch(self):
        s=self.w.new_spec(1,1,100);tree=s['passive_tree']['tree']['entity_id']
        s['passive_tree']['nodes']=[{'node_id':'36','node_index':36,'points':4,'ref':{'entity_id':'node:letools:mg-1:36'}}]
        state,catalog=prepare(self.w,s);self.assertIn('frozen_steel_enabled',catalog['derived'])
        state['states']['frozen_steel_enabled']=True;state,_=prepare(self.w,s,state)
        self.assertFalse(state['states']['frozen_steel_enabled'])
        state['states']['enemy_chilled']=True;state,_=prepare(self.w,s,state)
        self.assertTrue(state['states']['frozen_steel_enabled'])

if __name__=='__main__':unittest.main()
