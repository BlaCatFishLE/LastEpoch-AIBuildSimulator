"""Collect current build instances; definition selectors are lookup keys, never values."""
from __future__ import annotations


def collect_components(calc):
    rows = []
    def add(selector, instance, value=None, **extra):
        if selector:
            rows.append(dict(selector=selector, instance_id=instance, value=value, **extra))
    skills = calc.get("skills") or {}
    for skill in skills.get("action_bar") or []:
        sid = str(skill.get("entity_id") or "").rsplit(":", 1)[-1]
        if sid:
            add("ability:" + sid, "ability:" + sid)
    for group in (skills.get("active_buffs") or {}).values():
        if isinstance(group, list):
            for buff in group:
                if isinstance(buff, dict):
                    bid = buff.get("entity_id")
                    if bid:
                        add("mechanism:" + str(bid), str(bid))
    trees = list(skills.get("specializations") or [])
    passive = calc.get("passive_tree") or {}
    if passive:
        trees.append(passive)
    for tree in trees:
        tree_id = str((tree.get("tree") or {}).get("entity_id") or "").rsplit(":", 1)[-1]
        if not tree_id:
            continue
        if tree is not passive:
            add("ability:" + tree_id, "ability:" + tree_id, tree.get("points_total"))
            add("node:" + tree_id + ":0", "root:" + tree_id, 0, parameter="root")
        for node in tree.get("nodes") or []:
            if (node.get("points") or 0) > 0:
                key = f"node:{tree_id}:{node['node_id']}"
                add(key, key, node["points"], parameter="points")
    for item in calc.get("items") or []:
        iid = item.get("instance_id")
        add(item.get("definition_entity_id"), iid, role=item.get("role"))
    for mod in calc.get("mods") or []:
        stat = mod.get("stat") or {}
        extra = dict(stat_id=stat.get("stat_id"), property=mod.get("property"),
                     tags=mod.get("tags"), modifier_type=mod.get("modifier_type"),
                     item_instance_id=mod.get("item_instance_id"))
        add(stat.get("stat_id"), mod.get("mod_id"), mod.get("value_numeric"), **extra)
        affix = (mod.get("affix") or {}).get("entity_id")
        if affix:
            # The public knowledge namespace for an affix definition is item.
            add("item:" + affix, mod.get("mod_id"), mod.get("value_numeric"), **extra)
    unique = {(r["selector"], r["instance_id"]): r for r in rows}
    return sorted(unique.values(), key=lambda r: (r["selector"], str(r["instance_id"])))


def bind_instances(entry, components):
    selectors = set(entry.get("selectors") or [])
    seen = set()
    result = []
    for row in components:
        if row["selector"] in selectors and row["instance_id"] not in seen:
            result.append(row)
            seen.add(row["instance_id"])
    return result


def component_queries(components):
    return [dict(query_id="component:" + selector, term=selector, kind="id")
            for selector in sorted({r["selector"] for r in components})]


def snapshot_node_points(meta):
    """Compatibility adapter; current frozen input remains authoritative."""
    from pathlib import Path
    from ..catalog import REPO_ROOT
    ref = meta.get("calc_input") or {}
    if not ref.get("path"):
        return None
    path = Path(ref["path"])
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        return None
    points = _read_points(str(path), ref.get("sha256"))
    return dict(points) if points is not None else None


from functools import lru_cache
@lru_cache(maxsize=32)
def _read_points(path, digest):
    import json, hashlib
    from pathlib import Path
    content = Path(path).read_bytes()
    if not digest or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("snapshot calculation input hash mismatch while binding node points")
    calc = json.loads(content)
    for tree in (calc.get("skills") or {}).get("specializations") or []:
        if str((tree.get("tree") or {}).get("entity_id", "")).endswith(":bh2"):
            return {str(n["node_id"]): n.get("points", 0) for n in tree.get("nodes") or []}
    return None
