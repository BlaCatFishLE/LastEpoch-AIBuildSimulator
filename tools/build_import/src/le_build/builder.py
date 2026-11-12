"""BuildDraft -> BuildSpec v1 construction.

The builder is intentionally mechanical: it walks the preserved source payload, asks the
resolver for identities, restores values (when the roll module is available) and records
every unresolved/ambiguous/absent fact as an issue instead of guessing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from . import BUILD_SCHEMA_VERSION, MAPPING_VERSION, PARSER_VERSION, readable
from .catalog import Catalog
from .codecs import letools_ids
from .paths import canonical_json_bytes
from .resolve import AMBIGUOUS, RESOLVED, UNRESOLVED, Resolver

EQUIPMENT_SLOTS = (
    "head",
    "chest",
    "weapon1",
    "weapon2",
    "hands",
    "waist",
    "feet",
    "ring1",
    "ring2",
    "amulet",
    "relic",
    "idol_altar",
)
AFFIX_CHANNELS = (
    ("normal", "affixes"),
    ("sealed", "sealedAffix"),
    ("primordial", "primordialAffix"),
    ("corrupted", "corruptedAffix"),
)
# 2026-11-03 user scope: the weaver tree and its contents are excluded as one feature.
EXCLUDED_WEAVER_FIELDS = (
    "data.weaverTree",
    "data.weaverTreeContainers",
    "data.weaverTreeProgression",
)
BUILD_SCOPE_VERSION = "letools-build-scope-v2"


class IncompatibleBuildError(RuntimeError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class SchemaError(IncompatibleBuildError):
    """The source payload does not satisfy the supported protocol shape."""


def _json_loads(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _instance_display_name(instance: dict) -> str | None:
    definition = (instance.get("definition") or {}).get("ref") or {}
    base = (instance.get("base") or {}).get("ref") or {}
    return definition.get("name") or base.get("name")


def _data_field_key(field_path: str) -> str:
    return field_path.split("data.", 1)[1] if field_path.startswith("data.") else field_path


class IssueLog:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self._seen: set = set()

    def add(self, code: str, severity: str, path: str, message: str, **detail) -> dict:
        key = (code, path)
        if key in self._seen:
            for issue in self.issues:
                if issue["code"] == code and issue["path"] == path:
                    issue.setdefault("occurrences", 1)
                    return issue
        issue = {
            "code": code,
            "severity": severity,
            "path": path,
            "message": message,
        }
        if detail:
            issue["detail"] = {key: value for key, value in detail.items() if value is not None}
        self.issues.append(issue)
        self._seen.add(key)
        return issue

    def count(self, severity: str | None = None) -> int:
        if severity is None:
            return len(self.issues)
        return sum(1 for issue in self.issues if issue["severity"] == severity)

    def codes(self) -> dict:
        result: dict = {}
        for issue in self.issues:
            result[issue["code"]] = result.get(issue["code"], 0) + 1
        return result


class BuildSpecBuilder:
    def __init__(self, catalog: Catalog, resolver: Resolver, *, restorer=None, input_index=None):
        self.catalog = catalog
        self.resolver = resolver
        self.restorer = restorer
        self.input_index = input_index
        self.issues = IssueLog()

    # ------------------------------------------------------------------ helpers
    def _entity_ref(self, entity_id: str | None, *, status: str = RESOLVED) -> dict | None:
        if not entity_id:
            return None
        entity = self.catalog.entity(entity_id)
        return {
            "entity_id": entity_id,
            "kind": entity.get("kind") if entity else None,
            "name": self.catalog.name(entity_id),
            "status": status,
        }

    def _resolution_issues(self, resolution: dict, path: str) -> None:
        status = resolution.get("status")
        if status == RESOLVED:
            return
        code = "identity_unresolved" if status == UNRESOLVED else "identity_ambiguous"
        severity = "error" if status == UNRESOLVED else "warning"
        self.issues.add(
            code,
            severity,
            path,
            resolution.get("reason") or f"{resolution.get('what')} reference could not be resolved",
            query=resolution.get("query"),
            candidates=resolution.get("candidates"),
            conversions=resolution.get("conversions"),
        )

    # --------------------------------------------------------------- character
    def _build_character(self, data: dict) -> dict:
        bio = data.get("bio")
        if not isinstance(bio, dict):
            raise SchemaError("data.bio is missing or not an object", details={"path": "data.bio"})
        level = bio.get("level")
        if not isinstance(level, int) or level < 1 or level > 100:
            self.issues.add(
                "schema_level_invalid", "warning", "data.bio.level", f"unexpected character level {level!r}"
            )
        class_resolution = self.resolver.resolve_class(bio.get("characterClass"))
        self._resolution_issues(class_resolution, "data.bio.characterClass")
        class_entity = class_resolution.get("entity") or {}
        mastery_resolution = self.resolver.resolve_mastery(
            bio.get("characterClass"), bio.get("chosenMastery"), class_resolution
        )
        self._resolution_issues(mastery_resolution, "data.bio.chosenMastery")
        mastery_entity = mastery_resolution.get("entity") or {}
        mastery_definition = (
            self.catalog.entity(mastery_entity.get("entity_id")) if mastery_entity.get("entity_id") else None
        )
        mastery_index = None
        if mastery_definition:
            mastery_index = mastery_definition.get("source_mastery_id")
        characters = {
            "level": level,
            "class": {
                "source_value": bio.get("characterClass"),
                "source_path": "data.bio.characterClass",
                "status": class_resolution.get("status"),
                "ref": class_resolution.get("entity"),
                "character_class_definition": {
                    "class_id": (class_resolution.get("character_class") or {}).get("classID"),
                    "tree_id": (class_resolution.get("character_class") or {}).get("treeID"),
                }
                if class_resolution.get("status") == RESOLVED
                else None,
                "resolution": class_resolution,
            },
            "mastery": {
                "source_value": bio.get("chosenMastery"),
                "source_path": "data.bio.chosenMastery",
                "status": mastery_resolution.get("status"),
                "ref": mastery_resolution.get("entity"),
                "source_mastery_id": mastery_index,
                "resolution": mastery_resolution,
                "note": "the mastery index is only meaningful inside its class namespace",
            },
        }
        return characters

    # -------------------------------------------------------------------- items
    def _item_affix_instances(
        self, item_data: dict, *, source_path: str, instance_id: str
    ) -> tuple[dict, dict]:
        channel_map: dict[str, list[dict]] = {"normal": [], "sealed": [], "primordial": [], "corrupted": []}
        for channel, field in AFFIX_CHANNELS:
            raw_value = item_data.get(field)
            if raw_value is None:
                continue
            if field == "affixes":
                if not isinstance(raw_value, list):
                    self.issues.add(
                        "schema_affix_channel_type",
                        "warning",
                        f"{source_path}.{field}",
                        "affixes field is not a list",
                        actual=type(raw_value).__name__,
                    )
                    continue
                entries = list(raw_value)
            else:
                if not isinstance(raw_value, dict):
                    self.issues.add(
                        "schema_affix_channel_type",
                        "warning",
                        f"{source_path}.{field}",
                        f"{field} is not an object",
                        actual=type(raw_value).__name__,
                    )
                    continue
                entries = [raw_value]
            for index, raw_affix in enumerate(entries):
                entry_path = (
                    f"{source_path}.{field}.{index}" if field == "affixes" else f"{source_path}.{field}"
                )
                channel_map[channel].append(
                    self._affix_instance(
                        raw_affix,
                        channel=channel,
                        field=field,
                        index=index,
                        source_path=entry_path,
                        instance_id=f"{instance_id}.{channel}" + (f".{index}" if field == "affixes" else ""),
                    )
                )
        counts = {channel: len(entries) for channel, entries in channel_map.items()}
        return channel_map, counts

    def _affix_instance(
        self, raw_affix, *, channel: str, field: str, index: int, source_path: str, instance_id: str
    ) -> dict:
        base = {
            "instance_id": instance_id,
            "channel": channel,
            "original_field": field,
            "index": index,
            "source_path": source_path,
        }
        if not isinstance(raw_affix, dict):
            self.issues.add(
                "schema_affix_entry_type", "error", source_path, "affix entry is not an object"
            )
            return {**base, "status": "invalid", "raw": raw_affix}
        encoded_id = raw_affix.get("id")
        base["encoded_id"] = encoded_id
        base["source_tier"] = raw_affix.get("tier")
        base["raw_affix"] = raw_affix
        if "r" in raw_affix:
            roll_value = raw_affix.get("r")
            if roll_value is None:
                roll = {"raw": None, "status": "null", "source_path": f"{source_path}.r"}
            elif isinstance(roll_value, bool) or not isinstance(roll_value, int):
                roll = {"raw": roll_value, "status": "invalid", "source_path": f"{source_path}.r"}
                self.issues.add(
                    "schema_roll_type", "warning", f"{source_path}.r", "roll is not an integer", actual=roll_value
                )
            else:
                roll = {"raw": roll_value, "status": "present", "source_path": f"{source_path}.r"}
        else:
            roll = {"raw": None, "status": "absent", "source_path": f"{source_path}.r"}
        local_affix_input = self.input_index.affix_roll(source_path) if self.input_index is not None else None
        if local_affix_input is not None:
            roll = {
                "raw": int(local_affix_input["roll"]),
                "status": "local_input",
                "source_status": roll.get("status"),
                "source_path": f"{source_path}.r",
                "value_source": "local_input",
                "input_id": local_affix_input.get("input_id"),
                "note": local_affix_input.get("note"),
            }
        base["roll"] = roll

        decoded = letools_ids.decode_encoded_id(encoded_id, expected_marker="A")
        base["codec"] = {
            key: decoded.get(key)
            for key in (
                "codec_version",
                "marker",
                "payload",
                "status",
                "reason",
                "affix_id",
                "pad_width",
            )
            if key in decoded
        }
        if decoded.get("status") != "decoded":
            self.issues.add(
                "codec_unsupported",
                "error",
                source_path,
                f"affix encoded id {encoded_id!r} did not decode",
                codec_status=decoded.get("status"),
                reason=decoded.get("reason"),
            )
            return {**base, "affix": None, "status": "unresolved"}

        affix_resolution = self.resolver._resolve_affix(decoded)
        affix_status = affix_resolution.get("affix_status")
        affix = affix_resolution.get("affix")
        base["affix"] = {
            "ref": {
                "entity_id": affix["entity_id"],
                "kind": affix.get("kind"),
                "name": affix.get("name"),
            }
            if affix
            else None,
            "source_scope": affix.get("source_scope") if affix else None,
            "source_id": affix.get("source_id") if affix else None,
            "affix_slot": affix.get("affix_slot") if affix else None,
            "definition": {
                key: value
                for key, value in (affix.get("definition") or {}).items()
                if key in ("affix_type", "affix_title", "affix_slot", "source_affix_id")
            }
            if affix
            else None,
            "status": affix_status,
            "candidates": affix_resolution.get("affix_candidates"),
            "reason": affix_resolution.get("reason"),
        }
        if affix_status != RESOLVED:
            code = "identity_unresolved" if affix_status == UNRESOLVED else "identity_ambiguous"
            self.issues.add(
                code,
                "error" if affix_status == UNRESOLVED else "warning",
                source_path,
                affix_resolution.get("reason") or "affix reference did not resolve",
                encoded_id=encoded_id,
                candidates=affix_resolution.get("affix_candidates"),
            )

        tier_result = None
        if affix_status == RESOLVED:
            expected_payload = letools_ids.expected_payload_for_affix(int(affix["source_id"]))
            actual_payload = decoded.get("payload")
            reverse_ok = actual_payload is not None and int(actual_payload) == int(expected_payload)
            base["codec_reverse_check"] = {
                "expected_payload": expected_payload,
                "actual_payload": actual_payload,
                "status": "pass" if reverse_ok else "conflict",
            }
            if not reverse_ok:
                self.issues.add(
                    "codec_mapping_conflict",
                    "error",
                    source_path,
                    f"decoded payload {actual_payload!r} does not reproduce the catalog affix id {expected_payload!r}",
                )
            tier_result = self.resolver.resolve_tier(affix["entity_id"], raw_affix.get("tier"))
            base["tier"] = {
                "source_tier": tier_result.get("query", {}).get("source_tier"),
                "tier_index": tier_result.get("tier_index"),
                "status": tier_result.get("status"),
                "roll_min": (tier_result.get("tier") or {}).get("roll_min"),
                "roll_max": (tier_result.get("tier") or {}).get("roll_max"),
                "available_tiers": tier_result.get("available_tiers"),
                "reason": tier_result.get("reason"),
            }
            if tier_result.get("status") != RESOLVED:
                self.issues.add(
                    "tier_unresolved",
                    "error",
                    source_path,
                    tier_result.get("reason") or "affix tier did not resolve",
                    encoded_id=encoded_id,
                    source_tier=raw_affix.get("tier"),
                )
        else:
            base["tier"] = {"source_tier": raw_affix.get("tier"), "status": "not_run", "reason": "affix unresolved"}

        value = None
        if self.restorer and affix_status == RESOLVED and tier_result and tier_result.get("status") == RESOLVED:
            try:
                # Distinguish "source field missing" (absent) from "explicit null" by
                # passing the rolls module sentinel; never convert absent to 0/None.
                missing_marker = getattr(self.restorer, "MISSING_ROLL", None)
                if roll["status"] == "absent" and missing_marker is not None:
                    raw_roll_argument = missing_marker
                else:
                    raw_roll_argument = roll.get("raw")
                value = self.restorer.restore_affix_value(
                    affix["entity_id"], tier_result["tier_index"], raw_roll_argument, self.catalog
                )
            except Exception as exc:  # pragma: no cover - defensive; report instead of crash
                self.issues.add(
                    "value_restore_error", "warning", source_path, f"roll restoration raised: {exc}"
                )
        if local_affix_input is not None and value is not None:
            value["value_source"] = "local_input"
            value["input_id"] = local_affix_input.get("input_id")
            for value_entry in value.get("values") or []:
                value_entry["value_source"] = "local_input"
                value_entry["input_id"] = local_affix_input.get("input_id")
        explicit_affix_applied = 0
        if self.input_index is not None and value is not None:
            for value_index, override in sorted(self.input_index.explicit_affix(source_path).items()):
                value_entries = value.get("values") or []
                if value_index >= len(value_entries):
                    continue
                target_entry = value_entries[value_index]
                numeric = float(override["value_numeric"])
                display = None
                if self.restorer is not None:
                    stat_lookup = getattr(self.catalog, "stat", None)
                    stat_obj = (
                        stat_lookup(target_entry.get("stat_id"))
                        if stat_lookup and target_entry.get("stat_id")
                        else None
                    )
                    display = self.restorer.display_value(
                        numeric, stat_obj, int(target_entry.get("modifier_type") or 0)
                    )
                target_entry.update(
                    {
                        "value_numeric": numeric,
                        "display_value": display,
                        "value_source": "local_value_override",
                        "input_id": override.get("input_id"),
                        "status": RESOLVED,
                    }
                )
                self.input_index.mark_applied(override)
                explicit_affix_applied += 1
            if explicit_affix_applied:
                value["status"] = (
                    RESOLVED
                    if all(
                        (entry or {}).get("value_numeric") is not None
                        for entry in value.get("values") or []
                    )
                    else value.get("status")
                )
                # Do NOT set block-level value_source: sibling values that were not
                # overridden must keep value_source="source" (calc export reads it).
                value["local_value_overrides"] = explicit_affix_applied
        base["value"] = value
        if value is not None and value.get("status") not in (RESOLVED, "partial"):
            self.issues.add(
                "value_unresolved",
                "warning",
                source_path,
                value.get("reason") or "value could not be restored",
            )
        base["status"] = "resolved" if (
            affix_status == RESOLVED
            and tier_result
            and tier_result.get("status") == RESOLVED
            and (value is None or value.get("status") in (RESOLVED, "partial"))
        ) else "partial"
        if roll["status"] in ("absent", "null") and affix_status == RESOLVED:
            self.issues.add(
                "roll_absent",
                "info",
                f"{source_path}.r",
                "source payload did not provide a roll value for this affix instance",
                roll_status=roll["status"],
                encoded_id=encoded_id,
                affix=affix.get("name") if affix else None,
            )
        return base

    def _implicit_block(
        self, definition_entity_id: str | None, item_data: dict, source_path: str, *, role: str = "equipment"
    ) -> dict:
        ir_raw = item_data.get("ir")
        if ir_raw is None:
            status = "absent"
            values = None
        elif not isinstance(ir_raw, list):
            status = "invalid"
            values = ir_raw
            self.issues.add(
                "schema_implicit_roll_type", "warning", f"{source_path}.ir", "ir is not a list"
            )
        else:
            status = "present"
            values = list(ir_raw)
        definition = (
            self.catalog.item_definition(definition_entity_id) if definition_entity_id else {}
        )
        implicits = definition.get("implicits") or []
        provenance = definition.get("implicits_provenance") or {"source": "unavailable"}
        definition_kind = definition.get("kind")
        definition_available = provenance.get("source") not in (None, "unavailable")
        values_for_restore = values
        unused_ir = None
        if values is not None and definition_available and len(values) > len(implicits):
            # The source `ir` array is fixed-width (observed length 3); only the first
            # len(implicits) entries are meaningful.  Extra entries are preserved but unused.
            values_for_restore = values[: len(implicits)]
            unused_ir = values[len(implicits):]
        local_implicits: dict[int, dict] = {}
        if self.input_index is not None:
            for implicit_index, entry in self.input_index.implicit_rolls(source_path).items():
                if definition_available and 0 <= implicit_index < len(implicits):
                    local_implicits[implicit_index] = entry
        if local_implicits:
            base_values = (
                list(values_for_restore)
                if isinstance(values_for_restore, list)
                else [None] * len(implicits)
            )
            for implicit_index, entry in sorted(local_implicits.items()):
                while len(base_values) <= implicit_index:
                    base_values.append(None)
                base_values[implicit_index] = int(entry["roll"])
            values_for_restore = base_values
        block = {
            "source_path": f"{source_path}.ir",
            "ir": values,
            "status": status,
            "implicit_count": len(implicits),
            "definition_kind": definition_kind,
            "definition_provenance": provenance,
            "ir_width": len(values) if values is not None else None,
            "used_ir_values": values_for_restore,
            "unused_ir_values": unused_ir,
            "implicit_alignment": (
                "definition_unavailable" if values is not None and not definition_available else
                ("aligned" if values is not None and len(values) == len(implicits) else
                 ("present_with_unused_tail" if values is not None and len(values) > len(implicits) else "mismatch"))
            ),
        }
        if local_implicits:
            block["source_status"] = "absent" if ir_raw is None else block.get("status")
            block["local_implicit_rolls"] = {
                str(index): {
                    "roll": int(entry["roll"]),
                    "input_id": entry.get("input_id"),
                    "note": entry.get("note"),
                }
                for index, entry in sorted(local_implicits.items())
            }
        if unused_ir is not None:
            self.issues.add(
                "implicit_roll_tail_unused",
                "info",
                f"{source_path}.ir",
                f"ir has {len(values)} values but only the first {len(implicits)} map to implicits; "
                "the trailing values are preserved but not used",
                unused_values=unused_ir,
            )
        if values is not None and not definition_available:
            block["status"] = "definition_unavailable"
            self.issues.add(
                "implicit_definition_unavailable",
                "warning",
                f"{source_path}.ir",
                "source provides implicit rolls but neither the normalized catalog nor the archived "
                f"item_base record has an implicit definition for this {definition_kind or 'unknown'} entity",
                entity_id=definition_entity_id,
                role=role,
            )
        elif values is not None and len(values) < len(implicits):
            self.issues.add(
                "implicit_roll_count_mismatch",
                "warning",
                f"{source_path}.ir",
                f"ir has {len(values)} values but the definition has {len(implicits)} implicits",
            )
        if self.restorer and definition_entity_id and values_for_restore and definition_available:
            try:
                block["values"] = self.restorer.restore_implicit_values(
                    definition_entity_id, values_for_restore, self.catalog
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.issues.add("value_restore_error", "warning", f"{source_path}.ir", f"implicit restoration raised: {exc}")
        else:
            block["values"] = None
        if block.get("values"):
            for implicit_index, entry in sorted(local_implicits.items()):
                if implicit_index >= len(block["values"]):
                    continue
                result = block["values"][implicit_index]
                result["roll_status"] = "local_input"
                result["value_source"] = "local_input"
                result["input_id"] = entry.get("input_id")
                value_entry = (result.get("values") or [None])[0]
                if value_entry is not None:
                    value_entry["value_source"] = "local_input"
                    value_entry["input_id"] = entry.get("input_id")
            for implicit_index, override in sorted(
                self.input_index.explicit_implicit(source_path).items()
                if self.input_index is not None
                else []
            ):
                if implicit_index >= len(block["values"]):
                    continue
                result = block["values"][implicit_index]
                value_entry = (result.get("values") or [None])[0]
                if value_entry is None:
                    continue
                numeric = float(override["value_numeric"])
                display = None
                if self.restorer is not None:
                    stat_lookup = getattr(self.catalog, "stat", None)
                    stat_obj = (
                        stat_lookup(value_entry.get("stat_id"))
                        if stat_lookup and value_entry.get("stat_id")
                        else None
                    )
                    display = self.restorer.display_value(
                        numeric, stat_obj, int(value_entry.get("modifier_type") or 0)
                    )
                value_entry.update(
                    {
                        "value_numeric": numeric,
                        "display_value": display,
                        "value_source": "local_value_override",
                        "input_id": override.get("input_id"),
                        "status": RESOLVED,
                    }
                )
                result["status"] = RESOLVED
                result["value_source"] = "local_value_override"
                result["input_id"] = override.get("input_id")
                self.input_index.mark_applied(override)
            if local_implicits and block.get("status") in ("absent", "present"):
                block["source_status"] = block.get("status")
                block["status"] = "local_inputs_applied"
        return block

    def _unique_roll_block(self, definition_entity_id: str | None, item_data: dict, source_path: str) -> dict:
        ur_raw = item_data.get("ur")
        if ur_raw is None:
            status = "absent"
            values = None
        elif not isinstance(ur_raw, list):
            status = "invalid"
            values = ur_raw
            self.issues.add("schema_unique_roll_type", "warning", f"{source_path}.ur", "ur is not a list")
        elif len(ur_raw) == 0:
            status = "empty"
            values = []
        else:
            status = "present"
            values = list(ur_raw)
        mods = self.catalog.unique_mods(definition_entity_id) if definition_entity_id else []
        roll_ids = sorted({mod.get("rollId") for mod in mods if mod.get("rollId") is not None})
        roll_id_set = {int(roll_id) for roll_id in roll_ids}
        local_rolls: dict[int, dict] = {}
        if self.input_index is not None:
            for roll_id, entry in self.input_index.unique_rolls(source_path).items():
                if roll_id in roll_id_set:
                    local_rolls[roll_id] = entry
        restore_values = values if isinstance(values, list) else []
        if local_rolls:
            restore_values = list(restore_values)
            for roll_id, entry in sorted(local_rolls.items()):
                while len(restore_values) <= roll_id:
                    restore_values.append(None)
                restore_values[roll_id] = int(entry["roll"])
            source_status = status
            status = "present_with_local_inputs" if source_status == "present" else "local_inputs_only"
        else:
            source_status = status
        block = {
            "source_path": f"{source_path}.ur",
            "ur": values,
            "status": status,
            "unique_mod_count": len(mods),
            "rollable_mod_count": sum(1 for mod in mods if mod.get("canRoll")),
            "roll_ids": roll_ids,
            "indexing_note": "ur is indexed by unique mod rollId (0-based in JSON); not by array position",
            "mods": [],
        }
        if local_rolls:
            block["source_status"] = source_status
            block["effective_ur"] = restore_values
            block["local_rolls"] = {
                str(roll_id): {
                    "roll": int(entry["roll"]),
                    "input_id": entry.get("input_id"),
                    "note": entry.get("note"),
                }
                for roll_id, entry in sorted(local_rolls.items())
            }
            self.issues.add(
                "local_input_applied",
                "info",
                f"{source_path}.ur",
                f"{len(local_rolls)} unique roll slot(s) filled from local inputs",
                roll_ids=sorted(local_rolls),
            )
        if status in ("empty", "absent") and roll_ids:
            self.issues.add(
                "unique_rolls_absent_from_source",
                "warning",
                f"{source_path}.ur",
                f"unique definition has rollable mods but the source payload provides no ur values ({status}); "
                "definition ranges are still shown, actual roll values are reported as not provided",
                roll_ids=roll_ids,
            )
        elif status == "empty":
            self.issues.add(
                "unique_rolls_empty",
                "info",
                f"{source_path}.ur",
                "source payload provides an empty ur list and the definition has no rollable mods",
            )
        if values is not None and len(values) > 0 and roll_ids and max(roll_ids) >= len(values):
            self.issues.add(
                "unique_roll_index_out_of_range",
                "warning",
                f"{source_path}.ur",
                f"unique mod rollId {max(roll_ids)} exceeds ur length {len(values)}",
            )
        results = None
        if self.restorer and definition_entity_id:
            try:
                results = self.restorer.restore_unique_values(
                    definition_entity_id,
                    restore_values if isinstance(restore_values, list) else [],
                    self.catalog,
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.issues.add("value_restore_error", "warning", f"{source_path}.ur", f"unique restoration raised: {exc}")
        block["values"] = results
        for index, mod in enumerate(mods):
            if not isinstance(mod, dict):
                continue
            result = results[index] if results and index < len(results) else None
            entry = ((result or {}).get("values") or [None])[0]
            roll_id = mod.get("rollId")
            local_entry = local_rolls.get(int(roll_id)) if roll_id is not None and local_rolls else None
            if local_entry is not None and result is not None:
                result["roll_status"] = "local_input"
                result["value_source"] = "local_input"
                result["input_id"] = local_entry.get("input_id")
                if entry is not None:
                    entry["value_source"] = "local_input"
                    entry["input_id"] = local_entry.get("input_id")
            mod_record = {
                    "mod_index": index,
                    "roll_id": roll_id,
                    "can_roll": bool(mod.get("canRoll")),
                    "hidden": bool(mod.get("hideInTooltip")),
                    "raw_value": mod.get("value"),
                    "max_value": mod.get("maxValue"),
                    "roll_status": (result or {}).get("roll_status"),
                    "raw_roll": (result or {}).get("raw_roll"),
                    "restored": {
                        "status": (result or {}).get("status"),
                        "value_numeric": (entry or {}).get("value_numeric"),
                        "display_value": (entry or {}).get("display_value"),
                        "roll_min": (entry or {}).get("roll_min"),
                        "roll_max": (entry or {}).get("roll_max"),
                        "stat_name": (entry or {}).get("stat_name"),
                        "direction": (entry or {}).get("direction"),
                    },
            }
            value_source = (result or {}).get("value_source") or (entry or {}).get("value_source")
            input_id = (result or {}).get("input_id") or (entry or {}).get("input_id")
            if value_source and value_source != "source":
                mod_record["value_source"] = value_source
                mod_record["input_id"] = input_id
                mod_record["restored"]["value_source"] = value_source
                mod_record["restored"]["input_id"] = input_id
            block["mods"].append(mod_record)
        if self.input_index is not None:
            explicit_by_mod = self.input_index.explicit_unique(source_path)
            explicit_by_roll = self.input_index.explicit_unique_by_roll_id(source_path)
            explicit_targets: dict[int, dict] = {
                int(mod_index): override
                for mod_index, override in explicit_by_mod.items()
                if 0 <= int(mod_index) < len(mods)
            }
            for roll_id, override in explicit_by_roll.items():
                for mod_index, mod in enumerate(mods):
                    if mod.get("rollId") is None or int(mod["rollId"]) != int(roll_id):
                        continue
                    explicit_targets.setdefault(mod_index, override)
            for mod_index, override in sorted(explicit_targets.items()):
                result = results[mod_index] if results and mod_index < len(results) else None
                value_entry = ((result or {}).get("values") or [None])[0]
                if value_entry is None:
                    continue
                numeric = float(override["value_numeric"])
                display = None
                mod_definition = mods[mod_index] if mod_index < len(mods) else {}
                stat_obj = None
                if self.restorer is not None:
                    # ur=[] definition-only ranges do not carry a stat_id (stat resolution
                    # was impossible without a roll); resolve the stat from the mod's
                    # property/tags/specialTag so explicit values are formatted by unit.
                    stat_by_property = getattr(self.catalog, "stat_by_property", None)
                    if stat_by_property is not None and mod_definition.get("property") is not None:
                        stat_obj = stat_by_property(
                            int(mod_definition["property"]),
                            tags=int(mod_definition.get("tags") or 0),
                            special_tag=int(mod_definition.get("specialTag") or 0),
                        )
                    display = self.restorer.display_value(
                        numeric,
                        stat_obj,
                        int(mod_definition.get("type") or value_entry.get("modifier_type") or 0),
                    )
                if stat_obj:
                    # The unrolled definition entries carry explicit nulls for stat fields;
                    # fill them in (do not rely on setdefault, the keys already exist).
                    if value_entry.get("stat_id") is None:
                        value_entry["stat_id"] = stat_obj.get("stat_id")
                    if value_entry.get("stat_name") is None:
                        value_entry["stat_name"] = stat_obj.get("name")
                    if value_entry.get("unit") is None:
                        value_entry["unit"] = stat_obj.get("unit")
                    value_entry["stat_resolution"] = "propertyList"
                    value_entry["property"] = mod_definition.get("property")
                    if value_entry.get("tags") is None:
                        value_entry["tags"] = mod_definition.get("tags")
                    if value_entry.get("special_tag") is None:
                        value_entry["special_tag"] = mod_definition.get("specialTag")
                    if value_entry.get("modifier_type") is None:
                        value_entry["modifier_type"] = int(mod_definition.get("type") or 0)
                value_entry.update(
                    {
                        "value_numeric": numeric,
                        "display_value": display,
                        "value_source": "local_value_override",
                        "input_id": override.get("input_id"),
                        "status": RESOLVED,
                    }
                )
                result["status"] = RESOLVED
                result["value_source"] = "local_value_override"
                result["input_id"] = override.get("input_id")
                block["mods"][mod_index]["roll_status"] = result.get("roll_status")
                block["mods"][mod_index]["value_source"] = "local_value_override"
                block["mods"][mod_index]["input_id"] = override.get("input_id")
                block["mods"][mod_index]["restored"].update(
                    {
                        "status": RESOLVED,
                        "value_numeric": numeric,
                        "display_value": display,
                        "stat_name": value_entry.get("stat_name"),
                        "direction": value_entry.get("direction"),
                        "roll_min": value_entry.get("roll_min"),
                        "roll_max": value_entry.get("roll_max"),
                        "value_source": "local_value_override",
                        "input_id": override.get("input_id"),
                    }
                )
                block["mods"][mod_index]["value_source"] = "local_value_override"
                block["mods"][mod_index]["input_id"] = override.get("input_id")
                block["local_value_overrides"] = int(block.get("local_value_overrides") or 0) + 1
                self.input_index.mark_applied(override)
        return block

    def _item_instance(
        self,
        item_data,
        *,
        role: str,
        instance_id: str,
        source_path: str,
        slot: str | None = None,
        position: dict | None = None,
    ) -> dict:
        instance = {
            "instance_id": instance_id,
            "role": role,
            "slot": slot,
            "position": position,
            "source_path": source_path,
        }
        if not isinstance(item_data, dict):
            self.issues.add("schema_item_type", "error", source_path, "item entry is not an object")
            instance.update(status="invalid", raw=item_data)
            return instance
        encoded_id = item_data.get("id")
        instance["encoded_id"] = encoded_id
        decoded = letools_ids.decode_encoded_id(encoded_id)
        instance["codec"] = {
            key: decoded.get(key)
            for key in (
                "codec_version",
                "encoded_id",
                "marker",
                "payload",
                "status",
                "reason",
                "format_marker",
                "item_type_index",
                "base_id",
                "base_subtype_id",
                "unique_id",
                "tail",
            )
            if key in decoded
        }
        if decoded.get("status") != "decoded":
            self.issues.add(
                "codec_unsupported",
                "error",
                source_path,
                f"item encoded id {encoded_id!r} did not decode",
                codec_status=decoded.get("status"),
                reason=decoded.get("reason"),
            )
            instance.update(status="unresolved", definition=None, base=None)
            return instance

        resolution = self.resolver.decode_item(encoded_id, role=role, source_path=source_path)
        marker = decoded["marker"]
        instance["codec_class"] = {"A": "affix", "U": "unique", "I": "base"}.get(marker)
        definition = resolution.get("definition")
        base = resolution.get("base")
        request_status = resolution.get("status")
        if marker == "U":
            instance["rarity"] = "unique"
            instance["definition"] = (
                {
                    **definition,
                    "ref": {"entity_id": definition["entity_id"], "kind": "item", "name": definition.get("name")},
                }
                if definition
                else None
            )
            instance["base"] = (
                {**base, "ref": {"entity_id": base["entity_id"], "kind": "item", "name": base.get("name")}}
                if base
                else None
            )
            instance["definition_status"] = resolution.get("definition_status")
            instance["base_status"] = resolution.get("base_status")
            if resolution.get("status") != RESOLVED:
                self.issues.add(
                    "identity_ambiguous" if resolution.get("status") == AMBIGUOUS else "identity_unresolved",
                    "warning",
                    source_path,
                    resolution.get("reason") or "unique item identity incomplete",
                    encoded_id=encoded_id,
                    candidates=resolution.get("definition_candidates"),
                )
        elif marker == "I":
            instance["rarity"] = "base"
            instance["definition"] = None
            instance["definition_status"] = "not_applicable"
            instance["base"] = (
                {**base, "ref": {"entity_id": base["entity_id"], "kind": "item", "name": base.get("name")}}
                if base
                else None
            )
            instance["base_status"] = resolution.get("base_status")
            if resolution.get("status") != RESOLVED:
                self.issues.add(
                    "identity_unresolved" if resolution.get("status") != AMBIGUOUS else "identity_ambiguous",
                    "error",
                    source_path,
                    resolution.get("reason") or "base item identity did not resolve",
                    encoded_id=encoded_id,
                    candidates=resolution.get("base_candidates"),
                )
            else:
                tail = decoded.get("tail")
                if tail and set(tail) != {"0"}:
                    self.issues.add(
                        "codec_i_tail_uninterpreted",
                        "info",
                        source_path,
                        f"I-codec tail {tail!r} is not interpreted by this importer version",
                        tail=tail,
                    )
        else:
            instance["rarity"] = "affix-coded"
            instance["definition"] = None
            instance["base"] = None
            self.issues.add(
                "item_marker_unexpected",
                "warning",
                source_path,
                f"item id used affix marker {marker!r}",
                encoded_id=encoded_id,
            )

        affix_channels, affix_counts = self._item_affix_instances(
            item_data, source_path=source_path, instance_id=instance_id
        )
        instance["affixes"] = affix_channels
        instance["affix_counts"] = affix_counts
        instance["affix_total"] = sum(affix_counts.values())

        # Reverse-check the codec: rebuild the payload from the catalog source keys and
        # compare it with what the source string actually decoded to.
        reverse_check = None
        if marker == "U" and instance.get("definition") and instance["definition"].get("ref"):
            definition = resolution.get("definition") or {}
            expected = letools_ids.expected_payload_for_unique(
                decoded["base_subtype_id"], decoded["unique_id"]
            )
            actual = decoded.get("payload")
            reverse_check = {
                "expected_payload": expected,
                "actual_payload": actual,
                "status": "pass" if actual == expected else "conflict",
                "basis": "unique definition uniqueId + decoded base subtype id",
            }
        elif marker == "I":
            expected = letools_ids.expected_payload_for_base(
                decoded["item_type_index"], decoded["base_id"]
            )
            actual = decoded.get("payload") or ""
            tail = decoded.get("tail") or ""
            prefix_ok = actual[:7] == expected[:7]
            reverse_check = {
                "expected_payload_prefix": expected[:7],
                "actual_payload": actual,
                "tail": tail,
                "status": "pass" if prefix_ok else "conflict",
                "basis": "I codec payload = 1 + item-type index + base id + uninterpreted tail",
            }
        instance["codec_reverse_check"] = reverse_check
        if reverse_check and reverse_check["status"] == "conflict":
            self.issues.add(
                "codec_mapping_conflict",
                "error",
                source_path,
                "decoded payload does not reproduce the catalog source keys",
                detail=reverse_check,
            )

        definition_entity_id = None
        if instance.get("definition") and instance["definition"].get("ref"):
            definition_entity_id = instance["definition"]["ref"]["entity_id"]
        base_entity_id = None
        if instance.get("base") and instance["base"].get("ref"):
            base_entity_id = instance["base"]["ref"]["entity_id"]
        implicit_source = base_entity_id or definition_entity_id
        instance["implicit_rolls"] = self._implicit_block(implicit_source, item_data, source_path, role=role)
        instance["unique_rolls"] = (
            self._unique_roll_block(definition_entity_id, item_data, source_path)
            if marker == "U"
            else {
                "status": "not_applicable",
                "ur": None,
                "values": None,
                "mods": [],
                "source_path": f"{source_path}.ur",
            }
        )
        instance["faction"] = item_data.get("faction")
        instance["faction_note"] = (
            "faction array preserved verbatim; no faction semantic is claimed in phase 2"
            if item_data.get("faction") is not None
            else None
        )
        known_fields = {"id", "affixes", "sealedAffix", "primordialAffix", "corruptedAffix", "ir", "ur", "faction", "x", "y"}
        instance["unrecognized_fields"] = {
            key: value for key, value in item_data.items() if key not in known_fields
        }
        if instance["unrecognized_fields"]:
            self.issues.add(
                "unexpected_source_field",
                "info",
                source_path,
                f"unrecognized item fields preserved: {sorted(instance['unrecognized_fields'])}",
            )
        problems = []
        if instance.get("definition_status") not in (None, "not_applicable", RESOLVED):
            problems.append("definition")
        if instance.get("base_status") not in (None, RESOLVED):
            problems.append("base")
        if any(entry.get("status") == "invalid" for entries in affix_channels.values() for entry in entries):
            problems.append("affix")
        instance["status"] = "resolved" if not problems else "partial"
        instance["counts_as_equipped"] = True
        try:
            instance["readable"] = readable.build_item_readable(instance, self.catalog)
        except Exception as exc:  # pragma: no cover - defensive: never lose the import
            instance["readable"] = {
                "version": readable.READABLE_VERSION,
                "display_name": _instance_display_name(instance),
                "lines": [],
                "warnings": [f"readable rendering failed: {exc}"],
                "completeness": "unavailable",
            }
            self.issues.add("readable_generation_failed", "warning", source_path, str(exc))
        if instance["readable"].get("completeness") == "partial":
            self.issues.add(
                "readable_partial",
                "info",
                source_path,
                "player-readable text is partial; see readable.warnings",
                warnings=instance["readable"].get("warnings")[:5],
            )
        return instance

    # ------------------------------------------------------------- passive tree
    def _node_entry(self, *, node_id, points, tree_entity_id, tree_scope, kind, source_path, node_kind: str) -> dict:
        try:
            points_int = int(points)
        except (TypeError, ValueError):
            points_int = None
            self.issues.add(
                "node_points_invalid", "error", source_path, f"node points {points!r} is not an integer"
            )
        explicit = points_int == 0
        resolution = self.resolver.resolve_node(
            kind=kind,
            scope=tree_scope,
            source_id=node_id,
            tree_entity_id=tree_entity_id,
            points=points_int,
            source_path=source_path,
        )
        self._resolution_issues(resolution, source_path)
        node = resolution.get("node") or {}
        entry = {
            "node_id": str(node_id),
            "source_path": source_path,
            "points": points_int,
            "explicit_zero": explicit,
            "status": resolution.get("status"),
            "ref": resolution.get("entity"),
            "node_kind": node_kind,
            "node_type": node.get("node_type"),
            "max_points": node.get("max_points"),
            "node_index": node.get("node_index"),
            "conversions": resolution.get("conversions"),
        }
        if resolution.get("status") == RESOLVED and points_int is not None:
            max_points = node.get("max_points")
            if max_points is not None:
                entry["points_within_max"] = points_int <= int(max_points)
                if points_int > int(max_points):
                    self.issues.add(
                        "node_points_exceed_max",
                        "warning",
                        source_path,
                        f"{points_int} points allocated but catalog max_points is {max_points}",
                        node_id=str(node_id),
                    )
            else:
                entry["points_within_max"] = None
            if points_int < 0:
                self.issues.add(
                    "node_points_negative", "error", source_path, f"negative point allocation {points_int}"
                )
        return entry

    def _build_passive_tree(self, data: dict, tree_resolution: dict) -> dict:
        char_tree = data.get("charTree") or {}
        tree = tree_resolution.get("tree") or {}
        selected = char_tree.get("selected") or {}
        nodes = []
        if isinstance(selected, dict):
            for node_id, points in selected.items():
                nodes.append(
                    self._node_entry(
                        node_id=node_id,
                        points=points,
                        tree_entity_id=tree.get("entity_id"),
                        tree_scope=tree_resolution.get("source_tree_id"),
                        kind="passive_node",
                        source_path=f"data.charTree.selected.{node_id}",
                        node_kind="passive",
                    )
                )
        else:
            self.issues.add(
                "schema_tree_selected_type", "error", "data.charTree.selected", "selected is not an object"
            )
        return {
            "tree": tree_resolution.get("entity"),
            "source_tree_id": char_tree.get("treeID", ""),
            "derived_source_tree_id": tree_resolution.get("source_tree_id"),
            "source_version": char_tree.get("version"),
            "catalog_tree_version": (tree.get("extensions") and _json_loads(tree.get("extensions"), {}).get("version")),
            "node_entries": len(nodes),
            "points_total": sum(entry["points"] for entry in nodes if isinstance(entry.get("points"), int)),
            "explicit_zero_entries": sum(1 for entry in nodes if entry.get("explicit_zero")),
            "nodes": nodes,
            "status": "resolved" if tree_resolution.get("status") == RESOLVED else "partial",
            "resolution": {key: tree_resolution.get(key) for key in ("status", "conversions", "reason", "candidates")},
        }

    def _build_specializations(self, data: dict) -> list[dict]:
        specializations = []
        skill_trees = data.get("skillTrees")
        if skill_trees is None:
            self.issues.add("schema_skill_trees_missing", "error", "data.skillTrees", "skillTrees field missing")
            return specializations
        if not isinstance(skill_trees, list):
            self.issues.add("schema_skill_trees_type", "error", "data.skillTrees", "skillTrees is not a list")
            return specializations
        for order, skill_tree in enumerate(skill_trees):
            path = f"data.skillTrees.{order}"
            if not isinstance(skill_tree, dict):
                self.issues.add("schema_skill_tree_type", "error", path, "skill tree entry is not an object")
                continue
            tree_id = skill_tree.get("treeID")
            resolution = self.resolver.resolve_skill_tree(tree_id)
            self._resolution_issues(resolution, f"{path}.treeID")
            tree = resolution.get("tree") or {}
            tree_entity_id = tree.get("entity_id")
            selected = skill_tree.get("selected") or {}
            nodes = []
            if isinstance(selected, dict):
                for node_id, points in selected.items():
                    nodes.append(
                        self._node_entry(
                            node_id=node_id,
                            points=points,
                            tree_entity_id=tree_entity_id,
                            tree_scope=resolution.get("source_tree_id"),
                            kind="skill_tree_node",
                            source_path=f"{path}.selected.{node_id}",
                            node_kind="skill_tree",
                        )
                    )
            else:
                self.issues.add(
                    "schema_tree_selected_type", "error", f"{path}.selected", "selected is not an object"
                )
            slot_number = skill_tree.get("slotNumber")
            if not isinstance(slot_number, int) or slot_number < 0:
                self.issues.add(
                    "schema_slot_number", "warning", path, f"unexpected slotNumber {slot_number!r}"
                )
            tree_extensions = _json_loads((tree or {}).get("extensions"), {})
            catalog_tree_version = tree_extensions.get("version")
            source_tree_version = skill_tree.get("version")
            tree_version_matches = (
                catalog_tree_version is None
                or source_tree_version is None
                or int(catalog_tree_version) == int(source_tree_version)
            )
            if not tree_version_matches:
                self.issues.add(
                    "tree_version_mismatch",
                    "warning",
                    f"{path}.version",
                    f"source tree version {source_tree_version} != catalog tree version {catalog_tree_version}; "
                    "the build is bound to the catalog version and no silent migration is performed",
                    tree_id=tree_id,
                )
            specializations.append(
                {
                    "slot_number": slot_number,
                    "source_level": skill_tree.get("level"),
                    "tree_version": skill_tree.get("version"),
                    "catalog_tree_version": catalog_tree_version,
                    "tree_version_matches": tree_version_matches,
                    "tree": resolution.get("entity"),
                    "tree_entity_id": tree_entity_id,
                    "tree_source_id": resolution.get("source_tree_id"),
                    "owner_skill": resolution.get("owner_skill"),
                    "node_entries": len(nodes),
                    "positive_node_entries": sum(
                        1 for entry in nodes if isinstance(entry.get("points"), int) and entry["points"] > 0
                    ),
                    "points_total": sum(entry["points"] for entry in nodes if isinstance(entry.get("points"), int)),
                    "explicit_zero_entries": sum(1 for entry in nodes if entry.get("explicit_zero")),
                    "nodes": nodes,
                    "status": "resolved" if resolution.get("status") == RESOLVED else "partial",
                    "resolution": {key: resolution.get(key) for key in ("status", "conversions", "reason", "candidates")},
                }
            )
        slot_numbers = [spec.get("slot_number") for spec in specializations if isinstance(spec.get("slot_number"), int)]
        duplicates = {slot for slot in slot_numbers if slot_numbers.count(slot) > 1}
        if duplicates:
            self.issues.add(
                "specialization_duplicate_slot",
                "warning",
                "data.skillTrees",
                f"duplicate specialization slot numbers: {sorted(duplicates)}",
            )
        expected = list(range(len(specializations)))
        if slot_numbers != expected:
            self.issues.add(
                "specialization_slot_sequence",
                "info",
                "data.skillTrees",
                f"specialization slot numbers are {slot_numbers}, expected 0..{len(specializations) - 1}",
            )
        return specializations

    def _build_action_bar(self, data: dict, specializations: list[dict]) -> dict:
        hud = data.get("hud")
        entries = []
        if hud is None:
            self.issues.add("schema_hud_missing", "warning", "data.hud", "hud field missing")
            hud = []
        tree_owner_to_slot = {}
        for spec in specializations:
            owner = spec.get("owner_skill") or {}
            if owner.get("entity_id"):
                tree_owner_to_slot[owner["entity_id"]] = spec.get("slot_number")
        if isinstance(hud, list):
            for index, ability_id in enumerate(hud):
                path = f"data.hud.{index}"
                resolution = self.resolver.resolve_ability(ability_id)
                self._resolution_issues(resolution, path)
                entity = resolution.get("entity") or {}
                entries.append(
                    {
                        "slot": index,
                        "source_id": ability_id,
                        "source_path": path,
                        "ref": resolution.get("entity"),
                        "specialization_slot": tree_owner_to_slot.get(entity.get("entity_id")),
                        "status": resolution.get("status"),
                        "resolution": {key: resolution.get(key) for key in ("status", "conversions", "reason", "candidates")},
                    }
                )
        else:
            self.issues.add("schema_hud_type", "error", "data.hud", "hud is not a list")
        return {
            "slots": entries,
            "source_ids": [entry["source_id"] for entry in entries],
            "note": "action bar order is independent from the specialization list and may contain unmastered abilities",
        }

    # ---------------------------------------------------------------- equipment
    def _build_equipment(self, data: dict) -> dict:
        equipment = data.get("equipment")
        if equipment is None:
            self.issues.add("schema_equipment_missing", "error", "data.equipment", "equipment field missing")
            equipment = {}
        if not isinstance(equipment, dict):
            self.issues.add("schema_equipment_type", "error", "data.equipment", "equipment is not an object")
            equipment = {}
        slots = []
        for slot, item_data in equipment.items():
            if slot not in EQUIPMENT_SLOTS:
                self.issues.add(
                    "unknown_equipment_slot",
                    "warning",
                    f"data.equipment.{slot}",
                    f"unrecognized equipment slot {slot!r} preserved as a sourced entry",
                    slot=slot,
                )
            instance = self._item_instance(
                item_data,
                role="equipment" if slot in EQUIPMENT_SLOTS else "equipment_unknown_slot",
                instance_id=f"equipment.{slot}",
                source_path=f"data.equipment.{slot}",
                slot=slot,
            )
            slots.append({"slot": slot, "instance": instance})
        present = [entry["slot"] for entry in slots]
        absent = [slot for slot in EQUIPMENT_SLOTS if slot not in present]
        for slot in absent:
            self.issues.add(
                "equipment_slot_absent",
                "info",
                f"data.equipment.{slot}",
                f"source payload has no {slot} item; no item is invented for it",
            )
        return {
            "slot_order": list(equipment.keys()),
            "slots": slots,
            "absent_slots": absent,
            "note": "absent slots are not filled; the build is imported exactly as shared",
        }

    def _build_idols(self, data: dict) -> dict:
        idols = data.get("idols")
        if idols is None:
            self.issues.add("schema_idols_missing", "warning", "data.idols", "idols field missing")
            idols = []
        if not isinstance(idols, list):
            self.issues.add("schema_idols_type", "error", "data.idols", "idols is not a list")
            idols = []
        entries = []
        for index, item_data in enumerate(idols):
            position = {"x": None, "y": None, "source_width": None, "source_height": None}
            if isinstance(item_data, dict):
                position["x"] = item_data.get("x")
                position["y"] = item_data.get("y")
            instance = self._item_instance(
                item_data,
                role="idol",
                instance_id=f"idols.{index}",
                source_path=f"data.idols.{index}",
                position=position,
            )
            entries.append({"index": index, "position": position, "instance": instance})
        grid_validation = {
            "status": "not_verified",
            "reason": "idol grid bounds/flip rules are not proven for this protocol version; x/y are preserved verbatim",
            "new_grid": data.get("newGrid"),
        }
        return {"new_grid": data.get("newGrid"), "entries": entries, "grid_validation": grid_validation}

    def _build_blessings(self, data: dict) -> dict:
        blessings = data.get("blessings")
        if blessings is None:
            self.issues.add("schema_blessings_missing", "warning", "data.blessings", "blessings field missing")
            blessings = {}
        if not isinstance(blessings, dict):
            self.issues.add("schema_blessings_type", "error", "data.blessings", "blessings is not an object")
            blessings = {}
        entries = []
        for timeline_key, item_data in blessings.items():
            instance = self._item_instance(
                item_data,
                role="blessing",
                instance_id=f"blessings.{timeline_key}",
                source_path=f"data.blessings.{timeline_key}",
                position={"timeline": timeline_key},
            )
            entries.append({"timeline": timeline_key, "instance": instance})
        return {
            "entries": entries,
            "note": "timeline slot keys are preserved verbatim and never reordered by name",
        }

    # ------------------------------------------------------- excluded: weaver tree
    def _excluded_weaver_section(self, data: dict) -> dict:
        """The weaver tree is excluded by user scope (2026-11-03).

        Nothing is parsed or resolved; the raw payload stays archived and the excluded
        source fields are declared so consumers do not mistake the absence for a loss.
        """
        present = [field for field in EXCLUDED_WEAVER_FIELDS if _data_field_key(field) in data]
        return {
            "status": "excluded_by_scope",
            "source_fields": list(EXCLUDED_WEAVER_FIELDS),
            "present_in_source": present,
            "raw_preserved": True,
            "note": (
                "user requirement 2026-11-03: build import excludes the weaver tree, its "
                "containers and its progression; the raw payload remains archived"
            ),
        }

    def _build_scope(self, data: dict) -> dict:
        excluded = [
            {
                "feature": "weaver_tree",
                "source_fields": list(EXCLUDED_WEAVER_FIELDS),
                "reason": (
                    "user requirement 2026-11-03: build import excludes the weaver tree "
                    "and its containers"
                ),
                "decision_date": "2026-11-03",
                "raw_preserved": True,
            }
        ]
        return {
            "scope_version": BUILD_SCOPE_VERSION,
            "included_sections": [
                "character",
                "passive_tree",
                "specializations",
                "action_bar",
                "equipment",
                "idols",
                "blessings",
                "context",
                "progression",
            ],
            "excluded_features": excluded,
            "notes": [
                "ordinary idols, blessings and the equipped idol_altar are still in scope",
                "historical revisions may still contain weaver data; they are read-only history",
            ],
        }

    # ------------------------------------------------------------------- context
    def _build_context(self, data: dict) -> dict:
        conditions = data.get("conditions")
        custom = data.get("customConditions")
        buff_settings = data.get("buffSettings")
        active_buffs = data.get("activeBuffs") or {}
        if not isinstance(active_buffs, dict):
            self.issues.add("schema_active_buffs_type", "warning", "data.activeBuffs", "activeBuffs is not an object")
            active_buffs = {}
        resolved_skills = []
        for index, ability_id in enumerate(active_buffs.get("skills") or []):
            path = f"data.activeBuffs.skills.{index}"
            resolution = self.resolver.resolve_ability(ability_id)
            self._resolution_issues(resolution, path)
            resolved_skills.append(
                {
                    "raw": ability_id,
                    "source_path": path,
                    "ref": resolution.get("entity"),
                    "status": resolution.get("status"),
                    "resolution": {key: resolution.get(key) for key in ("status", "conversions", "reason")},
                }
            )
        resolved_ailments = []
        resolved_buffs = []
        for field_name, value in (
            ("buffs", active_buffs.get("buffs")),
            ("ailments", active_buffs.get("ailments")),
        ):
            target = resolved_buffs if field_name == "buffs" else resolved_ailments
            for index, raw_id in enumerate(value or []):
                path = f"data.activeBuffs.{field_name}.{index}"
                kind = "buff" if field_name == "buffs" else "ailment"
                scope = "UIBuffs" if field_name == "buffs" else "LEAbilities.ailmentList"
                rows = self.resolver._lookup(kind, scope, raw_id)
                resolution = self.resolver._resolve_rows(
                    what=kind,
                    query={"kind": kind, "source_scope": scope, "source_id": str(raw_id)},
                    rows=rows,
                )
                self._resolution_issues(resolution, path)
                target.append({"raw": raw_id, "ref": resolution.get("entity"), "status": resolution.get("status")})
        result = {
            "conditions": {"raw": conditions, "source_path": "data.conditions", "status": "empty" if not conditions else "present"},
            "custom_conditions": {
                "raw": custom,
                "source_path": "data.customConditions",
                "status": "empty" if not custom else "present",
            },
            "buff_settings": {
                "raw": buff_settings,
                "source_path": "data.buffSettings",
                "status": "empty" if not buff_settings else "present",
            },
            "active_buffs": {
                "raw": {
                    "buffs": active_buffs.get("buffs") or [],
                    "ailments": active_buffs.get("ailments") or [],
                    "skills": active_buffs.get("skills") or [],
                },
                "source_path": "data.activeBuffs",
                "skills": resolved_skills,
                "buffs": resolved_buffs,
                "ailments": resolved_ailments,
                "note": (
                    "saved activation configuration only; it is not evidence that every related buff is active "
                    "or at maximum stacks"
                ),
            },
        }
        for key, raw in (("conditions", conditions), ("custom_conditions", custom), ("buff_settings", buff_settings)):
            if raw:
                self.issues.add(
                    "context_present_unparsed",
                    "warning",
                    f"data.{'customConditions' if key == 'custom_conditions' else key}",
                    "context configuration is preserved but not interpreted by this importer version",
                )
        return result

    def _build_progression(self, data: dict) -> dict:
        result = {
            "char_tree_progression": data.get("charTreeProgression") or [],
            "skill_trees_progression": data.get("skillTreesProgression") or [],
            "completed_quests": data.get("completedQuests") or [],
            "excluded_by_scope": {
                "weaver_tree_progression": {
                    "status": "excluded_by_scope",
                    "source_field": "data.weaverTreeProgression",
                    "raw_preserved": True,
                }
            },
            "note": "progression data is preserved for traceability and never used to override current allocations",
        }
        for key in ("char_tree_progression", "skill_trees_progression", "completed_quests"):
            if not result[key]:
                self.issues.add(
                    "progression_empty", "info", f"data.{key}", f"{key} is empty in this payload"
                )
        return result

    def _readable_summary(self, instances: list[dict]) -> dict:
        by_completeness: dict = {}
        line_status: dict = {}
        lines_without_text = 0
        local_input_lines = 0
        unique_composition = {"mods": 0, "descriptions": 0, "hidden_mods": 0}
        for instance in instances:
            block = instance.get("readable") or {}
            completeness = block.get("completeness") or "unavailable"
            by_completeness[completeness] = by_completeness.get(completeness, 0) + 1
            for line in block.get("lines") or []:
                status = line.get("value_status") or "unresolved"
                line_status[status] = line_status.get(status, 0) + 1
                if not line.get("text"):
                    lines_without_text += 1
                if str((line.get("diagnostics") or {}).get("value_source") or "").startswith("local_"):
                    local_input_lines += 1
            composition = block.get("unique_composition") or {}
            for key in unique_composition:
                unique_composition[key] += int(composition.get(key) or 0)
        summary = {
            "version": readable.READABLE_VERSION,
            "items": len(instances),
            "by_completeness": by_completeness,
            "line_value_status": line_status,
            "lines_without_text": lines_without_text,
            "unique_composition": unique_composition,
            "items_not_complete": [
                {
                    "instance_id": instance.get("instance_id"),
                    "display_name": (instance.get("readable") or {}).get("display_name"),
                    "completeness": (instance.get("readable") or {}).get("completeness"),
                    "warnings": (instance.get("readable") or {}).get("warnings") or [],
                }
                for instance in instances
                if (instance.get("readable") or {}).get("completeness") not in ("complete", None)
            ],
        }
        if local_input_lines:
            summary["local_input_lines"] = local_input_lines
        return summary

    # -------------------------------------------------------------------- build
    def build(
        self,
        *,
        draft: dict,
        fetch_meta: dict,
        catalog_compatibility: dict,
        previous_revision: dict | None = None,
    ) -> dict:
        data = draft.get("data") or {}
        document = draft.get("document") or {}
        source_fields_hash = hashlib.sha256(canonical_json_bytes(data)).hexdigest()
        character = self._build_character(data)
        class_entity = (character.get("class") or {}).get("ref") or {}
        passive_resolution = self.resolver.resolve_passive_tree(
            class_entity.get("entity_id") or "", class_entity.get("name")
        )
        self._resolution_issues(passive_resolution, "data.charTree")
        passive_tree = self._build_passive_tree(data, passive_resolution)
        specializations = self._build_specializations(data)
        action_bar = self._build_action_bar(data, specializations)
        equipment = self._build_equipment(data)
        idols = self._build_idols(data)
        blessings = self._build_blessings(data)
        context = self._build_context(data)
        progression = self._build_progression(data)
        scope = self._build_scope(data)
        weaver = self._excluded_weaver_section(data)
        all_in_scope_instances = (
            [entry["instance"] for entry in equipment.get("slots", [])]
            + [entry["instance"] for entry in idols.get("entries", [])]
            + [entry["instance"] for entry in blessings.get("entries", [])]
        )
        readable_summary = self._readable_summary(all_in_scope_instances)
        extensions = {
            "unexpected_top_level_fields": draft.get("unexpected_top_level_fields") or [],
            "unexpected_data_fields": draft.get("unexpected_data_fields") or [],
            "excluded_by_scope_source_fields": {
                field.split("data.", 1)[1]: {
                    "status": "excluded_by_scope",
                    "present_in_source": _data_field_key(field) in data,
                    "raw_preserved": True,
                }
                for field in EXCLUDED_WEAVER_FIELDS
            },
            "source_field_catalog": {
                "top_level": sorted(document.keys()),
                "data": sorted(data.keys()),
            },
            "raw_source": {
                "archive_path": fetch_meta.get("raw_path"),
                "raw_sha256": fetch_meta.get("raw_sha256"),
                "pointer_scheme": "source_path values are dot-separated JSON pointers into the archived raw payload, rooted at 'data'",
            },
            "uninterpreted_top_level": {
                key: document.get(key)
                for key in (draft.get("unexpected_top_level_fields") or [])
            },
            "uninterpreted_data": {
                key: data.get(key) for key in (draft.get("unexpected_data_fields") or [])
            },
        }
        if extensions["unexpected_top_level_fields"] or extensions["unexpected_data_fields"]:
            self.issues.add(
                "unexpected_source_field",
                "info",
                "data",
                f"unrecognized source fields preserved: {extensions['unexpected_top_level_fields'] + extensions['unexpected_data_fields']}",
            )
        if document.get("tags") is not None:
            extensions.setdefault("unrecognized_top_level_values", {})["tags"] = document.get("tags")
        for key in ("gh", "weaponTypes", "fromSaveFile", "newGrid", "guide"):
            if key in document:
                extensions.setdefault("protocol_values", {})[key] = document[key]
        for key in ("gh", "weaponTypes", "fromSaveFile"):
            if key in data:
                extensions.setdefault("protocol_values", {})[key] = data[key]
        top_level_consistency = {}
        for source_field, data_field in (("level", "level"), ("class", "characterClass"), ("mastery", "chosenMastery")):
            top_level_value = document.get(source_field)
            bio = data.get("bio") or {}
            data_value = bio.get(data_field)
            if source_field == "level":
                data_value = bio.get("level")
            elif source_field == "class":
                data_value = bio.get("characterClass")
            else:
                data_value = bio.get("chosenMastery")
            consistent = top_level_value == data_value
            top_level_consistency[source_field] = {
                "top_level": top_level_value,
                "data.bio": data_value,
                "consistent": consistent,
            }
            if not consistent:
                self.issues.add(
                    "protocol_field_inconsistent",
                    "warning",
                    f"data.bio.{data_field}",
                    f"top-level {source_field!r}={top_level_value!r} disagrees with data.bio={data_value!r}",
                )
        extensions["top_level_consistency"] = top_level_consistency
        if document.get("guide") is not None:
            self.issues.add(
                "guide_present", "info", "data.guide", "guide content preserved but not interpreted"
            )
        if data.get("weaponTypes") is not None:
            self.issues.add(
                "weapon_types_preserved",
                "info",
                "data.weaponTypes",
                "weaponTypes are preserved verbatim; no slot legality is claimed from them in phase 2",
            )
        statuses = self._statuses(
            equipment, idols, blessings, specializations, passive_tree, action_bar, context,
            readable_summary, catalog_compatibility=catalog_compatibility or {},
        )
        spec = {
            "build_spec_version": BUILD_SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "mapping_version": MAPPING_VERSION,
            "meta": {
                "build_id": f"letools:{draft.get('share_id')}",
                "share_id": draft.get("share_id"),
                "share_url": draft.get("requested_url"),
                "source": "letools",
                "source_endpoint": fetch_meta.get("api_url"),
                "protocol": {
                    "protocol_version": draft.get("protocol_version"),
                    "versions": draft.get("versions"),
                    "version_compatibility": catalog_compatibility,
                },
                "source_payload": {
                    "raw_sha256": fetch_meta.get("raw_sha256"),
                    "raw_path": fetch_meta.get("raw_path"),
                    "attempt_id": fetch_meta.get("attempt_id"),
                    "fetch_mode": fetch_meta.get("fetch_mode") or fetch_meta.get("mode"),
                    "fetched_at": fetch_meta.get("fetched_at"),
                    "data_field_sha256": source_fields_hash,
                },
                "codec": {
                    "codec_version": letools_ids.CODE_VERSION,
                    "evidence": letools_ids.EVIDENCE,
                },
                "created_at": _now(),
                "idempotency": {
                    "content_hash_scope": "all fields except meta.revision, meta.created_at, meta.import_attempt and statuses/coverage",
                },
                "previous_revision": previous_revision,
            },
            "catalog": {
                "dataset_id": self.catalog.dataset_id,
                "schema_version": self.catalog.meta().get("schema_version"),
                "target_game_version": self.catalog.meta().get("target_game_version"),
                "target_season": self.catalog.meta().get("target_season"),
                "source_snapshot_id": self.resolver.snapshot_id,
                "logical_content_sha256": self.catalog.fingerprint().get("logical_sha256"),
                "file_sha256": self.catalog.verification.get("file_sha256"),
                "database_path": str(self.catalog.db_path),
                "verification": self.catalog.verification,
                "compatibility": catalog_compatibility,
                "counts": self.catalog.counts(),
            },
            "character": character,
            "scope": scope,
            "passive_tree": passive_tree,
            "specializations": specializations,
            "action_bar": action_bar,
            "equipment": equipment,
            "idols": idols,
            "blessings": blessings,
            "weaver": weaver,
            "context": context,
            "progression": progression,
            "readable_summary": readable_summary,
            "extensions": extensions,
            "issues": self.issues.issues,
            "statuses": statuses,
        }
        return spec

    # ----------------------------------------------------------------- statuses
    def _statuses(
        self,
        equipment,
        idols,
        blessings,
        specializations,
        passive_tree,
        action_bar,
        context,
        readable_summary,
        catalog_compatibility: dict | None = None,
    ) -> dict:
        def identity_state(items_stats: dict) -> str:
            total = items_stats["total"]
            resolved = items_stats["resolved"]
            if total and resolved == total:
                return "resolved"
            if resolved:
                return "partial"
            return "failed"

        equipment_items = [entry["instance"] for entry in equipment["slots"]]
        idol_items = [entry["instance"] for entry in idols.get("entries", [])]
        blessing_items = [entry["instance"] for entry in blessings.get("entries", [])]
        identity = {
            "equipment": {
                "total": len(equipment_items),
                "resolved": sum(1 for item in equipment_items if item.get("status") == "resolved"),
            },
            "idols": {
                "total": len(idol_items),
                "resolved": sum(1 for item in idol_items if item.get("status") == "resolved"),
            },
            "blessings": {
                "total": len(blessing_items),
                "resolved": sum(1 for item in blessing_items if item.get("status") == "resolved"),
            },
            "specializations": {
                "total": len(specializations),
                "resolved": sum(1 for spec in specializations if spec.get("status") == "resolved"),
            },
            "passive_nodes": {
                "total": passive_tree.get("node_entries", 0),
                "resolved": sum(1 for node in passive_tree.get("nodes", []) if node.get("status") == RESOLVED),
            },
            "action_bar": {
                "total": len(action_bar.get("slots", [])),
                "resolved": sum(1 for slot in action_bar.get("slots", []) if slot.get("status") == RESOLVED),
            },
        }
        overall = [identity_state(stats) for stats in identity.values()]
        if all(state == "resolved" for state in overall):
            identity_status = "resolved"
        elif any(state in ("resolved", "partial") for state in overall):
            identity_status = "partial"
        else:
            identity_status = "failed"
        line_status = readable_summary.get("line_value_status") or {}
        unresolved_lines = int(line_status.get(readable.VALUE_STATUS_UNRESOLVED, 0))
        range_lines = int(line_status.get(readable.VALUE_STATUS_RANGE, 0))
        if self.restorer is None:
            value_status = "not_run"
        elif unresolved_lines:
            value_status = "partial"
        elif range_lines:
            value_status = "partial"
        else:
            value_status = "resolved"
        completeness = readable_summary.get("by_completeness") or {}
        if not readable_summary.get("items"):
            text_readiness = "unavailable"
        elif readable_summary.get("lines_without_text"):
            text_readiness = "partial"
        elif completeness.get("unavailable"):
            text_readiness = "partial"
        else:
            text_readiness = "complete"
        schema_errors = sum(
            1
            for issue in self.issues.issues
            if issue["code"].startswith("schema_") and issue["severity"] == "error"
        )
        schema_status = "invalid" if schema_errors else "supported"
        return {
            "fetch": "ok",
            "schema": schema_status,
            "catalog_compatibility": catalog_compatibility.get("status"),
            "catalog_binding": {
                "dataset_id": self.catalog.dataset_id,
                "verified": self.catalog.verification.get("verified"),
                "source_snapshot_id": self.resolver.snapshot_id,
            },
            "identity_resolution": identity_status,
            "identity_resolution_detail": identity,
            "value_resolution": value_status,
            "text_readiness": text_readiness,
            "readable_completeness": completeness,
            "calculation_readiness": "not_assessed",
            "scope": BUILD_SCOPE_VERSION,
        }
