"""Negative / boundary regression tests for the phase-2 importer (P2-5, plan §11.2).

These tests craft small synthetic payloads and push them through the real builder and
the real read-only catalog, covering the boundaries that the fixed sample does not:

* ``r=0`` / ``r=255`` / ``r=null`` / missing ``r`` stay four distinct states;
* an unknown encoded item/affix id stays unresolved with its source path and the
  original encoded id preserved (no invented entity, no renamed field);
* source fields this importer does not interpret are preserved verbatim and reported;
* the weaver tree/containers/progression are excluded by user scope (2026-11-03):
  deleting or mutating them does not change any in-scope import result, they are never
  treated as allocatable nodes / item instances, and the raw payload stays archived;
* a catalog whose bytes do not match the recorded fingerprint is rejected;
* a duplicate import does not create a second revision.

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from le_build.builder import BuildSpecBuilder
from le_build.catalog import Catalog, CatalogError
from le_build.paths import BuildPaths
from le_build.resolve import Resolver
from le_build.sources import letools
from le_build.validate import coverage

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my" / "build-response.json"

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


def _load_restorer():
    try:
        from le_build import rolls as rolls_module
    except ImportError:  # pragma: no cover
        return None
    required = ("restore_affix_value", "restore_implicit_values", "restore_unique_values")
    if all(callable(getattr(rolls_module, name, None)) for name in required):
        return rolls_module
    return None  # pragma: no cover


def _in_scope_view(spec: dict) -> dict:
    """Everything the import must compute, independent of the excluded weaver payload.

    ``meta`` (timestamps, raw hash), ``extensions`` (raw pointers) and
    ``weaver.present_in_source`` are intentionally excluded because they describe the
    raw payload itself, which differs between the fixture and its mutated variants.
    """
    weaver = spec.get("weaver") or {}
    view = {
        "scope": spec.get("scope"),
        "weaver": {
            "status": weaver.get("status"),
            "source_fields": weaver.get("source_fields"),
            "raw_preserved": weaver.get("raw_preserved"),
        },
        "character": spec.get("character"),
        "passive_tree": spec.get("passive_tree"),
        "specializations": spec.get("specializations"),
        "action_bar": spec.get("action_bar"),
        "equipment": spec.get("equipment"),
        "idols": spec.get("idols"),
        "blessings": spec.get("blessings"),
        "context": spec.get("context"),
        "progression": spec.get("progression"),
        "readable_summary": spec.get("readable_summary"),
        "statuses": spec.get("statuses"),
        "issues": sorted(
            (
                issue.get("code"),
                issue.get("severity"),
                issue.get("path"),
                issue.get("message"),
            )
            for issue in spec.get("issues") or []
        ),
    }
    # readable blocks are part of the in-scope result and must be unchanged too.
    view["readable"] = [
        instance.get("readable")
        for instance in (
            [slot.get("instance") for slot in (spec.get("equipment") or {}).get("slots") or []]
            + [entry.get("instance") for entry in (spec.get("idols") or {}).get("entries") or []]
            + [entry.get("instance") for entry in (spec.get("blessings") or {}).get("entries") or []]
        )
    ]
    return view


def minimal_document() -> dict:
    """Smallest document that still satisfies the supported protocol shape."""
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


def _affix_entry(encoded_id: str, tier=1, **kwargs) -> dict:
    entry = {"id": encoded_id, "tier": tier}
    entry.update(kwargs)
    return entry


class NegativeCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # These negative cases exercise the historical phase-1 dataset release
        # ``phase1-dataset-002``, which is superseded and kept as an external
        # immutable asset (see docs/assets-external.json) instead of being versioned.
        # Without it the cases are declared as skipped rather than erroring; the
        # release package ships the asset so the package acceptance run covers them.
        release_db = REPO / "data" / "releases" / "phase1-dataset-002" / "data.sqlite"
        if not release_db.is_file():
            raise unittest.SkipTest(
                "历史发布数据 phase1-dataset-002 未随仓库分发（外部不可变资产）："
                "缺少 data/releases/phase1-dataset-002/data.sqlite；"
                "恢复步骤见 docs/assets-external.json，迁移对照见 docs/migration-map.csv"
            )
        cls.catalog = Catalog.open(root=REPO, dataset="phase1-dataset-002", verify=True)
        cls.restorer = _load_restorer()
        cls.target_version = cls.catalog.meta().get("target_game_version") or ""
        cls._documents: dict[str, dict] = {}
        cls._specs: dict[str, dict] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()

    # ---------------------------------------------------------------- helpers
    def build_from_document(self, document: dict, *, share_id: str) -> dict:
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        fetch = {
            "mode": "file",
            "file_path": "<synthetic>",
            "raw_sha256": __import__("hashlib").sha256(body).hexdigest(),
            "raw_bytes": body,
            "document": document,
            "http_status": None,
            "api_url": None,
        }
        draft = letools.build_draft(fetch, share_id=share_id, requested_url=None)
        compatibility = letools.classify_versions(draft["versions"], self.target_version)
        builder = BuildSpecBuilder(self.catalog, Resolver(self.catalog), restorer=self.restorer)
        return builder.build(
            draft=draft,
            fetch_meta={
                "attempt_id": f"negative-{share_id}",
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

    def one_item_spec(self, item: dict, *, share_id: str) -> dict:
        document = minimal_document()
        document["data"]["equipment"] = {"head": item}
        return self.build_from_document(document, share_id=share_id)

    def issues_with(self, spec: dict, code: str) -> list[dict]:
        return [issue for issue in spec["issues"] if issue["code"] == code]

    # ------------------------------------------------ r = 0/255/null/absent
    def test_roll_boundary_values_stay_distinct(self) -> None:
        item = {
            "id": "UAwJgzGCMBsQ",  # Apex of Thought (unique), base Runed Visage
            "affixes": [
                _affix_entry("AAwRgzEA", tier=7, r=0),
                _affix_entry("AAwRgzEA", tier=7, r=255),
                _affix_entry("AAwRgzEA", tier=7, r=None),
                _affix_entry("AAwRgzEA", tier=7),
            ],
        }
        spec = self.one_item_spec(item, share_id="p2negroll")
        instance = spec["equipment"]["slots"][0]["instance"]
        entries = instance["affixes"]["normal"]
        self.assertEqual(
            [entry["roll"]["status"] for entry in entries],
            ["present", "present", "null", "absent"],
        )
        # r=0 must never be collapsed into "absent" by truthiness.
        self.assertEqual(entries[0]["roll"]["raw"], 0)
        self.assertEqual(entries[1]["roll"]["raw"], 255)
        self.assertIsNone(entries[2]["roll"]["raw"])
        self.assertIsNone(entries[3]["roll"]["raw"])
        self.assertEqual([entry["encoded_id"] for entry in entries], ["AAwRgzEA"] * 4)
        # Both "null" and "absent" are reported (distinguishably), never fabricated.
        roll_absent = self.issues_with(spec, "roll_absent")
        self.assertEqual(len(roll_absent), 2)
        self.assertEqual(
            sorted(issue["detail"]["roll_status"] for issue in roll_absent), ["absent", "null"]
        )
        rolls = coverage(spec)["rolls"]
        self.assertEqual(rolls["present"], 2)
        self.assertEqual(rolls["null"], 1)
        self.assertEqual(rolls["absent"], 1)

        if self.restorer is not None:
            values = [entry["value"] for entry in entries]
            # present ones restore, and r=0 restores a real number.
            self.assertEqual(values[0]["roll_status"], "present")
            self.assertEqual(values[0]["raw_roll"], 0)
            self.assertEqual(values[0]["status"], "resolved")
            self.assertIsNotNone(values[0]["values"][0]["value_numeric"])
            self.assertEqual(values[1]["raw_roll"], 255)
            self.assertEqual(values[1]["status"], "resolved")
            self.assertIsNotNone(values[1]["values"][0]["value_numeric"])
            # null/absent are clearly labelled derived display values only.
            for value, expected_status in zip(values[2:], ("null", "absent")):
                self.assertEqual(value["roll_status"], expected_status)
                self.assertEqual(value["status"], "partial")
                self.assertIsNone(value["values"][0]["value_numeric"])
                self.assertIsNotNone(value["values"][0].get("derived_display_value"))
                self.assertEqual(
                    value["effective_value_policy"]["labelled"],
                    "derived_display_value_not_source_roll",
                )

    # -------------------------------------------------- unknown encoded ids
    def test_unknown_affix_id_is_unresolved_and_source_path_preserved(self) -> None:
        unknown_affix = "A" + reference_lz_compress("99999")
        item = {"id": "IIwBgTKKSQ", "affixes": [_affix_entry(unknown_affix, tier=1, r=0)]}
        spec = self.one_item_spec(item, share_id="p2negunk1")
        instance = spec["equipment"]["slots"][0]["instance"]
        entry = instance["affixes"]["normal"][0]
        self.assertEqual(entry["affix"]["status"], "unresolved")
        self.assertIsNone(entry["affix"]["ref"])
        self.assertEqual(entry["source_path"], "data.equipment.head.affixes.0")
        self.assertEqual(entry["encoded_id"], unknown_affix)
        self.assertEqual(entry["codec"]["affix_id"], 99999)
        self.assertEqual(entry["tier"]["status"], "not_run")
        unresolved = self.issues_with(spec, "identity_unresolved")
        self.assertTrue(unresolved)
        affix_issue = next(issue for issue in unresolved if issue["path"] == entry["source_path"])
        self.assertEqual(affix_issue["severity"], "error")
        self.assertEqual(affix_issue["detail"]["encoded_id"], unknown_affix)
        # The item identity itself still resolves; only the affix is reported unresolved.
        self.assertIn("base", instance)
        self.assertIsNotNone(instance["base"])

    def test_unknown_base_item_id_is_unresolved(self) -> None:
        unknown_item = "I" + reference_lz_compress("1099099000")  # type 99 / base 99
        item = {"id": unknown_item, "affixes": []}
        spec = self.one_item_spec(item, share_id="p2negunk2")
        instance = spec["equipment"]["slots"][0]["instance"]
        self.assertEqual(instance["codec"]["marker"], "I")
        self.assertEqual(instance["codec"]["item_type_index"], 99)
        self.assertEqual(instance["codec"]["base_id"], 99)
        self.assertEqual(instance["base_status"], "unresolved")
        self.assertIsNone(instance["base"])
        self.assertEqual(instance["status"], "partial")
        self.assertEqual(instance["encoded_id"], unknown_item)
        issues = self.issues_with(spec, "identity_unresolved")
        source_issue = next(issue for issue in issues if issue["path"] == "data.equipment.head")
        self.assertEqual(source_issue["detail"]["encoded_id"], unknown_item)
        self.assertEqual(source_issue["severity"], "error")

    def test_unknown_unique_id_is_unresolved(self) -> None:
        unknown_unique = "U" + reference_lz_compress("023999")  # subtype 23 / unique 999
        item = {"id": unknown_unique, "affixes": []}
        spec = self.one_item_spec(item, share_id="p2negunk3")
        instance = spec["equipment"]["slots"][0]["instance"]
        self.assertEqual(instance["codec"]["unique_id"], 999)
        self.assertEqual(instance["codec"]["base_subtype_id"], 23)
        self.assertIsNone(instance["definition"])
        self.assertEqual(instance["definition_status"], "unresolved")
        self.assertEqual(instance["status"], "partial")
        self.assertEqual(instance["encoded_id"], unknown_unique)
        issues = [
            issue
            for issue in self.issues_with(spec, "identity_unresolved")
            if issue["path"] == "data.equipment.head"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["detail"]["encoded_id"], unknown_unique)

    def test_item_with_affix_marker_is_flagged_not_silently_accepted(self) -> None:
        item = {"id": "AAwRgzEA", "affixes": []}
        spec = self.one_item_spec(item, share_id="p2negmarker")
        instance = spec["equipment"]["slots"][0]["instance"]
        self.assertEqual(instance["rarity"], "affix-coded")
        self.assertIsNone(instance["definition"])
        self.assertIsNone(instance["base"])
        marker_issues = self.issues_with(spec, "item_marker_unexpected")
        self.assertEqual(len(marker_issues), 1)
        self.assertEqual(marker_issues[0]["path"], "data.equipment.head")
        self.assertEqual(marker_issues[0]["detail"]["encoded_id"], "AAwRgzEA")

    # ------------------------------------------- unknown fields are preserved
    def test_unknown_top_level_data_and_item_fields_are_preserved_and_reported(self) -> None:
        document = minimal_document()
        document["phase2TopField"] = {"keep": "top"}
        document["data"]["phase2DataField"] = [1, 2, 3]
        document["data"]["equipment"] = {
            "head": {
                "id": "IIwBgTKKSQ",
                "affixes": [],
                "phase2ItemField": "keep-me",
            }
        }
        spec = self.build_from_document(document, share_id="p2negfields")
        extensions = spec["extensions"]
        self.assertEqual(extensions["unexpected_top_level_fields"], ["phase2TopField"])
        self.assertEqual(extensions["unexpected_data_fields"], ["phase2DataField"])
        self.assertEqual(extensions["uninterpreted_top_level"]["phase2TopField"], {"keep": "top"})
        self.assertEqual(extensions["uninterpreted_data"]["phase2DataField"], [1, 2, 3])
        instance = spec["equipment"]["slots"][0]["instance"]
        self.assertEqual(instance["unrecognized_fields"], {"phase2ItemField": "keep-me"})
        unexpected = self.issues_with(spec, "unexpected_source_field")
        paths = {issue["path"] for issue in unexpected}
        self.assertIn("data", paths)
        self.assertIn("data.equipment.head", paths)
        self.assertTrue(all(issue["severity"] == "info" for issue in unexpected))

    # ------------------------------------------- weaver tree excluded by scope
    def test_weaver_payload_is_excluded_and_never_parsed_as_business_objects(self) -> None:
        document = minimal_document()
        document["data"]["equipment"] = {"head": {"id": "IIwBgTKKSQ", "affixes": []}}
        # A valid old-style weaver allocation, bogus container contents and a
        # non-dict progression must all be ignored together.
        document["data"]["weaverTree"] = {
            "selected": {"192": 3, "7": 1},
            "treeID": "weaver",
            "version": 10,
        }
        document["data"]["weaverTreeContainers"] = {
            "91": {"id": "IIwBgTKKSQ", "affixes": [], "ir": [], "ur": []},
            "92": {"id": "not-a-valid-encoded-id", "affixes": []},
        }
        document["data"]["weaverTreeProgression"] = [{"node": 1}]
        spec = self.build_from_document(document, share_id="p2negexcl")

        weaver = spec["weaver"]
        self.assertEqual(weaver["status"], "excluded_by_scope")
        self.assertEqual(
            sorted(weaver["source_fields"]),
            sorted(
                [
                    "data.weaverTree",
                    "data.weaverTreeContainers",
                    "data.weaverTreeProgression",
                ]
            ),
        )
        self.assertTrue(weaver["raw_preserved"])
        self.assertFalse(weaver.get("nodes"))
        self.assertFalse(weaver.get("containers"))
        # No node entry was invented for the selected ids / container keys.
        self.assertFalse(
            any(
                instance.get("role") == "weaver_container_content"
                for slot in spec["equipment"]["slots"]
                for instance in [slot.get("instance") or {}]
            )
        )
        # Excluded fields never produce weaver issues (范围调整 item 4).
        issue_codes = {issue["code"] for issue in spec["issues"]}
        self.assertNotIn("weaver_container_unresolved", issue_codes)
        self.assertFalse([code for code in issue_codes if "weaver" in code])
        self.assertNotIn("codec_unsupported", issue_codes)
        # The scope decision is declared, and progression keeps only a reference.
        self.assertEqual(spec["scope"]["scope_version"], "letools-build-scope-v2")
        features = [
            feature
            for feature in spec["scope"]["excluded_features"]
            if feature["feature"] == "weaver_tree"
        ]
        self.assertEqual(len(features), 1)
        self.assertEqual(
            (spec["progression"].get("excluded_by_scope") or {})
            .get("weaver_tree_progression", {})
            .get("status"),
            "excluded_by_scope",
        )
        self.assertNotIn("weaver_tree_progression", spec["progression"])
        # The in-scope item still imports and resolves normally.
        instance = spec["equipment"]["slots"][0]["instance"]
        self.assertEqual(instance["base"]["ref"]["name"], "Ruby Ring")
        self.assertEqual(instance["status"], "resolved")
        self.assertTrue(instance.get("readable"))

    def test_deleting_or_mutating_weaver_fields_does_not_change_in_scope_import(self) -> None:
        """范围调整: weaver removal/mutation/unknown ids must not affect the imported BD."""
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        baseline = self.build_from_document(fixture, share_id="p2negscope0")
        baseline_view = _in_scope_view(baseline)
        # Independent precheck evidence: the baseline really contains 29/80/47.
        self.assertEqual(len(baseline["equipment"]["slots"]), 11)
        self.assertEqual(len(baseline["idols"]["entries"]), 8)
        self.assertEqual(len(baseline["blessings"]["entries"]), 10)
        self.assertEqual(len(baseline["passive_tree"]["nodes"]), 19)
        self.assertEqual(
            sum(len(entry["nodes"]) for entry in baseline["specializations"]), 61
        )
        self.assertEqual(
            sum(
                len(entries or [])
                for instance in [
                    slot["instance"] for slot in baseline["equipment"]["slots"]
                ]
                + [entry["instance"] for entry in baseline["idols"]["entries"]]
                + [entry["instance"] for entry in baseline["blessings"]["entries"]]
                for entries in (instance.get("affixes") or {}).values()
            ),
            47,
        )

        variants: list[tuple[str, dict]] = []
        deleted = copy.deepcopy(fixture)
        for field in ("weaverTree", "weaverTreeContainers", "weaverTreeProgression"):
            deleted["data"].pop(field, None)
        variants.append(("deleted", deleted))
        mutated = copy.deepcopy(fixture)
        mutated["data"]["weaverTree"] = {
            "selected": {"999": 5, "abc": 1},
            "treeID": "unknown-weaver-tree",
            "version": 999,
        }
        mutated["data"]["weaverTreeContainers"] = {
            "777": {"id": "AAwRgzEA", "affixes": []},
            "888": "not-an-object",
        }
        mutated["data"]["weaverTreeProgression"] = [{"bogus": True}]
        variants.append(("mutated", mutated))

        for label, document in variants:
            spec = self.build_from_document(document, share_id=f"p2negscope{label}")
            self.assertEqual(spec["weaver"]["status"], "excluded_by_scope", label)
            # Every in-scope section, the readable blocks, the issues and the statuses
            # are byte-for-byte identical to the baseline import.
            self.assertEqual(_in_scope_view(spec), baseline_view, label)
            # The (mutated) raw payload is still archived and traceable.
            body = json.dumps(document, ensure_ascii=False).encode("utf-8")
            self.assertEqual(
                spec["extensions"]["raw_source"]["raw_sha256"],
                hashlib.sha256(body).hexdigest(),
                label,
            )
            self.assertEqual(spec["extensions"]["unexpected_data_fields"], [], label)

    def test_published_revision_is_the_new_scope_and_has_no_weaver_objects(self) -> None:
        pointer = json.loads(
            (REPO / "data" / "builds" / "letools" / "o3ZRm9my" / "current.json").read_text(
                encoding="utf-8"
            )
        )
        spec = json.loads(
            (
                REPO
                / "data"
                / "builds"
                / "letools"
                / "o3ZRm9my"
                / "revisions"
                / pointer["revision_id"]
                / "build.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn("scope", spec, "published revision is not the reworked scope")
        self.assertEqual(spec["scope"]["scope_version"], "letools-build-scope-v2")
        weaver = spec["weaver"]
        self.assertEqual(weaver["status"], "excluded_by_scope")
        self.assertFalse(weaver.get("nodes"))
        self.assertFalse(weaver.get("containers"))
        issue_codes = {issue["code"] for issue in spec["issues"]}
        self.assertFalse([code for code in issue_codes if "weaver" in code])

    # ---------------------------------------------------- catalog fingerprint
    def test_catalog_fingerprint_mismatch_is_rejected(self) -> None:
        with mock.patch("le_build.catalog.sha256_file", return_value="0" * 64):
            with self.assertRaises(CatalogError) as caught:
                Catalog.open(root=REPO, dataset="phase1-dataset-002", verify=True)
        self.assertIn("file sha256 mismatch", str(caught.exception))

    def test_catalog_can_be_opened_without_fingerprint_verification(self) -> None:
        catalog = Catalog.open(root=REPO, dataset="phase1-dataset-002", verify=False)
        try:
            self.assertFalse(catalog.verification["verified"])
            self.assertEqual(catalog.dataset_id, "phase1-dataset-002")
        finally:
            catalog.close()

    # ------------------------------------------------- duplicate import / store
    def test_duplicate_import_does_not_create_a_second_revision(self) -> None:
        from le_build.store import list_revisions, load_current, publish_revision

        with tempfile.TemporaryDirectory(prefix="le_negative_store_") as tmp:
            paths = BuildPaths(Path(tmp))
            spec = {
                "build_spec_version": "build-spec-v1",
                "meta": {
                    "build_id": "letools:p2negdup",
                    "share_id": "p2negdup",
                    "source": "letools",
                    "source_payload": {"raw_sha256": "a" * 64, "data_field_sha256": "b" * 64},
                },
                "character": {"level": 100},
            }
            first = publish_revision(paths, "p2negdup", copy.deepcopy(spec))
            second = publish_revision(paths, "p2negdup", copy.deepcopy(spec))
            self.assertEqual(first["state"], "published")
            self.assertEqual(second["state"], "exists")
            self.assertEqual(first["revision_id"], second["revision_id"])
            self.assertEqual(list_revisions(paths, "p2negdup"), [first["revision_id"]])
            self.assertEqual(load_current(paths, "p2negdup")["revision_seq"], 1)


# ---------------------------------------------------------------------------
# Local manual inputs: malformed documents must be rejected before publish
# (2026-11-03 follow-up interface, contract §1/§5)
# ---------------------------------------------------------------------------
SOURCE_BUILD_DIR = REPO / "data" / "builds" / "letools" / "o3ZRm9my"

# 2026-11-04: the delivered current revision is the user-synced local-input revision
# (seq 4).  Source-only facts/mutations are pinned to the baseline revision; input
# negative cases must never touch the real data/builds inputs file.
BASELINE_REVISION = "rev-dbcf35ba60b0dd95"


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_pointer() -> dict:
    pointer = _read_json(SOURCE_BUILD_DIR / "current.json")
    baseline = _read_json(SOURCE_BUILD_DIR / "revisions" / BASELINE_REVISION / "build.json")
    meta = baseline["meta"]["revision"]
    pointer.update(
        {
            "revision_id": meta["revision_id"],
            "content_sha256": meta["content_sha256"],
            "revision_seq": meta["revision_seq"],
            "published_at": meta["published_at"],
            "previous_revision_id": meta.get("previous_revision_id"),
            "statuses": baseline.get("statuses", {}),
            "updated_at": meta["published_at"],
        }
    )
    return pointer


def seed_baseline_tree(root: Path) -> Path:
    """Copy the delivered tree into ``root`` and restore the source-only baseline state."""
    target = root / "data" / "builds" / "letools" / "o3ZRm9my"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_BUILD_DIR, target)
    inputs_dir = target / "inputs"
    if inputs_dir.is_dir():
        shutil.rmtree(inputs_dir)
    current_revision = _read_json(SOURCE_BUILD_DIR / "current.json")["revision_id"]
    if current_revision != BASELINE_REVISION:
        stale = target / "revisions" / current_revision
        if stale.is_dir():
            shutil.rmtree(stale)
    (target / "current.json").write_text(
        json.dumps(_baseline_pointer(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _run_cli(root: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "le_build", "--root", str(root), *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "tools" / "build_import" / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("LE_REPO_ROOT", None)
    return subprocess.run(
        command,
        cwd=str(REPO / "tools" / "build_import"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _valid_input_entry(**overrides) -> dict:
    entry = {
        "input_id": "in-20261103T233000-0001",
        "kind": "affix_roll",
        "instance_path": "data.equipment.relic.affixes.1",
        "roll": 200,
        "created_at": "2026-11-03T23:30:00+08:00",
    }
    entry.update(overrides)
    return entry


def _valid_inputs_document(entries: list[dict] | None = None) -> dict:
    return {
        "version": "build-local-inputs-v1",
        "build_id": "letools:o3ZRm9my",
        "updated_at": "2026-11-03T23:30:00+08:00",
        "entries": entries if entries is not None else [_valid_input_entry()],
    }


class LocalInputsNegativeTest(unittest.TestCase):
    """Malformed local-input documents are rejected and never publish a revision."""

    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="le_neg_inputs_")
        self.root = Path(self._tmp.name)
        self.build_dir = seed_baseline_tree(self.root)
        target = self.build_dir
        self.pointer_path = target / "current.json"
        self.pointer_before = self.pointer_path.read_bytes()
        self.revisions_before = sorted(
            entry.name for entry in (target / "revisions").iterdir() if entry.is_dir()
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _import_raw(self, text: str, name: str) -> subprocess.CompletedProcess:
        path = self.root / "tmp-inputs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return _run_cli(
            self.root, "input", "import", "--build", "letools:o3ZRm9my", "--file", str(path)
        )

    def _import_document(self, document, name: str) -> subprocess.CompletedProcess:
        return self._import_raw(json.dumps(document, ensure_ascii=False), name)

    def _assert_rejected_without_publish(self, proc: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("error", (proc.stderr or "").lower())
        self.assertNotIn("Traceback", proc.stderr or "")
        self.assertEqual(self.pointer_path.read_bytes(), self.pointer_before)
        self.assertEqual(
            sorted(entry.name for entry in (self.build_dir / "revisions").iterdir() if entry.is_dir()),
            self.revisions_before,
        )

    def _assert_rejected_document(self, document, name: str) -> None:
        self._assert_rejected_without_publish(self._import_document(document, name))

    # ------------------------------------------------------------- valid control
    def test_valid_document_is_accepted(self) -> None:
        proc = self._import_document(_valid_inputs_document(), "valid.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inputs = self.build_dir / "inputs" / "local-inputs.json"
        self.assertTrue(inputs.is_file())
        self.assertEqual(
            self.pointer_path.read_bytes(), self.pointer_before
        )  # input import never publishes

    # --------------------------------------------------------- malformed JSON
    def test_broken_json_is_rejected_without_publish(self) -> None:
        proc = self._import_raw("{not valid json", "broken.json")
        self._assert_rejected_without_publish(proc)

    def test_bad_metadata_is_rejected_without_publish(self) -> None:
        cases = {
            "bad_version.json": {**_valid_inputs_document(), "version": "build-local-inputs-v0"},
            "bad_build_id.json": {**_valid_inputs_document(), "build_id": "letools:someone-else"},
            "entries_not_list.json": {**_valid_inputs_document(), "entries": {"a": 1}},
            "missing_entries.json": {
                "version": "build-local-inputs-v1",
                "build_id": "letools:o3ZRm9my",
            },
        }
        for name, document in cases.items():
            with self.subTest(name=name):
                self._assert_rejected_document(document, name)

    def test_duplicate_entries_are_rejected_without_publish(self) -> None:
        duplicate = [
            _valid_input_entry(input_id="in-1", roll=10),
            _valid_input_entry(input_id="in-2", roll=20),
        ]
        self._assert_rejected_document(_valid_inputs_document(duplicate), "duplicate.json")

    def test_float_or_out_of_range_rolls_in_files_are_rejected(self) -> None:
        cases = {
            "roll_float.json": [_valid_input_entry(roll=1.5)],
            "roll_too_high.json": [_valid_input_entry(roll=256)],
            "roll_negative.json": [_valid_input_entry(roll=-1)],
            "roll_string.json": [_valid_input_entry(roll="200")],
        }
        for name, entries in cases.items():
            with self.subTest(name=name):
                self._assert_rejected_document(_valid_inputs_document(entries), name)

    def test_non_finite_explicit_value_is_rejected(self) -> None:
        document = _valid_inputs_document(
            [
                {
                    "input_id": "in-explicit",
                    "kind": "explicit_value",
                    "instance_path": "data.equipment.amulet",
                    "target": "unique_mod",
                    "mod_index": 1,
                    "value_numeric": 0.42,
                    "created_at": "2026-11-03T23:30:00+08:00",
                }
            ]
        )
        raw = json.dumps(document, ensure_ascii=False)
        for token in ("NaN", "Infinity"):
            with self.subTest(token=token):
                broken = raw.replace("0.42", token)
                self.assertIn(token, broken)
                self._assert_rejected_without_publish(
                    self._import_raw(broken, f"nonfinite_{token}.json")
                )

    def test_unknown_and_legacy_instance_paths_are_rejected(self) -> None:
        cases = {
            "unknown_item.json": [_valid_input_entry(instance_path="data.equipment.wings")],
            # Old (2026-11-03 pre-rework) scope path: excluded weaver tree, not an item.
            "legacy_weaver.json": [
                _valid_input_entry(
                    kind="implicit_roll",
                    instance_path="data.weaverTree.selected.0",
                    roll=1,
                    index=0,
                )
            ],
            # A valid item path but not an affix instance path for kind=affix_roll.
            "affix_path_not_affix.json": [
                _valid_input_entry(instance_path="data.equipment.hands")
            ],
        }
        for name, entries in cases.items():
            with self.subTest(name=name):
                self._assert_rejected_document(_valid_inputs_document(entries), name)

    # ------------------------------ explicit display values (2026-11-03 fix)
    def _explicit_entry(self, **overrides) -> dict:
        entry = {
            "input_id": "in-20261103T233000-9201",
            "kind": "explicit_value",
            "instance_path": "data.equipment.amulet",
            "target": "unique_mod",
            "mod_index": 1,
            "value_numeric": 0.35,
            "created_at": "2026-11-03T23:30:00+08:00",
        }
        entry.update(overrides)
        return entry

    def test_explicit_value_outside_declared_range_is_rejected(self) -> None:
        # Omnis Physical Resistance target: declared range 1%–45% (0.01..0.45).
        cases = {
            "explicit_too_high.json": self._explicit_entry(value_numeric=0.9),
            "explicit_negative.json": self._explicit_entry(value_numeric=-0.05),
        }
        # Flat Level of Skills target: declared range 1–2.
        cases["explicit_flat_below.json"] = self._explicit_entry(mod_index=0, value_numeric=0.5)
        cases["explicit_flat_above.json"] = self._explicit_entry(mod_index=0, value_numeric=3)
        for name, entry in cases.items():
            with self.subTest(name=name):
                proc = self._import_document(_valid_inputs_document([entry]), name)
                self._assert_rejected_without_publish(proc)
                self.assertIn("outside the declared range", proc.stderr)

    def test_malformed_explicit_display_metadata_is_rejected(self) -> None:
        cases = {
            # display_unit must be percent/flat.
            "bad_display_unit.json": self._explicit_entry(
                display_value=35.0, display_unit="bogus"
            ),
            # display_value must be a finite number when present.
            "bad_display_value.json": self._explicit_entry(
                display_value="thirty-five", display_unit="percent"
            ),
        }
        for name, entry in cases.items():
            with self.subTest(name=name):
                self._assert_rejected_document(_valid_inputs_document([entry]), name)

    def test_duplicate_explicit_targets_are_rejected(self) -> None:
        entries = [
            self._explicit_entry(input_id="in-dup-1", value_numeric=0.35),
            self._explicit_entry(input_id="in-dup-2", value_numeric=0.3),
        ]
        proc = self._import_document(_valid_inputs_document(entries), "dup-explicit.json")
        self._assert_rejected_without_publish(proc)
        self.assertIn("duplicate", proc.stderr.lower())

    def test_display_value_keys_and_multi_variable_targets_are_accepted(self) -> None:
        entries = [
            self._explicit_entry(
                input_id="in-ok-1",
                mod_index=0,
                value_numeric=2.0,
                display_value=2.0,
                display_unit="flat",
            ),
            self._explicit_entry(
                input_id="in-ok-2",
                mod_index=1,
                value_numeric=0.35,
                display_value=35.0,
                display_unit="percent",
            ),
            {
                "input_id": "in-ok-3",
                "kind": "explicit_value",
                "instance_path": "data.equipment.relic.primordialAffix",
                "target": "affix",
                "affix_value_index": 1,
                "value_numeric": 1.8,
                "display_value": 180.0,
                "display_unit": "percent",
                "created_at": "2026-11-03T23:30:00+08:00",
            },
        ]
        proc = self._import_document(_valid_inputs_document(entries), "valid-explicit.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inputs = self.build_dir / "inputs" / "local-inputs.json"
        saved = json.loads(inputs.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(len(saved), 3)
        self.assertEqual([entry.get("display_unit") for entry in saved], ["flat", "percent", "percent"])
        # input import never publishes a revision.
        self.assertEqual(self.pointer_path.read_bytes(), self.pointer_before)
        self.assertEqual(
            sorted(entry.name for entry in (self.build_dir / "revisions").iterdir() if entry.is_dir()),
            self.revisions_before,
        )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
