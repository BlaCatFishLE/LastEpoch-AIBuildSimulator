"""Local BD workspace. Catalogs are read-only; editor documents live separately."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/build_import/src'))
from le_build.catalog import Catalog
from le_build.builder import BuildSpecBuilder
from le_build.resolve import Resolver
from le_build import rolls, readable
from le_build.calculation import build_calculation_input
sys.path.insert(0,str(Path(__file__).parent))
from player_projection import player_text, item_view
from skill_budget import budgets as compute_skill_budgets
from conditions import prepare as prepare_conditions

SLOTS = {'head': '头部', 'chest': '胸甲', 'waist': '腰带', 'feet': '鞋子', 'hands': '手套', 'weapon1': '主手', 'weapon2': '副手', 'amulet': '项链', 'ring1': '戒指一', 'ring2': '戒指二', 'relic': '遗物'}
SLOT_DB = {'waist': 'belt', 'head': 'helm', 'chest': 'body', 'feet': 'boots', 'hands': 'gloves', 'weapon1': 'weapon', 'weapon2': 'offhand', 'ring1': 'ring', 'ring2': 'ring'}
SCHEMA = 'le-bd-editor-config-v1'

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def digest(v):
    return hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def load(v, default=None):
    if not v:
        return {} if default is None else default
    return json.loads(v) if isinstance(v, str) else v

def integer(v, lo, hi, label):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f'{label}需为 {lo}–{hi} 的整数')
    return v

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

class Workspace:
    def __init__(self, root=ROOT, tool_home=None, storage=None):
        self.root = Path(root).resolve()
        self.storage = Path(storage) if storage else self.root / 'data/editor-workspace'
        if not self.storage.is_absolute():self.storage=self.root/self.storage
        self.storage=self.storage.resolve()
        self.catalog = Catalog.open(root=self.root, verify=True)
        self.c = self.catalog._conn
        self.a = sqlite3.connect('file:' + (self.root/'data/affix-db/v1/normalized/affix-db.sqlite').as_posix() + '?mode=ro', uri=True)
        self.a.row_factory = sqlite3.Row
        self.i18n = {}
        self.blessing_map=load((self.root/'tools/save_import/data/mappings/mapping-blessings.json').read_text(encoding='utf-8'))
        self.names = {r['entity_id']: r['canonical_name'] for r in self.c.execute('select entity_id,canonical_name from entities')}
        self.items = {}
        for row in self.c.execute('select * from items where cast(base_type as int) <= 41'):
            d = dict(row); d['extra'] = load(d['extensions']); d['name'] = self.text(d['extra'].get('displayNameKey'), self.names.get(d['entity_id'], '未命名物品'))
            self.items[d['entity_id']] = d
        for row in self.c.execute("select b.entity_id,s.raw_payload from blessings b join source_id_map m on m.entity_id=b.entity_id and m.kind='item_base' join source_records s on s.source_scope=m.source_scope and s.source_id=m.source_id and s.entity_kind='item_base' and s.source_snapshot_id=m.source_snapshot_id"):
            e=load(row['raw_payload']);eid=row['entity_id']
            self.items[eid]={'entity_id':eid,'base_type':'34','is_unique':0,'is_set':0,'level_requirement':e.get('levelRequirement',0),'item_type':'Blessing','equip_slot':'blessing','class_requirement':None,'extra':e,'name':self.text(e.get('displayNameKey'),self.names.get(eid,'祝福'))}
        self.trees = {}
        for row in self.c.execute('select * from trees'):
            d = dict(row); d['extra'] = load(d['extensions']); d['name'] = self.name(d['owner_entity_id']) if d['owner_entity_id'] else self.text(d['extra'].get('classNameKey'), self.names.get(d['entity_id'], '天赋树'))
            self.trees[d['entity_id']] = d
        self.sources = {}

    def text(self, key, fallback=''):
        return player_text(fallback)

    def name(self, eid):
        if eid in self.items: return self.items[eid]['name']
        fallback = self.names.get(eid, '未命名')
        if eid and eid.startswith('skill:'):
            r = self.c.execute('select extensions from skills where entity_id=?', (eid,)).fetchone()
            if r:
                e = load(r[0]); key = e.get('nameKey')
                if not key and e.get('internalName'):
                    n=e['internalName']; key='Abilities.Ability_'+n[:1].upper()+n[1:]+'_Name'
                return self.text(key, fallback)
        return fallback

    def builder(self):
        return BuildSpecBuilder(self.catalog, Resolver(self.catalog), restorer=rolls)

    def new_spec(self, class_id, mastery, level):
        integer(class_id, 0, 4, '职业'); integer(mastery, 0, 3, '专精'); integer(level, 1, 100, '等级')
        data = {'bio': {'characterClass': class_id, 'chosenMastery': mastery, 'level': level}, 'charTree': {'selected': {}}, 'skillTrees': [], 'equipment': {}, 'idols': [], 'blessings': {}, 'abilityBar': [], 'newGrid': True}
        spec = self.builder().build(draft={'data':data,'document':{'class':class_id,'mastery':mastery,'level':level}}, fetch_meta={'share_id':'editor-'+uuid.uuid4().hex}, catalog_compatibility={'status':'compatible'})
        spec['extensions']['bd_editor']={'origin':'new', 'passive_quest_points':0, 'skill_budgets':{}}
        return spec

    def classes(self):
        out=[]
        for r in self.c.execute('select * from classes'):
            e=load(r['extensions']); ch=e.get('characterClass',{}); masters=ch.get('masteries',[])
            out.append({'id':int(r['source_class_id']),'entity_id':r['entity_id'],'name':self.text(e.get('classNameKey'),self.names.get(r['entity_id'])),'tree':ch.get('treeID'),'masteries':[self.text(m.get('nameKey'),m.get('name')) for m in masters]})
        return out

    def source_list(self):
        self.sources={}
        for p in sorted((self.root/'data/builds').glob('*/*/current.json')):
            try:
                pointer=load(p.read_text(encoding='utf-8')); rev=pointer.get('revision_id') or pointer.get('revision',{}).get('revision_id')
                bp=p.parent/'revisions'/str(rev)/'build.json'
                if not bp.is_file(): continue
                key=digest(str(bp.relative_to(self.root)))[:16];self.sources[key]=bp
            except (ValueError,OSError,AttributeError): continue
        return [{'id':k,'name':v.parents[2].name} for k,v in self.sources.items()]

    def list(self,deleted=False):
        out=[]
        for p in sorted(self.storage.glob('*/current.json')):
            try:
                d=load(p.read_text(encoding='utf-8'))
                if bool(d.get('deleted_at'))!=deleted:continue
                out.append({k:d.get(k) for k in ('id','name','version','updated_at','revision','deleted_at')})
            except (ValueError,OSError): continue
        return sorted(out,key=lambda d:d['updated_at'] or '',reverse=True)

    def config_path(self, key):
        if not re.fullmatch(r'[a-f0-9]{32}',key or ''): raise ValueError('配置标识无效')
        return self.storage/key

    def get(self,key):
        p=self.config_path(key)/'current.json'
        if not p.is_file(): raise ValueError('配置不存在')
        document=load(p.read_text(encoding='utf-8'))
        if document.get('deleted_at'):raise ValueError('配置已移入回收站，请先恢复')
        return self.decorate(document)

    def set_deleted(self,key,expected_version,deleted=True):
        integer(expected_version,1,2147483647,'配置版本')
        path=self.config_path(key)/'current.json'
        if not path.is_file():raise ValueError('配置不存在')
        document=load(path.read_text(encoding='utf-8'))
        if document.get('version')!=expected_version:raise ValueError('配置已更新，请刷新后重试')
        if bool(document.get('deleted_at'))==deleted:return {'id':key,'name':document['name'],'version':document['version']}
        document['version']+=1;document['updated_at']=now()
        if deleted:document['deleted_at']=now()
        else:document.pop('deleted_at',None)
        atomic(path,document)
        return {'id':key,'name':document['name'],'version':document['version']} if deleted else self.decorate(document)

    def normalize_slots(self,spec):
        for entry in spec.get('equipment',{}).get('slots',[]):
            if entry.get('slot')=='belt':
                entry['slot']='waist';entry['instance']['slot']='waist'
        return spec

    def decorate(self,doc):
        view=copy.deepcopy(doc);spec=self.normalize_slots(view['build_spec'])
        items=[e['instance'] for e in spec['equipment'].get('slots',[])]+[e['instance'] for e in spec['idols'].get('entries',[])]+[e['instance'] for e in spec['blessings'].get('entries',[])]
        view['presentation']={'items':{it['instance_id']:item_view(it,self.catalog) for it in items},'language':'en'}
        view['skill_budgets']=compute_skill_budgets(spec,self.catalog)
        view['validation']=self.validate(spec)
        view['conditions'],view['condition_catalog']=prepare_conditions(self,spec,view.get('conditions'))
        return view

    def save(self, doc, commit=False):
        spec=doc.get('build_spec')
        self.check_shape(spec)
        key=doc.get('id') or uuid.uuid4().hex;folder=self.config_path(key);path=folder/'current.json'
        previous=load(path.read_text(encoding='utf-8')) if path.exists() else None
        if previous and previous.get('deleted_at'):raise ValueError('配置已删除，不能从旧窗口重新保存；请先在回收站恢复')
        if previous and doc.get('version')!=previous['version']: raise ValueError('配置已在另一窗口更新，请重新载入再编辑')
        name=str(doc.get('name') or '未命名配置').strip()[:80]
        spec=self.normalize_slots(copy.deepcopy(spec));spec.setdefault('extensions',{})
        equipment=spec['equipment'];equipment['absent_slots']=[s for s in SLOTS if s not in {v['slot'] for v in equipment.get('slots',[])}]
        equipment['slot_order']=[v['slot'] for v in equipment.get('slots',[])]
        for allocation in [spec['passive_tree'],*spec['specializations']]:
            ns=allocation.get('nodes',[]);allocation['node_entries']=len(ns)
            allocation['points_total']=sum(n.get('points',0) for n in ns if isinstance(n.get('points'),int))
            allocation['positive_node_entries']=sum(bool(n.get('points',0)) for n in ns)
            allocation['explicit_zero_entries']=sum(n.get('points')==0 for n in ns)
        all_items=[e['instance'] for e in equipment.get('slots',[])]+[e['instance'] for e in spec['idols'].get('entries',[])]+[e['instance'] for e in spec['blessings'].get('entries',[])]
        spec['readable_summary']=self.builder()._readable_summary(all_items)
        meta=spec.setdefault('meta',{});origin=spec['extensions'].setdefault('bd_editor_origin',{'build_id':meta.get('build_id'),'revision':copy.deepcopy(meta.get('revision'))})
        meta['build_id']='editor:'+key;meta['source']='editor'
        meta.pop('revision',None)
        spec.setdefault('statuses',{})['editor']='configuration_only_not_calculated'
        content=digest(spec)
        previous_revision=((previous or {}).get('build_spec',{}).get('meta',{}).get('revision') or {})
        meta['revision']=previous_revision if previous_revision.get('content_sha256')==content else {'revision_id':'rev-'+content[:16],'content_sha256':content,'revision_seq':(previous or {}).get('version',0)+1,'published_at':now()}
        conditions,_=prepare_conditions(self,spec,doc.get('conditions'))
        warnings=self.validate(spec)
        out={**doc,'schema':SCHEMA,'id':key,'name':name,'build_spec':copy.deepcopy(spec),'version':(previous or {}).get('version',0)+1,'updated_at':now(),'validation':warnings}
        out['conditions']=conditions
        out.pop('presentation',None);out.pop('skill_budgets',None);out.pop('condition_catalog',None);out.pop('deleted_at',None)
        if commit:
            rev=digest({'name':name,'content':content,'conditions':conditions})
            revision_path=folder/'revisions'/rev/'config.json'
            if not revision_path.exists(): atomic(revision_path,{**out,'revision':rev})
            out['revision']=rev
        atomic(path,out)
        return self.decorate(out)

    def check_shape(self,spec):
        if not isinstance(spec,dict) or 'build_spec_version' not in spec or not isinstance(spec.get('character'),dict): raise ValueError('请导入 BuildSpec build.json 或本平台导出的配置 JSON')
        for k in ('equipment','passive_tree','idols','blessings'):
            if not isinstance(spec.get(k),dict):raise ValueError('构筑缺少 '+k)
        if not isinstance(spec.get('specializations'),list):raise ValueError('构筑缺少专精技能列表')
        integer(spec['character'].get('level'),1,100,'角色等级')
        if len(spec['specializations'])>5: raise ValueError('专精技能最多 5 个')

    def detail(self,eid):
        if eid not in self.items:raise ValueError('物品不在当前数据库中')
        d=self.items[eid]; e=d['extra']; definition=self.catalog.item_definition(eid)
        result={k:d[k] for k in ('entity_id','name','base_type','item_type','level_requirement','is_unique','equip_slot')}
        result['extra']=e;result['implicits']=definition.get('implicits',[]);result['mods']=self.catalog.unique_mods(eid) if d['is_unique'] else []
        grid=self.a.execute('select grid_x,grid_y from equipment_types where base_type_id=?',(int(d['base_type']),)).fetchone()
        result['grid']=list(grid) if grid else [1,1]
        result['descriptions']=[self.text(t.get('descriptionKey'),t.get('altText','')) for t in e.get('tooltipDescriptions',[])]
        for mod in result['implicits']+result['mods']:
            stat=self.catalog.stat_by_property(mod.get('property'),mod.get('tags'),mod.get('specialTag'))
            mod['label']=self.catalog.stat_description(stat,mod.get('type',0),mod.get('tags',0),mod.get('specialTag',0)) if stat else '未解析效果'
        return result

    def item_list(self,slot='head',class_id=1,mastery=0,level=100,q='',kind='all',legacy=False,timeline=''):
        out=[]
        for d in self.items.values():
            e=d['extra'];base=int(d['base_type'] or -1)
            if slot=='blessing':
                if base!=34:continue
                mapping=self.blessing_map.get('blessings',{}).get(str(e.get('subTypeId')), {})
                if timeline and int(timeline) not in mapping.get('timelines',[]):continue
            elif slot=='idol':
                if d['equip_slot']!='idol':continue
            elif d['equip_slot']!=SLOT_DB.get(slot,slot) and not (slot=='weapon2' and d['equip_slot']=='weapon' and base in (5,6,7,8,9,10)):continue
            if kind=='base' and d['is_unique']:continue
            if kind=='unique' and not d['is_unique']:continue
            if not legacy and ((slot!='blessing' and e.get('cannotDrop')) or e.get('isLegacySubType')):continue
            req=load(d['class_requirement']);cr=req.get('classRequirement',0) if isinstance(req,dict) else 0;mr=req.get('subClassRequirement',0) if isinstance(req,dict) else 0
            if cr and cr!=class_id+1:continue
            if mr and mr!=mastery:continue
            if (d['level_requirement'] or 0)>level:continue
            if q.casefold() not in (d['name']+' '+self.names.get(d['entity_id'],'')+' '+(d['item_type'] or '')).casefold():continue
            out.append({'id':d['entity_id'],'name':d['name'],'english':self.names.get(d['entity_id'],''),'type':d['item_type'],'level':d['level_requirement'],'unique':bool(d['is_unique']),'base_type':base,'sub_type':e.get('subTypeId')})
        return sorted(out,key=lambda x:(x['unique'],x['level'] or 0,x['name']))

    def affixes(self,base):
        out=[]
        for row in self.a.execute('select distinct a.* from affixes a join affix_eligible_bases b on a.affix_id=b.affix_id where b.base_type_id=? and b.resolved=1 and b.is_equippable=1 and b.excluded_by_tunklab=0',(int(base),)):
            a=dict(row);eid='affix:letools:affixList.singleAffixes:'+str(a['affix_id']);er=self.catalog.affix(eid)
            if not er:
                rr=self.c.execute('select entity_id from affixes where source_affix_id=?',(str(a['affix_id']),)).fetchone();eid=rr[0] if rr else eid
            rs=[dict(r) for r in self.a.execute('select tier,roll_slot,property_id,property_name,mod_type,tags,special_tag,effective_min,effective_max,display_min,display_max,display_unit,display_scale from affix_base_ranges where affix_id=? and base_type_id=? order by tier,roll_slot',(a['affix_id'],int(base)))]
            if not rs:continue
            title=self.text('Item_Affixes.ItemAffix_'+str(a['affix_id'])+'_Affix_A',a['name'])
            out.append({'id':a['affix_id'],'entity_id':eid,'name':title,'english':a['name'],'slot':'prefix' if a['type']==0 else 'suffix','special':a['special_affix_type'],'category_group':a['group_id'],'class_specificity':a['class_specificity'],'ranges':rs})
        return out

    def tree(self,eid):
        if eid not in self.trees:raise ValueError('技能树不存在')
        t=copy.deepcopy(self.trees[eid]);nodes=[]
        for r in self.c.execute('select * from nodes where tree_entity_id=? order by node_index',(eid,)):
            n=dict(r);e=load(n['extensions']);n['extra']=e
            n['name']=self.text(e.get('nodeNameKey'),self.names.get(n['entity_id'],'未命名节点'))
            n['description']=self.text(e.get('descriptionKey'),n['description'])
            effects=[]
            for er in self.c.execute('select raw_text,extensions from effects where owner_entity_id=?',(n['entity_id'],)):
                if player_text(er['raw_text']):effects.append(player_text(er['raw_text']))
            n['effects']=effects;n.pop('extensions',None);nodes.append(n)
        t['nodes']=nodes;t['edges']=[dict(r) for r in self.c.execute('select * from node_edges where tree_entity_id=?',(eid,))]
        return t

    def preview_item(self,request):
        """Exact display lookup, generated by the same roll restorer as saved items."""
        item=self.make_item(request)
        def table(entry):
            out=[]
            for raw in range(256):
                values=[]
                for v in entry.get('values',[]):
                    lo,hi=v.get('roll_min'),v.get('roll_max')
                    if lo is None or hi is None:
                        values.append(str(v.get('display_value') or '待解析'));continue
                    stat=self.catalog.stat_by_property(v.get('property'),v.get('tags',0),v.get('special_tag',0))
                    restored=rolls.restore_value_from_range(lo,hi,raw,v.get('modifier_type',0),rolls.round_added_to_int(stat))
                    values.append(str(rolls.display_value(restored['value_numeric'],stat,v.get('modifier_type',0))))
                out.append(' / '.join(values) or '待解析')
            return out
        affixes=[];seen={}
        for draft in request.get('affixes',[]):
            channel=draft.get('channel','normal');index=seen.get(channel,0);seen[channel]=index+1
            affixes.append(table(item['affixes'][channel][index]['value']))
        return {'item':item,'roll_values':{'ir':[table(v) for v in item.get('implicit_rolls',{}).get('values',[])],
                'ur':{str(v.get('mod_index',i)):table(v) for i,v in enumerate(item.get('unique_rolls',{}).get('values',[]))},'affixes':affixes}}

    def make_item(self,request):
        eid=request.get('definition');d=self.items.get(eid)
        if not d:raise ValueError('请先选择装备')
        slot=request.get('slot','head');level=integer(request.get('level',100),1,100,'等级');class_id=integer(request.get('class_id',1),0,4,'职业');mastery=integer(request.get('mastery',0),0,3,'专精')
        timeline=(request.get('position') or {}).get('timeline','')
        if not any(x['id']==eid for x in self.item_list(slot,class_id,mastery,level,legacy=bool(request.get('legacy')),timeline=timeline)):raise ValueError('此物品不适用于当前槽位、时间线、职业或等级')
        e=d['extra'];base=d
        if d['is_unique']:
            sub=request.get('sub_type', (e.get('subTypeIds') or [None])[0]);allowed=e.get('subTypeIds') or []
            if sub not in allowed:raise ValueError('暗金基底不匹配')
            base=next((b for b in self.items.values() if not b['is_unique'] and b['base_type']==d['base_type'] and b['extra'].get('subTypeId')==sub),None)
            if not base:raise ValueError('暗金基底定义缺失')
        role='idol' if slot=='idol' else 'blessing' if slot=='blessing' else 'equipment';path=request.get('instance_id') or (f'equipment.{slot}' if role=='equipment' else role+'.new')
        reference=lambda b:{'entity_id':b['entity_id'],'name':b['name'],'ref':{'entity_id':b['entity_id'],'kind':'item','name':b['name']}}
        inst={'instance_id':path,'role':role,'source_path':'editor.'+path,'slot':slot if role=='equipment' else None,'encoded_id':None,'base':reference(base),'definition':reference(d) if d['is_unique'] else None,'base_status':'resolved','definition_status':'resolved','rarity':'unique' if d['is_unique'] else 'base','codec_class':'unique' if d['is_unique'] else 'base','affixes':{k:[] for k in ('normal','sealed','primordial','corrupted')},'status':'resolved','counts_as_equipped':True,'position':request.get('position')}
        builder=self.builder();ir=request.get('ir',[]);ur=request.get('ur',[])
        for v in ir+ur:integer(v,0,255,'取值进度')
        inst['implicit_rolls']=builder._implicit_block(base['entity_id'],{'ir':ir},'editor.'+path,role=role)
        if d['is_unique']:inst['unique_rolls']=builder._unique_roll_block(eid,{'ur':ur},'editor.'+path)
        choices={a['id']:a for a in self.affixes(int(base['base_type']))};entries=request.get('affixes',[])
        if len(entries)>7:raise ValueError('词缀数量过多')
        counts={'prefix':0,'suffix':0};ids=set();warnings=[]
        for index,a in enumerate(entries):
            definition=choices.get(a.get('id'));channel=a.get('channel','normal')
            if not definition:raise ValueError('词缀不适用于该基底')
            if channel not in inst['affixes']:raise ValueError('词缀通道无效')
            aid=definition['id'];tier=integer(a.get('tier'),1,8,'词缀阶级');roll=integer(a.get('roll'),0,255,'词缀取值')
            if aid in ids:raise ValueError('同一物品不能重复选择相同词缀')
            ids.add(aid)
            if channel=='normal':
                counts[definition['slot']]+=1
                # affix-db group_id is a broad classification, not an exclusion rule.
            if definition['special'] or channel!='normal':warnings.append('特殊词缀组合尚未完全验证')
            if definition['class_specificity']:warnings.append('职业专属词缀的额外限制需核实')
            ranges=[r for r in definition['ranges'] if r['tier']==tier]
            if not ranges:raise ValueError('该词缀没有所选阶级')
            tier_definition=self.catalog.affix_tier(definition['entity_id'],tier-1)
            if tier_definition and (tier_definition.get('level_requirement') or 0)>level:raise ValueError('角色等级不足以使用该阶级词缀')
            values=[]
            for r in ranges:
                stat=self.catalog.stat_by_property(r['property_id'],r['tags'],r['special_tag'])
                v=rolls.restore_value_from_range(r['effective_min'],r['effective_max'],roll,r['mod_type'],rolls.round_added_to_int(stat))
                v.update(property=r['property_id'],stat_id=(stat or {}).get('stat_id'),stat_name=(stat or {}).get('name') or r['property_name'],unit=(stat or {}).get('unit'),modifier_type=r['mod_type'],tags=r['tags'],special_tag=r['special_tag'],display_value=rolls.display_value(v['value_numeric'],stat,r['mod_type']),value_source='local_input')
                values.append(v)
            aff={'instance_id':f'{path}.{channel}.{index}','channel':channel,'source_path':f'editor.{path}.{channel}.{index}','encoded_id':None,'source_tier':tier,'affix':{'ref':{'entity_id':definition['entity_id'],'name':definition['name'],'kind':'affix'},'source_id':str(aid),'affix_slot':definition['slot'],'status':'resolved'},'roll':{'raw':roll,'status':'present'},'tier':{'source_tier':tier,'tier_index':tier-1,'status':'resolved'},'value':{'status':'resolved','roll_status':'present','raw_roll':roll,'values':values,'value_source':'local_input'},'status':'resolved'}
            inst['affixes'][channel].append(aff)
        idol=role=='idol';normal=inst['affixes']['normal']
        if counts['prefix']>(1 if idol else 2) or counts['suffix']>(1 if idol else 2):raise ValueError('前后缀数量超过当前物品类型上限')
        if d['is_unique'] and normal:
            if len(normal)>4:raise ValueError('传奇附加词缀最多四条')
            inst['rarity']='legendary';warnings.append('传奇获取路径及特殊组合尚未验证')
        if role=='idol':
            pos=request.get('position') or {};integer(pos.get('x'),0,4,'神像 X 坐标');integer(pos.get('y'),0,7,'神像 Y 坐标')
        for ch in ('sealed','primordial','corrupted'):
            if len(inst['affixes'][ch])>1:raise ValueError('每种特殊通道最多保留一条词缀')
        inst['affix_total']=len(entries);inst['affix_counts']={k:len(v) for k,v in inst['affixes'].items()}
        inst['readable']=readable.build_item_readable(inst,self.catalog)
        inst['editor']={'definition':eid,'sub_type':base['extra'].get('subTypeId'),'ir':copy.deepcopy(ir),'ur':copy.deepcopy(ur),'affixes':copy.deepcopy(entries),'warnings':sorted(set(warnings)),'legality':'partial' if warnings else 'known_rules_passed'}
        return inst

    def validate(self,spec):
        problems=[]
        issue=lambda severity,message:problems.append({'severity':severity,'message':message})
        if spec.get('catalog',{}).get('dataset_id')!=self.catalog.dataset_id:issue('warning','导入配置与当前数据集不同，引用与合法性需重新核对')
        class_id=int((spec['character'].get('class') or {}).get('source_value',0));mastery=int((spec['character'].get('mastery') or {}).get('source_value',0))
        passive=spec.get('passive_tree') or {};allocations=[passive]+list(spec.get('specializations') or [])
        editor=spec.get('extensions',{}).get('bd_editor',{})
        skill_budgets=compute_skill_budgets(spec,self.catalog)
        for allocation in allocations:
            ref=allocation.get('tree') or {};tid=ref.get('entity_id') or allocation.get('tree_entity_id')
            if not tid:continue
            if tid not in self.trees:issue('error','存在不属于当前数据集的天赋树');continue
            t=self.tree(tid);byid={n['entity_id']:n for n in t['nodes']};points={}
            for entry in allocation.get('nodes',[]):
                eid=(entry.get('ref') or {}).get('entity_id');p=entry.get('points')
                if eid not in byid:issue('error',t['name']+'有无法识别的节点');continue
                if isinstance(p,bool) or not isinstance(p,int) or p<0 or p>(byid[eid]['max_points'] or 0):issue('error',byid[eid]['name']+'点数越界');continue
                if eid in points:issue('error',byid[eid]['name']+'重复出现')
                points[eid]=p
            for n in t['nodes']:
                if not points.get(n['entity_id']):continue
                edges=[e for e in t['edges'] if e['to_node_entity_id']==n['entity_id']]
                if edges and not any(points.get(e['from_node_entity_id'],0)>=(e['points_required'] or 0) for e in edges):issue('error',n['name']+'的前置节点投入不足')
                ex=n['extra'];branch=int(ex.get('mastery') or 0);threshold=int(ex.get('masteryRequirement') or 0)
                if t['tree_kind']=='passive':
                    total=sum(points.get(v['entity_id'],0) for v in t['nodes'] if int(v['extra'].get('mastery') or 0)==branch and v['entity_id']!=n['entity_id'])
                    if total<threshold:issue('error',n['name']+'的分支投入未达到门槛')
                    if ex.get('lockNode') and branch!=mastery:issue('error',n['name']+'要求选择对应专精')
            total=sum(points.values());editor=spec.get('extensions',{}).get('bd_editor',{})
            if t['tree_kind']=='passive':
                quest=editor.get('passive_quest_points');budget=max(0,spec['character']['level']-1)+(quest if isinstance(quest,int) else 15)
                if total>budget:issue('error','被动天赋投入超过等级与任务点数预算')
            else:
                budget=skill_budgets.get(tid,{})
                low,high=budget.get('minimum'),budget.get('maximum')
                if high is not None and total>high:issue('error',t['name']+f'投入超过技能点数预算（已用 {total} / 可用 {high}）')
                elif low is None or total>low:issue('warning',t['name']+'的标签或技能等级加成尚未完全解析，预算需核实')
        slots={x['slot']:x['instance'] for x in spec['equipment'].get('slots',[])}
        for slot,item in slots.items():
            eid=(item.get('definition') or item.get('base') or {}).get('entity_id')
            d=self.items.get(eid)
            if not d:issue('warning',SLOTS.get(slot,slot)+'物品定义未解析');continue
            if slot in SLOTS and not any(x['id']==eid for x in self.item_list(slot,class_id,mastery,spec['character']['level'],legacy=True)):issue('error',SLOTS[slot]+'不符合角色等级、职业或槽位要求')
            for msg in item.get('editor',{}).get('warnings',[]):issue('warning',SLOTS.get(slot,slot)+'：'+msg)
        main=slots.get('weapon1');off=slots.get('weapon2')
        bt=lambda it:int(self.items.get((it.get('base') or {}).get('entity_id'),{}).get('base_type') or -1)
        if main and off and bt(main) in (12,13,14,15,16):issue('error','双手武器与副手不能同时装备')
        if off and bt(off)==17 and (not main or bt(main)!=23):issue('error','箭袋需要主手装备弓')
        if off and bt(off) in (5,6,7,8,9,10):issue('warning','副手武器的职业解锁条件需要另行核实')
        if spec.get('idols',{}).get('entries'):issue('warning','神像格位边界按当前版本待确认；重叠与尺寸可检查')
        occupied={}
        for entry in spec.get('idols',{}).get('entries',[]):
            item=entry.get('instance',{});pos=entry.get('position') or item.get('position') or {};base=self.items.get((item.get('base') or {}).get('entity_id'))
            if not base:continue
            grid=self.a.execute('select grid_x,grid_y from equipment_types where base_type_id=?',(int(base['base_type']),)).fetchone()
            x,y=pos.get('x'),pos.get('y')
            if not isinstance(x,int) or not isinstance(y,int):issue('warning','神像坐标未解析');continue
            w,h=grid if grid else (1,1)
            for dx in range(max(1,int(w or 1))):
                for dy in range(max(1,int(h or 1))):
                    key=(x+dx,y+dy)
                    if key in occupied:issue('error','神像 '+str(entry.get('index',0)+1)+' 与其他神像重叠')
                    occupied[key]=entry.get('index')
        for entry in spec.get('blessings',{}).get('entries',[]):
            eid=(entry.get('instance',{}).get('base') or {}).get('entity_id');d=self.items.get(eid)
            if not d:continue
            allowed=self.blessing_map.get('blessings',{}).get(str(d['extra'].get('subTypeId')),{}).get('timelines',[])
            if str(entry.get('timeline')) not in [str(x) for x in allowed]:issue('error','祝福与所选时间线不匹配')
        if editor.get('passive_quest_points') is None:issue('warning','任务被动点数尚未核实，预算暂采用 15 点上限；请在角色设置中填写实际点数')
        issue('info','配置检查与计算分开执行；技能预算已计入可解析的装备加成')
        return problems

def serve(root=ROOT,port=8766,tool_home=None,storage=None):
    workspace=Workspace(root,tool_home,storage);token=secrets.token_urlsafe(32)
    from calculation import CalculationManager
    calculations=CalculationManager(workspace,port)
    class Handler(BaseHTTPRequestHandler):
        def reply(self,status,value,kind='application/json; charset=utf-8'):
            content=json.dumps(value,ensure_ascii=False,allow_nan=False).encode() if kind.startswith('application/json') else value
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(content)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(content)
        def log_message(self,*args):pass
        def do_GET(self):
            try:
                if self.headers.get('Host') not in (f'127.0.0.1:{port}',f'localhost:{port}'):self.reply(403,{'error':'仅支持本机地址访问'});return
                u=urlparse(self.path);q={k:v[0] for k,v in parse_qs(u.query).items()}
                if u.path=='/':
                    page=(Path(__file__).parent/'web/index.html').read_text(encoding='utf-8').replace('__EDITOR_TOKEN__',token);self.reply(200,page.encode(),'text/html; charset=utf-8');return
                if u.path=='/calculation-plan':self.reply(200,(Path(__file__).parent/'web/calculation-plan.html').read_bytes(),'text/html; charset=utf-8');return
                if u.path in ('/app.js','/style.css'):
                    p=Path(__file__).parent/'web'/u.path[1:];self.reply(200,p.read_bytes(),'text/javascript; charset=utf-8' if p.suffix=='.js' else 'text/css; charset=utf-8');return
                if u.path=='/api/bootstrap':data={'classes':workspace.classes(),'trees':list(workspace.trees.values()),'builds':workspace.list(),'deleted_count':len(workspace.list(deleted=True)),'sources':workspace.source_list(),'slots':SLOTS,'dataset':workspace.catalog.dataset_id,'game_version':workspace.catalog.meta().get('target_game_version'),'localized':False,'names':workspace.names,'item_names':{k:v['name'] for k,v in workspace.items.items()},'timelines':[{'id':k,'name':workspace.text(v.get('name_key'),v.get('name'))} for k,v in workspace.blessing_map.get('timelines',{}).items()]}
                elif u.path=='/api/trash':data=workspace.list(deleted=True)
                elif u.path=='/api/config':data=workspace.get(q.get('id'))
                elif u.path=='/api/calculation':data=calculations.status(q['id'],q.get('evaluation'))
                elif u.path=='/api/evaluation':
                    name=q.get('name','dps-report.html');content=calculations.artifact(q['id'],q['evaluation'],name)
                    self.reply(200,content,'text/html; charset=utf-8' if name.endswith('.html') else 'application/octet-stream');return
                elif u.path=='/api/items':data=workspace.item_list(q.get('slot','head'),int(q.get('class_id',1)),int(q.get('mastery',0)),int(q.get('level',100)),q.get('q',''),q.get('kind','all'),q.get('legacy')=='true',q.get('timeline',''))
                elif u.path=='/api/item':data=workspace.detail(q['id'])
                elif u.path=='/api/affixes':data=workspace.affixes(q['base'])
                elif u.path=='/api/tree':data=workspace.tree(q['id'])
                elif u.path=='/api/history':data=[{'revision':p.parent.name,'updated_at':load(p.read_text(encoding='utf-8')).get('updated_at')} for p in (workspace.config_path(q['id'])/'revisions').glob('*/config.json')]
                else:self.reply(404,{'error':'页面不存在'});return
                self.reply(200,data)
            except (ValueError,KeyError,TypeError) as e:self.reply(400,{'error':str(e)})
            except Exception as e:self.reply(500,{'error':'数据读取失败：'+str(e)})
        def do_POST(self):
            try:
                if self.headers.get('X-LE-Editor')!=token:self.reply(403,{'error':'请从本机编辑页面发起操作'});return
                origin=self.headers.get('Origin')
                if origin and urlparse(origin).netloc!=self.headers.get('Host'):self.reply(403,{'error':'请求来源不匹配'});return
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=12*1024*1024:raise ValueError('配置文件大小需在 12 MB 内')
                req=json.loads(self.rfile.read(length));path=urlparse(self.path).path
                if path=='/api/new':data=workspace.save({'name':req.get('name'),'build_spec':workspace.new_spec(req.get('class_id',1),req.get('mastery',0),req.get('level',100))},True)
                elif path=='/api/import':
                    if req.get('source'):
                        workspace.source_list();p=workspace.sources.get(req['source'])
                        if not p:raise ValueError('请选择已有构筑')
                        doc={'build_spec':load(p.read_text(encoding='utf-8')),'name':req.get('name') or p.parents[2].name}
                    else:
                        obj=req.get('document');doc=copy.deepcopy(obj) if isinstance(obj,dict) and obj.get('schema')==SCHEMA else {'build_spec':obj,'name':req.get('name') or '导入配置'}
                    doc.pop('id',None);doc.pop('version',None);doc.pop('deleted_at',None);data=workspace.save(doc,True)
                elif path=='/api/copy':
                    doc=workspace.get(req['id']);doc.pop('id');doc.pop('version');doc.pop('revision',None);doc['name']=req.get('name') or doc['name']+' · 副本';data=workspace.save(doc,True)
                elif path=='/api/delete':data=workspace.set_deleted(req['id'],req['version'],True)
                elif path=='/api/undelete':data=workspace.set_deleted(req['id'],req['version'],False)
                elif path in ('/api/save','/api/draft'):data=workspace.save(req,path=='/api/save')
                elif path=='/api/conditions':
                    workspace.check_shape(req['build_spec']);conditions,available=prepare_conditions(workspace,req['build_spec'],req.get('conditions'));data={'conditions':conditions,'catalog':available}
                elif path=='/api/validate':workspace.check_shape(req['build_spec']);data=workspace.validate(req['build_spec'])
                elif path=='/api/make-item':data=workspace.make_item(req)
                elif path=='/api/item-preview':data=workspace.preview_item(req)
                elif path=='/api/export-calculation':workspace.check_shape(req['build_spec']);data=build_calculation_input(req['build_spec'])
                elif path=='/api/calculate':data=calculations.start(req)
                elif path=='/api/restore':
                    doc=workspace.get(req['id']);revision=req.get('revision','')
                    if not re.fullmatch(r'[a-f0-9]{64}',revision):raise ValueError('版本无效')
                    old=load((workspace.config_path(doc['id'])/'revisions'/revision/'config.json').read_text(encoding='utf-8'));old['version']=doc['version'];data=workspace.save(old,True)
                else:self.reply(404,{'error':'操作不存在'});return
                self.reply(200,data)
            except (ValueError,KeyError,TypeError) as e:self.reply(400,{'error':str(e)})
            except Exception as e:self.reply(500,{'error':'操作失败：'+str(e)})
    server=HTTPServer(('127.0.0.1',port),Handler)
    print(f'BD 编辑平台：http://127.0.0.1:{port}/',flush=True)
    try:server.serve_forever()
    finally:server.server_close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description='本机 BD 编辑平台');p.add_argument('--port',type=int,default=8766);p.add_argument('--tool-home',default=os.environ.get('LE_TOOL_NATIVE_HOME'));p.add_argument('--storage');a=p.parse_args();serve(port=a.port,tool_home=a.tool_home,storage=a.storage)
