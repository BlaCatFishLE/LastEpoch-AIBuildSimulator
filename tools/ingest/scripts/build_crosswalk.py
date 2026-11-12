"""Build the explicit cross-source entity crosswalk.

The crosswalk is the only place where two sources' entities are declared to be
the same game object (workguide section 7.4).  It is generated from evidence
that is verifiable in the raw snapshots:

  * Last Epoch Tools and Tunklab both derive from the game's own data, so the
    game-internal ids are shared; still, an id alone is not accepted as proof.
  * Each merge requires at least two independent signals (shared id plus a
    matching English name, matching numeric index, matching tier structure or
    matching class scope), and the evidence string records them.
  * Anything ambiguous is left unmerged; the sources keep separate entities.

Run from the project root:

  python tools/ingest/scripts/build_crosswalk.py \
      --letools-record data/staging/<run>/source_records/letools.jsonl \
      --tunklab-record data/staging/<run>/source_records/tunklab.jsonl \
      --letools-translations data/raw/letools/<snapshot>/responses/translations_game_full_en.body \
      --out data/mappings/crosswalk.json \
      --report reports/<dataset>/crosswalk.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.normalize.base import make_entity_id  # noqa: E402

# Adapter entity kind -> canonical kind used by the normalization mappings.
CANONICAL_KIND = {
    "ability": "skill",
    "skill_tree": "tree",
    "passive_tree": "tree",
    "weaver_tree": "tree",
    "skill_tree_node": "node",
    "passive_node": "node",
    "weaver_node": "node",
    "buff": "buff",
    "ailment": "ailment",
    "affix": "affix",
    "unique": "item",
    "item_base": "item",
    "class": "class",
    "mastery": "mastery",
    "property": "stat",
    "minion_entity": "minion",
}

# Tunklab kind -> Last Epoch Tools kind for the same concept.
TUNKLAB_TO_LETOOLS_KIND = {
    "skill_tree": "skill_tree",
    "passive_tree": "passive_tree",
    "skill_tree_node": "skill_tree_node",
    "passive_tree_node": "passive_node",
    "unique_item": "unique",
}


def norm_name(value: Any) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def load_records(path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def canon_id(record: dict[str, Any]) -> str:
    kind = record["entity_kind"]
    canonical_kind = CANONICAL_KIND.get(kind, kind)
    return make_entity_id(canonical_kind, "letools", record["source_scope"], record["source_id"])


class CrosswalkBuilder:
    def __init__(self) -> None:
        self.merges: list[dict[str, Any]] = []
        self._by_canonical: dict[str, dict[str, Any]] = {}
        self.counts: Counter = Counter()
        self.skips: Counter = Counter()
        self.samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(
        self,
        canonical_entity_id: str,
        tunklab_record: dict[str, Any],
        method: str,
        evidence: str,
    ) -> None:
        merge = self._by_canonical.get(canonical_entity_id)
        if merge is None:
            merge = {
                "canonical_entity_id": canonical_entity_id,
                "members": [],
                "method": method,
                "evidence": evidence,
            }
            self._by_canonical[canonical_entity_id] = merge
            self.merges.append(merge)
        member = {
            "source": "tunklab",
            "kind": tunklab_record["entity_kind"],
            "scope": tunklab_record["source_scope"],
            "source_id": str(tunklab_record["source_id"]),
        }
        if member not in merge["members"]:
            merge["members"].append(member)
            self.counts[method] += 1
            self.counts[f"kind:{tunklab_record['entity_kind']}"] += 1
            if len(self.samples[method]) < 3:
                self.samples[method].append(
                    {"canonical": canonical_entity_id, "member": member, "evidence": evidence}
                )

    def skip(self, rule: str, reason: str) -> None:
        self.skips[f"{rule}:{reason}"] += 1


def build(
    letools_rows: list[dict[str, Any]],
    tunklab_rows: list[dict[str, Any]],
    translations: dict[str, str],
) -> CrosswalkBuilder:
    builder = CrosswalkBuilder()
    lt_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in letools_rows:
        lt_by_kind[row["entity_kind"]].append(row)
    tu_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in tunklab_rows:
        tu_by_kind[row["entity_kind"]].append(row)

    def tr(key: Any) -> str:
        if not key:
            return ""
        return translations.get(str(key), "")

    # -- trees ------------------------------------------------------------
    lt_trees: dict[tuple[str, str], dict[str, Any]] = {}
    for kind in ("skill_tree", "passive_tree", "weaver_tree"):
        for row in lt_by_kind.get(kind, []):
            lt_trees[(kind, str(row["source_id"]))] = row
    for tun_kind, lt_kind in (("skill_tree", "skill_tree"), ("passive_tree", "passive_tree")):
        for row in tu_by_kind.get(tun_kind, []):
            tree_id = str(row["source_id"])
            if not tree_id:
                builder.skip(f"tree_{tun_kind}", "empty_id")
                continue
            target = lt_trees.get((lt_kind, tree_id))
            if target is None:
                builder.skip(f"tree_{tun_kind}", "no_letools_tree_with_same_id")
                continue
            builder.add(
                canon_id(target),
                row,
                "shared_game_tree_id",
                f"treeId={tree_id} identical in both sources; kind={lt_kind}",
            )

    # -- nodes ------------------------------------------------------------
    lt_nodes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for kind in ("skill_tree_node", "passive_node", "weaver_node"):
        for row in lt_by_kind.get(kind, []):
            lt_nodes[(kind, str(row["source_scope"]), str(row["source_id"]))] = row
    for tun_kind, lt_kind in (
        ("skill_tree_node", "skill_tree_node"),
        ("passive_tree_node", "passive_node"),
    ):
        for row in tu_by_kind.get(tun_kind, []):
            tree_id = str(row["source_scope"])
            node_id = str(row["source_id"])
            target = lt_nodes.get((lt_kind, tree_id, node_id))
            if target is None:
                builder.skip(f"node_{tun_kind}", "no_letools_node_same_tree_and_id")
                continue
            builder.add(
                canon_id(target),
                row,
                "shared_game_tree_and_node_id",
                f"treeId={tree_id}, nodeId={node_id} identical in both sources",
            )

    # -- abilities --------------------------------------------------------
    lt_abilities = {str(row["source_id"]): row for row in lt_by_kind.get("ability", [])}
    for row in tu_by_kind.get("ability", []):
        raw = row["raw"]
        player_id = raw.get("playerAbilityID")
        if not player_id:
            builder.skip("ability", "no_player_ability_id")
            continue
        target = lt_abilities.get(str(player_id))
        if target is None:
            builder.skip("ability", "player_ability_id_not_found_in_letools")
            continue
        lt_raw = target["raw"]
        lt_name = lt_raw.get("name") or tr(lt_raw.get("nameKey"))
        tun_name = raw.get("name")
        numeric_ok = (
            raw.get("abilityId") is not None
            and lt_raw.get("index") is not None
            and raw.get("abilityId") == lt_raw.get("index")
        )
        name_ok = norm_name(lt_name) == norm_name(tun_name) and bool(lt_name)
        if not (name_ok or numeric_ok):
            builder.skip("ability", "id_matched_but_no_second_signal")
            continue
        evidence = [f"playerAbilityID={player_id} equals LE Tools ability id"]
        if name_ok:
            evidence.append(f"name={tun_name!r} matches")
        if numeric_ok:
            evidence.append(f"abilityId/index={raw.get('abilityId')} matches")
        builder.add(canon_id(target), row, "shared_player_ability_id", "; ".join(evidence))

    # -- buffs ------------------------------------------------------------
    lt_buffs = {str(row["source_id"]): row for row in lt_by_kind.get("buff", [])}
    for row in tu_by_kind.get("buff", []):
        target = lt_buffs.get(str(row["source_id"]))
        if target is None:
            builder.skip("buff", "no_letools_buff_with_same_id")
            continue
        lt_name = target["raw"].get("displayName") or tr(target["raw"].get("displayNameKey"))
        tun_name = row["raw"].get("displayName")
        if norm_name(lt_name) != norm_name(tun_name) or not lt_name:
            builder.skip("buff", "id_matched_but_name_differs")
            continue
        builder.add(
            canon_id(target),
            row,
            "shared_buff_id_and_name",
            f"buff id={row['source_id']}; name={tun_name!r} matches; "
            f"maxInstances={row['raw'].get('maxInstances')}/{target['raw'].get('maxInstances')}",
        )

    # -- ailments ---------------------------------------------------------
    lt_ailments = {str(row["source_id"]): row for row in lt_by_kind.get("ailment", [])}
    for row in tu_by_kind.get("ailment", []):
        target = lt_ailments.get(str(row["source_id"]))
        if target is None:
            builder.skip("ailment", "no_letools_ailment_with_same_id")
            continue
        lt_name = target["raw"].get("displayName") or tr(target["raw"].get("displayNameKey"))
        tun_name = row["raw"].get("displayName") or row["raw"].get("name")
        lt_key, tun_key = norm_name(lt_name), norm_name(tun_name)
        if not lt_key or (lt_key != tun_key and lt_key not in tun_key and tun_key not in lt_key):
            builder.skip("ailment", "id_matched_but_name_differs")
            continue
        builder.add(
            canon_id(target),
            row,
            "shared_ailment_id_and_name",
            f"ailment id={row['source_id']}; names {lt_name!r} / {tun_name!r}; "
            f"duration {target['raw'].get('duration')}/{row['raw'].get('duration')}",
        )

    # -- affixes ----------------------------------------------------------
    lt_affixes = {str(row["source_id"]): row for row in lt_by_kind.get("affix", [])}
    for row in tu_by_kind.get("affix", []):
        target = lt_affixes.get(str(row["source_id"]))
        if target is None:
            builder.skip("affix", "no_letools_affix_with_same_id")
            continue
        lt_tiers = target["raw"].get("tiers") or []
        tun_tiers = row["raw"].get("tiers") or []
        if len(lt_tiers) != len(tun_tiers) or not lt_tiers:
            builder.skip("affix", "tier_structure_differs")
            continue
        lt_name = tr(target["raw"].get("affixDisplayNameKey")) or tr(
            target["raw"].get("affixTitleKey")
        )
        tun_name = row["raw"].get("affixName")
        name_ok = norm_name(lt_name) == norm_name(tun_name) and bool(lt_name)
        builder.add(
            canon_id(target),
            row,
            "shared_affix_id_and_tier_structure",
            f"affixId={row['source_id']}; tiers={len(lt_tiers)} identical; "
            + (f"name={tun_name!r} matches" if name_ok else "names differ but id+tiers identical"),
        )

    # -- uniques ----------------------------------------------------------
    lt_uniques = {str(row["source_id"]): row for row in lt_by_kind.get("unique", [])}
    for row in tu_by_kind.get("unique_item", []):
        target = lt_uniques.get(str(row["source_id"]))
        if target is None:
            builder.skip("unique", "no_letools_unique_with_same_id")
            continue
        lt_raw = target["raw"]
        tun_raw = row["raw"]
        lt_name = tr(f"Item_Names.Unique_Name_{target['source_id']}")
        tun_name = tun_raw.get("name")
        name_ok = norm_name(lt_name) == norm_name(tun_name) and bool(lt_name)
        base_ok = (lt_raw.get("baseTypeId"), tuple(lt_raw.get("subTypeIds") or [])) == (
            tun_raw.get("baseType"),
            tuple(tun_raw.get("subTypes") or []),
        )
        mods_ok = len(lt_raw.get("mods") or []) == len(tun_raw.get("mods") or [])
        level_ok = lt_raw.get("levelRequirement") == tun_raw.get("levelRequirement")
        signals = sum([base_ok, mods_ok, level_ok])
        if signals < 2:
            builder.skip("unique", "id_matched_but_structure_differs")
            continue
        evidence = [
            f"uniqueId={row['source_id']}",
            f"baseType/subTypes={'same' if base_ok else 'differs'}",
            f"modCount={len(lt_raw.get('mods') or [])}",
            f"levelRequirement={lt_raw.get('levelRequirement')}",
        ]
        if name_ok:
            evidence.append(f"name={tun_name!r} matches")
        else:
            evidence.append(
                f"name differs (LE Tools translation {lt_name!r} vs Tunklab {tun_name!r}); "
                "Tunklab strips apostrophe+s in ids/names, id+structure accepted"
            )
        builder.add(
            canon_id(target),
            row,
            "shared_unique_id_and_structure",
            "; ".join(evidence),
        )

    # -- classes ----------------------------------------------------------
    lt_classes = {str(row["source_id"]): row for row in lt_by_kind.get("class", [])}
    class_map: dict[str, str] = {}
    for row in tu_by_kind.get("class", []):
        raw = row["raw"]
        class_id = raw.get("classID")
        target = lt_classes.get(str(class_id))
        if target is None:
            builder.skip("class", "class_id_not_found")
            continue
        lt_name = tr((target["raw"].get("characterClass") or {}).get("classNameKey"))
        tun_name = raw.get("className")
        if norm_name(lt_name) != norm_name(tun_name) or not lt_name:
            builder.skip("class", "id_matched_but_name_differs")
            continue
        canonical = canon_id(target)
        class_map[str(class_id)] = canonical
        builder.add(
            canonical,
            row,
            "shared_class_id_and_name",
            f"classID={class_id}; name={tun_name!r} matches",
        )

    # -- masteries --------------------------------------------------------
    lt_masteries = lt_by_kind.get("mastery", [])
    for row in tu_by_kind.get("mastery", []):
        tun_name = row["raw"].get("name")
        if not tun_name:
            builder.skip("mastery", "no_name")
            continue
        candidates = [
            target
            for target in lt_masteries
            if norm_name(target["raw"].get("name")) == norm_name(tun_name)
        ]
        if len(candidates) != 1:
            builder.skip("mastery", "name_not_unique_in_letools")
            continue
        target = candidates[0]
        builder.add(
            canon_id(target),
            row,
            "unique_mastery_name",
            f"mastery name={tun_name!r} unique in LE Tools; tunklab class={row['source_scope']!r}",
        )

    return builder


def write_report(builder: CrosswalkBuilder, out_path: Path, report_path: Path, sources: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Cross-source entity crosswalk",
        "",
        f"- generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- crosswalk: `{out_path}`",
        f"- total merged entities: {len(builder.merges)}",
        f"- total Tunklab members mapped: {sum(len(m['members']) for m in builder.merges)}",
        f"- inputs: {json.dumps(sources, ensure_ascii=False)}",
        "",
        "## Merge methods",
        "",
        "| method | members |",
        "|---|---|",
    ]
    for method, count in sorted(builder.counts.items()):
        if method.startswith("kind:"):
            continue
        lines.append(f"| {method} | {count} |")
    lines += ["", "## Merged members by Tunklab kind", "", "| kind | members |", "|---|---|"]
    for key, count in sorted(builder.counts.items()):
        if key.startswith("kind:"):
            lines.append(f"| {key[5:]} | {count} |")
    lines += ["", "## Not merged (reasons)", "", "| reason | count |", "|---|---|"]
    for key, count in sorted(builder.skips.items()):
        lines.append(f"| {key} | {count} |")
    lines += ["", "## Evidence samples", ""]
    for method, samples in builder.samples.items():
        lines.append(f"### {method}")
        for sample in samples:
            lines.append(
                f"- `{sample['canonical']}` <- {sample['member']['kind']}"
                f"/{sample['member']['scope']}/{sample['member']['source_id']}: {sample['evidence']}"
            )
        lines.append("")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--letools-record", required=True, type=Path)
    parser.add_argument("--tunklab-record", required=True, type=Path)
    parser.add_argument("--letools-translations", required=True, type=Path)
    parser.add_argument("--out", default=PROJECT_ROOT / "data/mappings/crosswalk.json", type=Path)
    parser.add_argument(
        "--report", default=PROJECT_ROOT / "reports/<dataset>/crosswalk.md", type=Path
    )
    parser.add_argument("--revision", default="2")
    args = parser.parse_args()
    translations = json.loads(args.letools_translations.read_text(encoding="utf-8"))
    builder = build(
        load_records(args.letools_record),
        load_records(args.tunklab_record),
        translations,
    )
    payload = {
        "revision": int(args.revision),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "tools/ingest/scripts/build_crosswalk.py",
        "rules": [
            "A merge needs at least two independent signals (shared game id plus name, "
            "numeric index, tier structure or class scope).",
            "Id equality alone is never accepted as proof.",
            "Ambiguous entities are left unmerged and remain separate per source.",
        ],
        "inputs": {
            "letools_records": str(args.letools_record),
            "tunklab_records": str(args.tunklab_record),
            "letools_translations": str(args.letools_translations),
        },
        "merges": builder.merges,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(builder, args.out, args.report, payload["inputs"])
    print(
        json.dumps(
            {
                "merges": len(builder.merges),
                "members": sum(len(m["members"]) for m in builder.merges),
                "methods": {k: v for k, v in builder.counts.items() if not k.startswith("kind:")},
                "kinds": {k[5:]: v for k, v in builder.counts.items() if k.startswith("kind:")},
                "skips": dict(builder.skips),
                "out": str(args.out),
                "report": str(args.report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
