"""Equipment-driven skill point budgets; never infer budgets from allocated points."""
import json
import math
from pathlib import Path
from le_build.calculation import build_calculation_input

def budgets(spec,catalog):
    rules=json.loads((Path(__file__).parent/'skill-tag-rules-v1.json').read_text(encoding='utf-8'))['rules']
    level_mods=[m for m in build_calculation_input(spec)['mods'] if m.get('property')==88 and m.get('kind')=='mod']
    result={};class_id=int(spec['character'].get('class',{}).get('source_value',0))
    manual=spec.get('extensions',{}).get('bd_editor',{}).get('skill_budgets',{})
    for allocation in spec.get('specializations',[]):
        tree=allocation.get('tree') or {};tid=tree.get('entity_id') or allocation.get('tree_entity_id')
        owner=allocation.get('owner_skill',{}).get('entity_id')
        row=catalog._row('select * from skills where entity_id=?',(owner,))
        if not row:
            result[tid]={'base':allocation.get('source_level') or 20,'minimum':None,'maximum':None,'status':'unresolved','sources':[]};continue
        ex=json.loads(row.get('extensions') or '{}');ability_index=ex.get('index');source_id=row.get('source_skill_id');tags=int(row.get('tags_raw') or 0)
        selected={int(n.get('node_index',n.get('node_id',-1))):n.get('points',0) for n in allocation.get('nodes',[])}
        if source_id in rules:
            for rule in rules[source_id]:
                if selected.get(rule['node_index'],0)>=rule['min_points']:tags=(tags&~rule['remove'])|rule['add']
            variants=[tags]
        else:
            potential=int(ex.get('skillTreeConversionDamageTags') or 0)
            bits=[1<<i for i in range(7) if potential&(1<<i)]
            variants=[tags] if not bits or not any(selected.values()) else list({(tags&~potential)|sum(bit for j,bit in enumerate(bits) if mask&(1<<j)) for mask in range(1,1<<len(bits))})
        base=allocation.get('source_level') or 20;lo=hi=base;sources=[];unknown=[]
        for mod in level_mods:
            value=mod.get('value_numeric');scope=catalog.skill_level_scope(mod.get('tags'),mod.get('special_tag'))
            if value is None or mod.get('modifier_type')!=0:
                unknown.append(mod['mod_id']);continue
            count=math.floor(value);mask=int(mod.get('tags') or 0);st=int(mod.get('special_tag') or 0)
            if st==1:matches=[ability_index==mask]*len(variants)
            elif 2<=st<=6:matches=[class_id==st-2 and (not mask or tag&mask==mask) for tag in variants]
            elif st==0 or 7<=st<=11:
                if st and str(st-7) not in ex.get('attributeScaling',{}):continue
                # Minion-scoped inherited tags require a separate semantic path.
                if mask&8192:unknown.append(mod['mod_id']);continue
                if mask&128:
                    remainder=mask&~128;matches=[bool(tag&(2|4|8|128)) and tag&remainder==remainder for tag in variants]
                else:matches=[not mask or tag&mask==mask for tag in variants]
            else:unknown.append(mod['mod_id']);continue
            if not any(matches):continue
            vals=[count if match else 0 for match in matches];lo+=min(vals);hi+=max(vals)
            sources.append({'item':mod.get('item_instance_id'),'label':scope['label'],'value':count,'certain':all(matches)})
        status='confirmed_from_catalog' if lo==hi and not unknown else 'partial'
        record={'base':base,'minimum':lo,'maximum':hi if not unknown else None,'status':status,'sources':sources,'unknown_sources':unknown,'effective_tags':variants}
        if tid in manual:record.update(minimum=manual[tid],maximum=manual[tid],status='manual_override')
        result[tid]=record
    return result
