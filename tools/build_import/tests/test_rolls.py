"""Independent tests for le_build.rolls (P2-4 value restoration).

Run from ``tools/build_import``::

    & $py -m unittest discover -s tests -v

The expected numbers below are *hand-derived* from the archived formula and the
declared ranges.  Every expected value is written as a literal (with the literal
arithmetic in a comment); none of them is produced by calling the implementation
under test.  The module is loaded by file path on purpose so these tests do not
depend on the rest of the in-progress ``le_build`` package.

The test module also implements a minimal read-only catalog adapter over the
same SQLite release that the Lead's ``le_build.catalog.Catalog`` reads.  It
follows the documented catalog contract and the archived ``getProperty`` dispatch
(``window.SP.AbilityProperty == 58``, ``window.SP.PlayerProperty == 98``,
everything else ``coreDB.propertyList``), so the roll module is exercised exactly
as designed without depending on a sibling file that is being edited concurrently.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

from _dataset_asset import require_dataset_db

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]  # .../LastEpoch-BuildSimulator
SRC = HERE.parents[1] / "src"
ROLLS_PATH = SRC / "le_build" / "rolls.py"
DISCOVERY = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my"

_spec = importlib.util.spec_from_file_location("le_build_rolls_under_test", ROLLS_PATH)
rolls = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(rolls)



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class SqliteCatalog:
    """Minimal read-only implementation of the documented catalog contract."""

    def __init__(self, db_path: Path, dataset_id: str):
        uri = "file:" + db_path.as_posix() + "?mode=ro&immutable=1"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        self.dataset_id = dataset_id

    def close(self) -> None:
        self._conn.close()

    def _row(self, sql: str, args: tuple = ()) -> dict | None:
        row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._conn.execute(sql, args)]

    # -- contract ---------------------------------------------------------
    def item(self, entity_id: str) -> dict | None:
        return self._row(
            """
            SELECT e.entity_id, e.canonical_name, i.item_category, i.item_type,
                   i.base_type, i.implicit_text, i.extensions
            FROM entities AS e LEFT JOIN items AS i ON i.entity_id = e.entity_id
            WHERE e.entity_id = ?
            """,
            (entity_id,),
        )

    def item_definition(self, entity_id: str) -> dict | None:
        """Mirror ``le_build.catalog.Catalog.item_definition`` for ``items.implicit_text``.

        The rework contract (``docs/phase-2-rework-contract.zh-CN.md`` §2) reads item
        definitions through one unified accessor; the fake adapter keeps the same
        minimal contract: ``kind``, ``implicits`` and ``implicits_provenance``.
        """
        entity = self._row(
            "SELECT entity_id, kind, canonical_name FROM entities WHERE entity_id = ?",
            (entity_id,),
        )
        if entity is None:
            return None
        row = self.item(entity_id) or {}
        kind = entity.get("kind")
        implicits: list = []
        provenance: dict = {"source": "unavailable"}
        if kind == "item" and row:
            raw = row.get("implicit_text")
            if raw is None:
                provenance = {"source": "items.implicit_text", "status": "absent"}
            else:
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = []
                implicits = parsed if isinstance(parsed, list) else []
                provenance = {"source": "items.implicit_text", "status": "normalized"}
        return {
            "entity_id": entity_id,
            "kind": kind,
            "name": entity.get("canonical_name"),
            "implicits": implicits,
            "implicits_provenance": provenance,
        }

    def unique_mods(self, entity_id: str) -> list[dict]:
        item = self.item(entity_id)
        if not item:
            return []
        mods = _json_obj(item.get("extensions")).get("mods") or []
        return [mod for mod in mods if isinstance(mod, dict)]

    def affix(self, entity_id: str) -> dict | None:
        return self._row(
            """
            SELECT e.entity_id, e.canonical_name, a.affix_type, a.source_affix_id,
                   a.affix_display_name, a.affix_slot, a.extensions
            FROM entities AS e LEFT JOIN affixes AS a ON a.entity_id = e.entity_id
            WHERE e.entity_id = ?
            """,
            (entity_id,),
        )

    def affix_tiers(self, entity_id: str) -> list[dict]:
        return self._rows(
            """
            SELECT tier_id, tier_index, roll_min, roll_max, extra_rolls, extensions
            FROM affix_tiers WHERE affix_entity_id = ? ORDER BY tier_index
            """,
            (entity_id,),
        )

    def stat_by_property(self, property_id, tags=0, special_tag=0) -> dict | None:
        try:
            prop = int(property_id)
        except (TypeError, ValueError):
            return None
        tags = int(tags or 0)
        special_tag = int(special_tag or 0)
        response = None
        if prop == 58:  # window.SP.AbilityProperty -> abilityPropertyList[tags].properties[specialTag]
            response = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.abilityPropertyList' "
                "AND raw_enum = ?",
                (f"{tags}.{special_tag}",),
            )
        elif prop == 98:  # window.SP.PlayerProperty -> playerPropertyList[tags]
            response = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.playerPropertyList' "
                "AND raw_enum = ?",
                (str(tags),),
            )
        else:
            response = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.propertyList' "
                "AND raw_enum = ?",
                (str(prop),),
            )
        return response

    def names(self, entity_id: str) -> dict:
        result: dict = {}
        for row in self._rows(
            "SELECT language, name FROM entity_names WHERE entity_id = ? ORDER BY language",
            (entity_id,),
        ):
            result.setdefault(row["language"], []).append(row["name"])
        return result


class StubCatalog:
    """Tiny in-memory catalog used to exercise malformed/ambiguous branches."""

    dataset_id = "stub-dataset"

    def __init__(self, *, affix=None, tiers=None, stats=None, items=None, mods=None):
        self._affix = affix
        self._tiers = tiers or []
        self._stats = stats or {}
        self._items = items or {}
        self._mods = mods or {}

    def affix(self, entity_id):
        return self._affix

    def affix_tiers(self, entity_id):
        return self._tiers

    def stat_by_property(self, property_id, tags=0, special_tag=0):
        return self._stats.get((property_id, int(tags or 0), int(special_tag or 0)))

    def item(self, entity_id):
        return self._items.get(entity_id)

    def item_definition(self, entity_id):
        item = self._items.get(entity_id)
        if item is None:
            return None
        raw = item.get("implicit_text")
        implicits: list = []
        provenance: dict = {"source": "items.implicit_text", "status": "normalized"}
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = []
            implicits = parsed if isinstance(parsed, list) else []
        return {
            "entity_id": entity_id,
            "kind": "item",
            "implicits": implicits,
            "implicits_provenance": provenance,
        }

    def unique_mods(self, entity_id):
        return self._mods.get(entity_id, [])

    def names(self, entity_id):
        return {}


def _affix_row(properties: list[dict], name: str = "fixture affix") -> dict:
    return {
        "entity_id": "affix:stub:1",
        "canonical_name": name,
        "affix_type": "stub",
        "source_affix_id": "1",
        "affix_display_name": name,
        "affix_slot": "suffix",
        "extensions": json.dumps({"affixProperties": properties}),
    }


def _tier_row(tier_index: int, rolls: list[dict], *, inverted: bool = False) -> dict:
    endpoints = [endpoint for roll in rolls for endpoint in (roll["min"], roll["max"])]
    return {
        "tier_id": f"affix:stub:1#tier#{tier_index}",
        "tier_index": tier_index,
        "roll_min": min(endpoints),
        "roll_max": max(endpoints),
        "extra_rolls": json.dumps(rolls[1:]) if len(rolls) > 1 else None,
        "extensions": json.dumps(
            {"tier": {"tierName": None, "requiredLevel": 0, "rolls": rolls}, "source_inverted_range": inverted}
        ),
    }


def _stat(stat_id, name, unit, g, *, pct_add=0, pct_of=0, tenth=0, rounding_for_more=0) -> dict:
    return {
        "stat_id": stat_id,
        "name": name,
        "unit": unit,
        "extensions": json.dumps(
            {
                "roundAddedToInt": g,
                "roundingForMore": rounding_for_more,
                "displayAddedAsPercentage": pct_add,
                "displayAsPercentageOf": pct_of,
                "displayAddedAsTenthOfValue": tenth,
            }
        ),
    }


# ---------------------------------------------------------------------------
# Pure formula / helper tests
# ---------------------------------------------------------------------------
class TestRoundingHelper(unittest.TestCase):
    def test_rrlb_is_round_half_to_even(self):
        # Archived rrlb: exact .5 ties go to the nearest even integer.
        # 2.5 -> 2 (even), 3.5 -> 4 (even), -2.5 -> -2, -3.5 -> -4.
        self.assertEqual(rolls.rrlb(2.5), 2)
        self.assertEqual(rolls.rrlb(3.5), 4)
        self.assertEqual(rolls.rrlb(-2.5), -2)
        self.assertEqual(rolls.rrlb(-3.5), -4)
        self.assertEqual(rolls.rrlb(0.5), 0)
        self.assertEqual(rolls.rrlb(1.5), 2)
        self.assertEqual(rolls.rrlb(12.5), 12)
        self.assertEqual(rolls.rrlb(37.5), 38)
        self.assertEqual(rolls.rrlb(62.5), 62)

    def test_round2_is_not_math_round(self):
        # 100*0.625 == 62.5 exactly; banker's -> 62 (even), Math.round would give 63.
        # 100*0.375 == 37.5 -> 38 (even), 100*0.125 == 12.5 -> 12 (even), 100*0.875 == 87.5 -> 88.
        self.assertAlmostEqual(
            rolls.restore_value_from_range(0.625, 0.625, rolls.MISSING_ROLL, 0, 0)["value_numeric"],
            0.62,
        )
        # 100*0.375 == 37.5 -> 38 (even), 100*0.125 == 12.5 -> 12 (even), 100*0.875 == 87.5 -> 88.
        self.assertAlmostEqual(
            rolls.restore_value_from_range(0.375, 0.375, rolls.MISSING_ROLL, 0, 0)["value_numeric"],
            0.38,
        )
        self.assertAlmostEqual(
            rolls.restore_value_from_range(0.125, 0.125, rolls.MISSING_ROLL, 0, 0)["value_numeric"],
            0.12,
        )
        self.assertAlmostEqual(
            rolls.restore_value_from_range(0.875, 0.875, rolls.MISSING_ROLL, 0, 0)["value_numeric"],
            0.88,
        )


class TestRestoreValueFromRange(unittest.TestCase):
    def test_ascending_general_branch_affix13_r113(self):
        # Fire Resistance T7 is 0.61..0.75, g=0, modifierType=0, r=113.
        # e = 113/255 = 0.44313725490196076
        # c = rrlb(100*0.61)/100 = 0.61, d = rrlb(100*0.75)/100 = 0.75
        # 100*((0.75+0.01-0.61)*e + 0.61 + 0.001)
        #   = 100*(0.15*0.44313725490196076 + 0.611)
        #   = 100*0.6774705882352941 = 67.74705882352941 -> floor 67 -> 0.67
        result = rolls.restore_value_from_range(0.61, 0.75, 113, 0, 0)
        self.assertEqual(result["status"], rolls.STATUS_RESOLVED)
        self.assertEqual(result["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(result["raw_roll"], 113)
        self.assertEqual(result["direction"], rolls.DIRECTION_ASCENDING)
        self.assertAlmostEqual(result["value_numeric"], 0.67, places=12)
        self.assertIsNone(result["effective_value_policy"])

    def test_ascending_endpoints_r0_and_r255(self):
        # r=0:   100*(0.15*0 + 0.611) = 61.1   -> floor 61 -> 0.61
        # r=255: 100*(0.15*1 + 0.611) = 76.1   -> floor 76 -> min(0.76, 0.75) = 0.75
        self.assertAlmostEqual(
            rolls.restore_value_from_range(0.61, 0.75, 0, 0, 0)["value_numeric"], 0.61
        )
        self.assertAlmostEqual(
            rolls.restore_value_from_range(0.61, 0.75, 255, 0, 0)["value_numeric"], 0.75
        )

    def test_round_added_to_int_1_branch(self):
        # 39..91, g=1, type=0, r=234: e = 234/255 = 0.9176470588235294
        # floor((91+1-39)*e + 39) = floor(53*0.9176470588235294 + 39)
        #   = floor(48.63529411764706 + 39) = floor(87.63529411764706) = 87
        # then min(87, 91) = 87.
        self.assertEqual(rolls.rrl7o(1, 0, 39, 91, 234), 87)

    def test_round_added_to_int_2_branch(self):
        # 0.2..0.4, g=2, type=0, r=128: e = 128/255 = 0.5019607843137255
        # c = rrlb(2)/10 = 0.2, d = rrlb(4)/10 = 0.4
        # floor(10*((0.4+0.1-0.2)*e + 0.2))/10 = floor(10*(0.3*e + 0.2))/10
        #   = floor(10*(0.15058823529411766 + 0.2))/10 = floor(3.5058823529411764)/10 = 0.3
        self.assertAlmostEqual(rolls.rrl7o(2, 0, 0.2, 0.4, 128), 0.3, places=12)

    def test_round_added_to_int_3_branch(self):
        # 0.001..0.002, g=3, type=0:
        # r=0   -> floor(1000*(0 + 0.001))/1000 = 1/1000 = 0.001 -> min 0.001
        # r=255 -> floor(1000*((0.002+0.001-0.001)*1 + 0.001))/1000
        #        = floor(1000*0.003)/1000 = 3/1000 = 0.003 -> min(0.003, 0.002) = 0.002
        self.assertAlmostEqual(rolls.rrl7o(3, 0, 0.001, 0.002, 0), 0.001, places=12)
        self.assertAlmostEqual(rolls.rrl7o(3, 0, 0.001, 0.002, 255), 0.002, places=12)

    def test_type_not_zero_general_branch(self):
        # 1.6..2.0, modifierType=1, r=128: e = 0.5019607843137255
        # 100*((2.0+0.01-1.6)*e + 1.6 + 0.001)
        #   = 100*(0.41*0.5019607843137255 + 1.601)
        #   = 100*(0.20580392156862744 + 1.601) = 180.68039215686274 -> floor 180 -> 1.8
        # then min(1.8, 2.0) = 1.8.
        self.assertAlmostEqual(rolls.rrl7o(0, 1, 1.6, 2.0, 128), 1.8, places=12)

    def test_inverted_range_general_branch(self):
        # affix 74 T4 declares rolls[0] = {min: -0.07, max: -0.08}: value > maxValue,
        # so c=-0.07, d=-0.08 and the inverted general branch runs:
        # r=0:   ceil(100*((-0.08-0.01-(-0.07))*0 + (-0.07)))/100
        #          = ceil(-7.000000000000001)/100 = -7/100 = -0.07
        #        max(-0.07, -0.08) = -0.07
        # r=255: ceil(100*((-0.02)*1 + (-0.07)))/100 = ceil(-9)/100 = -0.09
        #        max(-0.09, -0.08) = -0.08
        self.assertAlmostEqual(rolls.rrl7o(1, 1, -0.07, -0.08, 0), -0.07, places=12)
        self.assertAlmostEqual(rolls.rrl7o(1, 1, -0.07, -0.08, 255), -0.08, places=12)
        # r=127: 100*((-0.02)*127/255 - 0.07) = -7.996078431372548 -> ceil = -7 -> -0.07
        # r=128: 100*((-0.02)*128/255 - 0.07) = -8.00392156862745  -> ceil = -8 -> -0.08
        self.assertAlmostEqual(rolls.rrl7o(1, 1, -0.07, -0.08, 127), -0.07, places=12)
        self.assertAlmostEqual(rolls.rrl7o(1, 1, -0.07, -0.08, 128), -0.08, places=12)

    def test_inverted_integer_quirks(self):
        # Faithful port of the archived integer inverted branch
        # Math.max(Math.ceil((c-d-1)*e+d), c): with c=10 > d=5 the clamp is `c`,
        # so the branch returns 10 for every roll.  The archived code really does
        # this; the port keeps it verbatim instead of "fixing" the site.
        # r=0   -> ceil((10-5-1)*0 + 5) = 5 -> max(5,10) = 10
        # r=128 -> ceil(4*128/255 + 5) = ceil(7.007843...) = 8 -> max(8,10) = 10
        # r=255 -> ceil(4*1 + 5) = 9 -> max(9,10) = 10
        self.assertEqual(rolls.rrl7o(1, 0, 10, 5, 0), 10)
        self.assertEqual(rolls.rrl7o(1, 0, 10, 5, 128), 10)
        self.assertEqual(rolls.rrl7o(1, 0, 10, 5, 255), 10)

    def test_absent_roll_policy(self):
        # No roll for 0.4..0.6, type=1: the average branch is rrl6o:
        # (0.4+0.6)/2 = 0.5 -> rrlb(100*0.5)/100 = 0.5
        result = rolls.restore_value_from_range(0.4, 0.6, rolls.MISSING_ROLL, 1, 0)
        self.assertEqual(result["status"], rolls.STATUS_PARTIAL)
        self.assertEqual(result["roll_status"], rolls.ROLL_ABSENT)
        self.assertIsNone(result["raw_roll"])
        self.assertIsNone(result["value_numeric"])
        self.assertAlmostEqual(result["derived_display_value"], 0.5, places=12)
        policy = result["effective_value_policy"]
        self.assertEqual(policy["policy_id"], "letools-average-branch-v1")
        self.assertEqual(policy["labelled"], "derived_display_value_not_source_roll")
        self.assertEqual(policy["source_roll"], rolls.ROLL_ABSENT)

    def test_constant_range_needs_no_roll(self):
        # 77..77, g=1: rrl6o(77) = rrlb(77) = 77; any roll also gives 77.
        result = rolls.restore_value_from_range(77, 77, rolls.MISSING_ROLL, 0, 1)
        self.assertEqual(result["status"], rolls.STATUS_RESOLVED)
        self.assertEqual(result["roll_status"], rolls.ROLL_ABSENT)
        self.assertEqual(result["direction"], rolls.DIRECTION_CONSTANT)
        self.assertEqual(result["value_numeric"], 77)
        self.assertIsNone(result["effective_value_policy"])

    def test_null_roll_is_distinct_from_absent(self):
        absent = rolls.restore_value_from_range(0.61, 0.75, rolls.MISSING_ROLL, 0, 0)
        null = rolls.restore_value_from_range(0.61, 0.75, None, 0, 0)
        self.assertEqual(absent["roll_status"], rolls.ROLL_ABSENT)
        self.assertEqual(null["roll_status"], rolls.ROLL_NULL)
        self.assertEqual(absent["value_numeric"], None)
        self.assertEqual(null["value_numeric"], None)
        # Both derive the same average display value: (0.61+0.75)/2 = 0.68 -> 0.68.
        self.assertAlmostEqual(absent["derived_display_value"], 0.68, places=12)
        self.assertAlmostEqual(null["derived_display_value"], 0.68, places=12)

    def test_zero_roll_is_present_not_absent(self):
        result = rolls.restore_value_from_range(0.61, 0.75, 0, 0, 0)
        self.assertEqual(result["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(result["raw_roll"], 0)
        self.assertIsNotNone(result["value_numeric"])

    def test_invalid_inputs_raise(self):
        with self.assertRaises(rolls.RollValueError):
            rolls.restore_value_from_range(0.1, 0.2, 300, 0, 0)
        with self.assertRaises(rolls.RollValueError):
            rolls.restore_value_from_range(0.1, 0.2, 1.5, 0, 0)
        with self.assertRaises(rolls.RollValueError):
            rolls.restore_value_from_range(None, 0.2, 1, 0, 0)


# ---------------------------------------------------------------------------
# Shared fixtures loaded from the frozen dataset
# ---------------------------------------------------------------------------
class DatasetCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_dataset_db()
        cls.pointer = _load_json(REPO / "data" / "current.json")
        db_path = REPO / cls.pointer["database"].replace("\\", "/")
        cls.catalog = SqliteCatalog(db_path, cls.pointer["dataset_id"])
        cls.build = _load_json(DISCOVERY / "build-response.json")
        cls.decoded = _load_json(DISCOVERY / "decoded-id-probe.json")
        cls.capture_meta = _load_json(DISCOVERY / "capture-meta.json")

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()

    def affix_entity_for_encoded(self, encoded_id: str) -> str:
        source_id = int(self.decoded[encoded_id])
        rows = self.catalog._rows(
            "SELECT entity_id FROM affixes WHERE source_affix_id = ?", (str(source_id),)
        )
        self.assertEqual(len(rows), 1, f"expected exactly one affix entity for source id {source_id}")
        return rows[0]["entity_id"]


# ---------------------------------------------------------------------------
# Affix instances
# ---------------------------------------------------------------------------
class TestAffixRestoration(DatasetCase):
    AFFIX_13 = "affix:letools:affixList.singleAffixes:13"  # Fire Resistance
    AFFIX_74 = "affix:letools:affixList.singleAffixes:74"  # Damage Taken on Block (inverted T4+)
    AFFIX_720 = "affix:letools:affixList.multiAffixes:720"  # Fire Pen + Minion Fire Pen, 2 values

    def test_affix13_tier7_roll113(self):
        # Build head: {"id":"AAwRgzEA"(=affix 13),"tier":7,"r":113}.
        # Source tier 7 -> normalized tier_index 6.  Catalog: 0.61..0.75.
        # Derivation shown in TestRestoreValueFromRange.test_ascending_general_branch_affix13_r113:
        # 0.67 ("67%").
        result = rolls.restore_affix_value(self.AFFIX_13, 6, 113, self.catalog)
        self.assertEqual(result["status"], rolls.STATUS_RESOLVED)
        self.assertEqual(result["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(result["raw_roll"], 113)
        self.assertEqual(len(result["values"]), 1)
        value = result["values"][0]
        self.assertEqual(value["property"], 13)
        self.assertEqual(value["modifier_type"], 0)
        self.assertEqual(value["round_added_to_int"], 0)
        self.assertEqual(value["direction"], rolls.DIRECTION_ASCENDING)
        self.assertAlmostEqual(value["roll_min"], 0.61, places=12)
        self.assertAlmostEqual(value["roll_max"], 0.75, places=12)
        self.assertAlmostEqual(value["value_numeric"], 0.67, places=12)
        self.assertEqual(value["unit"], "percent")
        self.assertEqual(value["stat_id"], "stat:letools:coreDB.propertyList:13")
        self.assertEqual(value["display_value"], "67%")
        self.assertEqual(result["evidence"]["code_rule_version"], rolls.CODE_RULE_VERSION)
        self.assertIsNone(result["effective_value_policy"])

    def test_affix13_zero_and_max_roll(self):
        zero = rolls.restore_affix_value(self.AFFIX_13, 6, 0, self.catalog)
        self.assertEqual(zero["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(zero["raw_roll"], 0)
        self.assertAlmostEqual(zero["values"][0]["value_numeric"], 0.61, places=12)
        maximum = rolls.restore_affix_value(self.AFFIX_13, 6, 255, self.catalog)
        self.assertAlmostEqual(maximum["values"][0]["value_numeric"], 0.75, places=12)

    def test_affix13_explicit_null(self):
        result = rolls.restore_affix_value(self.AFFIX_13, 6, None, self.catalog)
        self.assertEqual(result["roll_status"], rolls.ROLL_NULL)
        self.assertIsNone(result["raw_roll"])
        self.assertIsNone(result["values"][0]["value_numeric"])
        # Average branch: (0.61+0.75)/2 = 0.68 -> 0.68.
        self.assertAlmostEqual(result["values"][0]["derived_display_value"], 0.68, places=12)
        self.assertEqual(result["effective_value_policy"]["source_roll"], rolls.ROLL_NULL)

    def test_affix13_absent(self):
        result = rolls.restore_affix_value(self.AFFIX_13, 6, rolls.MISSING_ROLL, self.catalog)
        self.assertEqual(result["roll_status"], rolls.ROLL_ABSENT)
        self.assertIsNone(result["raw_roll"])
        self.assertIsNone(result["values"][0]["value_numeric"])
        self.assertAlmostEqual(result["values"][0]["derived_display_value"], 0.68, places=12)
        self.assertEqual(result["status"], rolls.STATUS_PARTIAL)

    def test_inverted_affix74_uses_definition_direction(self):
        # affix_tiers.roll_min/roll_max are sorted (-0.08, -0.07) and lose direction;
        # extensions.tier.rolls[0] is {"min": -0.07, "max": -0.08} and
        # extensions.source_inverted_range == true.  Only the extension may be used.
        rows = self.catalog.affix_tiers(self.AFFIX_74)
        tier = next(row for row in rows if row["tier_index"] == 3)
        self.assertTrue(_json_obj(tier["extensions"])["source_inverted_range"])
        self.assertLess(tier["roll_min"], tier["roll_max"])  # sorted => direction lost
        zero = rolls.restore_affix_value(self.AFFIX_74, 3, 0, self.catalog)
        self.assertEqual(zero["values"][0]["direction"], rolls.DIRECTION_INVERTED)
        self.assertAlmostEqual(zero["values"][0]["roll_min"], -0.07, places=12)
        self.assertAlmostEqual(zero["values"][0]["roll_max"], -0.08, places=12)
        self.assertAlmostEqual(zero["values"][0]["value_numeric"], -0.07, places=12)
        maximum = rolls.restore_affix_value(self.AFFIX_74, 3, 255, self.catalog)
        self.assertAlmostEqual(maximum["values"][0]["value_numeric"], -0.08, places=12)
        # Branch check: modifierType=1 goes through the general (two-decimal) inverted branch.
        self.assertEqual(zero["values"][0]["branch"], "inverted.general.round2")
        # 2026-11-03 review P1-1: the item-card percentage path is taken whenever
        # modifier_type != 0 (archived kb `0 != k` test), so the displayed value is
        # -0.07 -> "-7%", not the internal normalized "-0.07".
        self.assertEqual(zero["values"][0]["display_value"], "-7%")

    def test_multi_value_affix720(self):
        # Build weapon1 affix: affix 720, tier 7 -> tier_index 6, r=225.
        # Both declared values are 0.19..0.22 (Fire Penetration / Minion Fire Penetration);
        # one instance roll applies to each value, paired by index.
        # e = 225/255 = 0.8823529411764706
        # c = 0.19, d = 0.22
        # 100*((0.22+0.01-0.19)*e + 0.19 + 0.001)
        #   = 100*(0.04*0.8823529411764706 + 0.191)
        #   = 100*0.22629411764705882 = 22.629411764705882 -> floor 22 -> 0.22
        # then min(0.22, 0.22) = 0.22.  Both entries therefore 0.22.
        result = rolls.restore_affix_value(self.AFFIX_720, 6, 225, self.catalog)
        self.assertEqual(result["status"], rolls.STATUS_RESOLVED)
        self.assertEqual(result["raw_roll"], 225)
        self.assertEqual(len(result["values"]), 2)
        self.assertEqual([value["property"] for value in result["values"]], [59, 59])
        self.assertNotEqual(
            result["values"][0]["tags"], result["values"][1]["tags"]
        )  # 8 vs 8200, both definition entries preserved
        for value in result["values"]:
            self.assertAlmostEqual(value["roll_min"], 0.19, places=12)
            self.assertAlmostEqual(value["roll_max"], 0.22, places=12)
            self.assertAlmostEqual(value["value_numeric"], 0.22, places=12)
            self.assertEqual(value["display_value"], "22%")

    def test_multi_value_affix_with_build_roll_pairs_by_index(self):
        # Idol affix 167 is a 2-property multi-affix (property 42, specialTag 1 and 24),
        # source tier 1 -> tier_index 0, both declared values 0.07..0.25.
        # Idol 0 affix 0 has r=188: e = 188/255 = 0.7372549019607844
        # 100*((0.25+0.01-0.07)*e + 0.07 + 0.001)
        #   = 100*(0.19*0.7372549019607844 + 0.071)
        #   = 100*(0.14007843137254902 + 0.071) = 21.107843137254903 -> floor 21 -> 0.21
        entity = self.affix_entity_for_encoded("AIwNg7EA")
        result = rolls.restore_affix_value(entity, 0, 188, self.catalog)
        self.assertEqual(result["status"], rolls.STATUS_RESOLVED)
        self.assertEqual(len(result["values"]), 2)
        self.assertEqual({value["special_tag"] for value in result["values"]}, {1, 24})
        for value in result["values"]:
            self.assertAlmostEqual(value["value_numeric"], 0.21, places=12)
            self.assertEqual(value["display_value"], "21%")

    def test_affix_tier_not_found_and_out_of_range(self):
        result = rolls.restore_affix_value(self.AFFIX_13, 99, 113, self.catalog)
        self.assertEqual(result["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(result["evidence"]["failure_reason"], "affix_tier_not_found")
        unknown = rolls.restore_affix_value("affix:letools:affixList.singleAffixes:999999", 0, 1, self.catalog)
        self.assertEqual(unknown["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(unknown["evidence"]["failure_reason"], "affix_not_found")

    def test_invalid_roll_does_not_fabricate_value(self):
        result = rolls.restore_affix_value(self.AFFIX_13, 6, 300, self.catalog)
        self.assertEqual(result["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(result["evidence"]["failure_reason"], "invalid_raw_roll")
        self.assertEqual(result["values"], [])


# ---------------------------------------------------------------------------
# Base item implicits
# ---------------------------------------------------------------------------
class TestImplicitRestoration(DatasetCase):
    BASE_23 = "item:letools:itemList.equippable_0:23"  # Runed Visage, base of the build's head

    def test_build_head_implicits(self):
        # head.ir = [234, 167, 203]; implicits are:
        #   [0] property 10 Armor, 77/77 constant, type 0, g=1
        #       r=234: floor((77+1-77)*234/255 + 77) = floor(0.9176470588235294+77) = 77 -> 77
        #   [1] property 99 Mana Spent Gained as Ward, 0.3..0.77, type 0, g=0
        #       e=167/255=0.6549019607843137
        #       100*((0.77+0.01-0.3)*e + 0.3 + 0.001)
        #         = 100*(0.48*0.6549019607843137 + 0.301)
        #         = 100*0.6153529411764706 = 61.53529411764706 -> floor 61 -> 0.61
        #   [2] property 18 Mana Regen, 0.14..0.3, type 1 (increased -> general branch)
        #       e=203/255=0.796078431372549
        #       100*((0.3+0.01-0.14)*e + 0.14 + 0.001)
        #         = 100*(0.17*0.796078431372549 + 0.141)
        #         = 100*0.2763333333333333 = 27.633333333333333 -> floor 27 -> 0.27
        results = rolls.restore_implicit_values(self.BASE_23, [234, 167, 203], self.catalog)
        self.assertEqual(len(results), 3)
        self.assertEqual(
            [entry["roll_status"] for entry in results],
            [rolls.ROLL_PRESENT, rolls.ROLL_PRESENT, rolls.ROLL_PRESENT],
        )
        self.assertEqual([entry["raw_roll"] for entry in results], [234, 167, 203])
        first, second, third = (entry["values"][0] for entry in results)
        self.assertEqual(first["property"], 10)
        self.assertEqual(first["direction"], rolls.DIRECTION_CONSTANT)
        self.assertEqual(first["value_numeric"], 77)
        self.assertEqual(first["display_value"], "77")
        self.assertAlmostEqual(second["value_numeric"], 0.61, places=12)
        self.assertEqual(second["unit"], "percent")
        self.assertEqual(second["display_value"], "61%")
        self.assertAlmostEqual(third["value_numeric"], 0.27, places=12)
        # 2026-11-03 review P1-1: property 18 is `Mana Regen`, modifier_type=1, so the
        # item-card percentage path applies: 0.27 -> "27%" (not the internal "0.27").
        self.assertEqual(third["display_value"], "27%")
        self.assertEqual(third["branch"], "ascending.type_ne_0.round2")

    def test_zero_roll_on_implicit(self):
        results = rolls.restore_implicit_values(self.BASE_23, [0, 0, 0], self.catalog)
        second = results[1]["values"][0]
        # r=0 -> floor(100*(0.48*0+0.301))/100 = floor(30.1)/100 = 0.3; min(0.3,0.77)=0.3
        self.assertAlmostEqual(second["value_numeric"], 0.3, places=12)
        self.assertEqual(results[1]["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(results[1]["raw_roll"], 0)

    def test_short_ir_marks_missing_rolls_absent(self):
        results = rolls.restore_implicit_values(self.BASE_23, [234], self.catalog)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(results[1]["roll_status"], rolls.ROLL_ABSENT)
        self.assertEqual(results[2]["roll_status"], rolls.ROLL_ABSENT)
        self.assertIsNone(results[1]["values"][0]["value_numeric"])
        self.assertIsNotNone(results[1]["effective_value_policy"])

    def test_long_ir_is_not_truncated(self):
        # ``restore_implicit_values`` keeps the documented honest behavior: one result
        # per implicit definition plus an explicitly unresolved entry for every surplus
        # ir slot, so a long ir is never silently truncated at this layer. The importer
        # builder is the layer that trims the ir to the definition count and preserves
        # the surplus as ``implicit_rolls.unused_ir_values`` (asserted in
        # test_builder_acceptance / test_negative_cases).
        results = rolls.restore_implicit_values(self.BASE_23, [234, 167, 203, 99], self.catalog)
        self.assertEqual(len(results), 4)
        self.assertEqual(results[3]["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(
            results[3]["evidence"]["failure_reason"], "implicit_definition_missing_for_ir_index"
        )
        self.assertEqual(results[3]["roll_status"], rolls.ROLL_PRESENT)
        self.assertEqual(results[3]["raw_roll"], 99)

    def test_null_implicit_roll(self):
        results = rolls.restore_implicit_values(self.BASE_23, [234, None, 203], self.catalog)
        self.assertEqual(results[1]["roll_status"], rolls.ROLL_NULL)
        self.assertIsNone(results[1]["values"][0]["value_numeric"])

    def test_unknown_base_item_is_reported(self):
        results = rolls.restore_implicit_values("item:does:not:exist", [1], self.catalog)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(results[0]["evidence"]["failure_reason"], "item_not_found")


# ---------------------------------------------------------------------------
# Unique rolls
# ---------------------------------------------------------------------------
class TestUniqueRestoration(DatasetCase):
    UNIQUE_316 = "item:letools:uniqueList.uniques:316"  # Apex of Thought (head)
    HEAD_UR = [244, 77, 35, 234, 167, 211, 133, 119]

    def test_unique_316_roll_id_indexing(self):
        # Build head.ur = the list above.  unique mods (catalog order):
        #   0 property 58 specialTag 0 tags 694 canRoll=1 rollId=3 value 39 max 91
        #   1 property 119                canRoll=1 rollId=1 value 78 max 182
        #   2 property 58 specialTag 3    canRoll=0 rollId=0 value 1 max 0
        #   3 property 88                 canRoll=1 rollId=2 value 1 max 2
        #   4..9 property 42 (6 specialTags) canRoll=1 rollId=0 value 0.39 max 0.52
        #
        #  mod0: ur[rollId=3] = ur[3] = 234
        #    floor((91+1-39)*234/255 + 39) = floor(53*0.9176470588235294 + 39)
        #    = floor(48.63529411764706 + 39) = 87 ; min(87,91) = 87
        #    (using ur[0]=244 by mistake would give floor(53*0.9568627450980393+39)
        #     = floor(89.71372549019608) = 89, so this distinguishes rollId indexing)
        #  mod1: ur[1]=77 -> floor(105*77/255 + 78)
        #    = floor(31.705882352941178 + 78) = 109
        #  mod2: canRoll=0 -> definitional constant 1 (no ur slot consumed)
        #  mod3: ur[2]=35 -> floor((2+1-1)*35/255 + 1) = floor(1.2745098039215685) = 1
        #  mods4..9: ur[0]=244 -> 0.39..0.52, g=0, type=0
        #    floor(100*((0.52+0.01-0.39)*244/255 + 0.39 + 0.001))/100
        #    = floor(100*(0.14*0.9568627450980393 + 0.391))/100
        #    = floor(100*0.5249607843137255)/100 = floor(52.49607843137255)/100 = 0.52
        results = rolls.restore_unique_values(self.UNIQUE_316, self.HEAD_UR, self.catalog)
        self.assertEqual(len(results), 10)
        expected = [87.0, 109.0, 1.0, 1.0] + [0.52] * 6
        self.assertEqual(len(results), len(expected))
        for index, expected_value in enumerate(expected):
            value = results[index]["values"][0]
            self.assertAlmostEqual(
                value["value_numeric"], expected_value, places=12, msg=f"mod {index}"
            )

        # rollId, not array position, selects the slot; duplicate rollIds share it.
        self.assertEqual(results[0]["raw_roll"], 234)
        self.assertEqual(results[0]["evidence"]["roll_id"], 3)
        self.assertEqual(results[1]["raw_roll"], 77)
        self.assertEqual(results[1]["evidence"]["roll_id"], 1)
        self.assertEqual(results[3]["raw_roll"], 35)
        self.assertEqual(results[3]["evidence"]["roll_id"], 2)
        for index in range(4, 10):
            self.assertEqual(results[index]["raw_roll"], 244)
            self.assertEqual(results[index]["evidence"]["roll_id"], 0)
            # six rollable mods share rollId 0 (the non-rollable mod with rollId 0
            # is definitional and does not consume an ur slot)
            self.assertEqual(results[index]["evidence"]["shared_roll_id_mods"], 6)

        # Non-rollable mod: constant, no roll consumed.
        self.assertEqual(results[2]["roll_status"], rolls.ROLL_ABSENT)
        self.assertEqual(results[2]["values"][0]["direction"], rolls.DIRECTION_CONSTANT)
        self.assertEqual(results[2]["values"][0]["value_numeric"], 1.0)

        # Property association follows the archived getProperty dispatch:
        # property 58 is the AbilityProperty marker (tags = ability id 694).
        self.assertEqual(
            results[0]["values"][0]["stat_id"], "stat:letools:coreDB.abilityPropertyList:694.0"
        )
        self.assertEqual(results[0]["values"][0]["round_added_to_int"], 1)
        self.assertEqual(results[1]["values"][0]["stat_id"], "stat:letools:coreDB.propertyList:119")

    def test_unique_missing_ur_yields_absent_not_zero(self):
        results = rolls.restore_unique_values(self.UNIQUE_316, [], self.catalog)
        self.assertEqual(len(results), 10)
        for index in (0, 1, 3, 4):
            self.assertEqual(results[index]["roll_status"], rolls.ROLL_ABSENT)
            self.assertIsNone(results[index]["values"][0]["value_numeric"])
            self.assertIsNotNone(results[index]["effective_value_policy"])
            value = results[index]["values"][0]["value_numeric"]
            self.assertNotEqual(value, 0)  # never silently becomes 0
        # Non-rollable mod is still definitionally 1.
        self.assertEqual(results[2]["values"][0]["value_numeric"], 1.0)

    def test_unique_unknown_entity(self):
        results = rolls.restore_unique_values("item:does:not:exist", [1, 2], self.catalog)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(results[0]["evidence"]["failure_reason"], "item_not_found")


# ---------------------------------------------------------------------------
# The build's six absent affix rolls (must all be reported explicitly)
# ---------------------------------------------------------------------------
class TestBuildAbsentRolls(DatasetCase):
    EXPECTED_ABSENT_PATHS = {
        "equipment/hands/sealedAffix",
        "equipment/hands/primordialAffix",
        "equipment/relic/affixes[1]",
        "equipment/relic/affixes[2]",
        "equipment/relic/affixes[3]",
        "equipment/relic/primordialAffix",
    }

    def _iter_affix_instances(self):
        data = self.build["data"]
        for slot, item in data["equipment"].items():
            for index, instance in enumerate(item.get("affixes") or []):
                yield f"equipment/{slot}/affixes[{index}]", instance
            for channel in ("sealedAffix", "primordialAffix", "corruptedAffix"):
                instance = item.get(channel)
                if isinstance(instance, dict):
                    yield f"equipment/{slot}/{channel}", instance
        for idol_index, idol in enumerate(data["idols"]):
            for index, instance in enumerate(idol.get("affixes") or []):
                yield f"idols[{idol_index}]/affixes[{index}]", instance
            for channel in ("sealedAffix", "primordialAffix", "corruptedAffix"):
                instance = idol.get(channel)
                if isinstance(instance, dict):
                    yield f"idols[{idol_index}]/{channel}", instance
        for key, container in (data.get("weaverTreeContainers") or {}).items():
            for index, instance in enumerate(container.get("affixes") or []):
                yield f"weaverTreeContainers[{key}]/affixes[{index}]", instance

    def test_exactly_six_absent_and_all_reported(self):
        instances = list(self._iter_affix_instances())
        absent = [(path, instance) for path, instance in instances if "r" not in instance]
        self.assertEqual(len(instances), 47)
        self.assertEqual(len(absent), 6)
        self.assertEqual({path for path, _ in absent}, self.EXPECTED_ABSENT_PATHS)

        for path, instance in absent:
            entity = self.affix_entity_for_encoded(instance["id"])
            tier_index = instance["tier"] - 1
            result = rolls.restore_affix_value(entity, tier_index, rolls.MISSING_ROLL, self.catalog)
            self.assertEqual(result["roll_status"], rolls.ROLL_ABSENT, msg=path)
            self.assertIsNone(result["raw_roll"], msg=path)
            for value in result["values"]:
                if value["direction"] == rolls.DIRECTION_CONSTANT:
                    self.assertIsNotNone(value["value_numeric"], msg=path)
                else:
                    self.assertIsNone(value["value_numeric"], msg=path)
                    self.assertIsNotNone(value["derived_display_value"], msg=path)
            if any(value["value_numeric"] is None for value in result["values"]):
                self.assertEqual(result["status"], rolls.STATUS_PARTIAL, msg=path)
                self.assertIsNotNone(result["effective_value_policy"], msg=path)
                self.assertEqual(
                    result["effective_value_policy"]["labelled"],
                    "derived_display_value_not_source_roll",
                    msg=path,
                )

        # Spot-check the two constant-range cases and two non-constant ones.
        by_path = {path: instance for path, instance in absent}
        sealed = by_path["equipment/hands/sealedAffix"]  # affix 1147, tier 7
        self.assertEqual(int(self.decoded[sealed["id"]]), 1147)
        sealed_result = rolls.restore_affix_value(
            self.affix_entity_for_encoded(sealed["id"]), sealed["tier"] - 1, rolls.MISSING_ROLL, self.catalog
        )
        self.assertEqual(sealed_result["status"], rolls.STATUS_RESOLVED)
        # constant 0.01 -> rrl6o g=0 -> rrlb(1.00)/100 = 0.01 -> display "1%"
        self.assertAlmostEqual(sealed_result["values"][0]["value_numeric"], 0.01, places=12)
        self.assertEqual(sealed_result["values"][0]["display_value"], "1%")

        relic_72 = by_path["equipment/relic/affixes[1]"]
        self.assertEqual(int(self.decoded[relic_72["id"]]), 72)
        relic_result = rolls.restore_affix_value(
            self.affix_entity_for_encoded(relic_72["id"]), relic_72["tier"] - 1, rolls.MISSING_ROLL, self.catalog
        )
        self.assertEqual(relic_result["status"], rolls.STATUS_PARTIAL)
        # 0.4..0.6 average 0.5 -> 0.5, but the actual rolled value stays unknown.
        self.assertAlmostEqual(relic_result["values"][0]["derived_display_value"], 0.5, places=12)

    def test_all_present_build_affix_rolls_restore(self):
        instances = list(self._iter_affix_instances())
        present = [(path, instance) for path, instance in instances if "r" in instance]
        self.assertEqual(len(present), 41)
        for path, instance in present:
            entity = self.affix_entity_for_encoded(instance["id"])
            result = rolls.restore_affix_value(
                entity, instance["tier"] - 1, instance["r"], self.catalog
            )
            self.assertEqual(result["roll_status"], rolls.ROLL_PRESENT, msg=path)
            self.assertEqual(result["raw_roll"], instance["r"], msg=path)
            self.assertIn(result["status"], (rolls.STATUS_RESOLVED, rolls.STATUS_PARTIAL), msg=path)
            self.assertEqual(len(result["values"]) >= 1, True, msg=path)


# ---------------------------------------------------------------------------
# Malformed / ambiguous catalog data must stay unresolved, not guessed
# ---------------------------------------------------------------------------
class TestHonestUnresolvedBranches(unittest.TestCase):
    def test_property_roll_count_mismatch_is_not_truncated(self):
        catalog = StubCatalog(
            affix=_affix_row([{"property": 13, "tags": 0, "specialTag": 0, "modifierType": 0}]),
            tiers=[_tier_row(0, [{"min": 0.1, "max": 0.2}, {"min": 0.3, "max": 0.4}])],
        )
        result = rolls.restore_affix_value("affix:stub:1", 0, 128, catalog)
        self.assertEqual(result["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(result["evidence"]["failure_reason"], "affix_property_roll_count_mismatch")
        self.assertEqual(result["evidence"]["affix_properties"], 1)
        self.assertEqual(result["evidence"]["tier_rolls"], 2)
        self.assertEqual(result["values"], [])

    def test_missing_tier_extensions_cannot_guess_direction(self):
        catalog = StubCatalog(
            affix=_affix_row([{"property": 13, "tags": 0, "specialTag": 0, "modifierType": 0}]),
            tiers=[
                {
                    "tier_id": "stub#tier#0",
                    "tier_index": 0,
                    "roll_min": 0.1,
                    "roll_max": 0.2,
                    "extra_rolls": None,
                    "extensions": None,
                }
            ],
        )
        result = rolls.restore_affix_value("affix:stub:1", 0, 128, catalog)
        self.assertEqual(result["status"], rolls.STATUS_UNRESOLVED)
        self.assertEqual(result["evidence"]["failure_reason"], "tier_roll_definition_missing")

    def test_unresolved_stat_is_partial_but_value_still_computed(self):
        catalog = StubCatalog(
            affix=_affix_row([{"property": 999, "tags": 0, "specialTag": 0, "modifierType": 0}]),
            tiers=[_tier_row(0, [{"min": 0.1, "max": 0.2}])],
        )
        result = rolls.restore_affix_value("affix:stub:1", 0, 128, catalog)
        # g defaults to 0 (archived getProperty miss); e=128/255=0.5019607843137255
        # 100*((0.2+0.01-0.1)*e + 0.1 + 0.001)/100 = 100*0.15621568627450982/100
        # = 15.621568627450983 -> floor 15 -> 0.15
        self.assertEqual(result["status"], rolls.STATUS_PARTIAL)
        value = result["values"][0]
        self.assertAlmostEqual(value["value_numeric"], 0.15, places=12)
        self.assertIsNone(value["stat_id"])
        self.assertIsNone(value["unit"])
        self.assertIsNone(value["display_value"])
        self.assertEqual(value["round_added_to_int"], 0)

    def test_constant_range_with_unresolved_stat_still_partial(self):
        catalog = StubCatalog(
            affix=_affix_row([{"property": 999, "tags": 0, "specialTag": 0, "modifierType": 0}]),
            tiers=[_tier_row(0, [{"min": 6.0, "max": 6.0}])],
        )
        result = rolls.restore_affix_value("affix:stub:1", 0, rolls.MISSING_ROLL, catalog)
        self.assertEqual(result["status"], rolls.STATUS_PARTIAL)
        self.assertEqual(result["values"][0]["value_numeric"], 6.0)

    def test_unique_mod_malformed_roll_id(self):
        mods = [
            {
                "value": 1,
                "maxValue": 2,
                "property": 88,
                "specialTag": 0,
                "tags": 0,
                "type": 0,
                "canRoll": 1,
                "rollId": "three",
                "hideInTooltip": 0,
            }
        ]
        catalog = StubCatalog(items={"item:stub:unique": {"entity_id": "item:stub:unique"}}, mods={"item:stub:unique": mods})
        results = rolls.restore_unique_values("item:stub:unique", [10, 20], catalog)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], rolls.STATUS_PARTIAL)
        self.assertEqual(results[0]["evidence"]["failure_reason"], "unique_mod_roll_id_malformed")
        self.assertIsNone(results[0]["values"][0]["value_numeric"])
        # The declared range is still known, so the average branch is a labelled
        # derived display value: stat unresolved -> g=0 -> rrlb(100*1.5)/100 = 1.5.
        self.assertAlmostEqual(results[0]["values"][0]["derived_display_value"], 1.5, places=12)
        self.assertIsNotNone(results[0]["effective_value_policy"])

    def test_argument_order_compatibility(self):
        # Both documented call orders must be accepted and return identical data.
        catalog = StubCatalog(
            affix=_affix_row([{"property": 13, "tags": 0, "specialTag": 0, "modifierType": 0}]),
            tiers=[_tier_row(0, [{"min": 0.1, "max": 0.2}])],
            stats={(13, 0, 0): _stat("stat:stub:13", "Stub Percent", "percent", 0, pct_add=1)},
        )
        entity_first = rolls.restore_affix_value("affix:stub:1", 0, 128, catalog)
        catalog_first = rolls.restore_affix_value(catalog, "affix:stub:1", 0, 128)
        self.assertEqual(entity_first["status"], catalog_first["status"])
        self.assertEqual(entity_first["values"][0]["value_numeric"], catalog_first["values"][0]["value_numeric"])
        # 0.1..0.2, r=128, g=0: 0.15 (see test_unresolved_stat_is_partial...) -> "15%"
        self.assertAlmostEqual(entity_first["values"][0]["value_numeric"], 0.15, places=12)
        self.assertEqual(entity_first["values"][0]["display_value"], "15%")

        # catalog-first must still distinguish "no roll key" (3 args -> absent) from
        # "explicit source null" (4 args with None -> null).
        catalog_first_absent = rolls.restore_affix_value(catalog, "affix:stub:1", 0)
        catalog_first_null = rolls.restore_affix_value(catalog, "affix:stub:1", 0, None)
        self.assertEqual(catalog_first_absent["roll_status"], rolls.ROLL_ABSENT)
        self.assertEqual(catalog_first_null["roll_status"], rolls.ROLL_NULL)

        base = "item:stub:base"
        items = {base: {"entity_id": base, "implicit_text": json.dumps([{"property": 13, "tags": 0, "specialTag": 0, "type": 0, "implicitValue": 0.1, "implicitMaxValue": 0.2}])}}
        stat_catalog = StubCatalog(
            items=items,
            stats={(13, 0, 0): _stat("stat:stub:13", "Stub Percent", "percent", 0, pct_add=1)},
        )
        implicit_entity_first = rolls.restore_implicit_values(base, [128], stat_catalog)
        implicit_catalog_first = rolls.restore_implicit_values(stat_catalog, base, [128])
        self.assertEqual(
            implicit_entity_first[0]["values"][0]["value_numeric"],
            implicit_catalog_first[0]["values"][0]["value_numeric"],
        )


# ---------------------------------------------------------------------------
# Evidence: archived script hash, offsets and transcribed text
# ---------------------------------------------------------------------------
class TestEvidenceWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime_path = DISCOVERY / "planner-runtime.js"
        cls.raw = cls.runtime_path.read_bytes()
        cls.text = cls.raw.decode("utf-8")
        cls.capture_meta = _load_json(DISCOVERY / "capture-meta.json")

    def test_rule_version(self):
        self.assertEqual(rolls.CODE_RULE_VERSION, "letools-roll-rules-v1")

    def test_archived_runtime_sha256_matches_capture_meta(self):
        digest = hashlib.sha256(self.raw).hexdigest()
        self.assertEqual(digest, rolls.EVIDENCE["runtime"]["sha256"])
        self.assertEqual(digest, self.capture_meta["planner_runtime"]["sha256"])

    def test_every_evidenced_symbol_offset_is_exact(self):
        for name, meta in rolls.EVIDENCE["symbols"].items():
            found = self.text.find("function " + name + "(")
            self.assertEqual(
                found, meta["offset"], msg=f"offset drift for archived symbol {name}"
            )

    def test_roll_rule_block_bounds(self):
        block = rolls.EVIDENCE["runtime"]["roll_rule_block"]
        start, end = block["start"], block["end"]
        self.assertEqual(self.text[start : start + 14], "function rrl5o")
        self.assertEqual(self.text[end : end + 15], "function rrlso(")
        segment = self.text[start:end]
        for name in ("rrl5o", "rrlM", "rrlZm", "rrl7o", "rrl6o"):
            self.assertIn("function " + name + "(", segment)

    def test_transcribed_branches_appear_verbatim(self):
        segment = self.text[
            rolls.EVIDENCE["runtime"]["roll_rule_block"]["start"] :
            rolls.EVIDENCE["runtime"]["roll_rule_block"]["end"]
        ]
        # rrl7o starts with roll/255 and an ascending/inverted decision.
        self.assertIn("function rrl7o(a,b,c,d,e){e/=255;if(c<=d){", segment)
        # ascending general + g=0 fallback share the two-decimal formula.
        self.assertIn(
            "if(0!=b)return c=rrlb(100*c)/100,d=rrlb(100*d)/100,"
            "Math.min(Math.floor(100*((d+.01-c)*e+c+.001))/100,d)",
            segment,
        )
        self.assertIn(
            "return Math.min(Math.floor(100*((d+.01-c)*e+c+.001))/100,",
            segment,
        )
        # g = 1, 2, 3 ascending branches.
        self.assertIn("if(1==a)return c=rrlb(c),d=rrlb(d),Math.min(Math.floor((d+1-c)*e+c),d)", segment)
        self.assertIn(
            "if(2==a)return c=rrlb(10*c)/10,d=rrlb(10*d)/10,"
            "Math.min(Math.floor(10*((d+.1-c)*e+c))/10,d)",
            segment,
        )
        self.assertIn(
            "if(3==a)return c=rrlb(1E3*c)/1E3,d=rrlb(1E3*d)/1E3,"
            "Math.min(Math.floor(1E3*((d+.001-c)*e+c))/1E3,d)",
            segment,
        )
        # inverted branches.
        self.assertIn("Math.max(Math.ceil((c-d-1)*e+d),c)", segment)
        self.assertIn(
            "Math.max(Math.ceil(10*((d-.1-c)*e+c))/10,d)", segment
        )
        self.assertIn(
            "return Math.max(Math.ceil(100*((d-.01-c)*e+c))/100,d)}", segment
        )
        # average branch.
        self.assertIn("function rrl6o(a,b,c){null==c&&(c=b);b=(b+c)/2;", segment)
        # banker's rounding helper.
        self.assertIn(
            "return(.5===Math.abs(a)%1?0===c%2?c:c-1:c)/Math.pow(10,b)",
            self.text,
        )
        # incidence of a roll is gated by canRoll and null throws (rrlZm).
        self.assertIn(
            "function rrlZm(a,b,c,d){if(!a.canRoll)return b;"
            'if(null==d)throw"roll shouldn\'t be null";',
            segment,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
