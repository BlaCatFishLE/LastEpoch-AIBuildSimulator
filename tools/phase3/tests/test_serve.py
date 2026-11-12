"""Tests for the loopback BH recompute service + shared recalc contract (3-A)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from le_calc import model, recalc
from le_calc.conditional import build_context
from le_calc.conditional.registry import DEFAULT_REGISTRY_PATH
from le_calc.ruleset import load_ruleset
from le_calc.serve import ScenarioReportService

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "examples" / "black-hole" / "snapshot" / "input-snapshot.affix-db.json"
CALC_INPUT = ROOT / "examples" / "black-hole" / "inputs" / "calculation-input.affix-db.json"
BUILD = ROOT / "examples" / "black-hole" / "inputs" / "bd.json"
RULESET = ROOT / "tools" / "phase3" / "rules" / "ruleset-bh-stage1-v4.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _embedded_result(html: str) -> dict:
    marker = 'id="result-data"'
    start = html.index(">", html.index(marker)) + 1
    end = html.index("</script>", start)
    return json.loads(html[start:end])


def _embedded_bindings(html: str) -> dict:
    match = re.search(
        r'<script id="le-recalc-bindings" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise AssertionError("embedded recalc bindings missing")
    return json.loads(match.group(1))


class ScenarioReportServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = model.Snapshot.from_dict(
            json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        )
        cls.ruleset = load_ruleset(str(RULESET))
        cls.base_context = build_context(
            cls.snapshot,
            scenario=None,
            ruleset=cls.ruleset,
            ruleset_path=str(RULESET),
            calc_input_path=str(CALC_INPUT),
            build_path=str(BUILD),
            repo_root=str(ROOT),
        )
        cls.service = ScenarioReportService(
            snapshot=cls.snapshot,
            ruleset=cls.ruleset,
            ruleset_path=str(RULESET),
            calc_input_path=str(CALC_INPUT),
            build_path=str(BUILD),
            registry_path=str(DEFAULT_REGISTRY_PATH),
            monster_level=100,
            run_id="test-scenario",
            port=9911,
            repo_root=ROOT,
        )

    # -- engine path (existing behaviour) -------------------------------
    def test_build_html_applies_selected_scenario_to_dps_pages(self) -> None:
        scenario = json.loads(json.dumps(self.base_context["scenario"]))
        scenario["states"]["player.craterborn_active"] = True
        scenario["effects"]["meteor.craterborn"] = {"enabled": True}
        html = self.service.build_html(scenario)
        self.assertIn("条件已应用", html)
        result = _embedded_result(html)
        self.assertTrue(result["conditional"]["scenario_applied"])
        self.assertEqual(
            (result["conditional"]["evaluation"]["summary"] or {}).get("configured"), 1
        )
        totals = result["single_cast"]["totals"]
        # scenario adds Craterborn (pen + cast speed) on top of the auto passive
        self.assertGreater(totals["single_cast_total"], 10_662_227.0)
        recompute = result["conditional"]["recompute"]
        applied = (recompute.get("condition_application") or {}).get("applied") or []
        self.assertIn("meteor.craterborn", {item.get("effect_id") for item in applied})

    def test_build_html_rejects_invalid_scenario(self) -> None:
        with self.assertRaises(ValueError):
            self.service.build_html({"schema": "bh-conditional-scenario-v1", "states": {"unknown": True}})

    def test_build_html_tolerates_legacy_enabled_for_stacks(self) -> None:
        scenario = json.loads(json.dumps(self.base_context["scenario"]))
        scenario["effects"]["idol.throne_of_ambition"] = {"enabled": True}
        html = self.service.build_html(scenario)
        self.assertIn("条件已应用", html)

    # -- 3-A endpoint parameterisation ----------------------------------
    def test_html_embeds_actual_endpoint_not_hardcoded_default(self) -> None:
        html = self.service.build_html(json.loads(json.dumps(self.base_context["scenario"])))
        self.assertIn("127.0.0.1:9911/recompute", html)
        self.assertNotIn("127.0.0.1:8765/recompute", html)

    def test_html_embeds_recalc_bindings_consistent_with_binding(self) -> None:
        html = self.service.build_html(json.loads(json.dumps(self.base_context["scenario"])))
        bindings = _embedded_bindings(html)
        self.assertEqual(bindings, self.service.bindings)
        self.assertEqual(bindings["schema_version"], recalc.SCHEMA_RECALC_BINDINGS)
        self.assertEqual(bindings["case_id"], "black-hole")
        self.assertEqual(bindings["model_id"], "black-hole")
        self.assertEqual(bindings["recompute_endpoint"], "http://127.0.0.1:9911/recompute")
        self.assertEqual(bindings["report_ref"], self.service.binding.report_ref)
        self.assertEqual(
            bindings["capabilities_version"],
            self.service.capabilities["recalculation"]["capabilities_version"],
        )
        self.assertEqual(
            bindings["input_fingerprints"], self.service.binding.input_fingerprints
        )
        # D1 single source: page capabilities/bindings share the artifact fingerprint.
        page_capabilities_match = re.search(
            r'<script id="report-capabilities" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(page_capabilities_match)
        page_capabilities = json.loads(page_capabilities_match.group(1))
        self.assertEqual(
            page_capabilities["renderer_fingerprint"], self.service.renderer_fingerprint
        )
        self.assertEqual(
            page_capabilities.get("hit_policy"), {"kind": "not_applicable"}
        )
        self.assertEqual(bindings["renderer_fingerprint"], self.service.renderer_fingerprint)
        # D1: page meta and embedded capabilities share the same single-source fingerprint.
        meta = re.search(
            r'<meta name="le-renderer-fingerprint" content="([^"]+)">', html
        )
        self.assertIsNotNone(meta)
        self.assertEqual(meta.group(1), self.service.renderer_fingerprint)
        # exactly one binding block (never duplicate ids)
        # exactly one payload block (JS doc comments also mention the id)
        self.assertEqual(html.count('<script id="le-recalc-bindings" type="application/json">{"'), 1)

    def test_bind_endpoint_uses_bound_socket(self) -> None:
        service = ScenarioReportService(
            snapshot=self.snapshot,
            ruleset=self.ruleset,
            ruleset_path=str(RULESET),
            calc_input_path=str(CALC_INPUT),
            build_path=str(BUILD),
            registry_path=str(DEFAULT_REGISTRY_PATH),
            monster_level=100,
            run_id="test-scenario",
            port=0,
            repo_root=ROOT,
        )
        service.bind_endpoint(recalc.loopback_endpoint(51234))
        self.assertEqual(service.recompute_endpoint, "http://127.0.0.1:51234/recompute")
        html = service.build_html(json.loads(json.dumps(self.base_context["scenario"])))
        self.assertIn("127.0.0.1:51234/recompute", html)

    # -- shared recalc contract (function level) ------------------------
    def _fresh_service(self, port: int = 9911) -> ScenarioReportService:
        return ScenarioReportService(
            snapshot=self.snapshot,
            ruleset=self.ruleset,
            ruleset_path=str(RULESET),
            calc_input_path=str(CALC_INPUT),
            build_path=str(BUILD),
            registry_path=str(DEFAULT_REGISTRY_PATH),
            monster_level=100,
            run_id="test-scenario",
            port=port,
            repo_root=ROOT,
        )

    def _request(
        self, *, revision: int = 1, command: str = "prepare", service=None, **overrides
    ) -> dict:
        binding = (service or self.service).binding
        request = {
            "schema_version": recalc.SCHEMA_RECALC_REQUEST,
            "request_id": f"req-{revision}",
            "client_edit_revision": revision,
            "client_session_id": "test-session-bh-0001",
            "case_id": binding.case_id,
            "model_id": binding.model_id,
            "capabilities_version": binding.capabilities_version,
            "report_ref": binding.report_ref,
            "input_fingerprints": dict(binding.input_fingerprints),
            "scenario": json.loads(json.dumps(self.base_context["scenario"])),
            "command": command,
        }
        request.update(overrides)
        return request

    def test_contract_prepare_accepts_and_rejects_stale_revision(self) -> None:
        service = self._fresh_service()
        status, response = service.handle_contract_request(
            self._request(revision=1, command="prepare", service=service)
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["accepted_revision"], 1)
        self.assertEqual(response["recompute_endpoint"], "http://127.0.0.1:9911/recompute")
        self.assertEqual(response["client_session_id"], "test-session-bh-0001")
        self.assertFalse(response["client_session_legacy"])
        self.assertIsNotNone(response["applied_scenario"])
        self.assertEqual(
            response["applied_scenario_digest"],
            recalc.digest_document(response["applied_scenario"]),
        )
        self.assertIsNone(response["html"])

        status, response = service.handle_contract_request(
            self._request(revision=1, command="prepare", service=service)
        )
        self.assertEqual(status, 422)
        self.assertFalse(response["accepted"])
        self.assertEqual(response["issues"][0]["code"], "stale_edit_revision")
        self.assertEqual(response["accepted_revision"], 1)

    def test_contract_compute_returns_html_with_actual_endpoint(self) -> None:
        service = self._fresh_service()
        request = self._request(revision=10, command="compute", service=service)
        status, response = service.handle_contract_request(request)
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["accepted_revision"], 10)
        self.assertIn("127.0.0.1:9911/recompute", response["html"])
        # the re-rendered document keeps the page bindings (LIVE-04 after document.write)
        bindings = _embedded_bindings(response["html"])
        self.assertEqual(bindings, service.bindings)
        self.assertEqual(bindings["recompute_endpoint"], "http://127.0.0.1:9911/recompute")
        self.assertIsNotNone(response["result_digest"])
        self.assertEqual(
            response["applied_scenario_digest"],
            recalc.digest_document(response["applied_scenario"]),
        )

    def test_contract_unknown_state_rejected_structurally(self) -> None:
        request = self._request(revision=20, command="prepare")
        request["scenario"]["states"]["bogus.state"] = True
        status, response = self.service.handle_contract_request(request)
        self.assertEqual(status, 422)
        self.assertEqual(response["issues"][0]["code"], "unknown_scenario_field")
        self.assertIsNone(response["html"])

    def test_contract_wrong_case_and_model_rejected(self) -> None:
        for field, value in (("case_id", "flame-reave"), ("model_id", "hit")):
            with self.subTest(field=field):
                request = self._request(revision=30, command="prepare", **{field: value})
                status, response = self.service.handle_contract_request(request)
                self.assertEqual(status, 422)
                self.assertIn(
                    response["issues"][0]["code"], ("case_mismatch", "model_mismatch")
                )

    def test_contract_stale_fingerprint_rejected(self) -> None:
        request = self._request(revision=40, command="prepare")
        request["input_fingerprints"]["ruleset"] = "f" * 64
        status, response = self.service.handle_contract_request(request)
        self.assertEqual(status, 422)
        self.assertEqual(response["issues"][0]["code"], "fingerprint_mismatch")

    def test_legacy_raw_scenario_path_still_returns_html(self) -> None:
        html = self.service.handle_legacy_request(json.loads(json.dumps(self.base_context["scenario"])))
        self.assertIn("条件已应用", html)
        self.assertIn("127.0.0.1:9911/recompute", html)


if __name__ == "__main__":
    unittest.main()
