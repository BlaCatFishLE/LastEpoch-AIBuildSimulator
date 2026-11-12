"""属性转换算子：保留技能/节点所用的等效属性，转换效果只作用于声明组件。"""
import math

def resolve_brutality(strength,conversion,mana_cost,rule):
    values=[float(strength),float(conversion),float(mana_cost)]
    if not all(math.isfinite(v) for v in values) or any(v<0 for v in values):
        raise ValueError('属性转换输入必须为非负有限数值')
    strength,conversion,mana_cost=values
    conversion=min(1.0,conversion)
    brutality=strength*conversion
    capped=min(mana_cost,float(rule['mana_cost_cap']))
    more=brutality*float(rule['more_per_point_per_mana'])*capped
    return {'strength_before':strength,'conversion_fraction':conversion,
            'strength':strength-brutality,'brutality':brutality,
            'effective_strength':strength,'mana_cost':mana_cost,'mana_cost_cap':float(rule['mana_cost_cap']),'capped_mana_cost':capped,
            'more_damage':more,'more_multiplier':1+more,
            'health_leech_reduction':brutality*float(rule['health_leech_reduction_per_point']),
            'converted_strength_grants_armor':False,'damage_tags':list(rule['damage_tags'])}
