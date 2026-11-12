"""Domain adapter tests: resolve_build / simulate / validate_result (P2)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import contract, domain

ROOT = Path(__file__).resolve().parents[3]
CASES = {
    "black-hole": "examples/black-hole/case.legacy.json",
    "flame-reave": "examples/flame-reave/case.legacy-hit.json",
}
ABILITIES = {"black-hole": "bh2", "flame-reave": "fr11mv"}

KNOWLEDGE_SRC = ROOT / "tools" / "knowledge" / "src"
if str(KNOWLEDGE_SRC) not in sys.path:
    sys.path.insert(0, str(KNOWLEDGE_SRC))

try:  # explicit adapter import; when unavailable the tests skip gracefully
    import le_knowledge  # type: ignore

    KNOWLEDGE_ERROR = None
except Exception as exc:  # noqa: BLE001
    le_knowledge = None  # type: ignore
    KNOWLEDGE_ERROR = str(exc)



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


class KnowledgeFixture:
    _dir = None
    _index = None

    @classmethod
    def index_path(cls) -> str:
        if cls._index is None:
            cls._dir = tempfile.TemporaryDirectory()
            cls._index = str(Path(cls._dir.name) / "knowledge.sqlite")
        return cls._index

    @classmethod
    def lookup(cls, query: dict) -> dict:
        return le_knowledge.search(  # type: ignore[union-attr]
            query, knowledge_dir=str(ROOT / "knowledge"), index_path=cls.index_path(), root=str(ROOT)
        )


@unittest.skipIf(le_knowledge is None, f"le_knowledge unavailable: {KNOWLEDGE_ERROR}")
class ResolveBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        for case_id, case_ref in CASES.items():
            request = {
                "schema_version": contract.SCHEMA_RESOLVE_BUILD_REQUEST,
                "case_id": case_id,
                "case_ref": case_ref,
                "main_skill": {"ability_id": ABILITIES[case_id]},
                "knowledge": {
                    "knowledge_dir": "knowledge",
                    "index_path": KnowledgeFixture.index_path(),
                    "lookup": KnowledgeFixture.lookup,
                },
            }
            cls.results[case_id] = domain.resolve_build(request)

    def test_plan_has_fixed_nine_item_checklist(self):
        for case_id, result in self.results.items():
            plan = result["analysis_plan"]
            self.assertEqual(plan["schema_version"], contract.SCHEMA_ANALYSIS_PLAN)
            self.assertEqual(len(plan["checklist"]), 9, case_id)
            for item in plan["checklist"]:
                self.assertIn(item["status"],
                              {"supported", "partial", "not_applicable", "needs_research", "needs_input"})
            self.assertTrue(plan["plan_id"].startswith("analysis_plan-"))

    def test_readiness_and_bound_mechanisms(self):
        for case_id, result in self.results.items():
            self.assertIn(result["readiness"], contract.READINESS)
            self.assertGreater(len(result["bound_mechanisms"]), 0, case_id)
            for mechanism in result["bound_mechanisms"]:
                self.assertTrue(mechanism["mechanism_id"])
                self.assertIsInstance(mechanism["components"], list)
            self.assertEqual(result["schema_version"], contract.SCHEMA_RESOLVE_BUILD_RESULT)

    def test_coverage_buckets_and_scope(self):
        for case_id, result in self.results.items():
            coverage = result["coverage"]
            self.assertEqual(coverage["schema_version"], contract.SCHEMA_COVERAGE)
            for key in ("included", "excluded_with_reason", "unresolved", "out_of_scope"):
                self.assertIsInstance(coverage[key], list)
                self.assertIn(key, coverage["counts"])
            self.assertEqual(coverage["counts"]["included"], len(coverage["included"]))
            scope = coverage["scope"]
            self.assertEqual(scope["case_id"], case_id)
            self.assertEqual(scope["main_skill"]["ability_id"], ABILITIES[case_id])
            self.assertGreaterEqual(len(scope["skill_trees"]), 1)
            # selected scope must list repository-relative inputs only
            for ref in scope["selected_inputs"]:
                if ref.get("path"):
                    self.assertFalse(Path(ref["path"]).is_absolute())

    def test_gaps_are_deterministic_and_shaped(self):
        request = {
            "schema_version": contract.SCHEMA_RESOLVE_BUILD_REQUEST,
            "case_id": "black-hole",
            "case_ref": CASES["black-hole"],
            "main_skill": {"ability_id": ABILITIES["black-hole"]},
            "knowledge": {
                "knowledge_dir": "knowledge",
                "index_path": KnowledgeFixture.index_path(),
                "lookup": KnowledgeFixture.lookup,
            },
        }
        first = domain.resolve_build(request)
        second = domain.resolve_build(request)
        self.assertEqual(first["gaps"], second["gaps"])
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])
        for gap in first["gaps"]:
            self.assertTrue(gap["gap_id"].startswith("gap-"))
            self.assertIn(gap["deliverable_kind"],
                          {"mapping", "parameter_rule", "handler", "bounded_assumption"})
            self.assertGreaterEqual(len(gap["verification_required"]), 1)

    def test_result_has_no_machine_absolute_paths(self):
        text = json.dumps(self.results, ensure_ascii=False)
        self.assertNotIn(str(ROOT), text)
        self.assertNotIn("C:\\", text)
        self.assertNotIn("C:/", text)

    def test_knowledge_unavailable_is_structured_issue(self):
        def broken_lookup(query):
            raise ImportError("knowledge stack not installed")

        request = {
            "schema_version": contract.SCHEMA_RESOLVE_BUILD_REQUEST,
            "case_id": "black-hole",
            "case_ref": CASES["black-hole"],
            "main_skill": {"ability_id": ABILITIES["black-hole"]},
            "knowledge": {"lookup": broken_lookup},
        }
        result = domain.resolve_build(request)
        self.assertEqual(result["readiness"], contract.NEEDS_INPUT)
        codes = [issue["code"] for issue in result["issues"]]
        self.assertIn("knowledge_unavailable", codes)
        # must still return a structurally valid analysis plan
        self.assertEqual(len(result["analysis_plan"]["checklist"]), 9)

    def test_request_validation(self):
        base = {
            "schema_version": contract.SCHEMA_RESOLVE_BUILD_REQUEST,
            "case_id": "black-hole",
            "case_ref": CASES["black-hole"],
            "main_skill": {"ability_id": "bh2"},
        }
        with self.assertRaises(contract.ContractError):
            domain.resolve_build({**base, "case_id": "flame-reave"})
        with self.assertRaises(contract.ContractError):
            domain.resolve_build({**base, "schema_version": "le-resolve-build-request-v2"})
        bad = dict(base)
        bad.pop("main_skill")
        with self.assertRaises(contract.ContractError):
            domain.resolve_build(bad)


class SimulateAndValidateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix="p2-workflow-", dir=scratch)
        cls.base = Path(cls.tmp.name)
        cls.runs = {}
        cls.results = {}
        for case_id, case_ref in CASES.items():
            out = cls.base / f"{case_id}-run"
            request = {
                "schema_version": contract.SCHEMA_SIMULATE_REQUEST,
                "case_id": case_id,
                "case_ref": case_ref,
                "run_id": f"p2-test-{case_id}",
                "output_dir": _rel(out),
                "render_html": False,
                "generated_at": "2026-11-07T00:00:00+00:00",
            }
            cls.results[case_id] = domain.simulate(request)
            cls.runs[case_id] = out

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_simulate_result_contract_and_artifacts(self):
        for case_id, result in self.results.items():
            self.assertEqual(result["schema_version"], contract.SCHEMA_SIMULATE_RESULT)
            self.assertEqual(result["readiness"], contract.READY, case_id)
            self.assertTrue(result["consistency"]["all_pass"])
            self.assertEqual(result["issues"], [])
            self.assertGreater(len(result["output_refs"]), 0)
            self.assertIn("result.json", result["artifacts"])
            self.assertTrue((ROOT / result["artifacts"]["result.json"]).is_file())
            self.assertTrue(result["input_fingerprint"])
            self.assertIn(result["model_id"], (domain.MODEL_BLACK_HOLE, domain.MODEL_HIT))

    def test_simulate_matches_frozen_numbers_via_validate_result(self):
        for case_id, case_ref in CASES.items():
            validation = domain.validate_result(
                {"case_id": case_id, "case_ref": case_ref, "run_dir": _rel(self.runs[case_id])}
            )
            self.assertTrue(validation.passed, f"{case_id}: {validation.errors[:3]}")
            self.assertGreater(len(validation.checks), 10)
            self.assertEqual(validation.kind, "result")
            self.assertEqual(validation.to_dict()["schema_version"], contract.SCHEMA_VALIDATION_RESULT)

    def test_simulate_refuses_non_empty_output_dir(self):
        target = self.base / "occupied"
        target.mkdir(exist_ok=True)
        (target / "existing.json").write_text("{}", encoding="utf-8")
        result = domain.simulate(
            {
                "schema_version": contract.SCHEMA_SIMULATE_REQUEST,
                "case_id": "black-hole",
                "case_ref": CASES["black-hole"],
                "run_id": "nope",
                "output_dir": _rel(target),
                "render_html": False,
            }
        )
        self.assertEqual(result["readiness"], contract.NEEDS_INPUT)
        self.assertIn("output_dir_not_empty", [issue["code"] for issue in result["issues"]])

    def _copy_run(self, case_id: str, name: str) -> Path:
        target = self.base / name
        shutil.copytree(self.runs[case_id], target)
        return target

    def test_validate_tampered_binding_fails(self):
        run = self._copy_run("black-hole", "tampered-binding")
        result_path = run / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["input"]["calc_input"]["sha256"] = "0" * 64
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        validation = domain.validate_result(
            {"case_id": "black-hole", "case_ref": CASES["black-hole"], "run_dir": _rel(run)}
        )
        self.assertFalse(validation.passed)
        ids = [error["id"] for error in validation.errors]
        self.assertIn("binding.calc_input", ids)

    def test_validate_tampered_numeric_fails(self):
        run = self._copy_run("black-hole", "tampered-number")
        result_path = run / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["single_cast"]["totals"]["body_total"] += 1.0
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        validation = domain.validate_result(
            {"case_id": "black-hole", "case_ref": CASES["black-hole"], "run_dir": _rel(run)}
        )
        self.assertFalse(validation.passed)
        ids = [error["id"] for error in validation.errors]
        self.assertIn("expected.check.legacy_single_skill.body_total", ids)

    def test_validate_hit_tampered_target_fails(self):
        run = self._copy_run("flame-reave", "tampered-hit")
        result_path = run / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["hits_per_cast"]["per_cast"] = 99
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        validation = domain.validate_result(
            {"case_id": "flame-reave", "case_ref": CASES["flame-reave"], "run_dir": _rel(run)}
        )
        self.assertFalse(validation.passed)
        self.assertIn("expected.run.hits_per_cast.per_cast",
                      [error["id"] for error in validation.errors])

    def test_validate_missing_result_is_not_an_exception(self):
        run = self.base / "missing-run"
        run.mkdir(exist_ok=True)
        validation = domain.validate_result(
            {"case_id": "black-hole", "case_ref": CASES["black-hole"], "run_dir": _rel(run)}
        )
        self.assertFalse(validation.passed)
        self.assertEqual(validation.errors[0]["id"], "structure.result_json")

    def test_validate_check_selector_subset(self):
        validation = domain.validate_result(
            {
                "case_id": "black-hole",
                "case_ref": CASES["black-hole"],
                "run_dir": _rel(self.runs["black-hole"]),
                "checks": ["binding"],
            }
        )
        self.assertTrue(all(check["id"].startswith("binding.") for check in validation.checks))


if __name__ == "__main__":
    unittest.main()
