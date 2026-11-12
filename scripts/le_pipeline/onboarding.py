"""新存档首接编排。数值逻辑由既有领域模块/阶段图负责。"""
from __future__ import annotations

import argparse
import json
import re
import time

from _common import (PipelineError, calculation_input_counts, load_json,
                     relative, sha256_file, write_json)
from . import save_adapter


def preflight(api, args):
    if args.model != 'research' and (args.main_skill, args.model) != ('bh2', 'black-hole'):
        raise PipelineError('完整首接 V1 仅支持显式 --main-skill bh2 --model black-hole；其它技能先使用骨架导入/研究流程')
    if not args.main_skill and args.model != 'research':
        raise PipelineError('完整首接 V1 必须声明用户已确认的 --main-skill；research 可先导入后选主技能')
    if not args.save or args.planner:
        raise PipelineError('完整首接 V1 使用 --save（可选 --stash）；planner 混合路线请使用既有骨架流程')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', api.resolve(api.ROOT, args.case).parent.name):
        raise PipelineError('新案例目录名只允许字母、数字、下划线、短横线，长度 1–96')
    if not 1 <= args.port <= 65535:
        raise PipelineError('端口必须在 1–65535')
    target = api.resolve(api.ROOT, args.case)
    if target.name != 'case.json':
        raise PipelineError('完整首接案例入口必须命名为 case.json')
    if (api.ROOT / 'data/builds/letools' / target.parent.name).exists():
        raise PipelineError('该案例名称已存在 BuildSpec；请使用新案例目录名，避免采用历史构筑')
    save_adapter.dependencies(api.ROOT, api.load_config(), tool_home=args.tool_home)
    save = api.resolve(api.ROOT, args.save)
    raw = save.read_text(encoding='utf-8-sig')
    if not raw.startswith('EPOCH'):
        raise PipelineError('角色存档缺少 EPOCH 头')
    data = json.loads(raw[5:])
    # 技能必须存在于当前输入，不按文件名或旧案例推断。
    # specialization 在正式 export 后再次检查。
    if not isinstance(data, dict) or not data.get('savedItems'):
        raise PipelineError('角色存档缺少 savedItems')


def _control_default(entry):
    """场景控制的中性默认：只用于用户未提供的非必需增益；布尔 false、数值 0。"""
    if "default" in entry and entry["default"] is not None:
        return entry["default"]
    kind = str(entry.get("type") or "").lower()
    if kind in ("boolean", "bool"):
        return False
    if kind in ("integer", "int"):
        return 0
    if kind in ("number", "float"):
        return 0.0
    return None


def _scenario_target(case, template):
    target = {
        "monster_level": int(case.get("monster_level") or 100),
        "resistance": 0.0,
    }
    rules = (template.get("definition") or {}).get("rules") or {}
    dot_target = rules.get("dot_target") if isinstance(rules.get("dot_target"), dict) else {}
    if dot_target.get("monster_damage_reduction_control"):
        target["monster_damage_reduction"] = float(
            dot_target.get("monster_damage_reduction_default", 0.0)
        )
    return target


def _generate_scenarios(api, root, base, case, template, candidate):
    """按当前输入/定义中性默认生成场景；不复制历史案例 scenario/corrections/expected。"""
    schema = template.get("scenario_schema") or {}
    controls = {
        str(entry["key"]): _control_default(entry)
        for entry in schema.get("controls") or []
        if isinstance(entry, dict) and entry.get("key")
    }
    target = _scenario_target(case, template)
    notes = [
        "场景由当前存档输入与定义中性默认生成；未复制历史案例数据。",
        "布尔控制中性 false、数值控制 0；用户可显式覆盖后重跑，不伪造未确认目标条件。",
        "目标抗性 0.0 为中性假设；怪物等级取 case.monster_level；减伤按定义证据默认。",
    ]
    definition = template.get("definition") or {}
    evaluations = definition.get("evaluations") or []
    documents = []
    if evaluations:
        for evaluation in evaluations:
            evaluation_id = str(evaluation.get("evaluation_id"))
            document = {
                "schema": "le-skill-scenario-v3",
                "scenario_id": f"{case['case_id']}-{evaluation_id}-v3",
                "template_id": template["template_id"],
                **target,
                "controls": dict(controls),
                "assumptions": list(notes),
                "notes": list(notes),
            }
            path = base / "inputs" / f"scenario-{evaluation_id}-v3.json"
            write_json(path, document)
            documents.append({"evaluation_id": evaluation_id, "path": relative(path, root)})
    else:
        document = {
            "schema": "le-skill-scenario-v3",
            "scenario_id": f"{case['case_id']}-v3",
            "template_id": template["template_id"],
            **target,
            "controls": dict(controls),
            "assumptions": list(notes),
            "notes": list(notes),
        }
        path = base / "inputs" / "scenario-v3.json"
        write_json(path, document)
        documents.append({"path": relative(path, root)})
    return documents


_PREPARE_ORDER = ("prepare_snapshot", "resolve_skills", "compile_plan", "min_validate")
_RUN_ORDER = ("simulate", "validate_result", "render_report", "commit", "serve")


def _project_onboarding_status(route, prepare_context, prepare_code, candidate_report):
    """状态/readiness/blocked_stages 由实际 route + prepare/compile 结果投影。"""
    engine = str(route.get("engine") or "")
    context = prepare_context or {}
    stage_results = [item for item in context.get("stage_results") or [] if isinstance(item, dict)]
    executed = {
        str(item.get("stage_id"))
        for item in stage_results
        if str(item.get("execution_status")) in ("completed", "ready", "ok", "partial")
    }
    stage_order = (
        ("resolve_skills", "compile_plan", "min_validate", *_RUN_ORDER)
        if engine == "unified_dps"
        else (*_PREPARE_ORDER, *_RUN_ORDER)
    )
    blocked = [name for name in stage_order if name not in executed]
    candidate_status = str((candidate_report or {}).get("status") or "not_discovered")
    diagnostics = list(route.get("diagnostics") or [])
    if engine == "needs_research":
        status = "needs_research"
    elif engine in ("needs_input", "unsupported_route") or route.get("status") in ("needs_input", "route_conflict"):
        status = "rejected" if route.get("status") == "route_conflict" else "needs_input"
    elif prepare_code not in (0, 3):
        status = "failed"
    else:
        readiness = str(context.get("readiness") or "")
        if readiness == "ready":
            status = "prepared"
        elif readiness == "partial":
            status = "partial"
        elif readiness == "needs_research":
            status = "needs_research"
        elif readiness == "needs_input":
            status = "needs_input"
        else:
            status = "partial"
    if status == "prepared":
        next_actions = ["run"]
        blocked = []
    elif status == "partial":
        next_actions = ["run"]
        blocked = []  # coverage partial 不是阻断；真实缺失由 diagnostics/next_actions 表达
    elif status == "failed":
        next_actions = ["修复 prepare/compile 失败后重跑；不覆盖历史产物"]
    elif status == "needs_input":
        next_actions = ["提供缺失输入/主技能/场景事实后重跑 prepare"]
    else:
        next_actions = ["进入当前案例研究队列；补齐定义能力或知识后再绑定"]
    return {
        "status": status,
        "readiness": context.get("readiness"),
        "prepare_exit_code": prepare_code,
        "blocked_stages": blocked,
        "next_actions": next_actions,
        "candidate_status": candidate_status,
        "route_status": route.get("status"),
        "route_engine": engine,
        "diagnostics": diagnostics,
    }


def _derive_main_skill_facts(root, case, calc, ability):
    """从当前输入的 dataset catalog 与真实节点 convert 事实生成适用性事实。"""
    dataset_ref = case.get('dataset') or 'data/current.json'
    dataset_path = root / str(dataset_ref) if isinstance(dataset_ref, str) else None
    pointer = load_json(dataset_path) if dataset_path and dataset_path.is_file() else {}
    database = root / str(pointer.get('database') or 'data/releases/phase1-dataset-003/data.sqlite')
    tags: list[str] = []
    component_types: list[str] = []
    if database.is_file():
        import sqlite3
        from le_calc import model as calc_model
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                'select entity_id, tags_raw from skills where source_skill_id = ?', (ability,)
            ).fetchone()
            if row:
                tags = [str(item) for item in calc_model.decode_tags(int(row[1] or 0))]
                for component in connection.execute(
                    'select damage_type from skill_components where skill_entity_id = ?', (row[0],)
                ):
                    if component[0]:
                        component_types.append(str(component[0]))
        finally:
            connection.close()
    premises: list[str] = []
    for spec in (calc.get('skills') or {}).get('specializations') or []:
        if not str((spec.get('tree') or {}).get('entity_id') or '').endswith(':' + ability):
            continue
        for node in spec.get('nodes') or []:
            if int(node.get('points') or 0) > 0 and node.get('name'):
                premises.append(str(node['name']))
    return {
        'damage_tags': tags,
        'component_damage_types': sorted(set(component_types)),
        'conversion_premises': premises,
    }


def complete(api, args, *, started):
    root = api.ROOT
    target = api.resolve(root, args.case)
    case = load_json(target)
    base = target.parent
    rows = [{'step': 'freeze_and_parse_save', 'status': 'ok',
             'elapsed_s': round(time.perf_counter() - started, 6)}]
    report = {'schema': 'le-onboarding-report-v1', 'case_id': case['case_id'],
              'status': 'running', 'steps': rows, 'numeric_golden': False}
    report_path = base / 'onboarding-report.json'

    def save_report():
        report['elapsed_s'] = round(time.perf_counter() - started, 6)
        write_json(report_path, report)

    def step(name, fn):
        tick = time.perf_counter()
        row = {'step': name, 'status': 'running'}
        rows.append(row)
        try:
            value = fn()
            row['status'] = 'ok'
            return value
        except Exception as exc:
            row.update(status='failed', message=str(exc))
            raise
        finally:
            row['elapsed_s'] = round(time.perf_counter() - tick, 6)
            save_report()

    def child(cmd, *, modules, allowed=(0,)):
        result = api.run_child(cmd, pythonpath=[api.MODULE_PATHS[m] for m in modules])
        if result.returncode not in allowed:
            raise PipelineError(f'子命令失败：exit {result.returncode}')
        return result.returncode

    def store_case():
        write_json(target, case)

    try:
        api._le_calc_modules()
        # cmd_import 已从冻结来源生成 bd；这里从不复制其它案例。
        case.update(model=args.model, main_skill={'ability_id': args.main_skill},
                    title=f"{case['case_id']} · Black Hole",
                    description='正式新存档首接：显式模型，空人工补值，未确认折射不放大。',
                    onboarding={'schema': 'le-save-onboarding-v1', 'model_selection': 'explicit',
                                'numeric_golden': False})
        dataset = load_json(root / case['dataset'])
        case['dataset_expected'] = {k: dataset[k] for k in (
            'dataset_id', 'logical_content_sha256', 'file_sha256', 'target_game_version')}
        case['game_version'] = dataset['target_game_version']
        for key, id_key in [('ruleset', 'ruleset_id'), ('conditional_registry', 'registry_id')]:
            if args.model == 'research':
                case[key] = {}
                continue
            path = root / case[key]['path']
            doc = load_json(path)
            case[key].update(id=doc.get(id_key) or doc.get('id'), sha256=sha256_file(path))
        source = case['source']
        wire = step('decode_wire', lambda: save_adapter.decode(
            api, root / source['character_save'], base / 'inputs/tool-native', tool_home=args.tool_home))
        source.update(tool_native_wire=relative(wire, root),
                      tool_native_run_summary=relative(wire.parent / 'run-summary.json', root),
                      save_extract=relative(base / 'inputs/save-extract.json', root),
                      decoded_items=relative(base / 'inputs/decoded-items.json', root))
        code = step('normalize_build', lambda: child(
            [api._python(), '-m', 'le_build', 'import', '--file', str(wire),
             '--share-id', case['case_id'], '--offline'], modules=['le_build'], allowed=(0, 3)))
        report['build_import_exit_code'] = code
        build_dir = root / 'data/builds/letools' / case['case_id']
        current = load_json(build_dir / 'current.json')
        spec_path = build_dir / 'revisions' / current['revision_id'] / 'build.json'
        case['build'] = {k: current[k] for k in ('build_id', 'revision_id', 'revision_seq', 'content_sha256')}
        case['build'].update(build_dir=relative(build_dir, root), build_spec=relative(spec_path, root),
                             build_spec_sha256=sha256_file(spec_path))
        inputs = case['inputs']
        export = base / 'inputs/calculation-input.json'
        step('export_calc_input', lambda: child([
            api._python(), '-m', 'le_build', 'export', '--build', current['build_id'],
            '--revision', current['revision_id'], '--out', str(export)], modules=['le_build']))
        calc = load_json(export)
        specs = (calc.get('skills') or {}).get('specializations') or []
        if args.main_skill and not any(
            str((s.get('tree') or {}).get('entity_id', '')).endswith(':' + args.main_skill)
            for s in specs
        ):
            raise PipelineError(f'当前存档没有 {args.main_skill} 专精，拒绝套用该技能')
        case['snapshot_expectations'] = calculation_input_counts(calc)
        corrections = base / 'inputs/corrections.json'
        selection = base / 'inputs/idol-refraction-selection.json'
        write_json(selection, {'build_id': current['build_id'], 'refracted_idols': [],
                              'altar_prefix_effect': 0.0, 'altar_suffix_effect': 0.0,
                              'status': 'assumed', 'note': '当前输入未确认折射；显式使用无放大假设。'})
        write_json(corrections, {'schema': 'le-case-corrections-v1', 'case_id': case['case_id'],
            'corrections': [], 'refraction': {'status': 'assumed', 'selection_file': relative(selection, root),
            'refracted_idols': [], 'altar_prefix_effect': 0.0, 'altar_suffix_effect': 0.0}})
        inputs.update(calculation_input=relative(export, root), corrections=relative(corrections, root),
                      idol_refraction_selection=relative(selection, root))
        store_case()
        scripts = root / 'tools/save_import/scripts/pipeline'
        affix = base / 'inputs/calculation-input.affix-db.json'
        corrected = base / 'inputs/calculation-input.corrected.json'
        def script(name, argv):
            return child([api._python(), str(scripts / name), '--case', str(target), *argv],
                         modules=['le_calc', 'le_save_import'])
        step('apply_affix_ranges', lambda: script('run_affix_db_pipeline.py', [
            '--calc-input', str(export), '--out', str(affix),
            '--report', str(base / 'inputs/affix-report.json')]))
        step('apply_corrections', lambda: script('apply_corrections.py', [
            '--input', str(affix), '--out', str(corrected),
            '--report', str(base / 'inputs/corrections-report.json')]))
        inputs['calculation_input'] = relative(corrected, root)
        # 首次结构声明来自 export，防止修正阶段意外增删来源；非数值金标准。
        store_case()
        if args.model == 'research':
            from le_calc import calculation_route
            from le_calc.workflow.portable import sanitize_directory
            # 主技能稳定事实来自当前输入（不是历史案例/文件名）。
            main_skill = dict(case.get('main_skill') or {})
            main_skill['ability_id'] = args.main_skill
            spec = next((item for item in specs
                         if str(((item.get('tree') or {}).get('entity_id') or '')).endswith(':' + args.main_skill)), None)
            if spec:
                owner = spec.get('owner_skill') or {}
                if owner.get('name'):
                    main_skill['name'] = owner['name']
            character = calc.get('character') or {}
            class_info = character.get('class') if isinstance(character.get('class'), dict) else {}
            mastery_info = character.get('mastery') if isinstance(character.get('mastery'), dict) else {}
            if class_info.get('name'):
                main_skill['class'] = class_info['name']
            if mastery_info.get('name'):
                main_skill['mastery'] = mastery_info['name']
            if args.main_skill:
                main_skill.update(_derive_main_skill_facts(root, case, calc, args.main_skill))
            case['main_skill'] = main_skill
            case['title'] = f"{case['case_id']} · {main_skill.get('name') or args.main_skill or 'Research'}"
            sanitize_directory(base / 'inputs', root)
            case['input_sha256'] = {k: sha256_file(root / v) for k, v in {**inputs, **source}.items()
                                   if v and (root / v).is_file()}
            store_case()
            report['candidate'] = calculation_route.discover_definition_candidates(case, root=root)
            report['route'] = calculation_route.resolve_calculation_route(case, root=root)
            candidates = report['candidate']
            if not args.main_skill:
                report['status'] = 'needs_input'
                report['readiness'] = 'needs_input'
                report['next_actions'] = ['select_main_skill', 'prepare']
                report['blocked_stages'] = ['candidate_binding']
                report['preflight_note'] = '已导入数据；未选主技能，选择后再执行 prepare。'
                write_json(target, case)
                write_json(base / 'onboarding-report.json', report)
                print('新存档数据已导入；尚未选择主技能。下一步：显式选择主技能后 prepare。')
                return 3
            context = {'readiness': 'needs_research', 'stage_results': []}
            prep_code = None
            prep_id = None
            if candidates.get('status') == 'applicable_single':
                selected = candidates['selected']
                template = load_json(root / selected['path'])
                source_doc = {'kind': 'definitions_v3', 'template': selected['path']}
                scenarios = _generate_scenarios(api, root, base, case, template, selected)
                if len(scenarios) > 1:
                    source_doc['scenarios'] = [
                        {'evaluation_id': item['evaluation_id'], 'path': item['path']} for item in scenarios
                    ]
                else:
                    source_doc['scenario'] = scenarios[0]['path']
                case['calculation'] = {'engine': 'unified_dps', 'source': source_doc}
                case['notes'] = [
                    'canonical 新案例：由当前真实存档独立导入，未复制历史案例 data/模型。',
                    '场景由当前输入与定义中性默认生成；假设见 inputs/scenario-*.json 的 assumptions。',
                    '本案例尚未生成/发布报告；run 使用正式阶段链，需显式调用。',
                ]
                sanitize_directory(base / 'inputs', root)
                case['input_sha256'] = {k: sha256_file(root / v) for k, v in {**inputs, **source}.items()
                                       if v and (root / v).is_file()}
                store_case()
                report['binding'] = {
                    'template': selected['path'],
                    'template_revision': selected.get('revision'),
                    'capabilities': selected.get('capabilities'),
                    'scenario_refs': scenarios,
                    'build': case.get('build'),
                    'calculation_input': case['inputs'].get('calculation_input'),
                }
                step('validate_data', lambda: checked(api.cmd_validate, argparse.Namespace(case=str(target))))
                prep_id = 'prepare-' + api._fresh_run_id(case)
                prep_args = api.build_parser().parse_args(['prepare', '--case', str(target), '--run-id', prep_id])
                prep_code = step('prepare_analysis', lambda: checked(api.cmd_prepare, prep_args, allowed=(0, 3)))
                context_path = root / case['output']['root'] / prep_id / 'prepare-context.json'
                context = load_json(context_path) if context_path.is_file() else {'readiness': 'partial', 'stage_results': []}
                plan_path = root / case['output']['root'] / prep_id / 'calculation-plan.json'
                if plan_path.is_file():
                    report['plan_ref'] = relative(plan_path, root)
            else:
                report['binding'] = None
                # Unknown skills still receive a bound, inspectable preparation context.
                step('validate_data', lambda: checked(api.cmd_validate, argparse.Namespace(case=str(target)), allowed=(0, 3)))
                prep_id = 'prepare-' + api._fresh_run_id(case)
                prep_args = api.build_parser().parse_args(['prepare', '--case', str(target), '--run-id', prep_id])
                prep_code = step('prepare_analysis', lambda: checked(api.cmd_prepare, prep_args, allowed=(0, 3)))
                context_path = root / case['output']['root'] / prep_id / 'prepare-context.json'
                context = load_json(context_path) if context_path.is_file() else {
                    'readiness': 'needs_research', 'stage_results': [],
                }
            report['route'] = calculation_route.resolve_calculation_route(case, root=root)
            projection = _project_onboarding_status(report['route'], context, prep_code, candidates)
            report.update(projection)
            report.update(case_ref=relative(target, root),
                          analysis_ref=f"{case['output']['root']}/{prep_id}" if prep_id else None,
                          numeric_golden=False)
            if projection['status'] == 'prepared':
                print(f"新存档数据接入+prepare 完成：{report['analysis_ref']}；下一步显式 run（import 不隐式发布）。")
                return 0
            if projection['status'] == 'partial':
                print(f"新存档数据接入完成，prepare 部分完成：{report['analysis_ref']}；下一步 run。")
                return 3
            print(f"新存档数据已独立准备；状态={projection['status']}（{report['route'].get('status')}）；"
                  f"候选={candidates.get('status')}；见 {relative(report_path, root)}")
            return 3
        snapshot = base / 'snapshot/input-snapshot.json'
        step('prepare_snapshot', lambda: script('make_snapshot.py', [
            '--calc-input', str(corrected), '--out', str(snapshot)]))
        inputs['snapshot'] = relative(snapshot, root)
        from le_calc.conditional.registry import load_registry
        from le_calc.conditional.scenario import default_scenario
        registry = load_registry(root / case['conditional_registry']['path'])
        binding = {'build_id': current['build_id'], 'revision_id': current['revision_id'],
                   'build_content_sha256': current['content_sha256'], 'calc_input_sha256': sha256_file(corrected),
                   'dataset_id': dataset['dataset_id'], 'registry_id': registry.registry_id,
                   'registry_sha256': case['conditional_registry']['sha256'],
                   'ruleset_id': case['ruleset']['id'], 'ruleset_sha256': case['ruleset']['sha256']}
        scenario = default_scenario(registry, {'binding': binding, 'target': {
            'monster_level': 100, 'fire_resistance': 0.0, 'boss': True, 'rare': True}})
        scenario_path = base / 'scenario/conditional-scenario.json'
        write_json(scenario_path, scenario)
        inputs['scenario'] = relative(scenario_path, root)
        expected_path = base / 'expected/expected-summary.json'
        write_json(expected_path, {'schema': 'le-case-expected-v1', 'case_id': case['case_id'],
            'tolerance_rel': 1e-9, 'run_expectations': {'status': 'assumption',
            'ruleset_id': case['ruleset']['id'], 'consistency_all_pass': True,
            'conditional_context_status': 'ready', 'conditional_baseline_profile': 'build_base',
            'conditional_applied_effects': 0, 'conditional_status': 'ok'},
            'checks': [{'name': '保留模型假设状态', 'file': 'result.json',
                        'pointer': '/run/status', 'expected': 'assumption'}],
            'note': '首次结构与状态验收；无独立伤害金标准，未将本次伤害输出写作预期。'})
        case['expected'] = relative(expected_path, root)
        case['input_sha256'] = {k: sha256_file(root / v) for k, v in {**inputs, **source}.items()
                               if v and (root / v).is_file()}
        case['notes'] = ['主技能与模型显式选择；知识匹配状态请读 analysis-plan/research-queue。',
                         '人工修正为空；折射未确认暂不放大；expected 仅结构与状态。']
        store_case()
        from le_calc.workflow.portable import sanitize_directory
        sanitize_directory(base / 'inputs', root)
        # 归一 bd 来源文本后刷新指纹；计算输入如含来源路径也同步快照。
        for key, path in {**inputs, **source}.items():
            if path and (root / path).is_file():
                case['input_sha256'][key] = sha256_file(root / path)
        store_case()
        step('validate', lambda: checked(api.cmd_validate, argparse.Namespace(case=str(target))))
        prep_id = 'prepare-' + api._fresh_run_id(case)
        prep_args = api.build_parser().parse_args(['prepare', '--case', str(target), '--run-id', prep_id])
        prep_code = step('prepare_analysis', lambda: checked(api.cmd_prepare, prep_args, allowed=(0, 3)))
        context_path = root / case['output']['root'] / prep_id / 'prepare-context.json'
        context = load_json(context_path) if context_path.is_file() else {'readiness': 'partial', 'stage_results': []}
        route = api._route_for_case(case)
        projection = _project_onboarding_status(route, context, prep_code, None)
        report.update(projection)
        report.update(knowledge_readiness=context.get('readiness'), case_ref=relative(target, root),
                      analysis_ref=f"{case['output']['root']}/{prep_id}", numeric_golden=False)
        plan_path = root / case['output']['root'] / prep_id / 'calculation-plan.json'
        if plan_path.is_file():
            report['plan_ref'] = relative(plan_path, root)
        if projection['status'] == 'prepared':
            print(f"新存档数据接入+prepare 完成：{report['analysis_ref']}；下一步显式 run（import 不隐式发布）。")
            return 0
        print(f"新存档接入 prepare 完成（{projection['status']}）；下一步 run。")
        return 3
    except Exception as exc:
        report.update(status='failed', error=str(exc))
        api.fail(f'新存档接入失败：{exc}；诊断保留 {relative(report_path, root)}')
        return 1
    finally:
        save_report()


def checked(fn, args, *, allowed=(0,)):
    code = fn(args)
    if code not in allowed:
        raise PipelineError(f'{fn.__name__}: exit {code}')
    return code
