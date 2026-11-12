"""Player-readable layer acceptance tests (2026-11-03 review P1-1 .. P1-4, P2-1;
2026-11-04 re-review R1 ward event / drop-rate non-DPS tags, R2 hideModifierValue).

The expected texts below are **independent expectations**: they are derived from

* the archived LE Tools item-card display rules (``reports/discovery/phase-2-o3ZRm9my/
  planner-runtime.js`` functions ``Ea``/``kb``/``Za``/``na``/``$a`` and the
  ``item_db.item_card.property.display_template.*`` / ``level_of_skills.*`` translations),
* the restored roll values documented in ``reports/phase-2/p2-4-value-restoration.zh-CN.md``
  (hand-computed from the archived ranges and the ``r``/``ir``/``ur`` arrays), and
* the rework contract ``docs/phase-2-rework-contract.zh-CN.md`` §2/§4.

They are not copy-pasted from the implementation output: e.g. ``11% increased Mana`` is
hand-derived from ``Mana -> modifier_type=1, value_numeric=0.11``; the tooltip-description
placeholder values ``52``/``346``/``63`` are hand-derived from the documented roll formula.

Two specs are built once against the real read-only catalog:

1. the fixed regression sample ``o3ZRm9my`` (archived response), so the review's sample
   facts (Unstable Core / Apex of Thought / Withstand the Elements / Omnis / Unsated Rage
   / the 10 blessings) are exercised end to end through ``BuildSpecBuilder``;
2. a small synthetic payload that reuses real catalog unique definitions for the two
   cases the fixed sample does not contain: a constant-only Unique with a missing ``ur``
   field (Scorched Earth, unique 485) and a ``more``-modifier case (Xithara's Conundrum,
   unique 133 with ``ur=[255]``).

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from le_build.builder import BuildSpecBuilder
from le_build.catalog import Catalog
from le_build.resolve import Resolver
from le_build.sources import letools

from _dataset_asset import require_dataset_db

REPO = Path(__file__).resolve().parents[3]
DISCOVERY = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my"
FIXTURE = DISCOVERY / "build-response.json"

READABLE_VERSION = "letools-readable-v1"
VALID_COMPLETENESS = {"complete", "partial", "definition_only", "unavailable"}
VALID_SECTIONS = {
    "implicit",
    "affix_normal",
    "affix_sealed",
    "affix_primordial",
    "affix_corrupted",
    "unique_mod",
    "unique_description",
}
VALID_KINDS = {"mod", "description"}
VALID_VALUE_STATUS = {"actual", "range", "constant", "not_provided", "unresolved"}

# Archived tooltip descriptions of the sample uniques (catalog translations, key
# Item_Names.Unique_Tooltip_*), used as expected text.
APEX_DESCRIPTION_0 = (
    "While standing on your Glyph of Dominion, 100% of your Ignite Chance is converted "
    "to Fire Resistance Shred Chance, 100% of your Chill Chance is converted to Cold "
    "Resistance Shred Chance, and 100% of your Shock Chance is converted to Lightning "
    "Resistance Shred Chance."
)
# Item_Names.Unique_Tooltip_1_316 = "[39,52,0]% increased Duration of Ignite, Chill,
# Shock, Fire Resistance Shred, Cold Resistance Shred, and Lightning Resistance Shred."
# ur[0]=244 on range 0.39..0.52 with the documented type=0 rounding rule -> 0.52 -> 52.
APEX_DESCRIPTION_1 = (
    "52% increased Duration of Ignite, Chill, Shock, Fire Resistance Shred, "
    "Cold Resistance Shred, and Lightning Resistance Shred."
)
# Item_Names.Unique_Tooltip_0_275 = "When you directly cast Elemental Nova 6 times
# without a 2 second gap, the 6th cast gains [200,350,3]% increased area and
# +[40,100,4] Spell Damage"; ur[3]=247 -> 3.46 -> 346, ur[4]=99 -> 63.
UNSTABLE_CORE_DESCRIPTION = (
    "When you directly cast Elemental Nova 6 times without a 2 second gap, the 6th cast "
    "gains 346% increased area and +63 Spell Damage"
)


# ---------------------------------------------------------------------------
# Synthetic helpers (self-contained; mirror the archived planner id codec rules).
# ---------------------------------------------------------------------------
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def reference_lz_compress(text: str, bits_per_char: int = 6) -> str:
    """Independent LZ-String compressToBase64 port (used to craft encoded ids)."""
    output: list[str] = []
    data_val = 0
    data_position = 0
    dictionary: dict[str, int] = {}
    to_create: dict[str, bool] = {}
    w = ""
    enlarge_in = 2
    dict_size = 3
    num_bits = 2

    def emit_bit(bit: int) -> None:
        nonlocal data_val, data_position
        data_val = (data_val << 1) | bit
        if data_position == bits_per_char - 1:
            output.append(ALPHABET[data_val])
            data_val = 0
            data_position = 0
        else:
            data_position += 1

    def emit_code(code: int, width: int) -> None:
        for _ in range(width):
            emit_bit(code & 1)
            code >>= 1

    def emit_literal(character: str) -> None:
        code = ord(character)
        for _ in range(num_bits):
            emit_bit(0)
        emit_code(code, 8 if code < 256 else 16)

    for character in text:
        if character not in dictionary:
            dictionary[character] = dict_size
            dict_size += 1
            to_create[character] = True
        wc = w + character
        if wc in dictionary:
            w = wc
            continue
        if w in to_create:
            emit_literal(w)
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
            del to_create[w]
        else:
            emit_code(dictionary[w], num_bits)
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        dictionary[wc] = dict_size
        dict_size += 1
        w = character
    if w:
        if w in to_create:
            emit_literal(w)
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
        else:
            emit_code(dictionary[w], num_bits)
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
    emit_code(2, num_bits)
    while True:
        data_val <<= 1
        if data_position == bits_per_char - 1:
            output.append(ALPHABET[data_val])
            break
        data_position += 1
    return "".join(output)


def craft_unique_id(base_subtype_id: int, unique_id: int) -> str:
    """U-codec id: marker U + compressed ``3-digit subtype + 3-digit unique id``."""
    if not (0 <= base_subtype_id <= 999 and 0 <= unique_id <= 999):
        raise ValueError("this helper only crafts 3+3 digit payloads")
    return "U" + reference_lz_compress(f"{base_subtype_id:03d}{unique_id:03d}")


def minimal_document() -> dict:
    data = {
        "dataVersion": "Version 1.5.2",
        "bio": {"level": 100, "characterClass": 1, "chosenMastery": 1},
        "equipment": {},
        "idols": [],
        "blessings": {},
        "charTree": {"selected": {}, "treeID": "", "version": 7},
        "skillTrees": [],
        "hud": [],
        "weaverTree": {"selected": {}, "treeID": "", "version": 10},
        "weaverTreeContainers": {},
        "activeBuffs": {"buffs": [], "ailments": [], "skills": []},
        "completedQuests": [],
        "conditions": [],
        "customConditions": [],
        "buffSettings": [],
        "charTreeProgression": [],
        "skillTreesProgression": [],
        "weaverTreeProgression": [],
        "newGrid": True,
        "weaponTypes": [],
        "fromSaveFile": 0,
        "gh": "0",
    }
    return {
        "data": data,
        "level": 100,
        "class": 1,
        "mastery": 1,
        "created_for_build": "Version 1.5.2",
        "data_version": "Version 1.5.2",
    }


def _load_restorer():
    """Mirror ``cli._load_restorer`` so the build is production-shaped."""
    from le_build import rolls as rolls_module

    required = ("restore_affix_value", "restore_implicit_values", "restore_unique_values")
    if all(callable(getattr(rolls_module, name, None)) for name in required):
        return rolls_module
    return None  # pragma: no cover - contract violation in P2-4


def _build(catalog, *, document, fixture_fetch=None, share_id: str) -> dict:
    if fixture_fetch is not None:
        fetch = fixture_fetch
    else:
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        fetch = {
            "mode": "file",
            "file_path": "<synthetic>",
            "raw_sha256": hashlib.sha256(body).hexdigest(),
            "raw_bytes": body,
            "document": document,
            "http_status": None,
            "api_url": None,
        }
    draft = letools.build_draft(fetch, share_id=share_id, requested_url=None)
    compatibility = letools.classify_versions(
        draft["versions"], catalog.meta().get("target_game_version") or ""
    )
    builder = BuildSpecBuilder(catalog, Resolver(catalog), restorer=_load_restorer())
    return builder.build(
        draft=draft,
        fetch_meta={
            "attempt_id": f"readable-{share_id}",
            "raw_path": f"data/builds/letools/{share_id}/raw/synthetic.json",
            "raw_sha256": fetch["raw_sha256"],
            "fetch_mode": "file",
        },
        catalog_compatibility={
            "status": compatibility["status"],
            "raw": compatibility["raw"],
            "normalized": compatibility["normalized"],
            "problems": compatibility["problems"],
            "warnings": compatibility["warnings"],
            "target_game_version": compatibility["target_game_version"],
        },
    )


# ---------------------------------------------------------------------------
# Spec accessors
# ---------------------------------------------------------------------------
def in_scope_instances(spec: dict) -> list[tuple[str, dict]]:
    """Every in-scope item instance: 11 equipment + 8 idols + 10 blessings."""
    instances: list[tuple[str, dict]] = []
    for slot in spec.get("equipment", {}).get("slots", []):
        instances.append((f"equipment.{slot.get('slot')}", slot.get("instance") or {}))
    for entry in spec.get("idols", {}).get("entries", []):
        instances.append((f"idols.{entry.get('index')}", entry.get("instance") or {}))
    for entry in spec.get("blessings", {}).get("entries", []):
        instances.append((f"blessings.{entry.get('timeline')}", entry.get("instance") or {}))
    return instances


def instances_by_path(spec: dict) -> dict[str, dict]:
    return dict(in_scope_instances(spec))


def readable(instance: dict) -> dict:
    return instance.get("readable") or {}


def lines(instance: dict, *, section: str | None = None, kind: str | None = None) -> list[dict]:
    result = list(readable(instance).get("lines") or [])
    if section is not None:
        result = [line for line in result if line.get("section") == section]
    if kind is not None:
        result = [line for line in result if line.get("kind") == kind]
    return result


def texts(instance: dict, **kwargs) -> list[str]:
    return [line.get("text") for line in lines(instance, **kwargs)]


def unique_mod_lines(instance: dict) -> list[dict]:
    return lines(instance, section="unique_mod")


def unique_description_lines(instance: dict) -> list[dict]:
    return lines(instance, section="unique_description")


def find_line(instance: dict, *, section: str | None = None, **diagnostics) -> dict | None:
    for line in lines(instance, section=section):
        diag = line.get("diagnostics") or {}
        if all(diag.get(key) == value for key, value in diagnostics.items()):
            return line
    return None


def struct_affix_by_source_path(instance: dict) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for channel, entries in (instance.get("affixes") or {}).items():
        for entry in entries or []:
            path = entry.get("source_path")
            if path:
                mapping[path] = entry
    return mapping


class ReadableLayerTest(unittest.TestCase):
    """The fixed regression sample through the real builder + read-only catalog."""

    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.pointer = json.loads((REPO / "data" / "current.json").read_text(encoding="utf-8"))
        cls.catalog = Catalog.open(
            root=REPO, dataset=cls.pointer["dataset_id"], verify=True
        )
        fetch = letools.load_archived_file(FIXTURE)
        cls.sample = _build(
            cls.catalog, document=None, fixture_fetch=fetch, share_id="o3ZRm9my"
        )
        cls.instances = instances_by_path(cls.sample)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()

    def instance(self, path: str) -> dict:
        return self.instances[path]

    # ------------------------------------------------------------- layer shape
    def test_every_in_scope_item_has_a_valid_readable_block(self) -> None:
        instances = in_scope_instances(self.sample)
        self.assertEqual(len(instances), 29)
        for path, instance in instances:
            block = readable(instance)
            self.assertTrue(block, f"{path}: missing readable block")
            self.assertEqual(block.get("version"), READABLE_VERSION, path)
            self.assertTrue(block.get("display_name"), f"{path}: display_name")
            self.assertIn(block.get("completeness"), VALID_COMPLETENESS, path)
            block_lines = block.get("lines")
            self.assertIsInstance(block_lines, list, path)
            self.assertTrue(block_lines, f"{path}: readable lines must not be empty")
            for line in block_lines:
                self.assertIn(line.get("section"), VALID_SECTIONS, path)
                self.assertIn(line.get("kind"), VALID_KINDS, path)
                self.assertIn(line.get("value_status"), VALID_VALUE_STATUS, path)
                self.assertIsInstance(line.get("text"), str, f"{path}: text")
                self.assertTrue(line["text"].strip(), f"{path}: empty text")
        summary = self.sample["readable_summary"]
        self.assertEqual(summary["version"], READABLE_VERSION)
        self.assertEqual(summary["items"], 29)
        self.assertEqual(sum(summary["by_completeness"].values()), 29)
        self.assertEqual(summary["lines_without_text"], 0)

    def test_readable_texts_are_free_of_placeholder_or_double_unit_artifacts(self) -> None:
        """A rendered line must never leak ``[a,b,rollId]`` or a doubled ``%%`` (P1-2)."""
        for path, instance in in_scope_instances(self.sample):
            for line in lines(instance):
                text = line.get("text") or ""
                self.assertNotIn("[", text, f"{path}: unresolved placeholder in {text!r}")
                self.assertNotIn("]", text, f"{path}: unresolved placeholder in {text!r}")
                self.assertNotIn("%%", text, f"{path}: doubled percent unit in {text!r}")

    # ------------------------------------------------------- P1-1 percentage
    def test_percent_modifier_sample_texts(self) -> None:
        chest = self.instance("equipment.chest")
        self.assertIn("11% increased Mana", texts(chest, section="implicit"))
        self.assertIn("22% increased Mana Regen", texts(chest, section="implicit"))
        head = self.instance("equipment.head")
        self.assertIn("27% increased Mana Regen", texts(head, section="implicit"))

        mana = find_line(chest, section="implicit", property=8)
        self.assertIsNotNone(mana)
        self.assertEqual(mana["text"], "11% increased Mana")
        # Internal normalized value stays 0.11; the display layer must not multiply it.
        self.assertAlmostEqual(mana["diagnostics"]["value_numeric"], 0.11, places=9)
        self.assertEqual(mana["diagnostics"]["modifier_type"], 1)

        regen = find_line(chest, section="implicit", property=18)
        self.assertEqual(regen["text"], "22% increased Mana Regen")
        self.assertAlmostEqual(regen["diagnostics"]["value_numeric"], 0.22, places=9)

        # Damage 0.68 must keep its tag qualifier (tags=128 -> Elemental), not become
        # an unconditional "all damage" line.
        damage = find_line(chest, section="unique_mod", property=0)
        self.assertIsNotNone(damage)
        self.assertEqual(damage["text"], "68% increased Elemental Damage")
        self.assertAlmostEqual(damage["diagnostics"]["value_numeric"], 0.68, places=9)
        # The structured roll value is also not scaled by 100.
        chest_mods = chest["unique_rolls"]["mods"]
        damage_mod = next(mod for mod in chest_mods if mod["roll_id"] == 0)
        self.assertAlmostEqual(damage_mod["restored"]["value_numeric"], 0.68, places=9)

    def test_added_increased_more_have_independent_expected_texts(self) -> None:
        # Same base property (Damage, property 0) for all three modifier types.
        # Added: Spine of Malatros mod property=0, type=0, tags=256 (Spell),
        # 61..72, ur[3]=90 -> 65, archived template base with + -> "+65 Spell Damage".
        weapon = self.instance("equipment.weapon1")
        added = [
            line
            for line in unique_mod_lines(weapon)
            if line["diagnostics"].get("property") == 0
            and line["diagnostics"].get("modifier_type") == 0
        ]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["text"], "+65 Spell Damage")

        # Increased: chest Unstable Core, type=1, value 0.68, tags=128 (Elemental).
        increased = find_line(self.instance("equipment.chest"), section="unique_mod", property=0)
        self.assertEqual(increased["text"], "68% increased Elemental Damage")

        # Added uses "+" and no word modifier; increased uses "% + increased".
        self.assertTrue(added[0]["text"].startswith("+"))
        self.assertNotIn("increased", added[0]["text"])
        self.assertIn("increased", increased["text"])

    # ------------------------------------------------------- P1-2 unique composition
    def test_withstand_the_elements_distinguishes_skill_level_tags(self) -> None:
        hands = self.instance("equipment.hands")
        level_lines = [
            line for line in unique_mod_lines(hands) if line["diagnostics"]["property"] == 88
        ]
        self.assertEqual(len(level_lines), 2)
        self.assertEqual(
            [line["text"] for line in level_lines],
            ["+1 to Level of Physical Skills", "+1 to Level of Cold Skills"],
        )
        # tags=1 -> Physical, tags=4 -> Cold: the two lines must be distinguishable.
        self.assertEqual([line["diagnostics"]["tags"] for line in level_lines], [1, 4])
        self.assertNotEqual(level_lines[0]["text"], level_lines[1]["text"])

    def test_apex_of_thought_composes_descriptions_instead_of_six_duplicate_mods(self) -> None:
        head = self.instance("equipment.head")
        mod_lines = unique_mod_lines(head)
        description_lines = unique_description_lines(head)

        # tooltipEntries = [0, 1, 128, 129, 3]: three visible mods, two descriptions,
        # six hidden property=42 mods must NOT be emitted as standalone lines.
        self.assertEqual(len(mod_lines), 3)
        self.assertEqual(len(description_lines), 2)
        for line in mod_lines:
            self.assertNotIn("Increased Ailment Duration", line["text"] or "")
        self.assertNotEqual(len({line["text"] for line in mod_lines}), 1)
        # Tooltip order is preserved.
        unique_sections = [
            line["section"]
            for line in readable(head)["lines"]
            if line["section"] in ("unique_mod", "unique_description")
        ]
        self.assertEqual(
            unique_sections,
            [
                "unique_mod",
                "unique_mod",
                "unique_description",
                "unique_description",
                "unique_mod",
            ],
        )
        self.assertEqual(description_lines[0]["text"], APEX_DESCRIPTION_0)
        self.assertEqual(description_lines[1]["text"], APEX_DESCRIPTION_1)
        self.assertEqual(description_lines[1]["value_status"], "actual")

        # Hidden mods are still retained in the structured data and counted.
        hidden_mods = [mod for mod in head["unique_rolls"]["mods"] if mod["hidden"]]
        self.assertEqual(len(hidden_mods), 7)
        self.assertEqual(readable(head)["unique_composition"]["hidden_mods"], 7)
        self.assertEqual(readable(head)["unique_composition"]["descriptions"], 2)

    def test_unstable_core_description_placeholders_use_restored_values(self) -> None:
        chest = self.instance("equipment.chest")
        descriptions = unique_description_lines(chest)
        self.assertEqual(len(descriptions), 1)
        self.assertEqual(descriptions[0]["text"], UNSTABLE_CORE_DESCRIPTION)
        self.assertEqual(descriptions[0]["value_status"], "actual")
        # tooltipEntries = [128, 1, 2, 3, 4, 5]: description first, then five mods.
        unique_sections = [
            line["section"]
            for line in readable(chest)["lines"]
            if line["section"] in ("unique_mod", "unique_description")
        ]
        self.assertEqual(
            unique_sections,
            ["unique_description"] + ["unique_mod"] * 5,
        )
        # The two hidden mods are expressed through the description, never as rows.
        self.assertEqual(
            {line["diagnostics"]["mod_index"] for line in unique_mod_lines(chest)},
            {1, 2, 3, 4, 5},
        )

    # ------------------------------------------------------------- P1-3 ur=[] 
    def test_ur_empty_uniques_show_definition_ranges_without_fabricated_actual(self) -> None:
        for path, expected_mods in (
            ("equipment.amulet", 8),   # Omnis, ur=[]
            ("equipment.ring2", 5),    # Unsated Rage, ur=[]
        ):
            instance = self.instance(path)
            self.assertEqual(instance["unique_rolls"]["status"], "empty", path)
            mod_lines = unique_mod_lines(instance)
            self.assertEqual(len(mod_lines), expected_mods, path)
            for line in mod_lines:
                self.assertEqual(line["value_status"], "range", f"{path}: {line['text']!r}")
                self.assertTrue(line["range_text"], f"{path}: missing range_text")
                self.assertIsNone(
                    line["diagnostics"]["value_numeric"],
                    f"{path}: range line must not expose an average/actual value",
                )
                self.assertIsNone(line["diagnostics"]["raw_roll"], path)
            self.assertNotIn(readable(instance)["completeness"], ("complete", "unavailable"))
            # A known constant unique mod would still be shown; these two have none,
            # but every line must still be readable (not hidden/skipped).
            self.assertTrue(all(line["text"] for line in mod_lines))

    # ---------------------------------------------------------- P1-4 blessings
    def test_blessing_implicit_definitions_are_restored_from_archived_source(self) -> None:
        blessings = self.sample["blessings"]["entries"]
        self.assertEqual(len(blessings), 10)
        for entry in blessings:
            instance = entry["instance"]
            block = instance["implicit_rolls"]
            self.assertEqual(block["status"], "present", entry["timeline"])
            self.assertNotEqual(
                block["status"], "definition_unavailable", entry["timeline"]
            )
            provenance = block.get("definition_provenance") or {}
            self.assertIn("source_records", provenance.get("source", ""), entry["timeline"])
            self.assertEqual(block["implicit_count"], len(block["values"] or []))
            self.assertGreaterEqual(block["implicit_count"], 1)
            for index, result in enumerate(block["values"]):
                self.assertEqual(result["roll_status"], "present", entry["timeline"])
                self.assertEqual(
                    result["raw_roll"], block["ir"][index], entry["timeline"]
                )
            self.assertEqual(readable(instance)["completeness"], "complete")

        # No "implicit_definition_unavailable" warning remains for the blessings.
        blessing_issues = [
            issue
            for issue in self.sample["issues"]
            if issue["code"] == "implicit_definition_unavailable"
        ]
        self.assertEqual(blessing_issues, [])

        first = blessings[0]["instance"]
        self.assertEqual(first["implicit_rolls"]["definition_kind"], "blessing")
        self.assertEqual(readable(first)["display_name"], "Grand Scales of Greed")
        value = first["implicit_rolls"]["values"][0]["values"][0]
        # Archived item_base record irreducibles: property=104, tags=1, type=0,
        # 0.45..0.70, ir[0]=200 -> 0.45 + (0.70+0.01-0.45)*200/255 floored to 2dp
        # = 0.65 -> "65%".
        self.assertEqual(value["property"], 104)
        self.assertEqual(value["roll_min"], 0.45)
        self.assertEqual(value["roll_max"], 0.70)
        self.assertEqual(first["implicit_rolls"]["values"][0]["raw_roll"], 200)
        self.assertAlmostEqual(value["value_numeric"], 0.65, places=9)
        self.assertEqual(value["display_value"], "65%")
        self.assertIn("65% Increased Drop Rate", texts(first, section="implicit"))

    # --------------------------------------------------- P2-1 per-mod roll ids
    def test_unique_roll_mods_carry_their_own_roll_id(self) -> None:
        head = self.instance("equipment.head")
        block = head["unique_rolls"]
        mods = block["mods"]
        self.assertEqual(len(mods), 10)
        self.assertEqual(block["roll_ids"], [0, 1, 2, 3])
        self.assertEqual(block["rollable_mod_count"], 9)
        # Apex first mod: rollId=3, ur[3]=234 -> 87 (if the sorted roll_ids[index]
        # bug were present this would read roll_ids[0]=0 and ur[0]=244 -> 89).
        self.assertEqual(mods[0]["roll_id"], 3)
        self.assertEqual(mods[0]["raw_roll"], 234)
        self.assertEqual(mods[0]["restored"]["value_numeric"], 87)
        self.assertEqual(mods[1]["roll_id"], 1)
        self.assertEqual(mods[1]["raw_roll"], 77)
        # A canRoll=false definition constant does not consume a roll slot.
        self.assertFalse(mods[2]["can_roll"])
        self.assertTrue(mods[2]["hidden"])
        self.assertEqual(mods[2]["roll_status"], "absent")
        self.assertIsNone(mods[2]["raw_roll"])
        self.assertEqual(mods[2]["restored"]["value_numeric"], 1.0)

        # Every readable unique line reports the diagnostics rollId of its own mod.
        for line in unique_mod_lines(head):
            mod_index = line["diagnostics"]["mod_index"]
            self.assertEqual(
                line["diagnostics"]["roll_id"], mods[mod_index]["roll_id"], mod_index
            )
            self.assertEqual(
                line["diagnostics"]["raw_roll"], mods[mod_index]["raw_roll"], mod_index
            )
        first_line = unique_mod_lines(head)[0]
        self.assertEqual(first_line["diagnostics"]["mod_index"], 0)
        self.assertEqual(first_line["diagnostics"]["roll_id"], 3)
        self.assertEqual(first_line["diagnostics"]["raw_roll"], 234)

        chest = self.instance("equipment.chest")
        self.assertEqual(chest["unique_rolls"]["mods"][0]["roll_id"], 3)
        self.assertEqual(chest["unique_rolls"]["mods"][0]["raw_roll"], 247)

    # ---------------------------------------------------- affixes / tier labels
    def test_affix_lines_use_source_tier_labels_and_keep_missing_rolls_honest(self) -> None:
        tier_by_path: dict[str, int] = {}
        roll_status_by_path: dict[str, str] = {}
        for _path, instance in in_scope_instances(self.sample):
            for source_path, entry in struct_affix_by_source_path(instance).items():
                tier_by_path[source_path] = entry["source_tier"]
                roll_status_by_path[source_path] = entry["roll"]["status"]

        affix_lines = []
        for path, instance in in_scope_instances(self.sample):
            for line in lines(instance):
                if (line.get("section") or "").startswith("affix_"):
                    affix_lines.append((path, line))
        # 47 affix instances; multi-property affixes legitimately render one line per
        # number, so compare the distinct instance paths, not the line count.
        affix_source_paths = {line["diagnostics"].get("source_path") for _, line in affix_lines}
        self.assertEqual(len(affix_source_paths), 47)
        self.assertGreaterEqual(len(affix_lines), 47)

        labels = set()
        for path, line in affix_lines:
            source_path = line["diagnostics"].get("source_path")
            self.assertIn(source_path, tier_by_path, f"{path}: {line['text']!r}")
            source_tier = tier_by_path[source_path]
            self.assertEqual(line["tier_label"], f"T{source_tier}", source_path)
            labels.add(line["tier_label"])
            # The library tier_index is 0-based; it must never leak as "T0".
            self.assertNotEqual(line["tier_label"], "T0", source_path)
            # Missing rolls: constant ranges keep their definitional text; real
            # ranges show a range, never a fabricated actual value.
            if roll_status_by_path[source_path] in ("absent", "null"):
                if line["value_status"] == "range":
                    self.assertTrue(line["range_text"], source_path)
                    self.assertIsNone(line["diagnostics"]["value_numeric"], source_path)
                else:
                    self.assertIn(line["value_status"], ("actual", "constant"), source_path)
                    self.assertTrue(line["value_text"], source_path)
                self.assertIsNone(line["diagnostics"]["raw_roll"], source_path)
        self.assertIn("T8", labels)
        self.assertIn("T7", labels)

    def test_readable_completeness_is_consistent_with_line_statuses(self) -> None:
        for path, instance in in_scope_instances(self.sample):
            block = readable(instance)
            statuses = [line["value_status"] for line in block.get("lines") or []]
            if block["completeness"] == "complete":
                self.assertTrue(
                    all(status in ("actual", "constant") for status in statuses),
                    f"{path}: complete readable has non-actual lines {statuses}",
                )
            if block["completeness"] in ("definition_only", "partial"):
                self.assertTrue(
                    any(status in ("range", "unresolved") for status in statuses),
                    f"{path}: {block['completeness']} readable has no range/unresolved line",
                )


    # ------------------------------------------------------------------
    # 2026-11-04 re-review R1/R2 semantic regressions
    # ------------------------------------------------------------------
    def test_review_r1_ward_gain_carries_kill_event(self) -> None:
        """R1: the Ward Gain blessing must state the trigger event (Kill)."""
        instances = instances_by_path(self.sample)
        ward = [
            (path, line)
            for path, instance in instances.items()
            for line in lines(instance)
            if "Ward Gain" in (line.get("text") or "")
        ]
        self.assertEqual(len(ward), 1, ward)
        path, line = ward[0]
        self.assertEqual(path, "blessings.3")
        self.assertEqual(line["text"], "18 Ward Gain on Kill")
        self.assertEqual(line["value_text"], "18")
        self.assertEqual(line["value_status"], "actual")
        diagnostics = line["diagnostics"]
        # propertyList 39 = SP.WardGain; event specialTag 3 = rrli.Kill (Tb -> "kill").
        self.assertEqual(diagnostics.get("property"), 39)
        self.assertEqual(diagnostics.get("special_tag"), 3)
        self.assertEqual(
            diagnostics.get("stat_id"), "stat:letools:coreDB.propertyList:39"
        )
        self.assertEqual(diagnostics.get("value_numeric"), 18)
        # The same blessing's plain +199 Ward Decay Threshold is a different property.
        decay = [
            line["text"]
            for line in lines(instances["blessings.3"])
            if "Ward Decay Threshold" in (line.get("text") or "")
        ]
        self.assertEqual(decay, ["+199 Ward Decay Threshold"])

    def test_review_r1_drop_rate_lines_are_tagged_non_dps(self) -> None:
        """R1: drop-rate lines stay in BuildSpec readable but are tagged non-DPS."""
        instances = instances_by_path(self.sample)
        drop = [
            (path, line)
            for path, instance in instances.items()
            for line in lines(instance)
            if (line.get("diagnostics") or {}).get("property") == 104
        ]
        expected = {
            ("blessings.1", "65% Increased Drop Rate"),
            ("blessings.2", "43% Increased Drop Rate"),
            ("blessings.4", "52% Increased Drop Rate"),
            ("blessings.6", "10% Increased Drop Rate"),
            ("blessings.8", "30% Increased Drop Rate"),
        }
        self.assertEqual({(path, line["text"]) for path, line in drop}, expected)
        self.assertEqual(len(drop), 5)
        for path, line in drop:
            diagnostics = line["diagnostics"]
            self.assertTrue(diagnostics.get("non_dps"), path)
            self.assertEqual(
                diagnostics.get("stat_id"),
                "stat:letools:coreDB.propertyList:104",
                path,
            )
        # Only these five lines carry the non-DPS marker on the fixed sample.
        marked = [
            line
            for instance in instances.values()
            for line in lines(instance)
            if (line.get("diagnostics") or {}).get("non_dps")
        ]
        self.assertEqual(len(marked), 5)

    def test_review_r2_hide_modifier_value_suppresses_only_the_value_prefix(self) -> None:
        """R2: hideModifierValue=1 rows show the description verbatim (no '+1')."""
        ring2 = instances_by_path(self.sample)["equipment.ring2"]
        sealed = [
            line
            for line in lines(ring2, section="affix_sealed")
            if "Crescendoing Rage" in (line.get("text") or "")
        ]
        self.assertEqual(len(sealed), 1, sealed)
        line = sealed[0]
        self.assertEqual(line["text"], "You have Crescendoing Rage")
        self.assertNotIn("+1", line["text"])
        # Structured value/source/diagnostics are preserved, only the player text hides it.
        self.assertEqual(line["value_text"], "1")
        self.assertEqual(line["value_status"], "actual")
        diagnostics = line["diagnostics"]
        self.assertEqual(diagnostics.get("property"), 58)
        self.assertEqual(diagnostics.get("special_tag"), 12)
        self.assertEqual(
            diagnostics.get("stat_id"),
            "stat:letools:coreDB.abilityPropertyList:950.12",
        )
        self.assertEqual(diagnostics.get("value_numeric"), 1.0)
        self.assertEqual(diagnostics.get("stat_name"), "You have Crescendoing Rage")

    def test_review_r2_plain_numeric_lines_are_not_hidden(self) -> None:
        """R2 counter-sample: ordinary numeric lines keep value and leading '+'.

        A hideModifierValue implementation that hides every value would break these.
        """
        instances = instances_by_path(self.sample)
        all_lines = [
            (path, line)
            for path, instance in instances.items()
            for line in lines(instance)
        ]
        by_text = {line.get("text"): (path, line) for path, line in all_lines}
        self.assertIn("+199 Ward Decay Threshold", by_text)
        self.assertIn("+109 Ward Decay Threshold", by_text)
        self.assertIn("+35 Ward per Second", by_text)
        path, line = by_text["+109 Ward Decay Threshold"]
        self.assertEqual(path, "equipment.head")
        self.assertEqual(line["value_text"], "109")
        self.assertEqual(line["value_status"], "actual")
        self.assertEqual((line.get("diagnostics") or {}).get("property"), 119)
        path, line = by_text["+35 Ward per Second"]
        self.assertEqual(path, "blessings.5")
        self.assertEqual(line["value_text"], "35")
        # Percent lines keep their value as well (no hidden-value behavior on percent stats).
        percent_lines = [
            (path, line)
            for path, instance in instances.items()
            for line in lines(instance)
            if "%" in (line.get("text") or "")
            and str(line.get("value_text") or "").endswith("%")
        ]
        self.assertTrue(percent_lines, "expected percent lines with value_text")
        for path, line in percent_lines[:5]:
            self.assertTrue(line["text"].strip(), path)

    def test_review_acceptance_checks_include_r1_r2_checks(self) -> None:
        """The re-review added 3 acceptance checks (23 -> 26) and all must pass."""
        from le_build.report import acceptance_checks

        checks = acceptance_checks(self.sample)
        self.assertEqual(len(checks), 26)
        names = {check.get("check") for check in checks}
        for name in ("ward_gain_event", "hidden_modifier_value", "drop_rate_non_dps"):
            self.assertIn(name, names)
        failed = [check for check in checks if check.get("status") != "pass"]
        self.assertEqual(failed, [])


class SyntheticUniqueReadableTest(unittest.TestCase):
    """Cases the fixed sample does not contain, built from real catalog definitions."""

    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.pointer = json.loads((REPO / "data" / "current.json").read_text(encoding="utf-8"))
        cls.catalog = Catalog.open(
            root=REPO, dataset=cls.pointer["dataset_id"], verify=True
        )
        # Scorched Earth: unique 485, base subtype 15, every mod canRoll=0, no ur key.
        cls.constant_only_id = craft_unique_id(15, 485)
        # Xithara's Conundrum: unique 133, base subtype 5, a visible type=2 mod on
        # property 0 (Damage) with rollId 0; ur[0]=255 restores 0.25..0.30 -> 0.30.
        cls.more_mod_id = craft_unique_id(5, 133)
        document = minimal_document()
        document["data"]["equipment"] = {
            "head": {"id": cls.constant_only_id, "affixes": []},
            "chest": {"id": cls.more_mod_id, "ur": [255], "affixes": []},
        }
        cls.spec = _build(cls.catalog, document=document, fixture_fetch=None, share_id="p2readablesyn")
        cls.instances = instances_by_path(cls.spec)
        # Independent check of the crafted ids against the documented U payload rule.
        from le_build.codecs import letools_ids

        self_decoded = letools_ids.decode_encoded_id(cls.constant_only_id)
        cls.assertPayload = self_decoded["payload"]
        cls.more_payload = letools_ids.decode_encoded_id(cls.more_mod_id)["payload"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()

    def test_crafted_ids_follow_the_documented_u_payload(self) -> None:
        self.assertEqual(self.assertPayload, "015485")
        self.assertEqual(self.more_payload, "005133")

    def test_constant_only_unique_with_missing_ur_keeps_its_constant_lines(self) -> None:
        head = self.instances["equipment.head"]
        self.assertEqual(readable(head)["display_name"], "Scorched Earth")
        block = head["unique_rolls"]
        self.assertEqual(block["status"], "absent")
        self.assertEqual(block["rollable_mod_count"], 0)
        self.assertEqual(len(block["mods"]), 2)

        mod_lines = unique_mod_lines(head)
        self.assertEqual(len(mod_lines), 2)  # tooltipEntries = [0, 1]
        texts_by_index = {line["diagnostics"]["mod_index"]: line for line in mod_lines}
        self.assertIn("Summon Tide Elemental", texts_by_index[0]["text"])
        # 2026-11-03 generic-property rework: a Penetration property is qualified by
        # its damage-type tags instead of the generic catalog name; the line must stay
        # readable and distinguishable ("... Penetration").
        self.assertIn("Penetration", texts_by_index[1]["text"])
        self.assertIn("Fire", texts_by_index[1]["text"])
        self.assertNotEqual(texts_by_index[0]["text"], texts_by_index[1]["text"])
        for line in mod_lines:
            self.assertIn(line["value_status"], ("actual", "constant"))
            self.assertTrue(line["value_text"])
            # A definitional constant is not a fabricated roll.
            self.assertIsNone(line["diagnostics"]["raw_roll"])
            self.assertEqual(line["diagnostics"]["roll_status"], "absent")
        self.assertEqual(readable(head)["completeness"], "complete")

    def test_more_modifier_has_its_own_expected_text(self) -> None:
        chest = self.instances["equipment.chest"]
        self.assertEqual(readable(chest)["display_name"], "Xithara's Conundrum")
        more_lines = [
            line
            for line in unique_mod_lines(chest)
            if line["diagnostics"].get("property") == 0
            and line["diagnostics"].get("modifier_type") == 2
        ]
        self.assertEqual(len(more_lines), 1)
        # ur[0]=255 on 0.25..0.30 with the documented type!=0 formula -> min(0.31, 0.30)
        # -> 0.30 -> 30%; more template.  The damage-type qualifier comes from the
        # damage-type tags (Physical); non-damage condition tags such as FullLife are
        # not rendered into this line (residual readable limit, see the response doc).
        self.assertEqual(more_lines[0]["text"], "30% more Physical Damage")
        self.assertAlmostEqual(
            more_lines[0]["diagnostics"]["value_numeric"], 0.30, places=9
        )
        self.assertIn("more", more_lines[0]["text"])
        self.assertNotIn("increased", more_lines[0]["text"])
        self.assertEqual(more_lines[0]["value_status"], "actual")


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
