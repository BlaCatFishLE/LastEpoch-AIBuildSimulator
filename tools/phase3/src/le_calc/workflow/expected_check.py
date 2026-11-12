"""Shared expected-summary comparison (``le-workflow-v1`` / P3).

Single implementation used by:

* ``scripts/le_commands.py`` — ``reproduce`` / ``run`` expected checks;
* ``le_calc.workflow.domain.validate_result`` — the ``expected`` section.

It mirrors the pre-P3 semantics exactly (check names, JSON pointers, relative
tolerance, ``run_expectations`` for both models, BH generated-scenario
comparison ignoring the binding block), so refactoring must not change the
``reproduce`` output (BH 82 / FR 40 items).

The function returns plain JSON-safe check dicts plus an ``ok`` flag::

    {"name": ..., "status": "PASS"|"FAIL"|"INFO",
     "detail": ..., "scope": "expected_check"|"run_expectation"|"conditional_scenario",
     "expected": ..., "actual": ..., "tolerance": ...}

Rule/evidence owners: the expected file itself is a frozen acceptance baseline;
this module only compares produced artefacts against it (it never writes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

DEFAULT_ROOT = Path(__file__).resolve().parents[5]

MODEL_BLACK_HOLE = "black-hole"
MODEL_HIT = "hit"


class ExpectedCheckError(ValueError):
    """Raised when the expected summary / run directory cannot be read."""


def _root(root: Optional[str | Path]) -> Path:
    return Path(root).resolve() if root is not None else DEFAULT_ROOT


def _read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExpectedCheckError(f"缺少{what}：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ExpectedCheckError(f"{what}不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExpectedCheckError(f"{what}必须是 JSON 对象：{path}")
    return payload


def _case_model(case: Mapping[str, Any]) -> str:
    return str(case.get("model") or MODEL_BLACK_HOLE).strip().lower()


def _case_path(case: Mapping[str, Any], dotted: str, root: Path) -> Optional[Path]:
    node: Any = case
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, (str, Path)) or not str(node):
        return None
    path = Path(str(node))
    return path if path.is_absolute() else (root / path)


def load_expected(case: Mapping[str, Any], *, root: Optional[str | Path] = None) -> dict[str, Any]:
    root_path = _root(root)
    expected_path = _case_path(case, "expected", root_path)
    if expected_path is None or not expected_path.is_file():
        raise ExpectedCheckError("案例未声明 expected 或文件不存在")
    expected = _read_json(expected_path, "expected summary")
    if expected.get("schema") != "le-case-expected-v1":
        raise ExpectedCheckError(f"expected summary schema 不正确：{expected.get('schema')!r}")
    return expected


def json_pointer(document: Any, pointer: str) -> Any:
    node = document
    for raw in [part for part in str(pointer).split("/") if part]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def rel_error(actual: float, expected: float) -> float:
    if expected == 0:
        return abs(actual - expected)
    return abs(actual - expected) / abs(expected)


def checks_ok(checks: Sequence[Mapping[str, Any]]) -> bool:
    return all(str(check.get("status")) in ("PASS", "INFO", "SKIP") for check in checks)


def compare_expected(
    case: Mapping[str, Any],
    run_dir: str | Path,
    *,
    root: Optional[str | Path] = None,
    expected: Optional[Mapping[str, Any]] = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Compare a produced run directory against the case's expected summary."""

    root_path = _root(root)
    run_path = Path(run_dir)
    if not run_path.is_absolute():
        run_path = root_path / run_path
    if not run_path.is_dir():
        raise ExpectedCheckError(f"运行目录不存在：{run_path}")
    expected_doc = dict(expected) if expected is not None else load_expected(case, root=root_path)
    tolerance = float(expected_doc.get("tolerance_rel") or 0.0)
    checks: list[dict[str, Any]] = []
    cache: dict[str, dict[str, Any]] = {}

    def document(name: str) -> dict[str, Any]:
        if name not in cache:
            cache[name] = _read_json(run_path / name, name)
        return cache[name]

    def add(name: str, ok: bool, detail: str, scope: str,
            expected_value: Any = None, actual: Any = None) -> None:
        checks.append(
            {
                "name": name,
                "status": "PASS" if ok else "FAIL",
                "detail": detail,
                "scope": scope,
                "expected": expected_value,
                "actual": actual,
                "tolerance": tolerance,
            }
        )

    # ---- per-file JSON-pointer checks ---------------------------------
    for spec in expected_doc.get("checks") or []:
        name = str(spec.get("name"))
        try:
            actual = json_pointer(document(str(spec["file"])), str(spec["pointer"]))
        except (ExpectedCheckError, KeyError, IndexError, ValueError) as exc:
            add(name, False, f"无法读取 {spec.get('file')}#{spec.get('pointer')}: {exc}",
                "expected_check", spec.get("expected"), None)
            continue
        expected_value = spec.get("expected")
        if isinstance(actual, bool) or isinstance(expected_value, bool):
            ok = actual == expected_value
            detail = f"实际 {actual!r} / 预期 {expected_value!r}"
        elif isinstance(actual, (int, float)):
            ok = rel_error(float(actual), float(expected_value)) <= tolerance
            detail = (
                f"实际 {actual!r} / 预期 {expected_value!r} / "
                f"相对误差 {rel_error(float(actual), float(expected_value)):.3e}"
            )
        else:
            ok = json.dumps(actual, ensure_ascii=False, sort_keys=True) == json.dumps(
                expected_value, ensure_ascii=False, sort_keys=True
            )
            detail = f"实际 {actual!r} / 预期 {expected_value!r}"
        add(name, ok, detail, "expected_check", expected_value, actual)

    result = document("result.json")
    model = _case_model(case)
    run_expect = expected_doc.get("run_expectations") or {}

    def expect(name: str, actual: Any, target: Any) -> None:
        add(name, actual == target, f"实际 {actual!r} / 预期 {target!r}",
            "run_expectation", target, actual)

    if model == MODEL_HIT:
        target_block = ((result.get("single_hit") or {}).get("totals") or {}).get("target") or {}
        expect("run.status", (result.get("run") or {}).get("status"), run_expect.get("status"))
        expect(
            "run.ruleset.id",
            ((result.get("run") or {}).get("ruleset") or {}).get("id"),
            run_expect.get("ruleset_id"),
        )
        expect(
            "consistency.all_pass",
            bool((result.get("consistency") or {}).get("all_pass")),
            run_expect.get("consistency_all_pass"),
        )
        expect("target.monster_level", target_block.get("monster_level"),
               run_expect.get("monster_level"))
        expect("hits_per_cast.per_cast", (result.get("hits_per_cast") or {}).get("per_cast"),
               run_expect.get("hits_per_cast"))
        expect(
            "hits_per_cast.effective_hits_per_cast",
            (result.get("hits_per_cast") or {}).get("effective_hits_per_cast"),
            run_expect.get("effective_hits_per_cast"),
        )
        expect("dps.policy.id", ((result.get("dps") or {}).get("policy") or {}).get("id"),
               run_expect.get("dps_policy"))
        return checks, checks_ok(checks)

    # ---- Black Hole: conditional metadata + generated scenario --------
    try:
        recompute = document("conditional-recompute.json")
    except ExpectedCheckError as exc:
        add("conditional-recompute.json", False, str(exc), "run_expectation", None, None)
        return checks, checks_ok(checks)
    conditional = result.get("conditional") or {}
    expect("run.status", (result.get("run") or {}).get("status"), run_expect.get("status"))
    expect(
        "run.ruleset.id",
        ((result.get("run") or {}).get("ruleset") or {}).get("id"),
        run_expect.get("ruleset_id"),
    )
    expect(
        "consistency.all_pass",
        bool((result.get("consistency") or {}).get("all_pass")),
        run_expect.get("consistency_all_pass"),
    )
    expect("conditional.context_status", conditional.get("status"),
           run_expect.get("conditional_context_status"))
    expect("conditional.recompute_status", (conditional.get("recompute") or {}).get("status"),
           run_expect.get("conditional_status"))
    expect("conditional.baseline_profile", recompute.get("baseline_profile"),
           run_expect.get("conditional_baseline_profile"))
    expected_applied = run_expect.get("conditional_applied_effects")
    applied = len(((recompute.get("condition_application") or {}).get("applied")) or [])
    expect("conditional.applied_effects", applied, expected_applied)

    shipped_path = _case_path(case, "inputs.scenario", root_path)
    generated_scenario = run_path / "conditional-scenario.json"
    if shipped_path is not None and shipped_path.is_file() and generated_scenario.is_file():
        shipped = _read_json(shipped_path, "case scenario")
        generated = _read_json(generated_scenario, "generated scenario")
        keys = ("schema", "preset_id", "mode", "states", "effects", "single_cast",
                "loop", "target", "baseline_profile")
        left = {key: shipped.get(key) for key in keys}
        right = {key: generated.get(key) for key in keys}
        same = json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(
            right, ensure_ascii=False, sort_keys=True
        )
        add(
            "conditional-scenario（忽略 binding）",
            same,
            "状态/效果/目标条件与案例一致" if same else "生成场景与案例不一致",
            "conditional_scenario",
            left,
            right,
        )
    return checks, checks_ok(checks)


__all__ = [
    "DEFAULT_ROOT",
    "ExpectedCheckError",
    "checks_ok",
    "compare_expected",
    "json_pointer",
    "load_expected",
    "rel_error",
]
