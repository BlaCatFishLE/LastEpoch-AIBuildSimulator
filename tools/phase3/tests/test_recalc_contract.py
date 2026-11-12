# -*- coding: utf-8 -*-
"""Shared local-recompute contract tests (``le-recalc-request/response-v1``, 3-A).

Covers the contract itself (validation, strict rejection, sequence guard, cache
keys, endpoint parameterisation, report_ref) plus an HTTP-level smoke test of the
shared handler with a synthetic in-process service (no browser acceptance).
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from le_calc import recalc

FP = {"calc_input": "a" * 64, "ruleset": "b" * 64, "snapshot": "c" * 64, "dataset": "d" * 64}
CAPABILITIES_VERSION = "le-model-capabilities-v1:test000000000000"


def capabilities_fixture() -> dict:
    return {
        "schema_version": recalc.SCHEMA_MODEL_CAPABILITIES,
        "model_id": "fixture-model",
        "skill_id": "fixture-skill",
        "ui_contract_version": "le-player-report-ui-v1",
        "renderer_fingerprint": "e" * 64,
        "pages": [{"id": "overview", "label": "总览", "kind": "generic"}],
        "target_fields": [
            {"field": "monster_level", "kind": "int", "min": 1, "max": 100, "default": 100},
            {"field": "fire_resistance", "kind": "float", "min": -1.0, "max": 1.0, "default": 0.0},
            {"field": "boss_or_rare", "kind": "bool", "default": True},
        ],
        "buff_controls": [
            {"state_key": "frenzy_active", "kind": "toggle", "default": False},
            {"state_key": "ambition_stacks", "kind": "stacks", "min": 0, "max": 20, "default": 0},
            {"state_key": "dual_wield", "kind": "toggle", "default": True, "readonly": True},
        ],
        "windows": [5.0, 30.0, 120.0],
        "modes": ["cooldown_limited", "attack_speed_limited"],
        "baseline_profiles": ["build_base", "legacy_single_skill"],
        "model_parameters": [{"key": "hits_per_cast", "kind": "int", "min": 1, "max": 8, "default": 4}],
        "scenario_fields": {
            "required": ["baseline_profile", "target", "states"],
            "allowed": [
                "baseline_profile",
                "target",
                "states",
                "mode_policy",
                "model_parameters",
                "hit_override",
                "effects",
            ],
        },
        "legacy_scenario_fields": {
            "hit_override": {"kind": "hit_override"},
            "effects": {"kind": "effect_map"},
        },
        "recalculation": {
            "supported": True,
            "request_kind": recalc.SCHEMA_RECALC_REQUEST,
            "response_kind": recalc.SCHEMA_RECALC_RESPONSE,
            "capabilities_version": CAPABILITIES_VERSION,
            "binding_checks": ["case_id", "model_id", "input_fingerprints", "report_ref"],
            "commands": list(recalc.COMMANDS),
        },
        "informational": [],
    }


def report_ref_fixture(*, endpoint: str | None = None) -> dict:
    return recalc.build_report_ref(
        case_id="fixture-case",
        model_id="fixture-model",
        run_id="fixture-run",
        run_dir="reports/fixture-case/fixture-run",
        report_path="reports/fixture-case/fixture-run/report.html",
        input_fingerprints=FP,
        recompute_endpoint=endpoint or recalc.loopback_endpoint(9911),
        ui_contract_version="le-player-report-ui-v1",
        renderer_fingerprint="e" * 64,
        canonical=True,
    )


def scenario_fixture(**overrides) -> dict:
    scenario = {
        "baseline_profile": "build_base",
        "target": {"monster_level": 100, "fire_resistance": 0.0, "boss_or_rare": True},
        "states": {"frenzy_active": False, "ambition_stacks": 0, "dual_wield": True},
        "mode_policy": {"mode": "cooldown_limited", "window_s": 30.0},
    }
    scenario.update(overrides)
    return scenario


def request_fixture(*, revision: int = 1, command: str = "prepare", **overrides) -> dict:
    request = {
        "schema_version": recalc.SCHEMA_RECALC_REQUEST,
        "request_id": f"req-{revision}",
        "client_edit_revision": revision,
        "case_id": "fixture-case",
        "model_id": "fixture-model",
        "capabilities_version": CAPABILITIES_VERSION,
        "report_ref": report_ref_fixture(),
        "input_fingerprints": dict(FP),
        "scenario": scenario_fixture(),
        "command": command,
    }
    request.update(overrides)
    return request


def validate(request: dict, *, capabilities: dict | None = None, sequence=None, ref=None):
    return recalc.validate_request(
        request,
        capabilities=capabilities or capabilities_fixture(),
        report_ref=ref or report_ref_fixture(),
        expected_input_fingerprints=FP,
        case_id="fixture-case",
        model_id="fixture-model",
        sequence=sequence,
    )


class EndpointTests(unittest.TestCase):
    def test_loopback_endpoint_and_parse(self) -> None:
        endpoint = recalc.loopback_endpoint(9911)
        self.assertEqual(endpoint, "http://127.0.0.1:9911/recompute")
        parsed = recalc.parse_endpoint(endpoint)
        self.assertEqual(parsed["port"], 9911)
        self.assertEqual(recalc.endpoint_port(endpoint), 9911)

    def test_endpoint_from_server_address_uses_actual_port(self) -> None:
        # ``--port 0`` binds an ephemeral port; the endpoint must use the real one.
        self.assertEqual(
            recalc.endpoint_from_server_address(("127.0.0.1", 51234)),
            "http://127.0.0.1:51234/recompute",
        )

    def test_invalid_endpoints_are_rejected(self) -> None:
        for endpoint in (
            "http://127.0.0.1:0/recompute",
            "http://example.com:8765/recompute",
            "https://127.0.0.1:8765/recompute",
            "127.0.0.1:8765/recompute",
            "http://127.0.0.1:8765/other",
            "http://127.0.0.1:99999/recompute",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(recalc.RecalcError):
                    recalc.parse_endpoint(endpoint)

    def test_endpoint_never_defaults_silently(self) -> None:
        with self.assertRaises(recalc.RecalcError):
            recalc.loopback_endpoint(0)


class ReportRefTests(unittest.TestCase):
    def test_build_and_validate(self) -> None:
        ref = report_ref_fixture()
        self.assertEqual(ref["schema_version"], recalc.SCHEMA_REPORT_REF)
        self.assertEqual(recalc.validate_report_ref(ref), ref)
        self.assertEqual(len(recalc.report_ref_digest(ref)), 64)

    def test_non_portable_paths_rejected(self) -> None:
        for field, value in (("run_dir", "C:\\Users\\x\\run"), ("report_path", "/tmp/report.html")):
            ref = report_ref_fixture()
            ref[field] = value
            with self.subTest(field=field):
                with self.assertRaises(recalc.RecalcRejected) as caught:
                    recalc.validate_report_ref(ref)
                self.assertIn("non_portable_path", [issue["code"] for issue in caught.exception.issues])

    def test_endpoint_in_ref_must_be_loopback_recompute(self) -> None:
        ref = report_ref_fixture()
        ref["recompute_endpoint"] = "http://127.0.0.1:8765/other"
        with self.assertRaises(recalc.RecalcRejected):
            recalc.validate_report_ref(ref)


class RequestValidationTests(unittest.TestCase):
    def test_complete_request_is_accepted(self) -> None:
        normalized = validate(request_fixture())
        self.assertEqual(normalized["case_id"], "fixture-case")
        self.assertEqual(normalized["capabilities_version"], CAPABILITIES_VERSION)
        self.assertEqual(normalized["scenario"]["target"]["monster_level"], 100)

    def test_unknown_request_field_rejected(self) -> None:
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request_fixture(unexpected="x"))
        self.assertIn("unknown_request_field", [issue["code"] for issue in caught.exception.issues])

    def test_unknown_scenario_field_rejected_not_ignored(self) -> None:
        request = request_fixture()
        request["scenario"]["unexpected_state_pocket"] = {"x": 1}
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        issues = caught.exception.issues
        self.assertEqual(issues[0]["code"], "unknown_scenario_field")
        self.assertEqual(issues[0]["details"]["field"], "$.scenario.unexpected_state_pocket")

    def test_invalid_stack_count_rejected(self) -> None:
        for value in (999, -1, 2.5, "3"):
            request = request_fixture()
            request["scenario"]["states"]["ambition_stacks"] = value
            with self.subTest(value=value):
                with self.assertRaises(recalc.RecalcRejected) as caught:
                    validate(request)
                self.assertIn(
                    [issue["code"] for issue in caught.exception.issues],
                    (["invalid_stack_count"], ["invalid_scenario_value"]),
                )

    def test_readonly_state_cannot_be_changed(self) -> None:
        request = request_fixture()
        request["scenario"]["states"]["dual_wield"] = False
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        self.assertEqual(caught.exception.issues[0]["code"], "readonly_state")

    def test_unknown_state_rejected(self) -> None:
        request = request_fixture()
        request["scenario"]["states"]["no_such_buff"] = True
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        self.assertEqual(caught.exception.issues[0]["code"], "unknown_scenario_field")

    def test_case_model_capabilities_mismatch(self) -> None:
        cases = {
            "case_mismatch": {"case_id": "other-case"},
            "model_mismatch": {"model_id": "other-model"},
            "capabilities_version_mismatch": {"capabilities_version": "v2"},
        }
        for code, update in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(recalc.RecalcRejected) as caught:
                    validate(request_fixture(**update))
                self.assertIn(code, [issue["code"] for issue in caught.exception.issues])

    def test_stale_input_fingerprint_rejected(self) -> None:
        request = request_fixture()
        request["input_fingerprints"]["ruleset"] = "f" * 64
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        self.assertEqual(caught.exception.issues[0]["code"], "fingerprint_mismatch")

    def test_missing_fingerprint_rejected(self) -> None:
        request = request_fixture()
        del request["input_fingerprints"]["snapshot"]
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        self.assertIn("missing_fingerprint", [issue["code"] for issue in caught.exception.issues])

    def test_invalid_command_and_unsupported_schema(self) -> None:
        with self.assertRaises(recalc.RecalcRejected):
            validate(request_fixture(command="delete"))
        request = request_fixture()
        request["schema_version"] = "le-recalc-request-v0"
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        self.assertEqual(caught.exception.issues[0]["code"], "unsupported_schema_version")

    def test_report_ref_binding_must_match_service(self) -> None:
        ref = report_ref_fixture()
        request = request_fixture()
        request["report_ref"] = dict(ref, run_id="other-run")
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request, ref=ref)
        self.assertEqual(caught.exception.issues[0]["code"], "report_ref_mismatch")

    def test_invalid_model_parameter_range(self) -> None:
        request = request_fixture()
        request["scenario"]["hit_override"] = {"hits_per_cast": 99}
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(request)
        self.assertEqual(caught.exception.issues[0]["code"], "out_of_range")

    def test_legacy_hit_weights_must_match_hits(self) -> None:
        request = request_fixture()
        request["scenario"]["hit_override"] = {
            "hits_per_cast": 4,
            "damage_weights": [1.0, 0.5],
        }
        with self.assertRaises(recalc.RecalcRejected):
            validate(request)

    def test_invalid_client_session_id_rejected(self) -> None:
        for value in ("short", "bad session", "x" * 200, 123):
            with self.subTest(value=value):
                with self.assertRaises(recalc.RecalcRejected) as caught:
                    validate(request_fixture(client_session_id=value))
                self.assertIn(
                    "invalid_client_session_id",
                    [issue["code"] for issue in caught.exception.issues],
                )

    def test_missing_client_session_id_is_legacy_isolated_scope(self) -> None:
        first = validate(request_fixture(revision=1, request_id="legacy-request-1"))
        second = validate(request_fixture(revision=1, request_id="legacy-request-2"))
        self.assertTrue(first["client_session_legacy"])
        self.assertTrue(first["client_session_id"].startswith(recalc.LEGACY_SESSION_PREFIX))
        self.assertNotEqual(first["client_session_id"], second["client_session_id"])

    def test_effect_map_strict_shape(self) -> None:
        request = request_fixture()
        request["scenario"]["effects"] = {"meteor.craterborn": {"enabled": "yes"}}
        with self.assertRaises(recalc.RecalcRejected):
            validate(request)
        request = request_fixture()
        request["scenario"]["effects"] = {"meteor.craterborn": {"unknown_key": 1}}
        with self.assertRaises(recalc.RecalcRejected):
            validate(request)


class BindingsTests(unittest.TestCase):
    def _bindings(self, **overrides):
        values = {
            "case_id": "fixture-case",
            "model_id": "fixture-model",
            "capabilities_version": CAPABILITIES_VERSION,
            "ui_contract_version": "le-player-report-ui-v1",
            "renderer_fingerprint": "e" * 64,
            "report_ref": report_ref_fixture(),
            "input_fingerprints": dict(FP),
            "recompute_endpoint": recalc.loopback_endpoint(9911),
        }
        values.update(overrides)
        return recalc.build_recalc_bindings(**values)

    def test_build_and_validate_bindings(self) -> None:
        bindings = self._bindings()
        self.assertEqual(bindings["schema_version"], recalc.SCHEMA_RECALC_BINDINGS)
        self.assertEqual(set(bindings), set(recalc.RECALC_BINDINGS_FIELDS))
        self.assertEqual(bindings["recompute_endpoint"], "http://127.0.0.1:9911/recompute")
        self.assertEqual(recalc.validate_recalc_bindings(bindings), bindings)
        self.assertEqual(len(recalc.bindings_digest(bindings)), 64)

    def test_bindings_consistency_rejected(self) -> None:
        cases = {
            "case": {"case_id": "other-case"},
            "model": {"model_id": "other-model"},
            "ui": {"ui_contract_version": "other-ui"},
            "renderer": {"renderer_fingerprint": "f" * 64},
            "fingerprints": {"input_fingerprints": {**FP, "ruleset": "f" * 64}},
            "endpoint": {"recompute_endpoint": "http://example.com:9911/recompute"},
        }
        for label, update in cases.items():
            with self.subTest(label=label):
                with self.assertRaises((recalc.RecalcRejected, recalc.RecalcError)):
                    self._bindings(**update)

    def test_validate_rejects_unknown_field(self) -> None:
        bindings = dict(self._bindings(), debug_note="x")
        with self.assertRaises(recalc.RecalcRejected) as caught:
            recalc.validate_recalc_bindings(bindings)
        self.assertEqual(caught.exception.issues[0]["code"], "unknown_binding_field")

    def test_render_with_context_passes_bindings_kwarg(self) -> None:
        captured = {}

        def renderer(payload, *, recalc_endpoint=None, capabilities=None, bindings=None):
            captured.update(bindings=bindings, endpoint=recalc_endpoint)
            return "<html/>"

        bindings = self._bindings()
        html = recalc.render_with_context(
            renderer,
            {},
            recalc_endpoint=recalc.loopback_endpoint(9911),
            capabilities=capabilities_fixture(),
            bindings=bindings,
        )
        self.assertEqual(html, "<html/>")
        self.assertEqual(captured["bindings"], bindings)
        self.assertEqual(captured["endpoint"], "http://127.0.0.1:9911/recompute")

    def test_render_with_context_without_bindings_support(self) -> None:
        def renderer(payload, *, recalc_endpoint=None):
            return "<html/>"

        # Old renderer without ``bindings`` must not receive an unexpected kwarg.
        html = recalc.render_with_context(
            renderer,
            {},
            recalc_endpoint=recalc.loopback_endpoint(9911),
            capabilities=capabilities_fixture(),
            bindings=self._bindings(),
        )
        self.assertEqual(html, "<html/>")

    def test_bindings_accept_ephemeral_bound_port(self) -> None:
        service = FakeService(port=0)
        service.bind_endpoint(recalc.loopback_endpoint(51234))
        bindings = service.bindings
        self.assertEqual(bindings["recompute_endpoint"], "http://127.0.0.1:51234/recompute")
        self.assertEqual(recalc.validate_recalc_bindings(bindings), bindings)


class RevisionTests(unittest.TestCase):
    SESSION = "session-test-0001"

    def _session_request(self, *, revision: int, session: str | None = None):
        request = request_fixture(revision=revision)
        if session is not None:
            request["client_session_id"] = session
        return request

    def test_sequence_guard_is_per_session(self) -> None:
        tracker = recalc.RevisionTracker()
        self.assertEqual(
            validate(self._session_request(revision=1, session=self.SESSION), sequence=tracker)[
                "client_edit_revision"
            ],
            1,
        )
        tracker.accept(1, request_id="req-1", session_id=self.SESSION)
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(self._session_request(revision=1, session=self.SESSION), sequence=tracker)
        self.assertEqual(caught.exception.issues[0]["code"], "stale_edit_revision")
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(self._session_request(revision=0, session=self.SESSION), sequence=tracker)
        self.assertEqual(caught.exception.issues[0]["code"], "stale_edit_revision")
        self.assertEqual(
            validate(self._session_request(revision=2, session=self.SESSION), sequence=tracker)[
                "client_edit_revision"
            ],
            2,
        )
        # a refreshed page / second tab gets a fresh session and is not blocked
        self.assertEqual(
            validate(
                self._session_request(revision=1, session="session-test-refresh"),
                sequence=tracker,
            )["client_edit_revision"],
            1,
        )

    def test_rejected_request_does_not_advance_revision(self) -> None:
        tracker = recalc.RevisionTracker()
        tracker.accept(3, request_id="req-3", session_id=self.SESSION)
        with self.assertRaises(recalc.RecalcRejected):
            validate(self._session_request(revision=2, session=self.SESSION), sequence=tracker)
        self.assertEqual(tracker.accepted_revision_for(self.SESSION), 3)

    def test_cross_tab_counterexample(self) -> None:
        """Review R8: B=2001 must not reject A's own (lower) edits."""
        tracker = recalc.RevisionTracker(run_id="cross-tab-run")
        tab_a = "session-tab-a-0001"
        tab_b = "session-tab-b-0001"
        tracker.accept(1001, request_id="a-1001", session_id=tab_a)
        self.assertEqual(validate(self._session_request(revision=1002, session=tab_a), sequence=tracker)["client_edit_revision"], 1002)
        tracker.accept(1002, request_id="a-1002", session_id=tab_a)
        tracker.accept(2001, request_id="b-2001", session_id=tab_b)
        self.assertEqual(validate(self._session_request(revision=1003, session=tab_a), sequence=tracker)["client_edit_revision"], 1003)
        with self.assertRaises(recalc.RecalcRejected) as caught:
            validate(self._session_request(revision=1002, session=tab_a), sequence=tracker)
        self.assertEqual(caught.exception.issues[0]["code"], "stale_edit_revision")
        with self.assertRaises(recalc.RecalcRejected):
            validate(self._session_request(revision=2000, session=tab_b), sequence=tracker)
        tracker.accept(1003, request_id="a-1003", session_id=tab_a)
        self.assertEqual(tracker.accepted_revision_for(tab_a), 1003)
        self.assertEqual(tracker.accepted_revision_for(tab_b), 2001)
        self.assertEqual(tracker.accepted_revision, 2001)

    def test_legacy_client_without_session_gets_isolated_scope(self) -> None:
        tracker = recalc.RevisionTracker()
        tracker.accept(5000, request_id="legacy-b", session_id=recalc.RevisionTracker.legacy_scope_id("legacy-b"))
        request = request_fixture(revision=1)
        request["request_id"] = "legacy-a"
        normalized = validate(request, sequence=tracker)
        self.assertTrue(normalized["client_session_legacy"])
        self.assertTrue(normalized["client_session_id"].startswith("legacy-"))
        self.assertNotEqual(
            normalized["client_session_id"],
            recalc.RevisionTracker.legacy_scope_id("legacy-b"),
        )


class ResponseTests(unittest.TestCase):
    def _request(self) -> dict:
        return validate(request_fixture())

    def test_accepted_response_roundtrips_and_binds(self) -> None:
        request = self._request()
        response = recalc.build_response(
            request=request,
            accepted=True,
            case_id="fixture-case",
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            report_ref=report_ref_fixture(),
            recompute_endpoint=recalc.loopback_endpoint(9911),
            accepted_revision=1,
            applied_scenario=request["scenario"],
            applied_scenario_digest=recalc.digest_document(request["scenario"]),
            result_digest="1" * 64,
            html="<html>endpoint</html>",
        )
        self.assertEqual(response["recompute_endpoint"], "http://127.0.0.1:9911/recompute")
        self.assertEqual(recalc.validate_response(response, request=request), response)

    def test_rejected_response_requires_issues_and_no_html(self) -> None:
        request = self._request()
        issue = recalc.make_issue("unknown_scenario_field", "bad field", field="$.scenario.x")
        response = recalc.build_response(
            request=request,
            accepted=False,
            case_id="fixture-case",
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            report_ref=report_ref_fixture(),
            recompute_endpoint=recalc.loopback_endpoint(9911),
            accepted_revision=0,
            issues=[issue],
        )
        self.assertIsNone(response["html"])
        self.assertFalse(response["accepted"])
        with self.assertRaises(recalc.RecalcError):  # accepted=false without issues is invalid
            recalc.build_response(
                request=request,
                accepted=False,
                case_id="fixture-case",
                model_id="fixture-model",
                capabilities_version=CAPABILITIES_VERSION,
                input_fingerprints=FP,
                report_ref=report_ref_fixture(),
                recompute_endpoint=recalc.loopback_endpoint(9911),
                accepted_revision=0,
                issues=[],
            )

    def test_client_rejects_mismatched_binding(self) -> None:
        request = self._request()
        response = recalc.build_response(
            request=request,
            accepted=True,
            case_id="fixture-case",
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            report_ref=report_ref_fixture(),
            recompute_endpoint=recalc.loopback_endpoint(9911),
            accepted_revision=1,
            applied_scenario=request["scenario"],
            applied_scenario_digest=recalc.digest_document(request["scenario"]),
            result_digest="1" * 64,
            html="<html/>",
        )
        broken = dict(response, request_id="other-request")
        with self.assertRaises(recalc.RecalcRejected) as caught:
            recalc.validate_response(broken, request=request)
        self.assertEqual(caught.exception.issues[0]["code"], "binding_mismatch")
        broken_digest = dict(response, applied_scenario_digest="0" * 64)
        with self.assertRaises(recalc.RecalcRejected):
            recalc.validate_response(broken_digest, request=request)


    def test_response_session_fields_roundtrip_and_legacy(self) -> None:
        request = validate(request_fixture(client_session_id="session-response-1"))
        response = recalc.build_response(
            request=request,
            accepted=True,
            case_id="fixture-case",
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            report_ref=report_ref_fixture(),
            recompute_endpoint=recalc.loopback_endpoint(9911),
            accepted_revision=1,
            applied_scenario=request["scenario"],
            applied_scenario_digest=recalc.digest_document(request["scenario"]),
            result_digest="1" * 64,
            html="<html/>",
        )
        self.assertEqual(response["client_session_id"], "session-response-1")
        self.assertFalse(response["client_session_legacy"])
        self.assertEqual(
            recalc.validate_response(response, request=request)["client_session_id"],
            "session-response-1",
        )
        wrong = dict(response, client_session_id="other-session")
        with self.assertRaises(recalc.RecalcRejected) as caught:
            recalc.validate_response(wrong, request=request)
        self.assertIn("binding_mismatch", [i["code"] for i in caught.exception.issues])
        legacy = dict(response, client_session_legacy=True)
        with self.assertRaises(recalc.RecalcRejected):
            recalc.validate_response(legacy, request=request)

    def test_legacy_response_marks_isolated_scope(self) -> None:
        request = validate(request_fixture(request_id="legacy-response-1"))
        response = recalc.build_response(
            request=request,
            accepted=True,
            case_id="fixture-case",
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            report_ref=report_ref_fixture(),
            recompute_endpoint=recalc.loopback_endpoint(9911),
            accepted_revision=1,
            applied_scenario=request["scenario"],
            applied_scenario_digest=recalc.digest_document(request["scenario"]),
            result_digest="1" * 64,
            html="<html/>",
        )
        self.assertTrue(response["client_session_legacy"])
        self.assertTrue(response["client_session_id"].startswith(recalc.LEGACY_SESSION_PREFIX))
        self.assertEqual(recalc.validate_response(response, request=request)["request_id"], "legacy-response-1")

    def test_response_session_required_by_client_validation(self) -> None:
        response = {
            "schema_version": recalc.SCHEMA_RECALC_RESPONSE,
            "request_id": "x",
            "client_edit_revision": 1,
            "accepted_revision": 1,
            "accepted": True,
            "issues": [],
            "case_id": "fixture-case",
            "model_id": "fixture-model",
            "capabilities_version": CAPABILITIES_VERSION,
            "input_fingerprints": FP,
            "report_ref": report_ref_fixture(),
            "recompute_endpoint": recalc.loopback_endpoint(9911),
            "applied_scenario": {},
            "applied_scenario_digest": recalc.digest_document({}),
            "result_digest": "1" * 64,
            "html": "<html/>",
        }
        with self.assertRaises(recalc.RecalcRejected) as caught:
            recalc.validate_response(response)
        self.assertIn(
            "$.client_session_id",
            [i.get("details", {}).get("field") for i in caught.exception.issues],
        )


class CacheKeyTests(unittest.TestCase):
    def test_numeric_key_ignores_endpoint_and_report_key_includes_it(self) -> None:
        numeric = recalc.numeric_cache_key(
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            scenario_digest="f" * 64,
        )
        same = recalc.numeric_cache_key(
            model_id="fixture-model",
            capabilities_version=CAPABILITIES_VERSION,
            input_fingerprints=FP,
            scenario_digest="f" * 64,
        )
        self.assertEqual(numeric, same)
        self.assertNotEqual(
            numeric,
            recalc.numeric_cache_key(
                model_id="fixture-model",
                capabilities_version=CAPABILITIES_VERSION,
                input_fingerprints=FP,
                scenario_digest="1" * 64,
            ),
        )
        report_a = recalc.report_cache_key(
            numeric_key=numeric,
            endpoint=recalc.loopback_endpoint(9911),
            ui_contract_version="le-player-report-ui-v1",
            renderer_fingerprint="e" * 64,
        )
        report_b = recalc.report_cache_key(
            numeric_key=numeric,
            endpoint=recalc.loopback_endpoint(9922),
            ui_contract_version="le-player-report-ui-v1",
            renderer_fingerprint="e" * 64,
        )
        self.assertNotEqual(report_a, report_b)  # port only invalidates the report cache
        self.assertNotEqual(
            report_a,
            recalc.report_cache_key(
                numeric_key=numeric,
                endpoint=recalc.loopback_endpoint(9911),
                ui_contract_version="le-player-report-ui-v1",
                renderer_fingerprint="f" * 64,
            ),
        )
        # The rendered document (run id / generated-at) is part of the report key:
        # same numbers + same port but a different result document must not reuse HTML.
        self.assertNotEqual(
            report_a,
            recalc.report_cache_key(
                numeric_key=numeric,
                endpoint=recalc.loopback_endpoint(9911),
                ui_contract_version="le-player-report-ui-v1",
                renderer_fingerprint="e" * 64,
                result_digest="1" * 64,
            ),
        )

    def test_report_file_cache_roundtrip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cache = recalc.ReportCache(tmp)
            key = "a" * 64
            self.assertIsNone(cache.get(key))
            path = cache.put(key, "<html>cached</html>")
            self.assertTrue(path.is_file())
            self.assertEqual(cache.get(key), "<html>cached</html>")
            self.assertIsNone(recalc.ReportCache(tmp, enabled=False).get(key))


class RendererWiringTests(unittest.TestCase):
    def test_renderer_with_keywords(self) -> None:
        captured = {}

        def renderer(payload, *, recalc_endpoint=None, capabilities=None):
            captured.update(endpoint=recalc_endpoint, capabilities=capabilities)
            return "<html/>"

        html = recalc.render_with_context(
            renderer,
            {"x": 1},
            recalc_endpoint="http://127.0.0.1:9911/recompute",
            capabilities={"model_id": "fixture-model"},
        )
        self.assertEqual(html, "<html/>")
        self.assertEqual(captured["endpoint"], "http://127.0.0.1:9911/recompute")
        self.assertEqual(captured["capabilities"], {"model_id": "fixture-model"})

    def test_renderer_without_keywords_falls_back(self) -> None:
        def renderer(payload):
            return f"<html>{payload['x']}</html>"

        html = recalc.render_with_context(
            renderer, {"x": 2}, recalc_endpoint="http://127.0.0.1:9911/recompute"
        )
        self.assertEqual(html, "<html>2</html>")

    def test_renderer_with_var_keywords(self) -> None:
        seen = {}

        def renderer(payload, **kwargs):
            seen.update(kwargs)
            return "<html/>"

        recalc.render_with_context(
            renderer,
            {},
            recalc_endpoint="http://127.0.0.1:9911/recompute",
            capabilities={"model_id": "m"},
        )
        self.assertEqual(seen["capabilities"], {"model_id": "m"})


class FakeService(recalc.RecalcServiceMixin):
    """Minimal service used for HTTP-level contract tests (no damage engine)."""

    def __init__(self, *, port: int) -> None:
        self.port = int(port)
        self._configured_endpoint = None
        self._bound_endpoint = None
        self.tracker = recalc.RevisionTracker()
        self.capabilities = capabilities_fixture()
        self.report_ref = report_ref_fixture()
        self.input_fingerprints = dict(FP)
        self.case_id = "fixture-case"
        self.model_id = "fixture-model"

    @property
    def recompute_endpoint(self) -> str:
        if self._bound_endpoint:
            return self._bound_endpoint
        if int(self.port) <= 0:
            return recalc.loopback_endpoint(recalc.DEFAULT_PORT)
        return recalc.loopback_endpoint(int(self.port))

    def bind_endpoint(self, endpoint: str) -> None:
        self._bound_endpoint = recalc.parse_endpoint(endpoint)["url"]

    @property
    def binding(self) -> recalc.RecalcBinding:
        return recalc.RecalcBinding(
            case_id=self.case_id,
            model_id=self.model_id,
            capabilities=self.capabilities,
            report_ref=self.report_ref,
            input_fingerprints=self.input_fingerprints,
            recompute_endpoint=self.recompute_endpoint,
        )

    def prepare_scenario(self, scenario):
        return dict(scenario)

    def compute_scenario(self, scenario):
        return {"scenario": scenario}, f"<html data-endpoint='{self.recompute_endpoint}'></html>"

    def legacy_scenario(self, payload):
        return dict(payload)

    def initial_html(self):
        return f"<html data-endpoint='{self.recompute_endpoint}'></html>"


class CliModelSelectionTests(unittest.TestCase):
    """D9: ``serve --case`` must explicitly reject unknown damage models."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        import sys

        root = Path(__file__).resolve().parents[3]
        scripts = str(root / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        spec = importlib.util.spec_from_file_location(
            "le_commands_under_test", root / "scripts" / "le_commands.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.module = module

    def test_known_models_pass(self) -> None:
        for model in ("black-hole", "hit", "BLACK-HOLE", "Hit"):
            with self.subTest(model=model):
                self.assertIn(
                    self.module._require_known_model({"case_id": "x", "model": model}, command="serve"),
                    ("black-hole", "hit"),
                )
        self.assertEqual(
            self.module._require_known_model({"case_id": "x"}, command="serve"), "black-hole"
        )

    def test_unknown_model_rejected(self) -> None:
        with self.assertRaises(self.module.PipelineError) as caught:
            self.module._require_known_model(
                {"case_id": "x", "model": "mystery-model"}, command="serve"
            )
        message = str(caught.exception)
        self.assertIn("未知 damage model", message)
        self.assertIn("black-hole", message)
        self.assertIn("hit", message)


class HttpHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = FakeService(port=0)
        cls.httpd = recalc.ThreadingHTTPServer(("127.0.0.1", 0), recalc.make_recalc_handler(cls.service))
        cls.endpoint = recalc.endpoint_from_server_address(cls.httpd.server_address)
        cls.service.bind_endpoint(cls.endpoint)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        parsed = recalc.parse_endpoint(cls.endpoint)
        cls.base = f"http://127.0.0.1:{parsed['port']}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _post(self, payload: dict) -> tuple[int, dict, dict]:
        request = urllib.request.Request(
            f"{self.base}/recompute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body), dict(response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, json.loads(body), dict(exc.headers)

    def _request(self, *, revision=1, command="prepare", service=None):
        service = service or self.service
        return {
            "schema_version": recalc.SCHEMA_RECALC_REQUEST,
            "request_id": f"http-{revision}",
            "client_edit_revision": revision,
            "client_session_id": "http-session-0001",
            "case_id": service.case_id,
            "model_id": service.model_id,
            "capabilities_version": service.capabilities["recalculation"]["capabilities_version"],
            "report_ref": service.report_ref,
            "input_fingerprints": service.input_fingerprints,
            "scenario": scenario_fixture(),
            "command": command,
        }

    def test_health_and_capabilities_expose_actual_endpoint(self) -> None:
        with urllib.request.urlopen(f"{self.base}/health") as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertEqual(health["recompute_endpoint"], self.endpoint)
        self.assertEqual(health["case_id"], "fixture-case")
        self.assertEqual(health["model_id"], "fixture-model")
        self.assertEqual(health["contracts"]["bindings"], recalc.SCHEMA_RECALC_BINDINGS)
        self.assertTrue(health["legacy_scenario_accepted"])
        with urllib.request.urlopen(f"{self.base}/capabilities") as response:
            caps = json.loads(response.read().decode("utf-8"))
        self.assertEqual(caps["recompute_endpoint"], self.endpoint)
        self.assertEqual(caps["capabilities"]["model_id"], "fixture-model")
        self.assertEqual(caps["bindings"]["schema_version"], recalc.SCHEMA_RECALC_BINDINGS)
        self.assertEqual(caps["bindings"]["recompute_endpoint"], self.endpoint)

    def test_prepare_then_compute_and_stale_revision(self) -> None:
        status, response, headers = self._post(self._request(revision=1, command="prepare"))
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["accepted_revision"], 1)
        self.assertEqual(response["recompute_endpoint"], self.endpoint)
        self.assertIsNone(response["html"])
        self.assertEqual(headers["X-Recompute-Endpoint"], self.endpoint)
        self.assertEqual(headers["X-Request-Id"], "http-1")
        self.assertEqual(headers["X-Client-Edit-Revision"], "1")
        self.assertEqual(headers["X-Accepted-Revision"], "1")
        self.assertEqual(headers["X-Recompute-Legacy"], "0")
        self.assertEqual(headers["X-Recompute-Contract"], recalc.SCHEMA_RECALC_REQUEST)

        status, response, _ = self._post(self._request(revision=1, command="compute"))
        self.assertEqual(status, 422)
        self.assertFalse(response["accepted"])
        self.assertEqual([issue["code"] for issue in response["issues"]], ["stale_edit_revision"])

        status, response, headers = self._post(self._request(revision=2, command="compute"))
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["accepted_revision"], 2)
        self.assertEqual(headers["X-Accepted-Revision"], "2")
        self.assertIn(self.endpoint, response["html"])

    def _session_request(self, session: str, revision: int, request_id: str):
        request = self._request(revision=revision)
        request["client_session_id"] = session
        request["request_id"] = request_id
        return request

    def test_cross_tab_sessions_and_session_headers(self) -> None:
        status, response, headers = self._post(
            self._session_request("http-tab-a-0001", 1001, "tab-a-1001")
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["client_session_id"], "http-tab-a-0001")
        self.assertEqual(headers["X-Client-Session-Id"], "http-tab-a-0001")
        self.assertEqual(headers["X-Client-Session-Legacy"], "0")

        status, response, _ = self._post(
            self._session_request("http-tab-b-0001", 2001, "tab-b-2001")
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["accepted_revision"], 2001)

        # Review R8 counterexample: after B=2001, A's own next edit must still be accepted.
        status, response, _ = self._post(
            self._session_request("http-tab-a-0001", 1002, "tab-a-1002")
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["accepted_revision"], 1002)

        status, response, _ = self._post(
            self._session_request("http-tab-a-0001", 1001, "tab-a-1003")
        )
        self.assertEqual(status, 422)
        self.assertEqual(response["issues"][0]["code"], "stale_edit_revision")
        self.assertEqual(response["accepted_revision"], 1002)

    def test_legacy_client_http_response_marks_legacy_session(self) -> None:
        request = self._request(revision=30)
        request.pop("client_session_id", None)
        request["request_id"] = "legacy-http-1"
        status, response, headers = self._post(request)
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertTrue(response["client_session_legacy"])
        self.assertTrue(response["client_session_id"].startswith(recalc.LEGACY_SESSION_PREFIX))
        self.assertEqual(headers["X-Client-Session-Legacy"], "1")

    def test_invalid_scenario_returns_structured_rejection(self) -> None:
        request = self._request(revision=10)
        request["scenario"]["states"]["ambition_stacks"] = 999
        status, response, _ = self._post(request)
        self.assertEqual(status, 422)
        self.assertFalse(response["accepted"])
        self.assertEqual(response["issues"][0]["code"], "invalid_stack_count")
        self.assertIsNone(response["html"])

    def test_legacy_raw_scenario_still_returns_html(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/recompute",
            data=json.dumps({"states": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))
            self.assertIn(self.endpoint, body)
            self.assertEqual(response.headers.get("X-Recompute-Legacy"), "1")
            self.assertEqual(response.headers.get("X-Recompute-Contract"), "raw-scenario")
        with urllib.request.urlopen(f"{self.base}/report") as response:
            self.assertIn(self.endpoint, response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
