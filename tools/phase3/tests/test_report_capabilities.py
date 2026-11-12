# -*- coding: utf-8 -*-
"""Report capability metadata tests (``report-capabilities.json``, 3-A §4.4/§10.1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc import recalc, report_capabilities

REPO = Path(__file__).resolve().parents[3]


def bh_result_fixture() -> dict:
    return {
        "issues": [{"code": "known-gap", "message": "未建模来源仍可见", "status": "assumption"}],
        "action_policy": {
            "loops": [{"policy_id": "eager"}, {"policy_id": "patient"}],
        },
        "conditional": {
            "view": {
                "states": [
                    {"id": "target.chilled", "kind": "bool", "editable": True, "value": False, "label": "目标处于冰缓"},
                    {"id": "player.ambition_stacks", "kind": "stacks", "editable": True, "value": 0, "bounds": {"min": 0, "max": 20}},
                    {"id": "player.auto_state", "kind": "bool", "editable": False, "value": True, "source": "auto"},
                ],
                "presets": [{"id": "build_base"}, {"id": "legacy_single_skill"}, {"id": "persistent_buffs"}],
            },
            "evaluation": {"effects": []},
        },
    }


def bh_registry_fixture() -> dict:
    return {
        "shared_states": [
            {"id": "player.enchant_weapon_active", "kind": "bool", "default": False, "source": "user"},
            {"id": "player.ambition_stacks", "kind": "stacks", "default": 0, "bounds": {"min": 0, "max": 20}, "source": "user"},
        ],
        "effects": [
            {
                "effect_id": "meteor.craterborn",
                "label": "Craterborn",
                "control": {"kind": "toggle", "state_id": "player.craterborn_active", "default": False},
            },
            {
                "effect_id": "idol.throne_of_ambition",
                "label": "Throne of Ambition",
                "control": {"kind": "stacks", "stack_state_id": "player.ambition_stacks", "bounds": {"min": 0, "max": 20}, "default": 0},
            },
        ],
    }


def fr_result_fixture() -> dict:
    return {
        "issues": [{"code": "r16", "message": "技能等级缩放未建模", "status": "unresolved"}],
        "buffs": {
            "applied": [
                {"state": "frenzy_active", "value": False},
                {"state": "ambition_stacks", "value": 0},
                {"state": "dual_wield", "value": True},
                {"state": "new_state", "value": 2},
            ],
            "screening": {
                "candidates": [
                    {"stacks_state": "blade_weaving_stacks", "max_stacks": 10, "label": "Blade Weaving"},
                ]
            },
        },
        "dps": {
            "policy": {"id": "cooldown_limited"},
            "alternate_policy": {"id": "attack_speed_limited"},
            "scenario_windows_s": [5.0, 30.0, 120.0],
        },
        "scenario": {"profiles": {"build_base": {}, "legacy_single_skill": {}}},
    }


class CapabilityDerivationTests(unittest.TestCase):
    def test_bh_capabilities_from_registry_and_result(self) -> None:
        caps = report_capabilities.capabilities_for_model(
            "black-hole",
            repo_root=REPO,
            result=bh_result_fixture(),
            registry=bh_registry_fixture(),
        )
        self.assertEqual(caps["schema_version"], recalc.SCHEMA_MODEL_CAPABILITIES)
        self.assertEqual(caps["model_id"], "black-hole")
        self.assertEqual(caps["ui_contract_version"], report_capabilities.UI_CONTRACT_VERSION)
        self.assertIn("timeline", [page["id"] for page in caps["pages"]])
        self.assertIn("ignite", [page["id"] for page in caps["pages"]])
        states = {control["state_key"]: control for control in caps["buff_controls"]}
        self.assertIn("player.ambition_stacks", states)
        self.assertEqual(states["player.ambition_stacks"]["max"], 20)
        self.assertIn("player.craterborn_active", states)
        self.assertIn("target.chilled", [field["field"] for field in caps["target_fields"]])
        self.assertEqual(caps["modes"], ["eager", "patient"])
        self.assertIn("legacy_single_skill", caps["baseline_profiles"])
        self.assertIn(
            "未建模来源仍可见", [item["message"] for item in caps["informational"]]
        )
        self.assertTrue(caps["recalculation"]["supported"])
        self.assertEqual(caps["recalculation"]["request_kind"], recalc.SCHEMA_RECALC_REQUEST)
        self.assertTrue(
            caps["recalculation"]["capabilities_version"].startswith(
                report_capabilities.CAPABILITIES_VERSION_PREFIX
            )
        )

    def test_fr_capabilities_from_result(self) -> None:
        caps = report_capabilities.capabilities_for_model(
            "hit", repo_root=REPO, result=fr_result_fixture()
        )
        self.assertEqual(caps["model_id"], "hit")
        self.assertEqual(caps["skill_id"], "fr11mv")
        controls = {control["state_key"]: control for control in caps["buff_controls"]}
        self.assertIn("frenzy_active", controls)
        self.assertIn("new_state", controls)  # observed states are kept visible
        self.assertIn("rhythm_of_fire_stacks", controls)  # profile states must be declared
        self.assertEqual(controls["rhythm_of_fire_stacks"]["max"], 12)
        self.assertEqual(controls["blade_weaving_stacks"]["max"], 10)
        self.assertEqual(caps["windows"], [5.0, 30.0, 120.0])
        self.assertEqual(caps["modes"], ["cooldown_limited", "attack_speed_limited"])
        self.assertEqual(
            [item["key"] for item in caps["model_parameters"]], ["hits_per_cast"]
        )
        self.assertEqual(caps["model_parameters"][0]["max"], 8)
        self.assertIn("r16", [item["id"] for item in caps["informational"]])

    def test_bh_baseline_profiles_merge_registry_presets_with_legacy_main_profile(self) -> None:
        # report-view presets (registry) do not contain legacy_single_skill, but the
        # engine's main口径 does; capabilities must expose it first (kh-bh LIVE-08).
        result = bh_result_fixture()
        result["conditional"]["view"]["presets"] = [
            {"id": "build_base"},
            {"id": "persistent_buffs"},
        ]
        caps = report_capabilities.capabilities_for_model(
            "black-hole"
            ,
            repo_root=REPO,
            result=result,
            registry=bh_registry_fixture(),
        )
        self.assertEqual(caps["baseline_profiles"][0], "legacy_single_skill")
        for expected in ("build_base", "persistent_buffs"):
            self.assertIn(expected, caps["baseline_profiles"])

    def test_unknown_model_rejected(self) -> None:
        with self.assertRaises(ValueError):
            report_capabilities.capabilities_for_model("unknown-model", repo_root=REPO)

    def test_version_changes_with_declaration(self) -> None:
        first = report_capabilities.capabilities_for_model("hit", repo_root=REPO)
        version = first["recalculation"]["capabilities_version"]
        self.assertEqual(version, report_capabilities.capabilities_version(first))
        mutated = json.loads(json.dumps(first))
        mutated["windows"] = [5.0]
        mutated["recalculation"]["capabilities_version"] = report_capabilities.capabilities_version(
            mutated
        )
        self.assertNotEqual(
            version, mutated["recalculation"]["capabilities_version"]
        )


class BindingsArtifactTests(unittest.TestCase):
    FINGERPRINTS = {
        "calc_input": "a" * 64,
        "ruleset": "b" * 64,
        "snapshot": "c" * 64,
        "dataset": "d" * 64,
    }

    def _scenario(self) -> dict:
        return {
            "profiles": {
                "build_base": {"states": {"enemy_high_health": True, "frenzy_active": False}},
                "legacy_single_skill": {"states": {"enemy_high_health": False, "frenzy_active": False}},
            }
        }

    def _capabilities(self) -> dict:
        return report_capabilities.capabilities_for_model(
            "hit", repo_root=REPO, result=fr_result_fixture(), scenario_doc=self._scenario()
        )

    def _report_ref(self, capabilities: dict) -> dict:
        return recalc.build_report_ref(
            case_id="flame-reave",
            model_id="hit",
            run_id="fixture-run",
            run_dir="reports/flame-reave/fixture-run",
            report_path="reports/flame-reave/fixture-run/flame-reave-damage.html",
            input_fingerprints=self.FINGERPRINTS,
            recompute_endpoint=recalc.loopback_endpoint(9922),
            ui_contract_version=report_capabilities.UI_CONTRACT_VERSION,
            renderer_fingerprint=capabilities.get("renderer_fingerprint")
            or report_capabilities.renderer_fingerprint(REPO),
        )

    def test_profile_states_carried_from_scenario(self) -> None:
        capabilities = self._capabilities()
        self.assertEqual(
            capabilities["profile_states"]["legacy_single_skill"]["enemy_high_health"], False
        )
        self.assertEqual(
            capabilities["profile_states"]["build_base"]["enemy_high_health"], True
        )

    def test_bindings_consistent_with_report_ref_and_capabilities(self) -> None:
        capabilities = self._capabilities()
        ref = self._report_ref(capabilities)
        bindings = report_capabilities.recalc_bindings_for_report(
            capabilities=capabilities,
            report_ref=ref,
            input_fingerprints=self.FINGERPRINTS,
            renderer_fingerprint=report_capabilities.renderer_fingerprint(REPO),
            recompute_endpoint=recalc.loopback_endpoint(9922),
        )
        self.assertEqual(bindings["schema_version"], recalc.SCHEMA_RECALC_BINDINGS)
        self.assertEqual(bindings["case_id"], "flame-reave")
        self.assertEqual(bindings["model_id"], "hit")
        self.assertEqual(
            bindings["capabilities_version"],
            capabilities["recalculation"]["capabilities_version"],
        )
        self.assertEqual(bindings["ui_contract_version"], report_capabilities.UI_CONTRACT_VERSION)
        self.assertEqual(bindings["report_ref"], ref)
        self.assertEqual(bindings["input_fingerprints"], self.FINGERPRINTS)
        self.assertEqual(bindings["recompute_endpoint"], "http://127.0.0.1:9922/recompute")
        self.assertEqual(recalc.validate_recalc_bindings(bindings), bindings)

    def test_artifact_embeds_bindings_and_validates(self) -> None:
        capabilities = self._capabilities()
        ref = self._report_ref(capabilities)
        renderer_fp = report_capabilities.renderer_fingerprint(REPO)
        bindings = report_capabilities.recalc_bindings_for_report(
            capabilities=capabilities,
            report_ref=ref,
            input_fingerprints=self.FINGERPRINTS,
            renderer_fingerprint=renderer_fp,
            recompute_endpoint=recalc.loopback_endpoint(9922),
        )
        document = report_capabilities.build_report_capabilities(
            case_id="flame-reave",
            model_id="hit",
            repo_root=REPO,
            run_id="fixture-run",
            report_path="reports/flame-reave/fixture-run/flame-reave-damage.html",
            result=fr_result_fixture(),
            scenario_doc=self._scenario(),
            bindings=bindings,
        )
        self.assertEqual(document["bindings"], bindings)
        self.assertTrue(document["profile_states"])
        self.assertEqual(
            report_capabilities.validate_report_capabilities(document, repo_root=REPO), document
        )
        broken = json.loads(json.dumps(document))
        broken["bindings"]["case_id"] = "black-hole"
        with self.assertRaises(ValueError):
            report_capabilities.validate_report_capabilities(broken, repo_root=REPO)

    def test_render_report_passes_bindings_to_renderer(self) -> None:
        from unittest import mock

        capabilities = self._capabilities()
        ref = self._report_ref(capabilities)
        renderer_fp = report_capabilities.renderer_fingerprint(REPO)
        bindings = report_capabilities.recalc_bindings_for_report(
            capabilities=capabilities,
            report_ref=ref,
            input_fingerprints=self.FINGERPRINTS,
            renderer_fingerprint=renderer_fp,
            recompute_endpoint=recalc.loopback_endpoint(9922),
        )
        captured = {}

        def fake_render_with_context(renderer, payload, **kwargs):
            captured.update(kwargs)
            return "<html/>"

        with mock.patch.object(recalc, "render_with_context", fake_render_with_context):
            html = report_capabilities.render_report(
                fr_result_fixture(),
                model_id="hit",
                recalc_endpoint=recalc.loopback_endpoint(9922),
                capabilities=capabilities,
                bindings=bindings,
            )
        self.assertEqual(html, "<html/>")
        self.assertEqual(captured["bindings"], bindings)
        self.assertEqual(captured["recalc_endpoint"], "http://127.0.0.1:9922/recompute")


class RendererFingerprintTests(unittest.TestCase):
    def test_stable_and_content_sensitive(self) -> None:
        first = report_capabilities.renderer_fingerprint(REPO)
        second = report_capabilities.renderer_fingerprint(REPO)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calc = root / "tools" / "phase3" / "src" / "le_calc"
            (calc / "report_ui" / "assets").mkdir(parents=True)
            (calc / "report.py").write_text("x = 1\n", encoding="utf-8")
            (calc / "hit_report.py").write_text("y = 1\n", encoding="utf-8")
            (calc / "html.py").write_text("z = 1\n", encoding="utf-8")
            (calc / "report_ui" / "eng.py").write_text("a = 1\n", encoding="utf-8")
            (calc / "report_ui" / "assets" / "app.js").write_text("// js\n", encoding="utf-8")
            before = report_capabilities.renderer_fingerprint(root)
            (calc / "report_ui" / "assets" / "app.js").write_text("// js changed\n", encoding="utf-8")
            after = report_capabilities.renderer_fingerprint(root)
            self.assertNotEqual(before, after)
            other = Path(tmp) / "other"
            other.mkdir()
            self.assertNotEqual(before, report_capabilities.renderer_fingerprint(other))

    def test_authoritative_module_covers_all_assets_and_document_delegates(self) -> None:
        from le_calc import report_fingerprint
        from le_calc.report_ui import document

        sources = report_fingerprint.renderer_source_paths(REPO)
        for name in (
            "tools/phase3/src/le_calc/report_ui/assets/app.js",
            "tools/phase3/src/le_calc/report_ui/assets/shell.js",
            "tools/phase3/src/le_calc/report_ui/assets/recalc_contract.js",
            "tools/phase3/src/le_calc/report.py",
            "tools/phase3/src/le_calc/hit_report.py",
        ):
            self.assertIn(name, sources)
        self.assertTrue(any(path.startswith("tools/phase3/src/le_calc/report_ui/") for path in sources))
        self.assertEqual(
            report_capabilities.renderer_fingerprint(REPO),
            report_fingerprint.renderer_fingerprint(REPO),
        )
        # O-C: the report_ui fallback delegates to the same authoritative value.
        self.assertEqual(document.renderer_fingerprint(), report_fingerprint.renderer_fingerprint(REPO))

    def test_sources_are_repository_relative(self) -> None:
        for path in report_capabilities.renderer_sources(REPO):
            self.assertTrue(str(path).startswith(str(REPO)))


class FingerprintSingleSourceTests(unittest.TestCase):
    """D1: one renderer fingerprint implementation, shared by artifact/meta/cache/bindings."""

    FINGERPRINTS = {
        "calc_input": "a" * 64,
        "ruleset": "b" * 64,
        "snapshot": "c" * 64,
        "dataset": "d" * 64,
    }

    def test_capabilities_include_the_shared_fingerprint(self) -> None:
        expected = report_capabilities.renderer_fingerprint(REPO)
        for model in ("black-hole", "hit"):
            with self.subTest(model=model):
                capabilities = report_capabilities.capabilities_for_model(
                    model, repo_root=REPO
                )
                self.assertEqual(capabilities["renderer_fingerprint"], expected)
                self.assertEqual(
                    capabilities["renderer_fingerprint"],
                    report_capabilities.capabilities_for_model(
                        model, repo_root=REPO
                    )["renderer_fingerprint"],
                )

    def test_artifact_capabilities_bindings_and_cache_share_one_fingerprint(self) -> None:
        result = fr_result_fixture()
        capabilities = report_capabilities.capabilities_for_model(
            "hit", repo_root=REPO, result=result
        )
        expected = report_capabilities.renderer_fingerprint(REPO)
        ref = recalc.build_report_ref(
            case_id="flame-reave",
            model_id="hit",
            run_id="fingerprint-run",
            run_dir="reports/flame-reave/fingerprint-run",
            report_path="reports/flame-reave/fingerprint-run/flame-reave-damage.html",
            input_fingerprints=self.FINGERPRINTS,
            recompute_endpoint=recalc.loopback_endpoint(9922),
            ui_contract_version=report_capabilities.UI_CONTRACT_VERSION,
            renderer_fingerprint=expected,
        )
        bindings = report_capabilities.recalc_bindings_for_report(
            capabilities=capabilities,
            report_ref=ref,
            input_fingerprints=self.FINGERPRINTS,
            renderer_fingerprint=expected,
            recompute_endpoint=recalc.loopback_endpoint(9922),
        )
        document = report_capabilities.build_report_capabilities(
            case_id="flame-reave",
            model_id="hit",
            repo_root=REPO,
            run_id="fingerprint-run",
            report_path="reports/flame-reave/fingerprint-run/flame-reave-damage.html",
            result=result,
            bindings=bindings,
        )
        self.assertEqual(document["renderer_fingerprint"], expected)
        self.assertEqual(document["capabilities"]["renderer_fingerprint"], expected)
        self.assertEqual(document["bindings"]["renderer_fingerprint"], expected)
        cache_key = recalc.report_cache_key(
            numeric_key="a" * 64,
            endpoint=recalc.loopback_endpoint(9922),
            ui_contract_version=report_capabilities.UI_CONTRACT_VERSION,
            renderer_fingerprint=document["renderer_fingerprint"],
            capabilities_digest=document["capabilities_digest"],
            result_digest="e" * 64,
            bindings_digest=recalc.bindings_digest(bindings),
        )
        self.assertEqual(cache_key, recalc.report_cache_key(
            numeric_key="a" * 64,
            endpoint=recalc.loopback_endpoint(9922),
            ui_contract_version=report_capabilities.UI_CONTRACT_VERSION,
            renderer_fingerprint=expected,
            capabilities_digest=document["capabilities_digest"],
            result_digest="e" * 64,
            bindings_digest=recalc.bindings_digest(bindings),
        ))


class ArtifactTests(unittest.TestCase):
    def _artifact(self, *, repo_root: Path = REPO) -> dict:
        return report_capabilities.build_report_capabilities(
            case_id="black-hole",
            model_id="black-hole",
            repo_root=repo_root,
            run_id="fixture-run",
            report_path="reports/black-hole/fixture-run/black-hole-damage.html",
            result=bh_result_fixture(),
            registry=bh_registry_fixture(),
        )

    def test_artifact_contract_and_validation(self) -> None:
        document = self._artifact()
        self.assertEqual(document["schema_version"], report_capabilities.SCHEMA_REPORT_CAPABILITIES)
        self.assertEqual(document["ui_contract_version"], report_capabilities.UI_CONTRACT_VERSION)
        self.assertEqual(len(document["renderer_fingerprint"]), 64)
        self.assertEqual(
            report_capabilities.validate_report_capabilities(document, repo_root=REPO), document
        )
        ref_ids = [ref["id"] for ref in document["acceptance_refs"]]
        self.assertIn("HTML-01..12", ref_ids)
        self.assertIn("LIVE-01..08", ref_ids)
        self.assertIn("model-black-hole", ref_ids)
        for ref in document["acceptance_refs"]:
            self.assertFalse(str(ref["path"]).startswith("/"))
            self.assertTrue((REPO / ref["path"]).exists(), ref["path"])

    def test_acceptance_refs_emit_only_existing_paths(self) -> None:
        """A public tree without examples/ reports/ must still build valid refs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs" / "contracts").mkdir(parents=True)
            contract = root / "docs" / "contracts" / "player-report-ui.md"
            contract.write_text("# contract\n", encoding="utf-8")
            workflow = root / "docs" / "contracts" / "workflow-interface.md"
            workflow.write_text("# workflow\n", encoding="utf-8")
            refs = report_capabilities.acceptance_refs(
                "demo-case", repo_root=root, model_id="black-hole"
            )
            self.assertTrue(refs)
            self.assertEqual(
                report_capabilities.validate_acceptance_refs(refs, repo_root=root), []
            )
            model_refs = [ref for ref in refs if ref["kind"] == "model"]
            self.assertEqual(len(model_refs), 1)
            # No case file -> the model ref falls back to the workflow contract.
            self.assertEqual(model_refs[0]["path"], "docs/contracts/workflow-interface.md")
            (root / "examples" / "demo-case").mkdir(parents=True)
            case_path = root / "examples" / "demo-case" / "case.json"
            case_path.write_text("{}", encoding="utf-8")
            refs = report_capabilities.acceptance_refs(
                "demo-case",
                repo_root=root,
                model_id="black-hole",
                case_ref="examples/demo-case/case.json",
            )
            model_refs = [ref for ref in refs if ref["kind"] == "model"]
            self.assertEqual(model_refs[0]["path"], "examples/demo-case/case.json")
            self.assertEqual(
                report_capabilities.validate_acceptance_refs(refs, repo_root=root), []
            )

    def test_artifact_rejects_mutated_metadata(self) -> None:
        document = json.loads(json.dumps(self._artifact()))
        document["capabilities_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            report_capabilities.validate_report_capabilities(document, repo_root=REPO)
        document = json.loads(json.dumps(self._artifact()))
        document["renderer_fingerprint"] = "0" * 64
        with self.assertRaises(ValueError) as caught:
            report_capabilities.validate_report_capabilities(document, repo_root=REPO)
        self.assertIn("renderer_fingerprint", str(caught.exception))
        document = json.loads(json.dumps(self._artifact()))
        document["acceptance_refs"] = [{"id": "missing", "kind": "audit", "path": "docs/not-there.md"}]
        with self.assertRaises(ValueError):
            report_capabilities.validate_report_capabilities(document, repo_root=REPO)

    def test_write_read_roundtrip_and_cache_metadata(self) -> None:
        document = self._artifact()
        document["cache"] = {
            "numeric_key": "a" * 64,
            "report_key": "b" * 64,
            "recompute_endpoint": recalc.loopback_endpoint(9911),
            "ui_contract_version": report_capabilities.UI_CONTRACT_VERSION,
            "renderer_fingerprint": document["renderer_fingerprint"],
        }
        report_capabilities.validate_report_capabilities(document, repo_root=REPO)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report-capabilities.json"
            report_capabilities.write_report_capabilities(path, document)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(str(REPO), text)
            loaded = report_capabilities.read_report_capabilities(path, repo_root=REPO)
            self.assertEqual(loaded, document)
            self.assertEqual(
                report_capabilities.capabilities_path_for_run(Path(tmp)), path
            )

    def test_acceptance_ref_validation_reports_missing_paths(self) -> None:
        problems = report_capabilities.validate_acceptance_refs(
            [{"id": "x", "kind": "audit", "path": "docs/not-there.md"}], repo_root=REPO
        )
        self.assertEqual(len(problems), 1)
        problems = report_capabilities.validate_acceptance_refs(
            [{"id": "x", "kind": "audit", "path": "C:\\tmp\\x.md"}], repo_root=REPO
        )
        self.assertIn("仓库相对路径", problems[0])

    def test_read_missing_artifact_raises(self) -> None:
        with self.assertRaises(ValueError):
            report_capabilities.read_report_capabilities(REPO / "docs" / "nope.json", repo_root=REPO)


if __name__ == "__main__":
    unittest.main()
