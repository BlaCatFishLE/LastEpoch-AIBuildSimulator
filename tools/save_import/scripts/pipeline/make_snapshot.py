# -*- coding: utf-8 -*-
r"""Build the phase-3 input snapshot for one case's calculation input.

Promoted from ``.scratch/save-import/make_phase3_snapshot_affix_db.py``; this is
the formal snapshot step of the chain behind ``scripts/le.ps1 reproduce``.

Why a "pinned expectations" step exists
---------------------------------------
``le_calc.inputs.load_bundle()`` validates the calculator export against module
level ``EXPECTED_*`` constants (the phase-2 fixpoint: 29 items / 148 mods / …).
The save-import calculation input is a different, case-specific export: it has
29 items but 147 mods (146 imported rows + 1 quest reward mod), so an
*unmodified* ``load_bundle()`` correctly rejects it.  The legacy prototypes
therefore monkey-patched ``le_calc.inputs.EXPECTED_*`` with the counts read from
the file — which silently disables the completeness check.

This script replaces that with a **case-scoped, data-driven contract**:

1. the case configuration declares the expected counts
   (``snapshot_expectations``: items, mods, passive_nodes, passive_points,
   specializations, bh2_points_total);
2. the actual calculation input is compared with the declared counts *first* —
   any mismatch is a fatal error naming the differing keys;
3. only then, for the duration of ``load_bundle()``, the ``le_calc.inputs``
   module constants are pinned to the values the case *declared* (never to the
   values read back from the file), and every pinned value is printed;
4. the constants are restored in a ``finally`` block, so other cases and the
   test suites always see the module's documented defaults.

Known limitation (deliberate; recorded in the case directory's README when one exists):
pinning mutates module-level constants of ``le_calc.inputs`` for the duration of
one call.  A future clean fix is to let ``load_bundle()`` accept per-call
expected counts; that change belongs to ``tools/phase3/src`` and is out of scope
for the save-import pipeline promotion.  Validation is **not** weakened: the
declared counts are asserted against the file before pinning.

No wall-clock data is written; the snapshot is deterministic for a given input.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    PipelineError,
    calculation_input_counts,
    case_counts,
    case_path,
    load_case,
    load_json,
    print_step,
    relative,
    repo_root,
    resolve,
    write_json,
)

#: mapping of ``le_calc.inputs`` module constant -> case expectation key
PINNED_CONSTANTS = {
    "EXPECTED_ITEM_COUNT": "items",
    "EXPECTED_MOD_COUNT": "mods",
    "EXPECTED_PASSIVE_NODES": "passive_nodes",
    "EXPECTED_PASSIVE_POINTS": "passive_points",
    "EXPECTED_SPECIALIZATIONS": "specializations",
}


def _import_le_calc(root: Path):
    phase3_src = root / "tools" / "phase3" / "src"
    if str(phase3_src) not in sys.path:
        sys.path.insert(0, str(phase3_src))
    try:
        from le_calc import inputs as calc_inputs  # noqa: WPS433 (local import by design)
        from le_calc.stats import build_snapshot
    except ImportError as exc:  # pragma: no cover - environment problem
        raise PipelineError(
            f"cannot import le_calc from {phase3_src}: {exc}; run with the repository "
            ".venv and PYTHONPATH=tools/save_import/src (le.ps1 does this)"
        ) from exc
    return calc_inputs, build_snapshot


@contextmanager
def pinned_expectations(calc_inputs, expectations: dict[str, int]) -> Iterator[None]:
    """Temporarily pin ``le_calc.inputs.EXPECTED_*`` to the case-declared counts.

    The actual input has already been checked against these same values, so the
    pin re-anchors the validator to the case contract instead of bypassing it.
    """
    saved = {name: getattr(calc_inputs, name) for name in PINNED_CONSTANTS}
    pinned = {name: int(expectations[key]) for name, key in PINNED_CONSTANTS.items()}
    try:
        for name, value in pinned.items():
            setattr(calc_inputs, name, value)
        print_step(
            "pinned le_calc.inputs EXPECTED_* to the case-declared counts: "
            + ", ".join(f"{name}={value}" for name, value in pinned.items())
        )
        yield
    finally:
        for name, value in saved.items():
            setattr(calc_inputs, name, value)


def _semantic_snapshot_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Projection used to compare a rebuilt snapshot with a shipped one.

    Paths, hashes and provenance strings legitimately differ between a run
    directory and the archived sample input, so they are excluded; the numeric
    and categorical content must not.
    """
    return {
        "character": snapshot.get("character"),
        "attributes": snapshot.get("attributes"),
        "resistances": snapshot.get("resistances"),
        "derived": snapshot.get("derived"),
        "contributions": [
            {
                key: row.get(key)
                for key in (
                    "contribution_id",
                    "stat_key",
                    "operation",
                    "value",
                    "unit",
                    "target",
                    "condition_status",
                    "included",
                    "value_source",
                    "status",
                )
            }
            for row in snapshot.get("contributions") or []
        ],
        "issues": snapshot.get("issues"),
        "excluded_sources": snapshot.get("excluded_sources"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_snapshot.py",
        description="Build the phase-3 input snapshot for a case calculation input.",
    )
    parser.add_argument("--case", default=None, help="case configuration (le-case-v1)")
    parser.add_argument("--root", default=None, help="project root override")
    parser.add_argument("--build-dir", default=None, help="build directory override")
    parser.add_argument("--calc-input", default=None, help="calculation-input JSON override")
    parser.add_argument(
        "--expectations",
        default=None,
        help="JSON file with expected counts (default: case snapshot_expectations)",
    )
    parser.add_argument("--out", required=True, help="output snapshot JSON")
    parser.add_argument(
        "--compare",
        default=None,
        help="optional shipped snapshot to compare against (semantic projection)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolve(Path.cwd(), args.root) if args.root else repo_root()
        case = load_case(args.case, root=root) if args.case else None
        if case is not None:
            build_dir = resolve(root, args.build_dir) if args.build_dir else case_path(
                case, "build.build_dir"
            )
            calc_input = resolve(root, args.calc_input) if args.calc_input else case_path(
                case, "inputs.calculation_input"
            )
            expectations = (
                load_json(resolve(root, args.expectations), what="expectations file")
                if args.expectations
                else case_counts(case)
            )
        else:
            if not args.build_dir or not args.calc_input or not args.expectations:
                raise PipelineError(
                    "without --case pass --build-dir, --calc-input and --expectations"
                )
            build_dir = resolve(root, args.build_dir)
            calc_input = resolve(root, args.calc_input)
            expectations = load_json(resolve(root, args.expectations), what="expectations file")
        expectations = {key: int(value) for key, value in expectations.items()}
        document = load_json(calc_input, what="calculation input")
        actual = calculation_input_counts(document)
        mismatches = {
            key: {"expected": expectations.get(key), "actual": actual.get(key)}
            for key in expectations
            if key in actual and expectations.get(key) != actual.get(key)
        }
        if mismatches:
            details = "; ".join(
                f"{key}: expected {value['expected']}, got {value['actual']}"
                for key, value in sorted(mismatches.items())
            )
            raise PipelineError(
                f"calculation input does not match the case's snapshot expectations ({details})"
            )
        print_step(f"build dir: {relative(build_dir, root)}")
        print_step(f"calculation input: {relative(calc_input, root)}")
        print_step(
            "verified case expectations: "
            + ", ".join(f"{key}={actual[key]}" for key in sorted(expectations))
        )

        calc_inputs, build_snapshot = _import_le_calc(root)
        with pinned_expectations(calc_inputs, expectations):
            bundle = calc_inputs.load_bundle(
                root=root,
                build_dir=build_dir,
                calc_input_path=calc_input,
            )
            snapshot = build_snapshot(bundle)
        snapshot_dict = snapshot.to_dict()
        out = resolve(root, args.out)
        write_json(out, snapshot_dict)
        print_step(
            f"wrote {relative(out, root)} (contributions={len(snapshot_dict.get('contributions') or [])}, "
            f"issues={len(snapshot_dict.get('issues') or [])}, "
            f"excluded_sources={len(snapshot_dict.get('excluded_sources') or [])})"
        )

        if args.compare:
            shipped = load_json(resolve(root, args.compare), what="comparison snapshot")
            if _semantic_snapshot_view(snapshot_dict) != _semantic_snapshot_view(shipped):
                raise PipelineError(
                    "rebuilt snapshot differs from the shipped snapshot beyond paths/hashes: "
                    f"{relative(resolve(root, args.compare), root)}"
                )
            print_step("rebuilt snapshot matches the shipped snapshot (semantic projection)")
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
