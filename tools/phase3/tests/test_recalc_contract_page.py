"""task-24: page bindings + shared recompute client + contract integration tests."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from le_calc import hit_report, recalc
from le_calc.report_ui import document, shell

import test_html
import test_hit_report





def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def embedded_bindings(html: str) -> dict | None:
    """Return the real bindings payload; ignore JS-comment text with the same shape."""

    for match in re.finditer(
        r'<script id="le-recalc-bindings" type="application/json">(.*?)</script>',
        html,
        re.S,
    ):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == "le-recalc-bindings-v1":
            return payload
    return None

def synthetic_bindings(model_id: str = "black-hole") -> dict:
    fingerprints = {
        "calc_input": "a" * 64,
        "ruleset": "b" * 64,
        "snapshot": "c" * 64,
        "dataset": "d" * 64,
    }
    endpoint = "http://127.0.0.1:9001/recompute"
    report_ref = recalc.build_report_ref(
        case_id=model_id,
        model_id=model_id,
        run_id="fixture-run",
        run_dir=f"reports/{model_id}/fixture-run",
        report_path=f"reports/{model_id}/fixture-run/report.html",
        input_fingerprints=fingerprints,
        recompute_endpoint=endpoint,
        ui_contract_version=document.UI_CONTRACT_VERSION,
        renderer_fingerprint="e" * 64,
    )
    return recalc.build_recalc_bindings(
        case_id=model_id,
        model_id=model_id,
        capabilities_version="le-model-capabilities-v1:fixture",
        ui_contract_version=document.UI_CONTRACT_VERSION,
        renderer_fingerprint="e" * 64,
        report_ref=report_ref,
        input_fingerprints=fingerprints,
        recompute_endpoint=endpoint,
    )


class SharedRecalcClientPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bh = test_html.load_result()
        cls.fr = test_hit_report.build_result()

    def test_shared_client_is_embedded_for_both_models(self):
        for html in (
            shell.render(self.bh, bindings=synthetic_bindings("black-hole")),
            hit_report.render_hit_html(self.fr, bindings=synthetic_bindings("hit")),
        ):
            for token in (
                "le-recalc-request-v1",
                "le-recalc-response-v1",
                "request_id",
                "client_edit_revision",
                "accepted_revision",
                "report_ref",
                "input_fingerprints",
            ):
                self.assertIn(token, html)
            self.assertEqual(html.count("fetch("), 1)

    def test_bindings_block_present_only_when_injected(self):
        bh_html = shell.render(self.bh, bindings=synthetic_bindings("black-hole"))
        payload = embedded_bindings(bh_html)
        self.assertIsNotNone(payload)
        recalc.validate_recalc_bindings(payload)
        self.assertEqual(payload["schema_version"], "le-recalc-bindings-v1")
        self.assertEqual(payload["input_fingerprints"]["calc_input"], "a" * 64)
        self.assertIsNone(embedded_bindings(shell.render(self.bh)))
        # accept the P3R-B alias kwarg too
        alias = hit_report.render_hit_html(
            self.fr, recalc_bindings=synthetic_bindings("hit")
        )
        self.assertIsNotNone(embedded_bindings(alias))

    def test_old_raw_scenario_payload_is_not_used_by_model_scripts(self):
        bh_html = shell.render(self.bh, bindings=synthetic_bindings("black-hole"))
        fr_html = hit_report.render_hit_html(self.fr, bindings=synthetic_bindings("hit"))
        # exactly one fetch (the shared client); model scripts no longer post raw
        self.assertEqual(bh_html.count("JSON.stringify(payload)"), 0)
        self.assertEqual(fr_html.count("JSON.stringify(payload)"), 0)
        self.assertIn('JSON.stringify(request)', bh_html)
        self.assertIn('JSON.stringify(request)', fr_html)
        # request construction includes every live contract field
        for html in (bh_html, fr_html):
            for token in (
                "schema_version: REQUEST_SCHEMA",
                "request_id: randomId()",
                "client_edit_revision: revision",
                "report_ref: bindings.report_ref",
                "input_fingerprints:",
                "command: command",
            ):
                self.assertIn(token, html)

    def test_fr_preset_feeds_baseline_profile_into_request(self):
        html = hit_report.render_hit_html(self.fr, bindings=synthetic_bindings("hit"))
        scripts = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        self.assertIn("scenario.baseline_profile = preset ? preset.value : initialBaseline", scripts)
        self.assertIn("controller.markEdit()", scripts)
        self.assertIn("data-cond-preset", html)


class RecalcContractIntegrationTest(unittest.TestCase):
    def _capabilities(self) -> dict:
        return {
            "schema_version": "le-model-capabilities-v1",
            "model_id": "black-hole",
            "ui_contract_version": document.UI_CONTRACT_VERSION,
            "recalculation": {"capabilities_version": "le-model-capabilities-v1:fixture"},
            "scenario_fields": {
                "required": ["baseline_profile", "target", "states"],
                "allowed": ["baseline_profile", "target", "states"],
            },
            "baseline_profiles": ["build_base", "legacy_single_skill"],
            "buff_controls": [],
            "target_fields": [],
        }

    def _fixture(self):
        capabilities = self._capabilities()
        bindings = synthetic_bindings("black-hole")
        request = {
            "schema_version": "le-recalc-request-v1",
            "request_id": "req-1",
            "client_session_id": "test-session-0001",
            "client_edit_revision": 3,
            "case_id": bindings["case_id"],
            "model_id": bindings["model_id"],
            "capabilities_version": bindings["capabilities_version"],
            "report_ref": bindings["report_ref"],
            "input_fingerprints": bindings["input_fingerprints"],
            "scenario": {
                "baseline_profile": "legacy_single_skill",
                "target": {},
                "states": {},
            },
            "command": "compute",
        }
        return capabilities, bindings, request

    def test_new_contract_request_is_accepted(self):
        capabilities, bindings, request = self._fixture()
        tracker = recalc.RevisionTracker()
        normalized = recalc.validate_request(
            request,
            capabilities=capabilities,
            report_ref=bindings["report_ref"],
            expected_input_fingerprints=bindings["input_fingerprints"],
            case_id=bindings["case_id"],
            model_id=bindings["model_id"],
            sequence=tracker,
        )
        self.assertEqual(normalized["schema_version"], "le-recalc-request-v1")
        self.assertEqual(normalized["scenario"]["baseline_profile"], "legacy_single_skill")
        tracker.accept(
            normalized["client_edit_revision"],
            request_id=normalized["request_id"],
            session_id=normalized["client_session_id"],
        )

    def test_stale_revision_is_rejected(self):
        capabilities, bindings, request = self._fixture()
        tracker = recalc.RevisionTracker()
        tracker.accept(5, request_id="older", session_id=request["client_session_id"])
        stale = dict(request, request_id="req-2", client_edit_revision=5)
        with self.assertRaises(recalc.RecalcRejected) as context:
            recalc.validate_request(
                stale,
                capabilities=capabilities,
                report_ref=bindings["report_ref"],
                expected_input_fingerprints=bindings["input_fingerprints"],
                case_id=bindings["case_id"],
                model_id=bindings["model_id"],
                sequence=tracker,
            )
        codes = [issue["code"] for issue in context.exception.issues]
        self.assertIn("stale_edit_revision", codes)

    def test_response_binding_and_digest_validation(self):
        capabilities, bindings, request = self._fixture()
        normalized = recalc.validate_request(
            request,
            capabilities=capabilities,
            report_ref=bindings["report_ref"],
            expected_input_fingerprints=bindings["input_fingerprints"],
            case_id=bindings["case_id"],
            model_id=bindings["model_id"],
        )
        scenario = normalized["scenario"]
        response = recalc.build_response(
            request=normalized,
            accepted=True,
            case_id=bindings["case_id"],
            model_id=bindings["model_id"],
            capabilities_version=bindings["capabilities_version"],
            input_fingerprints=bindings["input_fingerprints"],
            report_ref=bindings["report_ref"],
            recompute_endpoint=bindings["recompute_endpoint"],
            accepted_revision=3,
            applied_scenario=scenario,
            applied_scenario_digest=recalc.digest_document(scenario),
            result_digest="f" * 64,
            html="<html>ok</html>",
        )
        validated = recalc.validate_response(response, request=normalized)
        self.assertTrue(validated["accepted"])
        self.assertEqual(validated["request_id"], "req-1")
        # a response for another request is rejected (old response must be dropped)
        other = dict(normalized, request_id="req-other")
        with self.assertRaises(recalc.RecalcRejected):
            recalc.validate_response(response, request=other)


if __name__ == "__main__":
    unittest.main()
