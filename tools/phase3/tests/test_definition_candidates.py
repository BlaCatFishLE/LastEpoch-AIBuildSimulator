"""G1：definitions v3 候选发现与适用性判定回归。"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys_path_root = ROOT / "tools/phase3/src"
import sys

sys.path.insert(0, str(sys_path_root))

from le_calc import calculation_route, definitions_v3  # noqa: E402


def _case(
    ability: str,
    *,
    klass: str | None = None,
    tags: list[str] | None = None,
    premises: list[str] | None = None,
) -> dict:
    main_skill = {"ability_id": ability}
    if klass:
        main_skill["class"] = klass
    if tags is not None:
        main_skill["damage_tags"] = tags
    if premises is not None:
        main_skill["conversion_premises"] = premises
    return {"case_id": "fixture-case", "model": "research", "main_skill": main_skill}


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class DefinitionCandidateTest(unittest.TestCase):
    def test_three_skill_candidates_and_applicability(self):
        cases = {
            "bh": (_case("bh2", klass="Mage"), "applicable_single", "black-hole-v4"),
            "fr": (
                _case("fr11mv", klass="Mage", tags=["Fire", "Melee"]),
                "applicable_single",
                "fr-flame-reave-v4",
            ),
            "vc": (
                _case(
                    "v01cv",
                    klass="Sentinel",
                    tags=["Fire"],
                    premises=["Riftflame", "Hellish Chasm"],
                ),
                "applicable_single",
                "void-cleave-fire-main-v4",
            ),
        }
        for name, (case, overall, template_id) in cases.items():
            with self.subTest(skill=name):
                report = calculation_route.discover_definition_candidates(case, root=ROOT)
                self.assertEqual(report["schema"], "le-definition-candidates-v1")
                self.assertEqual(report["status"], overall, report)
                selected = report.get("selected") or {}
                self.assertEqual(selected.get("template_id"), template_id)
                self.assertEqual(selected.get("status"), "applicable")
                self.assertTrue(selected.get("sha256"))
                self.assertTrue(selected.get("revision", "").startswith("sha256:"))
                self.assertTrue(selected.get("capabilities"))

    def test_same_ability_but_inapplicable_is_not_selected(self):
        wrong_class = calculation_route.discover_definition_candidates(
            _case("bh2", klass="Sentinel"), root=ROOT
        )
        self.assertEqual(wrong_class["status"], "not_applicable")
        self.assertFalse(wrong_class.get("selected"))
        # VC applicability does not constrain damage tags; without the conversion
        # premises the candidate is undetermined (never silently selected).
        missing_premises = calculation_route.discover_definition_candidates(
            _case("v01cv", klass="Sentinel", tags=["Void"]), root=ROOT
        )
        self.assertEqual(missing_premises["status"], "undetermined")
        wrong_class = calculation_route.discover_definition_candidates(
            _case("v01cv", klass="Mage"), root=ROOT
        )
        self.assertEqual(wrong_class["status"], "not_applicable")
        undetermined = calculation_route.discover_definition_candidates(
            _case("v01cv"), root=ROOT
        )
        self.assertEqual(undetermined["status"], "undetermined")
        self.assertTrue(undetermined["candidates"][0]["missing_facts"])
        route = calculation_route.resolve_calculation_route(
            copy.deepcopy(_case("bh2", klass="Mage")), root=ROOT
        )
        self.assertEqual(route["engine"], "unified_dps")
        self.assertEqual(route["status"], "needs_input")
        self.assertEqual(route["source"]["kind"], "definitions_v3")
        self.assertEqual(route["source"]["candidate"]["status"], "applicable")
        self.assertIn("scenario_required", {item["code"] for item in route["diagnostics"]})
        self.assertNotIn("legacy", str(route).lower())

    def test_no_candidate_is_needs_research_and_never_falls_back(self):
        report = calculation_route.discover_definition_candidates(
            _case("zz99", klass="Mage"), root=ROOT
        )
        self.assertEqual(report["status"], "no_candidate")
        route = calculation_route.resolve_calculation_route(
            copy.deepcopy(_case("zz99", klass="Mage")), root=ROOT
        )
        self.assertEqual(route["engine"], "needs_research")
        self.assertEqual(route["status"], "needs_research")
        self.assertEqual(route["source"]["kind"], "definitions_v3")
        self.assertIsNone(route.get("fallback"))

    def test_temp_catalog_multiple_bad_and_capability_gap(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch", prefix="g1-") as tmp:
            root = Path(tmp)
            bh = json.loads(
                (ROOT / "knowledge/definitions/black-hole-v3.json").read_text(encoding="utf-8")
            )
            bh_second = copy.deepcopy(bh)
            bh_second["template_id"] = "bh-black-hole-v3-copy"
            _write(root / "knowledge/definitions/bh-a.json", bh)
            _write(root / "knowledge/definitions/bh-b.json", bh_second)
            vc = json.loads(
                (ROOT / "knowledge/definitions/void-cleave-fire-main-v3.json").read_text(
                    encoding="utf-8"
                )
            )
            _write(root / "knowledge/definitions/vc.json", vc)
            (root / "knowledge/definitions/bad.json").write_text("{not-json", encoding="utf-8")
            entries = [
                {
                    "template_id": "bh-black-hole-v3",
                    "path": "knowledge/definitions/bh-a.json",
                    "capabilities": ["dot_components", "ignite_clock", "charge_schedule"],
                    "applicability": {
                        "ability_id": "bh2",
                        "required_class": "Mage",
                        "required_facts": ["main_skill.ability_id", "main_skill.class"],
                    },
                },
                {
                    "template_id": "bh-black-hole-v3-copy",
                    "path": "knowledge/definitions/bh-b.json",
                    "capabilities": ["dot_components", "ignite_clock", "charge_schedule"],
                    "applicability": {
                        "ability_id": "bh2",
                        "required_class": "Mage",
                        "required_facts": ["main_skill.ability_id", "main_skill.class"],
                    },
                },
                {
                    "template_id": "void-cleave-fire-main-v3",
                    "path": "knowledge/definitions/vc.json",
                    "capabilities": ["dot_components"],
                    "applicability": {
                        "ability_id": "v01cv",
                        "required_facts": ["main_skill.ability_id"],
                    },
                },
            ]
            _write(
                root / "knowledge/definitions/index.json",
                {"schema": "le-definition-catalog-v1", "entries": entries},
            )
            multiple = calculation_route.discover_definition_candidates(
                _case("bh2", klass="Mage"), root=root
            )
            self.assertEqual(multiple["status"], "applicable_multiple")
            route = calculation_route.resolve_calculation_route(
                copy.deepcopy(_case("bh2", klass="Mage")), root=root
            )
            self.assertEqual(route["engine"], "needs_input")
            self.assertIn(
                "multiple_definition_candidates",
                {item["code"] for item in route["diagnostics"]},
            )
            gap = calculation_route.discover_definition_candidates(
                _case("v01cv", klass="Sentinel"), root=root
            )
            vc_candidate = next(
                item for item in gap["candidates"] if item["template_id"] == "void-cleave-fire-main-v3"
            )
            self.assertEqual(vc_candidate["status"], "capability_gap")
            self.assertIn("能力", " ".join(vc_candidate["reasons"]))
            missing_catalog = copy.deepcopy(bh)
            missing_catalog["template_id"] = "bh-no-catalog"
            _write(root / "knowledge/definitions/nocatalog.json", missing_catalog)
            report = calculation_route.discover_definition_candidates(
                _case("zz99", klass="Mage"), root=root
            )
            self.assertIn("bad_definition_template", {item["code"] for item in report["diagnostics"]})

    def test_explicit_binding_rejects_capability_gap_and_definite_mismatch(self):
        class FakeContext:
            def __init__(self, case):
                self.case = case

        vc = json.loads(
            (ROOT / "knowledge/definitions/void-cleave-fire-main-v3.json").read_text(
                encoding="utf-8"
            )
        )
        vc["capabilities"] = ["dot_components"]
        with self.assertRaisesRegex(definitions_v3.DefinitionBindingError, "capability_gap"):
            definitions_v3.bind_template(
                vc,
                context=FakeContext(
                    {"main_skill": {"ability_id": "v01cv", "class": "Sentinel", "damage_tags": ["Fire"]}}
                ),
                scenario={},
            )
        bh = json.loads(
            (ROOT / "knowledge/definitions/black-hole-v3.json").read_text(encoding="utf-8")
        )
        catalog = {
            entry["template_id"]: entry
            for entry in json.loads(
                (ROOT / "knowledge/definitions/index.json").read_text(encoding="utf-8")
            )["entries"]
        }
        bh["capabilities"] = catalog["bh-black-hole-v3"]["capabilities"]
        bh["applicability"] = catalog["bh-black-hole-v3"]["applicability"]
        with self.assertRaisesRegex(definitions_v3.DefinitionBindingError, "not_applicable"):
            definitions_v3.bind_template(
                bh,
                context=FakeContext({"main_skill": {"ability_id": "bh2", "class": "Sentinel"}}),
                scenario={},
            )

    def test_catalog_metadata_is_used_and_unknown_fields_rejected(self):
        catalog = json.loads(
            (ROOT / "knowledge/definitions/index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(catalog["schema"], "le-definition-catalog-v1")
        paths = {entry["path"] for entry in catalog["entries"]}
        self.assertIn("knowledge/definitions/black-hole-v3.json", paths)
        template = json.loads(
            (ROOT / "knowledge/definitions/black-hole-v3.json").read_text(encoding="utf-8")
        )
        bad = copy.deepcopy(template)
        bad["applicability"] = {"unknown_field": True}
        with self.assertRaises(definitions_v3.DefinitionBindingError):
            definitions_v3.validate_template(bad)
        bad_cap = copy.deepcopy(template)
        bad_cap["capabilities"] = ["not-a-real-capability"]
        with self.assertRaises(definitions_v3.DefinitionBindingError):
            definitions_v3.validate_template(bad_cap)


if __name__ == "__main__":
    unittest.main()
