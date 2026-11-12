"""Tests for :mod:`le_build.viewer` after the 2026-11-03 review rework.

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'
    & $py -m unittest tests.test_viewer -v

Coverage:

* the published real build (``data/builds/letools/o3ZRm9my``) as positive fixture;
  assertions adapt to whether the revision already carries the ``readable`` layer
  and the new ``scope`` (they are skipped on the old revision and fully executed
  after the reworked revision is published);
* the immutable historical revision ``rev-8dc664d73737a8b9`` as the backward
  compatibility fixture (old scope, weaver tree present, no readable layer);
* synthetic BuildSpecs for the information-architecture / degradation contract
  (four business areas, T7 labels, absent-roll wording, constant lines, rarity
  confidence, empty off-hand, diagnostics traceability, determinism).

The rendered HTML is parsed as text; expected facts are read directly from
``build.json`` instead of re-running the renderer.
"""

from __future__ import annotations

import html as html_module
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]  # .../LastEpoch-BuildSimulator
SRC = HERE.parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from le_build.viewer import _is_non_dps_line, render_build_html  # noqa: E402

BUILD_ROOT = REPO / "data" / "builds" / "letools" / "o3ZRm9my"
CURRENT_POINTER = BUILD_ROOT / "current.json"
HISTORICAL_REVISION_ID = "rev-8dc664d73737a8b9"
CURRENT_REVISION_ID = "rev-c746411ccebee9c0"  # delivered current (17 local inputs, seq6)
BASELINE_REVISION_ID = "rev-dbcf35ba60b0dd95"  # source-only demo (15 groups / 17 rows, seq5)
LEGACY_FLAGLESS_REVISION_ID = "rev-5e0e70bf7a26b3a5"  # pre-non_dps flag, property-104 fallback fixture

DATA_BLOCK_RE = re.compile(
    r'<script type="application/json" id="build-data">(.*?)</script>', re.DOTALL
)
RAW_BLOCK_RE = re.compile(
    r'<script type="application/json" id="build-raw">(.*?)</script>', re.DOTALL
)
DIAGNOSTICS_BLOCK_RE = re.compile(
    r"<!-- diagnostics:start -->.*?<!-- diagnostics:end -->", re.DOTALL
)
EXTERNAL_SUBRESOURCE_RE = re.compile(
    r"""(?:src|href)\s*=\s*(?:"\s*(?:https?:)?//|'\s*(?:https?:)?//)""", re.IGNORECASE
)
EXTERNAL_STYLE_URL_RE = re.compile(r"url\(\s*['\"]?(?:https?:)?//", re.IGNORECASE)
CONFIRMED_RARITY = {
    "definition",
    "catalog",
    "source",
    "source_authoritative",
    "resolved",
    "verified",
    "confirmed",
    "authoritative",
}
CHANNEL_SECTIONS = ("implicit", "affix_normal", "affix_sealed", "affix_primordial", "affix_corrupted", "unique_mod", "unique_description")

SLOT_LABELS_ZH = (
    "头部",
    "胸甲",
    "主手武器",
    "手套",
    "腰带",
    "鞋子",
    "戒指 1",
    "戒指 2",
    "项链",
    "遗物",
    "神像祭坛",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_published() -> tuple[dict, str, Path] | None:
    """Load the current immutable revision, or ``None`` when it is unavailable.

    The importer may republish while tests run, so if the pointer in ``current.json``
    is momentarily stale the newest available revision directory is used instead.
    """
    candidates: list[str] = []
    if CURRENT_POINTER.is_file():
        pointer = _load_json(CURRENT_POINTER)
        revision_id = pointer.get("revision_id")
        if revision_id:
            candidates.append(str(revision_id))
    revisions_dir = BUILD_ROOT / "revisions"
    if revisions_dir.is_dir():
        available = [
            child.name
            for child in revisions_dir.iterdir()
            if child.is_dir() and (child / "build.json").is_file()
        ]
        for name in sorted(available, reverse=True):
            if name not in candidates:
                candidates.append(name)
    for revision_id in candidates:
        directory = BUILD_ROOT / "revisions" / revision_id
        spec_path = directory / "build.json"
        if spec_path.is_file():
            return _load_json(spec_path), revision_id, directory
    return None


def _load_revision(revision_id: str) -> tuple[dict, str, Path] | None:
    """Load one immutable revision directory by id (or ``None`` when absent)."""
    if not revision_id:
        return None
    directory = BUILD_ROOT / "revisions" / revision_id
    spec_path = directory / "build.json"
    if not spec_path.is_file():
        return None
    return _load_json(spec_path), revision_id, directory


def _load_historical() -> tuple[dict, Path] | None:
    directory = BUILD_ROOT / "revisions" / HISTORICAL_REVISION_ID
    spec_path = directory / "build.json"
    if not spec_path.is_file():
        return None
    return _load_json(spec_path), directory


def _in_scope_instances(spec: dict) -> list[dict]:
    instances: list[dict] = []
    for entry in (spec.get("equipment") or {}).get("slots") or []:
        instance = (entry or {}).get("instance")
        if isinstance(instance, dict) and instance:
            instances.append(instance)
    for entry in (spec.get("idols") or {}).get("entries") or []:
        instance = (entry or {}).get("instance")
        if isinstance(instance, dict) and instance:
            instances.append(instance)
    for entry in (spec.get("blessings") or {}).get("entries") or []:
        instance = (entry or {}).get("instance")
        if isinstance(instance, dict) and instance:
            instances.append(instance)
    return instances


def _has_readable(spec: dict) -> bool:
    return any(isinstance(instance.get("readable"), dict) for instance in _in_scope_instances(spec))


def _default_region(document: str) -> str:
    """Everything outside the default-closed diagnostics block."""
    return DIAGNOSTICS_BLOCK_RE.sub("", document)


def _diagnostics_region(document: str) -> str:
    match = DIAGNOSTICS_BLOCK_RE.search(document)
    return match.group(0) if match else ""


def _input_panel_region(document: str) -> str:
    """Extract the (nested-details) 补充缺失数值 panel markup."""
    start = document.find('<details id="local-inputs"')
    if start < 0:
        return ""
    end = document.find('<details id="diagnostics"', start)
    return document[start:end] if end > start else document[start:]


def _visible_text(fragment: str) -> str:
    text = re.sub(r"<style.*?</style>", " ", fragment, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _lines(fragment: str, *, status: str | None = None, kind: str | None = None) -> list[str]:
    pattern = r'<li class="line[^"]*"[^>]*>.*?</li>'
    if status:
        pattern = rf'<li class="line[^"]*"[^>]*data-status="{re.escape(status)}"[^>]*>.*?</li>'
    if kind:
        pattern = rf'<li class="line[^"]*"[^>]*data-kind="{re.escape(kind)}"[^>]*>.*?</li>'
    return re.findall(pattern, fragment, flags=re.DOTALL)


def _card_region(document: str, item_name: str) -> str:
    marker = f'<h3 class="item-name">{html_module.escape(item_name, quote=True)}</h3>'
    index = document.find(marker)
    if index < 0:
        return ""
    start = document.rfind("<article", 0, index)
    end = document.find("</article>", index)
    if start < 0 or end < 0:
        return ""
    return document[start : end + len("</article>")]


def _spec_definition_name(instance: dict) -> str:
    readable = instance.get("readable") or {}
    if isinstance(readable.get("display_name"), str) and readable["display_name"]:
        return readable["display_name"]
    definition = instance.get("definition") or {}
    if definition.get("name"):
        return definition["name"]
    base = instance.get("base") or {}
    return base.get("name") or ""


def _instance_readable_lines(instance: dict) -> list[dict]:
    readable = instance.get("readable") or {}
    lines = readable.get("lines")
    return [line for line in lines if isinstance(line, dict)] if isinstance(lines, list) else []


def _missing_roll_affixes(spec: dict) -> list[dict]:
    rows = []
    for instance in _in_scope_instances(spec):
        for channel in ("normal", "sealed", "primordial", "corrupted"):
            for affix in (instance.get("affixes") or {}).get(channel) or []:
                roll = affix.get("roll") or {}
                if roll.get("raw") is None and str(roll.get("status") or "").lower() in (
                    "",
                    "absent",
                    "null",
                    "none",
                ):
                    rows.append(affix)
    return rows


# ---------------------------------------------------------------------------
# synthetic BuildSpecs
# ---------------------------------------------------------------------------
def _instance(
    *,
    instance_id: str,
    role: str,
    encoded_id: str,
    source_path: str,
    name: str,
    base_name: str = "",
    readable: dict | None = None,
    definition: dict | None = None,
    base: dict | None = None,
    affixes: dict | None = None,
    implicit: dict | None = None,
    unique: dict | None = None,
    slot: str | None = None,
    position: dict | None = None,
) -> dict:
    instance = {
        "instance_id": instance_id,
        "role": role,
        "slot": slot,
        "position": position,
        "source_path": source_path,
        "encoded_id": encoded_id,
        "codec": {
            "codec_version": "letools-id-codec-v1",
            "encoded_id": encoded_id,
            "marker": encoded_id[:1] or "U",
            "payload": "012345",
            "status": "decoded",
        },
        "codec_class": "unique" if readable and (readable.get("rarity") or {}).get("code") == "unique" else "base",
        "rarity": "unique" if readable and (readable.get("rarity") or {}).get("code") == "unique" else "base",
        "definition": dict(definition) if definition is not None else {"name": name},
        "base": dict(base) if base is not None else ({"name": base_name} if base_name else {}),
        "definition_status": "resolved" if definition is not None else "not_applicable",
        "base_status": "resolved",
        "affixes": affixes or {"normal": [], "sealed": [], "primordial": [], "corrupted": []},
        "affix_counts": {"normal": 0, "sealed": 0, "primordial": 0, "corrupted": 0},
        "affix_total": sum(len(value) for value in (affixes or {}).values()),
        "implicit_rolls": implicit
        or {
            "source_path": f"{source_path}.ir",
            "ir": None,
            "status": "absent",
            "implicit_count": 0,
            "used_ir_values": None,
            "unused_ir_values": None,
            "values": None,
            "definition_provenance": {"source": "synthetic"},
        },
        "unique_rolls": unique
        or {
            "source_path": f"{source_path}.ur",
            "ur": None,
            "status": "not_applicable",
            "roll_ids": [],
            "mods": [],
            "values": None,
        },
        "faction": None,
        "faction_note": None,
        "unrecognized_fields": {},
        "status": "resolved",
        "counts_as_equipped": True,
    }
    if readable is not None:
        instance["readable"] = readable
    return instance


def _readable(
    display_name: str,
    *,
    base_name: str = "",
    item_type: str = "",
    rarity: dict | None = None,
    completeness: str = "complete",
    lines: list[dict] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    block = {
        "version": "letools-readable-v1",
        "display_name": display_name,
        "base_name": base_name,
        "item_type": item_type,
        "rarity": rarity,
        "completeness": completeness,
        "lines": lines or [],
        "warnings": warnings or [],
    }
    return block


def _line(
    section: str,
    text: str,
    *,
    value_status: str = "actual",
    tier_label: str | None = None,
    range_text: str | None = None,
    kind: str = "mod",
    hidden: bool = False,
    diagnostics: dict | None = None,
) -> dict:
    return {
        "section": section,
        "kind": kind,
        "text": text,
        "tier_label": tier_label,
        "value_status": value_status,
        "range_text": range_text,
        "value_text": None,
        "hidden": hidden,
        "diagnostics": diagnostics or {},
    }


def _synthetic_affix(
    *,
    channel: str = "normal",
    source_tier: int = 7,
    raw_roll: int | None = 113,
    encoded_id: str = "AAwRgzEA",
    source_path: str = "data.equipment.head.affixes.0",
    values: list[dict] | None = None,
) -> dict:
    value = {
        "status": "resolved" if raw_roll is not None else "partial",
        "roll_status": "present" if raw_roll is not None else "absent",
        "raw_roll": raw_roll,
        "values": values
        or [
            {
                "property": 13,
                "stat_id": "stat:synthetic:13",
                "stat_name": "Fire Resistance",
                "unit": "percent",
                "modifier_type": 0,
                "value_numeric": 0.67,
                "display_value": "67%",
                "roll_min": 0.61,
                "roll_max": 0.75,
                "status": "resolved",
            }
        ],
        "evidence": {"roll_id": 3},
    }
    return {
        "instance_id": f"equipment.head.{channel}.0",
        "channel": channel,
        "source_path": source_path,
        "encoded_id": encoded_id,
        "source_tier": source_tier,
        "roll": {"raw": raw_roll, "status": "present" if raw_roll is not None else "absent", "source_path": f"{source_path}.r"},
        "affix": {"ref": {"entity_id": "affix:synthetic:13", "name": "Fire Resistance"}, "status": "resolved"},
        "tier": {"source_tier": source_tier, "tier_index": source_tier - 1, "roll_min": 0.61, "roll_max": 0.75, "status": "resolved"},
        "value": value,
        "status": "resolved",
    }


def _synthetic_spec() -> dict:
    unique_instance = _instance(
        instance_id="equipment.head",
        role="equipment",
        encoded_id="UAwJgzGCMBsQ",
        source_path="data.equipment.head",
        name="Test Unique",
        base_name="Test Base",
        slot="head",
        readable=_readable(
            "Test Unique",
            base_name="Test Base",
            item_type="Helmet",
            rarity={"code": "unique", "label_zh": "暗金", "label_en": "Unique", "confidence": "definition"},
            completeness="partial",
            lines=[
                _line("implicit", "+77 Armor", value_status="constant"),
                _line("affix_normal", "22% increased Mana", tier_label="T7"),
                _line(
                    "affix_sealed",
                    "(61%–75%) increased Fire Resistance",
                    value_status="range",
                    range_text="61%–75%",
                    diagnostics={"encoded_id": "AAwRgzEB"},
                ),
                _line("affix_corrupted", "+ (1–2) to Level of Skills", value_status="range", range_text="1–2"),
                _line("unique_mod", "109 Ward Decay Threshold"),
                _line(
                    "unique_mod",
                    "1% increased Physical Resistance",
                    value_status="range",
                    range_text="1%–45%",
                    diagnostics={"roll_id": 2, "mod_index": 1},
                ),
                _line("unique_description", "Apex of Thought combined description text", kind="description"),
                _line(
                    "unique_description",
                    "You deal 10%–14% more damage",
                    kind="description",
                    value_status="range",
                    range_text="10%–14%",
                ),
                _line("unique_mod", "hidden internal value", hidden=True),
            ],
            warnings=["synthetic readability warning"],
        ),
        affixes={
            "normal": [
                _synthetic_affix(
                    raw_roll=113,
                    values=[
                        {
                            "property": 13,
                            "stat_id": "stat:synthetic:13",
                            "stat_name": "Fire Resistance",
                            "unit": "percent",
                            "modifier_type": 0,
                            "value_numeric": 0.67,
                            "display_value": "67%",
                            "roll_min": 0.61,
                            "roll_max": 0.75,
                            "status": "resolved",
                        }
                    ],
                )
            ],
            "sealed": [
                _synthetic_affix(
                    channel="sealed",
                    raw_roll=None,
                    encoded_id="AAwRgzEB",
                    source_path="data.equipment.head.sealedAffix",
                    values=[
                        {
                            "property": 13,
                            "stat_id": "stat:synthetic:13",
                            "stat_name": "Fire Resistance",
                            "unit": "percent",
                            "modifier_type": 1,
                            "value_numeric": None,
                            "roll_min": 0.61,
                            "roll_max": 0.75,
                            "status": "partial",
                        }
                    ],
                )
            ],
            "primordial": [],
            "corrupted": [],
        },
        implicit={
            "source_path": "data.equipment.head.ir",
            "ir": [234, 167, 203],
            "status": "present",
            "implicit_count": 1,
            "definition_provenance": {"source": "items.implicit_text"},
            "ir_width": 3,
            "used_ir_values": [234, 167, 203],
            "unused_ir_values": None,
            "values": None,
        },
        unique={
            "source_path": "data.equipment.head.ur",
            "ur": [255, 234],
            "status": "present",
            "roll_ids": [3],
            "mods": [
                {
                    "mod_index": 0,
                    "roll_id": 3,
                    "can_roll": True,
                    "hidden": False,
                    "raw_roll": 234,
                    "roll_status": "present",
                    "restored": {"status": "resolved", "value_numeric": 109, "display_value": "109"},
                },
                {
                    "mod_index": 1,
                    "roll_id": 2,
                    "can_roll": True,
                    "hidden": False,
                    "raw_value": 0.01,
                    "max_value": 0.45,
                    "raw_roll": None,
                    "roll_status": "absent",
                    "restored": {"status": "partial", "value_numeric": None, "display_value": None},
                },
            ],
            "values": None,
        },
    )
    plain_instance = _instance(
        instance_id="equipment.chest",
        role="equipment",
        encoded_id="IAwJgzGCMBsQ",
        source_path="data.equipment.chest",
        name="Plain Chest",
        base_name="Arcane Regalia",
        slot="chest",
        readable=_readable(
            "Plain Chest",
            base_name="Arcane Regalia",
            item_type="Body Armor",
            rarity=None,
            lines=[
                _line("implicit", "+15 Armor", value_status="constant"),
                _line("affix_normal", "+67% Fire Resistance", tier_label="T7"),
            ],
        ),
        affixes={"normal": [_synthetic_affix()], "sealed": [], "primordial": [], "corrupted": []},
        implicit={
            "source_path": "data.equipment.chest.ir",
            "ir": [123],
            "status": "present",
            "implicit_count": 1,
            "used_ir_values": [123],
            "unused_ir_values": None,
            "values": [{"status": "unresolved", "values": None, "source_path": "data.equipment.chest.ir.0"}],
        },
    )
    unconfirmed_instance = _instance(
        instance_id="equipment.ring1",
        role="equipment",
        encoded_id="IAwJgzGCMBsR",
        source_path="data.equipment.ring1",
        name="Unconfirmed Ring",
        base_name="Silver Ring",
        slot="ring1",
        readable=_readable(
            "Unconfirmed Ring",
            base_name="Silver Ring",
            rarity={"code": "unique", "label_zh": "暗金", "label_en": "Unique", "confidence": "codec"},
            lines=[_line("affix_normal", "+10 Strength", tier_label="T5")],
        ),
    )
    idol_instance = _instance(
        instance_id="idols.0",
        role="idol",
        encoded_id="IAwNg7EA",
        source_path="data.idols.0",
        name="Test Idol",
        readable=_readable(
            "Test Idol",
            item_type="Idol",
            lines=[_line("implicit", "12% increased Mana", value_status="range", range_text="10%–14%")],
        ),
        position={"x": 1, "y": 2},
    )
    blessing_instance = _instance(
        instance_id="blessings.1",
        role="blessing",
        encoded_id="IIwBgzALMwGwvQ",
        source_path="data.blessings.1",
        name="Grand Test Blessing",
        readable=_readable(
            "Grand Test Blessing",
            item_type="Blessing",
            lines=[_line("implicit", "+45% increased Gold", value_status="range", range_text="45%–70%")],
        ),
        position={"timeline": "1"},
    )
    return {
        "build_spec_version": "build-spec-v1",
        "parser_version": "synthetic-parser",
        "mapping_version": "synthetic-mapping",
        "meta": {
            "build_id": "letools:synthetic",
            "share_url": "https://www.lastepochtools.com/planner/abc123",
            "source_payload": {
                "raw_sha256": "0" * 64,
                "data_field_sha256": "1" * 64,
                "raw_path": "data/builds/letools/synthetic/raw/raw.json",
            },
            "revision": {
                "revision_id": "rev-synthetic-000000000000",
                "revision_seq": 2,
                "content_sha256": "2" * 64,
                "published_at": "2026-11-03T00:00:00+00:00",
            },
        },
        "catalog": {
            "dataset_id": "dataset-synthetic",
            "source_snapshot_id": "snapshot-synthetic",
            "target_game_version": "1.5.2",
            "target_season": "Season 5",
            "logical_content_sha256": "3" * 64,
            "file_sha256": "4" * 64,
            "verification": {"verified": True},
        },
        "scope": {
            "scope_version": "letools-build-scope-v2",
            "included_sections": ["equipment", "idols", "blessings", "specializations", "action_bar"],
            "excluded_features": [
                {
                    "feature": "weaver_tree",
                    "source_fields": ["data.weaverTree", "data.weaverTreeContainers", "data.weaverTreeProgression"],
                    "reason": "user requirement 2026-11-03: build import excludes the weaver tree",
                    "decision_date": "2026-11-03",
                    "raw_preserved": True,
                }
            ],
        },
        "weaver": {
            "status": "excluded_by_scope",
            "source_fields": ["data.weaverTree"],
            "raw_preserved": True,
            "note": "not parsed as business objects",
        },
        "character": {
            "level": 100,
            "class": {"ref": {"name": "Mage"}},
            "mastery": {"ref": {"name": "Sorcerer"}},
        },
        "equipment": {
            "slot_order": ["head", "chest", "ring1"],
            "slots": [
                {"slot": "head", "instance": unique_instance},
                {"slot": "chest", "instance": plain_instance},
                {"slot": "ring1", "instance": unconfirmed_instance},
            ],
            "absent_slots": ["weapon2"],
            "note": "缺失槽位不会被自动补齐。",
        },
        "idols": {"new_grid": True, "entries": [{"index": 0, "position": {"x": 1, "y": 2}, "instance": idol_instance}]},
        "blessings": {"entries": [{"timeline": "1", "instance": blessing_instance}]},
        "action_bar": {
            "slots": [
                {"slot": 0, "ref": {"name": "Meteor"}, "specialization_slot": 1, "status": "resolved"},
                {"slot": 1, "ref": {"name": "Teleport"}, "specialization_slot": None, "status": "resolved"},
            ]
        },
        "specializations": [
            {
                "slot_number": 1,
                "source_level": 20,
                "points_total": 15,
                "tree": {"name": "Meteor"},
                "owner_skill": {"name": "Meteor"},
                "nodes": [
                    {"ref": {"name": "Meteor Node"}, "points": 2, "max_points": 5, "explicit_zero": False, "points_within_max": True},
                ],
                "status": "resolved",
            }
        ],
        "passive_tree": {
            "tree": {"name": "Mage Passive"},
            "points_total": 8,
            "nodes": [
                {"ref": {"name": "Arcanist"}, "points": 8, "max_points": 8, "explicit_zero": False, "points_within_max": True},
            ],
        },
        "issues": [
            {"code": "synthetic_issue", "severity": "warning", "path": "data.equipment.head", "message": "synthetic warning"},
            {"code": "synthetic_info", "severity": "info", "path": "data.idols.0", "message": "synthetic info"},
        ],
        "coverage": {
            "item_instances": {"total": 5, "identity_connected": 5, "base_resolved": 5},
            "affix_instances": {"total": 2, "resolved": 2, "tier_resolved": 2},
            "rolls": {"present": 1, "absent": 1, "denominator": 2},
            "values": {"computed": 2, "resolved": 2, "partial": 0, "unresolved": 0, "denominator": 2},
            "nodes": {"total": 2, "resolved": 2, "passive": 1, "skill_tree": 1, "weaver": 0},
        },
    }


def _render(spec, revision_id: str, build_dir: Path | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        directory = build_dir if build_dir is not None else Path(tmp)
        return render_build_html(spec, revision_id=revision_id, build_dir=directory)


# ---------------------------------------------------------------------------
# delivered current revision (pinned: rev-c746411ccebee9c0)
# ---------------------------------------------------------------------------
class PublishedBuildViewerTests(unittest.TestCase):
    """Render the delivered current revision and check the player-visible facts."""

    @classmethod
    def setUpClass(cls) -> None:
        if CURRENT is None:
            raise unittest.SkipTest("delivered current revision rev-c746411ccebee9c0 is not available")
        cls.spec, cls.revision_id, cls.revision_dir = CURRENT
        cls.html = render_build_html(cls.spec, revision_id=cls.revision_id, build_dir=cls.revision_dir)
        cls.default = _default_region(cls.html)
        cls.diagnostics = _diagnostics_region(cls.html)
        cls.has_readable = _has_readable(cls.spec)
        cls.has_scope = isinstance(cls.spec.get("scope"), dict) and bool(cls.spec.get("scope"))

    # -- document shape ---------------------------------------------------
    def test_single_standalone_document(self) -> None:
        html = self.html
        self.assertTrue(html.startswith("<!DOCTYPE html>"), "document must start with <!DOCTYPE html>")
        self.assertEqual(html.count("<html"), 1)
        self.assertEqual(html.count("</html>"), 1)
        self.assertEqual(html.count("<title>"), 1)
        self.assertIn('lang="zh-CN"', html)
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('id="build-data"', html)
        self.assertEqual(html.count('<section class="panel"'), 4, "exactly four business panels")
        self.assertIn('id="diagnostics"', html)

    def test_no_external_subresources(self) -> None:
        html = self.html
        self.assertIsNone(EXTERNAL_SUBRESOURCE_RE.search(html), "src/href must not point at http(s) URLs")
        self.assertIsNone(EXTERNAL_STYLE_URL_RE.search(html), "CSS must not load remote url(...) resources")
        self.assertNotIn("<link", html.lower(), "no <link> tags (no external stylesheets/fonts)")
        self.assertNotIn("<script src", html.lower(), "no external scripts")
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        self.assertNotIn("WebSocket", html)
        self.assertNotIn("importScripts", html)
        self.assertNotIn("sendBeacon", html)

    def test_default_view_is_business_only(self) -> None:
        text = _visible_text(self.default)
        # The four business areas and nothing else as top-level tabs.
        for label in ("构筑概览", "装备", "技能与被动", "神像与祝福"):
            self.assertIn(label, text)
        # Engineering identifiers / database terms never leak into the default view.
        for forbidden in ("rev-", "sha256", "tier_index", "codec", "snapshot", "fingerprint", "source_path", "encoded_id"):
            self.assertNotIn(forbidden, self.default, f"default view leaks {forbidden!r}")
        for forbidden in ("build_id", "dataset", "coverage", "issues", "r/ir/ur", "ir=[", "ur=["):
            self.assertNotIn(forbidden, text, f"default view leaks {forbidden!r}")
        self.assertNotIn("编织", text, "weaver tree must not appear in the default view")

    def test_diagnostics_is_default_closed_and_traceable(self) -> None:
        match = re.search(r"<details[^>]*id=\"diagnostics\"[^>]*>", self.html)
        self.assertIsNotNone(match)
        self.assertNotIn(" open", match.group(0))
        self.assertTrue(self.diagnostics)
        self.assertIn(self.revision_id, self.diagnostics)
        # traceability of the original data inside diagnostics
        for needle in ("tier_index", "codec", "source_path", "encoded_id"):
            self.assertIn(needle, self.diagnostics, f"diagnostics must keep {needle!r} traceability")
        self.assertIn('data-export-raw="build-raw"', self.html)
        raw_match = RAW_BLOCK_RE.search(self.html)
        self.assertIsNotNone(raw_match, "raw BuildSpec JSON block missing")
        raw = json.loads(raw_match.group(1))
        self.assertEqual(raw.get("build_spec_version"), self.spec.get("build_spec_version"))

    # -- post-sync local inputs (current revision) -----------------------
    def test_current_revision_is_delivered(self) -> None:
        self.assertEqual(self.revision_id, CURRENT_REVISION_ID)
        self.assertEqual(self.spec["meta"]["revision"]["revision_id"], CURRENT_REVISION_ID)
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        self.assertEqual(data["revision_id"], CURRENT_REVISION_ID)
        self.assertEqual(data["historical"], False)

    def test_current_pointer_matches_delivered_revision(self) -> None:
        published = _load_published()
        if published is None:
            self.skipTest("current pointer is not available")
        self.assertEqual(published[1], CURRENT_REVISION_ID)

    def test_local_inputs_overview_one_liner(self) -> None:
        local_inputs = self.spec["meta"].get("local_inputs") or {}
        self.assertEqual(local_inputs.get("version"), "build-local-inputs-v1")
        self.assertEqual(local_inputs.get("entries"), 17)
        self.assertEqual(local_inputs.get("applied"), 17)
        text = _visible_text(self.default)
        self.assertIn("本地手动输入：17 条（诊断详情可追溯）", text)
        self.assertEqual(text.count("本地手动输入："), 1)

    def test_manual_badges_on_local_value_lines(self) -> None:
        manual_lines = [
            line
            for instance in _in_scope_instances(self.spec)
            for line in ((instance.get("readable") or {}).get("lines") or [])
            if str(((line.get("diagnostics") or {}).get("value_source") or "")).startswith("local_")
        ]
        self.assertEqual(len(manual_lines), 17)
        self.assertEqual(self.html.count('<span class="badge badge-manual"'), 17)
        self.assertIn("手动", _visible_text(self.default))
        # every delivered manual line is a resolved actual value carrying its input_id
        for line in manual_lines:
            self.assertEqual(line.get("value_status"), "actual")
            input_id = (line.get("diagnostics") or {}).get("input_id")
            self.assertTrue(input_id)
            self.assertIn(f"input_id: {input_id}", self.default)
        # total visible readable lines = 124 source + 17 manual
        visible = [
            line
            for instance in _in_scope_instances(self.spec)
            for line in ((instance.get("readable") or {}).get("lines") or [])
            if not line.get("hidden")
        ]
        self.assertEqual(len(visible), 141)

    def test_no_pending_input_rows_on_delivered_build(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        template = data["local_inputs_template"]
        self.assertEqual(template["counts"], {"groups": 0, "variables": 0})
        self.assertEqual(template["groups"], [])
        match = re.search(r'<details[^>]*id="local-inputs"[^>]*>', self.html)
        self.assertIsNotNone(match)
        self.assertNotIn(" open", match.group(0))
        panel = _input_panel_region(self.html)
        self.assertTrue(panel)
        panel_text = _visible_text(panel)
        self.assertIn("没有待补数值", panel_text)
        self.assertIn("没有需要补充的来源缺失数值", panel_text)
        self.assertNotIn('class="input-display"', panel)
        self.assertNotIn('class="input-roll"', panel)
        self.assertNotIn('data-input-download', panel)

    def test_diagnostics_local_inputs_binding(self) -> None:
        diag = self.diagnostics
        for needle in (
            "local_inputs.version",
            "local_inputs.entries",
            "local_inputs.applied",
            "local_inputs.sha256",
            "local_inputs.path",
            "inputs_manifest",
            "in-20261104T002556-0001",
        ):
            self.assertIn(needle, diag)
        self.assertIn(">17<", diag, "entries/applied count must be 17 in diagnostics")
        self.assertIn("value_source", diag)
        self.assertIn("local_value_override", diag)
        self.assertIn("manifest", diag.lower())
        self.assertIn("手动行", diag)

    # -- re-review R1: non-DPS/drop-rate display filtering ----------------
    def test_drop_rate_lines_hidden_from_default_but_kept_in_diagnostics(self) -> None:
        text = _visible_text(self.default)
        self.assertNotIn("Drop Rate", text)
        self.assertNotIn("Drop Rate", self.default)
        drop_lines = [
            line
            for instance in _in_scope_instances(self.spec)
            for line in ((instance.get("readable") or {}).get("lines") or [])
            if _is_non_dps_line(line)
        ]
        self.assertEqual(len(drop_lines), 5)
        for line in drop_lines:
            diagnostics = line.get("diagnostics") or {}
            self.assertIs(diagnostics.get("non_dps"), True)
            self.assertEqual(diagnostics.get("property"), 104)
            self.assertIn(line.get("text"), self.diagnostics)
        self.assertIn("65% Increased Drop Rate", self.diagnostics)
        raw = json.loads(RAW_BLOCK_RE.search(self.html).group(1))
        self.assertEqual(len(raw["blessings"]["entries"]), 10)  # source data retained

    def test_drop_only_blessing_cards_removed_combat_blessings_kept(self) -> None:
        text = _visible_text(self.default)
        self.assertIn("祝福（5 条）", text)
        self.assertIn("仅掉落率类别的非战斗效果不进入默认 BD 展示", text)
        for dropped in (
            "Grand Scales of Greed",
            "Grand Talon of Grandeur",
            "Grand Subtlety of Slaughter",
            "Hope of the Beginning",
            "Grand Memory of Masters",
        ):
            self.assertNotIn(dropped, text, f"drop-only blessing card {dropped!r} must be hidden")
            self.assertIn(dropped, self.diagnostics)
        for kept in (
            "+35 Ward per Second",
            "+20% to All Resistances",
            "+51% Chance to Chill on Hit",
            "+74% Fire Resistance",
            "18 Ward Gain on Kill",
            "+199 Ward Decay Threshold",
        ):
            self.assertIn(kept, text, f"combat blessing line {kept!r} missing")

    def test_ward_gain_event_and_crescendo_text(self) -> None:
        text = _visible_text(self.default)
        self.assertIn("18 Ward Gain on Kill", text)
        self.assertIn("You have Crescendoing Rage", text)
        self.assertNotIn("+1 You have Crescendoing Rage", self.html)
        self.assertNotIn("+1 You have Crescendoing Rage", text)
        self.assertIn("+199 Ward Decay Threshold", text)

    def test_non_dps_filtering_is_property_based_not_text_based(self) -> None:
        # unit-level guard: text matching must never drive the filter
        self.assertFalse(
            _is_non_dps_line({"text": "65% Increased Drop Rate", "diagnostics": {"property": 13}})
        )
        self.assertTrue(_is_non_dps_line({"text": "totally different wording", "diagnostics": {"property": 104}}))
        self.assertTrue(_is_non_dps_line({"text": "x", "diagnostics": {"non_dps": True}}))
        self.assertFalse(_is_non_dps_line({"text": "65% Increased Drop Rate", "diagnostics": {}}))

    def test_no_missing_value_markers_on_delivered_build(self) -> None:
        # post-sync revision: all 17 previously-missing values are actual, so no
        # rendered line carries the missing-range marker (the overview explainer
        # sentence mentions the wording without being a data row).
        marked = [line for line in _lines(self.default) if "数值未提供（显示范围" in line]
        self.assertEqual(marked, [])
        self.assertNotIn("未提供 roll", _visible_text(self.default))

    def test_default_view_has_no_input_engineering_fields(self) -> None:
        default = self.default
        for forbidden in ("roll_id", "rollId", "instance_path", "data.equipment"):
            self.assertNotIn(forbidden, default, f"default region leaks {forbidden!r}")


    # -- overview facts ---------------------------------------------------
    def test_character_and_game_version_are_visible(self) -> None:
        character = self.spec["character"]
        self.assertIn(character["class"]["ref"]["name"], self.html)  # Mage
        self.assertIn(character["mastery"]["ref"]["name"], self.html)  # Sorcerer
        self.assertIn(f"等级 {character['level']}", _visible_text(self.default))
        self.assertIn(self.spec["catalog"]["target_game_version"], self.default)
        self.assertIn(self.spec["meta"]["share_url"], self.default)

    def test_integrity_one_liner_uses_business_counts(self) -> None:
        counts = self._counts()
        text = _visible_text(self.default)
        self.assertIn("导入完整性", text)
        self.assertIn(
            f"物品 {counts['items_ok']}/{counts['items_total']} · 词缀 {counts['affixes_ok']}/{counts['affix_total']}",
            text,
        )
        self.assertIn(f"{counts['absent_rolls']} 条来源缺 roll", text)

    def test_all_equipment_slots_are_visible(self) -> None:
        equipment = self.spec["equipment"]
        order = equipment.get("slot_order") or [slot["slot"] for slot in equipment.get("slots") or []]
        text = _visible_text(self.default)
        slot_labels = {key: label for key, label in zip(
            ("head", "chest", "weapon1", "hands", "waist", "feet", "ring1", "ring2", "amulet", "relic", "idol_altar"),
            SLOT_LABELS_ZH,
        )}
        for slot in order:
            label = slot_labels.get(slot, slot)
            self.assertIn(label, text, f"equipment slot label {label} missing")
        self.assertIn("未装备 / 来源未设置", text)
        self.assertNotIn("来源未提供", text, "empty off-hand needs one simple note only")

    def test_skills_and_passives_are_separate(self) -> None:
        action_bar = self.spec["action_bar"]
        hud_names = [slot["ref"]["name"] for slot in action_bar["slots"]]
        text = _visible_text(self.default)
        for name in hud_names:
            self.assertIn(name, text)
        self.assertIn("技能栏", text)
        self.assertIn("专精技能", text)
        for specialization in self.spec["specializations"]:
            tree_name = (specialization.get("tree") or {}).get("name")
            if tree_name:
                self.assertIn(tree_name, text)
            if specialization.get("source_level") is not None:
                self.assertIn(f"技能等级 {specialization['source_level']}", text)
            if specialization.get("points_total") is not None:
                self.assertIn(f"已分配点数 {specialization['points_total']}", text)
        passive = self.spec["passive_tree"]
        self.assertIn("被动", text)
        self.assertIn(f"已分配点数 {passive['points_total']}", text)

    def test_idols_and_blessings_are_visible(self) -> None:
        text = _visible_text(self.default)
        self.assertIn(f"神像（{len(self.spec['idols']['entries'])} 个）", text)
        self.assertIn("祝福（5 条）", text)  # 5 drop-only blessings are hidden from the default view
        self.assertEqual(len(self.spec["blessings"]["entries"]), 10)  # imported count unchanged
        for entry in self.spec["idols"]["entries"]:
            position = entry.get("position") or {}
            if isinstance(position.get("x"), int) and isinstance(position.get("y"), int):
                self.assertIn(f"位置 ({position['x']}, {position['y']})", text)

    # -- player-readable lines (new revision only) ------------------------
    def test_readable_equipment_lines(self) -> None:
        if not self.has_readable:
            self.skipTest("published revision has no readable layer yet (historical revision)")
        text = _visible_text(self.default)
        # review acceptance: percentage modifier semantics become player-readable
        self.assertIn("increased Mana", text)
        self.assertIn("T7", text)
        self.assertNotIn("未提供 roll", text, "player view must not use the engineering wording")
        # post-sync delivered revision: all values are actual, so no rendered data row
        # carries the missing-range marker (the overview explainer sentence still
        # mentions the wording).
        marked = [line for line in _lines(self.default) if "数值未提供（显示范围" in line]
        self.assertEqual(marked, [])

    def test_readable_unique_composition(self) -> None:
        if not self.has_readable:
            self.skipTest("published revision has no readable layer yet (historical revision)")
        html = self.html
        # P1-2: Apex of Thought must not repeat six identical rows anymore; its
        # tooltip description must be preserved as a combined description line.
        self.assertNotIn("Increased Ailment Duration 52%", html)
        apex_region = _card_region(html, "Apex of Thought")
        self.assertTrue(apex_region, "Apex of Thought card missing")
        self.assertIn('data-kind="description"', apex_region)
        # Withstand the Elements: the two Level of Skills rolls must be distinguishable.
        withstand_region = _card_region(html, "Withstand the Elements")
        if withstand_region:
            level_lines = _visible_text(
                "".join(
                    line
                    for line in re.findall(r'<li class="line[^>]*>.*?</li>', withstand_region, flags=re.DOTALL)
                    if "Level of" in line
                )
            )
            distinct = {part.strip() for part in re.split(r"(?=Level of)", level_lines) if "Level of" in part}
            self.assertGreaterEqual(len(distinct), 2, f"Level of Skills lines not distinguishable: {level_lines!r}")

    def test_generic_property_names_are_distinguishable(self) -> None:
        """P1-2 follow-up: multi-affix / generic property names keep their qualifier.

        The final revision fixed rows such as "Fire Resistance / Minion Fire
        Resistance" and "Ignite / Spreading Flames Duration"; both halves must be
        visible as separate, distinguishable player-readable lines.
        """
        if not self.has_readable:
            self.skipTest("published revision has no readable layer yet (historical revision)")
        pairs = (
            ("Increased Ignite Duration", "Increased Spreading Flames Duration"),
            ("Fire Resistance", "Minion Fire Resistance"),
            ("Fire Penetration", "Minion Fire Penetration"),
            ("All Resistances", "Minion All Resistances"),
        )
        found_pairs = 0
        for instance in _in_scope_instances(self.spec):
            readable = instance.get("readable") or {}
            lines = readable.get("lines")
            if not isinstance(lines, list):
                continue
            texts = [line.get("text") or "" for line in lines if isinstance(line, dict) and not line.get("hidden")]
            name = _spec_definition_name(instance)
            region = _card_region(self.html, name) if name else ""
            rows = [
                _visible_text(row)
                for row in re.findall(r'<li class="line[^"]*"[^>]*>.*?</li>', region, flags=re.DOTALL)
            ] if region else []
            for left, right in pairs:
                if not any(left in text for text in texts) or not any(right in text for text in texts):
                    continue
                found_pairs += 1
                self.assertTrue(region, f"card region missing for {name!r}")
                # the generic and the qualified line must be two separate rows
                self.assertTrue(
                    any(left in row and right not in row for row in rows),
                    f"{name!r}: no unqualified row for {left!r} (rows={rows!r})",
                )
                self.assertTrue(
                    any(right in row for row in rows),
                    f"{name!r}: no qualified row for {right!r} (rows={rows!r})",
                )
        self.assertGreaterEqual(
            found_pairs,
            5,
            "final revision should expose multiple distinguishable generic/minion property pairs",
        )

    def test_constant_lines_are_not_flagged(self) -> None:
        text = _visible_text(self.default)
        constant_lines = _lines(self.default, status="constant")
        if not constant_lines:
            self.skipTest("no constant line rendered in this revision")
        for line in constant_lines:
            self.assertNotIn("数值未提供", line, "constant lines must not be flagged as a missing roll")
            self.assertNotIn("数值未解析", line)

    def test_non_unique_items_have_no_unique_block(self) -> None:
        if not self.has_readable:
            self.skipTest("published revision has no readable layer yet (historical revision)")
        self.assertNotIn("暗金词条不适用", self.html)
        for instance in _in_scope_instances(self.spec):
            rarity = (instance.get("readable") or {}).get("rarity") or {}
            if rarity.get("code") == "unique":
                continue
            name = _spec_definition_name(instance)
            if not name:
                continue
            region = _card_region(self.html, name)
            if not region:
                continue
            self.assertNotIn("暗金词条", region, f"non-unique item {name!r} shows a unique block")

    def test_rarity_badge_respects_confidence(self) -> None:
        if not self.has_readable:
            self.skipTest("published revision has no readable layer yet (historical revision)")
        for instance in _in_scope_instances(self.spec):
            readable = instance.get("readable") or {}
            rarity = readable.get("rarity") or {}
            name = _spec_definition_name(instance)
            if not name:
                continue
            region = _card_region(self.html, name)
            if not region:
                continue
            confidence = str(rarity.get("confidence") or "").lower()
            label = rarity.get("label_zh") or "暗金"
            if confidence in CONFIRMED_RARITY and rarity:
                self.assertIn(label, region, f"confirmed rarity badge missing for {name!r}")
            else:
                self.assertNotIn("badge-unique", region, f"unconfirmed rarity must not show a badge for {name!r}")

    # -- scope change -----------------------------------------------------
    def test_weaver_is_ignored_in_default_view(self) -> None:
        text = _visible_text(self.default)
        self.assertNotIn("编织", text)
        self.assertNotIn("weaverTree", self.default)
        self.assertNotIn("weaver.nodes", self.default)
        self.assertNotIn('data-panel="weaver"', self.default)
        if self.has_scope:
            excluded = self.spec["scope"].get("excluded_features") or []
            self.assertTrue(any(item.get("feature") == "weaver_tree" for item in excluded))
            weaver = self.spec.get("weaver") or {}
            self.assertEqual(weaver.get("status"), "excluded_by_scope")
            self.assertFalse(weaver.get("nodes"), "new-scope weaver must not contain parsed nodes")
            self.assertFalse(weaver.get("containers"), "new-scope weaver must not contain parsed containers")

    def test_new_scope_counts(self) -> None:
        if not self.has_scope:
            self.skipTest("published revision still uses the old scope")
        counts = self._counts()
        self.assertEqual(counts["equipment"], 11)
        self.assertEqual(counts["idols"], 8)
        self.assertEqual(counts["blessings"], 10)
        self.assertEqual(counts["items_total"], 29)
        self.assertEqual(counts["affix_total"], 47)
        self.assertEqual(counts["passive_nodes"], 19)
        self.assertEqual(counts["skill_nodes"], 61)
        text = _visible_text(self.default)
        self.assertIn("物品 29/29", text)
        self.assertIn("词缀 47/47", text)

    # -- embedded data block ---------------------------------------------
    def test_embedded_data_block_is_valid_json(self) -> None:
        match = DATA_BLOCK_RE.search(self.html)
        self.assertIsNotNone(match, "embedded build-data JSON block missing")
        data = json.loads(match.group(1))
        self.assertEqual(data["build_id"], self.spec["meta"]["build_id"])
        self.assertEqual(data["revision_id"], self.revision_id)
        self.assertEqual(data["catalog"]["dataset_id"], self.spec["catalog"]["dataset_id"])
        self.assertIn("coverage", data)
        self.assertIn("scope", data)
        self.assertIn("counts", data)
        # Escaped markup characters must not break out of the script element.
        self.assertNotIn("</script", match.group(1).lower())

    # -- determinism ------------------------------------------------------
    def test_output_is_deterministic(self) -> None:
        again = render_build_html(
            self.spec, revision_id=self.revision_id, build_dir=self.revision_dir
        )
        self.assertEqual(self.html, again)

    def _counts(self) -> dict:
        spec = self.spec
        instances = _in_scope_instances(spec)
        affix_total = 0
        affixes_ok = 0
        absent_rolls = 0
        for instance in instances:
            affixes = instance.get("affixes") or {}
            affix_total += sum(len(entries or []) for entries in affixes.values())
            for channel in ("normal", "sealed", "primordial", "corrupted"):
                for affix in affixes.get(channel) or []:
                    if (affix.get("affix") or {}).get("ref"):
                        affixes_ok += 1
                    roll = affix.get("roll") or {}
                    if roll.get("raw") is None and str(roll.get("status") or "").lower() in ("", "absent", "null", "none"):
                        absent_rolls += 1
        items_ok = 0
        for instance in instances:
            if instance.get("readable"):
                items_ok += 1
            elif instance.get("definition_status") == "resolved" or instance.get("base_status") == "resolved":
                items_ok += 1
        specializations = spec.get("specializations") or []
        return {
            "equipment": len((spec.get("equipment") or {}).get("slots") or []),
            "idols": len((spec.get("idols") or {}).get("entries") or []),
            "blessings": len((spec.get("blessings") or {}).get("entries") or []),
            "items_total": len(instances),
            "items_ok": items_ok,
            "affix_total": affix_total,
            "affixes_ok": affixes_ok,
            "absent_rolls": absent_rolls,
            "passive_nodes": len((spec.get("passive_tree") or {}).get("nodes") or []),
            "skill_nodes": sum(len((entry or {}).get("nodes") or []) for entry in specializations),
        }


# ---------------------------------------------------------------------------
# source-only baseline revision (pre-sync demo backing the 15/17 derivation)
# ---------------------------------------------------------------------------
class BaselineInputDerivationTests(unittest.TestCase):
    """The preserved source-only revision backs the input derivation/panel tests.

    The delivered current revision has all 17 variable rows filled by local inputs,
    so the source-only baseline ``rev-b0f2782d178b73dd`` is rendered explicitly here
    for the 15 groups / 17 rows derivation, bounds metadata, pending-row UI and
    no-manual-badge assertions.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if BASELINE is None:
            raise unittest.SkipTest("baseline revision rev-b0f2782d178b73dd is not available")
        cls.spec, cls.revision_id, cls.revision_dir = BASELINE
        cls.html = render_build_html(cls.spec, revision_id=cls.revision_id, build_dir=cls.revision_dir)
        cls.default = _default_region(cls.html)
        cls.diagnostics = _diagnostics_region(cls.html)
        cls.has_readable = _has_readable(cls.spec)
        cls.has_scope = isinstance(cls.spec.get("scope"), dict) and bool(cls.spec.get("scope"))

    def test_baseline_revision_is_source_only(self) -> None:
        self.assertEqual(self.revision_id, BASELINE_REVISION_ID)
        self.assertFalse(self.spec.get("meta", {}).get("local_inputs"))
        manual = [
            line
            for instance in _in_scope_instances(self.spec)
            for line in ((instance.get("readable") or {}).get("lines") or [])
            if str(((line.get("diagnostics") or {}).get("value_source") or "")).startswith("local_")
        ]
        self.assertEqual(manual, [])
        self.assertEqual(self.html.count('<span class="badge badge-manual"'), 0)
        self.assertNotIn("本地手动输入", _visible_text(self.default))

    def test_baseline_pending_rows_and_missing_wording(self) -> None:
        self.assertIn("数值未提供（显示范围", _visible_text(self.default))
        panel = _input_panel_region(self.html)
        panel_text = _visible_text(panel)
        self.assertIn("15 组 / 17 个变量", panel_text)
        self.assertEqual(self.html.count('class="input-display"'), 17)
        self.assertEqual(self.html.count('class="input-roll"'), 15)
        self.assertIn("与同一 roll 槽共享", panel_text)
        self.assertIn('data-input-download', panel)

    def test_baseline_embedded_revision_and_determinism(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        self.assertEqual(data["revision_id"], BASELINE_REVISION_ID)
        self.assertEqual(data["local_inputs_template"]["counts"], {"groups": 15, "variables": 17})
        again = render_build_html(self.spec, revision_id=self.revision_id, build_dir=self.revision_dir)
        self.assertEqual(self.html, again)

    def test_baseline_drop_rate_filtering(self) -> None:
        text = _visible_text(self.default)
        self.assertNotIn("Drop Rate", self.default)
        self.assertIn("祝福（5 条）", text)
        self.assertIn("仅掉落率类别的非战斗效果不进入默认 BD 展示", text)
        self.assertIn("65% Increased Drop Rate", self.diagnostics)
        panel_text = _visible_text(_input_panel_region(self.html))
        self.assertIn("15 组 / 17 个变量", panel_text)

    def test_input_suggestions_are_range_maximums(self) -> None:
        """The placeholder/hint must suggest the declared range maximum (inside range)."""
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        variables_by_key = {
            variable["key"]: variable
            for group in data["local_inputs_template"]["groups"]
            for variable in group["variables"]
        }
        rows = re.findall(
            r'<tr class="input-row input-variable-row"[^>]*data-variable-key="([^"]+)"[^>]*>(.*?)</tr>',
            self.html,
            re.S,
        )
        self.assertEqual(len(rows), 17)
        for key, row_html in rows:
            variable = variables_by_key[key]
            maximum = float(variable["display_max"])
            expected = f"{round(maximum, 4):g}"
            placeholder = re.search(r'placeholder="([^"]+)"', row_html).group(1)
            hint = re.search(r'<span class="input-var-hint muted">(.*?)</span>', row_html).group(1)
            self.assertIn(expected, placeholder, (key, variable))
            self.assertIn("范围最高值", hint, (key, variable))
            self.assertIn(expected, hint, (key, variable))
            suggested = float(re.search(r"[\d.]+", placeholder).group(0))
            self.assertGreaterEqual(suggested, float(variable["display_min"]), variable)
            self.assertLessEqual(suggested, maximum, variable)
        # a few explicit spot checks across flat/percent ranges
        spot = {variable["key"]: variable for variable in variables_by_key.values()}
        self.assertTrue(any(v["label"].endswith("Level of Skills") and v["display_max"] == 2.0 for v in spot.values()))
        self.assertTrue(any("Physical Resistance" in v["label"] and v["display_max"] == 45.0 for v in spot.values()))
        self.assertTrue(any("Spell Damage" in v["label"] and v["display_max"] == 200.0 for v in spot.values()))

    # -- local missing-value inputs (build-local-inputs-v1) ---------------
    def test_local_input_groups_and_variables_derived(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        template = data["local_inputs_template"]
        self.assertEqual(template["version"], "build-local-inputs-v1")
        self.assertEqual(template["build_id"], self.spec["meta"]["build_id"])
        self.assertIn("le_build input import --build", template["import_command"])
        self.assertIn("le_build sync --build", template["import_command"])
        groups = template["groups"]
        self.assertEqual(len(groups), 15, f"expected 15 variable-level target groups, got {len(groups)}")
        self.assertEqual(template["counts"], {"groups": 15, "variables": 17})
        self.assertEqual(sum(len(group["variables"]) for group in groups), 17)
        kinds: dict[str, int] = {}
        for group in groups:
            kinds[group["kind"]] = kinds.get(group["kind"], 0) + 1
            self.assertTrue(group["key"])
            self.assertTrue(group["instance_path"])
            self.assertTrue(group["variables"], group)
            for variable in group["variables"]:
                self.assertTrue(variable["key"])
                self.assertTrue(variable["label"])
        self.assertEqual(kinds.get("affix_roll"), 4)
        self.assertEqual(kinds.get("unique_mod_roll"), 11)
        self.assertEqual(kinds.get("implicit_roll", 0), 0)
        self.assertEqual(len({group["key"] for group in groups}), len(groups))
        # constant-1% hands affixes must NOT be listed as targets
        item_labels = {group["item_label"] for group in groups}
        self.assertNotIn("Withstand the Elements", item_labels)
        group_paths = {group["instance_path"] for group in groups}
        self.assertNotIn("data.equipment.hands.sealedAffix", group_paths)
        self.assertNotIn("data.equipment.hands.primordialAffix", group_paths)
        # Omnis rollId 0 = one group with two independently fillable variables
        omnis_groups = [g for g in groups if g["item_label"] == "Omnis" and g.get("roll_id") == 0]
        self.assertEqual(len(omnis_groups), 1)
        omnis = omnis_groups[0]
        self.assertEqual([v["label"] for v in omnis["variables"]], ["Level of Skills", "Physical Resistance"])
        self.assertFalse(omnis["variables"][0]["percent"])
        self.assertEqual(omnis["variables"][0]["range_text"], "1–2")
        self.assertTrue(omnis["variables"][1]["percent"])
        self.assertEqual(omnis["variables"][1]["range_text"], "1%–45%")
        self.assertEqual(omnis["variables"][1]["display_min"], 1.0)
        self.assertEqual(omnis["variables"][1]["display_max"], 45.0)
        # Unsated Rage rollId 3 shares Strength + Vitality
        unsated_groups = [g for g in groups if g["item_label"] == "Unsated Rage" and g.get("roll_id") == 3]
        self.assertEqual(len(unsated_groups), 1)
        self.assertEqual([v["label"] for v in unsated_groups[0]["variables"]], ["Strength", "Vitality"])
        # relic primordial affix: only the missing Spell Damage variable (index 1) is a row
        primordial_groups = [g for g in groups if g["instance_path"] == "data.equipment.relic.primordialAffix"]
        self.assertEqual(len(primordial_groups), 1)
        primordial_variables = primordial_groups[0]["variables"]
        self.assertEqual(len(primordial_variables), 1)
        self.assertEqual(primordial_variables[0]["affix_value_index"], 1)
        self.assertIn("Spell Damage", primordial_variables[0]["label"])
        self.assertEqual(primordial_variables[0]["range_text"], "160%–200%")
        self.assertEqual((primordial_variables[0]["display_min"], primordial_variables[0]["display_max"]), (160.0, 200.0))
        roll_ids: dict[str, set[int]] = {}
        for group in groups:
            if group["kind"] == "unique_mod_roll":
                roll_ids.setdefault(group["item_label"], set()).add(group["roll_id"])
        self.assertEqual(roll_ids.get("Omnis"), {0, 1, 2, 3, 4, 5, 6})
        self.assertEqual(roll_ids.get("Unsated Rage"), {0, 1, 2, 3})

    def test_local_input_panel_ui_default_closed(self) -> None:
        match = re.search(r'<details[^>]*id="local-inputs"[^>]*>', self.html)
        self.assertIsNotNone(match)
        self.assertNotIn(" open", match.group(0), "补充缺失数值 panel must default closed")
        self.assertIn("补充缺失数值 / 手动输入", self.html)
        self.assertIn("15 组 / 17 个变量", _visible_text(self.html))
        display_inputs = re.findall(r'<input class="input-display" type="text"[^>]*>', self.html)
        self.assertEqual(len(display_inputs), 17)
        roll_inputs = re.findall(r'<input class="input-roll" type="number"[^>]*>', self.html)
        self.assertEqual(len(roll_inputs), 15)
        for input_html in roll_inputs:
            self.assertIn('min="0"', input_html)
            self.assertIn('max="255"', input_html)
        self.assertIn("data-row-error", self.html)
        self.assertIn('data-input-download', self.html)
        self.assertIn('data-input-copy', self.html)
        panel_match = re.search(re.escape('<details id="local-inputs"'), self.html)
        self.assertIsNotNone(panel_match)
        panel_text = _visible_text(_input_panel_region(self.html))
        for label in ("Omnis", "Unsated Rage", "Rune Quill", "Level of Skills", "Physical Resistance",
                      "Strength", "Vitality", "increased Spell Damage", "按 roll 品质输入（进阶）"):
            self.assertIn(label, panel_text)
        for range_text in ("1–2", "1%–45%", "160%–200%", "40%–60%", "4–8"):
            self.assertIn(range_text, panel_text)
        self.assertIn("le_build input import --build", panel_text)
        self.assertIn("le_build sync --build", panel_text)
        # all machine locators stay in the embedded block, not in the default business view
        self.assertNotIn("data.equipment", _default_region(self.html))

    def test_local_input_download_schema_explicit_entries(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        template = data["local_inputs_template"]
        groups = template["groups"]
        omnis = next(g for g in groups if g["item_label"] == "Omnis" and g.get("roll_id") == 0)
        variables = omnis["variables"]
        typed_values = [2, 35]
        entries: list[dict] = []
        for index, (variable, typed) in enumerate(zip(variables, typed_values), start=1):
            entries.append(
                {
                    "input_id": f"in-20261103T233000-{index:04d}",
                    "kind": "explicit_value",
                    "target": "unique_mod",
                    "instance_path": omnis["instance_path"],
                    "mod_index": variable["mod_index"],
                    "value_numeric": typed / 100.0 if variable["percent"] else typed,
                    "display_value": typed,
                    "display_unit": "percent" if variable["percent"] else "flat",
                    "created_at": "2026-11-03T23:30:00+08:00",
                }
            )
        document = {
            "version": template["version"],
            "build_id": template["build_id"],
            "updated_at": "2026-11-03T23:30:00+08:00",
            "entries": entries,
        }
        self.assertEqual(document["version"], "build-local-inputs-v1")
        self.assertEqual(document["build_id"], self.spec["meta"]["build_id"])
        self.assertEqual(len(document["entries"]), 2)
        flat, percent = document["entries"]
        self.assertEqual(flat["kind"], "explicit_value")
        self.assertEqual(flat["target"], "unique_mod")
        self.assertEqual(flat["instance_path"], "data.equipment.amulet")
        self.assertEqual(flat["mod_index"], 0)
        self.assertEqual(flat["value_numeric"], 2.0)
        self.assertEqual(flat["display_value"], 2)
        self.assertEqual(flat["display_unit"], "flat")
        self.assertEqual(percent["mod_index"], 1)
        self.assertAlmostEqual(percent["value_numeric"], 0.35)
        self.assertEqual(percent["display_value"], 35)
        self.assertEqual(percent["display_unit"], "percent")
        self.assertEqual(len({entry["input_id"] for entry in document["entries"]}), 2)
        # advanced roll-only mode for a group without display values emits the roll entry
        roll_group = next(group for group in groups if group["kind"] == "affix_roll")
        roll_entry = {
            "input_id": "in-20261103T233000-0003",
            "kind": roll_group["kind"],
            "instance_path": roll_group["instance_path"],
            "roll": 128,
            "created_at": "2026-11-03T23:30:00+08:00",
        }
        self.assertEqual(roll_entry["kind"], "affix_roll")
        self.assertGreaterEqual(roll_entry["roll"], 0)
        self.assertLessEqual(roll_entry["roll"], 255)
        # download/copy affordances are wired in the page
        self.assertIn("inputs.json", self.html)
        self.assertIn("triggerDownload", self.html)

    def test_local_input_bounds_metadata_and_rules(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        groups = data["local_inputs_template"]["groups"]
        omnis = next(g for g in groups if g["item_label"] == "Omnis" and g.get("roll_id") == 0)
        flat, percent = omnis["variables"]
        self.assertEqual((flat["display_min"], flat["display_max"]), (1.0, 2.0))
        self.assertEqual((percent["display_min"], percent["display_max"]), (1.0, 45.0))
        primordial = next(g for g in groups if g["instance_path"] == "data.equipment.relic.primordialAffix")
        self.assertEqual((primordial["variables"][0]["display_min"], primordial["variables"][0]["display_max"]), (160.0, 200.0))

        def evaluate(variable: dict, typed):
            """Python mirror of the browser-side validate + normalize rules."""
            text = str(typed).strip()
            if text == "":
                return ("empty", None)
            if variable["percent"] and text.endswith("%"):
                text = text[:-1].strip()
            try:
                number = float(text)
            except ValueError:
                return ("invalid", None)
            if number != number or number in (float("inf"), float("-inf")):
                return ("invalid", None)
            low, high = variable["display_min"], variable["display_max"]
            includes_negative = isinstance(low, (int, float)) and low < 0
            if number < 0 and not includes_negative:
                return ("out_of_range", None)
            if isinstance(low, (int, float)) and number < low - 1e-9:
                return ("out_of_range", None)
            if isinstance(high, (int, float)) and number > high + 1e-9:
                return ("out_of_range", None)
            return ("ok", number / 100.0 if variable["percent"] else number)

        self.assertEqual(evaluate(percent, 35), ("ok", 0.35))
        self.assertEqual(evaluate(percent, "35%"), ("ok", 0.35))
        self.assertEqual(evaluate(percent, 1), ("ok", 0.01))
        self.assertEqual(evaluate(percent, 45), ("ok", 0.45))
        self.assertEqual(evaluate(percent, 90), ("out_of_range", None))
        self.assertEqual(evaluate(percent, 45.1), ("out_of_range", None))
        self.assertEqual(evaluate(percent, -1), ("out_of_range", None))
        self.assertEqual(evaluate(flat, 2), ("ok", 2.0))
        self.assertEqual(evaluate(flat, 1), ("ok", 1.0))
        self.assertEqual(evaluate(flat, 3), ("out_of_range", None))
        self.assertEqual(evaluate(primordial["variables"][0], 180), ("ok", 1.8))
        self.assertEqual(evaluate(primordial["variables"][0], 160), ("ok", 1.6))
        self.assertEqual(evaluate(primordial["variables"][0], 200), ("ok", 2.0))
        self.assertEqual(evaluate(primordial["variables"][0], 159), ("out_of_range", None))
        self.assertEqual(evaluate(primordial["variables"][0], 201), ("out_of_range", None))

    def test_absent_rolls_are_honest(self) -> None:
        text = _visible_text(self.default)
        missing = _missing_roll_affixes(self.spec)
        self.assertGreater(len(missing), 0, "fixed sample is expected to keep source-missing rolls")
        self.assertIn("数值未提供", text)
        self.assertNotIn("未提供 roll", text)
        # No fabricated 0 roll marker in the player view.
        self.assertNotIn("r=0", text)

# ---------------------------------------------------------------------------
# legacy property-104 fallback (revision imported before diagnostics.non_dps)
# ---------------------------------------------------------------------------
class LegacyDropFallbackTests(unittest.TestCase):
    """The 2026-11-04 filter must also work on revisions without the non_dps flag."""

    @classmethod
    def setUpClass(cls) -> None:
        if LEGACY is None:
            raise unittest.SkipTest("legacy revision rev-5e0e70bf7a26b3a5 is not available")
        cls.spec, cls.revision_id, cls.revision_dir = LEGACY
        cls.html = render_build_html(cls.spec, revision_id=cls.revision_id, build_dir=cls.revision_dir)
        cls.default = _default_region(cls.html)
        cls.diagnostics = _diagnostics_region(cls.html)

    def test_legacy_revision_filters_by_property_identity(self) -> None:
        drop_lines = [
            line
            for instance in _in_scope_instances(self.spec)
            for line in ((instance.get("readable") or {}).get("lines") or [])
            if _is_non_dps_line(line)
        ]
        self.assertEqual(len(drop_lines), 5)
        for line in drop_lines:
            diagnostics = line.get("diagnostics") or {}
            self.assertNotEqual(diagnostics.get("non_dps"), True)  # legacy revision has no flag
            self.assertEqual(diagnostics.get("property"), 104)
            self.assertTrue(_is_non_dps_line(line))
        self.assertNotIn("Drop Rate", self.default)
        self.assertIn("65% Increased Drop Rate", self.diagnostics)
        text = _visible_text(self.default)
        self.assertIn("祝福（5 条）", text)
        for dropped in (
            "Grand Scales of Greed",
            "Grand Talon of Grandeur",
            "Grand Subtlety of Slaughter",
            "Hope of the Beginning",
            "Grand Memory of Masters",
        ):
            self.assertNotIn(dropped, text)
            self.assertIn(dropped, self.diagnostics)


# ---------------------------------------------------------------------------
# historical revision (backward compatibility)
# ---------------------------------------------------------------------------
class HistoricalRevisionViewerTests(unittest.TestCase):
    """The old immutable revision must stay readable: old scope, weaver, no readable."""

    @classmethod
    def setUpClass(cls) -> None:
        if HISTORICAL is None:
            raise unittest.SkipTest("historical revision is not available")
        cls.spec, cls.revision_dir = HISTORICAL
        cls.html = render_build_html(cls.spec, revision_id=HISTORICAL_REVISION_ID, build_dir=cls.revision_dir)
        cls.default = _default_region(cls.html)
        cls.diagnostics = _diagnostics_region(cls.html)

    def test_renders_four_business_panels_with_historical_banner(self) -> None:
        self.assertEqual(self.html.count('<section class="panel"'), 4)
        text = _visible_text(self.default)
        self.assertIn("历史 revision（旧范围/无可读词条层）", text)
        for label in ("构筑概览", "装备", "技能与被动", "神像与祝福"):
            self.assertIn(label, text)

    def test_historical_default_has_no_engineering_strings(self) -> None:
        for forbidden in ("rev-", "sha256", "tier_index", "codec"):
            self.assertNotIn(forbidden, self.default, f"default view leaks {forbidden!r}")
        self.assertNotIn("编织", _visible_text(self.default))

    def test_historical_fallback_lines_are_player_oriented(self) -> None:
        text = _visible_text(self.default)
        self.assertIn("increased Mana Regen", text)  # 0.27 -> 27% increased Mana Regen
        self.assertIn("T7", text)
        self.assertIn("数值未提供（显示范围", text)  # source-missing rolls keep their range
        self.assertNotIn("未提供 roll", text)

    def test_historical_weaver_note_lives_in_diagnostics(self) -> None:
        self.assertIn("该 revision 属于旧范围（包含编织者树），已按当前范围忽略", self.diagnostics)
        self.assertIn(HISTORICAL_REVISION_ID, self.diagnostics)
        self.assertIn("weaver", self.diagnostics)
        weaver = self.spec.get("weaver") or {}
        self.assertTrue(weaver.get("nodes") or weaver.get("containers"))

    def test_historical_lines_have_no_internal_ids(self) -> None:
        text = _visible_text(self.default)
        self.assertNotIn("encoded_id", text)
        self.assertNotIn("source_path", text)
        self.assertNotIn("tier_index", text)
        self.assertNotIn("ir=[", text)


# ---------------------------------------------------------------------------
# synthetic specs: IA / readable contract / degradation
# ---------------------------------------------------------------------------
class SyntheticViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _synthetic_spec()
        cls.html = _render(cls.spec, "rev-synthetic-000000000000")
        cls.default = _default_region(cls.html)
        cls.diagnostics = _diagnostics_region(cls.html)
        cls.text = _visible_text(cls.default)

    def test_four_business_areas_only(self) -> None:
        self.assertEqual(self.html.count('<section class="panel"'), 4)
        self.assertEqual(len(re.findall(r'data-tab="[a-z]+"', self.html)), 4)
        for label in ("构筑概览", "装备", "技能与被动", "神像与祝福"):
            self.assertIn(label, self.text)
        self.assertNotIn("编织", self.text)
        self.assertNotIn("weaverTree", self.default)

    def test_weaver_excluded_scope_in_diagnostics(self) -> None:
        self.assertIn("weaver_tree", self.diagnostics)
        self.assertIn("excluded_by_scope", self.diagnostics)
        # new-scope revision: no old-scope banner for weaver in diagnostics either
        self.assertNotIn("该 revision 属于旧范围", self.diagnostics)

    def test_readable_lines_and_tier_labels(self) -> None:
        self.assertIn("22% increased Mana", self.text)
        line_html = next(line for line in _lines(self.default) if "22% increased Mana" in line)
        self.assertIn("T7", line_html, "player-readable affix line must carry its T7 label")
        self.assertIn("109 Ward Decay Threshold", self.text)
        self.assertIn("Apex of Thought combined description text", self.text)

    def test_absent_roll_wording_attached_to_line(self) -> None:
        sealed = next(line for line in _lines(self.default, status="range") if "Fire Resistance" in line)
        self.assertIn("数值未提供（显示范围 61%–75%）", sealed)
        # the readable layer's "(range)" prefix is folded into the marker, not duplicated
        self.assertIn("increased Fire Resistance", sealed)
        self.assertNotIn("(61%–75%)", sealed)
        # leading value connectors are tidied so the marker explains the range
        connector = next(line for line in _lines(self.default, status="range") if "Level of Skills" in line)
        self.assertIn("数值未提供（显示范围 1–2）", connector)
        self.assertNotIn("+ to", connector)
        self.assertNotIn(">+ ", connector)
        # combined descriptions keep their substituted range verbatim and get the marker
        description = next(line for line in _lines(self.default) if "You deal 10%–14% more damage" in line)
        self.assertIn("数值未提供（显示范围 10%–14%）", description)
        # constant line never carries the missing-roll marker
        constant = next(line for line in _lines(self.default, status="constant") if "77 Armor" in line)
        self.assertNotIn("数值未提供", constant)
        # actual line never carries the missing-roll marker
        actual = next(line for line in _lines(self.default, status="actual") if "22% increased Mana" in line)
        self.assertNotIn("数值未提供", actual)

    def test_hidden_lines_are_not_rendered_as_standalone_rows(self) -> None:
        self.assertNotIn("hidden internal value", self.default)
        # but they stay traceable in diagnostics
        self.assertIn("hidden internal value", self.diagnostics)

    def test_non_unique_item_has_no_unique_block(self) -> None:
        self.assertNotIn("暗金词条不适用", self.html)
        plain = _card_region(self.html, "Plain Chest")
        self.assertTrue(plain)
        self.assertNotIn("暗金词条", plain)
        unique = _card_region(self.html, "Test Unique")
        self.assertIn("暗金词条", unique)
        self.assertIn("Apex of Thought combined description text", unique)

    def test_rarity_badge_only_when_confirmed(self) -> None:
        unique_region = _card_region(self.html, "Test Unique")
        self.assertIn("暗金", unique_region)
        unconfirmed_region = _card_region(self.html, "Unconfirmed Ring")
        self.assertTrue(unconfirmed_region)
        self.assertNotIn("badge-unique", unconfirmed_region)

    def test_empty_offhand_has_single_simple_note(self) -> None:
        self.assertIn("副手 / 双持", self.text)
        self.assertEqual(
            self.text.count("未装备 / 来源未设置"),
            1,
            "empty off-hand must render exactly one simple note",
        )
        self.assertNotIn("来源未提供该槽位", self.html)

    def test_skill_level_vs_allocated_points(self) -> None:
        self.assertIn("技能等级 20", self.text)
        self.assertIn("已分配点数 15", self.text)
        self.assertIn("专精槽 1", self.text)
        self.assertIn("仅技能栏", self.text)
        self.assertIn("被动", self.text)
        self.assertIn("已分配点数 8", self.text)

    def test_default_view_has_no_engineering_strings(self) -> None:
        for forbidden in ("rev-", "sha256", "tier_index", "codec", "snapshot", "fingerprint", "source_path", "encoded_id"):
            self.assertNotIn(forbidden, self.default, f"default view leaks {forbidden!r}")
        for forbidden in ("build_id", "dataset", "coverage", "issues"):
            self.assertNotIn(forbidden, self.text, f"default visible text leaks {forbidden!r}")

    def test_diagnostics_traceability_and_export(self) -> None:
        self.assertIn("rev-synthetic-000000000000", self.diagnostics)
        self.assertIn("UAwJgzGCMBsQ", self.diagnostics)
        self.assertIn("data.equipment.head", self.diagnostics)
        self.assertIn("tier_index", self.diagnostics)
        self.assertIn("[234, 167, 203]", self.html.replace("\\u003c", "<").replace("\\u003e", ">"))  # ir preserved
        self.assertIn('data-export-raw="build-raw"', self.html)
        self.assertIn('data-export-raw="build-data"', self.html)
        raw = json.loads(RAW_BLOCK_RE.search(self.html).group(1))
        self.assertEqual(raw["meta"]["build_id"], "letools:synthetic")

    def test_diagnostics_contains_issues_and_severity_filter(self) -> None:
        self.assertIn("synthetic_issue", self.diagnostics)
        self.assertIn('data-severity="warning"', self.html)

    def test_embedded_data_block(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        self.assertEqual(data["build_id"], "letools:synthetic")
        self.assertEqual(data["revision_id"], "rev-synthetic-000000000000")
        self.assertEqual(data["catalog"]["dataset_id"], "dataset-synthetic")
        self.assertEqual(data["mode"], "readable")
        self.assertFalse(data["historical"])
        self.assertEqual(data["counts"]["items_total"], 5)

    def test_output_is_deterministic(self) -> None:
        again = _render(self.spec, "rev-synthetic-000000000000")
        self.assertEqual(self.html, again)

    # -- local missing-value inputs (synthetic) ---------------------------
    def test_local_input_groups_synthetic(self) -> None:
        data = json.loads(DATA_BLOCK_RE.search(self.html).group(1))
        template = data["local_inputs_template"]
        groups = template["groups"]
        self.assertEqual(len(groups), 3)
        self.assertEqual(template["counts"], {"groups": 3, "variables": 2})
        self.assertEqual(sorted(group["kind"] for group in groups), ["affix_roll", "implicit_roll", "unique_mod_roll"])
        affix = next(group for group in groups if group["kind"] == "affix_roll")
        self.assertEqual(affix["instance_path"], "data.equipment.head.sealedAffix")
        self.assertEqual(len(affix["variables"]), 1)
        self.assertEqual(affix["variables"][0]["range_text"], "61%–75%")
        self.assertTrue(affix["variables"][0]["percent"])
        self.assertEqual((affix["variables"][0]["display_min"], affix["variables"][0]["display_max"]), (61.0, 75.0))
        unique = next(group for group in groups if group["kind"] == "unique_mod_roll")
        self.assertEqual(unique["instance_path"], "data.equipment.head")
        self.assertEqual(unique["roll_id"], 2)
        self.assertEqual(len(unique["variables"]), 1)
        self.assertEqual(unique["variables"][0]["mod_index"], 1)
        self.assertIn("Physical Resistance", unique["variables"][0]["label"])
        self.assertEqual((unique["variables"][0]["display_min"], unique["variables"][0]["display_max"]), (1.0, 45.0))
        implicit = next(group for group in groups if group["kind"] == "implicit_roll")
        self.assertEqual(implicit["instance_path"], "data.equipment.chest")
        self.assertEqual(implicit["index"], 0)
        self.assertEqual(implicit["variables"], [])
        # one display input per variable + one advanced roll input per group
        self.assertEqual(self.html.count('class="input-display"'), 2)
        self.assertEqual(self.html.count('class="input-roll"'), 3)
        self.assertIn("补充缺失数值 / 手动输入", self.html)

    def test_manual_input_badge_and_overview_line(self) -> None:
        spec = _synthetic_spec()
        spec["meta"]["local_inputs"] = {
            "version": "build-local-inputs-v1",
            "path": "data/builds/letools/synthetic/inputs/local-inputs.json",
            "sha256": "ab" * 32,
            "entries": 2,
            "applied": 2,
            "inputs_manifest": [
                {
                    "input_id": "in-synthetic-0001",
                    "kind": "affix_roll",
                    "instance_path": "data.equipment.head.sealedAffix",
                    "applied": True,
                }
            ],
        }
        head = spec["equipment"]["slots"][0]["instance"]
        for line in head["readable"]["lines"]:
            if line["section"] == "affix_sealed":
                line["diagnostics"] = {
                    "value_source": "local_input",
                    "input_id": "in-synthetic-0001",
                    "stat_name": "Fire Resistance",
                }
                line["value_status"] = "actual"
                break
        html = _render(spec, "rev-synthetic-000000000000")
        default = _default_region(html)
        text = _visible_text(default)
        self.assertIn("本地手动输入：2 条", text)
        card = _card_region(html, "Test Unique")
        self.assertTrue(card)
        self.assertIn("badge-manual", card)
        self.assertIn("手动", _visible_text(card))
        self.assertIn("in-synthetic-0001", _diagnostics_region(html))
        # exactly the local-input line carries the badge
        manual_rows = re.findall(
            r'<li class="line[^"]*"[^>]*>(?:(?!</li>).)*?badge-manual(?:(?!</li>).)*?</li>',
            card,
            flags=re.DOTALL,
        )
        self.assertEqual(len(manual_rows), 1)

    def test_no_manual_input_line_when_absent(self) -> None:
        text = _visible_text(self.default)
        self.assertNotIn("本地手动输入", text)
        self.assertNotIn('<span class="badge badge-manual"', self.default)


class SyntheticDegradationTests(unittest.TestCase):
    """The viewer must degrade to explicit 未提供 states instead of raising."""

    def test_empty_spec_does_not_raise(self) -> None:
        html = _render({}, "rev-missing")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertEqual(html.count("<html"), 1)
        self.assertIn("未提供", html)
        self.assertEqual(html.count('<section class="panel"'), 4)

    def test_none_spec_does_not_raise(self) -> None:
        html = _render(None, "rev-none")  # type: ignore[arg-type]
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("未提供", html)
        data = json.loads(DATA_BLOCK_RE.search(html).group(1))
        self.assertEqual(data["revision_id"], "rev-none")

    def test_build_dir_may_be_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            html = render_build_html(
                {"meta": {"build_id": "letools:no-dir"}},
                revision_id="rev-no-dir",
                build_dir=missing,
            )
        self.assertIn("未提供", html)

    def test_structured_fallback_without_readable_layer(self) -> None:
        spec = _synthetic_spec()
        spec.pop("scope", None)  # simulate a historical revision (old scope)
        spec.pop("weaver", None)
        for instance in _in_scope_instances(spec):
            instance.pop("readable", None)
        html = _render(spec, "rev-fallback")
        default = _default_region(html)
        text = _visible_text(default)
        self.assertIn("历史 revision（旧范围/无可读词条层）", text)
        self.assertIn("+67% Fire Resistance", text)
        self.assertIn("increased Fire Resistance", text)
        self.assertIn("T7", text)
        self.assertNotIn("rev-", default)
        # fallback lines never fabricate the average/derived value as an actual roll
        self.assertIn("数值未提供（显示范围", text)

    def test_failed_readable_layer_degrades_to_structured_fields(self) -> None:
        spec = _synthetic_spec()
        head = spec["equipment"]["slots"][0]["instance"]
        head["readable"] = {
            "version": "letools-readable-v1",
            "display_name": "Test Unique",
            "completeness": "unavailable",
            "lines": [],
            "warnings": ["readable rendering failed"],
        }
        html = _render(spec, "rev-readable-failed")
        card = _card_region(html, "Test Unique")
        self.assertTrue(card)
        self.assertIn("Fire Resistance", card, "failed readable layer must degrade to structured lines")
        self.assertEqual(html.count('<section class="panel"'), 4)


CURRENT = _load_revision(CURRENT_REVISION_ID)
HISTORICAL = _load_historical()
BASELINE = _load_revision(BASELINE_REVISION_ID)
LEGACY = _load_revision(LEGACY_FLAGLESS_REVISION_ID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
