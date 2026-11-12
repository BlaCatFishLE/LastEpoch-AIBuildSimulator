"""Collect shape facts for the author-local discovery notes (archived).

Reads the staged Tunklab snapshot (local raw bytes only) and prints a compact
fact sheet: page/chunk mapping, per-chunk data modules, entity counts, field
key unions, relationship counts and cross-checks against the page HTML tables.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_facts.py --snapshot tunklab-20261101 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.adapters.tunklab import TunklabAdapter  # noqa: E402
from le_data.config import load_sources  # noqa: E402
from le_data.paths import RAW_DIR, STAGING_DIR  # noqa: E402

SNAPSHOT_DEFAULT = "tunklab-20261101"
RUN_DEFAULT = "w1-tunklab"


def read_body(snapshot: str, name: str) -> str:
    return (RAW_DIR / "tunklab" / snapshot / "responses" / f"{name}.body").read_text(
        encoding="utf-8", errors="replace")


def table_rows(html: str) -> int:
    return len(re.findall(r"<tr", html))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default=SNAPSHOT_DEFAULT)
    parser.add_argument("--run", default=RUN_DEFAULT)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    snapshot = args.snapshot

    inv = json.loads((RAW_DIR / "tunklab" / snapshot / "inventory.json").read_text(encoding="utf-8"))
    summary = json.loads(
        (STAGING_DIR / args.run / "tunklab_summary.json").read_text(encoding="utf-8"))
    facts: dict = {"snapshot": snapshot, "run": args.run}
    facts["inventory"] = {
        "status": inv.get("status"),
        "base_url": inv.get("base_url"),
        "claimed_version": inv.get("claimed_version"),
        "version_evidence": inv.get("version_evidence"),
        "fetch_summary": inv.get("fetch_summary"),
        "page_chunks": inv.get("page_chunks"),
        "resources": [
            {k: r.get(k) for k in ("resource_name", "role", "page", "status", "size",
                                   "sha256", "referenced_by", "http_status")}
            for r in inv.get("resources", [])
        ],
    }
    facts["entity_kind_counts"] = summary["parse"]["entity_kind_counts"]
    facts["relation_counts"] = summary["parse"]["relation_counts"]
    facts["module_inventory"] = summary["parse"]["module_inventory"]
    facts["chunk_entity_map"] = summary["parse"]["chunk_entity_map"]
    facts["issues"] = summary["issues"]
    facts["stats"] = summary["parse"]["stats"]

    # page HTML cross-checks
    checks = {}
    for name in ("page_buffs", "page_ailments", "page_class_mage", "page_skillsearch", "page_root"):
        html = read_body(snapshot, name)
        checks[name] = {
            "bytes": len(html),
            "tr_rows": table_rows(html),
            "ability_links": len(re.findall(r"/ability\?a=", html)),
            "has_next_data": "__NEXT_DATA__" in html,
        }
    facts["page_checks"] = checks

    # parsed payload details straight from the modules
    adapter = TunklabAdapter(load_sources().get("tunklab"), RAW_DIR)
    collected: dict[str, list] = defaultdict(list)
    for resource_name in sorted(adapter._response_names(snapshot)):
        body = read_body(snapshot, resource_name)
        if adapter._sniff_role(body) != "chunk":
            continue
        literals, _ = adapter._iter_literals(resource_name, body)
        for literal in literals:
            kind = adapter._classify(literal.data)
            if kind == "config_block" and literal.wrapper != "versioned":
                kind = None
            literal.kind = kind
            if kind:
                collected[kind].append(literal)

    def payload(kind):
        return [lit.data for lit in collected.get(kind, [])]

    details: dict = {}
    # skill trees
    trees = payload("skill_tree")[0] if payload("skill_tree") else {}
    sizes = {tree: len(nodes) for tree, nodes in trees.items()}
    roots = [nodes[0].get("nodeName") for nodes in trees.values() if nodes]
    details["skill_trees"] = {
        "tree_ids": len(trees),
        "trees_with_nodes": sum(1 for n in trees.values() if n),
        "empty_keys": [k for k, v in trees.items() if not v],
        "node_total": sum(sizes.values()),
        "node_count_min": min(sizes.values()) if sizes else 0,
        "node_count_max": max(sizes.values()) if sizes else 0,
        "root_names": len(set(roots)),
        "duplicate_root_names": sorted(
            name for name, count in Counter(roots).items() if count > 1),
        "node_keys": sorted({k for nodes in trees.values() for node in nodes for k in node}),
        "requirement_total": sum(len(node.get("requirements") or [])
                                 for nodes in trees.values() for node in nodes),
        "node_with_ability": sum(1 for nodes in trees.values() for node in nodes
                                 if node.get("ability")),
        "node_with_stats": sum(1 for nodes in trees.values() for node in nodes
                               if node.get("stats")),
        "distinct_stats_properties": len({
            stat.get("property")
            for nodes in trees.values() for node in nodes
            for stat in node.get("stats") or [] if isinstance(stat, dict)}),
    }
    # passive trees
    passive = payload("passive_tree")[0] if payload("passive_tree") else {}
    details["passive_trees"] = {
        "tree_ids": sorted(passive),
        "node_total": sum(len(v) for v in passive.values()),
        "requirement_total": sum(len(n.get("requirements") or [])
                                 for v in passive.values() for n in v),
        "mastery_values": sorted({n.get("mastery") for v in passive.values() for n in v
                                  if "mastery" in n}),
        "node_keys": sorted({k for v in passive.values() for n in v for k in n}),
    }
    # abilities
    abilities = payload("ability")[0] if payload("ability") else []
    details["abilities"] = {
        "count": len(abilities),
        "distinct_ids": len({a.get("id") for a in abilities}),
        "with_player_ability_id": sum(1 for a in abilities if a.get("playerAbilityID")),
        "with_ability_id": sum(1 for a in abilities if a.get("abilityId")),
        "with_damage": sum(1 for a in abilities if a.get("damage")),
        "with_description": sum(1 for a in abilities if a.get("description")),
        "keys": sorted({k for a in abilities for k in a}),
        "player_ability_ids": len({a["playerAbilityID"] for a in abilities
                                   if a.get("playerAbilityID")}),
    }
    # classes
    classes = payload("class")[0] if payload("class") else []
    details["classes"] = {
        "classes": [
            {"className": c.get("className"), "classID": c.get("classID"),
             "treeID": c.get("treeID"),
             "masteries": [m.get("name") for m in c.get("masteries") or []],
             "unlockable": len(c.get("unlockableAbilities") or []),
             "keys": len(c)}
            for c in classes
        ],
    }
    # buffs / ailments
    buffs = payload("buff")[0] if payload("buff") else []
    ailments = payload("ailment")[0] if payload("ailment") else []
    details["buffs"] = {
        "count": len(buffs),
        "keys": sorted({k for b in buffs for k in b}),
        "positive": sum(1 for b in buffs if b.get("positive")),
        "with_description": sum(1 for b in buffs if b.get("description")),
        "max_instances_nonzero": sum(1 for b in buffs if b.get("maxInstances")),
        "no_fixed_duration": sum(1 for b in buffs if b.get("noFixedDuration")),
    }
    details["ailments"] = {
        "count": len(ailments),
        "keys": sorted({k for a in ailments for k in a}),
        "positive": sum(1 for a in ailments if a.get("positive")),
        "deals_damage": sum(1 for a in ailments if a.get("dealsDamage")),
        "with_duration": sum(1 for a in ailments if "duration" in a),
        "with_base_damage": sum(1 for a in ailments if a.get("baseDamage")),
        "with_max_instances": sum(1 for a in ailments if "maxInstances" in a),
        "with_tags": sum(1 for a in ailments if "tags" in a),
        "with_buffs_stats": sum(1 for a in ailments if a.get("buffs")),
        "spreads": sum(1 for a in ailments if a.get("spreads")),
        "sample": [a for a in ailments if a.get("name") == "Ignite"][:1],
    }
    name_overlap = len({b["displayName"] for b in buffs} & {a.get("displayName") for a in ailments})
    details["buff_ailment_name_overlap"] = name_overlap
    # items
    uniques = payload("unique_item")[0] if payload("unique_item") else []
    affixes = payload("affix")[0] if payload("affix") else []
    item_types = payload("item_type")[0] if payload("item_type") else {}
    sets = payload("item_set")[0] if payload("item_set") else []
    details["items"] = {
        "unique_count": len(uniques),
        "unique_is_set_item": sum(1 for u in uniques if u.get("isSetItem")),
        "unique_with_mods": sum(1 for u in uniques if u.get("mods")),
        "affix_count": len(affixes),
        "affix_keys": sorted({k for a in affixes for k in a}),
        "affix_with_tiers": sum(1 for a in affixes if a.get("tiers")),
        "affix_tier_lengths": sorted({len(a.get("tiers") or []) for a in affixes}),
        "set_count": len(sets),
        "set_with_mods": sum(1 for s in sets if s.get("mods")),
        "equippable_base_types": len(item_types.get("equippableItems") or []),
        "non_equippable_base_types": len(item_types.get("nonEquippableItems") or []),
        "equippable_sub_types": sum(len(i.get("subItems") or [])
                                   for i in item_types.get("equippableItems") or []),
        "non_equippable_sub_types": sum(len(i.get("subItems") or [])
                                       for i in item_types.get("nonEquippableItems") or []),
        "item_type_keys": sorted({k for group in ("equippableItems", "nonEquippableItems")
                                  for i in item_types.get(group) or [] for k in i}),
        "sub_item_keys": sorted({k for group in ("equippableItems", "nonEquippableItems")
                                 for i in item_types.get(group) or []
                                 for sub in i.get("subItems") or [] for k in sub})[:40],
        "implicit_keys": sorted({k for group in ("equippableItems", "nonEquippableItems")
                                 for i in item_types.get(group) or []
                                 for sub in i.get("subItems") or []
                                 for imp in sub.get("implicits") or [] for k in imp}),
    }
    # property dictionaries
    props = payload("property")[0] if payload("property") else []
    prop_texts = payload("property_text")[0] if payload("property_text") else []
    ability_props = payload("ability_properties")[0] if payload("ability_properties") else []
    details["properties"] = {
        "property_count": len(props),
        "property_keys": sorted({k for p in props for k in p}),
        "property_max_id": max((p.get("property") for p in props if "property" in p), default=None),
        "property_texts": len(prop_texts),
        "property_text_keys": sorted({k for p in prop_texts for k in p}),
        "ability_property_groups": len(ability_props),
        "ability_property_groups_with_fireball": [
            g.get("abilityID") for g in ability_props if "Fireball" in json.dumps(g)][:5],
        "ability_properties_item_keys": sorted({k for g in ability_props for k in g}),
        "per_ability_property_keys": sorted({k for g in ability_props
                                             for p in g.get("properties") or [] for k in p}),
    }
    # optional domain counts
    optional = {kind: (len(payload(kind)[0]) if payload(kind)
                       and isinstance(payload(kind)[0], list) else
                       len(payload(kind)[0]) if payload(kind) else 0)
                for kind in ("monster", "quest", "cache", "monolith_mod", "loot_group",
                             "dungeon", "timeline")}
    details["optional_domains"] = optional
    details["optional_keys"] = {
        kind: sorted({k for item in (payload(kind)[0] if payload(kind) else [])
                      for k in item}) if kind != "timeline" else None
        for kind in ("monster", "quest", "cache", "monolith_mod", "loot_group", "dungeon",
                     "timeline")}
    details["world_config"] = {
        "keys": list((payload("world_config")[0] if payload("world_config") else {}).keys()),
        "scene_count": len((payload("world_config")[0] if payload("world_config")
                            else {}).get("scenes") or []),
    }
    details["monolith_config"] = [
        {"keys": len(d), "sample_keys": list(d)[:12]}
        for d in payload("monolith_config")
    ]
    details["timelines"] = {
        "count": len(payload("timeline")[0]) if payload("timeline") else 0,
        "keys": sorted({k for t in (payload("timeline")[0] if payload("timeline") else [])
                        for k in t}),
    }
    details["unmodelled_literals"] = [
        m for m in facts["module_inventory"]
        if m["kind"] is None and m["wrapper"] == "assignment" and (m["count"] or 0) >= 10
    ]
    facts["details"] = details

    text = json.dumps(facts, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text, encoding="utf-8")
        print(f"wrote {args.json}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
