"""Player-only text projection. Raw evidence and saved definitions stay intact."""
import copy
import json
import re

def player_text(value):
    text=str(value or '').strip()
    if text.startswith(('{','[')):
        try:
            if isinstance(json.loads(text),(dict,list)):return ''
        except ValueError:pass
    text=re.sub(r'<[^>]*>','',text)
    text=text.replace("'{'",'{').replace("'}'",'}')
    def unwrap(match):
        inner=match.group(1).strip(" '\"")
        if ':' in inner or inner.lower() in ('value','stat','tags','property','conditions'):return ''
        return inner
    return re.sub(r'\{([^{}]*)\}',unwrap,text).replace('{','').replace('}','').strip()

def item_view(instance,catalog):
    from le_build.readable import build_item_readable
    item=copy.deepcopy(instance)
    for channel in item.get('affixes',{}).values():
        for affix in channel:
            ref=affix.get('affix',{}).get('ref',{})
            if ref.get('entity_id'):ref['name']=catalog.name(ref['entity_id']) or ref.get('name')
    result=build_item_readable(item,catalog)
    for line in result.get('lines',[]):line['text']=player_text(line.get('text'))
    return result
