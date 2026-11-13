"""Bind data-declared item Buff controls to current instances; reuse profile effects."""
import copy
import json
from . import calculation_plan as planmod


def bind(definition, context, path):
    if not path:return {}
    library=json.loads((context.root/path).read_text(encoding='utf-8'))
    if library.get('schema')!='le-conditional-item-buffs-v1':raise ValueError('装备条件 Buff 库格式不支持')
    ref={'role':'rule_file','path':path,'sha256':planmod.sha256_file(context.root/path)}
    if ref not in context.input_refs:context.input_refs.append(ref)
    effects=definition.setdefault('effects',[]);controls=definition.setdefault('controls',[])
    defaults={};bound=[]
    for rule in library['buffs']:
        key=rule['control']['key'];eid=rule['definition_entity_id']
        items=sorted([i for i in context.calc.get('items',[]) if i.get('definition_entity_id')==eid],key=lambda i:i['instance_id'])
        if not items:continue
        # Explicit template effects keep their declared scope; do not inject another copy.
        owned=[e for e in effects if e.get('stack_control')==key]
        if owned:
            prior=next((b for b in definition.get('_bound_conditional_item_buffs',[]) if b['control']==key),None)
            if prior:bound.append(prior)
            continue
        if rule.get('stacking')!='single_buff':raise ValueError('未支持的装备 Buff 叠加规则')
        item=items[0]
        descriptions=[m for m in item.get('mods',[]) if m.get('kind')=='description']
        for anchor in rule['required_texts']:
            if not any(anchor in str(m.get('readable_text') or '') for m in descriptions):
                raise ValueError(rule['label']+' 的当前物品说明与共享规则不匹配')
        emitted=[]
        for effect in rule['effects']:
            anchor=effect['source']['expected_text_contains']
            matches=[m for m in descriptions if anchor in str(m.get('readable_text') or '')]
            if len(matches)!=1:raise ValueError(rule['label']+' 的效果来源无法唯一绑定')
            mod=matches[0];index=int(str(mod['mod_id']).rsplit('#',1)[1])
            current=copy.deepcopy(effect)
            current['source'].update(item_instance_id=item['instance_id'],description_index=index,expected_definition_entity_id=eid)
            current['source']['evidence']=copy.deepcopy(rule.get('evidence') or [])
            current['stack_control']=key
            current['notes']=rule['note']
            effects.append(current);emitted.append(current['id'])
        control=copy.deepcopy(rule['control']);control.update(source=rule['label'],effect=rule['note'])
        existing=next((c for c in controls if c['key']==key),None)
        if existing:
            if any(existing.get(field)!=control.get(field) for field in ('type','min','max')):
                raise ValueError(rule['label']+' 的层数定义与共享规则冲突')
        else:controls.append(control);defaults[key]=control['default']
        note=rule['note']
        if note not in definition.setdefault('limitations',[]):definition['limitations'].append(note)
        bound.append({'id':rule['id'],'control':key,'source_instance':item['instance_id'],'available_instances':[i['instance_id'] for i in items],'effects':emitted})
    definition['_bound_conditional_item_buffs']=bound
    return defaults
