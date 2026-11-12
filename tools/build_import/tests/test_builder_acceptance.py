"""Acceptance tests for the reworked fixed regression sample (P2-5, scope v2).

Builds one BuildSpec from the archived real response
``reports/discovery/phase-2-o3ZRm9my/build-response.json`` against the real
read-only catalog, then asserts the reworked fixed-sample facts from
``docs/phase-2-rework-contract.zh-CN.md`` §1/§3/§4 and the 2026-11-03 review's
"范围调整" acceptance:

* 29 item instances = 11 equipment + 8 idols + 10 blessings (the 5 weaver container
  contents are excluded by user scope);
* 80 node entries = 19 passive + 61 skill (the 22 weaver nodes are excluded);
* 47 affixes unchanged (the excluded containers carried no affix instances);
* the weaver tree/containers/progression are declared ``excluded_by_scope`` and never
  parsed as business objects, while the raw payload is still archived;
* every in-scope item carries a player-readable block, and the published revision is
  the reworked one (new counts, readable layer, no weaver categories in report/coverage).

The expected counts come from the plan and from the independent P2-0 precheck /
identity-probe artifacts (which describe the historical 34/102 sample), filtered to the
new in-scope roles — not from re-running the importer's own arithmetic.

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from le_build.builder import BuildSpecBuilder
from le_build.catalog import Catalog
from le_build.paths import BuildPaths
from le_build.report import acceptance_checks
from le_build.resolve import Resolver
from le_build.sources import letools
from le_build.store import content_hash, load_build
from le_build.validate import coverage, validate_spec

from _dataset_asset import require_dataset_db

REPO = Path(__file__).resolve().parents[3]
DISCOVERY = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my"
FIXTURE = DISCOVERY / "build-response.json"
CAPTURE_META = DISCOVERY / "capture-meta.json"
PRECHECK = REPO / "reports" / "phase-2" / "p2-0-precheck-dataset-002.json"
IDENTITY_PROBE = DISCOVERY / "identity-probe.json"
BUILD_REF = "letools:o3ZRm9my"
HISTORICAL_REVISION_ID = "rev-8dc664d73737a8b9"

EXPECTED_SLOT_ORDER = [
    "head",
    "chest",
    "weapon1",
    "hands",
    "waist",
    "feet",
    "ring1",
    "ring2",
    "amulet",
    "relic",
    "idol_altar",
]
EXPECTED_HUD = ["me27", "sb44eQ", "te44", "fr4wl", "bh2"]
EXPECTED_AFFIX_CHANNELS = {"normal": 37, "sealed": 2, "primordial": 2, "corrupted": 6}
EXPECTED_IN_SCOPE_COUNTS = {"equipment": 11, "idols": 8, "blessings": 10}
EXPECTED_IN_SCOPE_TOTAL = 29
EXPECTED_SCOPE_VERSION = "letools-build-scope-v2"
WEAVER_SOURCE_FIELDS = [
    "data.weaverTree",
    "data.weaverTreeContainers",
    "data.weaverTreeProgression",
]
MISSING_ROLL_PATHS = [
    "data.equipment.hands.primordialAffix",
    "data.equipment.hands.sealedAffix",
    "data.equipment.relic.affixes.1",
    "data.equipment.relic.affixes.2",
    "data.equipment.relic.affixes.3",
    "data.equipment.relic.primordialAffix",
]
READABLE_COMPLETENESS = {"complete", "partial", "definition_only", "unavailable"}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load_restorer():
    """Mirror ``cli._load_restorer`` so the build is production-shaped when possible."""
    try:
        from le_build import rolls as rolls_module
    except ImportError:  # pragma: no cover - the restorer usually exists in P2
        return None
    required = ("restore_affix_value", "restore_implicit_values", "restore_unique_values")
    if all(callable(getattr(rolls_module, name, None)) for name in required):
        return rolls_module
    return None  # pragma: no cover - contract violation in P2-4


def _all_instances(spec: dict) -> list[dict]:
    """Every in-scope item instance (29): 11 equipment + 8 idols + 10 blessings."""
    instances = [
        slot.get("instance") or {} for slot in spec.get("equipment", {}).get("slots", [])
    ]
    instances += [entry.get("instance") or {} for entry in spec.get("idols", {}).get("entries", [])]
    instances += [
        entry.get("instance") or {} for entry in spec.get("blessings", {}).get("entries", [])
    ]
    return instances


def _affix_entries(spec: dict):
    """Yield (source_path, channel, entry) for every in-scope affix instance."""
    for instance in _all_instances(spec):
        for channel, entries in (instance.get("affixes") or {}).items():
            for entry in entries or []:
                yield entry.get("source_path"), channel, entry


class AcceptanceSampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.precheck = json.loads(PRECHECK.read_text(encoding="utf-8"))
        cls.identity = json.loads(IDENTITY_PROBE.read_text(encoding="utf-8"))
        cls.capture_meta = json.loads(CAPTURE_META.read_text(encoding="utf-8"))
        cls.fixture_document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.pointer = json.loads((REPO / "data" / "current.json").read_text(encoding="utf-8"))
        cls.catalog = Catalog.open(root=REPO, dataset=cls.pointer["dataset_id"], verify=True)
        cls.restorer = _load_restorer()
        fetch = letools.load_archived_file(FIXTURE)
        cls.draft = letools.build_draft(
            fetch,
            share_id="o3ZRm9my",
            requested_url="https://www.lastepochtools.com/planner/o3ZRm9my",
        )
        compatibility = letools.classify_versions(
            cls.draft["versions"], cls.catalog.meta().get("target_game_version") or ""
        )
        builder = BuildSpecBuilder(cls.catalog, Resolver(cls.catalog), restorer=cls.restorer)
        cls.spec = builder.build(
            draft=cls.draft,
            fetch_meta={
                "attempt_id": "p2-5-fixture-attempt",
                "raw_path": "data/builds/letools/o3ZRm9my/raw/3275ac08b5d9.json",
                "raw_sha256": fetch["raw_sha256"],
                "api_url": fetch.get("api_url"),
                "fetched_at": fetch.get("fetched_at"),
                "http_status": fetch.get("http_status"),
                "fetch_mode": fetch.get("mode"),
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
        cls.compatibility = compatibility
        cls.validation = validate_spec(cls.spec, cls.catalog)
        cls.coverage = coverage(cls.spec)
        cls.acceptance = acceptance_checks(cls.spec)
        cls.published = load_build(BuildPaths(REPO), BUILD_REF)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()

    # ------------------------------------------------------ character / binding
    def test_character_is_mage_sorcerer_level_100(self) -> None:
        character = self.spec["character"]
        self.assertEqual(character["level"], 100)
        self.assertEqual(character["class"]["ref"]["name"], "Mage")
        self.assertEqual(character["mastery"]["ref"]["name"], "Sorcerer")
        self.assertEqual(character["class"]["source_value"], 1)
        self.assertEqual(character["mastery"]["source_value"], 1)
        self.assertEqual(character["class"]["status"], "resolved")
        self.assertEqual(character["mastery"]["status"], "resolved")
        self.assertEqual(character["class"]["character_class_definition"]["tree_id"], "mg-1")

    def test_catalog_binding_and_version_compatibility(self) -> None:
        catalog_section = self.spec["catalog"]
        self.assertEqual(catalog_section["dataset_id"], "phase1-dataset-003")
        self.assertEqual(catalog_section["target_game_version"], "1.5.2")
        self.assertEqual(catalog_section["source_snapshot_id"], "letools-v152-20261101")
        self.assertTrue(catalog_section["verification"]["verified"])
        self.assertEqual(catalog_section["logical_content_sha256"], self.pointer["logical_content_sha256"])
        protocol = self.spec["meta"]["protocol"]
        self.assertEqual(protocol["versions"]["normalized"]["dataVersion"], "1.5.2")
        self.assertEqual(protocol["version_compatibility"]["status"], "matched")
        self.assertEqual(self.spec["statuses"]["catalog_compatibility"], "matched")
        self.assertEqual(self.spec["statuses"]["schema"], "supported")

    # --------------------------------------------------- scope (2026-11-03)
    def test_scope_declares_the_weaver_tree_excluded(self) -> None:
        scope = self.spec["scope"]
        self.assertEqual(scope["scope_version"], EXPECTED_SCOPE_VERSION)
        included = scope["included_sections"]
        for section in (
            "character",
            "passive_tree",
            "specializations",
            "action_bar",
            "equipment",
            "idols",
            "blessings",
            "context",
            "progression",
        ):
            self.assertIn(section, included)
        self.assertNotIn("weaver_tree", included)

        weaver_features = [
            feature
            for feature in scope["excluded_features"]
            if feature.get("feature") == "weaver_tree"
        ]
        self.assertEqual(len(weaver_features), 1)
        feature = weaver_features[0]
        self.assertEqual(sorted(feature["source_fields"]), sorted(WEAVER_SOURCE_FIELDS))
        self.assertEqual(feature["decision_date"], "2026-11-03")
        self.assertIs(feature["raw_preserved"], True)
        self.assertIn("weaver", feature["reason"].lower())
        self.assertIn("2026-11-03", feature["reason"])

    def test_weaver_section_is_excluded_and_not_parsed_as_business_objects(self) -> None:
        weaver = self.spec["weaver"]
        self.assertEqual(weaver["status"], "excluded_by_scope")
        self.assertEqual(sorted(weaver["source_fields"]), sorted(WEAVER_SOURCE_FIELDS))
        self.assertIs(weaver["raw_preserved"], True)
        # No parsed weaver business objects at all (neither present nor empty lists of
        # items are required; the source is archived instead).
        self.assertFalse(weaver.get("nodes"))
        self.assertFalse(weaver.get("containers"))
        self.assertIn(weaver.get("node_entries") or 0, (0,))
        self.assertIn(weaver.get("points_total") or 0, (0,))
        for instance in _all_instances(self.spec):
            self.assertNotEqual(instance.get("role"), "weaver_container_content")
        # No weaver-related warnings/issues are produced (范围调整 item 4).
        weaver_issues = [
            issue
            for issue in self.spec["issues"]
            if "weaver" in issue["code"]
        ]
        self.assertEqual(weaver_issues, [])
        self.assertNotIn(
            "weaver_container_unresolved", {issue["code"] for issue in self.spec["issues"]}
        )

    def test_progression_no_longer_contains_weaver_progression(self) -> None:
        progression = self.spec["progression"]
        self.assertNotIn("weaver_tree_progression", progression)
        excluded = (progression.get("excluded_by_scope") or {}).get("weaver_tree_progression") or {}
        self.assertEqual(excluded.get("status"), "excluded_by_scope")
        self.assertEqual(excluded.get("source_field"), "data.weaverTreeProgression")
        self.assertIs(excluded.get("raw_preserved"), True)
        self.assertEqual(len(progression["completed_quests"]), 20)

    def test_raw_payload_is_still_archived_and_exclusion_is_traced(self) -> None:
        extensions = self.spec["extensions"]
        # The raw data field catalog still records the excluded fields as present in
        # the source; exclusion is a scope decision, not silent field loss.
        for field in ("weaverTree", "weaverTreeContainers", "weaverTreeProgression"):
            self.assertIn(field, extensions["source_field_catalog"]["data"])
            self.assertIn(field, self.fixture_document["data"])
        excluded = extensions.get("excluded_by_scope_source_fields") or {}
        for field in ("weaverTree", "weaverTreeContainers", "weaverTreeProgression"):
            entry = excluded.get(field) or {}
            self.assertEqual(entry.get("status"), "excluded_by_scope", field)
            self.assertIs(entry.get("present_in_source"), True, field)
            self.assertIs(entry.get("raw_preserved"), True, field)
        self.assertEqual(
            extensions["raw_source"]["raw_sha256"], self.capture_meta["raw_sha256"]
        )
        self.assertEqual(extensions["unexpected_data_fields"], [])

    # ------------------------------------------------------ items / slots / idols
    def test_item_instance_counts_and_identity(self) -> None:
        counts = {
            "equipment": len(self.spec["equipment"]["slots"]),
            "idols": len(self.spec["idols"]["entries"]),
            "blessings": len(self.spec["blessings"]["entries"]),
        }
        self.assertEqual(counts, EXPECTED_IN_SCOPE_COUNTS)
        self.assertEqual(sum(counts.values()), EXPECTED_IN_SCOPE_TOTAL)

        instances = _all_instances(self.spec)
        self.assertEqual(len(instances), EXPECTED_IN_SCOPE_TOTAL)
        # Every instance carries a resolved base item and a source pointer.
        self.assertTrue(all((instance.get("base") or {}).get("ref") for instance in instances))
        self.assertTrue(all(instance.get("source_path") for instance in instances))
        unique_instances = [
            instance for instance in instances if instance.get("codec_class") == "unique"
        ]
        # 7 equipped uniques + Throne of Ambition idol; the 3 container uniques are out.
        self.assertEqual(len(unique_instances), 8)
        self.assertTrue(
            all((instance.get("definition") or {}).get("ref") for instance in unique_instances)
        )
        # Independent evidence: the P2-0 precheck counted 34 instances including the
        # 5 excluded weaver container contents; filter to the new in-scope roles.
        expected_by_role = self.precheck["summary"]["items"]["by_role"]
        self.assertEqual(
            {
                "equipment": expected_by_role["equipment"],
                "idols": expected_by_role["idol"],
                "blessings": expected_by_role["blessing"],
            },
            counts,
        )
        self.assertEqual(expected_by_role["weaver_container_content"], 5)
        for slot in self.spec["equipment"]["slots"]:
            instance_body = slot["instance"]
            fixture_item = self.fixture_document["data"]["equipment"][slot["slot"]]
            self.assertEqual(instance_body["encoded_id"], fixture_item["id"])
            self.assertEqual(instance_body["role"], "equipment")

    def test_slots_are_preserved_and_absent_slot_is_not_invented(self) -> None:
        equipment = self.spec["equipment"]
        self.assertEqual(equipment["slot_order"], EXPECTED_SLOT_ORDER)
        self.assertEqual(equipment["absent_slots"], ["weapon2"])
        self.assertEqual([slot["slot"] for slot in equipment["slots"]], EXPECTED_SLOT_ORDER)
        self.assertEqual(self.spec["idols"]["new_grid"], True)
        fixture_positions = [
            (item["x"], item["y"]) for item in self.fixture_document["data"]["idols"]
        ]
        spec_positions = [
            (entry["position"]["x"], entry["position"]["y"]) for entry in self.spec["idols"]["entries"]
        ]
        self.assertEqual(spec_positions, fixture_positions)
        self.assertEqual(len([position for position in spec_positions if None not in position]), 8)
        grid = self.spec["idols"]["grid_validation"]
        self.assertEqual(grid["status"], "not_verified")
        self.assertTrue(grid["reason"])

    # -------------------------------------------------------- specs / HUD split
    def test_specializations_and_action_bar_are_independent(self) -> None:
        specializations = self.spec["specializations"]
        action_bar = self.spec["action_bar"]
        self.assertEqual(len(specializations), 5)
        self.assertEqual(len(action_bar["slots"]), 5)
        self.assertEqual(action_bar["source_ids"], EXPECTED_HUD)
        self.assertEqual([slot["source_id"] for slot in action_bar["slots"]], EXPECTED_HUD)
        self.assertEqual(action_bar["slots"][2]["ref"]["name"], "Teleport")
        self.assertEqual(action_bar["slots"][2]["specialization_slot"], None)
        expected_owner_skills = ["Enchant Weapon", "Meteor", "Flame Ward", "Black Hole", "Frost Wall"]
        self.assertEqual(
            [entry["owner_skill"]["name"] for entry in specializations], expected_owner_skills
        )
        self.assertEqual(
            [entry["tree_source_id"] for entry in specializations],
            ["sb44eQ", "me27", "fw3d", "bh2", "fr4wl"],
        )
        self.assertTrue(
            any(entry["tree_entity_id"].endswith(":fw3d") for entry in specializations)
        )
        self.assertNotIn("Teleport", [entry["owner_skill"]["name"] for entry in specializations])
        self.assertIn("te44", action_bar["source_ids"])
        self.assertNotIn("fw3d", action_bar["source_ids"])
        self.assertEqual(
            [entry["points_total"] for entry in specializations], [22, 24, 23, 36, 23]
        )
        self.assertEqual(sum(entry["points_total"] for entry in specializations), 128)
        self.assertEqual(
            [entry["source_level"] for entry in specializations], [24, 26, 28, 38, 29]
        )

    # ------------------------------------------------------------------ nodes
    def test_node_entry_counts_points_and_explicit_zeros(self) -> None:
        passive_nodes = self.spec["passive_tree"]["nodes"]
        skill_nodes = [node for entry in self.spec["specializations"] for node in entry["nodes"]]
        self.assertEqual(len(passive_nodes), 19)
        self.assertEqual(len(skill_nodes), 61)
        self.assertEqual(len(passive_nodes) + len(skill_nodes), 80)
        self.assertEqual(sum(node["points"] for node in passive_nodes), 113)
        self.assertEqual(sum(node["points"] for node in skill_nodes), 128)
        self.assertEqual(self.spec["passive_tree"]["points_total"], 113)
        self.assertTrue(all(node["status"] == "resolved" for node in passive_nodes + skill_nodes))
        for node in passive_nodes + skill_nodes:
            self.assertNotEqual(node.get("node_kind"), "weaver")

        expected_zero_paths = {
            path.replace("/", ".") for path in self.precheck["nodes"]["explicit_zero_paths"]
        }
        actual_zero_paths = set()
        for entry in self.spec["specializations"]:
            for node in entry["nodes"]:
                if node["explicit_zero"]:
                    actual_zero_paths.add(node["source_path"])
        self.assertEqual(actual_zero_paths, expected_zero_paths)
        self.assertEqual(len(actual_zero_paths), 3)
        self.assertEqual(
            [node["points"] for node in skill_nodes if node["source_path"] in expected_zero_paths],
            [0, 0, 0],
        )

    # ---------------------------------------------------------------- affixes
    def test_affix_channel_counts_and_tier_mapping(self) -> None:
        entries = list(_affix_entries(self.spec))
        self.assertEqual(len(entries), 47)
        channels: dict[str, int] = {}
        for _, channel, _entry in entries:
            channels[channel] = channels.get(channel, 0) + 1
        self.assertEqual(channels, EXPECTED_AFFIX_CHANNELS)
        self.assertEqual(self.spec["equipment"]["slots"][3]["instance"]["affix_total"], 3)
        for path, _channel, entry in entries:
            self.assertEqual(entry["tier"]["status"], "resolved", path)
            # Source tier is 1-based; catalog tier_index is source tier - 1.
            self.assertEqual(entry["tier"]["tier_index"], entry["source_tier"] - 1, path)
            self.assertEqual(entry["affix"]["status"], "resolved", path)
        distinct_ids = {entry["codec"]["affix_id"] for _, _, entry in entries}
        self.assertEqual(len(distinct_ids), 27)
        self.assertEqual(len(distinct_ids), self.precheck["affixes"]["distinct_affix_ids"])

    def test_t8_affixes_are_present_with_tier_index_7(self) -> None:
        tier8 = [entry for _, _, entry in _affix_entries(self.spec) if entry["source_tier"] == 8]
        self.assertEqual(len(tier8), 2)
        self.assertEqual({entry["codec"]["affix_id"] for entry in tier8}, {1144, 554})
        self.assertEqual({entry["tier"]["tier_index"] for entry in tier8}, {7})
        self.assertEqual({entry["affix"]["source_scope"] for entry in tier8},
                         {"affixList.singleAffixes", "affixList.multiAffixes"})

    def test_six_absent_rolls_are_located_at_exact_paths(self) -> None:
        entries = list(_affix_entries(self.spec))
        absent_paths = sorted(
            entry["source_path"]
            for _, _, entry in entries
            if entry["roll"]["status"] == "absent"
        )
        self.assertEqual(absent_paths, sorted(MISSING_ROLL_PATHS))
        self.assertEqual(len(absent_paths), 6)
        self.assertEqual(
            sorted(row["source_path"].replace("/", ".") for row in self.precheck["affixes"]["missing_rolls"]),
            sorted(MISSING_ROLL_PATHS),
        )
        present_paths = [
            entry["source_path"] for _, _, entry in entries if entry["roll"]["status"] == "present"
        ]
        self.assertEqual(len(present_paths), 41)
        # Absent rolls are reported, never fabricated as a value.
        absent_issues = [
            issue for issue in self.spec["issues"]
            if issue["code"] == "roll_absent" and issue["path"].endswith(".r")
        ]
        self.assertEqual(sorted(issue["path"] for issue in absent_issues),
                         sorted(path + ".r" for path in MISSING_ROLL_PATHS))

    def test_new_definitions_resolve_from_catalog_only(self) -> None:
        slots = {
            slot["slot"]: slot["instance"] for slot in self.spec["equipment"]["slots"]
        }
        self.assertEqual(slots["hands"]["definition"]["ref"]["name"], "Withstand the Elements")
        self.assertEqual(slots["ring2"]["definition"]["ref"]["name"], "Unsated Rage")
        entries = list(_affix_entries(self.spec))
        for affix_id in (1147, 1144, 1124):
            matches = [
                entry
                for _, _, entry in entries
                if entry["codec"]["affix_id"] == affix_id
                and entry["affix"]["status"] == "resolved"
            ]
            self.assertTrue(matches, f"affix {affix_id} not resolved")
            self.assertEqual(matches[0]["affix"]["source_scope"], "affixList.singleAffixes")
        self.assertEqual(
            self.spec["meta"]["codec"]["codec_version"], "letools-id-codec-v1"
        )

    # ------------------------------------------------------ context / progression
    def test_context_factions_and_progression_preserved(self) -> None:
        context = self.spec["context"]
        self.assertEqual(context["active_buffs"]["raw"]["skills"], ["sb44eQ"])
        self.assertEqual(context["active_buffs"]["raw"]["buffs"], [])
        self.assertEqual(context["active_buffs"]["raw"]["ailments"], [])
        self.assertEqual(len(self.spec["progression"]["completed_quests"]), 20)
        self.assertEqual(self.spec["progression"]["char_tree_progression"], [])
        self.assertEqual(self.spec["progression"]["skill_trees_progression"], [])
        # faction is preserved verbatim on every equipped item, with no semantic claim.
        for slot in self.spec["equipment"]["slots"]:
            fixture_item = self.fixture_document["data"]["equipment"][slot["slot"]]
            self.assertEqual(slot["instance"]["faction"], fixture_item["faction"])
            self.assertIsNotNone(slot["instance"]["faction_note"])
        protocol_values = self.spec["extensions"]["protocol_values"]
        self.assertEqual(protocol_values["gh"], self.fixture_document["data"]["gh"])
        self.assertEqual(protocol_values["weaponTypes"], self.fixture_document["data"]["weaponTypes"])
        self.assertEqual(protocol_values["fromSaveFile"], self.fixture_document["data"]["fromSaveFile"])
        self.assertEqual(context["conditions"]["raw"], [])
        self.assertEqual(context["conditions"]["status"], "empty")

    # --------------------------------------------------------- readable layer
    def test_readable_layer_is_present_for_all_29_in_scope_items(self) -> None:
        instances = _all_instances(self.spec)
        self.assertEqual(len(instances), 29)
        for instance in instances:
            block = instance.get("readable")
            self.assertIsInstance(block, dict, instance.get("source_path"))
            self.assertEqual(block.get("version"), "letools-readable-v1")
            self.assertIn(block.get("completeness"), READABLE_COMPLETENESS)
            self.assertTrue(block.get("lines"), instance.get("source_path"))
        summary = self.spec["readable_summary"]
        self.assertEqual(summary["version"], "letools-readable-v1")
        self.assertEqual(summary["items"], 29)
        self.assertEqual(sum(summary["by_completeness"].values()), 29)
        self.assertEqual(summary["lines_without_text"], 0)
        self.assertGreaterEqual(summary["by_completeness"].get("complete", 0), 20)
        # The read-only sample must include the blessing implicit restoration (P1-4).
        for entry in self.spec["blessings"]["entries"]:
            mine = entry["instance"]["implicit_rolls"]
            self.assertEqual(mine["status"], "present")
            self.assertGreaterEqual(mine["implicit_count"], 1)
            self.assertNotEqual(mine["status"], "definition_unavailable")

    # --------------------------------------------------------- no field loss
    def test_no_source_field_is_silently_dropped(self) -> None:
        extensions = self.spec["extensions"]
        data_keys = sorted(self.fixture_document["data"].keys())
        top_keys = sorted(self.fixture_document.keys())
        self.assertEqual(extensions["source_field_catalog"]["data"], data_keys)
        self.assertEqual(extensions["source_field_catalog"]["top_level"], top_keys)
        self.assertEqual(extensions["unexpected_top_level_fields"], [])
        self.assertEqual(extensions["unexpected_data_fields"], [])
        self.assertEqual(
            extensions["raw_source"]["raw_sha256"], self.capture_meta["raw_sha256"]
        )
        self.assertTrue(extensions["raw_source"]["archive_path"])
        self.assertTrue(extensions["raw_source"]["pointer_scheme"])
        for instance in _all_instances(self.spec):
            self.assertEqual(instance["unrecognized_fields"], {})
        errors = [issue for issue in self.spec["issues"] if issue["severity"] == "error"]
        self.assertEqual(errors, [])
        self.assertNotIn("codec_unsupported", {issue["code"] for issue in self.spec["issues"]})

    # --------------------------------------------------- validation / coverage
    def test_validation_and_acceptance_checklist_pass(self) -> None:
        self.assertEqual(self.validation["status"], "ok", self.validation)
        self.assertEqual(self.validation["fail"], 0)
        self.assertEqual(self.validation["warn"], 0)
        self.assertGreaterEqual(self.validation["pass"], 34)

        # The §11.1 acceptance list must be updated for the new scope and all pass.
        self.assertGreaterEqual(len(self.acceptance), 20)
        failed = [check for check in self.acceptance if check["status"] != "pass"]
        self.assertEqual(failed, [])
        # Count-style observed values must contain no weaver/container business
        # category; the dedicated exclusion check and the traceability check (which
        # records the excluded source field names on purpose) are exempt.
        for check in self.acceptance:
            if check["check"] not in ("item_instance_counts", "node_entry_counts", "context_preserved"):
                continue
            observed = check.get("observed")
            if isinstance(observed, dict):
                serialized = json.dumps(observed, ensure_ascii=False).lower()
                self.assertNotIn("weaver", serialized, check["check"])
                self.assertNotIn("container", serialized, check["check"])

        self.assertEqual(self.coverage["item_instances"]["total"], 29)
        self.assertEqual(self.coverage["item_instances"]["resolved"], 29)
        self.assertEqual(self.coverage["item_instances"]["identity_connected"], 29)
        self.assertEqual(self.coverage["item_instances"]["unique_instances"], 8)
        self.assertEqual(self.coverage["affix_instances"]["total"], 47)
        self.assertEqual(self.coverage["affix_instances"]["resolved"], 47)
        self.assertEqual(self.coverage["affix_instances"]["tier_resolved"], 47)
        self.assertEqual(
            self.coverage["affix_instances"]["by_channel"], EXPECTED_AFFIX_CHANNELS
        )
        self.assertEqual(self.coverage["rolls"]["absent"], 6)
        self.assertEqual(self.coverage["rolls"]["present"], 41)
        self.assertEqual(self.coverage["nodes"]["total"], 80)
        self.assertEqual(self.coverage["nodes"]["resolved"], 80)
        self.assertEqual(self.coverage["nodes"]["passive"], 19)
        self.assertEqual(self.coverage["nodes"]["skill_tree"], 61)
        self.assertEqual(self.coverage["nodes"]["explicit_zero"], 3)
        # No weaver category in coverage (范围调整 item 3/5).
        self.assertNotIn("weaver", self.coverage["nodes"])
        self.assertNotIn("containers", self.coverage)
        if self.restorer is None:  # pragma: no cover - restorer exists in P2
            self.assertEqual(self.spec["statuses"]["value_resolution"], "not_run")
        else:
            # All 47 affix values attempted; 4 are range-only due to missing rolls.
            self.assertEqual(self.coverage["values"]["computed"], 47)
            self.assertEqual(self.coverage["values"]["unresolved"], 0)
            self.assertEqual(self.coverage["values"]["partial"], 4)

    def test_statuses_are_layered_and_honest(self) -> None:
        statuses = self.spec["statuses"]
        self.assertEqual(statuses["fetch"], "ok")
        self.assertEqual(statuses["schema"], "supported")
        self.assertEqual(statuses["catalog_compatibility"], "matched")
        self.assertEqual(statuses["identity_resolution"], "resolved")
        self.assertEqual(statuses["calculation_readiness"], "not_assessed")
        self.assertEqual(statuses["scope"], EXPECTED_SCOPE_VERSION)
        detail = statuses["identity_resolution_detail"]
        self.assertEqual(detail["equipment"], {"total": 11, "resolved": 11})
        self.assertEqual(detail["idols"], {"total": 8, "resolved": 8})
        self.assertEqual(detail["blessings"], {"total": 10, "resolved": 10})
        self.assertEqual(detail["specializations"], {"total": 5, "resolved": 5})
        self.assertEqual(detail["passive_nodes"], {"total": 19, "resolved": 19})
        self.assertEqual(detail["action_bar"], {"total": 5, "resolved": 5})
        self.assertNotIn("weaver_nodes", detail)
        # The readable layer distinguishes text readiness from numeric resolution.
        self.assertEqual(statuses["text_readiness"], "complete")
        self.assertIn(statuses["value_resolution"], ("resolved", "partial"))
        self.assertIn("readable_completeness", statuses)
        self.assertEqual(self.spec["build_spec_version"], "build-spec-v1")

    # ------------------------------------------------- published artifact (RO)
    def test_published_revision_is_the_reworked_scope(self) -> None:
        pointer = json.loads(
            (REPO / "data" / "builds" / "letools" / "o3ZRm9my" / "current.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(self.published["revision_id"], pointer["revision_id"])
        # The rework must publish a NEW revision; the historical 34/102 revision is kept
        # read-only and must not be rewritten in place.
        self.assertNotEqual(self.published["revision_id"], HISTORICAL_REVISION_ID)
        stored = self.published["spec"]
        self.assertEqual(content_hash(stored), pointer["content_sha256"])
        revision_dir = self.published["revision_dir"]
        for name in (
            "build.json",
            "import-report.json",
            "import-report.zh-CN.md",
            "calculation-gap-report.json",
            "calculation-gap-report.zh-CN.md",
        ):
            self.assertTrue((revision_dir / name).is_file(), name)
        import_report = json.loads((revision_dir / "import-report.json").read_text(encoding="utf-8"))
        self.assertEqual(import_report["validation"]["status"], "ok")
        self.assertEqual(import_report["validation"]["fail"], 0)
        self.assertEqual(import_report["validation"]["warn"], 0)
        self.assertGreaterEqual(len(import_report["acceptance"]), 20)
        self.assertEqual(
            [check["status"] for check in import_report["acceptance"]],
            ["pass"] * len(import_report["acceptance"]),
        )
        counts = import_report["counts"]
        self.assertEqual(counts["item_instances"], 29)
        self.assertEqual(counts["equipment_slots"], 11)
        self.assertEqual(counts["idols"], 8)
        self.assertEqual(counts["blessings"], 10)
        self.assertEqual(counts["affix_instances"], 47)
        self.assertEqual(counts["completed_quests"], 20)
        serialized_counts = json.dumps(counts, ensure_ascii=False).lower()
        self.assertNotIn("weaver", serialized_counts)
        self.assertNotIn("container", serialized_counts)
        # Report issues must no longer contain weaver categories.
        report_issue_codes = set((import_report["issues"]["by_code"] or {}).keys())
        self.assertFalse([code for code in report_issue_codes if "weaver" in code])
        # Stored spec is the reworked shape.
        self.assertEqual(stored["scope"]["scope_version"], EXPECTED_SCOPE_VERSION)
        self.assertEqual(stored["weaver"]["status"], "excluded_by_scope")
        self.assertEqual(stored["readable_summary"]["items"], 29)
        self.assertEqual(len(_all_instances(stored)), 29)
        self.assertEqual(stored["statuses"]["identity_resolution"], "resolved")
        self.assertEqual(stored["equipment"]["slot_order"], EXPECTED_SLOT_ORDER)

    def test_historical_revision_stays_readable(self) -> None:
        historical = load_build(
            BuildPaths(REPO), BUILD_REF, revision_id=HISTORICAL_REVISION_ID
        )
        spec = historical["spec"]
        # The old revision still carries the old scope (weaver nodes/containers); it is
        # history and must stay openable for traceability.
        self.assertEqual(len(spec["weaver"]["nodes"]), 22)
        self.assertEqual(len(spec["weaver"]["containers"]), 5)
        self.assertNotIn("scope", spec)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
