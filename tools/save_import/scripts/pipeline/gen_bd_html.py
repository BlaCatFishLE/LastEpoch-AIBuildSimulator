# -*- coding: utf-8 -*-
r"""Generate the player-readable BD HTML view with the corrected item values.

Promoted from ``.scratch/save-import/gen_bd_html.py``.  The phase-2 viewer is
BuildSpec-based; the corrected per-base affix values live in the case's
``bd.json``.  This script overlays those values on the BuildSpec **in memory**
before rendering, so the HTML matches the corrected local BD document while the
stored BuildSpec revision is not modified.

Nothing here is sample specific: the build revision and the ``bd.json`` document
come from the case configuration (or explicit CLI arguments), and output goes to
the caller's run directory.

Usage (from anywhere; ``--case`` supplies the defaults)::

    & .\.venv\Scripts\python.exe tools\save_import\scripts\pipeline\gen_bd_html.py \
        --case examples\<case>\case.json --out reports\<case>\<run-id>\bd-view.html
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    PipelineError,
    case_path,
    load_case,
    portable_text,
    print_step,
    relative,
    repo_root,
    resolve,
)


def _import_viewer(root: Path):
    src = root / "tools" / "build_import" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from le_build.viewer import render_build_html  # noqa: WPS433 (local by design)
    except ImportError as exc:  # pragma: no cover - environment problem
        raise PipelineError(
            f"cannot import le_build.viewer from {src}: {exc}; run with the repository .venv"
        ) from exc
    return render_build_html


def _corrected_rows(bd_item: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for affix in bd_item.get("affixes") or []:
        rows = affix.get("effective_ranges") or []
        if rows:
            out[int(affix["affix_id"])] = {
                "rows": rows,
                "value": (affix.get("affix_db") or {}).get("effective_value"),
            }
    return out


def _display(info: dict) -> tuple[float, str]:
    rows = info.get("rows") or []
    value = info.get("value")
    unit = str((rows[0] if rows else {}).get("display_unit") or "")
    if value is None:
        value = (rows[0].get("display_max") if rows else 0) / (100.0 if unit == "%" else 1.0)
    value = float(value)
    if unit == "%":
        return value, f"{value * 100:g}%"
    return value, f"{value:g}"


def overlay(spec: dict, bd: dict) -> None:
    """Overlay corrected ``bd.json`` affix values on the BuildSpec in memory."""
    for slot in spec["equipment"]["slots"]:
        slot_name = slot.get("slot")
        bd_item = (bd.get("equipment") or {}).get(slot_name)
        if not bd_item:
            continue
        rows_by_affix = _corrected_rows(bd_item)
        if not rows_by_affix:
            continue
        instance = slot["instance"]
        # 1) structured affix values (Viewer fallback / raw JSON)
        for channel, entries in (instance.get("affixes") or {}).items():
            for entry in entries:
                codec = entry.get("codec") or {}
                affix_id = codec.get("affix_id")
                if affix_id is None:
                    affix_id = (entry.get("affix") or {}).get("source_id")
                try:
                    affix_id = int(affix_id)
                except (TypeError, ValueError):
                    continue
                info = rows_by_affix.get(affix_id)
                if not info:
                    continue
                rows = info.get("rows") or []
                if not rows:
                    continue
                values = ((entry.get("value") or {}).get("values")) or []
                for index, value in enumerate(values):
                    row = rows[min(index, len(rows) - 1)]
                    numeric, display = _display({"rows": [row], "value": info.get("value")})
                    value["value_numeric"] = numeric
                    value["display_value"] = display
                    value["roll_min"] = row.get("display_min")
                    value["roll_max"] = row.get("display_max")
                tier = entry.get("tier")
                if isinstance(tier, dict):
                    tier["roll_min"] = rows[0].get("display_min")
                    tier["roll_max"] = rows[0].get("display_max")
        # 2) player-readable lines
        readable = instance.get("readable") or {}
        for line in readable.get("lines") or []:
            if not str(line.get("section") or "").startswith("affix"):
                continue
            diagnostics = line.get("diagnostics") or {}
            entity = str(diagnostics.get("affix_entity_id") or "")
            try:
                affix_id = int(entity.rsplit(":", 1)[-1])
            except (TypeError, ValueError):
                continue
            info = rows_by_affix.get(affix_id)
            if not info:
                continue
            rows = info.get("rows") or []
            if not rows:
                continue
            try:
                index = int(diagnostics.get("affix_index") or 0)
            except (TypeError, ValueError):
                index = 0
            row = rows[min(index, len(rows) - 1)]
            numeric, display = _display({"rows": [row], "value": info.get("value")})
            old = str(line.get("value_text") or "")
            text = str(line.get("text") or "")
            if old and old in text:
                text = text.replace(old, display, 1)
            elif text:
                # no exact old token: replace the first numeric percentage/token
                text = re.sub(r"\d+(?:\.\d+)?%", display, text, count=1)
            line["text"] = text
            line["value_text"] = display
            line["range_text"] = display
            diagnostics["value_numeric"] = numeric


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_bd_html.py",
        description="Render the player-readable BD HTML view (corrected bd.json overlay).",
    )
    parser.add_argument("--case", default=None, help="case configuration (le-case-v1)")
    parser.add_argument("--root", default=None, help="project root override")
    parser.add_argument("--build-dir", default=None, help="build directory override")
    parser.add_argument("--revision-id", default=None, help="revision id override")
    parser.add_argument("--bd", default=None, help="bd.json override")
    parser.add_argument("--out", required=True, help="output HTML file")
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
            revision_id = args.revision_id or str(case["build"]["revision_id"])
            bd_path = resolve(root, args.bd) if args.bd else case_path(case, "inputs.bd")
        else:
            if not args.build_dir or not args.revision_id or not args.bd:
                raise PipelineError(
                    "without --case pass --build-dir, --revision-id and --bd"
                )
            build_dir = resolve(root, args.build_dir)
            revision_id = args.revision_id
            bd_path = resolve(root, args.bd)
        revision_dir = build_dir / "revisions" / revision_id
        build_json = revision_dir / "build.json"
        if not build_json.is_file():
            raise PipelineError(f"build revision not found: {build_json}")
        render_build_html = _import_viewer(root)
        print_step(f"build revision: {relative(revision_dir, root)}")
        print_step(f"bd.json: {relative(bd_path, root)}")
        spec = json.loads(build_json.read_text(encoding="utf-8"))
        bd = json.loads(bd_path.read_text(encoding="utf-8"))
        overlay(spec, bd)
        html = render_build_html(spec, revision_id=revision_id, build_dir=revision_dir)
        # The viewer may embed paths from the frozen import-report.json; this is a
        # derived HTML artefact, so normalise machine paths to keep it portable.
        html, scrubbed = portable_text(html, root)
        if scrubbed:
            print_step(f"normalised {scrubbed} machine path(s) for portability")
        out = resolve(root, args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8", newline="\n")
        print_step(f"wrote {relative(out, root)} ({len(html)} chars)")
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
