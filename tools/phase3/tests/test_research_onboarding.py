"""新存档隔离与未知技能门控回归。"""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import le_commands as cli
from le_pipeline import onboarding
from le_calc.workflow import contract, stage

class ResearchOnboardingTests(unittest.TestCase):
    def test_research_snapshot_never_calls_any_damage_adapter(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.scratch') as d:
            ctx=contract.StageContext(repo_root=ROOT, output_root=Path(d))
            req=contract.StageRequest(stage_id='prepare_snapshot', case_id='fresh', run_id='r', parameters={'main_skill':'v01cv'})
            with mock.patch.object(stage, '_case', return_value={'model':'research'}), mock.patch.object(stage, '_run_step') as child:
                result=stage.stage_prepare_snapshot(req,ctx)
            self.assertEqual(result['execution_status'],'waiting')
            self.assertEqual(result['readiness'],'needs_research')
            self.assertEqual(result['issues'][0]['required_input']['ability_id'],'v01cv')
            child.assert_not_called()

    def test_skeleton_never_inherits_old_default_build_or_rules(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.scratch') as d:
            source=Path(d)/'planner.json';source.write_text('{}')
            target=Path(d)/'fresh/case.json'
            args=cli.build_parser().parse_args(['import','--case',str(target),'--planner',str(source)])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_import(args),3)
            case=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(case['build'],{})
            self.assertEqual(case['ruleset'],{})
            self.assertEqual(case['conditional_registry'],{})

    def test_research_model_cannot_run_or_serve_using_bh_default(self):
        for command in ('run','serve','reproduce'):
            with self.subTest(command=command):
                with self.assertRaisesRegex(cli.PipelineError,'research'):
                    cli._require_known_model({'model':'research','case_id':'fresh'},command=command)

    def test_wrong_skill_model_pair_is_still_rejected(self):
        args=argparse.Namespace(main_skill='v01cv',model='black-hole')
        with self.assertRaises(cli.PipelineError):
            onboarding.preflight(cli,args)
