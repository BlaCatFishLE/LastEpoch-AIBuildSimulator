"""第四步 S2/S3：VC definitions_v3、真实输入绑定、场景分离与 workflow-6 差异映射。"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools/phase3/src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from le_calc import calculation_plan as planmod
from le_calc import calculation_route, definitions_v3, plan_executor
from le_calc.workflow import unified

CASE = ROOT / "examples/void-cleave-20261110/case.definitions-v3.json"
TEMPLATE = ROOT / "knowledge/definitions/void-cleave-fire-main-v3.json"
SCENARIO = ROOT / "examples/void-cleave-20261110/inputs/scenario-v3.json"
STEP3_RUN = ROOT / "reports/void-cleave-20261110/dps-step3-workflow-6"
STEP4_RUN = ROOT / "reports/void-cleave-20261110/dps-step4-vc-8"
SCRATCH = ROOT / ".scratch/unified-dps-step4/tests"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _case() -> dict:
    case = _load(CASE)
    case["_path"] = CASE.relative_to(ROOT).as_posix()
    return case


def _compile(case: dict, *, overrides=None):
    route = calculation_route.resolve_calculation_route(case, root=ROOT)
    resolved, compiled = calculation_route.compile_route(
        root=ROOT, case=case, input_overrides=overrides
    )
    return route, resolved, compiled


class V3TemplateTest(unittest.TestCase):
    def test_template_is_definition_not_profile_and_has_stable_selectors(self):
        template = _load(TEMPLATE)
        self.assertEqual(template["schema"], "le-skill-template-v3")
        self.assertEqual(template["binding_version"], "definitions_v3.selector-v1")
        definition = template["definition"]
        self.assertEqual(set(template), {"schema", "template_id", "ability_id", "binding_version", "title", "definition", "scenario_schema", "migration"})
        self.assertNotIn("monster_level", definition)
        self.assertNotIn("resistance", definition)
        self.assertIn("scenario_schema", template)
        self.assertIsInstance(definition["evidence"], list)
        self.assertNotIn("default", definition["controls"][0])
        text = json.dumps(template, ensure_ascii=False)
        for token in ("item_instance_id", '"mod_id"', '"roll"', '"dps_profile"', '"profile_version"'):
            self.assertNotIn(token, text)
        # selector 使用稳定身份/序号，不复制当前词条的精确数值文本
        coverage_selectors = json.dumps(
            [
                row.get("binding_selector")
                for key in ("confirmed_inactive", "not_applicable_mods", "assumed_active_mods")
                for row in definition["coverage"].get(key) or []
            ],
            ensure_ascii=False,
        )
        self.assertNotIn("12% increased Effect of Frenzy", coverage_selectors)
        self.assertIn("binding_selector", json.dumps(definition["coverage"], ensure_ascii=False))
        for effect in definition["effects"]:
            selector = effect.get("source", {}).get("binding_selector")
            if selector:
                self.assertIn("item_definition_entity_id", selector)
                self.assertNotIn("expected_readable_text_contains", selector)

    def test_schema_type_unknown_and_instance_field_are_rejected(self):
        case = _case()
        for name, mutate in (
            ("bad-evidence", lambda t: t["definition"].__setitem__("evidence", "not-array")),
            ("unknown-field", lambda t: t.__setitem__("unexpected", True)),
            ("instance-field", lambda t: t["definition"]["state_sources"]["frenzy"]["binding_selector"].__setitem__("item_instance_id", "equipment.waist")),
            ("source-instance-field", lambda t: t["definition"]["effects"][0]["source"].__setitem__("item_instance_id", "equipment.waist")),
            ("unknown-rule", lambda t: t["definition"]["rules"].__setitem__("unknown_damage_handler", {"value": 999})),
            ("bad-conversion-type", lambda t: t["definition"].__setitem__("attribute_conversion", 42)),
            ("conversion-no-expected", lambda t: t["definition"]["attribute_conversion"].pop("expected")),
            ("conversion-empty-expected", lambda t: t["definition"]["attribute_conversion"].__setitem__("expected", {})),
            ("conversion-null-expected", lambda t: t["definition"]["attribute_conversion"].__setitem__("expected", None)),
            ("conversion-instance-only", lambda t: t["definition"]["attribute_conversion"].__setitem__("expected", {"item_instance_id": "equipment.amulet"})),
        ):
            template = _load(TEMPLATE)
            mutate(template)
            path = SCRATCH / f"{name}-{unittest.util._get_test_runner if False else ''}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path = SCRATCH / f"{name}.json"
            path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            bad_case = copy.deepcopy(case)
            bad_case["calculation"]["source"]["template"] = path.relative_to(ROOT).as_posix()
            route = calculation_route.resolve_calculation_route(bad_case, root=ROOT)
            self.assertEqual(route["status"], "ready")
            _, _, compiled = _compile(bad_case)
            self.assertEqual(compiled.execution_status, "rejected", name)
            self.assertIsNone(compiled.plan, name)


class V3RealBindingTest(unittest.TestCase):
    def test_native_binding_matches_workflow6_except_plan_identity(self):
        route = calculation_route.resolve_calculation_route(_case(), root=ROOT)
        self.assertEqual(route["route_id"], "unified:definitions_v3")
        _, _, compiled = _compile(_case())
        self.assertEqual(compiled.execution_status, "completed")
        plan = compiled.plan
        result = plan_executor.execute_plan(plan, unified.default_scenario(plan))
        self.assertEqual(result["sustained_dps"], 21491.54090674077)
        self.assertEqual(result["single_hit"], 44605.452247191075)
        self.assertEqual(result["dot_dps"], 350.4151020824999)
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["coverage"]["gap_count"], 7)
        old = _load(STEP3_RUN / "result.json")
        for key in ("coverage", "contributions", "applications", "effect_ledger", "issues"):
            self.assertEqual(result[key], old[key], key)
        self.assertIsNone(result.get("profile_sha256"))
        roles = {ref["role"] for ref in plan["input_refs"]}
        self.assertTrue({"template", "scenario", "calculation_input", "build_spec", "dataset_content"} <= roles)
        self.assertNotIn("profile", roles)

    def test_production_binding_never_reads_legacy_profile_file(self):
        from unittest import mock

        original = Path.read_text

        def guarded(path, *args, **kwargs):
            if path.name == "dps-profile-v2.json":
                raise AssertionError("definitions_v3 生产绑定不得读取旧 profile")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded):
            _, _, compiled = _compile(_case())
        self.assertEqual(compiled.execution_status, "completed")
        self.assertIsNotNone(compiled.plan)

    def test_input_override_is_used_and_invalid_handoff_rejected(self):
        work = SCRATCH / "override"
        work.mkdir(parents=True, exist_ok=True)
        variant = _load(ROOT / _case()["inputs"]["calculation_input"])
        for spec in (variant.get("skills") or {}).get("specializations") or []:
            if not str((spec.get("tree") or {}).get("entity_id") or "").endswith(":v01cv"):
                continue
            for node in spec.get("nodes") or []:
                if str(node.get("node_id")) == "6":
                    node["points"] = 0
        calc_path = work / "calculation-input.json"
        calc_path.write_text(json.dumps(variant, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sha = hashlib.sha256(calc_path.read_bytes()).hexdigest()
        _, _, compiled = _compile(
            _case(),
            overrides={"calculation_input": calc_path.relative_to(ROOT).as_posix(), "calculation_input_sha256": sha},
        )
        self.assertEqual(compiled.execution_status, "completed")
        result = plan_executor.execute_plan(compiled.plan, unified.default_scenario(compiled.plan))
        self.assertEqual(result["sustained_dps"], 15351.100647671983)
        refs = {ref["role"]: ref for ref in compiled.plan["input_refs"]}
        self.assertEqual(refs["calculation_input"]["path"], calc_path.relative_to(ROOT).as_posix())
        self.assertEqual(refs["calculation_input"]["sha256"], sha)
        # 不存在/错 SHA/缺 SHA 都必须拒绝
        for overrides in (
            {"calculation_input": ".scratch/unified-dps-step4/missing.json", "calculation_input_sha256": sha},
            {"calculation_input": calc_path.relative_to(ROOT).as_posix(), "calculation_input_sha256": "0" * 64},
            {"calculation_input": calc_path.relative_to(ROOT).as_posix()},
        ):
            _, _, rejected = _compile(_case(), overrides=overrides)
            self.assertEqual(rejected.execution_status, "rejected", overrides)
            self.assertIsNone(rejected.plan)

    def test_instance_variation_rebinds_points_and_removes_contribution(self):
        work = SCRATCH / "variant"
        work.mkdir(parents=True, exist_ok=True)
        case = _case()
        variant = _load(ROOT / case["inputs"]["calculation_input"])
        for spec in (variant.get("skills") or {}).get("specializations") or []:
            if not str((spec.get("tree") or {}).get("entity_id") or "").endswith(":v01cv"):
                continue
            for node in spec.get("nodes") or []:
                if str(node.get("node_id")) == "6":
                    node["points"] = 0
        new_calc = work / "calculation-input.json"
        new_calc.write_text(json.dumps(variant, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case["inputs"]["calculation_input"] = new_calc.relative_to(ROOT).as_posix()
        case["input_sha256"]["calculation_input"] = hashlib.sha256(new_calc.read_bytes()).hexdigest()
        variant_case = work / "case.json"
        variant_case.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case["_path"] = variant_case.relative_to(ROOT).as_posix()
        _, _, compiled = _compile(case)
        self.assertEqual(compiled.execution_status, "completed")
        result = plan_executor.execute_plan(compiled.plan, unified.default_scenario(compiled.plan))
        base = _load(STEP4_RUN / "result.json")
        self.assertNotEqual(result["sustained_dps"], base["sustained_dps"])
        self.assertNotIn("skill:v01cv:6:Abyssal Walker", {row["contribution_id"] for row in result["contributions"]})

    def test_optional_missing_and_required_missing_semantics(self):
        work = SCRATCH / "optional"
        work.mkdir(parents=True, exist_ok=True)
        base_case = _case()
        scenario = _load(SCENARIO)
        template = _load(TEMPLATE)
        for required in (False, True):
            variant = copy.deepcopy(template)
            effect = next(item for item in variant["definition"]["effects"] if item["id"] == "sword_catcher:frenzy")
            effect["required"] = required
            effect["source"]["binding_selector"]["item_definition_entity_id"] = "item:letools:uniqueList.uniques:999999"
            effect["source"]["binding_selector"].pop("item_base_entity_id", None)
            path = work / f"template-required-{required}.json"
            path.write_text(json.dumps(variant, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            case = copy.deepcopy(base_case)
            case["calculation"]["source"]["template"] = path.relative_to(ROOT).as_posix()
            _, _, compiled = _compile(case)
            if required:
                self.assertEqual(compiled.execution_status, "rejected")
                self.assertIsNone(compiled.plan)
            else:
                self.assertEqual(compiled.execution_status, "completed")
                result = plan_executor.execute_plan(compiled.plan, unified.default_scenario(compiled.plan))
                self.assertNotIn(
                    "equipment.weapon1#unique_mod#5:Sword Catcher · Frenzy",
                    {row["contribution_id"] for row in result["contributions"]},
                )

    def test_same_ownership_different_definition_is_blocked(self):
        work = SCRATCH / "ownership"
        work.mkdir(parents=True, exist_ok=True)
        template = _load(TEMPLATE)
        duplicate = copy.deepcopy(template["definition"]["effects"][0])
        duplicate["value"] = 0.9
        template["definition"]["effects"].append(duplicate)
        path = work / "template-ownership.json"
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case = _case()
        case["calculation"]["source"]["template"] = path.relative_to(ROOT).as_posix()
        _, _, compiled = _compile(case)
        self.assertEqual(compiled.execution_status, "rejected")
        self.assertIsNone(compiled.plan)
        self.assertTrue(any(item.get("code") == "ownership_conflict" for item in compiled.diagnostics))

    def test_movement_conversion_optional_missing_and_required_missing(self):
        work = SCRATCH / "movement"
        work.mkdir(parents=True, exist_ok=True)
        base_case = _case()
        template = _load(TEMPLATE)
        for required in (False, True):
            variant = copy.deepcopy(template)
            rule = variant["definition"]["rules"]["movement_conversion"]
            rule["required"] = required
            rule["source"]["binding_selector"]["item_definition_entity_id"] = "item:letools:uniqueList.uniques:999999"
            rule["source"]["binding_selector"].pop("item_base_entity_id", None)
            path = work / f"template-movement-{required}.json"
            path.write_text(json.dumps(variant, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            case = copy.deepcopy(base_case)
            case["calculation"]["source"]["template"] = path.relative_to(ROOT).as_posix()
            _, _, compiled = _compile(case)
            if required:
                self.assertEqual(compiled.execution_status, "rejected")
                self.assertIsNone(compiled.plan)
            else:
                self.assertEqual(compiled.execution_status, "completed")
                result = plan_executor.execute_plan(compiled.plan, unified.default_scenario(compiled.plan))
                self.assertFalse(
                    any(row.get("source_kind") == "movement_conversion" and row.get("included")
                        for row in result["contributions"])
                )

    def test_attribute_conversion_rule_file_is_plan_and_cache_dependency(self):
        work = SCRATCH / "rule-dependency"
        work.mkdir(parents=True, exist_ok=True)
        scenario = _load(SCENARIO)
        template = _load(TEMPLATE)
        rules_source = ROOT / "tools/phase3/rules/attribute-conversions-v1.json"
        rules_copy = work / "attribute-conversions-v1.json"
        rules_copy.write_bytes(rules_source.read_bytes())
        template["definition"]["attribute_conversion"]["rules"] = rules_copy.relative_to(ROOT).as_posix()
        template_path = work / "template.json"
        template_path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case = _case()
        case["calculation"]["source"]["template"] = template_path.relative_to(ROOT).as_posix()
        case_path = work / "case.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case["_path"] = case_path.relative_to(ROOT).as_posix()
        _, _, compiled = _compile(case)
        self.assertEqual(compiled.execution_status, "completed")
        refs = {ref["role"]: ref for ref in compiled.plan["input_refs"]}
        self.assertIn("attribute_conversion_rules", refs)
        self.assertEqual(refs["attribute_conversion_rules"]["sha256"], hashlib.sha256(rules_copy.read_bytes()).hexdigest())
        first_plan = compiled.plan["plan_sha256"]
        first_sha = refs["attribute_conversion_rules"]["sha256"]
        # 同路径内容变化：计划引用与内容指纹必须变化，不能继续复用旧结果
        rules_copy.write_text(rules_copy.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        _, _, compiled2 = _compile(case)
        refs2 = {ref["role"]: ref for ref in compiled2.plan["input_refs"]}
        self.assertNotEqual(refs2["attribute_conversion_rules"]["sha256"], first_sha)
        self.assertNotEqual(compiled2.plan["plan_sha256"], first_plan)

    def test_rule_file_change_invalidates_compile_cache(self):
        import os
        import subprocess
        import sys

        work = SCRATCH / "rule-cache"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        template = _load(TEMPLATE)
        rules_copy = work / "attribute-conversions-v1.json"
        rules_copy.write_bytes((ROOT / "tools/phase3/rules/attribute-conversions-v1.json").read_bytes())
        template["definition"]["attribute_conversion"]["rules"] = rules_copy.relative_to(ROOT).as_posix()
        template_path = work / "template.json"
        template_path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case = _case()
        case["calculation"]["source"]["template"] = template_path.relative_to(ROOT).as_posix()
        case_path = work / "case.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        cache_root = work / "cache"
        env = dict(os.environ, PYTHONIOENCODING="utf-8", LE_PIPELINE_CACHE_ROOT=cache_root.relative_to(ROOT).as_posix())

        def run(out_name: str) -> None:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/le_commands.py"), "run", "--case",
                 case_path.relative_to(ROOT).as_posix(), "--out", (work / out_name).relative_to(ROOT).as_posix(),
                 "--run-id", "rule-cache"],
                cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(completed.returncode, 3, completed.stderr[-1000:])

        run("out1")
        run("out2")
        state2 = _load(work / "out2" / "pipeline-state.json")
        entries2 = {entry["stage_id"]: entry for entry in state2.get("stages") or []}
        self.assertEqual(entries2["compile_plan"].get("cache_status"), "hit")
        rules_copy.write_text(rules_copy.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        run("out3")
        state3 = _load(work / "out3" / "pipeline-state.json")
        entries3 = {entry["stage_id"]: entry for entry in state3.get("stages") or []}
        self.assertEqual(entries3["compile_plan"].get("cache_status"), "miss")

    def test_multi_instance_selector_and_single_instance_ambiguity(self):
        calc = _load(ROOT / _case()["inputs"]["calculation_input"])
        weapon = next(item for item in calc["items"] if item.get("instance_id") == "equipment.weapon1")
        clone = copy.deepcopy(weapon)
        clone["instance_id"] = "equipment.weapon2"
        for mod in clone.get("mods") or []:
            mod["mod_id"] = str(mod.get("mod_id")).replace("equipment.weapon1", "equipment.weapon2")
            mod["item_instance_id"] = "equipment.weapon2"
        calc["items"].append(clone)
        source = {
            "kind": "equipment_mod",
            "binding_selector": {
                "kind": "equipment_mod",
                "item_definition_entity_id": "item:letools:uniqueList.uniques:338",
                "expected_unique_mod_index": 5,
                "selection": "many",
            },
        }
        resolved = definitions_v3._resolve_effect(source, calc)
        self.assertEqual(len(resolved), 2)
        self.assertEqual({row["item_instance_id"] for row in resolved}, {"equipment.weapon1", "equipment.weapon2"})
        ambiguous = copy.deepcopy(source)
        ambiguous["binding_selector"].pop("selection")
        with self.assertRaises(definitions_v3.DefinitionBindingError):
            definitions_v3._resolve_effect(ambiguous, calc)


class V3FormalRunTest(unittest.TestCase):
    def test_formal_run_artifacts_and_diff_mapping(self):
        if not (STEP4_RUN / "result.json").is_file():
            self.skipTest("缺少 dps-step4-vc-8 正式 run")
        result = _load(STEP4_RUN / "result.json")
        old = _load(STEP3_RUN / "result.json")
        for key in ("sustained_dps", "single_hit", "hit_dps", "dot_dps", "interval_s"):
            self.assertEqual(result[key], old[key])
        for key in ("coverage", "contributions", "applications", "effect_ledger", "issues"):
            self.assertEqual(result[key], old[key], key)
        self.assertIsNone(result.get("profile_sha256"))
        manifest = _load(STEP4_RUN / "run-manifest.json")
        self.assertEqual(len(manifest["files"]), 12)
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((STEP4_RUN / name).read_bytes()).hexdigest(), digest, name)
        validation = _load(STEP4_RUN / "validation-result.json")
        self.assertTrue(validation["passed"])
        plan = _load(STEP4_RUN / "calculation-plan.json")
        self.assertEqual(plan["plan_sha256"], manifest["plan_sha256"])
        binding = _load(STEP4_RUN / "report-binding.json")
        self.assertEqual(binding["report_ref"]["run_dir"], STEP4_RUN.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    unittest.main(verbosity=2)
