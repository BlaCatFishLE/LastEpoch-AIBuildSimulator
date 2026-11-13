"""Editor orchestration of the shared compiler/executor; no damage formulas here."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def semantic(value):
    """Names, UI projections and revision timestamps do not change calculations."""
    ignored={'meta','readable','presentation','validation','skill_budgets','condition_catalog','labels','name','stat_name','display_value','source_path','editor','bd_editor_origin'}
    if isinstance(value, dict):
        return {k:semantic(v) for k,v in value.items() if k not in ignored}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


def document_key(doc):
    return sha({'build':semantic(doc['build_spec']), 'conditions':semantic(doc['conditions'])})


class NeedsResearch(ValueError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics=diagnostics or []


def normalized_spec(workspace, source):
    """Bind authoritative per-base ranges to current rolls, never scale twice."""
    from le_build import rolls, readable
    from le_save_import.affix_ranges import AffixRangeResolver
    spec=copy.deepcopy(source)
    resolver=AffixRangeResolver(workspace.root/'data/affix-db/v1/normalized/affix-db.sqlite')
    corrections=[]
    try:
        instances=[e['instance'] for e in spec['equipment'].get('slots',[])]+[e['instance'] for e in spec['idols'].get('entries',[])]+[e['instance'] for e in spec['blessings'].get('entries',[])]
        for item in instances:
            base=workspace.items.get((item.get('base') or {}).get('entity_id'),{})
            for entries in item.get('affixes',{}).values():
                for affix in entries:
                    raw=affix.get('roll',{}).get('raw')
                    values=affix.get('value',{}).get('values',[])
                    aid=affix.get('affix',{}).get('source_id')
                    tier=affix.get('source_tier')
                    if not isinstance(raw,int) or not base or aid is None:continue
                    rows=resolver.rows(aid,base.get('base_type'),base.get('extra',{}).get('subTypeId'),tier)
                    for index,value in enumerate(values):
                        matching=[r for r in rows if r['property_id']==value.get('property') and r['tags']==value.get('tags',0) and r['special_tag']==value.get('special_tag',0) and r['mod_type']==value.get('modifier_type')]
                        if len(matching)!=1:continue
                        row=matching[0]
                        if not all(isinstance(row.get(k),(int,float)) for k in ('effective_min','effective_max')):continue
                        stat=workspace.catalog.stat_by_property(row['property_id'],row['tags'],row['special_tag'])
                        restored=rolls.restore_value_from_range(row['effective_min'],row['effective_max'],raw,row['mod_type'],rolls.round_added_to_int(stat))
                        previous=value.get('value_numeric')
                        value.update(restored,display_value=rolls.display_value(restored['value_numeric'],stat,row['mod_type']),value_source='editor_calculation_per_base')
                        if previous!=value['value_numeric']:corrections.append({'affix':affix.get('instance_id'),'index':index,'before':previous,'after':value['value_numeric']})
            item['readable']=readable.build_item_readable(item,workspace.catalog)
        # Exported identity binds this normalized snapshot, without altering editor/import revisions.
        identity=sha(semantic(spec))
        spec.setdefault('meta',{})['revision']={'revision_id':'eval-'+identity[:16],'content_sha256':identity}
        return spec,corrections
    finally:resolver.close()


def skill_facts(workspace, spec, ability):
    from le_calc.model import decode_tags
    row=workspace.c.execute('select entity_id,tags_raw from skills where source_skill_id=?',(ability,)).fetchone()
    if not row:raise NeedsResearch('当前数据库尚未解析这个主技能。')
    components=[r[0] for r in workspace.c.execute('select damage_type from skill_components where skill_entity_id=?',(row[0],)) if r[0]]
    nodes=[n.get('ref',{}).get('name') for s in spec.get('specializations',[]) if s.get('tree',{}).get('entity_id','').endswith(':'+ability) for n in s.get('nodes',[]) if n.get('points',0)>0]
    character=spec['character']
    tree=next((t for t in workspace.trees.values() if t.get('owner_entity_id')==row[0] and t.get('tree_kind')=='skill'),{})
    return {'class':workspace.name(character['class']['ref']['entity_id']),'ability_class':workspace.name(tree['class_entity_id']) if tree.get('class_entity_id') else None,'mastery':workspace.name(character['mastery']['ref']['entity_id']),'damage_tags':list(decode_tags(int(row[1] or 0))),'component_damage_types':sorted(set(components)),'conversion_premises':[n for n in nodes if n]}


def choose_template(root, ability, case):
    from le_calc.definitions_v3 import check_applicability
    facts=case.get('main_skill') or {}
    if not facts.get('ability_class') or facts.get('class')!=facts['ability_class']:
        raise NeedsResearch('主技能与当前职业不匹配，或技能职业归属尚未解析。')
    choices=[]
    for path in (root/'knowledge/definitions').glob('*.json'):
        data=read(path)
        if data.get('schema')!='le-skill-template-v3' or data.get('ability_id')!=ability:continue
        if check_applicability(data,case)['status']!='applicable':continue
        choices.append((data.get('definition',{}).get('plan_version',''),path.name,path,data))
    if not choices:raise NeedsResearch('当前主技能或节点组合尚无适用的共享计算定义。')
    _,_,path,template=max(choices)
    return path,template


def configure_template(template, conditions):
    """Materialize declared scheduling parameters in a new evaluation input."""
    template=copy.deepcopy(template);definition=template['definition']
    horizon=conditions['horizon_s'];applied=False
    for rule in (definition.get('rules') or {}).values():
        if isinstance(rule,dict) and 'windows_s' in rule:
            if 'horizon_s' in rule:rule['horizon_s']=horizon
            rule['windows_s']=sorted(set([float(v) for v in rule['windows_s'] if 0<float(v)<=horizon]+[float(horizon)]))
            applied=True
    for key in ('schedule','frequency'):
        schedule=definition.get(key)
        if isinstance(schedule,dict) and 'windows_s' in schedule:
            schedule['windows_s']=sorted(set([float(v) for v in schedule['windows_s'] if 0<float(v)<=horizon]+[float(horizon)]));applied=True
    strategy=conditions['strategy'];evaluations=definition.get('evaluations') or []
    if strategy!='default':
        if not any(e.get('evaluation_id')==strategy for e in evaluations):raise NeedsResearch('当前技能尚不支持所选施放策略，请使用默认策略。')
        for evaluation in evaluations:evaluation['primary']=evaluation['evaluation_id']==strategy
    return template,applied


def dependency_key(root, template):
    # Include direct, indirect rules/data and renderer/executor source. File content, not mtime.
    paths=[root/'data/current.json',root/'data/affix-db/v1/normalized/affix-db.sqlite']
    for folder,pattern in [('knowledge/definitions','*.json'),('knowledge/mechanisms','*.json'),('tools/phase3/rules','*.json'),('tools/phase3/data','*.json'),('tools/phase3/src/le_calc','*.py'),('tools/phase3/src/le_calc/report_ui/assets','*'),('tools/build_import/src/le_build','*.py'),('tools/bd_editor','*.py')]:
        paths.extend((root/folder).rglob(pattern))
    pointer=read(root/'data/current.json');paths.append(root/pointer['database'])
    return sha({p.relative_to(root).as_posix():file_sha(p) for p in sorted(set(paths)) if p.is_file()})


def scenario_for(conditions, controls):
    from le_calc.monster_dr import dr_for_level
    declared={c['key']:c for c in controls}
    unknown=[k for k,v in conditions['states'].items() if k not in declared and v not in (False,0)]
    if unknown:raise NeedsResearch('已启用的条件暂未接入当前计算：'+ '、'.join(conditions.get('labels',{}).get(k,k) for k in unknown),[{'code':'unsupported_active_controls','controls':unknown}])
    values={k:conditions['states'].get(k,c.get('default',False if c['type']=='boolean' else 0)) for k,c in declared.items()}
    resistances=conditions['target']['resistances']
    # Assembly-backed executors expose one resistance scalar. Never collapse unequal channels.
    if len(set(resistances.values()))!=1:raise NeedsResearch('当前计算入口使用统一目标抗性。请将各系目标抗性设为相同值；分系抗性需要补充执行能力。',[{'code':'per_channel_resistance_not_supported'}])
    level=conditions['target']['monster_level']
    return {'states':values,'target':{'monster_level':level,'resistance':next(iter(resistances.values()))/100,'monster_damage_reduction':dr_for_level(level),'boss':conditions['target']['kind']=='boss','high_health':False,'blinded':False,'slowed':False}}


class CalculationManager:
    def __init__(self, workspace, port=8766):
        self.workspace=workspace;self.root=workspace.root;self.port=port
        for folder in ('tools/phase3/src','tools/save_import/src','tools/knowledge/src'):
            sys.path.insert(0,str(self.root/folder))
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='bd-calculation')
        self.lock=threading.Lock();self.sessions={};self.jobs={};self.latest={}
        from service import atomic
        for status_path in self.workspace.storage.glob('*/evaluations/*/status.json'):
            info=read(status_path)
            if info.get('status') in ('queued','running'):
                info.update(status='failed',message='上次计算已中断，请重新计算；原有成功报告仍保留。')
                atomic(status_path,info)

    def paths(self,key,evaluation):
        if not re.fullmatch(r'[a-f0-9]{32}',str(evaluation)):raise ValueError('计算记录无效')
        return self.workspace.config_path(key)/'evaluations'/evaluation

    def start(self,request):
        from service import atomic,now
        doc=self.workspace.get(request['id'])
        if request.get('version')!=doc['version']:raise ValueError('配置已更新，请载入最新配置后计算')
        if not doc['conditions'].get('main_skill'):raise ValueError('请先在“条件与 Buff”页选择并应用主技能')
        session=str(request.get('client_session_id',''))
        revision=request.get('client_edit_revision')
        if not re.fullmatch(r'[a-zA-Z0-9-]{8,100}',session) or isinstance(revision,bool) or not isinstance(revision,int) or revision<1:raise ValueError('计算会话或序号无效')
        with self.lock:
            scope=(doc['id'],session)
            if revision<=self.sessions.get(scope,0):raise ValueError('计算请求已过期')
            self.sessions[scope]=revision
            active=self.jobs.get(doc['id'])
            if active and not active.done():raise ValueError('当前配置正在计算，请等待完成后重试')
            evaluation=uuid.uuid4().hex;path=self.paths(doc['id'],evaluation)
            snapshot=copy.deepcopy(doc)
            info={'evaluation_id':evaluation,'config_id':doc['id'],'config_version':doc['version'],'document_key':document_key(doc),'client_session_id':session,'accepted_revision':revision,'request_id':request.get('request_id'), 'status':'queued','message':'等待计算','created_at':now()}
            atomic(path/'config.json',snapshot);atomic(path/'status.json',info)
            atomic(self.workspace.config_path(doc['id'])/'latest-calculation.json',{'evaluation_id':evaluation})
            self.jobs[doc['id']]=self.pool.submit(self._run,snapshot,info,path)
        return info

    def status(self,key,evaluation=None):
        from service import atomic
        doc=self.workspace.get(key)
        if not evaluation:
            pointer=self.workspace.config_path(key)/'latest-calculation.json'
            if not pointer.is_file():return {'status':'idle','stale':False}
            evaluation=read(pointer)['evaluation_id']
        info=read(self.paths(key,evaluation)/'status.json')
        info['stale']=document_key(doc)!=info['document_key']
        if info['status']!='completed':
            for prior in sorted((self.workspace.config_path(key)/'evaluations').glob('*/status.json'),key=lambda p:p.stat().st_mtime,reverse=True):
                success=read(prior)
                if success.get('status')=='completed':info['previous_success']=success;break
        return info

    def artifact(self,key,evaluation,name):
        self.workspace.get(key) # Deleted documents cannot receive/serve new results.
        if name not in ('dps-report.html','research-task.json','result.json','config.json'):raise ValueError('不支持的计算产物')
        path=self.paths(key,evaluation)
        info=read(path/'status.json')
        if name=='dps-report.html' and info['status']!='completed':raise ValueError('当前计算尚未生成可用报告')
        return (path/name).read_bytes()

    def _run(self,doc,info,path):
        from service import Workspace,atomic,now
        from le_build.calculation import build_calculation_input
        from le_calc.workflow import unified
        from le_calc.workflow.domain import resolve_build
        from le_calc.conditional_node_buffs import bind as bind_buffs
        from types import SimpleNamespace
        workspace=None;analysis={};case={};diagnostics=[]
        def update(status,message,**extra):
            info.update(status=status,message=message,**extra);atomic(path/'status.json',info)
        def rel(p):return p.relative_to(self.root).as_posix()
        def research_package(message,diagnostics):
            request={'schema':'le-bd-research-task-v1','task_id':info['evaluation_id'],'config_id':doc['id'],'document_key':info['document_key'],'snapshot':rel(path/'config.json'),'case_ref':rel(path/'case.json') if case else None,'message':message,'diagnostics':diagnostics,'knowledge_resolution':rel(path/'knowledge-resolution.json') if analysis else None,'requirements':['以此冻结配置为输入，按稳定 ID 查询共享知识；不要复制历史 BD 数值。','只补齐本次缺少的通用规则，做最小正反例验证并登记知识。','回到统一 compile/simulate/validate/render 流程，生成新计算记录，不改本任务快照。'],'agent_status':'awaiting_handoff'}
            atomic(path/'research-task.json',request)
            return {'research_url':'/api/evaluation?id='+doc['id']+'&evaluation='+info['evaluation_id']+'&name=research-task.json','handoff':'请处理 BD 编辑器研究任务 '+rel(path/'research-task.json')+'。先读取项目 AGENTS.md；核对冻结配置和诊断，复用或补齐共享知识与统一引擎能力，完成后在编辑器重新计算。'}
        try:
            update('running','正在绑定当前装备、节点与共享知识')
            workspace=Workspace(self.root,storage=self.workspace.storage)
            spec,corrections=normalized_spec(workspace,doc['build_spec'])
            atomic(path/'build-spec.json',spec);atomic(path/'normalization.json',{'corrections':corrections})
            calc=build_calculation_input(spec);atomic(path/'calculation-input.json',calc)
            conditions=doc['conditions'];ability=conditions['main_skill']
            main=next((s for s in spec['specializations'] if s.get('tree',{}).get('entity_id','').endswith(':'+ability)),None)
            if not main:raise NeedsResearch('主技能不在当前配置的专精技能中，请重新选择。')
            main_name=workspace.name(workspace.trees[main['tree']['entity_id']]['owner_entity_id'])
            case={'schema':'le-case-v1','case_id':'editor-'+doc['id'],'model':'dps','dataset':'data/current.json','main_skill':{'ability_id':ability,'name':main_name,**skill_facts(workspace,spec,ability)},'build':{'build_id':spec['meta']['build_id'],'revision_id':spec['meta']['revision']['revision_id'],'build_spec':rel(path/'build-spec.json'),'build_spec_sha256':file_sha(path/'build-spec.json')},'inputs':{'calculation_input':rel(path/'calculation-input.json')},'input_sha256':{'calculation_input':file_sha(path/'calculation-input.json')}}
            atomic(path/'case.json',case)
            # Search all current components before selecting a reusable definition.
            analysis=resolve_build({'schema_version':'le-resolve-build-request-v1','case_id':case['case_id'],'case_ref':rel(path/'case.json'),'main_skill':case['main_skill']},root=self.root)
            atomic(path/'knowledge-resolution.json',analysis)
            template_path,original=choose_template(self.root,ability,case)
            template,has_window=configure_template(original,conditions)
            if not has_window:raise NeedsResearch('当前技能的时间窗口尚未映射到编辑器，需要补充调度参数绑定。',[{'code':'schedule_binding_unavailable','template':rel(template_path)}])
            definition=copy.deepcopy(template['definition'])
            bind_buffs(definition,SimpleNamespace(root=self.root,calc=calc,input_refs=[]))
            controls=definition.get('controls',[]);scenario=scenario_for(conditions,controls)
            runtime_keys={e['control'] for e in definition.get('_bound_conditional_buffs',[]) if not e.get('single_cast_only')}
            runtime_keys.update(e['control'] for e in definition.get('_bound_conditional_item_buffs',[]))
            # Conservatively recompile non-library switches, strategy and schedule changes.
            compile_conditions={'main_skill':ability,'horizon_s':conditions['horizon_s'],'strategy':conditions['strategy'],'states':{k:v for k,v in scenario['states'].items() if k not in runtime_keys}}
            deps=dependency_key(self.root,template)
            plan_key=sha({'build':semantic(spec),'scene':compile_conditions,'dependencies':deps,'template':original})
            atomic(path/'template.json',template)
            scenarios=[]
            for evaluation in template['definition'].get('evaluations') or [{'evaluation_id':'default'}]:
                eid=evaluation['evaluation_id'];file=path/('scenario-'+eid+'.json')
                payload={'schema':'le-skill-scenario-v3','template_id':template['template_id'],'scenario_id':'editor-'+info['evaluation_id']+'-'+eid,'monster_level':scenario['target']['monster_level'],'resistance':scenario['target']['resistance'],'target':scenario['target'],'controls':scenario['states']}
                atomic(file,payload);scenarios.append({'evaluation_id':eid,'path':rel(file)})
            source={'kind':'definitions_v3','template':rel(path/'template.json')}
            if template['definition'].get('evaluations'):source['scenarios']=scenarios
            else:source['scenario']=scenarios[0]['path']
            case['calculation']={'engine':'unified_dps','source':source};atomic(path/'case.json',case)
            prior=None
            # Only immutable, successful evaluations with identical dependencies are candidates.
            for status_path in sorted(path.parent.glob('*/status.json'),key=lambda p:p.stat().st_mtime,reverse=True):
                previous=read(status_path)
                if previous.get('status')=='completed' and previous.get('plan_key')==plan_key:
                    prior=status_path.parent;break
            route='A' if prior else 'B'
            update('running','正在复用计算计划' if prior else '正在重新编译当前构筑',route=route,plan_key=plan_key)
            if prior:
                atomic(path/'calculation-plan.json',read(prior/'calculation-plan.json'))
                # Existing input refs remain pinned to the identical frozen build/rule closure.
                case=read(prior/'case.json')
                atomic(path/'execution-case.json',case)
            else:
                compiled=unified.compile_stage(case,root=self.root,run_dir=path)
                if compiled['execution_status']!='completed':
                    diagnostics=read(path/'plan-diagnostics.json') if (path/'plan-diagnostics.json').is_file() else compiled['issues']
                    raise NeedsResearch('当前组合还需要补充计算规则，已保留配置和诊断。',diagnostics)
            update('running','正在执行统一计算并检查结果绑定')
            simulated=unified.simulate_stage(case,root=self.root,run_dir=path,scenario=scenario)
            if simulated['execution_status']!='completed':raise RuntimeError('统一执行器未完成：'+str(simulated.get('issues')))
            validation=unified.validate_stage(case,root=self.root,run_dir=path)
            if not validation['passed']:raise RuntimeError('结果一致性检查未通过：'+str(validation.get('issues')))
            base='/api/evaluation?id='+doc['id']+'&evaluation='+info['evaluation_id']
            report_context={'read_only':True,'editor_url':'/?config='+doc['id'],'editor_snapshot':{'name':doc['name'],'character':{'class':workspace.name(spec['character']['class']['ref']['entity_id']),'mastery':workspace.name(spec['character']['mastery']['ref']['entity_id']),'level':spec['character']['level']},'conditions':conditions,'allocations':[{'name':workspace.trees.get(s.get('tree',{}).get('entity_id'),{}).get('name','Talents'),'nodes':[{'name':workspace.name(n.get('ref',{}).get('entity_id')),'points':n.get('points',0)} for n in s.get('nodes',[]) if n.get('points',0)>0]} for s in [spec.get('passive_tree',{}),*spec.get('specializations',[])]]}}
            rendered=unified.render_stage(case,root=self.root,run_dir=path,endpoint=f'http://127.0.0.1:{self.port}/recompute',report_context=report_context)
            result=read(path/'result.json')
            totals=result.get('totals') or {};dps=totals.get('dps')
            if dps is None:dps=totals.get('horizon_dps')
            if dps is not None and (not isinstance(dps,(int,float)) or not math.isfinite(dps)):raise RuntimeError('结果 DPS 不是有限数值')
            if (result.get('coverage') or {}).get('status')=='partial':
                info.update(research_package('当前结果仍有未覆盖来源，请补齐这些机制后生成新结果。',(result.get('coverage') or {}).get('gaps') or []))
            update('completed','计算完成',finished_at=now(),dps=dps,totals=totals,scope=result.get('scope'),coverage=result.get('coverage'),limitations=result.get('limitations') or [],main_skill=main_name,horizon_s=conditions['horizon_s'],window_applied=has_window,report_url=base+'&name=dps-report.html',result_url=base+'&name=result.json',normalization_count=len(corrections),template_source=rel(template_path),dependencies=deps)
        except NeedsResearch as exc:
            update('needs_research',str(exc),route='C',**research_package(str(exc),exc.diagnostics))
        except Exception as exc:
            import traceback
            atomic(path/'failure.json',{'error':str(exc),'traceback':traceback.format_exc()})
            update('failed','计算未完成，已保留上次成功结果和本次错误记录。请重试或交由 agent 排查。')
        finally:
            if workspace:
                workspace.c.close();workspace.a.close()

