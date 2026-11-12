"""Render the local BD HTML from ``bd.json`` + the phase-2 BuildSpec.

The phase-2 viewer is BuildSpec-based; the corrected per-base affix values live in
``bd.json``.  This script overlays those values on the BuildSpec **in memory** and
renders the familiar ``save-build-view.html`` (including the manual idol-refraction
panel) without modifying the stored revision.

Usage (from repo root; every input is explicit — the public release ships no
case, build revision or save)::

    python tools/save_import/scripts/render_bd_html.py \
        --bd <case>/inputs/bd.json \
        --build-dir data/builds/<tool>/<share>/revisions/<rev> \
        --out reports/<case>/<run-id>/save-build-view.html
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


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


def render(spec: dict, bd: dict) -> dict:
    for slot in spec.get("equipment", {}).get("slots", []):
        slot_name = slot.get("slot")
        bd_item = (bd.get("equipment") or {}).get(slot_name)
        if not bd_item:
            continue
        rows_by_affix = _corrected_rows(bd_item)
        if not rows_by_affix:
            continue
        instance = slot["instance"]
        for channel, entries in (instance.get("affixes") or {}).items():
            for entry in entries:
                codec = entry.get("codec") or {}
                aid = codec.get("affix_id")
                if aid is None:
                    aid = (entry.get("affix") or {}).get("source_id")
                try:
                    aid = int(aid)
                except (TypeError, ValueError):
                    continue
                info = rows_by_affix.get(aid)
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

        readable = instance.get("readable") or {}
        for line in readable.get("lines") or []:
            if not str(line.get("section") or "").startswith("affix"):
                continue
            diag = line.get("diagnostics") or {}
            entity = str(diag.get("affix_entity_id") or "")
            try:
                aid = int(entity.rsplit(":", 1)[-1])
            except (TypeError, ValueError):
                continue
            info = rows_by_affix.get(aid)
            if not info:
                continue
            rows = info.get("rows") or []
            if not rows:
                continue
            try:
                idx = int(diag.get("affix_index") or 0)
            except (TypeError, ValueError):
                idx = 0
            row = rows[min(idx, len(rows) - 1)]
            numeric, display = _display({"rows": [row], "value": info.get("value")})
            old = str(line.get("value_text") or "")
            text = str(line.get("text") or "")
            if old and old in text:
                text = text.replace(old, display, 1)
            elif text:
                text = re.sub(r"\d+(?:\.\d+)?%", display, text, count=1)
            line["text"] = text
            line["value_text"] = display
            line["range_text"] = display
            diag["value_numeric"] = numeric
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bd", type=Path, required=True,
                        help="case bd.json with corrected per-base affix values")
    parser.add_argument("--build-dir", type=Path, required=True,
                        help="published BuildSpec revision directory (contains build.json)")
    parser.add_argument("--out", type=Path, required=True,
                        help="output HTML path (e.g. reports/<case>/<run-id>/save-build-view.html)")
    args = parser.parse_args(argv)

    build_dir = args.build_dir
    if not (build_dir / "build.json").is_file():
        parser.error(f"build revision not found: {build_dir / 'build.json'}")
    sys.path.insert(0, str(ROOT / "tools" / "build_import" / "src"))
    from le_build.viewer import render_build_html  # noqa: E402

    spec = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    bd = json.loads(args.bd.read_text(encoding="utf-8"))
    spec = render(spec, bd)
    html = render_build_html(spec, revision_id=build_dir.name, build_dir=build_dir)
    args.out.write_text(html, encoding="utf-8")
    print(args.out, len(html))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
