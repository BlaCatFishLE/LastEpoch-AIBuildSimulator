'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone=v=>JSON.parse(JSON.stringify(v));
const canonical=(ref,fallback='')=>boot?.names?.[ref?.entity_id]||ref?.name||fallback;
let boot,doc,baseDoc,tab='equipment',slot='head',treeId=null,nodeId=null,itemDraft=null,itemDetail=null,affixChoices=[],itemTarget=null,editSequence=0,pendingSaves=0,saveChain=Promise.resolve(),saveTimer,viewSequence=0,treeCache=new Map(),versions=new Map();
const token=$('meta[name=le-editor-token]').content;
const calculationSession=crypto.randomUUID();
let calculationRequest=0,calculationGeneration=0,calculationTimer;
const calculationCache=new Map(),lastSuccessfulCalculation=new Map();
function markCalculationDirty(){
  calculationGeneration++;clearTimeout(calculationTimer);
  const info=calculationCache.get(doc?.id);
  if(info){info.stale=true;showCalculation(info);}else showCalculation({status:'idle',stale:true});
}
function showCalculation(info){
  if(!doc)return;
  if(info.status==='completed')lastSuccessfulCalculation.set(doc.id,info);
  const previous=lastSuccessfulCalculation.get(doc.id)||info.previous_success;
  const active=['queued','running'].includes(info.status);
  $('#calculate').disabled=active&&!info.stale;
  const format=v=>Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
  const result=info.status==='completed'?info:previous;
  let html=`<p class="calculation-status ${info.status==='failed'?'error':''}">${esc(info.stale?'配置已更改 · 当前结果待更新':info.message||'尚未计算')}</p>`;
  if(result){
    if(result.dps!==null&&result.dps!==undefined)html+=`<div class="dps-value">${esc(format(result.dps))}<small>预计 DPS · ${esc(result.horizon_s)} 秒</small></div>`;
    html+=`<p class="help">${esc(result.main_skill)} · ${result.route==='A'?'复用计划':'重新编译'}${result.coverage?.status==='partial'?' · 已支持范围':''}</p><a class="report-link" href="${esc(result.report_url)}" target="_blank" rel="noopener">查看完整计算详情 →</a>`;
    if(result.normalization_count)html+=`<p class="help">${esc(result.normalization_count)} 条导入词缀已按当前装备基底恢复取值，完整数值见详情中的装备页。</p>`;
    html+='<p class="help">报告保留计算时的完整配置、条件与各乘区。'+(info.stale||info.status!=='completed'?'此链接属于上次成功计算。':'')+'</p>';
  }
  if(info.research_url)html+=`<div class="research-handoff"><p class="help">${info.status==='completed'?'仍有部分来源待确认，可交接任务补齐。':'需要补充计算能力。'}任务已保存，复制给当前对话中的 agent 继续处理。</p><button id="copy-research">复制研究任务</button><a href="${esc(info.research_url)}" download="bd-research-task.json">下载任务</a><p class="help">等待交接，尚未启动 agent。</p></div>`;
  if(info.status==='idle')html+='<button id="choose-main-skill">选择主技能与条件</button>';
  $('#calculation-result').innerHTML=html;
  if($('#choose-main-skill'))$('#choose-main-skill').onclick=safe(async()=>{tab='conditions';await renderTab();});
  if($('#copy-research'))$('#copy-research').onclick=safe(async()=>{await navigator.clipboard.writeText(info.handoff);status('研究任务已复制，可粘贴到当前对话');});
  if(info.status==='completed'&&!info.stale&&baseDoc?.id&&baseDoc.id!==doc.id){
    const holder=document.createElement('div');holder.id='calculation-comparison';holder.className='section';$('#calculation-result').append(holder);compareCalculations(info,holder);
  }
}
async function compareCalculations(current,holder){
  const id=doc.id,baseline=baseDoc.id,sequence=editSequence;
  try{
    const base=await api('calculation?id='+baseline);
    if(doc?.id!==id||baseDoc?.id!==baseline||sequence!==editSequence||!holder.isConnected)return;
    if(base.status!=='completed'||base.stale){holder.innerHTML='<p class="help">基准配置尚无对应当前设置的结果，计算后可比较。</p>';return;}
    const coverage=x=>JSON.stringify({status:x.coverage?.status,codes:(x.coverage?.gaps||[]).map(g=>g.code).sort()});
    if(current.main_skill!==base.main_skill||current.horizon_s!==base.horizon_s||coverage(current)!==coverage(base)){
      holder.innerHTML=`<p class="help">主技能、窗口或计算覆盖范围不同，暂不排列收益。</p><a href="${esc(base.report_url)}" target="_blank" rel="noopener">查看基准计算详情 →</a>`;return;
    }
    const number=v=>Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
    const rows=[['预计 DPS',base.dps,current.dps],['窗口总伤害',base.totals?.horizon_damage,current.totals?.horizon_damage],['施放次数',base.totals?.cast_count,current.totals?.cast_count],['最大并发',base.totals?.max_concurrent,current.totals?.max_concurrent]].filter(r=>r[1]!=null&&r[2]!=null);
    holder.innerHTML=`<h3>与基准的计算比较</h3><p class="help">按各自条件比较；目标与 Buff 变化也会影响收益。</p>${rows.map(([label,b,a])=>`<div class="delta"><strong>${esc(label)}</strong><small>${esc(number(b))} → ${esc(number(a))}</small><small>变化 ${a-b>=0?'+':''}${esc(number(a-b))}${b?'（'+(a-b>=0?'+':'')+number((a-b)/b*100)+'%）':''}</small></div>`).join('')}<a href="${esc(base.report_url)}" target="_blank" rel="noopener">查看基准计算详情 →</a>`;
  }catch(error){if(holder.isConnected)holder.textContent='基准结果暂不可用';}
}
async function loadCalculation(expected=null){
  const generation=++calculationGeneration,id=doc.id,seq=editSequence;clearTimeout(calculationTimer);
  $('#calculate').disabled=false;$('#calculation-result').innerHTML='<p class="help">正在读取计算记录…</p>';
  async function poll(){
    try{
      const info=await api('calculation?id='+id+(expected?'&evaluation='+encodeURIComponent(expected.evaluation_id):''));
      if(expected&&(info.evaluation_id!==expected.evaluation_id||info.client_session_id!==calculationSession||info.accepted_revision!==expected.accepted_revision||info.request_id!==expected.request_id))throw new Error('计算响应与当前请求不匹配');
      if(doc?.id!==id||generation!==calculationGeneration||seq!==editSequence)return;
      calculationCache.set(id,info);showCalculation(info);
      if(['queued','running'].includes(info.status))calculationTimer=setTimeout(poll,900);
    }catch(error){if(generation===calculationGeneration)$('#calculation-result').textContent=error.message;}
  }
  await poll();
}
$('#calculate').onclick=safe(async()=>{
  if(!doc)return;await flush();const id=doc.id,seq=editSequence;
  const info=await api('calculate',{id,version:doc.version,client_session_id:calculationSession,client_edit_revision:++calculationRequest,request_id:calculationSession+':'+calculationRequest});
  if(doc?.id!==id||seq!==editSequence)return;
  calculationCache.set(id,info);showCalculation(info);await loadCalculation(info);
});
async function api(path,body){const r=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-LE-Editor':token},body:body===undefined?undefined:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d;}
function status(text,error=false){$('#status').textContent=text;$('#status').classList.toggle('error',error);}
function safe(fn){return (...args)=>Promise.resolve().then(()=>fn(...args)).catch(e=>status(e.message,true));}
function character(){const c=doc.build_spec.character;return {class_id:Number(c.class?.source_value||0),mastery:Number(c.mastery?.source_value||0),level:c.level};}
function editor(){return doc.build_spec.extensions.bd_editor??={passive_quest_points:null,skill_budgets:{}};}
function displayName(item){const e=item?.editor;return item?.definition?.ref?.name||item?.definition?.name||item?.base?.ref?.name||item?.base?.name||item?.readable?.display_name||'空槽位';}
function gameName(item){const eid=(item?.definition||item?.base)?.entity_id;return catalogNames.get(eid)||displayName(item);}
const catalogNames=new Map();
function refId(ref){return ref?.entity_id||ref?.ref?.entity_id;}
function instances(spec){const out={};for(const x of spec.equipment.slots||[])out['装备/'+x.slot]=x.instance;for(const x of spec.idols.entries||[])out['神像/'+x.index]=x.instance;for(const x of spec.blessings.entries||[])out['祝福/'+x.timeline]=x.instance;return out;}
function flatten(spec){const out={};out['角色/等级']=spec.character.level;out['角色/职业']=canonical(spec.character.class?.ref);out['角色/专精']=canonical(spec.character.mastery?.ref);for(const [key,it]of Object.entries(instances(spec))){const label=key.startsWith('装备/')?(boot.slots[key.slice(3)]||key):key;out[label+'/物品']=gameName(it);out[label+'/基底']=catalogNames.get(it.base?.entity_id)||it.base?.name||'';for(const [ch,list]of Object.entries(it.affixes||{}))list.forEach((a,i)=>{const nm=canonical(a.affix?.ref,'词缀');const vals=(a.value?.values||[]).map(v=>v.display_value??v.value_numeric??'缺值').join(' / ');out[label+'/'+({'normal':'普通','sealed':'封印','corrupted':'腐化','primordial':'特殊'}[ch]||ch)+'词缀 '+(i+1)]=nm+' T'+a.source_tier+' '+vals;});for(const [ch,block] of [['固有',it.implicit_rolls],['暗金',it.unique_rolls]])(block?.values||[]).forEach((r,i)=>out[label+'/'+ch+'取值 '+(i+1)]=(r.values||[]).map(v=>v.display_value??v.value_numeric??'缺值').join(' / '));if(it.position)out[label+'/位置']=JSON.stringify(it.position);}for(const tr of [spec.passive_tree,...spec.specializations]){const label=boot.trees.find(t=>t.entity_id===tr.tree?.entity_id)?.name||canonical(tr.tree,'节点');for(const n of tr.nodes||[])if(n.points)out[label+'/'+canonical(n.ref,'未命名节点')]=n.points+' 点';}return out;}
function differences(){if(!doc||!baseDoc)return [];const a=flatten(baseDoc.build_spec),b=flatten(doc.build_spec);return [...new Set([...Object.keys(a),...Object.keys(b)])].filter(k=>a[k]!==b[k]).map(k=>[k,a[k]??'无',b[k]??'无']).concat(conditionDifferences());}
function renderDiff(){const ds=differences();$('#diff-count').textContent=ds.length+' 项';$('#baseline-name').textContent=baseDoc?.name||'尚未选择基准';$('#diff-summary').innerHTML=ds.length?ds.slice(0,8).map(r=>`<div class="delta"><strong>${esc(r[0])}</strong><small>原：${esc(r[1])}</small><small>现：${esc(r[2])}</small></div>`).join('')+(ds.length>8?`<p class="help">另有 ${ds.length-8} 项，请打开配置差异。</p>`:''):'<p class="help">当前配置与基准一致</p>';if(tab==='diff')renderDiffPanel();}
function renderValidation(){const ps=doc.validation||[];$('#validation').innerHTML=ps.length?ps.map(p=>`<p class="message ${p.severity==='error'?'error':''}">${p.severity==='error'?'需修正：':p.severity==='warning'?'待核实：':''}${esc(p.message)}</p>`).join(''):'<p class="message">已知规则检查通过</p>';}
function save(commit=false){clearTimeout(saveTimer);saveTimer=null;pendingSaves++;const snapshot=clone(doc);const seq=editSequence;saveChain=saveChain.catch(()=>{}).then(async()=>{snapshot.version=versions.get(snapshot.id)??snapshot.version;const out=await api(commit?'save':'draft',snapshot);versions.set(out.id,out.version);if(doc?.id===out.id){doc.version=out.version;doc.revision=out.revision;doc.build_spec.meta=out.build_spec.meta;doc.build_spec.extensions.bd_editor_origin=out.build_spec.extensions.bd_editor_origin;if(seq===editSequence){doc.build_spec=out.build_spec;doc.presentation=out.presentation;doc.skill_budgets=out.skill_budgets;doc.conditions=out.conditions;doc.condition_catalog=out.condition_catalog;doc.validation=out.validation;if(document.querySelector('[data-budget-label]')&&treeCache.has(treeId))document.querySelector('[data-budget-label]').textContent=budgetLabel(treeCache.get(treeId));renderValidation();status(commit?'已保存版本 · 可在历史版本中恢复':'草稿已自动保存');}}boot.builds=boot.builds.map(b=>b.id===out.id?{...b,name:out.name,version:out.version}:b);renderBuildList();return out;}).finally(()=>{pendingSaves--;});saveChain.catch(e=>status('保存失败：'+e.message,true));return saveChain;}
function changed(message){editSequence++;markCalculationDirty();status(message+' · 正在保存草稿');renderDiff();saveTimer=setTimeout(()=>save(false),600);}
async function flush(){await flushItemEdits();if(saveTimer){clearTimeout(saveTimer);saveTimer=null;await save(false);}await saveChain;}
function modal(title,html){$('#dialog-title').textContent=title;$('#dialog-body').innerHTML=html;$('#dialog').showModal();}
function closeModal(){$('#dialog').close();}
function options(rows,value,label){return rows.map(r=>`<option value="${esc(value(r))}">${esc(label(r))}</option>`).join('');}
function renderBuildList(){$('#builds').innerHTML=boot.builds.map(b=>`<button class="build" data-id="${esc(b.id)}" aria-pressed="${doc?.id===b.id}"><strong>${esc(b.name)}</strong><small>${b.id===baseDoc?.id?'比较基准':'独立配置'} · ${b.revision?'有保存版本':'草稿'}</small></button>`).join('');$$('#builds button').forEach(b=>b.onclick=safe(()=>selectBuild(b.dataset.id)));const current=$('#baseline').value;$('#baseline').innerHTML=options(boot.builds,x=>x.id,x=>x.name);$('#baseline').value=baseDoc?.id||current||doc?.id||'';$('#baseline').disabled=!boot.builds.length;$('#trash').textContent='回收站'+(boot.deleted_count?'（'+boot.deleted_count+'）':'');}
async function refreshList(){const latest=await api('bootstrap');boot.builds=latest.builds;boot.deleted_count=latest.deleted_count;renderBuildList();}
async function selectBuild(id){await flush();doc=await api('config?id='+encodeURIComponent(id));versions.set(id,doc.version);editSequence=0;treeId=null;nodeId=null;itemTarget=null;await render();status('已载入 '+doc.name);}
async function render(){if(!doc){renderEmpty();return;}$('#name').disabled=false;['save','copy','history','delete-config','export'].forEach(id=>$('#'+id).disabled=false);$$('#tabs button').forEach(b=>b.disabled=false);$('#name').value=doc.name;const ch=character(),cls=boot.classes.find(c=>c.id===ch.class_id);$('#identity').textContent=`${cls?.name||'角色'} / ${cls?.masteries[ch.mastery]||'基础职业'} · 等级 ${ch.level}`;renderBuildList();renderValidation();renderDiff();loadCalculation();await renderTab();}
async function renderTab(){await flushItemEdits();itemSession=null;const seq=++viewSequence;$$('#tabs button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.tab===tab)));$('#content').innerHTML='<p class="empty">正在加载…</p>';if(tab==='equipment')await renderEquipment(seq);else if(tab==='passive'||tab==='skill')await renderTrees(seq);else if(tab==='idol'||tab==='blessing')await renderOther(seq);else if(tab==='conditions')await renderConditions();else if(tab==='diff')renderDiffPanel();else renderCharacter();}
function itemLines(item){return (doc.presentation?.items?.[item?.instance_id]?.lines||item?.readable?.lines||[]).map(l=>`<p class="item-line">${esc(l.tier_label?l.tier_label+' · ':'')}${esc(l.text||'效果尚未解析')}</p>`).join('');}
async function renderEquipment(seq){const slots=doc.build_spec.equipment.slots||[];$('#content').innerHTML=`<div class="split"><div class="slots">${Object.entries(boot.slots).map(([s,n])=>`<button data-slot="${s}" aria-pressed="${s===slot}">${esc(n)}<small>${esc(gameName(slots.find(e=>e.slot===s)?.instance))}</small></button>`).join('')}</div><div id="item-editor"></div></div>`;$$('[data-slot]').forEach(b=>b.onclick=safe(async()=>{slot=b.dataset.slot;await renderTab();}));const item=slots.find(e=>e.slot===slot)?.instance;itemTarget={kind:'equipment',slot};await renderItemEditor(item,seq);}
function existingDraft(item){if(item?.editor)return clone(item.editor);const definition=(item?.definition||item?.base)?.entity_id;let affixes=[];for(const [channel,as]of Object.entries(item?.affixes||{}))for(const a of as){const id=Number(a.affix?.source_id??a.codec?.affix_id);if(Number.isFinite(id))affixes.push({id,channel,tier:a.source_tier,roll:a.roll?.raw??128});}return {definition,sub_type:item?.definition?.allowed_subtypes?.[0],ir:clone(item?.implicit_rolls?.ir||[]),ur:clone(item?.unique_rolls?.ur||[]),affixes};}
async function renderItemEditor(item,seq){const target=$('#item-editor');if(!target)return;const targetSlot=itemTarget.kind==='equipment'?itemTarget.slot:itemTarget.kind;itemDraft=existingDraft(item);startItemSession(!!item);const idForCurrent=itemDraft.definition;const query=new URLSearchParams({slot:targetSlot,...character(),...(itemTarget.kind==='blessing'?{timeline:itemTarget.timeline}:{})});const list=await api('items?'+query);if(seq!==viewSequence)return;list.forEach(i=>catalogNames.set(i.id,i.name));const current=list.find(i=>i.id===idForCurrent);target.innerHTML=`<div class="panel"><div class="row between"><h2>${esc(itemTarget.kind==='equipment'?boot.slots[itemTarget.slot]:itemTarget.kind==='idol'?'神像配置':'祝福配置')}</h2><span class="badge">数据库候选</span></div><p class="help">${esc(gameName(item))}</p><div id="current-lines">${itemLines(item)}</div><div class="section fields"><label>搜索名称或类型<input id="item-search" placeholder="支持中文 / English"></label><label>类别<select id="item-kind"><option value="all">全部</option><option value="base">基底</option><option value="unique">暗金 / 套装</option></select></label></div><label class="help"><input type="checkbox" id="legacy"> 显示遗留 / 不可正常掉落基底</label><label>选择物品 <span id="candidate-count"></span><select id="definition"></select></label><div id="item-properties"></div><div id="item-warning" class="help" aria-live="polite"></div><div class="actions section"><button class="primary" id="apply-item">应用到配置</button><button id="remove-item" ${item?'':'disabled'}>卸下 / 移除</button></div></div>`;
  let candidates=list;
  function drawOptions(){const q=$('#item-search').value.toLowerCase(),kind=$('#item-kind').value;const filtered=candidates.filter(i=>(i.name+' '+i.english+' '+i.type).toLowerCase().includes(q)&&(kind==='all'||(kind==='unique')===i.unique));$('#definition').innerHTML='<option value="">请选择…</option>'+options(filtered,x=>x.id,x=>`${x.name} · ${x.type||''} · Lv.${x.level||0}`);$('#definition').value=filtered.some(x=>x.id===itemDraft.definition)?itemDraft.definition:'';$('#candidate-count').textContent=`（${filtered.length} 个）`;}
  drawOptions();$('#item-search').oninput=drawOptions;$('#item-kind').onchange=drawOptions;
  $('#legacy').onchange=safe(async()=>{candidates=await api('items?'+query+'&legacy='+$('#legacy').checked);drawOptions();});
  $('#definition').onchange=safe(async()=>{const eid=$('#definition').value;if(!eid)return;await flushItemEdits();itemDraft={definition:eid,ir:[],ur:[],affixes:[]};startItemSession(false);await loadItemProperties();status('候选物品已选择 · 应用后计入配置差异');});
  $('#apply-item').onclick=safe(applyItem);$('#remove-item').onclick=safe(()=>removeItem());
  if(idForCurrent){await loadItemProperties();if(!current){itemSession.allowLegacy=true;$('#item-warning').textContent='当前物品不在默认候选内，已保留；替换时会重新检查角色和槽位限制。';}}else $('#apply-item').disabled=true;
}
async function loadItemProperties(){const seq=viewSequence;const eid=itemDraft.definition;const detail=await api('item?id='+encodeURIComponent(eid));const choices=await api('affixes?base='+detail.base_type);if(seq!==viewSequence||eid!==itemDraft.definition)return;itemDetail=detail;affixChoices=choices;catalogNames.set(eid,detail.name);const target=$('#item-properties');if(!target)return;
  const allowedSub=detail.extra.subTypeIds||[];itemDraft.sub_type??=allowedSub[0];
  const implicit=detail.implicits||[],mods=detail.mods||[];while(itemDraft.ir.length<implicit.length)itemDraft.ir.push(128);const ids=mods.map(m=>m.rollId).filter(Number.isInteger);const maxRoll=ids.length?Math.max(...ids):-1;while(itemDraft.ur.length<=maxRoll)itemDraft.ur.push(128);
  target.innerHTML=`${allowedSub.length>1?`<label class="help">暗金基底子类型<select id="subtype">${options(allowedSub,x=>x,x=>'子类型 '+x)}</select></label>`:''}<div class="section"><h3>固有属性取值</h3><p class="help">新选择的浮动数值默认取值进度 50%，应用前可调整。</p><div id="implicit-values"></div></div>${mods.length||detail.descriptions.length?'<div class="section"><h3>暗金效果取值</h3><div id="unique-values"></div><div id="unique-descriptions"></div></div>':''}${Number(detail.base_type)!==34?'<div class="section"><div class="row between"><h3>词缀</h3><button class="compact" id="add-affix">＋ 添加词缀</button></div><div id="affixes"></div></div>':''}<p class="notice section">取值进度对应 0–255 的物品 roll；复合词缀共享同一进度。特殊组合与获取路径会单独标注待核实。</p>`;
  if($('#subtype')){$('#subtype').value=itemDraft.sub_type;$('#subtype').onchange=()=>{itemDraft.sub_type=Number($('#subtype').value);itemDraftChanged(true);};}
  const ranges=(entries,field,parent)=>{const container=$(parent);if(!container)return;container.innerHTML=entries.map((m,i)=>{const ri=field==='ur'?m.rollId:i;if(field==='ur'&&!Number.isInteger(ri))return `<p class="item-line">${esc(m.label)} · 固定效果</p>`;const min=m.implicitValue??m.value??0,max=m.implicitMaxValue??m.maxValue??min;return `<label class="help">${esc(m.label||'效果')} <span>（定义值 ${esc(min)}–${esc(max)}）</span><div class="row"><input type="range" min="0" max="255" step="1" value="${itemDraft[field][ri]}" data-roll-field="${field}" data-roll-index="${ri}" data-mod-index="${i}" aria-label="${esc(m.label||'效果')}取值" style="flex:1"><output data-actual-roll="${field}-${i}">…</output></div><small data-progress="${field}-${i}">取值进度 ${Math.round(itemDraft[field][ri]/255*100)}% · roll ${itemDraft[field][ri]}/255</small></label>`;}).join('');};
  ranges(implicit,'ir','#implicit-values');ranges(mods,'ur','#unique-values');if($('#unique-descriptions'))$('#unique-descriptions').innerHTML=detail.descriptions.filter(Boolean).map(s=>`<p class="item-line">${esc(s)}</p>`).join('');
  $$('[data-roll-field]').forEach(e=>e.oninput=()=>{itemDraft[e.dataset.rollField][Number(e.dataset.rollIndex)]=Number(e.value);itemDraftChanged();});
  if($('#add-affix'))$('#add-affix').onclick=()=>{if(itemDraft.affixes.length>=7){status('最多编辑 7 个词缀通道',true);return;}const first=affixChoices.find(a=>!a.special);if(!first){status('该基底没有已解析的合法词缀候选',true);return;}itemDraft.affixes.push({id:first.id,tier:1,roll:128,channel:'normal'});renderAffixes();itemDraftChanged(true);};
  renderAffixes();$('#apply-item').disabled=false;updateItemHelp();await processItemSession(itemSession);
}
function renderAffixes(){if(!$('#affixes'))return;$('#affixes').innerHTML=itemDraft.affixes.map((a,i)=>{const def=affixChoices.find(x=>x.id===a.id),tiers=[...new Set((def?.ranges||[]).map(r=>r.tier))];return `<div class="affix" data-affix-index="${i}"><label class="affix-name">词缀<select data-field="id">${options(affixChoices,x=>x.id,x=>`${x.slot==='prefix'?'前缀':'后缀'} · ${x.name}${x.special?' · 特殊':''}`)}</select></label><label>通道<select data-field="channel"><option value="normal">普通</option><option value="sealed">封印</option><option value="corrupted">腐化</option><option value="primordial">特殊</option></select></label><label>阶级<select data-field="tier">${options(tiers,x=>x,x=>'T'+x)}</select></label><button class="compact" data-remove>移除</button><div class="range-row"><label>取值进度</label><input aria-label="词缀取值进度" type="range" min="0" max="255" step="1" value="${a.roll}" data-field="roll"><output data-actual-affix="${i}">…</output></div><small class="affix-name" data-affix-progress="${i}">取值进度 ${Math.round(a.roll/255*100)}% · roll ${a.roll}/255</small><div class="affix-name help">${esc(affixValue(a,def))}</div></div>`;}).join('');$$('[data-affix-index]').forEach(row=>{const i=Number(row.dataset.affixIndex),a=itemDraft.affixes[i];for(const field of ['id','tier','channel']){const e=row.querySelector('[data-field='+field+']');e.value=a[field];e.onchange=()=>{a[field]=field==='channel'?e.value:Number(e.value);if(field==='id')a.tier=affixChoices.find(x=>x.id===a.id)?.ranges[0]?.tier||1;renderAffixes();itemDraftChanged(true);};}row.querySelector('[data-field=roll]').oninput=e=>{a.roll=Number(e.target.value);itemDraftChanged();};row.querySelector('[data-remove]').onclick=()=>{itemDraft.affixes.splice(i,1);renderAffixes();itemDraftChanged(true);};});paintItemValues(itemSession);}
function affixValue(a,def){if(!def)return '原词缀适用性未解析；应用前请重新选择';return def.ranges.filter(r=>r.tier===a.tier).map(r=>`${r.property_name}：${r.display_min}–${r.display_max}${r.display_unit||''}`).join('；')+' · 具体取值见拖条右侧';}
let itemSession=null;
function targetItem(target){return target.kind==='equipment'?doc.build_spec.equipment.slots.find(x=>x.slot===target.slot)?.instance:target.kind==='idol'?doc.build_spec.idols.entries.find(x=>x.index===target.index)?.instance:doc.build_spec.blessings.entries.find(x=>String(x.timeline)===String(target.timeline))?.instance;}
function startItemSession(auto){
  itemSession={docId:doc.id,target:clone(itemTarget),draft:itemDraft,auto,revision:0,dirty:false,timer:null,promise:null,tables:null,allowLegacy:false};
}
function itemRequest(session,draft){
  const t=session.target,old=targetItem(t);
  const position=t.kind==='idol'?{x:Number($('#idol-x')?.value||0),y:Number($('#idol-y')?.value||0)}:t.kind==='blessing'?{timeline:t.timeline}:null;
  return {...draft,slot:t.kind==='equipment'?t.slot:t.kind,...character(),position,legacy:session.allowLegacy||$('#legacy')?.checked,
    instance_id:old?.instance_id||(t.kind==='equipment'?'equipment.'+t.slot:t.kind==='idol'?'idols.'+t.index:'blessings.'+t.timeline)};
}
function updateItemHelp(){
  const s=itemSession;if(!s)return;
  $('#apply-item').textContent=s.auto?'立即保存修改':'应用候选物品';
  if(!$('#item-edit-status'))$('#item-properties').insertAdjacentHTML('afterbegin','<p id="item-edit-status" class="message" role="status"></p>');
  $('#item-edit-status').textContent=s.auto?'修改阶级、roll 或词缀后自动保存；切换页面前会完成应用。':'候选物品可预览，点击“应用候选物品”后装备。';
}
function paintItemValues(session){
  if(!session||session!==itemSession)return;
  const tables=session.tables;
  $$('[data-roll-field]').forEach(e=>{
    const field=e.dataset.rollField,index=e.dataset.modIndex,raw=Number(e.value);
    e.nextElementSibling.textContent=tables?.[field]?.[index]?.[raw]??'…';
    const progress=$('[data-progress="'+field+'-'+index+'"]');if(progress)progress.textContent='取值进度 '+Math.round(raw/255*100)+'% · roll '+raw+'/255';
  });
  $$('[data-affix-index]').forEach(row=>{
    const i=Number(row.dataset.affixIndex),raw=session.draft.affixes[i]?.roll??0;
    row.querySelector('[data-actual-affix]').textContent=tables?.affixes?.[i]?.[raw]??'…';
    row.querySelector('[data-affix-progress]').textContent='取值进度 '+Math.round(raw/255*100)+'% · roll '+raw+'/255';
  });
}
function itemDraftChanged(rangesChanged=false){
  const s=itemSession;if(!s)return;s.revision++;s.dirty=true;if(rangesChanged)s.tables=null;
  if(s.auto){editSequence++;markCalculationDirty();status('装备修改待保存…');}
  paintItemValues(s);if($('#item-edit-status'))$('#item-edit-status').textContent=s.auto?'正在保存装备修改…':'候选取值已修改，尚未装备。';
  if(!s.timer)s.timer=setTimeout(()=>{s.timer=null;processItemSession(s).catch(e=>itemEditError(s,e));},160);
}
function itemEditError(session,error){
  if(session!==itemSession)return;
  if($('#item-edit-status')){$('#item-edit-status').textContent='未保存：'+error.message;$('#item-edit-status').classList.add('error');}
  status('装备修改未保存：'+error.message,true);
}
function putItem(session,item,position){
  const t=session.target;
  const list=t.kind==='equipment'?doc.build_spec.equipment.slots:t.kind==='idol'?doc.build_spec.idols.entries:doc.build_spec.blessings.entries;
  const ix=list.findIndex(x=>t.kind==='equipment'?x.slot===t.slot:t.kind==='idol'?x.index===t.index:String(x.timeline)===String(t.timeline));
  const entry=t.kind==='equipment'?{slot:t.slot,instance:item}:t.kind==='idol'?{index:t.index,position,instance:item}:{timeline:String(t.timeline),instance:item};
  if(ix<0)list.push(entry);else list[ix]=entry;
  if(t.kind==='equipment')doc.build_spec.equipment.absent_slots=Object.keys(boot.slots).filter(s=>!list.some(x=>x.slot===s));
  if(doc.presentation?.items)delete doc.presentation.items[item.instance_id];
}
async function processItemSession(session){
  if(!session||session.docId!==doc?.id)return;
  if(session.promise)return session.promise;
  if(!session.dirty&&session.tables)return;
  const revision=session.revision,draft=clone(session.draft),request=itemRequest(session,draft),needsTables=!session.tables;
  session.promise=(async()=>{
    const response=await api(needsTables?'item-preview':'make-item',request);
    if(session.docId!==doc?.id||session!==itemSession)return;
    if(revision!==session.revision)return;
    const item=needsTables?response.item:response;
    if(needsTables)session.tables=response.roll_values;
    paintItemValues(session);
    if(session.auto&&session.dirty){
      putItem(session,item,request.position);session.dirty=false;changed('装备修改已应用');
      if($('#current-lines'))$('#current-lines').innerHTML=itemLines(item);
      if($('#item-edit-status')){$('#item-edit-status').textContent='装备修改已应用，正在保存草稿…';$('#item-edit-status').classList.remove('error');}
      await save(false);
      if(session===itemSession&&revision===session.revision&&$('#item-edit-status'))$('#item-edit-status').textContent='装备修改已保存';
    }
  })();
  try{return await session.promise;}catch(error){session.dirty=true;itemEditError(session,error);throw error;}
  finally{session.promise=null;if(session===itemSession&&session.revision>revision&&session.dirty&&!session.timer)session.timer=setTimeout(()=>{session.timer=null;processItemSession(session).catch(e=>itemEditError(session,e));},100);}
}
async function flushItemEdits(){
  const s=itemSession;if(!s||!s.auto)return;
  clearTimeout(s.timer);s.timer=null;
  if(s.promise)await s.promise;
  while(s.dirty){clearTimeout(s.timer);s.timer=null;await processItemSession(s);}
}
async function applyItem(){
  const s=itemSession;if(!s)return;s.auto=true;s.dirty=true;s.revision++;
  await flushItemEdits();await renderTab();
}
async function removeItem(){
  await flushItemEdits();const t=clone(itemTarget);itemSession=null;
  if(t.kind==='equipment')doc.build_spec.equipment.slots=doc.build_spec.equipment.slots.filter(x=>x.slot!==t.slot);
  else if(t.kind==='idol')doc.build_spec.idols.entries=doc.build_spec.idols.entries.filter(x=>x.index!==t.index);
  else doc.build_spec.blessings.entries=doc.build_spec.blessings.entries.filter(x=>String(x.timeline)!==String(t.timeline));
  changed('已移除物品');return renderTab();
}
async function getTree(id){if(!treeCache.has(id))treeCache.set(id,await api('tree?id='+encodeURIComponent(id)));return treeCache.get(id);}
function allocation(t){return t.tree_kind==='passive'?doc.build_spec.passive_tree:doc.build_spec.specializations.find(s=>refId(s.tree)===t.entity_id);}
function pointsFor(t){return new Map((allocation(t)?.nodes||[]).map(n=>[refId(n.ref),n.points]));}
function budgetFor(t){if(t.tree_kind==='passive')return character().level-1+(editor().passive_quest_points??15);return doc.skill_budgets?.[t.entity_id]?.maximum??Infinity;}
function budgetLabel(t){if(t.tree_kind==='passive')return String(budgetFor(t));const b=doc.skill_budgets?.[t.entity_id];if(!b||b.minimum===null)return '待确认';if(b.maximum===null)return '至少 '+b.minimum+'（待确认）';return b.minimum===b.maximum?String(b.maximum):b.minimum+'–'+b.maximum;}

async function renderTrees(seq){const ch=character();const trees=boot.trees.filter(t=>t.class_entity_id===boot.classes.find(c=>c.id===ch.class_id)?.entity_id&&t.tree_kind===(tab==='passive'?'passive':'skill'));if(!trees.some(t=>t.entity_id===treeId)){const selected=doc.build_spec.specializations[0]?.tree?.entity_id;treeId=tab==='skill'&&trees.some(t=>t.entity_id===selected)?selected:trees[0]?.entity_id;nodeId=null;}if(!treeId){$('#content').innerHTML='<p class="empty">当前职业没有可用天赋树</p>';return;}
  const t=await getTree(treeId);if(seq!==viewSequence)return;const alloc=allocation(t);const points=pointsFor(t);const specialized=tab==='passive'||!!alloc;
  $('#content').innerHTML=`<div class="fields"><label>${tab==='passive'?'职业被动树':'浏览完整技能树'}<select id="tree-select">${options(trees,x=>x.entity_id,x=>`${x.name}`)}</select></label><label>搜索节点<input id="node-search" placeholder="名称 / 效果 / 前置关系"></label></div><div class="row between section"><h2>${esc(t.name)} · 全部 ${t.nodes.length} 个节点</h2><span class="badge">已用 ${[...points.values()].reduce((a,b)=>a+b,0)} / <span data-budget-label>${budgetLabel(t)}</span> 点</span></div><div class="row help">${specialized?'<label>只看已投入 <input type="checkbox" id="allocated-only"></label>':'<button id="specialize" class="primary">加入专精配置</button>'}${tab==='skill'&&alloc?'<button class="compact" id="unspecialize">移除这项专精</button>':''}<span>效果说明来自数据库；当前仅编辑点数。</span></div><div id="node-detail" class="panel node-detail"></div><div id="node-list" class="node-list"></div>`;
  $('#tree-select').value=treeId;$('#tree-select').onchange=safe(async()=>{treeId=$('#tree-select').value;nodeId=null;await renderTab();});
  const search=$('#node-search');function list(){const q=search.value.toLowerCase(),only=$('#allocated-only')?.checked;const ns=t.nodes.filter(n=>(n.name+' '+n.description).toLowerCase().includes(q)&&(!only||points.get(n.entity_id)));$('#node-list').innerHTML=ns.map(n=>`<button class="node" data-node="${esc(n.entity_id)}" aria-pressed="${n.entity_id===nodeId}"><span>${esc(n.name)}<small>${n.extra.mastery?esc(boot.classes.find(c=>c.id===ch.class_id)?.masteries[n.extra.mastery]||'分支'):'基础分支'}${n.extra.masteryRequirement?' · 门槛 '+n.extra.masteryRequirement+' 点':''}</small></span><span class="points">${points.get(n.entity_id)||0} / ${n.max_points||0}</span></button>`).join('');$$('[data-node]').forEach(b=>b.onclick=()=>{nodeId=b.dataset.node;detail();list();});}
  function detail(){const n=t.nodes.find(n=>n.entity_id===nodeId)||t.nodes.find(n=>n.max_points>0)||t.nodes[0];if(!n)return;nodeId=n.entity_id;const pre=t.edges.filter(e=>e.to_node_entity_id===n.entity_id),next=t.edges.filter(e=>e.from_node_entity_id===n.entity_id);const link=(e,forward)=>{const id=forward?e.to_node_entity_id:e.from_node_entity_id,v=t.nodes.find(n=>n.entity_id===id);return `<button class="compact" data-jump="${esc(id)}">${esc(v?.name||'未解析节点')}${forward?'':' ≥ '+(e.points_required||0)+' 点'}</button>`;};$('#node-detail').innerHTML=`<div class="row between"><h2>${esc(n.name)}</h2><span class="badge">配置可编辑 · 未执行计算</span></div><p class="node-text help">${esc(n.description||'该节点未提供独立描述')}</p>${n.effects.map(s=>`<p class="effect">${esc(s)}</p>`).join('')}<div class="counter section"><button id="minus" ${!specialized?'disabled':''} aria-label="减少节点点数">−</button><output>${points.get(n.entity_id)||0} / ${n.max_points||0}</output><button id="plus" ${!specialized?'disabled':''} aria-label="增加节点点数">＋</button></div><div class="relation section">前置${pre.length>1?'（任一路径满足）':''}：${pre.length?pre.map(e=>link(e,false)).join(''):'无'}</div><div class="relation help">后续：${next.length?next.map(e=>link(e,true)).join(''):'无'}</div><p id="node-message" class="message" aria-live="polite"></p>`;$$('[data-jump]').forEach(b=>b.onclick=()=>{nodeId=b.dataset.jump;detail();list();});$('#minus').onclick=safe(()=>adjust(-1));$('#plus').onclick=safe(()=>adjust(1));}
  async function adjust(delta){const n=t.nodes.find(n=>n.entity_id===nodeId),p=(points.get(nodeId)||0)+delta;let err='';if(p<0||p>n.max_points)err='已达到节点点数边界';else if(delta>0&&[...points.values()].reduce((a,b)=>a+b,0)>=budgetFor(t))err='技能 / 被动点数预算不足，请先核对角色设置';else if(delta>0){const pre=t.edges.filter(e=>e.to_node_entity_id===nodeId);if(pre.length&&!pre.some(e=>(points.get(e.from_node_entity_id)||0)>=(e.points_required||0)))err='请先满足任一前置路径';}else{const affected=t.edges.filter(e=>e.from_node_entity_id===nodeId&&(points.get(e.to_node_entity_id)||0)>0).filter(e=>!t.edges.filter(x=>x.to_node_entity_id===e.to_node_entity_id).some(x=>(x.from_node_entity_id===nodeId?p:points.get(x.from_node_entity_id)||0)>=(x.points_required||0)));if(affected.length)err='后续节点仍依赖此点数，请先撤回后续节点';}if(err){$('#node-message').textContent=err;$('#node-message').classList.add('error');return;}
    const entries=alloc.nodes;let entry=entries.find(e=>refId(e.ref)===nodeId);if(!entry){entry={node_id:String(n.node_index),source_path:'editor.nodes.'+n.node_index,points:0,status:'resolved',ref:{entity_id:n.entity_id,kind:'node',name:n.name},node_type:n.node_type,max_points:n.max_points,node_index:n.node_index};entries.push(entry);}entry.points=p;entry.explicit_zero=p===0;entry.points_within_max=true;alloc.points_total=entries.reduce((a,e)=>a+e.points,0);alloc.node_entries=entries.length;changed('节点点数已调整');await renderTab();}
  search.oninput=list;if($('#allocated-only'))$('#allocated-only').onchange=list;detail();list();
  if($('#specialize'))$('#specialize').onclick=safe(async()=>{if(doc.build_spec.specializations.length>=5)throw new Error('最多配置 5 项专精技能');if(t.class_entity_id!==boot.classes.find(c=>c.id===ch.class_id)?.entity_id)throw new Error('该技能属于其他职业，仅供浏览');doc.build_spec.specializations.push({slot_number:[0,1,2,3,4].find(i=>!doc.build_spec.specializations.some(s=>s.slot_number===i)),tree:{entity_id:t.entity_id,name:t.name,kind:'tree'},tree_entity_id:t.entity_id,tree_source_id:t.source_tree_id,owner_skill:{entity_id:t.owner_entity_id,name:t.name,kind:'skill'},source_level:20,nodes:[],points_total:0,node_entries:0,status:'resolved'});changed('已加入专精技能');await renderTab();});
  if($('#unspecialize'))$('#unspecialize').onclick=safe(async()=>{if(!confirm('移除该专精及其已分配点数？'))return;doc.build_spec.specializations=doc.build_spec.specializations.filter(s=>refId(s.tree)!==treeId);changed('已移除专精');await renderTab();});
}
async function renderOther(seq){if(tab==='idol'){const es=doc.build_spec.idols.entries||[];if(itemTarget?.kind!=='idol')itemTarget={kind:'idol',index:es[0]?.index??0};const entry=es.find(x=>x.index===itemTarget.index);$('#content').innerHTML=`<div class="split"><div><div class="slots">${es.map(e=>`<button data-idol="${e.index}" aria-pressed="${e.index===itemTarget.index}">神像 ${e.index+1}<small>${esc(gameName(e.instance))}</small></button>`).join('')}</div><button id="new-idol" class="compact" style="margin-top:10px">＋ 添加神像</button></div><div><div class="panel" style="margin-bottom:12px"><div class="row between"><h2>神像坐标</h2><span class="muted">实际解锁区域待核实</span></div><div class="fields"><label>X 坐标<input id="idol-x" type="number" min="0" max="4" value="${entry?.position?.x??0}"></label><label>Y 坐标<input id="idol-y" type="number" min="0" max="7" value="${entry?.position?.y??0}"></label></div><div id="idol-grid" class="grid section"></div></div><div id="item-editor"></div></div></div>`;$$('[data-idol]').forEach(b=>b.onclick=safe(async()=>{itemTarget={kind:'idol',index:Number(b.dataset.idol)};await renderTab();}));$('#new-idol').onclick=safe(async()=>{itemTarget={kind:'idol',index:Math.max(-1,...es.map(x=>x.index))+1};await renderTab();});const cells=Array.from({length:40},(_,i)=>`<div class="grid-cell">${i%5},${Math.floor(i/5)}</div>`);$('#idol-grid').innerHTML=cells.join('');for(const e of es){const x=e.position?.x,y=e.position?.y;if(x>=0&&x<5&&y>=0&&y<8){const cell=$$('#idol-grid .grid-cell')[y*5+x];cell.classList.add('filled');cell.textContent='神像 '+(e.index+1);}}await renderItemEditor(entry?.instance,seq);
  }else{const timelines=boot.timelines||[];if(itemTarget?.kind!=='blessing')itemTarget={kind:'blessing',timeline:String(timelines[0]?.id||1)};const entry=doc.build_spec.blessings.entries.find(x=>String(x.timeline)===String(itemTarget.timeline));$('#content').innerHTML=`<div class="fields"><label>时间线<select id="timeline">${options(timelines,x=>x.id,x=>x.name)}</select></label><label>提示<p class="help">每条时间线保留一个选择；正常 / 强化祝福均从数据库筛选。</p></label></div><div id="item-editor" style="margin-top:14px"></div>`;$('#timeline').value=itemTarget.timeline;$('#timeline').onchange=safe(async()=>{itemTarget={kind:'blessing',timeline:$('#timeline').value};await renderTab();});await renderItemEditor(entry?.instance,seq);}}
function renderDiffPanel(){const ds=differences();$('#content').innerHTML=`<div class="panel"><div class="row between"><h2>与 ${esc(baseDoc?.name||'基准')} 比较</h2><span class="badge">${ds.length} 项配置变化</span></div><div class="table-wrap section"><table><thead><tr><th>修改项</th><th>基准配置</th><th>当前配置</th></tr></thead><tbody>${ds.length?ds.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join(''):'<tr><td colspan="3">无配置差异</td></tr>'}</tbody></table></div></div>`;}
function renderCharacter(){const ch=character(),cls=boot.classes.find(c=>c.id===ch.class_id);$('#content').innerHTML=`<div class="panel stack"><h2>角色与点数预算</h2><div class="fields"><label>职业<input value="${esc(cls?.name)}" disabled></label><label>等级<input id="level" type="number" min="1" max="100" value="${ch.level}"></label><label>专精<select id="mastery">${options(cls?.masteries||[],x=>cls.masteries.indexOf(x),x=>x)}</select></label><label>已获得的任务被动点数<input id="quest-points" type="number" min="0" max="30" placeholder="未核实" value="${editor().passive_quest_points??''}"></label></div><p class="notice">技能点数预算由基础等级与可解析的装备加成自动计算。未解析的转换规则会显示预算范围；手动覆盖仅用于已经核实的特殊情况，留空则继续自动计算。</p><div id="budgets" class="stack">${doc.build_spec.specializations.map(s=>`<label>${esc(boot.trees.find(t=>t.entity_id===refId(s.tree))?.name||canonical(s.tree))} · 自动预算 ${esc(budgetLabel({entity_id:refId(s.tree),tree_kind:'skill'}))}；手动覆盖（可留空）<input data-budget="${esc(refId(s.tree))}" type="number" min="1" max="100" value="${editor().skill_budgets?.[refId(s.tree)]??''}"></label>`).join('')}</div><button id="apply-character" class="primary">应用角色设置</button></div>`;$('#mastery').value=ch.mastery;$('#apply-character').onclick=safe(()=>{const level=Number($('#level').value),mastery=Number($('#mastery').value),q=$('#quest-points').value;if(!Number.isInteger(level)||level<1||level>100)throw new Error('等级需在 1–100 之间');const qp=q===''?null:Number(q);if(qp!==null&&(!Number.isInteger(qp)||qp<0||qp>30))throw new Error('任务点数需在 0–30 之间');doc.build_spec.character.level=level;doc.build_spec.character.mastery={...doc.build_spec.character.mastery,source_value:mastery,ref:{entity_id:`mastery:letools:class_${ch.class_id}:${ch.class_id}.${mastery}`,kind:'mastery',name:cls.masteries[mastery]}};editor().passive_quest_points=qp;editor().skill_budgets??={};$$('[data-budget]').forEach(e=>{if(e.value===''){delete editor().skill_budgets[e.dataset.budget];return;}const v=Number(e.value);if(!Number.isInteger(v)||v<1||v>100)throw new Error('技能点数预算需在 1–100 之间');editor().skill_budgets[e.dataset.budget]=v;});changed('角色设置已应用');return render();});}
function download(name,obj){const url=URL.createObjectURL(new Blob([JSON.stringify(obj,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name.replace(/[<>:"/\\|?*]/g,'_')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
let conditionRequestRevision=0;
function conditionEntries(record){
  const c=record.conditions||{},target=c.target||{},out={};
  const add=(key,label,value)=>out[key]=[label,String(value)];
  add('main','条件 / 主技能',boot.names?.['skill:letools:LEAbilities.abilityList:'+c.main_skill]||c.main_skill||'未指定');
  add('kind','条件 / 目标类型',({normal:'普通敌人',rare:'稀有敌人',boss:'Boss'})[target.kind||'normal']);
  add('level','条件 / 目标等级',target.monster_level??100);
  for(const type of ['Physical','Fire','Cold','Lightning','Void','Necrotic','Poison'])add('resistance:'+type,'条件 / '+type+' 抗性',(target.resistances?.[type]??0)+'%');
  add('horizon','条件 / 观察时间',(c.horizon_s??30)+' 秒');
  add('strategy','条件 / 施放策略',({default:'由计算模型决定',eager:'尽快施放',patient:'等待已有技能结束'})[c.strategy||'default']);
  for(const [key,value] of Object.entries(c.states||{})){
    if(record.condition_catalog?.derived?.[key])continue;
    if(value===false||value===0)continue;
    const control=record.condition_catalog?.controls?.find(x=>x.key===key);
    add('state:'+key,'条件 / '+(control?.label||c.labels?.[key]||'已保存条件'),typeof value==='boolean'?(value?'已启用':'未启用'):value+' 层 / 数值');
  }
  return out;
}
function conditionDifferences(){
  const a=conditionEntries(baseDoc),b=conditionEntries(doc);
  return [...new Set([...Object.keys(a),...Object.keys(b)])].filter(k=>a[k]?.[1]!==b[k]?.[1]).map(k=>[(b[k]||a[k])[0],a[k]?.[1]??'未启用 / 0',b[k]?.[1]??'未启用 / 0']);
}
function renderEmpty(){
  doc=null;baseDoc=null;clearTimeout(saveTimer);saveTimer=null;
  calculationGeneration++;clearTimeout(calculationTimer);$('#calculate').disabled=true;$('#calculation-result').innerHTML='<p class="help">导入或新建配置后开始计算。</p>';
  $('#name').value='';$('#name').disabled=true;$('#identity').textContent='';
  ['save','copy','history','delete-config','export'].forEach(id=>$('#'+id).disabled=true);
  $$('#tabs button').forEach(b=>b.disabled=true);
  $('#content').innerHTML='<div class="empty"><h2>暂无配置</h2><p>新建、导入一个 BD，或从回收站恢复。</p></div>';
  $('#validation').replaceChildren();renderBuildList();renderDiff();
}
async function renderConditions(draft,catalog){
  draft=clone(draft||doc.conditions);catalog=catalog||doc.condition_catalog;
  conditionRequestRevision++;const currentId=doc.id,currentView=viewSequence;
  const specs=doc.build_spec.specializations.filter(s=>boot.trees.some(t=>t.entity_id===refId(s.tree)&&t.class_entity_id===boot.classes.find(c=>c.id===character().class_id)?.entity_id));
  const controls=catalog.controls||[],known=new Set(controls.map(c=>c.key));
  const orphan=Object.entries(draft.states).filter(([key,value])=>!known.has(key)&&value!==false&&value!==0);
  const controlHTML=c=>{
    const value=draft.states[c.key],auto=!!c.readonly,label=esc(c.label),key=esc(c.key);
    const field=c.type==='boolean'?`<label class="row"><input type="checkbox" data-condition-state="${key}" ${value?'checked':''} ${auto?'disabled':''}>${auto?'由目标条件自动判定':'启用'}</label>`:`<label>层数 / 数值<input type="number" data-condition-state="${key}" min="${c.min??0}" ${c.max===null||c.max===undefined?'':`max="${c.max}"`} step="${c.type==='integer'?1:'any'}" value="${value??0}" ${auto?'disabled':''}></label>`;
    return `<article class="panel condition-card" data-condition-card><div class="row between"><h3>${label}</h3><span class="badge">${c.calculation_status==='not_mapped'?'待接入计算':c.single_cast_only?'仅本次施法':auto?'按目标判定':'固定条件设置'}</span></div><p class="help">${esc(c.source)}</p>${field}${c.effect?`<p class="help">${esc(c.effect)}</p>`:''}</article>`;
  };
  $('#content').innerHTML=`<div class="stack"><div class="row between"><h2>目标与条件 Buff</h2><span class="badge">每个配置独立保存</span></div><p class="help">选择主技能后补充对应场景条件；Buff 来源覆盖当前被动、全部专精与已映射装备。应用后点击右侧“计算当前配置”。未映射条件会进入研究任务。</p><label>计算主技能<select id="condition-main"><option value="">请选择主技能…</option>${options(specs,s=>s.tree_source_id||refId(s.tree).split(':').pop(),s=>boot.trees.find(t=>t.entity_id===refId(s.tree))?.name||canonical(s.tree))}</select></label>${!catalog.main_available?'<p class="message error">原主技能已不在当前专精中，请重新选择。</p>':''}<div class="panel stack"><h3>目标设置</h3><p class="help">第一版计算入口使用统一抗性；计算时各系抗性需相同。</p><div class="fields"><label>敌人类型<select id="condition-kind"><option value="normal">普通敌人</option><option value="rare">稀有敌人</option><option value="boss">Boss</option></select></label><label>怪物等级<input type="number" id="condition-level" min="1" max="100" value="${draft.target.monster_level}"></label></div><div class="fields">${Object.entries(draft.target.resistances).map(([k,v])=>`<label>${esc(k)} Resistance（%）<input type="number" data-resistance="${esc(k)}" min="-100" max="75" step="any" value="${v}"></label>`).join('')}</div></div>${controls.some(c=>c.group==='target')?'<div class="condition-grid" id="target-condition-cards">'+controls.filter(c=>c.group==='target').map(controlHTML).join('')+'</div>':''}<div class="panel fields"><label>观察时间（秒）<input id="condition-horizon" type="number" min="0.1" max="600" step="0.1" value="${draft.horizon_s}"></label><label>施放策略<select id="condition-strategy"><option value="default">由计算模型决定</option><option value="eager">尽快施放</option><option value="patient">等待已有技能结束</option></select></label><p class="help">所选技能的计算入口会检查策略是否适用。</p></div><div class="row between section"><h3>条件 Buff · ${controls.filter(c=>c.group!=='target').length} 项</h3><label class="sr-only" for="condition-search">搜索条件与来源</label><input id="condition-search" placeholder="搜索 Buff 或来源" style="max-width:230px"></div><div class="condition-grid" id="condition-cards">${controls.some(c=>c.group!=='target')?controls.filter(c=>c.group!=='target').map(controlHTML).join(''):'<p class="help">当前构筑暂无已映射的可配置条件。启用未支持条件时将生成研究任务。</p>'}</div>${orphan.length?`<div class="notice">保留了 ${orphan.length} 个已保存但当前来源不可用的条件；不会将其视为可用 Buff。${orphan.map(([key])=>'<div>'+esc(draft.labels[key]||'未识别条件')+'</div>').join('')}</div>`:''}<div class="row section"><button class="primary" id="apply-conditions">应用条件到配置</button><button id="reset-condition-buffs">关闭可操作 Buff</button><span class="muted">应用后自动保存，可复制为另一种 Buff 方案。</span></div><p id="condition-message" class="message" aria-live="polite"></p></div>`;
  $('#condition-main').value=draft.main_skill;$('#condition-kind').value=draft.target.kind;$('#condition-strategy').value=draft.strategy;
  function read(){
    const out=clone(draft);out.main_skill=$('#condition-main').value;out.target.kind=$('#condition-kind').value;
    const number=(el,label)=>{if(el.value.trim()==='')throw new Error(label+'不能为空');const value=Number(el.value);if(!Number.isFinite(value))throw new Error(label+'格式不正确');return value;};
    out.target.monster_level=number($('#condition-level'),'目标等级');out.horizon_s=number($('#condition-horizon'),'观察时间');out.strategy=$('#condition-strategy').value;
    $$('[data-resistance]').forEach(e=>out.target.resistances[e.dataset.resistance]=number(e,e.dataset.resistance+' 抗性'));
    $$('[data-condition-state]').forEach(e=>{const c=controls.find(c=>c.key===e.dataset.conditionState);if(!c.readonly)out.states[c.key]=c.type==='boolean'?e.checked:number(e,c.label);});
    return out;
  }
  function updateDerived(){
    const values={};$$('[data-condition-state]').forEach(e=>values[e.dataset.conditionState]=e.type==='checkbox'?e.checked:Number(e.value));
    for(const [key,rule]of Object.entries(catalog.derived||{})){
      const e=$$('[data-condition-state]').find(e=>e.dataset.conditionState===key);if(e)e.checked=rule.target_kind?['rare','boss'].includes($('#condition-kind').value):(rule.any||[]).some(k=>values[k]);
    }
  }
  $$('#content input,#content select').forEach(e=>e.addEventListener('change',()=>{conditionRequestRevision++;updateDerived();$('#condition-message').textContent='条件已修改，点击“应用条件到配置”后计入差异。';}));
  $('#condition-main').onchange=safe(async()=>{const state=read(),requestRevision=++conditionRequestRevision;const result=await api('conditions',{build_spec:doc.build_spec,conditions:state});if(doc?.id!==currentId||viewSequence!==currentView||requestRevision!==conditionRequestRevision)return;await renderConditions(result.conditions,result.catalog);});
  $('#condition-search').oninput=()=>{$$('#condition-cards [data-condition-card]').forEach(c=>c.hidden=!c.textContent.toLowerCase().includes($('#condition-search').value.toLowerCase()));};
  $('#apply-conditions').onclick=safe(async()=>{
    const requestRevision=++conditionRequestRevision;const result=await api('conditions',{build_spec:doc.build_spec,conditions:read()});if(doc?.id!==currentId||viewSequence!==currentView||requestRevision!==conditionRequestRevision)return;
    doc.conditions=result.conditions;doc.condition_catalog=result.catalog;changed('条件已应用');await renderConditions();
  });
  $('#reset-condition-buffs').onclick=()=>{$$('[data-condition-state]').forEach(e=>{const c=controls.find(c=>c.key===e.dataset.conditionState);if(!c.readonly&&c.group!=='target'){if(e.type==='checkbox')e.checked=false;else e.value=c.min??0;}});updateDerived();$('#condition-message').textContent='已关闭可操作 Buff，点击应用保存。';};
}
$('#delete-config').onclick=safe(async()=>{
  await flush();if(!doc)return;const id=doc.id,name=doc.name;
  modal('删除配置',`<p>将 <strong>${esc(name)}</strong> 移入回收站？可随时恢复。</p><p class="help">项目原始构筑和历史报告会保留。</p><div class="actions section"><button class="primary" id="confirm-delete">移入回收站</button><button id="cancel-delete">取消</button></div>`);
  $('#cancel-delete').onclick=closeModal;
  $('#confirm-delete').onclick=safe(async()=>{
    await api('delete',{id,version:versions.get(id)??doc.version});closeModal();versions.delete(id);await refreshList();
    if(baseDoc?.id===id){const next=boot.builds[0];baseDoc=next?await api('config?id='+next.id):null;if(baseDoc)localStorage.setItem('le-bd-baseline',baseDoc.id);else localStorage.removeItem('le-bd-baseline');}
    if(boot.builds.length){doc=await api('config?id='+boot.builds[0].id);versions.set(doc.id,doc.version);treeId=null;nodeId=null;await render();}else renderEmpty();
    status('已将 '+name+' 移入回收站');
  });
});
$('#trash').onclick=safe(async()=>{
  const items=await api('trash');modal('回收站',`<div class="stack">${items.length?items.map(item=>`<div class="row between"><div><strong>${esc(item.name)}</strong><p class="muted">${esc(new Date(item.deleted_at).toLocaleString())}</p></div><button data-undelete="${item.id}">恢复配置</button></div>`).join(''):'<p class="help">回收站为空。</p>'}</div>`);
  $$('[data-undelete]').forEach(b=>b.onclick=safe(async()=>{const item=items.find(i=>i.id===b.dataset.undelete);const restored=await api('undelete',{id:item.id,version:item.version});closeModal();await flush();doc=restored;versions.set(doc.id,doc.version);await refreshList();if(!baseDoc)baseDoc=clone(doc);await render();status('配置已恢复');}));
});

$('#close-dialog').onclick=closeModal;$('#name').onchange=()=>{doc.name=$('#name').value.trim()||'未命名配置';changed('配置名称已修改');renderBuildList();};
$$('#tabs button').forEach(b=>b.onclick=safe(async()=>{tab=b.dataset.tab;treeId=null;nodeId=null;itemTarget=null;await renderTab();}));
$('#save').onclick=safe(async()=>{await flushItemEdits();return save(true);});$('#copy').onclick=safe(async()=>{await flush();const name=prompt('新配置名称',doc.name+' · 副本');if(!name)return;doc=await api('copy',{id:doc.id,name});versions.set(doc.id,doc.version);await refreshList();await render();status('已复制配置');});
$('#baseline').onchange=safe(async()=>{await flush();baseDoc=await api('config?id='+$('#baseline').value);localStorage.setItem('le-bd-baseline',baseDoc.id);renderBuildList();renderDiff();loadCalculation();});
$('#new').onclick=()=>{modal('新建 BD',`<div class="stack"><label>名称<input id="new-name" value="新构筑"></label><div class="fields"><label>职业<select id="new-class">${options(boot.classes,x=>x.id,x=>x.name)}</select></label><label>等级<input id="new-level" type="number" min="1" max="100" value="100"></label></div><label>专精<select id="new-mastery"></select></label><button class="primary" id="create">创建配置</button></div>`);const masters=()=>{$('#new-mastery').innerHTML=options(boot.classes.find(c=>c.id===Number($('#new-class').value)).masteries,(x)=>boot.classes.find(c=>c.id===Number($('#new-class').value)).masteries.indexOf(x),x=>x);};$('#new-class').onchange=masters;masters();$('#create').onclick=safe(async()=>{await flush();doc=await api('new',{name:$('#new-name').value,class_id:Number($('#new-class').value),mastery:Number($('#new-mastery').value),level:Number($('#new-level').value)});versions.set(doc.id,doc.version);closeModal();await refreshList();treeId=null;await render();status('新 BD 已创建');});};
$('#import').onclick=()=>{modal('导入 BD 配置',`<div class="stack"><p class="help">选择现有 BuildSpec，或载入本平台导出的 JSON。离线游戏存档可先使用项目 import 流程转换。</p><button class="primary" id="choose-file">选择配置 JSON 文件</button><div class="section"><h3>项目已有构筑</h3>${boot.sources.length?boot.sources.map(s=>`<button class="source-button" data-source="${esc(s.id)}">${esc(s.name)}</button>`).join(''):'<p class="help">尚未发现已有 BuildSpec。</p>'}</div></div>`);$('#choose-file').onclick=()=>$('#file').click();$$('[data-source]').forEach(b=>b.onclick=safe(async()=>{await flush();doc=await api('import',{source:b.dataset.source});versions.set(doc.id,doc.version);closeModal();await refreshList();treeId=null;await render();status('构筑已导入为独立配置');}));};
$('#file').onchange=safe(async()=>{const f=$('#file').files[0];if(!f)return;if(f.size>12*1024*1024)throw new Error('文件超过 12 MB');const document=JSON.parse(await f.text());await flush();doc=await api('import',{document,name:f.name.replace(/\.json$/i,'')});versions.set(doc.id,doc.version);$('#file').value='';closeModal();await refreshList();treeId=null;await render();status('配置已导入');});
$('#export').onclick=safe(async()=>{await flush();download(doc.name,doc);status('已导出完整配置');});
$('#history').onclick=safe(async()=>{await flush();const id=doc.id,hs=await api('history?id='+id);modal('历史版本',`<div class="stack">${hs.length?hs.sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).map(h=>`<div class="row between"><span>${esc(new Date(h.updated_at).toLocaleString())}</span><button data-revision="${h.revision}">恢复此版本</button></div>`).join(''):'尚未保存版本'}</div>`);$$('[data-revision]').forEach(b=>b.onclick=safe(async()=>{if(!confirm('恢复这个版本？当前草稿将被替换，已保存的历史版本会保留。'))return;doc=await api('restore',{id,revision:b.dataset.revision});versions.set(id,doc.version);closeModal();await render();status('已恢复历史版本');}));});
window.addEventListener('beforeunload',e=>{if(saveTimer||pendingSaves||itemSession?.dirty||itemSession?.promise){e.preventDefault();e.returnValue='';}});
(async()=>{try{boot=await api('bootstrap');Object.entries(boot.item_names||{}).forEach(([id,name])=>catalogNames.set(id,name));$('#dataset').textContent=`数据版本 ${boot.game_version} · 装备与完整节点来自本机数据库 · English 原文`;if(!boot.builds.length){renderEmpty();status('新建、导入或从回收站恢复配置即可开始');return;}else doc=await api('config?id='+(boot.builds.find(b=>b.id===new URLSearchParams(location.search).get('config'))?.id||boot.builds[0].id));versions.set(doc.id,doc.version);let baseline=localStorage.getItem('le-bd-baseline');if(!boot.builds.some(b=>b.id===baseline))baseline=doc.id;baseDoc=await api('config?id='+baseline);await render();status('配置已就绪 · 可从现有构筑导入或新建');}catch(e){status(e.message,true);$('#content').innerHTML='<p class="empty">加载失败，请查看底部错误信息并确认本机服务仍在运行。</p>';}})();
