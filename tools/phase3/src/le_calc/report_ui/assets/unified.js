(function(){
'use strict';
const $=(s)=>document.querySelector(s), $$=(s)=>Array.from(document.querySelectorAll(s));
const B=JSON.parse($('#le-recalc-bindings').textContent),R=JSON.parse($('#result-data').textContent),V=JSON.parse($('#view-data').textContent);
const controls=R.controls, defs=V.defaults;
const client=window.__dpsClient||(window.__dpsClient={session:crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random(),revision:0,edit:0,request:0});
const canon=(x)=>JSON.stringify(x&&typeof x==='object'?(Array.isArray(x)?x.map(y=>JSON.parse(canon(y))):Object.fromEntries(Object.keys(x).sort().map(k=>[k,JSON.parse(canon(x[k]))]))):x);
function status(text){$('#compute-status').textContent=text;}
function dirty(){client.edit++;status('设置已更改，结果仍为上次计算');}
function collect(){
 const states={};controls.forEach(c=>{const e=$('[data-state="'+c.key+'"]');states[c.key]=c.type==='boolean'?e.checked:($('[data-enable="'+c.key+'"]').checked?Number(e.value):0);});
 const target={...defs.target,monster_level:Number($('#target-level').value),resistance:Number($('#target-resistance').value)/100,high_health:$('#target-high').checked,boss:$('#target-boss').checked,blinded:$('#target-blinded').checked,slowed:$('#target-slowed').checked}; return {states,target:Object.fromEntries(Object.keys(defs.target).map(k=>[k,target[k]]))};
}
function validate(s){
 if(!s||canon(Object.keys(s).sort())!==canon(['states','target']))throw Error('配置场景不完整');
 if(!s.states||canon(Object.keys(s.states).sort())!==canon(controls.map(c=>c.key).sort()))throw Error('配置包含不支持的 Buff');
 controls.forEach(c=>{const v=s.states[c.key];if(c.type==='boolean'){if(typeof v!=='boolean')throw Error('开关格式无效');}else if(!Number.isInteger(v)||v<c.min||v>c.max)throw Error(c.label+' 层数超出范围');if(c.readonly&&v!==c.default)throw Error(c.label+' 是常驻状态');});
 const t=s.target;if(!t||canon(Object.keys(t).sort())!==canon(Object.keys(defs.target).sort()))throw Error('目标配置不完整');
 if(!Number.isInteger(t.monster_level)||t.monster_level<1||t.monster_level>100||typeof t.resistance!=='number'||!Number.isFinite(t.resistance)||t.resistance< -1||t.resistance>.75||typeof t.high_health!=='boolean'||typeof t.boss!=='boolean'||typeof t.blinded!=='boolean'||typeof t.slowed!=='boolean')throw Error('目标参数超出范围');
 return s;
}
function apply(s){validate(s);controls.forEach(c=>{const e=$('[data-state="'+c.key+'"]'),v=s.states[c.key];if(c.type==='boolean')e.checked=v;else{const toggle=$('[data-enable="'+c.key+'"]');toggle.checked=v>0;e.value=v||1;e.disabled=!toggle.checked;}});$('#target-level').value=s.target.monster_level;$('#target-resistance').value=s.target.resistance*100;$('#target-high').checked=s.target.high_health;$('#target-boss').checked=s.target.boss;$('#target-blinded').checked=s.target.blinded;$('#target-slowed').checked=s.target.slowed;}
$$('[data-state], #target-level, #target-resistance, #target-high, #target-boss, #target-blinded, #target-slowed').forEach(e=>e.addEventListener('change',dirty));
$$('[data-enable]').forEach(e=>e.addEventListener('change',()=>{const input=$('[data-state="'+e.dataset.enable+'"]');input.disabled=!e.checked;dirty();}));
function targetOpen(open){$('#target-drawer').hidden=!open;$('#target-backdrop').hidden=!open;if(open)$('#target-level').focus();}
$$('[data-open-drawer="scenario"]').forEach(e=>e.addEventListener('click',()=>targetOpen(true)));
$('#close-target').onclick=()=>targetOpen(false);$('#target-backdrop').onclick=()=>targetOpen(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape')targetOpen(false);});
$('#reset-buffs').onclick=()=>{const s=collect();s.states={...defs.states};apply(s);dirty();};
$('#reset-target').onclick=()=>{const s=collect();s.target={...defs.target};apply(s);dirty();};
function download(data,name){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('#save-config').onclick=()=>{try{download({schema_version:'le-report-config-v1',binding:B,scenario:validate(collect())},'build-config.json');status('配置已保存');}catch(e){status(e.message);}};
$('#load-config').onclick=()=>$('#config-file').click();
$('#config-file').onchange=async e=>{const f=e.target.files[0];if(!f)return;try{const config=JSON.parse(await f.text());if(config.schema_version!=='le-report-config-v1'||!config.binding)throw Error('旧配置缺少报告绑定，无法自动载入');if(canon(config.binding)!==canon(B))throw Error('配置属于其它构筑或规则版本');const s=validate(config.scenario);apply(s);dirty();status('配置已载入，点击计算 DPS 应用');}catch(error){status(error.message);}e.target.value='';};
$('#compute').onclick=async()=>{
 let scenario;try{scenario=validate(collect());}catch(error){status(error.message);return;}
 const revision=++client.revision,edit=client.edit,request=++client.request,id=client.session+':'+revision;
 status('计算中…');
 try{
  const response=await fetch(B.recompute_endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({binding:B,scenario,request_id:id,client_session_id:client.session,client_edit_revision:revision})});
  const data=await response.json();if(!response.ok)throw Error(data.error||'重算失败');
  if(request!==client.request||edit!==client.edit){status('设置在计算期间已更改，请重新计算');return;}
  if(canon(data.binding)!==canon(B)||data.request_id!==id||data.client_session_id!==client.session||data.accepted_revision!==revision)throw Error('响应绑定无效，保留上次结果');
  const page=document.body.dataset.page||'overview';history.replaceState(null,'','#page-'+page);
  document.open();document.write(data.html);document.close();
 }catch(error){if(request===client.request){status(error instanceof TypeError?'本机重算服务未连接，保留上次结果；请打开报告的本机地址。':error.message);}}
};
function filter(){const text=$('#source-search').value.toLowerCase(),category=$('#source-category').value,active=$('#source-status').value;$$('[data-source-group]').forEach(g=>g.hidden=!(g.innerText.toLowerCase().includes(text)&&(!category||g.dataset.category===category)&&(!active||g.dataset.active===active)));}
$('#source-search').oninput=filter;$('#source-category').onchange=filter;$('#source-status').onchange=filter;
})();
