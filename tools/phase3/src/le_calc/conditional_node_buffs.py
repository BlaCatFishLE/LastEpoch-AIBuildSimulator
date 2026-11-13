"""跨技能节点 Buff 的稳定身份绑定；规则来自共享库，数值绑定当前点数。"""
import copy
import json
from . import calculation_plan as planmod, contribution_mapping as mapping, model


def bind(definition, context):
    path = definition.get('conditional_buff_library')
    if not path:
        return {}
    library = json.loads((context.root / path).read_text(encoding='utf-8'))
    if library.get('schema') != 'le-conditional-node-buffs-v1':
        raise ValueError('条件 Buff 库格式不支持')
    context.input_refs.append({'role':'rule_file','path':path,'sha256':planmod.sha256_file(context.root/path)})
    specs=list((context.calc.get('skills') or {}).get('specializations') or [])+[context.calc.get('passive_tree') or {}]
    bound=[]; defaults={};controls=definition['controls'];existing={c['key'] for c in controls}
    def add_control(key,label,source,effect='',readonly=False,default=False,single=False):
        if key in existing:
            return
        controls.append({'key':key,'label':label,'source':source,'effect':effect,'type':'boolean','min':0,'max':1,'default':default,'readonly':readonly,'single_cast_only':single})
        defaults[key]=default;existing.add(key)
    for rule in library['effects']:
        spec=next((s for s in specs if str((s.get('tree') or {}).get('entity_id','')).split(':')[-1]==rule['tree']),None)
        node=next((n for n in (spec or {}).get('nodes',[]) if str(n.get('node_id'))==rule['node']),None)
        points=int((node or {}).get('points') or 0)
        if points<rule['min_points']:
            continue
        entry=copy.deepcopy(rule);entry['points']=points
        source=(spec.get('tree') or {}).get('name','')+' · '+str(points)+' 点'
        effects=[]
        for modifier in entry['modifiers']:
            modifier['value']=modifier['constant'] if modifier['constant'] is not None else modifier['per_point']*points
            effects.append(('火焰' if 'Fire' in modifier['tags'] else '')+{'damage':'伤害','penetration':'穿透','cast_speed':'施法速度'}.get(modifier['stat'],modifier['stat'])+' +'+format(modifier['value']*100,'.6g')+'%'+(' 独立' if modifier['operation']=='more' else ''))
        automatic=bool(entry.get('any'))
        add_control(entry['control'],entry['label'],source,'；'.join(effects)+'。'+entry['note'],readonly=automatic,default=automatic,single=entry['single_cast_only'])
        for key in entry.get('requires',[])+entry.get('any',[]):
            add_control(key,{'enemy_chilled':'目标冰缓','enemy_frozen':'目标冻结'}.get(key,key),'目标条件')
        bound.append(entry)
    definition['_bound_conditional_buffs']=bound
    from .conditional_item_buffs import bind as bind_items
    defaults.update(bind_items(definition,context,library.get('item_buff_library')))
    return defaults


def contributions(profile):
    for entry in profile.get('_bound_conditional_buffs') or []:
        keys=entry.get('any') or [entry['control']]+entry.get('requires',[])
        program={'op':'any' if entry.get('any') else 'all','terms':[{'kind':'control_true','key':key} for key in keys]}
        for i,modifier in enumerate(entry['modifiers']):
            row=mapping.make_contribution(contribution_id='buff:'+entry['id']+':'+str(i),source_kind=entry['source_kind'],source_label=entry['label'],source_path='skill_tree.'+entry['tree']+'.nodes.'+entry['node'],stat_name=modifier['stat'],stat_key=modifier['stat'],operation=modifier['operation'],value=modifier['value'],unit=modifier['unit'],damage_tags=modifier['tags'],group='buff:'+entry['id'],included=True,exclusion_reason=None,value_source='source',status=entry['status'],evidence_rows=[model.Evidence(kind=e['kind'],locator=e['locator'],detail=entry['label']) for e in entry['evidence']],notes=entry['note'],raw={'tree':entry['tree'],'node':entry['node'],'stat_index':i,'points':entry['points'],'single_cast_only':entry['single_cast_only']})
            row.target=modifier.get('target')
            yield row,program
