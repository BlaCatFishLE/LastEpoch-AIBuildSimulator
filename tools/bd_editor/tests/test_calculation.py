"""Focused tests for the editor/engine boundary, not the historic BD suite."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools/bd_editor'))
from service import Workspace
from calculation import CalculationManager,NeedsResearch,document_key,normalized_spec,scenario_for,read,choose_template,skill_facts

class CalculationBoundaryTests(unittest.TestCase):
    def test_fingerprint_uses_values_points_and_conditions_not_player_names(self):
        doc={'build_spec':{'meta':{'revision':1},'character':{'name':'A','level':100},'specializations':[{'nodes':[{'name':'Craterborn','points':4}]}]},'conditions':{'states':{'buff':False},'labels':{'buff':'Buff'}}}
        renamed=copy.deepcopy(doc);renamed['build_spec']['character']['name']='B';renamed['conditions']['labels']['buff']='另一名称'
        self.assertEqual(document_key(doc),document_key(renamed))
        renamed['build_spec']['specializations'][0]['nodes'][0]['points']=3
        self.assertNotEqual(document_key(doc),document_key(renamed))
        changed=copy.deepcopy(doc);changed['conditions']['states']['buff']=True
        self.assertNotEqual(document_key(doc),document_key(changed))

    def test_unconsumed_controls_and_channels_are_rejected(self):
        from conditions import normalize
        scene=normalize({'states':{'unknown':True}},{'controls':[],'derived':{}})
        with self.assertRaises(NeedsResearch):scenario_for(scene,[])
        scene['states']={};scene['target']['resistances']['Cold']=50
        with self.assertRaises(NeedsResearch):scenario_for(scene,[])

    def test_relative_storage_resolves_inside_project(self):
        workspace=Workspace(ROOT,storage='.scratch/editor-relative-storage-test')
        try:self.assertEqual(workspace.storage,ROOT/'.scratch/editor-relative-storage-test')
        finally:workspace.c.close();workspace.a.close()

    def test_current_class_is_required_for_template_selection(self):
        workspace=Workspace(ROOT)
        try:
            source=next((read(p) for p in (ROOT/'data/editor-workspace').glob('*/current.json') if 'black-hole-beta33-v4' in read(p).get('name','')),None)
            if source is None:self.skipTest('Current user BD not available')
            sys.path.insert(0,str(ROOT/'tools/phase3/src'))
            facts={'ability_id':'bh2',**skill_facts(workspace,source['build_spec'],'bh2')}
            _,template=choose_template(ROOT,'bh2',{'main_skill':facts});self.assertEqual(template['ability_id'],'bh2')
            facts['class']='Rogue'
            with self.assertRaises(NeedsResearch):choose_template(ROOT,'bh2',{'main_skill':facts})
        finally:workspace.c.close();workspace.a.close()

    def test_current_build_runtime_rebind_and_research_paths(self):
        root=ROOT;source=next((read(p) for p in (root/'data/editor-workspace').glob('*/current.json') if 'black-hole-beta33-v4' in read(p).get('name','')),None)
        if source is None:self.skipTest('Current user BD not available; no historical fallback')
        with tempfile.TemporaryDirectory(prefix='editor-calc-test-',dir=root/'.scratch') as storage:
            w=Workspace(root,storage=storage);manager=CalculationManager(w)
            def evaluate(doc):
                request={'id':doc['id'],'version':doc['version'],'client_session_id':'test-session-'+str(time.time_ns()),'client_edit_revision':1,'request_id':'test'}
                info=manager.start(request);manager.jobs[doc['id']].result(timeout=90)
                out=manager.status(doc['id']);self.assertNotEqual(out['status'],'failed',out.get('message'))
                return out,manager.paths(doc['id'],info['evaluation_id'])
            try:
                doc=w.save({'name':'Current BD boundary test','build_spec':source['build_spec'],'conditions':{'main_skill':'bh2'}},True)
                original=w.get(doc['id'])
                spec,corrections=normalized_spec(w,doc['build_spec']);spec2,corrections2=normalized_spec(w,spec)
                self.assertTrue(corrections);self.assertFalse(corrections2)
                first,first_path=evaluate(doc);self.assertEqual(first['status'],'completed');self.assertEqual(first['route'],'B')
                self.assertGreater(first['dps'],0)
                if first['coverage']['status']=='partial':
                    self.assertIn('research_url',first);self.assertTrue(read(first_path/'research-task.json')['diagnostics'])
                self.assertEqual(w.get(doc['id'])['build_spec'],original['build_spec'])
                old_result=(first_path/'result.json').read_bytes()
                changed=copy.deepcopy(doc);changed['conditions']['states']['craterborn_active']=True;doc=w.save(changed)
                self.assertTrue(manager.status(doc['id'])['stale'])
                buff,buff_path=evaluate(doc);self.assertEqual(buff['route'],'A');self.assertGreater(buff['dps'],first['dps'])
                binding=read(buff_path/'report-binding.json')
                self.assertTrue(binding['read_only']);self.assertTrue(binding['scenario_binding']['scenario']['states']['craterborn_active'])
                html=(buff_path/'dps-report.html').read_text(encoding='utf-8')
                self.assertIn('天赋与完整配置',html);self.assertIn('返回编辑器调整',html)
                # Fresh compile must agree with the reused runtime plan for the same changed state.
                independent=w.save({'name':'Fresh binding check','build_spec':doc['build_spec'],'conditions':doc['conditions']})
                rebound,_=evaluate(independent);self.assertEqual(rebound['route'],'B');self.assertAlmostEqual(buff['dps'],rebound['dps'],places=7)
                # Equipment changes rebind the actual current input rather than stale profile stats.
                changed=copy.deepcopy(doc);changed['build_spec']['equipment']['slots']=[s for s in changed['build_spec']['equipment']['slots'] if s['slot']!='weapon1'];doc=w.save(changed)
                gear,_=evaluate(doc);self.assertEqual(gear['route'],'B');self.assertLess(gear['dps'],buff['dps'])
                changed=copy.deepcopy(doc);changed['conditions']['states']['unmapped_test_condition']=True;doc=w.save(changed)
                research,research_path=evaluate(doc);self.assertEqual(research['status'],'needs_research');self.assertEqual(research['route'],'C');self.assertNotIn('dps',research)
                self.assertTrue((research_path/'research-task.json').is_file());self.assertIsNotNone(research.get('previous_success'))
                self.assertEqual((first_path/'result.json').read_bytes(),old_result)
                w.set_deleted(doc['id'],doc['version'])
                with self.assertRaises(ValueError):manager.status(doc['id'])
                with self.assertRaises(ValueError):manager.artifact(doc['id'],first['evaluation_id'],'dps-report.html')
            finally:
                manager.pool.shutdown(wait=True);w.c.close();w.a.close()

if __name__=='__main__':unittest.main()
