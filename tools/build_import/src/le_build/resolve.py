"""Identity resolution: source references -> catalog entities (never a guess).

Every resolver returns a plain dict with a ``status`` of ``resolved`` / ``unresolved``
/ ``ambiguous``, the original query, all candidates and the evidence trail.  A single
candidate is only accepted when the full ``source_snapshot_id + source + kind +
source_scope + source_id`` namespace yields exactly one row; otherwise the reference
stays unresolved/ambiguous and is reported.
"""

from __future__ import annotations

import json

from .catalog import Catalog
from .codecs import letools_ids

RESOLVED = "resolved"
UNRESOLVED = "unresolved"
AMBIGUOUS = "ambiguous"

DEFAULT_SOURCE = "letools"


def _candidate(row: dict) -> dict:
    return {
        "entity_id": row.get("entity_id"),
        "entity_kind": row.get("entity_kind"),
        "canonical_name": row.get("canonical_name"),
        "source_scope": row.get("source_scope"),
        "source_id": row.get("source_id"),
        "mapping_method": row.get("mapping_method"),
        "evidence": row.get("evidence"),
    }


class Resolver:
    """Resolves LE Tools build references against one fixed catalog dataset."""

    def __init__(self, catalog: Catalog, *, source: str = DEFAULT_SOURCE):
        self.catalog = catalog
        self.source = source
        self.snapshot_id = catalog.snapshot_id(source)
        self._cache: dict = {}

    # ------------------------------------------------------------------ helpers
    def _lookup(self, kind: str, scope: str | None, source_id) -> list[dict]:
        if scope is None:
            rows = self.catalog.source_lookup_any_scope(kind, str(source_id), source=self.source)
        else:
            rows = self.catalog.source_lookup(kind, scope, str(source_id), source=self.source)
        return rows

    def _resolve_rows(
        self,
        *,
        what: str,
        query: dict,
        rows: list[dict],
        conversions: list[str] | None = None,
        extra_reason: str | None = None,
    ) -> dict:
        base = {
            "what": what,
            "query": query,
            "conversions": conversions or [],
            "candidates": [_candidate(row) for row in rows],
        }
        if len(rows) == 1:
            row = rows[0]
            base.update(
                status=RESOLVED,
                entity={
                    "entity_id": row.get("entity_id"),
                    "kind": row.get("entity_kind"),
                    "name": row.get("canonical_name"),
                },
                evidence=row.get("evidence"),
            )
            return base
        if not rows:
            base.update(status=UNRESOLVED, entity=None, reason=extra_reason or "no catalog match")
            return base
        base.update(
            status=AMBIGUOUS,
            entity=None,
            reason=extra_reason or f"{len(rows)} candidate rows share the same query namespace",
        )
        return base

    def _resolve_entity_id(self, *, what: str, query: dict, entity_id: str | None,
                           conversions: list[str] | None = None, reason: str | None = None) -> dict:
        if entity_id:
            entity = self.catalog.entity(entity_id)
            if entity:
                return {
                    "what": what,
                    "query": query,
                    "conversions": conversions or [],
                    "status": RESOLVED,
                    "entity": {
                        "entity_id": entity["entity_id"],
                        "kind": entity.get("kind"),
                        "name": entity.get("canonical_name"),
                    },
                    "candidates": [],
                    "evidence": None,
                }
        return {
            "what": what,
            "query": query,
            "conversions": conversions or [],
            "status": UNRESOLVED,
            "entity": None,
            "candidates": [],
            "reason": reason or "derived entity id is missing from the catalog",
        }

    # --------------------------------------------------------------- character
    def resolve_class(self, source_class_id) -> dict:
        rows = self._lookup("class", "classInfoForSources", source_class_id)
        result = self._resolve_rows(
            what="class",
            query={"kind": "class", "source_scope": "classInfoForSources", "source_id": str(source_class_id)},
            rows=rows,
            conversions=[f"bio.characterClass={source_class_id!r} -> classInfoForSources[{source_class_id!r}]"],
        )
        if result["status"] == RESOLVED:
            entity = self.catalog.entity(result["entity"]["entity_id"])
            try:
                ext = json.loads(entity.get("extensions") or "{}")
            except json.JSONDecodeError:
                ext = {}
            class_info = ext.get("characterClass") or {}
            result["character_class"] = class_info
            if class_info.get("classID") is not None and str(class_info["classID"]) != str(source_class_id):
                result["status"] = AMBIGUOUS
                result["reason"] = (
                    "catalog class row source id does not match the embedded characterClass.classID"
                )
        return result

    def resolve_mastery(self, class_source_id, chosen_mastery, class_resolution: dict) -> dict:
        if class_resolution.get("status") != RESOLVED or not chosen_mastery:
            return {
                "what": "mastery",
                "query": {"class_source_id": class_source_id, "chosenMastery": chosen_mastery},
                "status": UNRESOLVED,
                "entity": None,
                "candidates": [],
                "conversions": [],
                "reason": "class unresolved or chosenMastery absent",
            }
        scope = f"class:{class_source_id}"
        source_id = f"{class_source_id}.{chosen_mastery}"
        rows = self._lookup("mastery", scope, source_id)
        result = self._resolve_rows(
            what="mastery",
            query={"kind": "mastery", "source_scope": scope, "source_id": source_id},
            rows=rows,
            conversions=[
                f"bio.chosenMastery={chosen_mastery!r} scoped by class -> {scope}/{source_id}",
                "mastery index is only valid inside its class namespace",
            ],
        )
        if result["status"] == RESOLVED:
            entity = self.catalog.entity(result["entity"]["entity_id"])
            if entity.get("kind") != "mastery":
                result["status"] = UNRESOLVED
                result["reason"] = "resolved entity is not a mastery"
        return result

    def resolve_passive_tree(self, class_entity_id: str, class_name: str | None) -> dict:
        rows = self.catalog.passive_tree_for_class(class_entity_id)
        result = {
            "what": "passive_tree",
            "query": {"tree_kind": "passive", "class_entity_id": class_entity_id, "source_treeID": ""},
            "conversions": [
                "charTree.treeID is empty -> derive the unique passive tree from the class entity",
            ],
            "candidates": [
                {
                    "entity_id": row["entity_id"],
                    "entity_kind": "tree",
                    "canonical_name": self.catalog.name(row["entity_id"]),
                    "source_scope": str(row.get("source_tree_id")),
                    "source_id": str(row.get("source_tree_id")),
                    "mapping_method": "tree_kind+class_entity_id",
                    "evidence": None,
                }
                for row in rows
            ],
        }
        if len(rows) == 1:
            tree = rows[0]
            result.update(
                status=RESOLVED,
                entity={
                    "entity_id": tree["entity_id"],
                    "kind": "tree",
                    "name": self.catalog.name(tree["entity_id"]),
                },
                source_tree_id=tree.get("source_tree_id"),
                tree=tree,
            )
        elif not rows:
            result.update(status=UNRESOLVED, entity=None, reason="no passive tree bound to this class")
        else:
            result.update(status=AMBIGUOUS, entity=None, reason="multiple passive trees bound to this class")
        return result

    def resolve_weaver_tree(self, source_version=None) -> dict:
        rows = self.catalog.weaver_trees()
        result = {
            "what": "weaver_tree",
            "query": {"tree_kind": "weaver", "source_treeID": "", "source_version": source_version},
            "conversions": [
                "weaverTree.treeID is empty -> resolve the single weaver tree entity of the catalog",
            ],
            "candidates": [
                {
                    "entity_id": row["entity_id"],
                    "entity_kind": "tree",
                    "canonical_name": self.catalog.name(row["entity_id"]),
                    "source_scope": str(row.get("source_tree_id")),
                    "source_id": str(row.get("source_tree_id")),
                    "mapping_method": "tree_kind=weaver",
                    "evidence": None,
                }
                for row in rows
            ],
        }
        if len(rows) == 1:
            tree = rows[0]
            result.update(
                status=RESOLVED,
                entity={
                    "entity_id": tree["entity_id"],
                    "kind": "tree",
                    "name": self.catalog.name(tree["entity_id"]),
                },
                source_tree_id=tree.get("source_tree_id"),
                tree=tree,
            )
        else:
            result.update(
                status=UNRESOLVED if not rows else AMBIGUOUS,
                entity=None,
                reason="expected exactly one weaver tree entity in the catalog",
            )
        return result

    def resolve_skill_tree(self, tree_id: str) -> dict:
        rows = self._lookup("skill_tree", "LESkillTrees", tree_id)
        result = self._resolve_rows(
            what="skill_tree",
            query={"kind": "skill_tree", "source_scope": "LESkillTrees", "source_id": tree_id},
            rows=rows,
        )
        if result["status"] == RESOLVED:
            tree = self.catalog.tree(result["entity"]["entity_id"])
            result["tree"] = tree
            owner = tree.get("owner_entity_id") if tree else None
            if owner:
                owner_entity = self.catalog.entity(owner)
                result["owner_skill"] = {
                    "entity_id": owner,
                    "kind": owner_entity.get("kind") if owner_entity else None,
                    "name": self.catalog.name(owner),
                }
            result["source_tree_id"] = tree.get("source_tree_id") if tree else None
        return result

    def resolve_ability(self, ability_id: str) -> dict:
        rows = self._lookup("ability", "LEAbilities.abilityList", ability_id)
        result = self._resolve_rows(
            what="ability",
            query={"kind": "ability", "source_scope": "LEAbilities.abilityList", "source_id": ability_id},
            rows=rows,
        )
        if result["status"] == RESOLVED:
            result["skill"] = self.catalog.skill(result["entity"]["entity_id"])
        return result

    # ------------------------------------------------------------------- nodes
    def resolve_node(self, *, kind: str, scope: str, source_id, tree_entity_id: str | None,
                     points=None, source_path: str | None = None) -> dict:
        rows = self._lookup(kind, scope, source_id)
        query = {"kind": kind, "source_scope": scope, "source_id": str(source_id)}
        result = self._resolve_rows(what="node", query=query, rows=rows)
        result["source_path"] = source_path
        result["points"] = points
        if result["status"] == RESOLVED:
            node = self.catalog.node(result["entity"]["entity_id"])
            result["node"] = node
            if node is None:
                result["status"] = UNRESOLVED
                result["entity"] = None
                result["reason"] = "source_id_map target has no nodes row"
                return result
            if tree_entity_id and node.get("tree_entity_id") != tree_entity_id:
                # The id resolves, but it belongs to another tree: never expose it as the
                # resolved entity.  Keep it only as a diagnostic candidate.
                result["status"] = AMBIGUOUS
                result["candidate_node"] = node
                result["entity"] = None
                result["node"] = None
                result["reason"] = (
                    f"node {node['entity_id']} belongs to tree {node['tree_entity_id']} "
                    f"not the selected tree {tree_entity_id}"
                )
        return result

    # ------------------------------------------------------------ items/affixes
    def decode_item(self, encoded_id: str, *, role: str, source_path: str) -> dict:
        decoded = letools_ids.decode_encoded_id(encoded_id)
        result = {
            "role": role,
            "source_path": source_path,
            "encoded_id": encoded_id,
            "codec": decoded,
            "status": decoded.get("status", "unsupported"),
            "item": None,
            "base": None,
            "affix": None,
            "conversions": [],
        }
        marker = decoded.get("marker")
        if decoded.get("status") != "decoded":
            return result
        if marker == "A":
            result = {**result, **self._resolve_affix(decoded)}
        elif marker == "U":
            result.update(self._resolve_unique(decoded))
        elif marker == "I":
            result.update(self._resolve_base(decoded))
        return result

    def _resolve_affix(self, decoded: dict) -> dict:
        affix_id = decoded["affix_id"]
        scopes = []
        for scope in ("affixList.singleAffixes", "affixList.multiAffixes"):
            rows = self._lookup("affix", scope, affix_id)
            if rows:
                scopes.append((scope, rows))
        conversions = [
            f"A codec payload {decoded['payload']!r} -> affix id {affix_id}",
        ]
        if len(scopes) == 1:
            scope, rows = scopes[0]
            if len(rows) == 1:
                row = rows[0]
                entity = self.catalog.entity(row["entity_id"])
                affix = self.catalog.affix(row["entity_id"])
                return {
                    "affix_status": RESOLVED,
                    "affix": {
                        "entity_id": row["entity_id"],
                        "kind": entity.get("kind") if entity else None,
                        "name": row.get("canonical_name"),
                        "source_scope": scope,
                        "source_id": str(affix_id),
                        "affix_slot": affix.get("affix_slot") if affix else None,
                        "definition": affix,
                    },
                    "affix_candidates": [_candidate(r) for r in rows],
                    "conversions": conversions,
                }
            return {
                "affix_status": AMBIGUOUS,
                "affix_candidates": [_candidate(r) for r in rows],
                "reason": f"{len(rows)} affix rows in scope {scope}",
                "conversions": conversions,
            }
        if not scopes:
            return {
                "affix_status": UNRESOLVED,
                "affix_candidates": [],
                "reason": "affix id not present in singleAffixes or multiAffixes",
                "conversions": conversions,
            }
        all_rows = [row for _, rows in scopes for row in rows]
        return {
            "affix_status": AMBIGUOUS,
            "affix_candidates": [_candidate(r) for r in all_rows],
            "reason": "affix id present in more than one affix scope",
            "conversions": conversions,
        }

    def _resolve_unique(self, decoded: dict) -> dict:
        unique_id = decoded["unique_id"]
        rows = self._lookup("unique", "uniqueList.uniques", unique_id)
        conversions = [
            f"U codec payload {decoded['payload']!r} -> unique id {unique_id}, base subtype {decoded['base_subtype_id']}",
        ]
        if len(rows) != 1:
            return {
                "definition_status": AMBIGUOUS if rows else UNRESOLVED,
                "definition": None,
                "definition_candidates": [_candidate(r) for r in rows],
                "status": AMBIGUOUS if rows else UNRESOLVED,
                "reason": "unique id did not resolve to exactly one catalog row",
                "conversions": conversions,
            }
        row = rows[0]
        entity_id = row["entity_id"]
        definition = self.catalog.unique_definition(entity_id)
        allowed_subtypes = definition.get("subTypeIds") or []
        base_type_id = definition.get("baseTypeId")
        base_status = UNRESOLVED
        base: dict | None = None
        base_candidates: list[dict] = []
        subtype_ok = decoded["base_subtype_id"] in allowed_subtypes
        if base_type_id is not None:
            for scope in (f"itemList.equippable:{base_type_id}", f"itemList.nonEquippable:{base_type_id}"):
                base_rows = self._lookup("item_base", scope, decoded["base_subtype_id"])
                if base_rows:
                    base_candidates.extend(base_rows)
                    if base is None:
                        base = base_rows[0]
                        base_status = RESOLVED if len(base_rows) == 1 and subtype_ok else AMBIGUOUS
                        if not subtype_ok:
                            base_status = AMBIGUOUS
        status = RESOLVED
        reasons: list[str] = []
        if not subtype_ok:
            status = AMBIGUOUS
            reasons.append(
                f"decoded base subtype {decoded['base_subtype_id']} is not among the unique's allowed subtypes {allowed_subtypes}"
            )
        if base_status != RESOLVED:
            status = AMBIGUOUS if status == RESOLVED else status
            reasons.append("base item could not be resolved uniquely from the unique definition")
        return {
            "status": status,
            "reason": "; ".join(reasons) if reasons else None,
            "definition_status": RESOLVED,
            "definition": {
                "entity_id": entity_id,
                "name": row.get("canonical_name"),
                "source_scope": row.get("source_scope"),
                "source_id": str(unique_id),
                "unique_id": definition.get("uniqueId", unique_id),
                "base_type_id": base_type_id,
                "allowed_subtypes": allowed_subtypes,
                "is_set_item": definition.get("isSetItem"),
            },
            "definition_candidates": [_candidate(r) for r in rows],
            "base_status": base_status,
            "base": {
                "entity_id": base["entity_id"],
                "name": base.get("canonical_name"),
                "source_scope": base.get("source_scope"),
                "source_id": str(decoded["base_subtype_id"]),
            }
            if base
            else None,
            "base_candidates": [_candidate(r) for r in base_candidates],
            "conversions": conversions,
        }

    def _resolve_base(self, decoded: dict) -> dict:
        type_index = decoded["item_type_index"]
        base_id = decoded["base_id"]
        rows: list[dict] = []
        scopes: list[str] = []
        for scope in (f"itemList.equippable:{type_index}", f"itemList.nonEquippable:{type_index}"):
            found = self._lookup("item_base", scope, base_id)
            if found:
                rows.extend(found)
                scopes.append(scope)
        conversions = [
            f"I codec payload {decoded['payload']!r} -> item-type index {type_index}, base id {base_id}, tail {decoded['tail']!r}",
        ]
        if len(rows) == 1:
            row = rows[0]
            return {
                "status": RESOLVED,
                "definition": None,
                "definition_candidates": [],
                "base_status": RESOLVED,
                "base": {
                    "entity_id": row["entity_id"],
                    "name": row.get("canonical_name"),
                    "source_scope": scopes[0],
                    "source_id": str(base_id),
                },
                "base_candidates": [_candidate(r) for r in rows],
                "tail": decoded["tail"],
                "conversions": conversions,
            }
        return {
            "status": AMBIGUOUS if rows else UNRESOLVED,
            "definition": None,
            "base": None,
            "base_status": AMBIGUOUS if len(rows) > 1 else UNRESOLVED,
            "base_candidates": [_candidate(r) for r in rows],
            "reason": f"{len(rows)} base-item candidate rows across scopes {scopes}",
            "tail": decoded["tail"],
            "conversions": conversions,
        }

    # -------------------------------------------------------------- node tiers
    def resolve_tier(self, affix_entity_id: str, source_tier) -> dict:
        """Map a source affix tier (1-based) to the catalog tier (observed: tier-1)."""
        try:
            source_tier_int = int(source_tier)
        except (TypeError, ValueError):
            return {
                "status": UNRESOLVED,
                "query": {"affix_entity_id": affix_entity_id, "source_tier": source_tier},
                "reason": "source tier is not an integer",
            }
        tiers = self.catalog.affix_tiers(affix_entity_id)
        tier_index = source_tier_int - 1
        match = next((tier for tier in tiers if tier["tier_index"] == tier_index), None)
        result = {
            "query": {"affix_entity_id": affix_entity_id, "source_tier": source_tier_int},
            "conversions": [f"source tier {source_tier_int} -> tier_index {tier_index} (source tier is 1-based)"],
            "tier_index": tier_index,
            "tier": match,
            "available_tiers": len(tiers),
        }
        if match is None:
            result.update(
                status=UNRESOLVED,
                reason=f"catalog affix has no tier_index {tier_index} (affix has {len(tiers)} tiers)",
            )
        else:
            result["status"] = RESOLVED
        return result
