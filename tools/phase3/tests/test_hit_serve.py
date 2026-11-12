# -*- coding: utf-8 -*-
"""Integration test: FR player report + local recompute service (3-A contract)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from le_calc import hit_ruleset, hit_serve, model, recalc

REPO = Path(__file__).resolve().parents[3]
CASE = REPO / "examples/flame-reave/case.legacy-hit.json"
SCENARIO = REPO / "examples/flame-reave/scenario/conditional-scenario.json"
SNAPSHOT = REPO / "examples/flame-reave/snapshot/input-snapshot.hit.json"
RULESET = REPO / "tools/phase3/rules/ruleset-fr-hit-stage1-v2.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _result(html: str) -> dict:
    match = re.search(r'<script id="result-data" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        raise AssertionError("embedded result JSON missing")
    return json.loads(match.group(1))


def _bindings(html: str) -> dict:
    match = re.search(
        r'<script id="le-recalc-bindings" type="application/json">(.*?)</script>', html, re.S
    )
    if not match:
        raise AssertionError("embedded recalc bindings missing")
    return json.loads(match.group(1))


def _baseline_rows(result: dict) -> dict:
    return {
        profile: block.get("single_hit_damage")
        for profile, block in ((result.get("baselines") or {}).get("profiles") or {}).items()
    }


class HitServeRecomputeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = json.loads(CASE.read_text(encoding="utf-8"))
        cls.scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
        cls.snapshot = model.Snapshot.from_dict(json.loads(SNAPSHOT.read_text(encoding="utf-8")))
        cls.ruleset = hit_ruleset.load_ruleset(str(RULESET))

    def _service(self, port: int = 9922) -> hit_serve.HitReportService:
        return hit_serve.HitReportService(
            case=self.case,
            snapshot=self.snapshot,
            ruleset=self.ruleset,
            base_scenario=self.scenario,
            ruleset_path=str(RULESET),
            scenario_path=str(SCENARIO),
            port=port,
            repo_root=REPO,
        )

    # -- engine path ----------------------------------------------------
    def test_offline_page_is_player_facing_and_single_recompute_channel(self) -> None:
        service = self._service()
        html = service.build_html({})
        self.assertIn("Buff 设置", html)
        self.assertIn("预计伤害", html)
        self.assertNotIn("status = assumption", html)
        self.assertNotIn("Rogue", html)
        self.assertGreaterEqual(html.count("fetch("), 1)
        # 3-A: the page embeds the actual endpoint, never the hardcoded default.
        self.assertIn("127.0.0.1:9922/recompute", html)
        self.assertNotIn("127.0.0.1:8765/recompute", html)

    def test_recompute_uses_same_engine_and_changes_values(self) -> None:
        service = self._service()
        base = _result(service.build_html({}))
        frenzy = _result(service.build_html({"states": {"frenzy_active": True}}))
        self.assertGreater(
            frenzy["single_hit"]["totals"]["damage"],
            base["single_hit"]["totals"]["damage"],
        )
        self.assertEqual(frenzy["hits_per_cast"]["per_cast"], 4)

    def test_bind_endpoint_follows_bound_socket(self) -> None:
        service = self._service(port=0)
        service.bind_endpoint(recalc.loopback_endpoint(51235))
        html = service.build_html({})
        self.assertIn("127.0.0.1:51235/recompute", html)

    def test_page_embeds_recalc_bindings_consistent_with_service(self) -> None:
        service = self._service()
        html = service.build_html({})
        bindings = _bindings(html)
        self.assertEqual(bindings, service.bindings)
        self.assertEqual(bindings["schema_version"], recalc.SCHEMA_RECALC_BINDINGS)
        self.assertEqual(bindings["case_id"], "flame-reave")
        self.assertEqual(bindings["model_id"], "hit")
        self.assertEqual(bindings["recompute_endpoint"], "http://127.0.0.1:9922/recompute")
        # D1 single source: page capabilities/bindings share the service fingerprint.
        page_capabilities_match = re.search(
            r'<script id="report-capabilities" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(page_capabilities_match)
        page_capabilities = json.loads(page_capabilities_match.group(1))
        self.assertEqual(
            page_capabilities["renderer_fingerprint"], service.renderer_fingerprint
        )
        hit_policy = page_capabilities.get("hit_policy") or {}
        self.assertEqual(hit_policy.get("kind"), "first_full_then_fraction")
        self.assertEqual(hit_policy.get("first_weight"), 1.0)
        self.assertEqual(hit_policy.get("subsequent_weight"), 0.5)
        self.assertEqual(hit_policy.get("hits", {}).get("default"), 4)
        self.assertEqual(bindings["renderer_fingerprint"], service.renderer_fingerprint)
        meta = re.search(
            r'<meta name="le-renderer-fingerprint" content="([^"]+)">', html
        )
        self.assertIsNotNone(meta)
        self.assertEqual(meta.group(1), service.renderer_fingerprint)
        # exactly one payload block (JS doc comments also mention the id)
        self.assertEqual(html.count('<script id="le-recalc-bindings" type="application/json">{"'), 1)

    def test_capabilities_include_profile_states_for_preset_switching(self) -> None:
        service = self._service()
        states = service.capabilities["profile_states"]
        self.assertEqual(states["legacy_single_skill"]["enemy_high_health"], False)
        self.assertEqual(states["build_base"]["enemy_high_health"], True)

    def test_legacy_and_new_paths_share_baseline_semantics(self) -> None:
        """The two baseline rows must be the profile reference values, not both current states."""
        service = self._service()
        scenario = {
            "baseline_profile": "legacy_single_skill",
            "states": {
                "enemy_high_health": False,
                "enchant_weapon_active": True,
                "frenzy_active": False,
                "ignited": False,
                "target_in_melee_range": True,
            },
            "target": {"boss_or_rare": True, "monster_level": 100, "fire_resistance": 0.0},
            "hit_override": {"hits_per_cast": 4, "damage_weights": [1.0, 0.5, 0.5, 0.5]},
        }
        legacy_result = _result(service.handle_legacy_request({"scenario": scenario}))
        legacy_rows = _baseline_rows(legacy_result)
        self.assertAlmostEqual(legacy_rows["legacy_single_skill"], 34786.28114736705, delta=1e-6)
        self.assertAlmostEqual(legacy_rows["build_base"], 51831.5589095769, delta=1e-6)
        self.assertAlmostEqual(
            legacy_result["single_hit"]["totals"]["damage"], 34786.28114736705, delta=1e-6
        )

        status, response = service.handle_contract_request(
            self._request(service, revision=1, command="compute", scenario=scenario)
        )
        self.assertEqual(status, 200)
        new_result = _result(response["html"])
        self.assertEqual(_baseline_rows(new_result), legacy_rows)
        self.assertAlmostEqual(
            new_result["single_hit"]["totals"]["damage"],
            legacy_result["single_hit"]["totals"]["damage"],
            delta=1e-9,
        )
        # both baseline rows are distinct after the profile-defaults fix
        self.assertNotEqual(legacy_rows["legacy_single_skill"], legacy_rows["build_base"])

    def test_legacy_path_marks_response_as_legacy(self) -> None:
        """The legacy HTML path keeps its old-shaped response but the service marks it."""
        service = self._service()
        html = service.handle_legacy_request({"scenario": {"states": {"frenzy_active": True}}})
        self.assertIn("预计伤害", html)
        self.assertIn("127.0.0.1:9922/recompute", html)

    # -- shared recalc contract -----------------------------------------
    def _request(self, service, *, revision: int = 1, command: str = "prepare", **overrides) -> dict:
        binding = service.binding
        scenario = {
            "baseline_profile": "build_base",
            "states": {"frenzy_active": True, "ambition_stacks": 3},
            "target": {"boss_or_rare": True, "monster_level": 100, "fire_resistance": 0.0},
            "hit_override": {"hits_per_cast": 4, "damage_weights": [1.0, 0.5, 0.5, 0.5]},
        }
        request = {
            "schema_version": recalc.SCHEMA_RECALC_REQUEST,
            "request_id": f"fr-req-{revision}",
            "client_edit_revision": revision,
            "client_session_id": "test-session-fr-0001",
            "case_id": binding.case_id,
            "model_id": binding.model_id,
            "capabilities_version": binding.capabilities_version,
            "report_ref": binding.report_ref,
            "input_fingerprints": dict(binding.input_fingerprints),
            "scenario": scenario,
            "command": command,
        }
        request.update(overrides)
        return request

    def test_contract_prepare_and_compute(self) -> None:
        service = self._service()
        status, response = service.handle_contract_request(self._request(service, revision=1, command="prepare"))
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["recompute_endpoint"], "http://127.0.0.1:9922/recompute")
        self.assertIsNone(response["html"])

        status, response = service.handle_contract_request(self._request(service, revision=2, command="compute"))
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["accepted_revision"], 2)
        self.assertIn("127.0.0.1:9922/recompute", response["html"])
        result = _result(response["html"])
        self.assertGreater(result["single_hit"]["totals"]["damage"], 0.0)

    def test_contract_rejects_stale_revision_and_illegal_edits(self) -> None:
        service = self._service()
        status, _ = service.handle_contract_request(self._request(service, revision=5, command="prepare"))
        self.assertEqual(status, 200)
        status, response = service.handle_contract_request(self._request(service, revision=5, command="prepare"))
        self.assertEqual(status, 422)
        self.assertEqual(response["issues"][0]["code"], "stale_edit_revision")

        request = self._request(service, revision=6, command="prepare")
        request["scenario"]["states"]["ambition_stacks"] = 999
        status, response = service.handle_contract_request(request)
        self.assertEqual(status, 422)
        self.assertEqual(response["issues"][0]["code"], "invalid_stack_count")

    def test_contract_rejects_wrong_case_and_model(self) -> None:
        service = self._service()
        for field, value in (("case_id", "black-hole"), ("model_id", "black-hole")):
            with self.subTest(field=field):
                request = self._request(service, revision=7, command="prepare", **{field: value})
                status, response = service.handle_contract_request(request)
                self.assertEqual(status, 422)
                self.assertIn(
                    response["issues"][0]["code"], ("case_mismatch", "model_mismatch")
                )

    def test_legacy_scenario_payload_still_works(self) -> None:
        service = self._service()
        html = service.handle_legacy_request({"scenario": {"states": {"frenzy_active": True}}})
        self.assertIn("127.0.0.1:9922/recompute", html)


if __name__ == "__main__":
    unittest.main()
