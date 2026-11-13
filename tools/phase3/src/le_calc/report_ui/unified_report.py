"""统一玩家报告：只投影已计算结果，不按技能/案例路由或重新计算伤害。"""
from pathlib import Path
import re
from . import components as c, shared_ui as ui
from .document import DocumentSpec, render_document

TYPES = {'Fire':'火焰','Cold':'冰霜','Physical':'物理','Lightning':'闪电','Void':'虚空','Necrotic':'腐化','Poison':'毒素'}
KINDS = {'equipment':'装备','idol':'神像','blessing':'祝福','passive_node':'职业被动','skill_node':'技能节点','base_character':'角色基础','derived':'属性计算'}
SLOTS = dict(zip(('head','chest','weapon1','weapon2','hands','waist','feet','ring1','ring2','amulet','relic'),('头盔','胸甲','主手','副手','手套','腰带','鞋子','左戒指','右戒指','项链','圣物')))
STATS = {'damage':'伤害加成','added_damage':'附加伤害','crit_chance':'暴击率','crit_multiplier':'暴击伤害','penetration':'穿透','cooldown_recovery_speed':'冷却恢复','cooldown_reduction':'冷却缩减','ailment_duration':'异常持续时间','ailment_chance':'异常施加概率','skill_duration':'技能持续时间','cast_speed':'施法速度','attack_speed':'攻击速度','attribute':'属性','all_attributes':'所有属性','skill_level':'技能等级','resistance':'抗性','unsupported':'其它角色属性'}
ATTRS = {'strength':'力量','dexterity':'敏捷','intelligence':'智力','vitality':'体质','attunement':'协调'}

def num(v):
    return '—' if v is None else f'{float(v):,.2f}'.rstrip('0').rstrip('.')

def name(value, fallback='当前来源'):
    text = re.sub(r'\s*\[(?:equipment|idols|blessings|passive node|.*tree node).*?\]', '', str(value or ''))
    return fallback if not text or text.startswith(('mod:', 'passive:', 'skill:', 'rule:', 'effect:', 'node:', 'stat:', 'propertyList')) else text

def effect(row):
    key = row.get('stat_key'); label = STATS.get(key, name(row.get('stat_name'), '其它属性'))
    if key == 'attribute': label = ATTRS.get(row.get('target'), '属性')
    tags = ' / '.join(TYPES.get(t,t) for t in row.get('damage_tags',[]) if t in TYPES)
    if tags: label = tags + ' · ' + label
    value = row.get('value',0); op = row.get('operation')
    percent = row.get('unit') == 'percent' or op in ('more','increased')
    return c.esc(label) + ' <b>' + c.esc(('+' if value >= 0 else '')+num(value * (100 if percent else 1))+('%' if percent else '')) + '</b>' + (' 独立' if op == 'more' else '')

def state(row, result):
    app = (result.get('contribution_application_summary') or {}).get(row.get('contribution_id')) or row.get('application') or {}
    if app.get('components'): return '生效'
    statuses = app.get('statuses') or {}
    for key,label in [('unresolved','待确认'),('condition_not_satisfied','条件未满足'),('not_applicable','不适用'),('duplicate','重复已抑制')]:
        if statuses.get(key): return label
    return '已计入' if row.get('included') else '未启用'

def chart(windows):
    if not windows: return ''
    maximum = max((w.get('dps',0) for w in windows),default=1) or 1
    rows = ''.join('<div class="window-bar"><span>'+num(w['window_s'])+' 秒</span><div><i style="width:'+str(max(0,w.get('dps',0)/maximum*100))+'%"></i></div><b>'+num(w.get('dps'))+'</b></div>' for w in windows)
    return c.card(rows,title='观察窗口 · 平均 DPS',cls='window-chart')

CHANNEL_LABELS = {
    'implicit': '基底隐含',
    'affix': '普通词条',
    'unique': '暗金固有效果',
    'unique_mod': '暗金固有效果',
    'unique_description': '暗金说明',
    'corrupted': '腐化词条',
    'sealed': '封印词条',
    'legendary': '传奇合成词条',
}
CHANNEL_ORDER = ('implicit', 'affix', 'unique_mod', 'unique_description', 'corrupted', 'sealed', 'legendary')
ROLE_LABELS = {'equipment': '装备', 'idol': '神像', 'blessing': '祝福'}


def mod_channel(mod):
    """词条通道：优先显式 channel，description 归入 unique_description，普通 affix 归 affix。"""
    mod_id = str(mod.get('mod_id') or '')
    if '#unique_description#' in mod_id or mod.get('kind') == 'description':
        return 'unique_description'
    channel = str(mod.get('channel') or mod.get('source') or '').strip()
    if channel == 'normal':
        return 'affix'
    if channel == 'unique':
        return 'unique_mod'
    return channel or 'affix'


def mod_text(mod):
    """玩家可读词条文本：readable_text → name → stat+value → 明确缺失说明。"""
    for key in ('readable_text', 'name', 'label'):
        value = mod.get(key)
        if isinstance(value, str) and value.strip():
            return ' '.join(value.split())
    stat = mod.get('stat') if isinstance(mod.get('stat'), dict) else {}
    stat_name = stat.get('name') or mod.get('stat_name')
    value_text = mod.get('display_value')
    if value_text is None and mod.get('value_numeric') is not None:
        percent = str(stat.get('unit') or '') == 'percent'
        value_text = num(float(mod['value_numeric']) * (100 if percent else 1)) + ('%' if percent else '')
    if value_text is not None and stat_name:
        return str(value_text) + ' ' + str(stat_name)
    if stat_name:
        return str(stat_name)
    if mod.get('hidden'):
        return '内部记录：无玩家可读说明（未参与展示换算）'
    return '词条说明尚未解析'


def gear_mod_groups(item):
    mods = item.get('mods')
    if not isinstance(mods, list) or not mods:
        mods = item.get('affixes') if isinstance(item.get('affixes'), list) else []
    groups = {}
    for index, mod in enumerate(mods):
        if not isinstance(mod, dict):
            continue
        channel = mod_channel(mod)
        groups.setdefault(channel, []).append((index, mod))
    ordered = []
    for channel in CHANNEL_ORDER:
        if channel in groups:
            ordered.append((channel, groups.pop(channel)))
    for channel in sorted(groups):
        ordered.append((channel, groups[channel]))
    return ordered, mods


def gear_card(item, index):
    role = str(item.get('role') or 'equipment')
    slot = item.get('slot')
    slot_label = SLOTS.get(slot) if isinstance(slot, str) else None
    if not slot_label:
        slot_label = ROLE_LABELS.get(role, KINDS.get(role, '装备'))
    base_name = item.get('base_name') or item.get('base') or ''
    rarity = item.get('rarity')
    rarity_label = (rarity.get('label_zh') or rarity.get('label_en') or '') if isinstance(rarity, dict) else str(rarity or '')
    instance_id = str(item.get('instance_id') or f'{role}#{index}')
    groups, mods = gear_mod_groups(item)
    total = sum(len(rows) for _channel, rows in groups)
    completeness = '完整词条' if item.get('mods_complete') else '摘要词条（非完整解码）'
    meta = ' · '.join(x for x in (slot_label, base_name, rarity_label, completeness + ' · ' + str(total) + ' 条') if x)
    sections = []
    for channel, rows in groups:
        label = CHANNEL_LABELS.get(channel, channel or '词条')
        lines = []
        for mod_index, mod in rows:
            mod_id = str(mod.get('mod_id') or '')
            body = '<li data-mod-id="' + c.esc(mod_id) + '" data-channel="' + c.esc(channel) + '">' + c.esc(mod_text(mod)) + '</li>'
            lines.append(body)
        sections.append('<section class="mod-channel" data-channel="' + c.esc(channel) + '"><h5>' + c.esc(label + ' · ' + str(len(rows)) + ' 条') + '</h5><ul>' + ''.join(lines) + '</ul></section>')
    content = ('<p class="muted">' + c.esc(meta) + '</p>'
               + ('<div class="mod-channels">' + ''.join(sections) + '</div>' if sections else '<p class="muted">该实例没有已解码词条记录。</p>'))
    return ('<article class="card gear-card" data-instance-id="' + c.esc(instance_id)
            + '" data-role="' + c.esc(role) + '" data-slot="' + c.esc(str(slot or ''))
            + '" data-mod-count="' + str(total) + '"><header class="card-head"><h4>'
            + c.esc(name(item.get('display_name') or item.get('name') or base_name or instance_id))
            + '</h4></header>' + content + '</article>')


def render(result, binding):
    details = result.get('report_details') or {}
    equipment_by_id = {str(item.get('instance_id')): item for item in details.get('equipment') or []}
    for item in binding.get('equipment') or []:
        iid = str(item.get('instance_id'))
        if item.get('mods_complete') or iid not in equipment_by_id:
            equipment_by_id[iid] = item
    equipment = list(equipment_by_id.values())
    contributions = result.get('contributions') or []
    by_id = {r['contribution_id']:r for r in contributions}
    items = {i['instance_id']:i for i in equipment if i.get('instance_id')}
    def item_for(row):
        cid = row.get('contribution_id','')
        return next((item for iid,item in items.items() if cid.startswith(('mod:'+iid+'#',iid+'#'))),None)
    def source_name(row):
        item = item_for(row)
        return name(item.get('display_name') or item.get('name')) if item else name(row.get('source_label'))
    def source_table(rows):
        return c.table(['来源','效果','状态'],[[c.esc(source_name(r)),effect(r),c.badge(state(r,result),'ok' if state(r,result)=='生效' else 'muted')] for r in rows])
    components = details.get('components') or result.get('components') or []
    loops = details.get('loops') or result.get('policy_results') or []
    primary = next((p for p in loops if p.get('primary')),loops[0] if loops else {})
    windows = result.get('windows') or []
    totals = result.get('totals') or {}
    single = (details.get('single_cast') or {}).get('totals') or {}
    title = (result.get('presentation') or {}).get('title') or '伤害分析'
    target = (result.get('scenario') or {}).get('target') or {}
    states = (result.get('scenario') or {}).get('states') or {}
    horizon = windows[-1].get('window_s') if windows else None
    overview = '<div class="kpi-grid">'+c.kpi('持续预计 DPS',c.num(totals.get('horizon_dps')),hint=c.esc(num(horizon)+' 秒观察窗口'),tone='primary')+c.kpi('单次施放总伤害',c.num(single.get('single_cast_total')),hint='包含本次施放的后续伤害')+c.kpi('窗口总伤害',c.num(totals.get('horizon_damage')))+c.kpi('施放次数 / 最大并发',num(totals.get('cast_count'))+' / '+num(totals.get('max_concurrent')))+'</div>'
    evaluations = result.get('evaluations') or []
    if evaluations:
        overview += c.card(c.table(['评估','策略','预计 DPS','总伤害','施放','窗口'],[[c.esc(e.get('evaluation_id')),c.esc(e.get('policy_id') or e.get('action_policy') or ''),num(e.get('horizon_dps')),num(e.get('horizon_damage')),num(e.get('cast_count')),num(e.get('window_count'))] for e in evaluations]),title='评估口径')
    overview += '<div class="report-columns">'+chart(windows)
    timing=[]
    for key,label in [('cast_time_s','施放动作（秒）'),('effective_recharge_s','充能恢复（秒）'),('max_charges','最大充能'),('instance_duration_s','单实例持续（秒）')]:
        val = (details.get('cast') or {}).get(key,(details.get('cooldown') or {}).get(key))
        if val is not None: timing.append((label,num(val)))
    if components and components[0].get('duration_s') is not None: timing.append(('本体持续（秒）',num(components[0]['duration_s'])))
    timing += [('目标等级',num(target.get('monster_level'))),('目标抗性',num(target.get('resistance',0)*100)+'%'),('怪物减伤',num(target.get('monster_damage_reduction',0)*100)+'%')]
    overview += c.card(c.fact_list(timing),title='关键时间与目标')+'</div>'
    if single:
        parts=[('本体',single.get('body_total')),('持续施加效果',single.get('ignite_total'))]
        overview += c.section('单次施放 · 伤害构成',c.meter([{'label':label,'value':value,'share':value/single['single_cast_total'] if single.get('single_cast_total') else 0,'color':color} for (label,value),color in zip(parts,['body','ignite']) if value is not None]))
    if result.get('coverage',{}).get('status')=='partial': overview += c.note('当前结果仅覆盖已建模伤害，仍有部分来源待确认。')
    damage=''
    if any(control.get('single_cast_only') and states.get(control['key']) for control in details.get('controls') or []):
        overview+=c.note('已声明下一次施法就绪：仅“单次施放总伤害”包含该增益；持续 DPS、窗口图与循环乘区不假设每次都能获得。')
        single_components=(details.get('single_cast') or {}).get('components') or []
        damage+=c.section('本次施法 · 就绪效果',c.table(['伤害类型','本次施法本体总伤害'],[[c.esc(TYPES.get(x.get('damage_type'),'伤害')),num(x.get('damage_total'))] for x in single_components])+c.note('以下乘区按持续循环条件展示；本次施法就绪效果不传播到持续施加效果。'))
    for component in components:
        cid=component.get('component_id') or component.get('id')
        label=TYPES.get(component.get('damage_type'),name(component.get('presentation_label'),'伤害'))+' · '+{'dot':'持续伤害','hit':'命中'}.get(component.get('kind'),'伤害')
        facts=[]
        fields=[('base_damage','基础伤害',1,''),('added_damage_total','附加伤害',1,''),('added_damage_scaling','附加效用',100,'%'),('increased_total','加算增伤',100,'%'),('more_product','独立增伤',1,' 倍'),('tick_interval_s','每跳间隔',1,' 秒'),('tick_count','跳数',1,''),('per_tick_damage','每跳伤害',1,''),('duration_s','持续时间',1,' 秒'),('crit_chance','暴击率',100,'%'),('crit_multiplier','暴击倍率',1,' 倍')]
        for key,label_,scale,suffix in fields:
            if key in component: facts.append((label_,num(component[key]*scale)+suffix))
        target_ = component.get('target') or component.get('target_side') or {}
        if target_: facts.extend([('穿透',num(target_.get('penetration',0)*100)+'%'),('目标结算',num(target_.get('multiplier'))+' 倍')])
        formula=''
        if all(k in component for k in ('per_tick_base','tick_count','increased_total','more_product')) and 'multiplier' in target_:
            formula='<p class="formula-line">'+f"{num(component['per_tick_base'])} 每跳基础 × {num(1+component['increased_total'])} 加算 × {format(component['more_product'],'.6g')} 独立 × {format(target_['multiplier'],'.6g')} 目标 × {component['tick_count']} 跳 = <b>{num(component.get('damage_total'))}</b>"+'</p>'
        applied=[r for r in contributions if cid in ((result.get('contribution_application_summary') or {}).get(r['contribution_id'],r.get('application') or {}).get('components') or [])]
        pools=''
        for op,pool_label in [('increased','加算增伤来源'),('more','独立增伤来源'),('added','基础与附加来源')]:
            rows=[r for r in applied if r.get('operation')==op]
            if rows: pools+=c.detail(pool_label+' · '+str(len(rows))+' 项',source_table(rows))
        damage+=c.section(label,formula+c.fact_list(facts)+pools)
    ailment = details.get('ignite') or {}
    if ailment:
        facts=[]
        for key,label,scale,suffix in [('base_damage','基础伤害',1,''),('per_stack_damage','单层总伤害',1,''),('duration_s','持续时间',1,' 秒'),('increased_total','加算增伤',100,'%'),('more_product','独立增伤',1,' 倍'),('penetration_total','穿透',100,'%')]:
            if key in ailment: facts.append((label,num(ailment[key]*scale)+suffix))
        for key,label in [('roll_count','单次判定次数'),('roll_interval_s','判定间隔（秒）'),('stacks_per_roll','每次期望层数'),('expected_total_stacks_applied','总期望层数'),('uncapped_fire_resistance_percent','施加使用的火焰抗性（%）')]:
            if key in (ailment.get('application') or {}): facts.append((label,num(ailment['application'][key])))
        unit_ids={u.get('component_id') for u in result.get('application_units',[]) if u.get('kind')=='application' and u.get('ailment_id')==ailment.get('ailment')}
        applied=[r for r in contributions if unit_ids.intersection(((result.get('contribution_application_summary') or {}).get(r['contribution_id'],r.get('application') or {}).get('components') or []))]
        extra=c.detail('施加效果的增伤与持续时间来源',source_table(applied)) if applied else ''
        damage+=c.section(name(ailment.get('ailment'),'持续施加效果'),c.fact_list(facts)+extra+c.note('层数为期望值；每个施加批次独立持续、到期。窗口之后的伤害不计入窗口 DPS。'))
    timeline=''
    for index,loop in enumerate(loops):
        label=name(loop.get('label'), '输出策略 '+str(index+1))
        lw=loop.get('windows') or []
        timeline+=c.section(label,c.table(['窗口','施放次数','总伤害','平均 DPS','未结算尾伤'],[[num(w.get('window_s',w.get('seconds')))+' 秒',num(w.get('cast_count',w.get('casts'))),num(w.get('total_damage',w.get('damage'))),num(w.get('dps')),num(w.get('unsettled_tail_damage'))] for w in lw]))
        casts=loop.get('casts')
        if isinstance(casts,list) and casts:
            horizon_=loop.get('horizon_s') or (lw[-1].get('window_s') if lw else 0) or 1
            marks=''.join('<line x1="'+str(30+900*float(x.get('cast_start_s',0))/horizon_)+'" x2="'+str(30+900*float(x.get('cast_start_s',0))/horizon_)+'" y1="18" y2="60"><title>第 '+str(i+1)+' 次 · '+num(x.get('cast_start_s'))+' 秒</title></line>' for i,x in enumerate(casts))
            timeline+=c.card('<svg class="cast-chart" viewBox="0 0 960 90" role="img" aria-label="施放时间分布"><path d="M30 60H930" stroke="#d5dfe8"/><g stroke="#378ca4" stroke-width="3">'+marks+'</g><text x="30" y="82">0 秒</text><text x="870" y="82">'+num(horizon_)+' 秒</text></svg>',title='施放时间分布')
            timeline+=c.detail('逐次施放记录 · '+str(len(casts))+' 次',c.table(['次序','开始（秒）','动作时长（秒）','施放前充能','施放后充能'],[[str(i+1),num(x.get('cast_start_s')),num(x.get('cast_time_s')),num(x.get('charges_before')),num(x.get('charges_after'))] for i,x in enumerate(casts)]))
    groups={}
    for row in contributions:
        item=item_for(row)
        identity=item['instance_id'] if item else (row.get('source_kind'),row.get('source_path'),row.get('source_label'))
        groups.setdefault(identity,[]).append(row)
    source_html=''
    categories=set()
    for rows in groups.values():
        first=rows[0]; item=item_for(first); category=item.get('role','equipment') if item else first.get('source_kind','unknown');categories.add(category)
        active='true' if any(state(r,result)=='生效' for r in rows) else 'false'
        source_html+=ui.source_group(name=source_name(first),caption=SLOTS.get((item or {}).get('slot'),KINDS.get(category,'其它来源')),effects=[(effect(r),c.badge(state(r,result),'ok' if state(r,result)=='生效' else 'muted')) for r in rows],group_attrs='data-category="'+c.esc(category)+'" data-active="'+active+'"')
    sources='<div class="toolbar"><label>搜索 <input id="source-search" type="search" placeholder="装备、节点或属性"></label><label>类别 <select id="source-category"><option value="">全部来源</option>'+''.join('<option value="'+c.esc(k)+'">'+c.esc(KINDS.get(k,'其它来源'))+'</option>' for k in sorted(categories))+'</select></label><label>状态 <select id="source-status"><option value="">全部</option><option value="true">有生效属性</option><option value="false">未作用于伤害</option></select></label></div>'+ui.source_table(body_html=source_html)
    gear=''
    role_counts={}
    total_mods=0
    for index,item in enumerate(equipment):
        role_counts[str(item.get('role') or 'equipment')]=role_counts.get(str(item.get('role') or 'equipment'),0)+1
        total_mods+=sum(len(rows) for _channel,rows in gear_mod_groups(item)[0])
        gear+=gear_card(item,index)
    summary=' · '.join([ROLE_LABELS.get(role,KINDS.get(role,role))+' '+str(count)+' 件' for role,count in sorted(role_counts.items())]) + ' · 合计 ' + str(total_mods) + ' 条词条'
    gear_intro=c.note('装备页展示当前实例的全部已解码词条（含未参与计算、防御/功能属性）；不按主技能 DPS 相关性筛选，展示不代表已计入 DPS。')+'<p class="muted">'+c.esc(summary)+'</p>'
    controls=details.get('controls') or []
    target_controls=(binding or {}).get('target_controls') or details.get('target_controls') or []
    buffs=''
    target_controls_html=''
    for control in controls:
        key=control['key']; value=states.get(key,control.get('default')); disabled=' disabled' if control.get('readonly') else ''
        if control.get('type')=='boolean': field='<label><input type="checkbox" data-state="'+c.esc(key)+'"'+(' checked' if value else '')+disabled+'>启用</label>'
        else: field='<label><input type="checkbox" data-enable="'+c.esc(key)+'"'+(' checked' if value else '')+disabled+'>启用</label><input aria-label="'+c.esc(control['label'])+' 层数" class="stack-input" type="number" data-state="'+c.esc(key)+'" value="'+str(value or 1)+'" min="'+str(control.get('min',0))+'" max="'+str(control.get('max',100))+'"'+disabled+(' disabled' if not value and not disabled else '')+'>'
        if control.get('source')=='目标条件':
            target_controls_html+='<div class="target-condition"><b>'+c.esc(control['label'])+'</b>'+field+'</div>'
            continue
        if control.get('readonly'):
            field='<input type="checkbox" data-state="'+c.esc(key)+'" hidden'+(' checked' if value else '')+'> '
        status_label=('按目标判定' if control.get('readonly') else ('仅本次施法' if control.get('single_cast_only') else '已启用')) if value else '未启用'
        buffs+=ui.effect_card(title=control['label'],subtitle=('条件效果' if 'legacy' in str(control.get('source')) else control.get('source') or ''),status_label=status_label,control_html=field,value_html='<p>'+c.esc(control.get('effect') or '')+'</p>',variant='hit')
    toolbar='<div class="toolbar"><button class="btn" id="save-config">保存配置</button><button class="btn" id="load-config">载入配置</button><button class="btn" id="reset-buffs">重置 Buff</button><input type="file" id="config-file" accept=".json" hidden></div>'
    drawers='<div id="target-backdrop" hidden></div><aside id="target-drawer" aria-label="目标设置" hidden><h2>目标设置</h2><button id="close-target" class="btn">关闭</button><label>怪物等级<input id="target-level" type="number" min="1" max="100" value="'+str(target.get('monster_level',100))+'"></label><label>抗性（%）<input id="target-resistance" type="number" min="-100" max="75" value="'+str(target.get('resistance',0)*100)+'"></label>'
    for key,id_,label in [('high_health','high','高生命目标'),('boss','boss','Boss'),('blinded','blinded','致盲'),('slowed','slowed','减速')]: drawers+='<label>'+label+'<input type="checkbox" id="target-'+id_+'"'+(' checked' if target.get(key) else '')+'></label>'
    drawers+=target_controls_html
    for control in target_controls:
        key=control.get('key'); value=target.get(key,control.get('default')); disabled=' disabled' if control.get('readonly') else ''
        if control.get('type')=='boolean':
            field='<label><input type="checkbox" data-target-state="'+c.esc(key)+'"'+(' checked' if value else '')+disabled+'>启用</label>'
        else:
            field='<label><input class="stack-input" type="number" data-target-state="'+c.esc(key)+'" value="'+str(value if value is not None else 0)+'" min="'+str(control.get('min',0))+'" max="'+str(control.get('max',100))+'"'+disabled+'></label>'
        drawers+='<div class="target-condition"><b>'+c.esc(control.get('label') or key)+'</b>'+field+'</div>'
    drawers+='<button class="btn" id="reset-target">重置目标</button><p class="muted">修改后点击“计算 DPS”应用。只有适用的目标条件影响伤害。</p></aside>'
    bodies={'overview':overview,'damage':damage or c.empty('当前结果未提供乘区明细。'),'windows':timeline,'sources':sources,'equipment':gear_intro+'<div class="gear-grid">'+gear+'</div>','buffs':toolbar+'<div class="buff-grid">'+buffs+'</div>'}
    pages=[{'id':k,'label':v} for k,v in [('overview','总览'),('damage','伤害构成'),('windows','时间与循环'),('sources','增伤来源'),('equipment','装备与神像'),('buffs','Buff 设置')]]
    assets=Path(__file__).with_name('assets')
    # 浏览器复用统一配置/重算协议；展示细节不参与计算。
    client_result=dict(result,controls=controls,report_details=dict(details,equipment=equipment))
    return render_document(DocumentSpec(model='dps',title=title+' · 伤害分析',brand_mark='LE',heading=title,subtitle='构筑伤害分析 · 固定条件预计值',result=client_result,view={'defaults':{'states':states,'target':target,'target_controls':target_controls}},pages=pages,page_html={p['id']:c.page(p['id'],p['label'],'',bodies[p['id']]) for p in pages},first_page='overview',header_summary_html=c.esc(str(target.get('monster_level',100))+' 级 · 抗性 '+num(target.get('resistance',0)*100)+'%'),header_controls_html='<button class="btn btn-primary" id="compute">计算 DPS</button><span id="compute-status" role="status">当前结果已应用</span>',drawers_html=drawers,number_toggle=True,bindings=binding or None,recalc_endpoint=binding.get('recompute_endpoint'),model_scripts=('shell.js',),inline_scripts=((assets/'unified.js').read_text(encoding='utf-8'),),extra_css=(assets/'dps.css').read_text(encoding='utf-8')+(assets/'unified.css').read_text(encoding='utf-8'),footer_text='预计伤害 · 固定条件计算；实际战斗受走位、覆盖率与目标状态影响。'))
