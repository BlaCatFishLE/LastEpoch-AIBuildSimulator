"""首接边界回归：来源归一、显式模型、冻结输入、服务身份。"""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import le_commands as cli
from le_pipeline import onboarding, save_adapter
from _common import PipelineError


class SaveAdapterTest(unittest.TestCase):
    def fixture(self):
        save = {'savedItems': [{'containerID': 33, 'formatVersion': 2,
            'data': [6,0,0,127,255,34,116,0,0,255,17,0,0,0]}]}
        wire = {'bio': {'level': 100, 'characterClass': 1, 'chosenMastery': 1},
                'dataVersion': 'Version 1.5.2', 'skillTrees': [{'treeID':'bh2'}],
                'blessings': {'1': {'id':'example', 'ir':0}}}
        return save, wire

    def test_wire_normalization_preserves_identity_without_mutation(self):
        save, wire = self.fixture()
        before = copy.deepcopy(wire)
        got = save_adapter.normalize_wire(wire, save)
        self.assertEqual(got['data']['blessings']['1'], {'id':'example', 'ir':[255,17,0]})
        self.assertEqual(got['level'], 100)
        self.assertEqual(got['data_version'], 'Version 1.5.2')
        self.assertEqual(wire, before)

    def test_raw_roll_zero_is_preserved(self):
        save, wire = self.fixture()
        item = next(i for i in save['savedItems'] if i['containerID'] == 33)
        item['data'][9:12] = [0, 1, 255]
        self.assertEqual(save_adapter.normalize_wire(wire, save)['data']['blessings']['1']['ir'], [0, 1, 255])

    def test_unknown_layout_is_rejected(self):
        save, wire = self.fixture()
        next(i for i in save['savedItems'] if i['containerID'] == 33)['data'][0] = 99
        with self.assertRaises(PipelineError):
            save_adapter.normalize_wire(wire, save)

    def test_duplicate_blessing_is_rejected(self):
        save, wire = self.fixture()
        save['savedItems'].append(copy.deepcopy(next(i for i in save['savedItems'] if i['containerID'] == 33)))
        with self.assertRaises(PipelineError):
            save_adapter.normalize_wire(wire, save)

    def test_missing_version_is_rejected(self):
        save, wire = self.fixture()
        wire.pop('dataVersion')
        with self.assertRaises(PipelineError):
            save_adapter.normalize_wire(wire, save)

    def test_missing_external_dependencies_are_explicit(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=True), mock.patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(PipelineError, 'needs_input'):
                save_adapter.dependencies(Path(d), {})


class OnboardingEntryTest(unittest.TestCase):
    def test_unknown_skill_stops_before_creating_case(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.scratch') as d:
            target = Path(d) / 'unknown/case.json'
            args = cli.build_parser().parse_args(['import', '--case', str(target), '--save', 'missing',
                                                  '--main-skill', 'unknown', '--model', 'black-hole'])
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.cmd_import(args), 2)
            self.assertFalse(target.parent.exists())

    def test_existing_case_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.scratch') as d:
            target = Path(d) / 'case.json'
            target.write_text('original')
            args = cli.build_parser().parse_args(['import', '--case', str(target), '--save', 'missing'])
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.cmd_import(args), 1)
            self.assertEqual(target.read_text(), 'original')

    def test_complete_entry_freezes_source_before_parse(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.scratch', prefix='onboard-test-') as d:
            source = Path(d) / 'source-save'
            content = b'EPOCH{"savedItems":[{}]}'
            source.write_bytes(content)
            target = Path(d) / 'fresh/case.json'
            args = cli.build_parser().parse_args(['import', '--case', str(target), '--save', str(source),
                                                  '--main-skill', 'bh2', '--model', 'black-hole'])
            observed = {}
            def fake_child(argv, **kw):
                frozen = Path(argv[argv.index('--save') + 1])
                observed['save'] = frozen
                self.assertNotEqual(frozen, source)
                self.assertEqual(frozen.read_bytes(), content)
                out = Path(argv[argv.index('--out') + 1])
                (out / 'bd.json').write_text('{}')
                return argparse.Namespace(returncode=0)
            with mock.patch.object(save_adapter, 'dependencies', return_value=(Path('node'), Path('tool'))), \
                 mock.patch.object(cli, 'run_child', side_effect=fake_child), \
                 mock.patch.object(onboarding, 'complete', return_value=0), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_import(args), 0)
            self.assertEqual(source.read_bytes(), content)
            self.assertTrue(observed['save'].is_file())

    def test_partial_is_not_accepted_as_success_by_default(self):
        with self.assertRaises(PipelineError):
            onboarding.checked(lambda _: 3, None)
        self.assertEqual(onboarding.checked(lambda _: 3, None, allowed=(0, 3)), 3)


class NewCaseServiceTest(unittest.TestCase):
    def test_service_uses_selected_case_and_run(self):
        from le_calc import serve
        args = argparse.Namespace(snapshot='snapshot', ruleset='rules', calc_input='calc', build='bd',
                                  registry='registry', monster_level=100, run_id='new-run', port=9023)
        with mock.patch.dict(os.environ, {'LE_CASE_ID': 'fresh-case'}, clear=True), \
             mock.patch('le_calc.cli._load_snapshot'), mock.patch.object(serve, 'load_ruleset'), \
             mock.patch.object(serve, 'ScenarioReportService') as constructor:
            serve.load_service(args)
        self.assertEqual(constructor.call_args.kwargs['case_id'], 'fresh-case')
        self.assertEqual(constructor.call_args.kwargs['run_id'], 'new-run')

    def test_report_ref_identity_fallback(self):
        from le_calc import serve
        args = argparse.Namespace(snapshot='snapshot', ruleset='rules', calc_input='calc', build='bd',
                                  registry=None, monster_level=100, run_id='new-run', port=9023)
        with mock.patch.dict(os.environ, {'LE_REPORT_REF': json.dumps({'case_id': 'ref-case'})}, clear=True), \
             mock.patch('le_calc.cli._load_snapshot'), mock.patch.object(serve, 'load_ruleset'), \
             mock.patch.object(serve, 'ScenarioReportService') as constructor:
            serve.load_service(args)
        self.assertEqual(constructor.call_args.kwargs['case_id'], 'ref-case')


if __name__ == '__main__':
    unittest.main()
