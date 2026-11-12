# -*- coding: utf-8 -*-
"""P3 输入路线与依赖检查（``scripts/le_pipeline``）的单元测试。

全部使用标准库 + 临时目录 fixture + monkeypatch（环境变量/文件删除），
不依赖真实外部工具、不联网、不写仓库文件、不执行路线命令。
另含少量只读的仓库真实 case 判定测试（BH 混合来源 / FR 存档路线），
用于锁定 D2 修复后的三路线语义。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from contextlib import chdir
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from le_pipeline import (  # noqa: E402
    DEPENDENCIES,
    PLANNER_ROUTE_ID,
    ROUTE_IDS,
    ROUTES,
    SAVE_ROUTE_ID,
    check_routes,
    dependency_catalog,
    get_route,
    list_routes,
    resolve_route,
    resolve_route_detail,
    route_to_dict,
)
from le_pipeline.routes import SAVE_PLANNER_ROUTE_ID  # noqa: E402

ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|,;)]*")

#: 合成 Windows 风格路径（拼接构造，源码中不出现字面机器路径；用于验证结果不带绝对路径）
_FAKE_DRIVE = chr(68) + ":"
_FAKE_TOOL = _FAKE_DRIVE + "/tools/lastepochsupertools_v1.3.1"
_FAKE_SAVE = chr(67) + ":/some/where/1CHARACTERSLOT_BETA_1"

SAVE_CASE_REL = "examples/save-case/case.json"
SAVE_NEW_CASE_REL = "examples/save-new-case/case.json"
COMBINED_CASE_REL = "examples/combined-case/case.json"
PLANNER_ONLY_CASE_REL = "examples/planner-only-case/case.json"
AMBIGUOUS_CASE_REL = "examples/ambiguous-case/case.json"

MODULE_DIRS = (
    ("tools/save_import/src/le_save_import", "le_save_import"),
    ("tools/build_import/src/le_build", "le_build"),
    ("tools/phase3/src/le_calc", "le_calc"),
)
PIPELINE_SCRIPTS = (
    "tools/save_import/scripts/pipeline/run_affix_db_pipeline.py",
    "tools/save_import/scripts/pipeline/apply_corrections.py",
    "tools/save_import/scripts/pipeline/make_snapshot.py",
)


def _write_json(path: pathlib.Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: pathlib.Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_bytes(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class _TempRepo:
    """最小可移植仓库 fixture（只含 check_routes 需要的路径骨架）。"""

    def __init__(self, *, configure_external: bool = False) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="le-pipeline-test-")
        self.root = pathlib.Path(self._tmp.name)
        self.configure_external = configure_external
        self._build()

    def cleanup(self) -> None:
        self._tmp.cleanup()

    # -- fixture ---------------------------------------------------------
    def _build(self) -> None:
        root = self.root
        config = {
            "schema": "le-project-config-v1",
            "python": {"candidates": [".venv/Scripts/python.exe"], "min_version": "3.12"},
            "dataset": {"pointer": "data/current.json"},
            "affix_db": "data/affix-db/v1/normalized/affix-db.sqlite",
        }
        if self.configure_external:
            config["external_tools"] = {
                "node": {"path": "vendor/node/node.exe"},
                "tool_native": {
                    "harness_script": "vendor/run_tool_native.js",
                    "tool_home": "vendor/tool",
                },
            }
        _write_json(root / "config" / "project.json", config)
        _write_json(
            root / "data" / "current.json",
            {
                "dataset_id": "phase1-dataset-003",
                "database": "data/releases/phase1-dataset-003/data.sqlite",
                "logical_content_sha256": "0" * 64,
            },
        )
        _write_bytes(root / "data" / "affix-db" / "v1" / "normalized" / "affix-db.sqlite", b"")
        _write_bytes(root / "data" / "raw" / "saves" / "1CHARACTERSLOT_BETA_1", b"save")
        _write_bytes(root / "data" / "raw" / "saves" / "STASH_0", b"stash")
        for directory, module in MODULE_DIRS:
            _write_text(root / directory / "__init__.py", "# %s\n" % module)
        for script in PIPELINE_SCRIPTS:
            _write_text(root / script, "# fixture script\n")
        _write_text(root / "scripts" / "le_commands.py", "# fixture\n")

        # save 路线（含归档 wire + run-summary 证据）
        _write_json(
            root / "examples" / "save-case" / "inputs" / "tool-native" / "wire.json",
            {"data": {"bio": {"level": 100}}, "dataVersion": "Version 1.5.2"},
        )
        _write_json(
            root / "examples" / "save-case" / "inputs" / "tool-native" / "run-summary.json",
            {
                "tool": _FAKE_TOOL,
                "dbFiles": [
                    _FAKE_TOOL + "/data/version150/db/js/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.js",
                ],
                "save": _FAKE_SAVE,
            },
        )
        _write_json(
            root / SAVE_CASE_REL,
            self._case(
                "save-case",
                source={
                    "character_save": "data/raw/saves/1CHARACTERSLOT_BETA_1",
                    "tool_native_wire": "examples/save-case/inputs/tool-native/wire.json",
                    "tool_native_run_summary": "examples/save-case/inputs/tool-native/run-summary.json",
                },
                main_skill="fr11mv",
            ),
        )

        # 新存档输入（只有存档，尚无 wire → 依赖 harness）
        _write_json(
            root / SAVE_NEW_CASE_REL,
            self._case(
                "save-new-case",
                source={"character_save": "data/raw/saves/1CHARACTERSLOT_BETA_1"},
                main_skill="fr11mv",
            ),
        )

        # 混合来源（存档 + planner payload；BH 形态）
        _write_json(
            root / "examples" / "combined-case" / "inputs" / "planner-build-response.json",
            {"data": {"bio": {"level": 100}}, "dataVersion": "Version 1.5.2"},
        )
        _write_json(
            root / COMBINED_CASE_REL,
            self._case(
                "combined-case",
                source={
                    "character_save": "data/raw/saves/1CHARACTERSLOT_BETA_1",
                    "stash": "data/raw/saves/STASH_0",
                    "planner_build_response": "examples/combined-case/inputs/planner-build-response.json",
                },
                main_skill=None,
            ),
        )

        # 只有 planner payload（无存档）
        _write_json(
            root / "examples" / "planner-only-case" / "inputs" / "planner-build-response.json",
            {"data": {"bio": {"level": 100}}, "dataVersion": "Version 1.5.2"},
        )
        _write_json(
            root / PLANNER_ONLY_CASE_REL,
            self._case(
                "planner-only-case",
                source={
                    "planner_build_response": "examples/planner-only-case/inputs/planner-build-response.json",
                },
                main_skill=None,
            ),
        )

        # 归档来源歧义（wire 与 planner payload 同时存在）
        _write_json(
            root / AMBIGUOUS_CASE_REL,
            self._case(
                "ambiguous-case",
                source={
                    "character_save": "data/raw/saves/1CHARACTERSLOT_BETA_1",
                    "tool_native_wire": "examples/save-case/inputs/tool-native/wire.json",
                    "planner_build_response": "examples/combined-case/inputs/planner-build-response.json",
                },
                main_skill="fr11mv",
            ),
        )

        if self.configure_external:
            _write_bytes(root / "vendor" / "node" / "node.exe", b"fake-node")
            _write_text(root / "vendor" / "run_tool_native.js", "// fake harness\n")
            _write_text(root / "vendor" / "tool" / "planner" / "js" / "planner.js", "// fake planner\n")
            _write_text(
                root
                / "vendor"
                / "tool"
                / "data"
                / "version150"
                / "db"
                / "js"
                / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.js",
                "// fake db\n",
            )

    @staticmethod
    def _case(case_id: str, *, source: dict, main_skill: str | None) -> dict:
        case = {
            "schema": "le-case-v1",
            "case_id": case_id,
            "dataset": "data/current.json",
            "affix_db": "data/affix-db/v1/normalized/affix-db.sqlite",
            "source": source,
        }
        if main_skill:
            case["main_skill"] = {"ability_id": main_skill}
        return case


class PipelineTestBase(unittest.TestCase):
    configure_external = False

    def setUp(self) -> None:
        self.repo = _TempRepo(configure_external=self.configure_external)
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root

    def report(self, case_ref: str, **kwargs):
        return check_routes(case_ref, self.root, **kwargs)

    @staticmethod
    def dep_by_id(report: dict, dep_id: str) -> dict:
        for item in report["dependencies"]:
            if item["id"] == dep_id:
                return item
        raise AssertionError("dependency not found: %s" % dep_id)

    @staticmethod
    def assert_no_abs_paths(testcase: unittest.TestCase, payload: object) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        testcase.assertIsNone(ABS_PATH_RE.search(text), "结果中出现机器绝对路径：%s" % text[:300])


# ---------------------------------------------------------------------------
# 路线描述（三条）
# ---------------------------------------------------------------------------


class RouteCatalogTests(unittest.TestCase):
    def test_three_routes_registered(self) -> None:
        self.assertEqual(len(ROUTES), 3)
        route_ids = [route.route_id for route in ROUTES]
        self.assertEqual(
            route_ids, [SAVE_ROUTE_ID, SAVE_PLANNER_ROUTE_ID, PLANNER_ROUTE_ID]
        )
        self.assertEqual(route_ids, list(ROUTE_IDS))
        self.assertEqual(get_route(SAVE_ROUTE_ID).route_id, SAVE_ROUTE_ID)
        self.assertEqual(get_route(SAVE_PLANNER_ROUTE_ID).route_id, SAVE_PLANNER_ROUTE_ID)
        self.assertEqual(get_route(PLANNER_ROUTE_ID).route_id, PLANNER_ROUTE_ID)
        self.assertIsNone(get_route("no-such-route"))

    def test_route_spec_contract(self) -> None:
        save = get_route(SAVE_ROUTE_ID)
        combined = get_route(SAVE_PLANNER_ROUTE_ID)
        planner = get_route(PLANNER_ROUTE_ID)
        self.assertEqual(save.input_kind, "character_save")
        self.assertEqual(combined.input_kind, "character_save+planner_payload")
        self.assertEqual(planner.input_kind, "planner_payload")
        for route in (save, combined, planner):
            self.assertTrue(route.requires_main_skill)
            self.assertEqual(route.fallback_policy, "never_fallback")
            self.assertEqual(route.stage_sequence[0], "freeze_input")
            self.assertEqual(route.stage_sequence[-1], "min_validate")
        # D2：混合来源路线必须包含 parse_input（save → bd.json）与后续 7 阶段
        self.assertIn("parse_input", combined.stage_sequence)
        self.assertEqual(len(combined.stage_sequence), 8)
        self.assertEqual(len(save.stage_sequence), 8)
        self.assertEqual(len(planner.stage_sequence), 7)
        self.assertIn("source.character_save", combined.required_input_fields)
        self.assertIn("source.planner_build_response", combined.required_input_fields)
        self.assertIn("source.character_save", save.required_input_fields)
        self.assertIn("source.planner_build_response", planner.required_input_fields)

    def test_combined_stage_sequence_matches_prepare_stages(self) -> None:
        """D2 核心：路线 stage_sequence 必须与实际 prepare 执行的 8 段一致。"""
        expected = (
            "freeze_input",
            "parse_input",
            "normalize_build",
            "export_calc_input",
            "apply_affix_ranges",
            "resolve_skills",
            "prepare_snapshot",
            "min_validate",
        )
        combined = get_route(SAVE_PLANNER_ROUTE_ID)
        self.assertEqual(combined.stage_sequence, expected)
        self.assertEqual(get_route(SAVE_ROUTE_ID).stage_sequence, expected)
        self.assertEqual(
            get_route(PLANNER_ROUTE_ID).stage_sequence,
            tuple(stage for stage in expected if stage != "parse_input"),
        )
        # 混合路线的 parse_input 必须调用既有存档解析入口（含 --planner）
        step = combined.step("parse_input")
        self.assertIsNotNone(step)
        command = step.commands[0]
        self.assertEqual(command.tool, "le_save_import")
        self.assertIn("--save", command.argv)
        self.assertIn("--planner", command.argv)

    def test_stage_ids_are_contract_ids(self) -> None:
        contract_stages = {
            "freeze_input",
            "parse_input",
            "normalize_build",
            "export_calc_input",
            "apply_affix_ranges",
            "resolve_skills",
            "prepare_snapshot",
            "min_validate",
        }
        for route in ROUTES:
            self.assertTrue(set(route.stage_sequence) <= contract_stages)
            for step in route.steps:
                self.assertIn(step.stage_id, contract_stages)
                if step.stage_id not in route.stage_sequence:
                    self.assertIsNotNone(step.when, "optional step %s 必须标 when" % step.stage_id)
                self.assertIn(step.availability, {"existing", "external_tool", "adapter_required"})
                for command in step.commands:
                    self.assertEqual(command.stage_id, step.stage_id)
                    self.assertTrue(command.argv)
                    self.assertTrue(command.tool)

    def test_commands_have_placeholders_and_repo_relative_paths(self) -> None:
        placeholder_re = re.compile(r"^\{[^}]+\}$")
        for route in ROUTES:
            for step in route.steps:
                for command in step.commands:
                    for token in command.argv:
                        if token.startswith("{") and token.endswith("}"):
                            self.assertTrue(placeholder_re.match(token), token)
                            continue
                        # 非占位符参数必须是相对路径/普通参数，不得是机器绝对路径
                        self.assertIsNone(ABS_PATH_RE.search(token), token)
                        self.assertFalse(token.startswith("/"), token)
                    for path in command.pythonpath:
                        self.assertFalse(pathlib.PurePosixPath(path).is_absolute(), path)
                        self.assertIsNone(ABS_PATH_RE.search(path), path)
                    for output in command.outputs:
                        self.assertIsNone(ABS_PATH_RE.search(output), output)
                        self.assertFalse(output.startswith("/"), output)

    def test_route_to_dict_json_serialisable(self) -> None:
        for route in ROUTES:
            payload = route_to_dict(route)
            text = json.dumps(payload, ensure_ascii=False)
            self.assertIn(route.route_id, text)
            self.assertIsNone(ABS_PATH_RE.search(text))
        listing = list_routes()
        self.assertEqual(len(listing), 3)
        json.dumps(listing, ensure_ascii=False)

    def test_dependency_ids_resolve_to_catalog(self) -> None:
        catalog_ids = {dep.id for dep in DEPENDENCIES}
        for route in ROUTES:
            for dep_id in route.dependency_ids:
                self.assertIn(dep_id, catalog_ids, dep_id)


class ResolveRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root

    @staticmethod
    def _load(root: pathlib.Path, rel: str) -> dict:
        return json.loads((root / rel).read_text(encoding="utf-8"))

    def test_save_case_resolves_to_save_route(self) -> None:
        route = resolve_route(self._load(self.root, SAVE_CASE_REL))
        self.assertIsNotNone(route)
        self.assertEqual(route.route_id, SAVE_ROUTE_ID)

    def test_save_only_case_resolves_to_save_route(self) -> None:
        route = resolve_route(self._load(self.root, SAVE_NEW_CASE_REL))
        self.assertEqual(route.route_id, SAVE_ROUTE_ID)

    def test_save_plus_planner_resolves_to_combined_route(self) -> None:
        """D2：BH 形态（存档 + planner 同时存在）必须返回混合路线，而不是 planner-payload。"""
        case = self._load(self.root, COMBINED_CASE_REL)
        route = resolve_route(case)
        self.assertEqual(route.route_id, SAVE_PLANNER_ROUTE_ID)
        detail = resolve_route_detail(case)
        self.assertEqual(detail["candidates"], [SAVE_PLANNER_ROUTE_ID])
        self.assertIn("混合来源", detail["reason"])

    def test_planner_only_case_resolves_to_planner_route(self) -> None:
        route = resolve_route(self._load(self.root, PLANNER_ONLY_CASE_REL))
        self.assertEqual(route.route_id, PLANNER_ROUTE_ID)

    def test_route_from_path(self) -> None:
        self.assertEqual(resolve_route(self.root / SAVE_CASE_REL).route_id, SAVE_ROUTE_ID)
        with chdir(self.root):
            self.assertEqual(resolve_route(SAVE_CASE_REL).route_id, SAVE_ROUTE_ID)

    def test_archived_ambiguity_returns_none(self) -> None:
        case = self._load(self.root, AMBIGUOUS_CASE_REL)
        self.assertIsNone(resolve_route(case))
        detail = resolve_route_detail(case)
        self.assertIsNone(detail["route"])
        self.assertEqual(detail["reason"], "ambiguous_archived_sources")
        self.assertEqual(detail["candidates"], [SAVE_ROUTE_ID, PLANNER_ROUTE_ID])

    def test_explicit_source_kind_wins(self) -> None:
        empty_case = {"case_id": "new-input", "source": {}}
        self.assertEqual(resolve_route(empty_case, source_kind="save").route_id, SAVE_ROUTE_ID)
        self.assertEqual(
            resolve_route(empty_case, source_kind="planner_payload").route_id, PLANNER_ROUTE_ID
        )
        for alias in ("save+planner", "save_planner", "save-planner", "mixed"):
            self.assertEqual(
                resolve_route(empty_case, source_kind=alias).route_id, SAVE_PLANNER_ROUTE_ID
            )
        self.assertIsNone(resolve_route(empty_case))

    def test_three_routes_are_mutually_exclusive_by_source_shape(self) -> None:
        shapes = {
            SAVE_CASE_REL: SAVE_ROUTE_ID,
            SAVE_NEW_CASE_REL: SAVE_ROUTE_ID,
            COMBINED_CASE_REL: SAVE_PLANNER_ROUTE_ID,
            PLANNER_ONLY_CASE_REL: PLANNER_ROUTE_ID,
        }
        for rel, expected in shapes.items():
            self.assertEqual(resolve_route(self._load(self.root, rel)).route_id, expected, rel)

    def test_unreadable_case_returns_none(self) -> None:
        self.assertIsNone(resolve_route(self.root / "missing.json"))
        self.assertIsNone(resolve_route(12345))


# ---------------------------------------------------------------------------
# 依赖目录
# ---------------------------------------------------------------------------


class DependencyCatalogTests(unittest.TestCase):
    def test_catalog_contract(self) -> None:
        catalog = dependency_catalog()
        self.assertTrue(catalog)
        kinds = {"python", "repo_file", "external_tool", "local_config"}
        route_ids = set(ROUTE_IDS)
        for item in catalog:
            self.assertIn(item["kind"], kinds)
            self.assertTrue(item["required_for"])
            self.assertTrue(item["label"])
            self.assertTrue(item["hint"])
            self.assertIsInstance(item["required"], bool)
            self.assertTrue(item["routes"])
            for route_id in item["routes"]:
                self.assertTrue(route_id == "*" or route_id in route_ids, route_id)
        json.dumps(catalog, ensure_ascii=False)
        self.assertIsNone(ABS_PATH_RE.search(json.dumps(catalog, ensure_ascii=False)))

    def test_catalog_matches_route_dependency_lists(self) -> None:
        catalog_ids = {item["id"] for item in dependency_catalog()}
        for route in ROUTES:
            self.assertTrue(set(route.dependency_ids) <= catalog_ids)
        # 目录里每个依赖都应至少被一条路线引用（避免死条目）
        referenced = set()
        for route in ROUTES:
            referenced.update(route.dependency_ids)
        self.assertEqual(catalog_ids, referenced)

    def test_combined_dependency_union(self) -> None:
        save = get_route(SAVE_ROUTE_ID)
        planner = get_route(PLANNER_ROUTE_ID)
        combined = get_route(SAVE_PLANNER_ROUTE_ID)
        self.assertEqual(set(combined.dependency_ids), set(save.dependency_ids) | set(planner.dependency_ids))
        self.assertEqual(len(combined.dependency_ids), len(set(combined.dependency_ids)))
        self.assertIn("input.character_save", combined.dependency_ids)
        self.assertIn("input.planner_payload", combined.dependency_ids)
        self.assertIn("external.tool_native.harness", combined.dependency_ids)

    def test_dependency_resolve_is_path_safe(self) -> None:
        repo = _TempRepo()
        try:
            for dep in DEPENDENCIES:
                resolved = dep.resolve(repo.root, {"external_tools": {}}, {})
                text = json.dumps(resolved, ensure_ascii=False, default=str)
                self.assertIsNone(ABS_PATH_RE.search(text), dep.id)
                self.assertIn("id", resolved)
        finally:
            repo.cleanup()


# ---------------------------------------------------------------------------
# check_routes：save（归档 wire）/ 混合来源 / planner-only / 失败语义
# ---------------------------------------------------------------------------


class CheckRoutesSaveArchivedTests(PipelineTestBase):
    def test_archived_wire_case_ready_with_optional_undetermined(self) -> None:
        report = self.report(SAVE_CASE_REL)
        self.assertTrue(report["ready"])
        self.assertEqual(report["route_id"], SAVE_ROUTE_ID)
        self.assertEqual(report["needs_input"], [])
        self.assertEqual(report["fallback_policy"], "never_fallback")
        # 已归档 wire：harness 变为可选；未配置时必须 undetermined，而不是 ok
        harness = self.dep_by_id(report, "external.tool_native.harness")
        install = self.dep_by_id(report, "external.tool_native.install")
        self.assertEqual(harness["status"], "undetermined")
        self.assertFalse(harness["required"])
        self.assertEqual(install["status"], "undetermined")
        self.assertFalse(install["required"])
        self.assertTrue(
            any(w["dependency_id"] == "external.tool_native.harness" for w in report["warnings"])
        )
        self.assert_no_abs_paths(self, report)

    def test_optional_missing_stash_is_warning_only(self) -> None:
        report = self.report(SAVE_CASE_REL)
        stash = self.dep_by_id(report, "input.stash")
        self.assertEqual(stash["status"], "missing")
        self.assertFalse(stash["required"])
        self.assertTrue(any(w["dependency_id"] == "input.stash" for w in report["warnings"]))
        self.assertTrue(report["ready"])

    def test_main_skill_from_case(self) -> None:
        report = self.report(SAVE_CASE_REL)
        self.assertEqual(self.dep_by_id(report, "input.main_skill")["status"], "ok")
        self.assertIn("fr11mv", self.dep_by_id(report, "input.main_skill")["detail"])


class CheckRoutesCombinedTests(PipelineTestBase):
    def test_combined_ready_with_main_skill_and_dependency_union(self) -> None:
        report = self.report(COMBINED_CASE_REL, main_skill="bh2")
        self.assertTrue(report["ready"], report["needs_input"])
        self.assertEqual(report["route_id"], SAVE_PLANNER_ROUTE_ID)
        self.assertEqual(report["needs_input"], [])
        route = report["route"]
        self.assertIn("parse_input", route["stage_sequence"])
        self.assertEqual(len(route["stage_sequence"]), 8)
        self.assertEqual(route["fallback_policy"], "never_fallback")
        # 依赖取并集：存档 + planner payload 都 required；harness 在归档 wire 缺失时仍为可选
        self.assertTrue(self.dep_by_id(report, "input.character_save")["required"])
        self.assertTrue(self.dep_by_id(report, "input.planner_payload")["required"])
        for dep_id in (
            "external.tool_native.node",
            "external.tool_native.harness",
            "external.tool_native.install",
        ):
            self.assertFalse(self.dep_by_id(report, dep_id)["required"], dep_id)
        harness = self.dep_by_id(report, "external.tool_native.harness")
        self.assertEqual(harness["status"], "undetermined")
        self.assertTrue(report["ready"], "可选 undetermined 不得把既有案例判失败")
        self.assert_no_abs_paths(self, report)

    def test_combined_missing_main_skill_needs_input(self) -> None:
        report = self.report(COMBINED_CASE_REL)
        self.assertFalse(report["ready"])
        self.assertEqual(report["route_id"], SAVE_PLANNER_ROUTE_ID)
        codes = {item["code"] for item in report["needs_input"]}
        self.assertIn("main_skill_required", codes)
        item = next(i for i in report["needs_input"] if i["code"] == "main_skill_required")
        self.assertIn("--main-skill", item["hint"])

    def test_combined_losing_planner_falls_back_by_fields_to_save_harness(self) -> None:
        """显式字段变化 → 存档单来源；此时 harness 必需，不得静默 ready。"""
        case = json.loads((self.root / COMBINED_CASE_REL).read_text(encoding="utf-8"))
        case["source"].pop("planner_build_response")
        case["main_skill"] = {"ability_id": "bh2"}
        report = check_routes(case, self.root)
        self.assertFalse(report["ready"])
        self.assertEqual(report["route_id"], SAVE_ROUTE_ID)
        required_ids = {item["dependency_ids"][0] for item in report["needs_input"]}
        self.assertIn("external.tool_native.harness", required_ids)
        self.assertNotIn(SAVE_PLANNER_ROUTE_ID, [report["route_id"]])

    def test_combined_losing_character_save_becomes_planner_only(self) -> None:
        case = json.loads((self.root / COMBINED_CASE_REL).read_text(encoding="utf-8"))
        case["source"].pop("character_save")
        case["source"].pop("stash")
        case["main_skill"] = {"ability_id": "bh2"}
        report = check_routes(case, self.root)
        self.assertEqual(report["route_id"], PLANNER_ROUTE_ID)
        self.assertTrue(report["ready"], report["needs_input"])
        self.assertFalse(self.dep_by_id(report, "input.character_save")["required"])


class CheckRoutesPlannerOnlyTests(PipelineTestBase):
    def test_planner_ready_with_explicit_main_skill(self) -> None:
        report = self.report(PLANNER_ONLY_CASE_REL, main_skill="bh2")
        self.assertTrue(report["ready"])
        self.assertEqual(report["route_id"], PLANNER_ROUTE_ID)
        self.assertEqual(report["needs_input"], [])
        self.assertNotIn("parse_input", report["route"]["stage_sequence"])
        self.assertFalse(self.dep_by_id(report, "input.character_save")["required"])
        self.assertFalse(
            any(dep["id"].startswith("external.tool_native") for dep in report["dependencies"])
        )
        self.assert_no_abs_paths(self, report)

    def test_planner_missing_main_skill_needs_input(self) -> None:
        report = self.report(PLANNER_ONLY_CASE_REL)
        self.assertFalse(report["ready"])
        codes = {item["code"] for item in report["needs_input"]}
        self.assertIn("main_skill_required", codes)
        self.assertEqual(report["route_id"], PLANNER_ROUTE_ID)


class CheckRoutesSaveNewInputTests(PipelineTestBase):
    def test_missing_harness_needs_input_without_fallback(self) -> None:
        report = self.report(SAVE_NEW_CASE_REL)
        self.assertFalse(report["ready"])
        self.assertEqual(report["route_id"], SAVE_ROUTE_ID)
        required_ids = {item["dependency_ids"][0] for item in report["needs_input"]}
        self.assertIn("external.tool_native.harness", required_ids)
        self.assertIn("external.tool_native.install", required_ids)
        self.assertTrue(all(item["required"] for item in report["needs_input"]))
        hint = " ".join(item["hint"] for item in report["needs_input"])
        self.assertIn("config/project.json", hint)
        self.assertIn("external_tools", hint)
        # 不静默 fallback 到 planner 路线
        self.assertNotIn(PLANNER_ROUTE_ID, [report["route_id"]])
        self.assertFalse(
            any(dep["id"] == "input.planner_payload" for dep in report["dependencies"])
        )
        self.assert_no_abs_paths(self, report)

    def test_unconfigured_external_tools_are_undetermined_not_ok(self) -> None:
        report = self.report(SAVE_NEW_CASE_REL)
        for dep_id in (
            "external.tool_native.harness",
            "external.tool_native.install",
        ):
            status = self.dep_by_id(report, dep_id)["status"]
            self.assertEqual(status, "undetermined")
        node = self.dep_by_id(report, "external.tool_native.node")
        self.assertIn(node["status"], {"missing", "undetermined"})

    def test_configured_harness_makes_new_save_input_ready(self) -> None:
        repo = _TempRepo(configure_external=True)
        self.addCleanup(repo.cleanup)
        report = check_routes(SAVE_NEW_CASE_REL, repo.root)
        self.assertTrue(report["ready"], report["needs_input"])
        for dep_id in (
            "external.tool_native.node",
            "external.tool_native.harness",
            "external.tool_native.install",
        ):
            item = self.dep_by_id(report, dep_id)
            self.assertEqual(item["status"], "ok", item)
        self.assert_no_abs_paths(self, report)
        install = self.dep_by_id(report, "external.tool_native.install")
        self.assertIn("vendor/tool", install["detail"])


class CheckRoutesToolNativeEvidenceTests(unittest.TestCase):
    def test_tool_label_derived_from_run_summary(self) -> None:
        repo = _TempRepo(configure_external=True)
        self.addCleanup(repo.cleanup)
        report = check_routes(SAVE_CASE_REL, repo.root)
        install = next(d for d in report["dependencies"] if d["id"] == "external.tool_native.install")
        self.assertEqual(install["status"], "ok")
        self.assertIn("lastepochsupertools_v1.3.1", install["detail"])
        self.assertIsNone(ABS_PATH_RE.search(json.dumps(report, ensure_ascii=False)))


class CheckRoutesFailureTests(PipelineTestBase):
    def test_invalid_case_path_is_structured(self) -> None:
        report = check_routes("examples/does-not-exist/case.json", self.root)
        self.assertFalse(report["ready"])
        self.assertIsNone(report["route_id"])
        codes = {item["code"] for item in report["needs_input"]}
        self.assertIn("case_unreadable", codes)
        self.assert_no_abs_paths(self, report)

    def test_case_without_supported_source_needs_route_selection(self) -> None:
        report = check_routes({"case_id": "empty", "source": {}}, self.root)
        self.assertFalse(report["ready"])
        self.assertIsNone(report["route_id"])
        codes = {item["code"] for item in report["needs_input"]}
        self.assertIn("route_selection", codes)

    def test_missing_repo_file_reports_missing_with_hint(self) -> None:
        (self.root / "tools" / "save_import" / "scripts" / "pipeline" / "make_snapshot.py").unlink()
        report = self.report(SAVE_CASE_REL)
        self.assertFalse(report["ready"])
        item = self.dep_by_id(report, "repo.pipeline_scripts")
        self.assertEqual(item["status"], "missing")
        self.assertTrue(item["hint"])

    def test_missing_dataset_pointer_reports_missing(self) -> None:
        (self.root / "data" / "current.json").unlink()
        report = self.report(SAVE_CASE_REL)
        self.assertFalse(report["ready"])
        self.assertEqual(self.dep_by_id(report, "repo.dataset_pointer")["status"], "missing")

    def test_missing_python_module_reports_missing(self) -> None:
        import shutil

        shutil.rmtree(self.root / "tools" / "build_import" / "src" / "le_build")
        report = self.report(SAVE_CASE_REL)
        self.assertFalse(report["ready"])
        item = self.dep_by_id(report, "python.module.le_build")
        self.assertEqual(item["status"], "missing")

    def test_never_raises_on_weird_inputs(self) -> None:
        for case_ref in (None, 123, object(), self.root / "missing.json"):
            report = check_routes(case_ref, self.root)
            self.assertIsInstance(report, dict)
            self.assertFalse(report["ready"])
            self.assertTrue(report["needs_input"])
        report = check_routes(SAVE_CASE_REL, self.root / "no-such-root")
        self.assertFalse(report["ready"])
        json.dumps(report, ensure_ascii=False)


class CheckRoutesSideEffectTests(PipelineTestBase):
    def _snapshot(self) -> list[tuple[str, int]]:
        rows = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                rows.append((path.relative_to(self.root).as_posix(), path.stat().st_size))
        return rows

    def test_check_routes_is_read_only(self) -> None:
        before = self._snapshot()
        sys_path_before = list(sys.path)
        report = self.report(SAVE_CASE_REL)
        after = self._snapshot()
        self.assertTrue(report["ready"])
        self.assertEqual(before, after)
        # 模块可导入性检查临时改 sys.path 后必须恢复
        self.assertEqual(sys_path_before, list(sys.path))

    def test_check_routes_is_deterministic(self) -> None:
        first = self.report(SAVE_CASE_REL)
        second = self.report(SAVE_CASE_REL)
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))

    def test_env_override_does_not_require_real_tool(self) -> None:
        fake_node = self.root / "vendor" / "node" / "node.exe"
        _write_bytes(fake_node, b"fake")
        with mock.patch.dict(os.environ, {"LE_NODE": "vendor/node/node.exe"}, clear=False):
            report = self.report(SAVE_CASE_REL)
        node = self.dep_by_id(report, "external.tool_native.node")
        self.assertEqual(node["status"], "ok")
        self.assertIn("LE_NODE", node["detail"])


# ---------------------------------------------------------------------------
# 真实仓库案例（只读；锁定 D2 三路线语义）
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    (REPO_ROOT / "examples/black-hole/case.legacy.json").is_file(),
    "author-local legacy case assets absent (public release ships no cases)",
)
class RealRepoCaseTests(unittest.TestCase):
    def test_black_hole_is_save_plus_planner_and_ready(self) -> None:
        report = check_routes("examples/black-hole/case.legacy.json", REPO_ROOT, main_skill="bh2")
        self.assertEqual(report["route_id"], SAVE_PLANNER_ROUTE_ID)
        self.assertTrue(report["ready"], report["needs_input"])
        self.assertEqual(report["needs_input"], [])
        route = report["route"]
        self.assertIn("parse_input", route["stage_sequence"])
        self.assertEqual(len(route["stage_sequence"]), 8)
        self.assertEqual(len(report["dependencies"]), 19)
        self.assertIsNone(ABS_PATH_RE.search(json.dumps(report, ensure_ascii=False)))

    def test_black_hole_without_main_skill_only_needs_main_skill(self) -> None:
        report = check_routes("examples/black-hole/case.legacy.json", REPO_ROOT)
        self.assertEqual(report["route_id"], SAVE_PLANNER_ROUTE_ID)
        self.assertFalse(report["ready"])
        self.assertEqual(
            [item["code"] for item in report["needs_input"]],
            ["main_skill_required"],
        )

    def test_flame_reave_still_save_route_ready(self) -> None:
        report = check_routes("examples/flame-reave/case.legacy-hit.json", REPO_ROOT)
        self.assertEqual(report["route_id"], SAVE_ROUTE_ID)
        self.assertTrue(report["ready"], report["needs_input"])
        self.assertIn("parse_input", report["route"]["stage_sequence"])
        self.assertEqual(report["needs_input"], [])
        self.assertIsNone(ABS_PATH_RE.search(json.dumps(report, ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main()
