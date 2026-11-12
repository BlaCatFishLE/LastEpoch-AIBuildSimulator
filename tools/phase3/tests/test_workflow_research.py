# -*- coding: utf-8 -*-
"""研究交接契约测试：ResearchTask / ResearchProposal / validate_proposal / 文件交换 / schema。

全程使用临时目录与合成 payload，不依赖真实知识库条目、案例数值或 run 产物。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import contract, research

FP_A = "a" * 64
FP_B = "b" * 64
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas" / "workflow-v1"


# ---------------------------------------------------------------------------
# 最小 JSON Schema 子集校验（无第三方依赖，只覆盖本任务两份 schema 用到的关键字）
# ---------------------------------------------------------------------------


def _type_ok(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise AssertionError(f"unsupported schema type: {expected}")


def schema_errors(document: object, schema: dict, pointer: str = "$") -> list[str]:
    errors: list[str] = []
    if "const" in schema and document != schema["const"]:
        errors.append(f"{pointer}: const mismatch {document!r}")
    if "enum" in schema and document not in schema["enum"]:
        errors.append(f"{pointer}: {document!r} not in enum")
    if "type" in schema and not _type_ok(document, schema["type"]):
        errors.append(f"{pointer}: expected {schema['type']}, got {type(document).__name__}")
        return errors
    if isinstance(document, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, document):
            errors.append(f"{pointer}: {document!r} does not match {pattern}")
        if isinstance(schema.get("minLength"), int) and len(document) < schema["minLength"]:
            errors.append(f"{pointer}: shorter than minLength")
    if isinstance(document, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(document):
                errors.extend(schema_errors(item, item_schema, f"{pointer}[{index}]"))
        if isinstance(schema.get("minItems"), int) and len(document) < schema["minItems"]:
            errors.append(f"{pointer}: fewer than minItems")
    if isinstance(document, dict):
        if isinstance(schema.get("minProperties"), int) and len(document) < schema["minProperties"]:
            errors.append(f"{pointer}: fewer than minProperties")
        required = schema.get("required") or []
        for name in required:
            if name not in document:
                errors.append(f"{pointer}: missing required {name!r}")
        properties = schema.get("properties") or {}
        for name, item in document.items():
            if name in properties:
                errors.extend(schema_errors(item, properties[name], f"{pointer}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{pointer}: unexpected property {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(schema_errors(item, schema["additionalProperties"], f"{pointer}.{name}"))
        any_of = schema.get("anyOf")
        if isinstance(any_of, list):
            if not any(not schema_errors(document, option, pointer) for option in any_of):
                errors.append(f"{pointer}: does not satisfy anyOf")
    return errors


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def gap_dict(**overrides: object) -> dict:
    gap: dict = {
        "question": "目标侧减少与抗性穿透的结算顺序是否独立乘区？",
        "source_refs": [
            {"locator": "docs/evidence/fixture.md#L12", "field": "dr_formula"},
            {"artifact_id": "ruleset-fixture", "locator": "tools/phase3/rules/fixture.json#/rules id=R1"},
        ],
        "related_mechanism_refs": ["common-monster-dr", "common-target-resistance-penetration"],
        "researched_already": ["R13 只给出目标侧乘区，未说明顺序"],
        "affected": {
            "components": ["target_side", "hit"],
            "calculation_blocks": ["target_side"],
            "blocks_target": True,
        },
        "deliverable_kind": "parameter_rule",
        "verification_required": ["正向：BH/FR 单次数值在容差内", "反向：res=0 时两式逐位相等"],
    }
    gap.update(overrides)
    return gap


class TemporaryWorkspace:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def cleanup(self) -> None:
        self._temporary.cleanup()

    def evidence_file(self, relative: str = "docs/evidence/fixture.md") -> tuple[Path, str]:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture evidence\n", encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def list_files(self) -> list[str]:
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())


def build_task(**gap_overrides: object) -> dict:
    return research.make_research_task(gap_dict(**gap_overrides), input_fingerprint=FP_A)


def build_proposal(task: dict, **overrides: object) -> dict:
    proposal: dict = {
        "schema_version": contract.SCHEMA_RESEARCH_PROPOSAL,
        "proposal_id": "proposal-fixture-1",
        "task_id": task["task_id"],
        "based_on_fingerprint": task["input_fingerprint"],
        "outcome": contract.OUTCOME_PROPOSED_CHANGE,
        "mechanism_refs": ["common-target-resistance-penetration"],
        "mapping_refs": ["mapping:target-side"],
        "code_change_refs": [],
        "evidence_refs": [{"kind": "document", "locator": "docs/evidence/fixture.md#L12"}],
        "assumptions": [
            {
                "assumption_id": "assume-target-order",
                "status": "assumed",
                "note": "顺序取独立乘区；影响 target_side 组件",
            }
        ],
        "verification_refs": ["正向：两案例 reproduce 在容差内", "反向：res=0 / pen=0 时逐位一致"],
    }
    proposal.update(overrides)
    return proposal


# ---------------------------------------------------------------------------
# make_research_task
# ---------------------------------------------------------------------------


class ResearchTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TemporaryWorkspace()
        self.addCleanup(self.workspace.cleanup)

    def test_minimal_gap_builds_complete_contract(self) -> None:
        task = research.make_research_task(
            {"question": "Q?", "affected": {"components": ["hit"], "calculation_blocks": ["more_groups"]}},
            input_fingerprint=FP_A,
        )
        self.assertEqual(set(task), set(research.TASK_FIELDS))
        self.assertEqual(task["schema_version"], contract.SCHEMA_RESEARCH_TASK)
        self.assertRegex(task["task_id"], r"^research_task-[0-9a-f]{16}$")
        self.assertRegex(task["gap_id"], r"^gap-[0-9a-f]{16}$")
        self.assertEqual(task["input_fingerprint"], FP_A)
        self.assertEqual(task["deliverable_kind"], "mapping")
        self.assertEqual(task["affected"]["blocks_target"], False)
        self.assertEqual(task["source_refs"], [])
        self.assertEqual(task["related_mechanism_refs"], [])
        self.assertEqual(task["verification_required"], [])

    def test_full_gap_roundtrips_through_dataclass(self) -> None:
        task = build_task()
        self.assertEqual(set(task), set(research.TASK_FIELDS))
        self.assertEqual(task["affected"]["components"], ["target_side", "hit"])
        self.assertEqual(task["source_refs"][0]["field"], "dr_formula")
        self.assertEqual(task["deliverable_kind"], "parameter_rule")
        self.assertTrue(task["affected"]["blocks_target"])
        self.assertEqual(contract.ResearchTask.from_dict(task).to_dict(), task)

    def test_ids_are_deterministic_and_order_insensitive(self) -> None:
        first = build_task()
        second = build_task()
        self.assertEqual(first, second)
        reordered_gap = gap_dict(
            source_refs=list(reversed(gap_dict()["source_refs"])),
            related_mechanism_refs=list(reversed(gap_dict()["related_mechanism_refs"])),
            verification_required=list(reversed(gap_dict()["verification_required"])),
        )
        reordered = research.make_research_task(reordered_gap, input_fingerprint=FP_A)
        self.assertEqual(first["task_id"], reordered["task_id"])
        self.assertEqual(first["gap_id"], reordered["gap_id"])

    def test_input_fingerprint_change_generates_new_ids(self) -> None:
        first = build_task()
        changed = research.make_research_task(gap_dict(), input_fingerprint=FP_B)
        self.assertNotEqual(first["task_id"], changed["task_id"])
        self.assertNotEqual(first["gap_id"], changed["gap_id"])
        self.assertEqual(changed["input_fingerprint"], FP_B)

    def test_gap_content_change_generates_new_ids(self) -> None:
        first = build_task()
        changed = research.make_research_task(
            gap_dict(question="另一条缺口：时间行为是否可线性缩放？"), input_fingerprint=FP_A
        )
        self.assertNotEqual(first["task_id"], changed["task_id"])

    def test_explicit_gap_id_is_honoured(self) -> None:
        first = research.make_research_task(gap_dict(gap_id="gap-known-1"), input_fingerprint=FP_A)
        second = research.make_research_task(gap_dict(gap_id="gap-known-1"), input_fingerprint=FP_A)
        self.assertEqual(first["gap_id"], "gap-known-1")
        self.assertEqual(first["task_id"], second["task_id"])
        changed_fp = research.make_research_task(
            gap_dict(gap_id="gap-known-1"), input_fingerprint=FP_B
        )
        self.assertEqual(changed_fp["gap_id"], "gap-known-1")
        self.assertNotEqual(first["task_id"], changed_fp["task_id"])

    def test_fingerprint_prefers_argument_then_gap_field_then_fallback(self) -> None:
        explicit = research.make_research_task(gap_dict(input_fingerprint=FP_B), input_fingerprint=FP_A)
        self.assertEqual(explicit["input_fingerprint"], FP_A)
        from_gap = research.make_research_task(gap_dict(input_fingerprint=FP_B))
        self.assertEqual(from_gap["input_fingerprint"], FP_B)
        fallback = research.make_research_task(gap_dict())
        self.assertRegex(fallback["input_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(fallback, research.make_research_task(gap_dict()))

    def test_queue_style_aliases_are_accepted(self) -> None:
        queue_entry = {
            "question": "缺口问题",
            "sources": [{"locator": "docs/evidence/fixture.md"}],
            "related_mechanisms": ["common-monster-dr"],
            "researched": ["已查，未解决"],
            "affected": {"components": ["ignite"], "calculation_blocks": ["more_groups"]},
            "required_deliverable": "handler",
            "verification": ["正向场景"],
        }
        task = research.make_research_task(queue_entry, input_fingerprint=FP_A)
        self.assertEqual(task["deliverable_kind"], "handler")
        self.assertEqual(task["related_mechanism_refs"], ["common-monster-dr"])
        self.assertEqual(task["researched_already"], ["已查，未解决"])
        self.assertEqual(task["verification_required"], ["正向场景"])
        self.assertEqual(task["source_refs"], [{"locator": "docs/evidence/fixture.md"}])

    def test_queue_style_task_payload_roundtrips_with_same_ids(self) -> None:
        # 并行工作流 artifacts.build_research_queue 生成 ResearchTask 形状的条目；
        # make_research_task 必须保留其 task_id/gap_id，避免 queue → task 文件换 ID。
        queue_entry = {
            "schema_version": contract.SCHEMA_RESEARCH_TASK,
            "task_id": "research_task-0123456789abcdef",
            "gap_id": "gap-queue-entry-1",
            "input_fingerprint": FP_A,
            "question": "queue 缺口",
            "source_refs": [{"locator": "docs/evidence/fixture.md"}],
            "related_mechanism_refs": ["common-monster-dr"],
            "researched_already": ["已查"],
            "affected": {"components": ["target_side"], "blocks_target": True},
            "deliverable_kind": "mapping",
            "verification_required": ["正向场景"],
        }
        task = research.make_research_task(queue_entry)
        self.assertEqual(task["task_id"], "research_task-0123456789abcdef")
        self.assertEqual(task["gap_id"], "gap-queue-entry-1")
        self.assertEqual(task["input_fingerprint"], FP_A)
        self.assertEqual(task["affected"]["calculation_blocks"], [])
        self.assertEqual(task["question"], "queue 缺口")
        path = self.workspace.root / "queue-task.json"
        research.write_task_file(path, task)
        self.assertEqual(research.read_task_file(path), task)

    def test_invalid_explicit_task_id_raises(self) -> None:
        with self.assertRaises(contract.ContractError) as caught:
            research.make_research_task(gap_dict(task_id="not-a-task-id"), input_fingerprint=FP_A)
        self.assertIn("$.task_id", str(caught.exception))

    def test_unknown_gap_keys_are_tolerated(self) -> None:
        task = research.make_research_task(
            gap_dict(priority="high", gap_kind="mapping", extra={"x": 1}), input_fingerprint=FP_A
        )
        self.assertEqual(set(task), set(research.TASK_FIELDS))

    def test_invalid_gap_raises_contract_error(self) -> None:
        cases = [
            ({"affected": {"components": ["hit"]}}, "$.question"),
            ({"question": "  ", "affected": {"components": ["hit"]}}, "$.question"),
            ({"question": "Q?"}, "$.affected"),
            ({"question": "Q?", "affected": "not-an-object"}, "$.affected"),
            ({"question": "Q?", "affected": {"blocks_target": True}}, "$.affected"),
            (
                {"question": "Q?", "affected": {"components": ["hit"]}, "deliverable_kind": "magic"},
                "$.deliverable_kind",
            ),
            (
                {"question": "Q?", "affected": {"components": ["hit"]}, "blocks_target": "yes"},
                "blocks_target",
            ),
            (
                {"question": "Q?", "affected": {"components": ["hit"]}, "source_refs": [123]},
                "$.source_refs[0]",
            ),
            (
                {"question": "Q?", "affected": {"components": ["hit"]}, "gap_id": "has space"},
                "$.gap_id",
            ),
        ]
        for gap, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(contract.ContractError) as caught:
                    research.make_research_task(gap, input_fingerprint=FP_A)
                self.assertIn(field, str(caught.exception))

    def test_non_mapping_gap_raises(self) -> None:
        with self.assertRaises(contract.ContractError):
            research.make_research_task("just a question", input_fingerprint=FP_A)

    def test_invalid_fingerprints_raise(self) -> None:
        for fingerprint in ("not-a-sha", "A" * 64, ""):
            with self.subTest(fingerprint=fingerprint):
                with self.assertRaises(contract.ContractError) as caught:
                    research.make_research_task(gap_dict(), input_fingerprint=fingerprint)
                self.assertIn("input_fingerprint", str(caught.exception))
        with self.assertRaises(contract.ContractError) as caught:
            research.make_research_task(gap_dict(input_fingerprint="short"))
        self.assertIn("input_fingerprint", str(caught.exception))

    def test_absolute_paths_in_gap_are_rejected(self) -> None:
        for locator in ("C:\\Users\\x\\evidence.md", "\\\\server\\share\\evidence.md"):
            with self.subTest(locator=locator):
                with self.assertRaises(contract.ContractError) as caught:
                    research.make_research_task(
                        gap_dict(source_refs=[{"path": locator}]), input_fingerprint=FP_A
                    )
                self.assertIn("$", str(caught.exception))

    def test_make_task_does_not_print(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            task = build_task()
            with self.assertRaises(contract.ContractError):
                research.make_research_task({"question": ""}, input_fingerprint=FP_A)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(task["input_fingerprint"], FP_A)


# ---------------------------------------------------------------------------
# 文件交换
# ---------------------------------------------------------------------------


class ResearchFileExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TemporaryWorkspace()
        self.addCleanup(self.workspace.cleanup)
        self.task = build_task()
        self.proposal = build_proposal(self.task)

    def test_task_file_roundtrip(self) -> None:
        path = self.workspace.root / "task.json"
        returned = research.write_task_file(path, self.task)
        self.assertEqual(returned, path)
        self.assertEqual(research.read_task_file(path), self.task)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], contract.SCHEMA_RESEARCH_TASK)
        self.assertNotIn(str(self.workspace.root), path.read_text(encoding="utf-8"))

    def test_task_file_roundtrip_accepts_dataclass(self) -> None:
        path = self.workspace.root / "task-dataclass.json"
        research.write_task_file(path, contract.ResearchTask.from_dict(self.task))
        self.assertEqual(research.read_task_file(path), self.task)

    def test_proposal_file_roundtrip(self) -> None:
        path = self.workspace.root / "proposal.json"
        research.write_proposal_file(path, self.proposal)
        self.assertEqual(research.read_proposal_file(path), self.proposal)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], contract.SCHEMA_RESEARCH_PROPOSAL)

    def test_written_files_are_byte_stable(self) -> None:
        first = self.workspace.root / "a.json"
        second = self.workspace.root / "b.json"
        research.write_task_file(first, self.task)
        research.write_task_file(second, self.task)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_read_rejects_wrong_schema_version(self) -> None:
        path = self.workspace.root / "task.json"
        payload = dict(self.task, schema_version="le-research-task-v0")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(contract.ContractError) as caught:
            research.read_task_file(path)
        self.assertIn("schema_version", str(caught.exception))

        proposal_path = self.workspace.root / "proposal.json"
        payload = dict(self.proposal, schema_version="le-research-proposal-v0")
        proposal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(contract.ContractError):
            research.read_proposal_file(proposal_path)

    def test_read_rejects_broken_or_missing_files(self) -> None:
        broken = self.workspace.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertRaises(contract.ContractError):
            research.read_task_file(broken)
        missing = self.workspace.root / "missing.json"
        with self.assertRaises(contract.ContractError):
            research.read_proposal_file(missing)
        array_file = self.workspace.root / "array.json"
        array_file.write_text("[]", encoding="utf-8")
        with self.assertRaises(contract.ContractError):
            research.read_task_file(array_file)

    def test_read_rejects_absolute_paths(self) -> None:
        path = self.workspace.root / "task.json"
        payload = json.loads(json.dumps(self.task))
        payload["source_refs"] = [{"path": "C:\\Users\\x\\task.md"}]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(contract.ContractError) as caught:
            research.read_task_file(path)
        self.assertIn("$.source_refs[0].path", str(caught.exception))

    def test_write_rejects_incomplete_or_non_portable_proposal(self) -> None:
        with self.assertRaises(contract.ContractError) as caught:
            research.write_proposal_file(
                self.workspace.root / "bad.json",
                {"schema_version": contract.SCHEMA_RESEARCH_PROPOSAL},
            )
        self.assertIn("$.proposal_id", str(caught.exception))
        with self.assertRaises(contract.ContractError) as caught:
            research.write_proposal_file(
                self.workspace.root / "bad2.json",
                build_proposal(self.task, evidence_refs=[{"locator": "C:\\Users\\x\\e.md"}]),
            )
        self.assertIn("绝对路径", str(caught.exception))

    def test_file_exchange_does_not_print(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            research.write_task_file(self.workspace.root / "t.json", self.task)
            research.read_task_file(self.workspace.root / "t.json")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


# ---------------------------------------------------------------------------
# validate_proposal
# ---------------------------------------------------------------------------


class ValidateProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TemporaryWorkspace()
        self.addCleanup(self.workspace.cleanup)
        self.task = build_task()
        self.evidence_path, self.evidence_sha = self.workspace.evidence_file()
        self.proposal = build_proposal(
            self.task,
            evidence_refs=[
                {
                    "kind": "document",
                    "locator": "docs/evidence/fixture.md#L12",
                    "sha256": self.evidence_sha,
                }
            ],
        )

    def _validate(self, proposal=None, **kwargs):
        return research.validate_proposal(
            self.proposal if proposal is None else proposal, task=self.task, **kwargs
        )

    def test_complete_proposed_change_passes(self) -> None:
        result = self._validate(evidence_root=self.workspace.root)
        self.assertEqual(result.kind, "proposal")
        self.assertTrue(result.passed)
        self.assertEqual(result.errors, [])
        self.assertIn("outcome:proposed_change", result.accepted)
        self.assertIn("common-target-resistance-penetration", result.accepted)
        self.assertTrue(result.outcome_effective)
        checks = {check["id"]: check["status"] for check in result.checks}
        self.assertEqual(checks["proposal_contract"], "pass")
        self.assertEqual(checks["task_lookup"], "pass")
        self.assertEqual(checks["fingerprint_consistency"], "pass")
        self.assertEqual(checks["evidence_existence"], "pass")
        self.assertEqual(checks["candidate_completeness"], "pass")
        self.assertEqual(checks["verification_declared"], "pass")
        actions = " ".join(result.next_actions)
        self.assertIn("commit_operation", actions)
        self.assertIn("不等于已发布", actions)

    def test_validation_result_roundtrips(self) -> None:
        result = self._validate(evidence_root=self.workspace.root)
        payload = result.to_dict()
        self.assertEqual(contract.ValidationResult.from_dict(payload).to_dict(), payload)
        self.assertEqual(payload["kind"], "proposal")

    def test_validation_does_not_publish_or_write_files(self) -> None:
        before = self.workspace.list_files()
        result = self._validate(evidence_root=self.workspace.root)
        self.assertTrue(result.passed)
        self.assertEqual(self.workspace.list_files(), before)

    def test_validation_accepts_dataclasses(self) -> None:
        result = research.validate_proposal(
            contract.ResearchProposal.from_dict(self.proposal),
            task=contract.ResearchTask.from_dict(self.task),
            evidence_root=self.workspace.root,
        )
        self.assertTrue(result.passed)

    def test_unresolved_closes_the_round(self) -> None:
        proposal = build_proposal(
            self.task,
            outcome=contract.OUTCOME_UNRESOLVED,
            mechanism_refs=[],
            mapping_refs=[],
            code_change_refs=[],
            evidence_refs=[],
            assumptions=[{"assumption_id": "a1", "status": "unresolved", "note": "证据不足"}],
            verification_refs=[],
        )
        result = self._validate(proposal)
        self.assertTrue(result.passed)
        self.assertEqual(result.accepted, ["outcome:unresolved"])
        self.assertTrue(result.outcome_effective)
        actions = " ".join(result.next_actions)
        self.assertIn("本轮研究结束", actions)
        self.assertIn("不重复调查", actions)

    def test_needs_input_closes_the_round(self) -> None:
        proposal = build_proposal(
            self.task,
            outcome=contract.OUTCOME_NEEDS_INPUT,
            mechanism_refs=[],
            evidence_refs=[],
            verification_refs=[],
        )
        result = self._validate(proposal)
        self.assertTrue(result.passed)
        self.assertEqual(result.accepted, ["outcome:needs_input"])
        self.assertIn("补齐", " ".join(result.next_actions))

    def test_missing_task_is_explicit(self) -> None:
        result = research.validate_proposal(self.proposal)
        self.assertFalse(result.passed)
        self.assertIsNone(result.outcome_effective)
        self.assertEqual(result.accepted, [])
        self.assertEqual([error["code"] for error in result.errors], ["invalid_task_id"])
        self.assertIn("tasks_by_id", result.errors[0]["message"])

    def test_unknown_task_id_in_tasks_by_id(self) -> None:
        result = research.validate_proposal(self.proposal, tasks_by_id={})
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "invalid_task_id")
        self.assertIn("不存在", result.errors[0]["message"])

    def test_explicit_task_id_mismatch(self) -> None:
        wrong_task = build_task(question="另一个问题")
        result = research.validate_proposal(self.proposal, task=wrong_task)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "invalid_task_id")
        self.assertIn("不一致", result.errors[0]["message"])

    def test_tasks_by_id_mapping_and_list_forms(self) -> None:
        for tasks in (
            {self.task["task_id"]: self.task},
            [self.task],
            {self.task["task_id"]: contract.ResearchTask.from_dict(self.task)},
        ):
            with self.subTest(tasks=type(tasks).__name__):
                result = research.validate_proposal(
                    self.proposal, tasks_by_id=tasks, evidence_root=self.workspace.root
                )
                self.assertTrue(result.passed)

    def test_stale_fingerprint_against_task(self) -> None:
        proposal = dict(self.proposal, based_on_fingerprint=FP_B)
        result = self._validate(proposal)
        self.assertFalse(result.passed)
        self.assertIsNone(result.outcome_effective)
        self.assertEqual([error["code"] for error in result.errors], ["stale_proposal"])
        self.assertIn("task.input_fingerprint", result.errors[0]["message"])
        self.assertIn("不得静默套用", result.errors[0]["message"])
        self.assertIn("重新生成 ResearchTask", " ".join(result.next_actions))

    def test_stale_fingerprint_against_current_input(self) -> None:
        result = self._validate(current_fingerprint=FP_B)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "stale_proposal")
        self.assertIn("当前输入指纹", result.errors[0]["message"])

    def test_matching_current_fingerprint_passes(self) -> None:
        result = self._validate(current_fingerprint=FP_A, evidence_root=self.workspace.root)
        self.assertTrue(result.passed)

    def test_incomplete_proposal_returns_structured_errors(self) -> None:
        result = research.validate_proposal(
            {"schema_version": contract.SCHEMA_RESEARCH_PROPOSAL, "task_id": "x"}
        )
        self.assertFalse(result.passed)
        self.assertIsNone(result.outcome_effective)
        fields = {error["details"].get("field") for error in result.errors}
        self.assertIn("$.proposal_id", fields)
        self.assertIn("$.based_on_fingerprint", fields)
        self.assertIn("$.outcome", fields)
        codes = {error["code"] for error in result.errors}
        self.assertIn("incomplete_payload", codes)
        self.assertIn("invalid_fingerprint", codes)

    def test_unknown_schema_version(self) -> None:
        result = research.validate_proposal({"schema_version": "le-research-proposal-v2"})
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "invalid_schema_version")

    def test_invalid_outcome(self) -> None:
        result = research.validate_proposal(dict(self.proposal, outcome="maybe_change"))
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "invalid_outcome")
        self.assertIn("$.outcome", result.errors[0]["message"])

    def test_proposed_change_requires_candidates(self) -> None:
        proposal = dict(self.proposal, mechanism_refs=[], mapping_refs=[], code_change_refs=[])
        result = self._validate(proposal, evidence_root=self.workspace.root)
        self.assertFalse(result.passed)
        self.assertIn("missing_candidates", [error["code"] for error in result.errors])
        self.assertFalse(result.outcome_effective)

    def test_proposed_change_requires_evidence(self) -> None:
        proposal = dict(self.proposal, evidence_refs=[])
        result = self._validate(proposal)
        self.assertFalse(result.passed)
        self.assertIn("missing_evidence", [error["code"] for error in result.errors])

    def test_proposed_change_requires_verification(self) -> None:
        proposal = dict(self.proposal, verification_refs=[])
        result = self._validate(proposal, evidence_root=self.workspace.root)
        self.assertFalse(result.passed)
        self.assertIn("missing_verification", [error["code"] for error in result.errors])

    def test_missing_evidence_file_fails(self) -> None:
        proposal = dict(
            self.proposal,
            evidence_refs=[{"kind": "document", "locator": "docs/evidence/missing.md#L1"}],
        )
        result = self._validate(proposal, evidence_root=self.workspace.root)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "evidence_missing")
        self.assertEqual(result.errors[0]["details"]["field"], "$.evidence_refs[0].locator")

    def test_evidence_sha_mismatch_fails(self) -> None:
        proposal = dict(
            self.proposal,
            evidence_refs=[
                {"kind": "document", "locator": "docs/evidence/fixture.md", "sha256": "0" * 64}
            ],
        )
        result = self._validate(proposal, evidence_root=self.workspace.root)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "evidence_sha_mismatch")

    def test_evidence_existence_is_skipped_without_root(self) -> None:
        proposal = dict(
            self.proposal,
            evidence_refs=[
                {"kind": "dataset", "locator": "data/releases/fixture/data.sqlite#ailments"}
            ],
        )
        result = self._validate(proposal)
        self.assertTrue(result.passed)
        evidence_check = next(
            check for check in result.checks if check["id"] == "evidence_existence"
        )
        self.assertEqual(evidence_check["status"], "skip")

    def test_non_portable_evidence_path_fails_contract(self) -> None:
        proposal = dict(
            self.proposal,
            evidence_refs=[{"kind": "document", "locator": "C:\\Users\\x\\evidence.md"}],
        )
        result = self._validate(proposal, evidence_root=self.workspace.root)
        self.assertFalse(result.passed)
        codes = {error["code"] for error in result.errors}
        self.assertIn("non_portable_path", codes)

    def test_assumption_contract_errors(self) -> None:
        cases = [
            ([{"note": "缺少 id"}], "$.assumptions[0].assumption_id"),
            ([{"assumption_id": "a1", "status": "maybe"}], "$.assumptions[0].status"),
            (
                [{"assumption_id": "a1"}, {"assumption_id": "a1"}],
                "$.assumptions[1].assumption_id",
            ),
        ]
        for assumptions, field in cases:
            with self.subTest(field=field):
                proposal = dict(self.proposal, assumptions=assumptions)
                result = self._validate(proposal)
                self.assertFalse(result.passed)
                fields = {error["details"].get("field") for error in result.errors}
                self.assertIn(field, fields)

    def test_validate_does_not_print(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self._validate(evidence_root=self.workspace.root)
            research.validate_proposal({"schema_version": "wrong"})
            research.validate_proposal(self.proposal, task=self.task, current_fingerprint=FP_B)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_validate_never_imports_or_executes_proposal_text(self) -> None:
        # 研究文本只作为字符串保存；校验不产生任何可执行副作用（无文件写入、无发布）。
        proposal = dict(
            self.proposal,
            code_change_refs=["definitely-not-a-module:exec_me()"],
        )
        before = self.workspace.list_files()
        result = self._validate(proposal, evidence_root=self.workspace.root)
        self.assertTrue(result.passed)
        self.assertEqual(self.workspace.list_files(), before)
        self.assertIn("outcome:proposed_change", result.accepted)


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


class ResearchSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task_schema = load_schema("research-task-v1.json")
        self.proposal_schema = load_schema("research-proposal-v1.json")
        self.task = build_task()
        self.proposal = build_proposal(self.task)

    def test_schema_required_fields_match_contract_fixtures(self) -> None:
        self.assertEqual(set(self.task_schema["required"]), set(research.TASK_FIELDS))
        self.assertEqual(set(self.proposal_schema["required"]), set(research.PROPOSAL_FIELDS))
        self.assertEqual(
            set(self.task_schema["properties"]["deliverable_kind"]["enum"]),
            set(research.DELIVERABLE_KINDS),
        )
        self.assertEqual(
            set(self.proposal_schema["properties"]["outcome"]["enum"]),
            set(contract.OUTCOMES),
        )

    def test_generated_task_validates_against_schema(self) -> None:
        self.assertEqual(schema_errors(self.task, self.task_schema), [])

    def test_generated_proposal_validates_against_schema(self) -> None:
        proposal = contract.ResearchProposal.from_dict(self.proposal).to_dict()
        self.assertEqual(schema_errors(proposal, self.proposal_schema), [])

    def test_task_schema_rejects_invalid_payloads(self) -> None:
        broken = json.loads(json.dumps(self.task))
        broken["deliverable_kind"] = "magic"
        broken["input_fingerprint"] = "short"
        errors = "\n".join(schema_errors(broken, self.task_schema))
        self.assertIn("deliverable_kind", errors)
        self.assertIn("input_fingerprint", errors)
        missing = dict(self.task)
        del missing["affected"]
        self.assertIn(
            "affected", "\n".join(schema_errors(missing, self.task_schema))
        )

    def test_proposal_schema_requires_evidence_locator(self) -> None:
        proposal = contract.ResearchProposal.from_dict(self.proposal).to_dict()
        proposal["evidence_refs"] = [{"kind": "document", "note": "no locator"}]
        errors = "\n".join(schema_errors(proposal, self.proposal_schema))
        self.assertIn("anyOf", errors)

    def test_schema_files_are_draft_2020_12(self) -> None:
        for schema in (self.task_schema, self.proposal_schema):
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            self.assertIn("$id", schema)
            self.assertTrue(schema["description"])


if __name__ == "__main__":
    unittest.main()
