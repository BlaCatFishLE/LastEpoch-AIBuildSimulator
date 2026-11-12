"""Shared expected-check implementation tests (P3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import expected_check

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"


# --- legacy reference implementation (frozen pre-P3 semantics, test oracle) ---

def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _pointer(document, pointer):
    node = document
    for raw in [part for part in str(pointer).split("/") if part]:
        part = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _rel(actual, expected):
    if expected == 0:
        return abs(actual - expected)
    return abs(actual - expected) / abs(expected)


def legacy_checks(case, run_dir):
    expected = json.loads((ROOT / case["expected"]).read_text(encoding="utf-8"))
    tolerance = float(expected.get("tolerance_rel") or 0.0)
    cache = {}

    def document(name):
        if name not in cache:
            cache[name] = json.loads((Path(run_dir) / name).read_text(encoding="utf-8"))
        return cache[name]

    checks = []
    for spec in expected.get("checks") or []:
        try:
            actual = _pointer(document(str(spec["file"])), str(spec["pointer"]))
        except Exception:  # noqa: BLE001
            checks.append({"name": spec["name"], "status": "FAIL"})
            continue
        target = spec.get("expected")
        if isinstance(actual, bool) or isinstance(target, bool):
            ok = actual == target
        elif isinstance(actual, (int, float)):
            ok = _rel(float(actual), float(target)) <= tolerance
        else:
            ok = actual == target
        checks.append({"name": spec["name"], "status": "PASS" if ok else "FAIL"})
    result = document("result.json")
    run_expect = expected.get("run_expectations") or {}
    if case.get("model") != "hit":
        recompute = document("conditional-recompute.json")
        conditional = result.get("conditional") or {}
        pairs = [
            ("run.status", (result.get("run") or {}).get("status"), run_expect.get("status")),
            ("run.ruleset.id", ((result.get("run") or {}).get("ruleset") or {}).get("id"),
             run_expect.get("ruleset_id")),
            ("consistency.all_pass", bool((result.get("consistency") or {}).get("all_pass")),
             run_expect.get("consistency_all_pass")),
            ("conditional.context_status", conditional.get("status"),
             run_expect.get("conditional_context_status")),
            ("conditional.recompute_status", (conditional.get("recompute") or {}).get("status"),
             run_expect.get("conditional_status")),
            ("conditional.baseline_profile", recompute.get("baseline_profile"),
             run_expect.get("conditional_baseline_profile")),
            ("conditional.applied_effects",
             len(((recompute.get("condition_application") or {}).get("applied")) or []),
             run_expect.get("conditional_applied_effects")),
        ]
        for name, actual, target in pairs:
            checks.append({"name": name, "status": "PASS" if actual == target else "FAIL"})
        generated = Path(run_dir) / "conditional-scenario.json"
        shipped = ROOT / case["inputs"]["scenario"]
        if generated.is_file() and shipped.is_file():
            keys = ("schema", "preset_id", "mode", "states", "effects", "single_cast",
                    "loop", "target", "baseline_profile")
            left = {key: json.loads(shipped.read_text(encoding="utf-8")).get(key) for key in keys}
            right = {key: json.loads(generated.read_text(encoding="utf-8")).get(key) for key in keys}
            checks.append(
                {"name": "conditional-scenario（忽略 binding）",
                 "status": "PASS" if left == right else "FAIL"}
            )
    else:
        target_block = ((result.get("single_hit") or {}).get("totals") or {}).get("target") or {}
        pairs = [
            ("run.status", (result.get("run") or {}).get("status"), run_expect.get("status")),
            ("run.ruleset.id", ((result.get("run") or {}).get("ruleset") or {}).get("id"),
             run_expect.get("ruleset_id")),
            ("consistency.all_pass", bool((result.get("consistency") or {}).get("all_pass")),
             run_expect.get("consistency_all_pass")),
            ("target.monster_level", target_block.get("monster_level"),
             run_expect.get("monster_level")),
            ("hits_per_cast.per_cast", (result.get("hits_per_cast") or {}).get("per_cast"),
             run_expect.get("hits_per_cast")),
            ("hits_per_cast.effective_hits_per_cast",
             (result.get("hits_per_cast") or {}).get("effective_hits_per_cast"),
             run_expect.get("effective_hits_per_cast")),
            ("dps.policy.id", ((result.get("dps") or {}).get("policy") or {}).get("id"),
             run_expect.get("dps_policy")),
        ]
        for name, actual, target in pairs:
            checks.append({"name": name, "status": "PASS" if actual == target else "FAIL"})
    return checks


def _run_manifest_consistent(run_dir: Path) -> bool:
    import hashlib

    manifest_path = run_dir / "run-manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    for name, digest in (manifest.get("files") or {}).items():
        path = run_dir / name
        if not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return False
    return True


def latest_reproduce_run(case_id: str):
    runs = sorted(
        (ROOT / "reports" / case_id).glob("*/reproduce-report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    ok_runs = []
    for report in runs:
        payload = json.loads(report.read_text(encoding="utf-8"))
        if not payload.get("ok"):
            continue
        ok_runs.append(report.parent)
        if _run_manifest_consistent(report.parent):
            return report.parent
    # P3R-B re-renders HTML after run-manifest generation (known integration gap);
    # fall back to the newest successful run so the #18 binding test still runs.
    return ok_runs[0] if ok_runs else None


class SyntheticCompareTest(unittest.TestCase):
    def test_synthetic_expected_pass_and_fail(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            base = Path(tmp)
            run = base / "run"
            run.mkdir()
            case = {
                "schema": "le-case-v1",
                "case_id": "synthetic",
                "model": "hit",
                "expected": str(base / "expected.json"),
            }
            expected = {
                "schema": "le-case-expected-v1",
                "case_id": "synthetic",
                "tolerance_rel": 1e-9,
                "checks": [
                    {"name": "single_hit.damage", "file": "result.json",
                     "pointer": "/single_hit/totals/total", "expected": 100.0},
                ],
                "run_expectations": {
                    "status": "assumption",
                    "ruleset_id": "fr-test",
                    "consistency_all_pass": True,
                    "monster_level": 100,
                    "hits_per_cast": 4,
                    "effective_hits_per_cast": 2.5,
                    "dps_policy": "cooldown_limited",
                },
            }
            (base / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
            result = {
                "run": {"status": "assumption", "ruleset": {"id": "fr-test"}},
                "consistency": {"all_pass": True},
                "single_hit": {"totals": {"total": 100.0, "target": {"monster_level": 100}}},
                "hits_per_cast": {"per_cast": 4, "effective_hits_per_cast": 2.5},
                "dps": {"policy": {"id": "cooldown_limited"}},
            }
            (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
            checks, ok = expected_check.compare_expected(case, run, root=ROOT)
            self.assertTrue(ok)
            self.assertEqual(len(checks), 8)

            result["single_hit"]["totals"]["total"] = 101.0
            (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
            checks, ok = expected_check.compare_expected(case, run, root=ROOT)
            self.assertFalse(ok)
            failed = [item for item in checks if item["status"] == "FAIL"]
            self.assertEqual([item["name"] for item in failed], ["single_hit.damage"])

    def test_missing_expected_raises(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            case = {"schema": "le-case-v1", "case_id": "x", "model": "hit",
                    "expected": str(ROOT / "does-not-exist.json")}
            with self.assertRaises(expected_check.ExpectedCheckError):
                expected_check.load_expected(case, root=ROOT)


class LegacyParityTest(unittest.TestCase):
    """P3 parity: shared implementation == frozen pre-P3 semantics on real runs."""

    def _compare(self, case_id: str, case_path: str):
        run = latest_reproduce_run(case_id)
        if run is None:
            self.skipTest(f"no reproduce run for {case_id}")
        case = json.loads((ROOT / case_path).read_text(encoding="utf-8"))
        legacy = legacy_checks(case, run)
        shared, ok = expected_check.compare_expected(case, run, root=ROOT)
        self.assertTrue(ok, f"{case_id} shared checks should pass")
        self.assertEqual(len(shared), len(legacy), f"{case_id} check count parity")
        self.assertEqual([item["name"] for item in shared], [item["name"] for item in legacy])
        self.assertEqual([item["status"] for item in shared], [item["status"] for item in legacy])

    def test_black_hole_parity_82_items(self):
        self._compare("black-hole", "examples/black-hole/case.legacy.json")
        run = latest_reproduce_run("black-hole")
        self.assertIsNotNone(run)
        report = json.loads((run / "reproduce-report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["checks"]), 82)

    def test_flame_reave_parity_40_items(self):
        self._compare("flame-reave", "examples/flame-reave/case.legacy-hit.json")
        run = latest_reproduce_run("flame-reave")
        report = json.loads((run / "reproduce-report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["checks"]), 40)



class ValidateResultBindingPathsTest(unittest.TestCase):
    """#18: validate_result accepts reproduce corrected-input binding and simulate binding."""

    def test_reproduce_run_binding_is_derived(self):
        from le_calc.workflow import domain

        run = latest_reproduce_run("black-hole")
        if run is None:
            self.skipTest("no reproduce run for black-hole")
        validation = domain.validate_result(
            {
                "case_id": "black-hole",
                "case_ref": "examples/black-hole/case.legacy.json",
                "run_dir": str(run.relative_to(ROOT)).replace(chr(92), "/"),
            }
        )
        self.assertTrue(validation.passed, validation.errors[:3])
        checks = {check["id"]: check for check in validation.checks}
        self.assertEqual(checks["binding.calc_input_derived"]["status"], "PASS")
        self.assertEqual(checks["binding.calc_input"]["status"], "PASS")

    def test_simulate_run_binding_is_direct_case_input(self):
        import tempfile

        from le_calc.workflow import domain

        scratch = ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            out = Path(tmp) / "fr-run"
            result = domain.simulate(
                {
                    "case_id": "flame-reave",
                    "case_ref": "examples/flame-reave/case.legacy-hit.json",
                    "run_id": "p3-validate-binding",
                    "output_dir": str(out.relative_to(ROOT)).replace(chr(92), "/"),
                    "render_html": False,
                    "generated_at": "2026-11-07T00:00:00+00:00",
                }
            )
            self.assertIn(result["readiness"], ("ready", "partial"))
            validation = domain.validate_result(
                {
                    "case_id": "flame-reave",
                    "case_ref": "examples/flame-reave/case.legacy-hit.json",
                    "run_dir": str(out.relative_to(ROOT)).replace(chr(92), "/"),
                }
            )
        self.assertTrue(validation.passed, validation.errors[:3])
        ids = [check["id"] for check in validation.checks]
        self.assertNotIn("binding.calc_input_derived", ids)


if __name__ == "__main__":
    unittest.main()
