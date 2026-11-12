"""Local (user-supplied) value inputs for a build.

The source payload does not always contain every roll: e.g. six affix instances in the
fixed sample have no ``r`` and two uniques have ``ur=[]``.  A player can read those
values in game; this module stores the typed values in a small, versioned local file and
turns them into build provenance on the next sync.

Design rules:

* the archived raw response is never modified and published revisions stay immutable;
  inputs only take effect through a **new** revision (``meta.local_inputs`` records the
  file hash / entries applied);
* every applied value keeps ``value_source`` = ``local_input`` (roll input) or
  ``local_value_override`` (explicit value), plus the ``input_id`` and optional note;
* invalid / unknown / duplicate targets are rejected before anything is published;
* ``r=0`` is a valid roll and is never treated as "missing".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

INPUTS_VERSION = "build-local-inputs-v1"
INPUT_KINDS = ("affix_roll", "unique_mod_roll", "implicit_roll", "explicit_value")
EXPLICIT_TARGETS = ("affix", "unique_mod", "implicit")
MAX_ROLL = 255
MIN_ROLL = 0

_IMMUTABLE_ROLES = ("equipment", "idol", "blessing")


class InputError(ValueError):
    """Raised for an invalid local-inputs document or entry."""


class InputApplyError(InputError):
    """Raised when an input entry cannot be applied to the build structure."""

    def __init__(self, message: str, *, unapplied: list[dict] | None = None):
        super().__init__(message)
        self.unapplied = unapplied or []


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def empty_document(build_id: str) -> dict:
    return {
        "version": INPUTS_VERSION,
        "build_id": build_id,
        "updated_at": _now(),
        "entries": [],
    }


def document_sha256(document: dict) -> str:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def new_input_id(existing_ids: set[str] | None = None, *, now: dt.datetime | None = None) -> str:
    base = (now or dt.datetime.now(dt.timezone.utc)).strftime("in-%Y%m%dT%H%M%S")
    existing = existing_ids or set()
    for index in range(1, 100000):
        candidate = f"{base}-{index:04d}"
        if candidate not in existing:
            return candidate
    raise InputError("cannot allocate a unique input_id")  # pragma: no cover - defensive


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read local inputs file {path}: {exc}") from exc


def load_document(path: Path | str, *, build_id: str | None = None) -> dict | None:
    """Load and structurally validate the local-inputs file (``None`` when absent)."""
    path = Path(path)
    document = _read_json(path)
    if document is None:
        return None
    errors = validate_document(document, build_id=build_id)
    if errors:
        raise InputError("; ".join(errors))
    return normalize_document(document)


def save_document(path: Path | str, document: dict) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = normalize_document(document)
    document["updated_at"] = _now()
    text = json.dumps(document, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return document_sha256(document)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _explicit_bounds(meta: dict | None) -> tuple[float | None, float | None]:
    """Declared numeric bounds for an explicit-value target (normalized units)."""
    if not meta:
        return None, None
    for low_key, high_key in (("roll_min", "roll_max"), ("raw_value", "max_value")):
        low, high = meta.get(low_key), meta.get(high_key)
        if _is_number(low) and _is_number(high):
            return float(low), float(high)
    return None, None


def _format_display_number(value: float, percent: bool) -> str:
    number = round(value * 100, 4) if percent else round(value, 4)
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{text}%" if percent else text


def explicit_value_bounds_error(meta: dict | None, value_numeric: float) -> str | None:
    """Reject explicit values outside the value's declared range (e.g. negative rolls)."""
    low, high = _explicit_bounds(meta)
    if low is None or high is None:
        return None
    if low - 1e-9 <= value_numeric <= high + 1e-9:
        return None
    percent = bool(meta.get("percent")) if meta else False
    return (
        f"value {value_numeric!r} is outside the declared range "
        f"{_format_display_number(low, percent)}–{_format_display_number(high, percent)}"
    )


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def target_key(entry: dict) -> tuple:
    """Stable dedupe key: one value per logical target slot."""
    kind = entry.get("kind")
    path = entry.get("instance_path")
    if kind == "affix_roll":
        return (kind, path)
    if kind == "unique_mod_roll":
        return (kind, path, entry.get("roll_id"))
    if kind == "implicit_roll":
        return (kind, path, entry.get("index"))
    if kind == "explicit_value":
        target = entry.get("target")
        if target == "affix":
            return (kind, path, target, entry.get("affix_value_index", 0))
        if target == "unique_mod":
            if entry.get("mod_index") is not None:
                return (kind, path, target, "mod", entry.get("mod_index"))
            return (kind, path, target, "roll", entry.get("roll_id"))
        return (kind, path, target, entry.get("index"))
    return (kind, path)


def validate_entry(entry: dict) -> list[str]:
    """Structural validation for one entry (independent of a concrete BuildSpec)."""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    kind = entry.get("kind")
    if kind not in INPUT_KINDS:
        return [f"unknown kind {kind!r}; expected one of {list(INPUT_KINDS)}"]
    if not isinstance(entry.get("instance_path"), str) or not entry["instance_path"].strip():
        errors.append(f"{kind}: instance_path must be a non-empty string")
    if entry.get("input_id") is not None and not isinstance(entry.get("input_id"), str):
        errors.append(f"{kind}: input_id must be a string")
    if entry.get("note") is not None and not isinstance(entry.get("note"), str):
        errors.append(f"{kind}: note must be a string")
    if kind in ("affix_roll", "unique_mod_roll", "implicit_roll"):
        roll = entry.get("roll")
        if not _is_int(roll) or not (MIN_ROLL <= roll <= MAX_ROLL):
            errors.append(f"{kind}: roll must be an integer 0..255 (r=0 is a valid roll), got {roll!r}")
    if kind == "unique_mod_roll":
        roll_id = entry.get("roll_id")
        if not _is_int(roll_id) or roll_id < 0:
            errors.append(f"{kind}: roll_id must be an integer >= 0, got {roll_id!r}")
    if kind == "implicit_roll":
        index = entry.get("index")
        if not _is_int(index) or index < 0:
            errors.append(f"{kind}: index must be an integer >= 0, got {index!r}")
    if kind == "explicit_value":
        target = entry.get("target")
        if target not in EXPLICIT_TARGETS:
            errors.append(f"{kind}: target must be one of {list(EXPLICIT_TARGETS)}, got {target!r}")
        if not _is_number(entry.get("value_numeric")):
            errors.append(f"{kind}: value_numeric must be a finite number, got {entry.get('value_numeric')!r}")
        if entry.get("display_value") is not None and not _is_number(entry.get("display_value")):
            errors.append(f"{kind}: display_value must be a finite number when present")
        if entry.get("display_unit") not in (None, "percent", "flat"):
            errors.append(f"{kind}: display_unit must be 'percent' or 'flat' when present")
        if target == "affix":
            value_index = entry.get("affix_value_index", 0)
            if not _is_int(value_index) or value_index < 0:
                errors.append(f"{kind}: affix_value_index must be an integer >= 0")
        elif target == "unique_mod":
            has_mod_index = entry.get("mod_index") is not None
            has_roll_id = entry.get("roll_id") is not None
            if not has_mod_index and not has_roll_id:
                errors.append(f"{kind}: unique_mod requires mod_index or roll_id")
            if has_mod_index and (not _is_int(entry.get("mod_index")) or entry["mod_index"] < 0):
                errors.append(f"{kind}: mod_index must be an integer >= 0")
            if has_roll_id and (not _is_int(entry.get("roll_id")) or entry["roll_id"] < 0):
                errors.append(f"{kind}: roll_id must be an integer >= 0")
        elif target == "implicit":
            index = entry.get("index")
            if not _is_int(index) or index < 0:
                errors.append(f"{kind}: index must be an integer >= 0")
    return errors


def validate_document(document: dict, *, build_id: str | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["local inputs document is not an object"]
    if document.get("version") != INPUTS_VERSION:
        errors.append(f"version must be {INPUTS_VERSION!r}, got {document.get('version')!r}")
    document_build = document.get("build_id")
    if not isinstance(document_build, str) or not document_build:
        errors.append("build_id must be a non-empty string")
    elif build_id and document_build != build_id:
        errors.append(f"build_id mismatch: file has {document_build!r}, expected {build_id!r}")
    entries = document.get("entries")
    if not isinstance(entries, list):
        errors.append("entries must be a list")
        return errors
    seen_ids: set[str] = set()
    seen_targets: dict[tuple, int] = {}
    for index, entry in enumerate(entries):
        entry_errors = validate_entry(entry)
        for error in entry_errors:
            errors.append(f"entries[{index}]: {error}")
        if entry_errors:
            continue
        input_id = entry.get("input_id")
        if input_id:
            if input_id in seen_ids:
                errors.append(f"entries[{index}]: duplicate input_id {input_id!r}")
            seen_ids.add(input_id)
        key = target_key(entry)
        if key in seen_targets:
            errors.append(
                f"entries[{index}]: duplicate target {key!r} (already set by entries[{seen_targets[key]}])"
            )
        else:
            seen_targets[key] = index
    return errors


def normalize_document(document: dict) -> dict:
    """Return a deterministic copy: sorted entries, stable field order."""
    if not isinstance(document, dict):
        raise InputError("local inputs document is not an object")
    result = {
        "version": document.get("version", INPUTS_VERSION),
        "build_id": document.get("build_id"),
        "updated_at": document.get("updated_at") or _now(),
        "entries": [],
    }
    for entry in document.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        keep = {
            key: entry[key]
            for key in (
                "input_id",
                "kind",
                "instance_path",
                "roll",
                "roll_id",
                "index",
                "target",
                "affix_value_index",
                "mod_index",
                "value_numeric",
                "display_value",
                "display_unit",
                "note",
                "created_at",
            )
            if key in entry and entry[key] is not None
        }
        keep.setdefault("created_at", _now())
        result["entries"].append(keep)
    result["entries"].sort(key=lambda item: str(item.get("input_id") or ""))
    return result


def add_entry(document: dict, entry: dict) -> dict:
    """Validate and append one entry (assigns an input_id when absent)."""
    errors = validate_entry(entry)
    if errors:
        raise InputError("; ".join(errors))
    document = normalize_document(document)
    key = target_key(entry)
    for existing in document["entries"]:
        if target_key(existing) == key:
            raise InputError(f"an input for target {key!r} already exists (input_id={existing.get('input_id')})")
    existing_ids = {item.get("input_id") for item in document["entries"] if item.get("input_id")}
    candidate = dict(entry)
    candidate.setdefault("created_at", _now())
    provided_id = candidate.get("input_id")
    if provided_id and provided_id in existing_ids:
        raise InputError(f"input_id {provided_id!r} already exists; omit it to allocate a new id")
    candidate["input_id"] = provided_id or new_input_id(existing_ids)
    document["entries"].append(candidate)
    document["updated_at"] = _now()
    return document


def remove_entry(document: dict, input_id: str) -> tuple[dict, bool]:
    document = normalize_document(document)
    kept = [entry for entry in document["entries"] if entry.get("input_id") != input_id]
    removed = len(kept) != len(document["entries"])
    document["entries"] = kept
    document["updated_at"] = _now()
    return document, removed


def merge_documents(document: dict, incoming: dict) -> dict:
    document = normalize_document(document)
    incoming = normalize_document(incoming)
    if incoming.get("build_id") != document.get("build_id"):
        raise InputError(
            f"build_id mismatch: file {incoming.get('build_id')!r} vs current {document.get('build_id')!r}"
        )
    for entry in incoming["entries"]:
        try:
            document = add_entry(document, entry)
        except InputError as exc:
            raise InputError(f"cannot merge input {entry.get('input_id') or entry.get('kind')}: {exc}") from exc
    return document


# ---------------------------------------------------------------------------
# Target derivation / validation against a concrete BuildSpec
# ---------------------------------------------------------------------------
def _iter_item_instances(spec: dict):
    for slot in (spec.get("equipment") or {}).get("slots", []):
        yield f"equipment.{slot.get('slot')}", slot.get("instance") or {}
    for entry in (spec.get("idols") or {}).get("entries", []):
        yield f"idols.{entry.get('index')}", entry.get("instance") or {}
    for entry in (spec.get("blessings") or {}).get("entries", []):
        yield f"blessings.{entry.get('timeline')}", entry.get("instance") or {}


def _instance_label(instance: dict) -> str:
    readable = instance.get("readable") or {}
    definition = (instance.get("definition") or {}).get("ref") or {}
    base = (instance.get("base") or {}).get("ref") or {}
    return (
        readable.get("display_name")
        or definition.get("name")
        or base.get("name")
        or instance.get("instance_id")
        or "?"
    )


def _readable_lines(instance: dict) -> list[dict]:
    return (instance.get("readable") or {}).get("lines") or []


def _affix_value_range_text(affix: dict) -> str | None:
    value = affix.get("value") or {}
    for item in value.get("values") or []:
        if item.get("range_text"):
            return item.get("range_text")
    return None


def _value_display_stats(value_entry: dict, line: dict | None) -> dict:
    """Unit/percent/range metadata for one displayed variable (input UI + CLI helpers)."""
    line = line or {}
    text = line.get("text") or ""
    range_text = line.get("range_text") or ""
    unit = value_entry.get("unit")
    try:
        modifier_type = int(value_entry.get("modifier_type") or 0)
    except (TypeError, ValueError):
        modifier_type = 0
    percent = unit == "percent" or modifier_type != 0 or "%" in text or "%" in range_text
    return {
        "unit": "percent" if percent else (unit or "flat"),
        "modifier_type": modifier_type,
        "percent": percent,
        "range_text": range_text or None,
        "roll_min": value_entry.get("roll_min"),
        "roll_max": value_entry.get("roll_max"),
        "direction": value_entry.get("direction"),
    }


def build_target_index(spec: dict) -> dict:
    """Index every target the local-input interface can address in one BuildSpec.

    Each explicit-value target also carries enough display metadata for CLI/UI helpers to
    convert a typed *displayed* value (e.g. ``35`` for 35%) into the normalized
    ``value_numeric`` stored in the inputs file.  The percent flag is inferred from the
    structured unit / modifier type and from the rendered range text (``%``), covering
    properties whose percentage is display-only (e.g. modifier_type=0 percent stats).
    """
    index: dict = {"affix_roll": {}, "unique_mod_roll": {}, "implicit_roll": {}, "explicit_value": {}}
    for path, instance in _iter_item_instances(spec):
        source_path = instance.get("source_path") or path
        label = _instance_label(instance)
        readable_lines = _readable_lines(instance)
        affix_line_groups: dict[tuple, list[dict]] = {}
        for line in readable_lines:
            section = line.get("section") or ""
            if not section.startswith("affix_"):
                continue
            diagnostics = line.get("diagnostics") or {}
            affix_line_groups.setdefault((section, diagnostics.get("encoded_id")), []).append(line)
        affix_line_cursor: dict[tuple, int] = {}

        def _display_meta(value_entry: dict, line: dict | None) -> dict:
            text = (line or {}).get("text") or ""
            range_text = (line or {}).get("range_text") or ""
            unit = value_entry.get("unit")
            modifier_type = int(value_entry.get("modifier_type") or 0)
            percent = (
                unit == "percent"
                or modifier_type != 0
                or "%" in text
                or "%" in range_text
            )
            return {
                "unit": "percent" if percent else (unit or "flat"),
                "modifier_type": modifier_type,
                "roll_min": value_entry.get("roll_min"),
                "roll_max": value_entry.get("roll_max"),
                "stat_name": value_entry.get("stat_name"),
                "text": text or None,
                "range_text": range_text or None,
                "percent": percent,
                "value_numeric": value_entry.get("value_numeric"),
            }

        for channel, entries in (instance.get("affixes") or {}).items():
            for affix_index, affix in enumerate(entries or []):
                affix_path = affix.get("source_path")
                if not affix_path:
                    continue
                affix_values = ((affix.get("value") or {}).get("values")) or []
                affix_section = (
                    f"affix_{channel}"
                    if channel in ("sealed", "primordial", "corrupted")
                    else "affix_normal"
                )
                group_key = (affix_section, affix.get("encoded_id"))
                group = affix_line_groups.get(group_key) or []
                start = affix_line_cursor.get(group_key, 0)
                affix_line_cursor[group_key] = start + len(affix_values)
                value_lines = group[start : start + len(affix_values)]
                value_meta = []
                for value_index, value_entry in enumerate(affix_values):
                    line = value_lines[value_index] if value_index < len(value_lines) else None
                    meta = _display_meta(value_entry or {}, line)
                    meta["value_index"] = value_index
                    meta["affix_value_index"] = value_index
                    value_meta.append(meta)
                index["affix_roll"][affix_path] = {
                    "kind": "affix_roll",
                    "instance_path": affix_path,
                    "item_instance_id": instance.get("instance_id") or path,
                    "item_label": label,
                    "channel": channel,
                    "label": (
                        ((affix.get("affix") or {}).get("ref") or {}).get("name")
                        or affix.get("encoded_id")
                    ),
                    "source_tier": affix.get("source_tier"),
                    "range_text": _affix_value_range_text(affix),
                    "value_count": len(affix_values),
                    "values": value_meta,
                }
                index["explicit_value"][("affix", affix_path)] = {
                    "kind": "explicit_value",
                    "target": "affix",
                    "instance_path": affix_path,
                    "item_instance_id": instance.get("instance_id") or path,
                    "item_label": label,
                    "value_count": len(affix_values),
                    "values": value_meta,
                }
        implicit_block = instance.get("implicit_rolls") or {}
        implicit_count = implicit_block.get("implicit_count") or 0
        for implicit_index in range(implicit_count):
            key = (source_path, implicit_index)
            index["implicit_roll"][key] = {
                "kind": "implicit_roll",
                "instance_path": source_path,
                "index": implicit_index,
                "item_instance_id": instance.get("instance_id") or path,
                "item_label": label,
            }
            implicit_result = ((implicit_block.get("values") or [])[implicit_index] if implicit_index < len(implicit_block.get("values") or []) else {}) or {}
            implicit_value = ((implicit_result.get("values") or [None])[0]) or {}
            implicit_line = next(
                (
                    line
                    for line in readable_lines
                    if line.get("section") == "implicit"
                    and (line.get("diagnostics") or {}).get("implicit_index") == implicit_index
                ),
                None,
            )
            index["explicit_value"][("implicit", source_path, implicit_index)] = {
                "kind": "explicit_value",
                "target": "implicit",
                "instance_path": source_path,
                "index": implicit_index,
                "item_instance_id": instance.get("instance_id") or path,
                "item_label": label,
                **_display_meta(implicit_value, implicit_line),
            }
        unique_blocks = instance.get("unique_rolls") or {}
        roll_ids: dict[int, dict] = {}
        for mod in unique_blocks.get("mods") or []:
            roll_id = mod.get("roll_id")
            if roll_id is None:
                continue
            bucket = roll_ids.setdefault(int(roll_id), {"mod_indexes": [], "can_roll": False})
            bucket["mod_indexes"].append(mod.get("mod_index"))
            bucket["can_roll"] = bucket["can_roll"] or bool(mod.get("can_roll"))
        if roll_ids:
            index["unique_mod_roll"][source_path] = {
                "kind": "unique_mod_roll",
                "instance_path": source_path,
                "item_instance_id": instance.get("instance_id") or path,
                "item_label": label,
                "roll_ids": roll_ids,
            }
            for mod in unique_blocks.get("mods") or []:
                if mod.get("mod_index") is None:
                    continue
                mod_index = int(mod["mod_index"])
                unique_line = next(
                    (
                        line
                        for line in readable_lines
                        if line.get("section") == "unique_mod"
                        and (line.get("diagnostics") or {}).get("mod_index") == mod_index
                    ),
                    None,
                )
                restored = mod.get("restored") or {}
                value_meta = _display_meta(
                    {
                        "unit": restored.get("unit"),
                        "modifier_type": restored.get("modifier_type"),
                        "roll_min": restored.get("roll_min"),
                        "roll_max": restored.get("roll_max"),
                        "stat_name": restored.get("stat_name"),
                        "value_numeric": restored.get("value_numeric"),
                    },
                    unique_line,
                )
                value_meta.update(
                    {
                        "mod_index": mod_index,
                        "roll_id": mod.get("roll_id"),
                        "can_roll": bool(mod.get("can_roll")),
                        "raw_value": mod.get("raw_value"),
                        "max_value": mod.get("max_value"),
                    }
                )
                index["explicit_value"][("unique_mod", source_path, mod_index)] = {
                    "kind": "explicit_value",
                    "target": "unique_mod",
                    "instance_path": source_path,
                    "mod_index": mod_index,
                    "roll_id": mod.get("roll_id"),
                    "item_instance_id": instance.get("instance_id") or path,
                    "item_label": label,
                    **value_meta,
                }
    return index


def describe_targets(spec: dict) -> list[dict]:
    """List the *currently missing* values that the input interface can fill."""
    targets: list[dict] = []
    readable_by_item: dict[str, list[dict]] = {}
    for path, instance in _iter_item_instances(spec):
        readable_by_item[instance.get("source_path") or path] = _readable_lines(instance)
    for path, instance in _iter_item_instances(spec):
        source_path = instance.get("source_path") or path
        label = _instance_label(instance)
        lines = readable_by_item.get(source_path) or []
        affix_line_groups: dict[tuple, list[dict]] = {}
        for line in lines:
            section = line.get("section") or ""
            if not section.startswith("affix_"):
                continue
            diagnostics = line.get("diagnostics") or {}
            affix_line_groups.setdefault((section, diagnostics.get("encoded_id")), []).append(line)
        affix_line_cursor: dict[tuple, int] = {}
        for channel, entries in (instance.get("affixes") or {}).items():
            for affix in entries or []:
                roll = affix.get("roll") or {}
                if roll.get("status") not in ("absent", "null", "local_input"):
                    continue
                values = ((affix.get("value") or {}).get("values")) or []
                section = f"affix_{channel}"
                group_key = (section, affix.get("encoded_id"))
                group = affix_line_groups.get(group_key) or []
                start = affix_line_cursor.get(group_key, 0)
                affix_line_cursor[group_key] = start + len(values)
                value_lines = group[start : start + len(values)]
                variables = []
                for value_index, value_entry in enumerate(values):
                    if value_entry.get("value_numeric") is not None:
                        continue
                    if str(value_entry.get("value_source") or "").startswith("local_"):
                        continue
                    line = value_lines[value_index] if value_index < len(value_lines) else {}
                    stats = _value_display_stats(value_entry, line)
                    variables.append(
                        {
                            "value_index": value_index,
                            "affix_value_index": value_index,
                            "label": line.get("text") or value_entry.get("stat_name"),
                            "stat_name": value_entry.get("stat_name"),
                            **stats,
                        }
                    )
                if not variables:
                    continue
                affix_name = ((affix.get("affix") or {}).get("ref") or {}).get("name") or affix.get("encoded_id")
                range_text = _affix_value_range_text(affix)
                targets.append(
                    {
                        "kind": "affix_roll",
                        "instance_path": affix.get("source_path"),
                        "item_label": label,
                        "channel": channel,
                        "label": affix_name,
                        "source_tier": affix.get("source_tier"),
                        "range_text": range_text,
                        "variables": variables,
                        "suggested_command": (
                            "le_build input set --build <build-id> --kind affix_roll "
                            f"--instance {affix.get('source_path')} --roll <0-255>"
                        ),
                        "suggested_variable_commands": [
                            (
                                "le_build input set --build <build-id> --kind explicit_value "
                                f"--target affix --instance {affix.get('source_path')} "
                                f"--affix-value-index {variable['affix_value_index']} "
                                "--display-value <displayed-value>"
                            )
                            for variable in variables
                        ],
                    }
                )
        implicit_block = instance.get("implicit_rolls") or {}
        for result_index, result in enumerate(implicit_block.get("values") or []):
            entry_values = (result or {}).get("values") or []
            if entry_values and entry_values[0].get("value_numeric") is not None:
                continue
            if entry_values and entry_values[0].get("status") == "resolved":
                continue
            targets.append(
                {
                    "kind": "implicit_roll",
                    "instance_path": source_path,
                    "index": result_index,
                    "item_label": label,
                    "label": "implicit",
                    "range_text": None,
                    "suggested_command": (
                        "le_build input set --build <build-id> --kind implicit_roll "
                        f"--instance {source_path} --index {result_index} --roll <0-255>"
                    ),
                }
            )
        unique_blocks = instance.get("unique_rolls") or {}
        grouped: dict[int, dict] = {}
        for mod in unique_blocks.get("mods") or []:
            if not mod.get("can_roll"):
                continue
            if mod.get("roll_status") not in ("absent", "null"):
                continue
            restored = mod.get("restored") or {}
            if restored.get("value_numeric") is not None:
                continue
            if str(restored.get("value_source") or "").startswith("local_"):
                continue
            roll_id = mod.get("roll_id")
            if roll_id is None:
                continue
            line = next(
                (
                    line
                    for line in lines
                    if line.get("section") == "unique_mod"
                    and (line.get("diagnostics") or {}).get("mod_index") == mod.get("mod_index")
                ),
                None,
            )
            label = None
            if line is not None:
                label = line.get("text") or line.get("range_text")
            label = label or restored.get("stat_name") or f"mod_index {mod.get('mod_index')}"
            bucket = grouped.setdefault(
                int(roll_id), {"mod_indexes": [], "labels": [], "variables": []}
            )
            bucket["mod_indexes"].append(mod.get("mod_index"))
            bucket["labels"].append(label)
            bucket["variables"].append(
                {
                    "mod_index": mod.get("mod_index"),
                    "label": label,
                    "range_text": (line or {}).get("range_text"),
                    "raw_value": mod.get("raw_value"),
                    "max_value": mod.get("max_value"),
                    **_value_display_stats(restored, line),
                }
            )
        for roll_id, bucket in sorted(grouped.items()):
            targets.append(
                {
                    "kind": "unique_mod_roll",
                    "instance_path": source_path,
                    "roll_id": roll_id,
                    "item_label": label,
                    "label": "; ".join(bucket["labels"]) or f"rollId {roll_id}",
                    "mod_indexes": bucket["mod_indexes"],
                    "variables": bucket["variables"],
                    "range_text": "; ".join(
                        variable["range_text"]
                        for variable in bucket["variables"]
                        if variable.get("range_text")
                    )
                    or None,
                    "suggested_command": (
                        "le_build input set --build <build-id> --kind unique_mod_roll "
                        f"--instance {source_path} --roll-id {roll_id} --roll <0-255>"
                    ),
                    "suggested_variable_commands": [
                        (
                            "le_build input set --build <build-id> --kind explicit_value "
                            f"--target unique_mod --instance {source_path} "
                            f"--mod-index {variable['mod_index']} --display-value <displayed-value>"
                        )
                        for variable in bucket["variables"]
                    ],
                }
            )
    return targets


def validate_against_spec(document: dict, spec: dict) -> list[str]:
    """Reject entries whose targets do not exist in this BuildSpec."""
    index = build_target_index(spec)
    errors: list[str] = []
    for entry in document.get("entries") or []:
        kind = entry.get("kind")
        entry_errors: list[str] = []
        if kind == "affix_roll":
            path = entry.get("instance_path")
            if path not in index["affix_roll"]:
                entry_errors.append(f"unknown affix instance_path {path!r}")
        elif kind == "unique_mod_roll":
            path = entry.get("instance_path")
            bucket = index["unique_mod_roll"].get(path)
            if not bucket:
                entry_errors.append(f"no unique rolls on instance_path {path!r}")
            else:
                roll_id = int(entry.get("roll_id"))
                info = bucket["roll_ids"].get(roll_id)
                if not info:
                    entry_errors.append(f"roll_id {roll_id} does not exist on {path!r}")
                elif not info["can_roll"]:
                    entry_errors.append(f"roll_id {roll_id} is not rollable (canRoll=0) on {path!r}")
        elif kind == "implicit_roll":
            path = entry.get("instance_path")
            info = index["implicit_roll"].get((path, int(entry.get("index"))))
            if not info:
                entry_errors.append(
                    f"implicit index {entry.get('index')} does not exist on {path!r}"
                )
        elif kind == "explicit_value":
            target = entry.get("target")
            path = entry.get("instance_path")
            if target == "affix":
                info = index["explicit_value"].get(("affix", path))
                if not info:
                    entry_errors.append(f"unknown affix instance_path {path!r}")
                elif int(entry.get("affix_value_index", 0)) >= info["value_count"]:
                    entry_errors.append(
                        f"affix_value_index {entry.get('affix_value_index', 0)} out of range "
                        f"(affix has {info['value_count']} value(s))"
                    )
            elif target == "unique_mod":
                if entry.get("mod_index") is not None:
                    info = index["explicit_value"].get(("unique_mod", path, int(entry["mod_index"])))
                    if not info:
                        entry_errors.append(f"unique mod_index {entry['mod_index']} does not exist on {path!r}")
                else:
                    roll_id = int(entry.get("roll_id"))
                    bucket = index["unique_mod_roll"].get(path)
                    if not bucket or roll_id not in bucket["roll_ids"]:
                        entry_errors.append(f"roll_id {roll_id} does not exist on {path!r}")
            elif target == "implicit":
                info = index["explicit_value"].get(("implicit", path, int(entry.get("index"))))
                if not info:
                    entry_errors.append(
                        f"implicit index {entry.get('index')} does not exist on {path!r}"
                    )
        if kind == "explicit_value" and _is_number(entry.get("value_numeric")):
            meta = None
            target = entry.get("target")
            path = entry.get("instance_path")
            if target == "affix":
                info = index["explicit_value"].get(("affix", path))
                values = (info or {}).get("values") or []
                value_index = int(entry.get("affix_value_index", 0))
                meta = values[value_index] if value_index < len(values) else None
            elif target == "unique_mod" and entry.get("mod_index") is not None:
                meta = index["explicit_value"].get(
                    ("unique_mod", path, int(entry["mod_index"]))
                )
            elif target == "unique_mod" and entry.get("roll_id") is not None:
                for (kind, item_path, _mod_index), info in index["explicit_value"].items():
                    if kind == "unique_mod" and item_path == path and info.get("roll_id") == entry.get("roll_id"):
                        meta = info
                        break
            elif target == "implicit":
                meta = index["explicit_value"].get(("implicit", path, int(entry.get("index"))))
            bounds_error = explicit_value_bounds_error(meta, float(entry["value_numeric"]))
            if bounds_error:
                entry_errors.append(bounds_error)
        for error in entry_errors:
            errors.append(f"{kind} {entry.get('input_id') or ''}: {error}".strip())
    return errors


class InputIndex:
    """Runtime application helper used by the builder; tracks applied entries."""

    def __init__(self, document: dict | None = None):
        self.document = normalize_document(document) if document else None
        self._by_kind: dict[str, list[dict]] = {kind: [] for kind in INPUT_KINDS}
        self._applied: set[str] = set()
        if self.document:
            for entry in self.document["entries"]:
                self._by_kind.setdefault(entry["kind"], []).append(entry)

    @property
    def enabled(self) -> bool:
        return bool(self.document and self.document.get("entries"))

    @property
    def entries(self) -> list[dict]:
        return list(self.document.get("entries") or []) if self.document else []

    def _mark(self, entry: dict) -> dict:
        if entry.get("input_id"):
            self._applied.add(entry["input_id"])
        return entry

    def affix_roll(self, source_path: str) -> dict | None:
        for entry in self._by_kind.get("affix_roll", []):
            if entry.get("instance_path") == source_path:
                return self._mark(entry)
        return None

    def unique_rolls(self, item_source_path: str) -> dict[int, dict]:
        result: dict[int, dict] = {}
        for entry in self._by_kind.get("unique_mod_roll", []):
            if entry.get("instance_path") == item_source_path:
                result[int(entry["roll_id"])] = self._mark(entry)
        return result

    def implicit_rolls(self, item_source_path: str) -> dict[int, dict]:
        result: dict[int, dict] = {}
        for entry in self._by_kind.get("implicit_roll", []):
            if entry.get("instance_path") == item_source_path:
                result[int(entry["index"])] = self._mark(entry)
        return result

    def explicit_affix(self, affix_source_path: str) -> dict[int, dict]:
        result: dict[int, dict] = {}
        for entry in self._by_kind.get("explicit_value", []):
            if entry.get("target") == "affix" and entry.get("instance_path") == affix_source_path:
                result[int(entry.get("affix_value_index", 0))] = self._mark(entry)
        return result

    def explicit_unique(self, item_source_path: str) -> dict[int, dict]:
        """Explicit unique value overrides keyed by ``mod_index``."""
        result: dict[int, dict] = {}
        for entry in self._by_kind.get("explicit_value", []):
            if entry.get("target") != "unique_mod" or entry.get("instance_path") != item_source_path:
                continue
            if entry.get("mod_index") is not None:
                result[int(entry["mod_index"])] = self._mark(entry)
        return result

    def explicit_unique_by_roll_id(self, item_source_path: str) -> dict[int, dict]:
        """Explicit unique value overrides keyed by ``roll_id`` (expanded by the builder)."""
        result: dict[int, dict] = {}
        for entry in self._by_kind.get("explicit_value", []):
            if entry.get("target") != "unique_mod" or entry.get("instance_path") != item_source_path:
                continue
            if entry.get("mod_index") is None and entry.get("roll_id") is not None:
                result[int(entry["roll_id"])] = self._mark(entry)
        return result

    def explicit_implicit(self, item_source_path: str) -> dict[int, dict]:
        result: dict[int, dict] = {}
        for entry in self._by_kind.get("explicit_value", []):
            if entry.get("target") == "implicit" and entry.get("instance_path") == item_source_path:
                result[int(entry["index"])] = self._mark(entry)
        return result

    def mark_applied(self, entry: dict) -> dict:
        return self._mark(entry)

    def unapplied(self) -> list[dict]:
        return [
            entry
            for entry in self.entries
            if entry.get("input_id") and entry["input_id"] not in self._applied
        ]

    def manifest(self) -> list[dict]:
        return [
            {
                "input_id": entry.get("input_id"),
                "kind": entry.get("kind"),
                "instance_path": entry.get("instance_path"),
                "applied": entry.get("input_id") in self._applied,
            }
            for entry in self.entries
        ]


__all__ = [
    "INPUTS_VERSION",
    "INPUT_KINDS",
    "InputError",
    "InputApplyError",
    "empty_document",
    "document_sha256",
    "new_input_id",
    "load_document",
    "save_document",
    "validate_entry",
    "validate_document",
    "normalize_document",
    "add_entry",
    "remove_entry",
    "merge_documents",
    "target_key",
    "build_target_index",
    "describe_targets",
    "validate_against_spec",
    "InputIndex",
]
