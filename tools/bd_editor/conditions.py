"""Saved editor conditions and declared engine controls; no damage formulas."""
import copy
import json
import math
import sys
from types import SimpleNamespace
from player_projection import player_text

SCHEMA='le-bd-conditions-v1'
DAMAGE_TYPES=('Physical','Fire','Cold','Lightning','Void','Necrotic','Poison')

def catalog(workspace,spec,main_skill=''):
    root=workspace.root
    sys.path.insert(0,str(root/'tools/phase3/src'))
    from le_calc.conditional_node_buffs import bind
    from le_build.calculation import build_calculation_input
    definition={'controls':[],'conditional_buff_library':'tools/phase3/rules/shared-conditional-node-buffs-v1.json'}
    bind(definition,SimpleNamespace(root=root,calc=build_calculation_input(spec),input_refs=[]))
    controls={};derived={}
    def add(c):
        key=c.get('key')
        if not key:return
        control=copy.deepcopy(c);control['label']=player_text(c.get('label')) or '未命名条件'
        control['source']=player_text(c.get('source') or '当前构筑')
        control['effect']=player_text(c.get('effect'))
        control['group']='target' if c.get('source')=='目标条件' else c.get('group','buff')
        control.setdefault('default',False if control.get('type')=='boolean' else 0)
        controls.setdefault(key,control)
    for c in definition['controls']:add(c)
    for effect in definition.get('_bound_conditional_buffs',[]):
        if effect.get('any'):derived[effect['control']]={'any':effect['any']}
    specs=spec.get('specializations',[])
    selected={str(s.get('tree_source_id') or (s.get('tree',{}).get('entity_id') or '').split(':')[-1]):s for s in [spec.get('passive_tree',{}),*specs]}
    def has_node(tree,node):return any(str(n.get('node_id'))==str(node) and n.get('points',0)>0 for n in selected.get(tree,{}).get('nodes',[]))
    instances=[e['instance'] for e in spec['equipment'].get('slots',[])]+[e['instance'] for e in spec['idols'].get('entries',[])]+[e['instance'] for e in spec['blessings'].get('entries',[])]
    itemids={(i.get('definition') or i.get('base') or {}).get('entity_id') for i in instances}
    affixids={a.get('affix',{}).get('ref',{}).get('entity_id') for i in instances for channel in i.get('affixes',{}).values() for a in channel}
    recipes=json.loads((root/'tools/phase3/rules/shared-conditional-mechanisms-v1.json').read_text(encoding='utf-8'))
    for recipe in recipes.get('mechanisms',{}).values():
        for effect in recipe.get('effects',[]):
            source=effect.get('source',{});itemid=source.get('definition_entity_id');matched=itemid in itemids if itemid else False
            if source.get('kind')=='skill_node':matched=has_node(source.get('tree'),source.get('node'))
            grant=source.get('grant_selector',{})
            if grant:matched=any(eid.removeprefix('affix:') in affixids or eid in affixids for eid in grant.get('ids',[]))
            if not matched:continue
            label=effect.get('label','当前条件效果');source_name=workspace.name(itemid) if itemid else '当前节点 / 词缀'
            for key in effect.get('states') or []:
                add({'key':key,'label':'Frenzy' if key=='frenzy' else label,'source':source_name,'type':'boolean','effect':'固定条件假设；计算前会检查是否支持当前作用域。'})
            if effect.get('stack_control'):
                add({'key':effect['stack_control'],'label':label,'source':source_name,'type':'integer','min':0,'max':effect.get('max_stacks'),'default':0,'effect':'层数上限待映射；未接入计算；启用后会生成研究任务。'})
    main=next((s for s in specs if (s.get('tree_source_id') or s.get('tree',{}).get('entity_id','').split(':')[-1])==main_skill),None)
    main_name=(main or {}).get('tree',{}).get('name') or '主技能'
    # A template declares controls, not proof of current build calculation coverage.
    supported=set();template_ref=None
    if main:
        candidates=[]
        for p in (root/'knowledge/definitions').glob('*.json'):
            data=json.loads(p.read_text(encoding='utf-8'))
            if data.get('ability_id')==main_skill:
                body=data.get('definition',{});candidates.append((body.get('plan_version',''),p.name,body))
        if candidates:
            from calculation import choose_template,skill_facts,NeedsResearch
            try:
                path,template=choose_template(root,main_skill,{'main_skill':{'ability_id':main_skill,**skill_facts(workspace,spec,main_skill)}})
                body=copy.deepcopy(template['definition']);template_ref=path.relative_to(root).as_posix()
                bind(body,SimpleNamespace(root=root,calc=build_calculation_input(spec),input_refs=[]))
                supported={c['key'] for c in body.get('controls',[])}
                for c in body.get('controls',[]):
                    add({**c,'source':main_name if c['key'] not in controls else controls[c['key']]['source'],'effect':c.get('effect') or '由共享计算入口绑定当前场景。','group':'target' if c['key'].startswith('target_') else 'buff'})
            except NeedsResearch:
                pass
    if 'target_is_boss_or_rare' in controls:derived['target_is_boss_or_rare']={'target_kind':True};controls['target_is_boss_or_rare']['readonly']=True
    for key,c in controls.items():
        if key in derived:c['readonly']=True
        c['calculation_status']='declared' if key in supported else 'not_mapped'
    return {'controls':list(controls.values()),'derived':derived,'main_available':bool(main) or not main_skill,'main_name':main_name,'calculation_template':template_ref}

def normalize(raw,available):
    raw=copy.deepcopy(raw or {})
    if not isinstance(raw,dict):raise ValueError('条件配置必须是对象')
    if raw.get('schema',SCHEMA)!=SCHEMA:raise ValueError('条件配置格式不受支持')
    main=raw.get('main_skill','')
    if not isinstance(main,str) or len(main)>100:raise ValueError('主技能格式无效')
    target=raw.get('target',{})
    if not isinstance(target,dict):raise ValueError('目标设置必须是对象')
    kind=target.get('kind','normal')
    if kind not in ('normal','rare','boss'):raise ValueError('目标类型无效')
    level=target.get('monster_level',100)
    if isinstance(level,bool) or not isinstance(level,int) or not 1<=level<=100:raise ValueError('目标等级需为 1–100 的整数')
    resistances=target.get('resistances',{})
    if not isinstance(resistances,dict):raise ValueError('目标抗性设置无效')
    def numeric(v,low,high,label,whole=False):
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<low or (high is not None and v>high) or (whole and not isinstance(v,int)):raise ValueError(label+'超出支持范围或类型无效')
        return v
    resistances={k:numeric(resistances.get(k,0),-100,75,k+' 抗性') for k in DAMAGE_TYPES}
    horizon=numeric(raw.get('horizon_s',30),.1,600,'观察时间')
    strategy=raw.get('strategy','default')
    if strategy not in ('default','eager','patient'):raise ValueError('施放策略无效')
    states=raw.get('states',{})
    if not isinstance(states,dict) or len(states)>250:raise ValueError('条件状态格式无效')
    for key,value in states.items():
        if not isinstance(key,str) or len(key)>200 or not isinstance(value,(bool,int,float)) or (isinstance(value,(int,float)) and not math.isfinite(value)):raise ValueError('条件值需为布尔值或有限数字')
    labels=raw.get('labels',{})
    if not isinstance(labels,dict):raise ValueError('条件名称格式无效')
    labels={k:player_text(v)[:160] for k,v in labels.items() if k in states}
    for c in available['controls']:
        key=c['key'];value=states.get(key,c.get('default',False if c['type']=='boolean' else 0))
        if key not in available['derived']:
            if c['type']=='boolean':
                if not isinstance(value,bool):raise ValueError(c['label']+'需为开关值')
            else:numeric(value,c.get('min',0),c.get('max'),c['label'],c['type']=='integer')
        states[key]=value;labels[key]=c['label']
    for key,rule in available['derived'].items():
        states[key]=kind in ('boss','rare') if rule.get('target_kind') else any(bool(states.get(k,False)) for k in rule.get('any',[]))
    return {**raw,'schema':SCHEMA,'main_skill':main,'target':{'kind':kind,'monster_level':level,'resistances':resistances},'horizon_s':horizon,'strategy':strategy,'states':states,'labels':labels}

def prepare(workspace,spec,raw=None):
    if raw is not None and not isinstance(raw,dict):raise ValueError('条件配置必须是对象')
    available=catalog(workspace,spec,(raw or {}).get('main_skill',''))
    return normalize(raw,available),available
