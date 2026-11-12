"""统一 DPS 报告：共享外壳、来源组件、效果卡及回环重算服务。"""
import json, math, re, uuid, threading
from pathlib import Path
from html import escape as esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import recalc
from .report_ui import components as c, shared_ui as ui
from .report_ui.document import DocumentSpec, render_document, renderer_fingerprint, UI_CONTRACT_VERSION

def _legacy_dps():
    """Legacy profile renderer 需要时才加载；统一 v4/v5/v6 生产路径不加载旧 dps。"""
    from . import dps as legacy
    return legacy


LABELS={'damage':'伤害加成','added_damage':'附加伤害','crit_chance':'暴击率','crit_multiplier':'暴击伤害倍率','attack_speed':'攻击速度','cooldown_recovery_speed':'冷却恢复','cooldown_reduction':'冷却缩减','penetration':'穿透','ailment_chance':'异常施加概率','ailment_duration':'异常持续时间','movement_speed':'移动速度','effect_of':'状态效果','damage_taken':'受到伤害','attribute':'属性','all_attributes':'所有属性','attribute_conversion':'力量转换为 Brutality','state_marker':'状态标记','skill_level':'技能等级','unsupported':'其它属性'}
TYPES={'Physical':'物理','Fire':'火焰','Cold':'冰霜','Lightning':'闪电','Void':'虚空','Necrotic':'腐化','Poison':'毒素'}
SLOTS={'head':'头盔','chest':'胸甲','weapon1':'主手','weapon2':'副手','hands':'手套','waist':'腰带','feet':'鞋子','ring1':'左戒指','ring2':'右戒指','amulet':'项链','relic':'圣物','idol_altar':'神像祭坛'}
COVERAGE_STATUS={'complete':'完整','partial':'部分'}

def fingerprints(root,case):
    profile=_legacy_dps().read(root/case['dps_profile'])
    return _legacy_dps().dependency_fingerprints(root,case,profile)

def bindings(root,case,out,port):
    endpoint=recalc.loopback_endpoint(port);fp=fingerprints(root,case);renderer=renderer_fingerprint()
    ref=recalc.build_report_ref(case_id=case['case_id'],model_id='dps',run_id=out.name,run_dir=out.relative_to(root).as_posix(),report_path=(out/'dps-report.html').relative_to(root).as_posix(),input_fingerprints=fp,recompute_endpoint=endpoint,ui_contract_version=UI_CONTRACT_VERSION,renderer_fingerprint=renderer)
    return recalc.build_recalc_bindings(case_id=case['case_id'],model_id='dps',capabilities_version='dps-report-v3',ui_contract_version=UI_CONTRACT_VERSION,renderer_fingerprint=renderer,report_ref=ref,input_fingerprints=fp,recompute_endpoint=endpoint)

def clean(name):
    name=re.sub(r'\s*\[(?:equipment|idols|blessings|passive node|.*tree node).*?\]','',str(name))
    return '当前来源' if name.startswith(('mod:','passive:','skill:','rule:','effect:','attribute_conversion:')) else name

def row_effect(row):
    label=LABELS.get(row['stat_key'],'其它属性');v=row['value'];op=row['operation']
    if row['stat_key']=='attribute_conversion':return format(v*100,'.0f')+'% 力量转为 Brutality（保留等效力量）'
    if row['stat_key']=='attribute':return str(row.get('target') or '属性')+' '+format(v,'+g')
    if row['stat_key']=='state_marker':return '状态标记'
    if op=='more':return label+' '+format(v*100,'+.2f')+'% 独立'
    if op=='increased' or row['stat_key'] in ('crit_chance','crit_multiplier','penetration','cooldown_recovery_speed','cooldown_reduction','ailment_chance','ailment_duration','movement_speed','effect_of'):return label+' '+format(v*100,'+.2f')+'%'
    return label+' '+format(v,'+g')

def row_status(row,result=None):
    key=row['stat_key']
    if key=='unsupported':return '其它角色属性'
    if key in ('attribute','all_attributes','attribute_conversion'):return '生效'
    if key in ('state_marker','skill_level'):return '来源标记'
    application=(result or {}).get('contribution_application_summary',{}).get(row['contribution_id']) or row.get('application')
    if application:
        if application.get('components'):return '生效'
        statuses=application.get('statuses') or {}
        if statuses.get('unresolved'):return '未解析'
        if statuses.get('condition_not_satisfied'):return '条件未满足'
        if statuses.get('duplicate'):return '重复已抑制'
        if statuses.get('not_applicable'):return '不适用于当前组件'
    if not row['included']:return '未启用' if row.get('exclusion_reason') in ('当前场景未启用',None) else '未计入本次伤害'
    if row.get('condition_status') not in ('always','satisfied'):return '条件未满足'
    if key in ('ailment_chance','ailment_duration'):return '持续伤害来源'
    return '生效'

def source_projection(result):
    items={i['instance_id']:i for i in result['equipment']};groups={}
    for row in result['contributions']:
        cid=row['contribution_id'];item_id=next((x for x in items if cid.startswith('mod:'+x+'#') or cid.startswith(x+'#') or cid.startswith(x+':')),None)
        if item_id:
            item=items[item_id];name=item['display_name'];caption=SLOTS.get(item.get('slot'),{'idol':'神像','blessing':'祝福'}.get(item['role'],'装备'));category=item['role'];identity=item_id
        else:
            name=clean(row['source_label']);caption={'passive_node':'职业被动','skill_node':result['skill'],'base_character':'角色基础','derived':'属性计算','equipment':'装备条件效果'}.get(row['source_kind'],'当前构筑');category=row['source_kind'];identity=caption+':'+name
        group=groups.setdefault(identity,{'name':name,'caption':caption,'category':category,'rows':[]});group['rows'].append(row)
    return list(groups.values())

def render_v5(result, binding):
    """plan-v3 / result-v5（BH 纯 DoT + 独立施加时钟 + 充能调度）静态报告。"""
    n=lambda x:format(float(x),',.2f')
    loops=result.get('loops') or []
    primary=next((item for item in loops if item.get('primary')), loops[0] if loops else {})
    totals=primary.get('totals') or {}
    single=(result.get('single_cast') or {}).get('totals') or {}
    body=(result.get('body') or {}).get('totals') or {}
    coverage=result.get('coverage') or {'status':'partial','gap_count':0}
    target=result.get('scenario',{}).get('target') or {}
    kpis=(c.kpi('主策略预期 DPS',c.num(totals.get('horizon_dps')))
          +c.kpi('单次施放总伤害',c.num(single.get('single_cast_total')))
          +c.kpi('单次本体 / Ignite',n(single.get('body_total'))+' / '+n(single.get('ignite_total')))
          +c.kpi('120 秒施放次数',str(primary.get('cast_count') or 0))
          +c.kpi('最大并发黑洞',str(primary.get('max_concurrent') or 0))
          +c.kpi('计算覆盖',COVERAGE_STATUS.get(coverage.get('status'),coverage.get('status'))+'；'+str(coverage.get('gap_count') or 0)+' 项缺口'))
    loop_rows=[[str(item.get('policy_id')),str(item.get('cast_count')),str(item.get('max_concurrent')),n((item.get('totals') or {}).get('horizon_damage')),n((item.get('totals') or {}).get('horizon_dps'))] for item in loops]
    applications=result.get('applications') or []
    units=result.get('application_units') or []
    unit_rows=[]
    for unit in units:
        uid=unit.get('component_id')
        rows=[record for record in applications if record.get('component_id')==uid]
        statuses={}
        for record in rows:
            statuses[record.get('status')]=statuses.get(record.get('status'),0)+1
        unit_rows.append([str(uid),str(unit.get('kind')),str(statuses.get('applied',0)),str(statuses.get('not_applicable',0)),str(statuses.get('condition_not_satisfied',0)),str(statuses.get('unresolved',0))])
    ledger_note='每条 contribution 都登记为 applied 或带原因的排除（标签/scope/条件/未解析）；来源页状态与覆盖投影均读取该账本。'
    overview=c.section('当前预计伤害','<div class="kpi-grid">'+kpis+'</div>')
    overview+=c.section('组件级应用账本',c.table(['组件','类型','生效','不适用','条件未满足','未解析'],unit_rows)+c.note(ledger_note))
    overview+=c.section('充能调度对比（120 秒）',c.table(['策略','施放次数','最大并发','总伤害','预计 DPS'],loop_rows))
    overview+=c.note('多实例并发由充能调度决定：eager 允许新旧黑洞同时存在，patient 等待现有黑洞消失；两者共享同一场景与输入，仅 action policy 不同。')
    windows=''
    for loop in loops:
        rows=[[str(w['window_s'])+' 秒',str(w['cast_count']),n(w['body_damage']),n(w['ignite_damage']),n(w['dps']),n(w['unsettled_tail_damage'])] for w in loop.get('windows') or []]
        windows+=c.section(str(loop.get('policy_id'))+' 观察窗口',c.table(['窗口','施放次数','本体伤害','Ignite 伤害','预计 DPS','未结算尾伤'],rows))
    windows+=c.note('窗口内只计入窗口前已发生伤害；窗口前已施加、窗口后才结算的 Ignite 计入未结算尾伤，不重复计入 DPS。')
    damage=''
    applied_by_unit={}
    for record in applications:
        if record.get('status')=='applied':
            applied_by_unit.setdefault(record.get('component_id'),[]).append(record)
    for component in result.get('components') or []:
        formula=f"({n(component['per_tick_base'])} 每跳 × {component['tick_count']} 跳) × {n(1+component['increased_total'])} 加算 × ×{component['more_product']:.6g} more × {component['target']['multiplier']:.6g} 目标 = {n(component['damage_total'])}"
        facts=c.fact_list([('伤害类型',TYPES.get(component['damage_type'],component['damage_type'])),('基础 + 附加',n(component['base_damage'])+' + '+n(component['added_damage_total'])+' × '+format(component['added_damage_scaling'],'g')),('tick 间隔 / 数量',format(component['tick_interval_s'],'g')+' 秒 × '+str(component['tick_count'])),('加算增伤',format(component['increased_total']*100,'.2f')+'%'),('独立增伤乘区','×'+format(component['more_product'],'.6g')),('穿透',format(component['penetration_total']*100,'.2f')+'%'),('目标结算',format(component['target']['multiplier'],'.6g')+'（抗性 '+format(component['target']['resistance']*100,'.2f')+'%，怪物 DR '+format(component['target']['monster_damage_reduction']*100,'.0f')+'%）')])
        applied=applied_by_unit.get(component['component_id'],[])
        applied_rows=[[esc(str(record.get('contribution_id'))),str(record.get('role')),str(record.get('stat_key'))] for record in applied[:40]]
        details=c.detail('查看计入本组件的贡献（%d 条）'%len(applied),c.table(['贡献','角色','属性'],applied_rows)) if applied_rows else c.note('本组件没有生效的贡献行（基础伤害仍按计划常量计算）')
        damage+=c.section('本体 DoT · '+component['component_id'],'<p class="formula-line">'+esc(formula)+'</p>'+facts+details)
    ignite=result.get('ignite') or {}
    ignite_facts=[('单层伤害',n(ignite.get('per_stack_damage'))),('持续时间',n(ignite.get('duration_s'))+' 秒（×'+format(ignite.get('duration_multiplier') or 1,'.6g')+'）'),('加算增伤',format((ignite.get('increased_total') or 0)*100,'.2f')+'%'),('独立增伤乘区','×'+format(ignite.get('more_product') or 1,'.6g')),('穿透',format((ignite.get('penetration_total') or 0)*100,'.2f')+'%')]
    app=(ignite.get('application') or {})
    ignite_facts += [('未封顶火焰抗性',n(app.get('uncapped_fire_resistance_percent'))+'%'),('每判定期望层数',format(float(app.get('stacks_per_roll') or 0),'.6g')),('单次黑洞判定期数',str(app.get('roll_count') or 0)),('单次施放期望总层数',n((result.get('single_cast') or {}).get('totals',{}).get('expected_total_stacks_applied')))]
    damage+=c.section('Ignite（独立施加时钟）',c.fact_list(ignite_facts)+c.note('每次 Heat Wave 判定独立计时并各自到期；层数按期望值模型，不做整数事件抽样。'))
    source_rows=[[clean(row.get('source_label')),row_effect(row),row_status(row)] for row in (result.get('contributions') or []) if row.get('included')]
    source_page=c.section('计入本次伤害的来源',c.table(['来源','效果','状态'],source_rows[:220]))
    gear=''
    for item in result.get('equipment') or []:
        lines=[]
        for m in item.get('mods') or []:
            text=m.get('readable_text')
            if not text:
                stat=m.get('stat') or {};name=stat.get('name')
                if not name or name.startswith(('propertyList','stat:')):continue
                val=m.get('value_numeric');unit=stat.get('unit')
                text=(format(val*100,'+.2f')+'% ' if val is not None and unit=='percent' else (format(val,'+g')+' ' if val is not None else ''))+name
            if text not in lines:lines.append(text)
        slot=SLOTS.get(item.get('slot'),'神像' if item.get('role')=='idol' else '祝福')
        count=len(lines)
        gear+=c.card('<p class="muted">'+esc(slot+' · '+str(item.get('base_name') or ''))+'</p><p class="muted">'+str(count)+' 条词条（完整清单见 result/plan）</p>',title=item.get('display_name'),cls='gear-card')
    gear=c.section('装备与来源实例','<div class="gear-grid">'+gear+'</div>') if gear else ''
    pages=[{'id':'overview','label':'总览'},{'id':'windows','label':'观察窗口'},{'id':'damage','label':'DoT 乘区'},{'id':'sources','label':'增伤来源'},{'id':'equipment','label':'装备列表'}]
    bodies={'overview':overview,'windows':windows,'damage':damage,'sources':source_page,'equipment':gear or '<p class="muted">无装备数据</p>'}
    page_html={item['id']:c.page(item['id'],item['label'],'',bodies[item['id']]) for item in pages}
    script=(Path(__file__).with_name('report_ui')/'assets/dps.js').read_text(encoding='utf-8')
    css=(Path(__file__).with_name('report_ui')/'assets/dps.css').read_text(encoding='utf-8')
    footer='固定条件预计伤害；范围：'+str(result.get('scope') or '')+'。覆盖状态：'+str(coverage.get('status'))+'。'
    spec=DocumentSpec(model='dps',title=str((result.get('skill') or {}).get('name') or result.get('case_id'))+' · BH DoT 报告',brand_mark='LE',heading=str((result.get('skill') or {}).get('name') or ''),subtitle='纯 DoT 本体 + Ignite 期望层数 · 充能调度（eager/patient）',result=result,view={'defaults':{'states':{x['key']:x['default'] for x in result.get('controls') or []},'target':{'monster_level':target.get('monster_level',100),'resistance':target.get('resistance',0.0),'high_health':False,'boss':True,'blinded':False,'slowed':False}}},pages=pages,page_html=page_html,first_page='overview',header_summary_html=str(target.get('monster_level','100'))+' 级 · 抗性 '+str(round(float(target.get('resistance',0.0))*100,2))+'%',drawers_html='',model_scripts=('shell.js',),inline_scripts=(script,),extra_css=css,bindings=binding,recalc_endpoint=binding.get('recompute_endpoint',''),footer_text=footer)
    return render_document(spec)


def render(result,binding):
    if result.get('schema')=='le-dps-result-v6':
        from . import plan_v4
        return plan_v4.render_report_v6(result, binding)
    if result.get('schema')=='le-dps-result-v5':
        return render_v5(result,binding)
    n=lambda x:format(x,',.2f');factor=lambda x:'×'+format(x,'.4f').rstrip('0').rstrip('.')
    states=result['scenario']['states'];target=result['scenario']['target'];sources=source_projection(result);coverage=result.get('coverage') or {'status':'complete','gap_count':0}
    coverage_label=COVERAGE_STATUS.get(coverage['status'],coverage['status'])+('；'+str(coverage['gap_count'])+' 项缺口' if coverage['gap_count'] else '')
    kpis=c.kpi('持续预计 DPS',c.num(result['sustained_dps']))+c.kpi('单次本体命中',c.num(result['single_hit']))+c.kpi('本体 / 持续 DPS',n(result['hit_dps'])+' / '+n(result['dot_dps']))+c.kpi('施放间隔',n(result['interval_s'])+' 秒')+c.kpi('计算覆盖',coverage_label)
    window_rows=[[str(w['seconds'])+' 秒',str(w['casts']),n(w['hit_damage']),n(w['dot_damage']),n(w['dps'])] for w in result['windows']]
    overview=c.section('当前预计伤害','<div class="kpi-grid">'+kpis+'</div>')+c.section('观察窗口',c.table(['窗口','施放次数','本体伤害','持续伤害','预计 DPS'],window_rows))+c.note('固定条件估算：选中的 Buff 和目标层数在观察期内保持。首次施放在 0 秒；窗口不含右端点。')
    if result.get('schema')=='le-dps-result-v4':
        hits=result.get('hits_per_cast') or {}
        evaluation_rows=[[str(e.get('evaluation_id')),str(e.get('label') or ''),str(e.get('scenario_sha256') or '')[:12]] for e in result.get('evaluations') or []]
        policy_rows=[[str(pid),n(p.get('interval_s')),n(p.get('casts_per_second')),n(p.get('hits_per_second')),n(p.get('damage_per_cast')),n(p.get('damage_per_second'))] for pid,p in (result.get('policies') or {}).items()]
        weights=hits.get('damage_weights') or []
        overview+=c.section('多命中与频率',c.fact_list([('每次施放命中数',str(hits.get('per_cast') or '')),('伤害权重当量',n(hits.get('effective_hits_per_cast'))),('逐击伤害权重',' / '.join(format(float(w),'g') for w in weights) or '—'),('每秒施放数',n(result.get('casts_per_second'))),('每秒命中数',n(result.get('hits_per_second'))),('当前频率策略',str(result.get('policy') or '')),('周期平均因子',factor(result.get('rotation_more_factor') or 1.0))])+c.table(['评估','名称','场景哈希前 12 位'],evaluation_rows)+c.table(['频率策略','施放间隔（秒）','每秒施放','每秒命中','每次施放伤害','预计 DPS'],policy_rows))
        overview+=c.note('单次命中评估只表示首击伤害；持续输出评估才计入周期平均因子。逐击权重与 Hit/Cast 计数分开记录在 result.json。')
    if coverage['status']=='partial':
        overview+=c.note('覆盖状态为部分：'+str(coverage['gap_count'])+' 项未建模或未验证的伤害相关来源，见 result.json 的 coverage.gaps；数值只覆盖已建模范围。')
    overview+=c.section('频率与冷却',c.fact_list([('冷却',n(result['cooldown_s'])+' 秒'),('动作时长',n(result['action_s'])+' 秒'),('冷却恢复',format(result['cooldown_recovery']*100,'.1f')+'%'),('攻击速度加成',format(result['attack_speed_increased']*100,'.1f')+'%')]))
    damage=''
    by_id={r['contribution_id']:r for r in result['contributions']}
    for component in result['components']:
        title=TYPES[component['damage_type']]+'命中'
        formula=f"({n(component['base'])} + {n(component['added'])} × {component['added_effectiveness']:g}) {factor(component['increased_multiplier'])} {factor(component['more_multiplier'])} {factor(component['average_crit'])} {factor(component['target_multiplier'])} = {n(component['damage'])}"
        rows=[]
        for cid in component['source_ids']:
            r=by_id.get(cid)
            if r:rows.append([clean(r['source_label']),row_effect(r),row_status(r,result)])
        details=c.detail('查看本分量的来源',c.table(['来源','效果','状态'],rows))
        facts=c.fact_list([('基础与附加',n(component['subtotal'])),('加算增伤池',factor(component['increased_multiplier'])),('独立增伤池',factor(component['more_multiplier'])),('暴击期望',factor(component['average_crit'])),('暴击率',format(component['crit_chance']*100,'.2f')+'%'),('暴击倍率',factor(component['crit_multiplier'])),('抗性穿透',format(component['penetration']*100,'.2f')+'%')])
        damage+=c.section(title,'<p class="formula-line">'+esc(formula)+'</p>'+facts+details)
    conversion=result.get('attribute_conversion')
    if conversion:
        damage+=c.section('Strength → Brutality',c.fact_list([
            ('转换前力量',n(conversion['strength_before'])),('转换后力量 / Brutality',n(conversion['strength'])+' / '+n(conversion['brutality'])),
            ('技能与节点使用的等效力量',n(conversion['effective_strength'])),('主技能当前法力消耗',n(conversion['mana_cost'])+'（规则上限 '+n(conversion['mana_cost_cap'])+'，实际计入 '+n(conversion['capped_mana_cost'])+'）'),
            ('独立近战命中倍率',factor(conversion['more_multiplier'])),('全局生命偷取属性修正',format(-100*conversion['health_leech_reduction'],'.2f')+'%（未模拟回复量）')
        ])+c.note('Brutality 计作力量参与技能、节点与力量要求；转换部分不再提供力量护甲。独立增伤只作用于声明的命中组件（'+'、'.join(conversion.get('damage_tags') or ['命中'])+'），不加入持续伤害组件。'))
    ailment_rows=[[a['name'],format(a['chance']*100,'.1f')+'%',n(a['duration_s'])+' 秒',n(a['stack_damage']),n(a['damage_per_cast'])] for a in result['ailments']]
    damage+=c.section('自然施加的持续伤害',c.table(['效果','每次施加概率','持续时间','单层完整伤害','每次施放完整伤害'],ailment_rows))+c.note('持续伤害按施放后的实际覆盖时间积分。Crit 不作用于持续伤害组件；触发类持续伤害不属于本报告范围。')
    source_bodies=[]
    for group in sources:
        effects=[];statuses=[]
        for r in group['rows']:
            status=row_status(r,result);statuses.append(status)
            text=row_effect(r) if r['stat_key']!='unsupported' else '角色其它属性（见装备明细）'
            effects.append((esc(text),esc(status)))
        search=esc(group['name']+' '+group['caption']);attrs=f'data-search="{search}" data-category="{esc(group["category"])}" data-active="{str("生效" in statuses).lower()}"'
        source_bodies.append(ui.source_group(name=group['name'],caption=group['caption'],group_attrs=attrs,effects=effects))
    filters='<div class="toolbar"><label>搜索 <input id="source-search" placeholder="装备、节点或效果"></label><label>类别 <select id="source-category"><option value="">全部</option><option value="equipment">装备</option><option value="idol">神像</option><option value="blessing">祝福</option><option value="skill_node">技能</option><option value="passive_node">被动</option></select></label><label>状态 <select id="source-status"><option value="">全部</option><option value="true">生效</option><option value="false">其它</option></select></label></div>'
    source_page=filters+ui.source_table(body_html=''.join(source_bodies))
    gear=''
    for role,title in [('equipment','装备'),('idol','神像'),('blessing','祝福')]:
        cards=[]
        for i in result['equipment']:
            if i['role']!=role:continue
            lines=[]
            for m in i.get('mods',[]):
                text=m.get('readable_text')
                if not text:
                    stat=m.get('stat') or {};name=stat.get('name')
                    if not name or name.startswith(('propertyList','stat:')):continue
                    val=m.get('value_numeric');unit=stat.get('unit')
                    name=('召唤物 · ' if (m.get('tags') or 0)&8192 else '')+name
                    text=(format(val*100,'+.2f')+'% ' if val is not None and unit=='percent' else (format(val,'+g')+' ' if val is not None else ''))+name
                if text not in lines:lines.append(text)
            slot=SLOTS.get(i.get('slot'),'神像' if role=='idol' else '祝福')
            body='<p class="muted">'+esc(slot+' · '+str(i.get('base_name') or ''))+'</p><ul>'+''.join('<li>'+esc(s)+'</li>' for s in lines)+'</ul>'
            cards.append(c.card(body,title=i['display_name'],cls='gear-card'))
        gear+=c.section(title,'<div class="gear-grid">'+''.join(cards)+'</div>')
    buff_cards=[]
    for control in result['controls']:
        key=control['key'];value=states[key];readonly=control.get('readonly');checked=' checked' if value else '';disabled=' disabled' if readonly else ''
        if control['type']=='boolean':
            control_html=f'<label class="switch"><input type="checkbox" data-state="{esc(key)}"{checked}{disabled}> '+('常驻' if readonly else '启用')+'</label>'
        else:
            control_html=f'<label><input type="checkbox" data-enable="{esc(key)}"{checked}> 启用</label><label>层数 <input class="stack-input" type="number" data-state="{esc(key)}" min="0" max="{control["max"]}" value="{value or 1}"'+('' if value else ' disabled')+'></label>'
        effective=bool(value) and (target['boss'] or key!='ambition') and (not control.get('requires_target') or target[control['requires_target']])
        status='生效' if effective else ('目标条件未满足' if value else '未启用')
        buff_cards.append(ui.effect_card(title=control['label'],subtitle=control['source'],status_label=status,status_tone='ok' if effective else 'muted',control_html=control_html,value_html='<p>'+esc(control['effect'])+'</p>',attrs=f'data-buff-card="{esc(key)}"'))
    actions=ui.action_bar('<button class="btn" id="save-config">保存配置</button><button class="btn" id="load-config">载入配置</button><button class="btn" id="reset-buffs">重置 Buff</button><button class="btn btn-primary" id="compute">计算 DPS</button><input hidden type="file" accept="application/json" id="config-file">',aria_label='Buff 操作',status_html='<span id="compute-status" role="status">已应用当前场景</span>')
    buffs=actions+c.note('开关/层数修改后，点击“计算 DPS”应用。所选条件固定保持，不自动推定可以同时自然维持。')+'<div class="buff-grid">'+''.join(buff_cards)+'</div>'
    pages=[{'id':key,'label':label} for key,label in [('overview','总览'),('damage','乘区分析'),('sources','增伤来源'),('equipment','装备列表'),('buffs','Buff 设置')]]
    page_html={key:c.page(key,label,'',body) for (key,label),body in zip([(p['id'],p['label']) for p in pages],[overview,damage,source_page,gear,buffs])}
    drawer=f'<div id="target-backdrop" hidden></div><aside id="target-drawer" hidden aria-label="目标设置"><h2>目标设置</h2><label>等级 <input id="target-level" type="number" min="1" max="100" value="{target["monster_level"]}"></label><label>有效抗性（%） <input id="target-resistance" type="number" min="-100" max="75" value="{target["resistance"]*100:g}"></label><label><input id="target-high" type="checkbox"'+(' checked' if target['high_health'] else '')+'> 高血量目标</label><label><input id="target-boss" type="checkbox"'+(' checked' if target['boss'] else '')+'> 首领或稀有目标</label><p>有效抗性统一作用于本报告各伤害类型，不包含敌方护甲模拟。</p><button class="btn" id="reset-target">重置目标</button><button class="btn" id="close-target">完成设置</button></aside>'
    extra_targets='<label><input id="target-blinded" type="checkbox"'+(' checked' if target['blinded'] else '')+'> 致盲目标</label><label><input id="target-slowed" type="checkbox"'+(' checked' if target['slowed'] else '')+'> 减速目标</label>'
    drawer=drawer.replace('<p>有效抗性',extra_targets+'<p>有效抗性')
    script=(Path(__file__).with_name('report_ui')/'assets/dps.js').read_text(encoding='utf-8')
    css=(Path(__file__).with_name('report_ui')/'assets/dps.css').read_text(encoding='utf-8')
    footer='固定条件预计伤害；范围：'+result['scope']+'。覆盖状态：'+coverage['status']+'。'
    spec=DocumentSpec(model='dps',title=result['skill']+' · DPS 报告',brand_mark='LE',heading=result['skill'],subtitle=result['character']['mastery']['name']+' · 本体命中 + 自然异常',result=result,view={'defaults':{'states':{x['key']:x['default'] for x in result['controls']},'target':{'monster_level':100,'resistance':0.,'high_health':False,'boss':True,'blinded':False,'slowed':False}}},pages=pages,page_html=page_html,first_page='overview',header_summary_html=f'{target["monster_level"]} 级 · 抗性 {target["resistance"]*100:g}%',drawers_html=drawer,model_scripts=('shell.js',),inline_scripts=(script,),extra_css=css,bindings=binding,recalc_endpoint=binding['recompute_endpoint'],footer_text=footer)
    return render_document(spec)


def _serve_unified(root, case, report, port):
    """统一计划报告重算：读取已保存 plan/scenario，复用 execute_plan。"""
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from . import calculation_plan as planmod
    from . import calculation_route
    from . import plan_executor

    binding = _legacy_dps().read(report / 'report-binding.json')
    if recalc.endpoint_port(binding['recompute_endpoint']) != port:
        raise ValueError('端口与所选报告不匹配，请用对应端口')
    plan = planmod.load_plan(report / 'calculation-plan.json')
    saved_scenario = _legacy_dps().read(report / 'scenario.json')
    saved_scenario = {
        'states': saved_scenario.get('states') or {},
        'target': saved_scenario.get('target') or {},
    }
    if plan.get('schema') == 'le-calculation-plan-v4':
        from . import plan_v4
        try:
            plan_v4.validate_plan_v4(plan)
        except plan_v4.PlanV4Error as exc:
            raise ValueError('报告计划结构校验失败：' + str(exc))
    else:
        plan_issues = planmod.validate_plan_structure(plan)
        if plan_issues:
            raise ValueError('报告计划结构校验失败：' + plan_issues[0].get('code', ''))
    tracker = recalc.RevisionTracker(run_id=report.name)
    lock = _threading.Lock()
    route = calculation_route.resolve_calculation_route(dict(case), root=root)
    expected_fingerprints = calculation_route.dependency_fingerprints(
        root=root, route=route, plan=None
    )
    if binding.get('input_fingerprints') != expected_fingerprints:
        raise ValueError('报告依赖与当前输入/实现不一致，请生成新报告')

    def verify_plan_inputs() -> None:
        for ref in plan.get('input_refs') or []:
            ref_path = ref.get('path')
            if not ref_path:
                continue
            absolute = Path(root) / ref_path
            if not absolute.is_file():
                raise ValueError(f'报告依赖文件缺失，请生成新报告：{ref_path}')
            if planmod.sha256_file(absolute) != ref.get('sha256'):
                raise ValueError(f'报告依赖已变化，请生成新报告：{ref_path}')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, data, content_type='application/json; charset=utf-8'):
            body = data.encode('utf-8') if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.end_headers()

        def do_GET(self):
            if self.path in ('/', '/dps-report.html'):
                self.send(200, (report / 'dps-report.html').read_text(encoding='utf-8'), 'text/html; charset=utf-8')
            else:
                self.send(404, {'error': '页面不存在'})

        def do_POST(self):
            if self.path != '/recompute':
                self.send(404, {'error': '接口不存在'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 150000:
                    raise ValueError('请求大小无效')
                p = json.loads(self.rfile.read(size))
                if set(p) != {'binding', 'scenario', 'request_id', 'client_session_id', 'client_edit_revision'}:
                    raise ValueError('请求字段不完整或不受支持')
                session = p.get('client_session_id')
                revision = p.get('client_edit_revision')
                request = p.get('request_id')
                if p.get('binding') != binding:
                    raise ValueError('报告绑定不匹配，请打开当前报告')
                verify_plan_inputs()
                if calculation_route.dependency_fingerprints(root=root, route=route, plan=None) != binding['input_fingerprints']:
                    raise ValueError('输入或规则已经变化，请生成新报告')
                if not isinstance(session, str) or not session or not isinstance(request, str):
                    raise ValueError('请求身份无效')
                if plan.get('schema') == 'le-calculation-plan-v4':
                    scenario_doc = dict(p.get('scenario') or {})
                else:
                    scenario = plan_executor.validate_scenario(plan, p.get('scenario'))
                    scenario_doc = {'states': scenario[0], 'target': scenario[1]}
                with lock:
                    issue = tracker.check(revision, request_id=request, session_id=session)
                    if issue:
                        raise ValueError(issue['code'])
                    result = plan_executor.execute_plan(plan, scenario_doc)
                    html = render(result, binding)
                    accepted = tracker.accept(revision, request_id=request, session_id=session)
                self.send(200, {
                    'binding': binding,
                    'request_id': request,
                    'client_session_id': session,
                    'accepted_revision': accepted,
                    'result': result,
                    'html': html,
                })
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                self.send(422, {'error': str(exc)})
            except Exception:
                self.send(500, {'error': '本次计算失败，请重试；已有结果保持不变'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'DPS 报告（{case.get("case_id")}）：http://127.0.0.1:{port}/', flush=True)
    server.serve_forever()


def serve(root,case,report,port):
    if (report / 'calculation-plan.json').is_file() and (report / 'result.json').is_file():
        return _serve_unified(root, case, report, port)
    profile=_legacy_dps().read(root/case['dps_profile']);binding=_legacy_dps().read(report/'report-binding.json')
    if recalc.endpoint_port(binding['recompute_endpoint'])!=port:raise ValueError('端口与所选报告不匹配，请用对应端口')
    tracker=recalc.RevisionTracker(run_id=report.name);lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,code,data,content_type='application/json; charset=utf-8'):
            body=data.encode('utf-8') if isinstance(data,str) else json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(code);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)));self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Private-Network','true');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
        def do_OPTIONS(self):
            self.send_response(204);self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Methods','POST, GET, OPTIONS');self.send_header('Access-Control-Allow-Headers','Content-Type');self.send_header('Access-Control-Allow-Private-Network','true');self.end_headers()
        def do_GET(self):
            if self.path in ('/','/dps-report.html'):self.send(200,(report/'dps-report.html').read_text(encoding='utf-8'),'text/html; charset=utf-8')
            else:self.send(404,{'error':'页面不存在'})
        def do_POST(self):
            if self.path!='/recompute':self.send(404,{'error':'接口不存在'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=150000:raise ValueError('请求大小无效')
                p=json.loads(self.rfile.read(size))
                if set(p)!={'binding','scenario','request_id','client_session_id','client_edit_revision'}:raise ValueError('请求字段不完整或不受支持')
                session=p.get('client_session_id');revision=p.get('client_edit_revision');request=p.get('request_id')
                if p.get('binding')!=binding:raise ValueError('报告绑定不匹配，请打开当前报告')
                if fingerprints(root,case)!=binding['input_fingerprints']:raise ValueError('输入或规则已经变化，请生成新报告')
                if not isinstance(session,str) or not session or not isinstance(request,str):raise ValueError('请求身份无效')
                scenario=_legacy_dps().validate_scenario(profile,p.get('scenario'))
                with lock:
                    issue=tracker.check(revision,request_id=request,session_id=session)
                    if issue:raise ValueError(issue['code'])
                    result=_legacy_dps().calculate(root,case,profile,scenario['states'],scenario['target'])
                    html=render(result,binding);accepted=tracker.accept(revision,request_id=request,session_id=session)
                self.send(200,{'binding':binding,'request_id':request,'client_session_id':session,'accepted_revision':accepted,'result':result,'html':html})
            except (ValueError,KeyError,TypeError,OverflowError) as exc:self.send(422,{'error':str(exc)})
            except Exception:self.send(500,{'error':'本次计算失败，请重试；已有结果保持不变'})
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'DPS 报告（{case.get("case_id")}）：http://127.0.0.1:{port}/',flush=True)
    server.serve_forever()
