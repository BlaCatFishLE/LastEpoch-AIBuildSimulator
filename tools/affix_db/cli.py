"""Command line interface for the affix database toolkit.

    python -m affix_db build        # offline: snapshot -> normalized DB
    python -m affix_db query        # offline: look up effective ranges
    python -m affix_db validate     # offline: schema/formula/known-value checks
    python -m affix_db fetch        # network: refresh the frozen snapshot
    python -m affix_db render-pages # network + Chrome: post-hydration DOM evidence
    python -m affix_db verify-display
    python -m affix_db crosscheck-game
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .build import build
from .crosscheck import crosscheck_against_game
from .fetch import DEFAULT_BASE_URL, DEFAULT_PAGE_SLUGS, fetch_snapshot
from .query import (
    DEFAULT_DB,
    QueryError,
    all_bases,
    connect,
    format_range_value,
    format_rows,
    get_affix,
    get_base_type,
    query,
    query_to_json,
    dumps,
)
from .render_pages import render_pages
from .validate import report_to_text, validate_db
from .verify_display import verify_rendered_pages

#: Repository root derived from this file, so every default path below works
#: from any current directory (``python tools/affix_db/run.py validate``).
REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_path(relative: str) -> Path:
    """Repository-root relative default path (independent of the CWD)."""
    return REPO_ROOT / relative


def _default_db() -> Path:
    return _repo_path(str(DEFAULT_DB))


def _cmd_fetch(args: argparse.Namespace) -> int:
    slugs = tuple(args.slugs) if args.slugs else DEFAULT_PAGE_SLUGS
    inventory = fetch_snapshot(
        args.out,
        base_url=args.base_url,
        page_slugs=slugs,
        timeout=args.timeout,
        progress=print,
    )
    print(json.dumps(inventory["summary"], indent=2))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    result = build(args.snapshot, args.out, progress=print)
    print(json.dumps(result.manifest["counts"], indent=2))
    if result.manifest["integrity_problems"]:
        print("INTEGRITY PROBLEMS:", result.manifest["integrity_problems"], file=sys.stderr)
        return 1
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    connection = connect(args.db)
    try:
        affix_row = get_affix(connection, args.affix)
        if args.omen_sub_base is not None:
            from .query import query_omen_path

            omen = query_omen_path(connection, affix_row["affix_id"], args.omen_sub_base)
            if args.json:
                print(
                    dumps(
                        {
                            "affix": {"affix_id": affix_row["affix_id"], "slug": affix_row["slug"], "name": affix_row["name"]},
                            "omen_special_path": omen["rows"],
                        }
                    )
                )
                return 0
            print(
                f"# {affix_row['name']} (affixId {affix_row['affix_id']}) -- Omen idol special path "
                "[DERIVED, source-level eligibility unresolved]"
            )
            for row in omen["rows"]:
                print(
                    f"  {row['sub_base_name']} (baseTypeID {row['base_type_id']} / subTypeID {row['sub_type_id']}) "
                    f"T{row['tier']} slot{row['roll_slot']}  raw={row['raw_min']:g}-{row['raw_max']:g}  "
                    f"omenAffixEffectModifier={row['omen_affix_effect_modifier']} modifier={row['modifier']:g}  "
                    f"effective={row['effective_min_decimal']}..{row['effective_max_decimal']}  "
                    f"game={format_range_value(row, 'game')}  tunklab={format_range_value(row, 'tunklab')}"
                )
            if omen["rows"]:
                print(f"  eligibility_status: {omen['rows'][0]['eligibility_status']}")
                print(f"  evidence: {omen['rows'][0]['evidence']}")
            else:
                print("  no derived Omen rows for this affix")
            return 0
        if args.all_bases:
            bases = all_bases(connection, affix_row["affix_id"])
            if args.json:
                payload = {
                    "affix": {
                        "affix_id": affix_row["affix_id"],
                        "slug": affix_row["slug"],
                        "name": affix_row["name"],
                    },
                    "bases": {
                        str(base_id): {"base_type": data["base_type"], "ranges": data["rows"]}
                        for base_id, data in bases.items()
                    },
                }
                print(dumps(payload))
                return 0
            print(
                f"# {affix_row['name']} (affixId {affix_row['affix_id']}, {affix_row['type_name']}, "
                f"slug={affix_row['slug']}, standardAffixEffectModifier={affix_row['standard_affix_effect_modifier']})"
            )
            for base_id, data in bases.items():
                base_type = data["base_type"] or {}
                multiplier = {row["modifier"] for row in data["rows"]}
                print(
                    f"\n## {base_type.get('display_name', base_id)} (baseTypeID {base_id}, "
                    f"affixEffectModifier={base_type.get('affix_effect_modifier')}, multiplier={sorted(multiplier)})"
                )
                for row in data["rows"]:
                    print(
                        f"  T{row['tier']} slot{row['roll_slot']}  raw={row['raw_min']:g}-{row['raw_max']:g}  "
                        f"effective={row['effective_min_decimal']}..{row['effective_max_decimal']}  "
                        f"game={format_range_value(row, 'game')}  tunklab={format_range_value(row, 'tunklab')}"
                    )
            return 0
        result = query(connection, affix_row["affix_id"], args.base, args.tier, args.slot)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        connection.close()

    if args.json:
        print(dumps(query_to_json(result)))
        return 0
    base_label = result.base_type["display_name"] if result.base_type else "(all bases)"
    print(
        f"# {result.affix['name']} (affixId {result.affix['affix_id']}, {result.affix['type_name']}, "
        f"slug={result.affix['slug']}) x {base_label}"
    )
    if result.base_type:
        print(
            f"# baseTypeID={result.base_type['base_type_id']} "
            f"affixEffectModifier={result.base_type['affix_effect_modifier']} "
            f"eligible={result.eligible}"
        )
    if not result.rows:
        print("no matching ranges")
        return 0 if result.eligible else 3
    if args.rounding in ("both", "game", "tunklab"):
        if args.rounding == "both":
            print(format_rows(result.rows))
            print("\n[tunklab page rounding]")
            print(format_rows(result.rows, "tunklab"))
        else:
            print(format_rows(result.rows, args.rounding))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_db(args.db, args.manifest)
    if args.json:
        print(dumps(report.as_dict()))
    else:
        print(report_to_text(report))
    return 0 if report.ok else 1


def _cmd_render_pages(args: argparse.Namespace) -> int:
    slugs = tuple(args.slugs) if args.slugs else DEFAULT_PAGE_SLUGS
    manifest = render_pages(
        args.out,
        base_url=args.base_url,
        slugs=slugs,
        window=args.window,
        virtual_time_budget_ms=args.virtual_time_budget_ms,
        chrome_path=args.chrome,
    )
    print(json.dumps({"pages": len(manifest["pages"]), "errors": manifest["errors"]}, indent=2))
    return 0 if not manifest["errors"] else 1


def _cmd_verify_display(args: argparse.Namespace) -> int:
    slugs = tuple(args.slugs) if args.slugs else ()
    result = verify_rendered_pages(args.db, args.rendered, args.out, slugs=slugs)
    print(json.dumps(result["summary"], indent=2))
    return 0 if result["summary"]["pages_match"] == result["summary"]["pages"] else 1


def _cmd_crosscheck(args: argparse.Namespace) -> int:
    if args.game_evidence is None:
        print(
            "error: crosscheck-game 需要显式 --game-evidence <path>：公开版不随包分发"
            "本机游戏参考数据（原先的默认目录不在公开树内）。",
            file=sys.stderr,
        )
        return 2
    payload = crosscheck_against_game(args.db, args.game_evidence, args.out)
    print(json.dumps(payload["summary"], indent=2))
    return 0 if payload["summary"]["affixes_with_mismatched_tiers"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m affix_db",
        description="Local normalized Last Epoch affix roll-range database (tunklab static data)",
    )
    parser.add_argument("--version", action="version", version=f"affix_db {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="download the frozen tunklab snapshot (network)")
    fetch_parser.add_argument("--out", type=Path, default=_repo_path("data/affix-db/v1/source-snapshot"))
    fetch_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    fetch_parser.add_argument("--slugs", nargs="*", default=list(DEFAULT_PAGE_SLUGS))
    fetch_parser.add_argument("--timeout", type=float, default=60.0)
    fetch_parser.set_defaults(func=_cmd_fetch)

    build_parser = subparsers.add_parser("build", help="build the normalized DB from the frozen snapshot (offline)")
    build_parser.add_argument("--snapshot", type=Path, default=_repo_path("data/affix-db/v1/source-snapshot"))
    build_parser.add_argument("--out", type=Path, default=_repo_path("data/affix-db/v1"))
    build_parser.set_defaults(func=_cmd_build)

    query_parser = subparsers.add_parser("query", help="query effective roll ranges (offline)")
    query_parser.add_argument("--db", type=Path, default=_default_db())
    query_parser.add_argument("--affix", required=True, help="affix id, slug or exact name (e.g. fire_resistance)")
    query_parser.add_argument("--base", help="equipment base id, display name, baseTypeName or sub-base slug")
    query_parser.add_argument("--tier", type=int, help="1-based tier (e.g. 7)")
    query_parser.add_argument("--slot", type=int, help="roll slot (0 = main tier roll)")
    query_parser.add_argument("--all-bases", action="store_true", help="show every eligible base type")
    query_parser.add_argument(
        "--omen-sub-base",
        default=None,
        help="show the derived Omen-idol special path for an Omen sub-base (e.g. grand_arcane_omen_idol)",
    )
    query_parser.add_argument("--rounding", choices=("game", "tunklab", "both"), default="game")
    query_parser.add_argument("--json", action="store_true")
    query_parser.set_defaults(func=_cmd_query)

    validate_parser = subparsers.add_parser("validate", help="run validation checks (offline)")
    validate_parser.add_argument("--db", type=Path, default=_default_db())
    validate_parser.add_argument("--manifest", type=Path, default=None)
    validate_parser.add_argument("--json", action="store_true")
    validate_parser.set_defaults(func=_cmd_validate)

    render_parser = subparsers.add_parser("render-pages", help="capture hydrated tunklab DOM with headless Chrome (network)")
    render_parser.add_argument("--out", type=Path, default=_repo_path("data/affix-db/v1/verification"))
    render_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    render_parser.add_argument("--slugs", nargs="*", default=list(DEFAULT_PAGE_SLUGS))
    render_parser.add_argument("--window", default="1920,1080")
    render_parser.add_argument("--virtual-time-budget-ms", type=int, default=20000)
    render_parser.add_argument("--chrome", default=None)
    render_parser.set_defaults(func=_cmd_render_pages)

    verify_parser = subparsers.add_parser("verify-display", help="compare rendered pages against the DB (offline)")
    verify_parser.add_argument("--db", type=Path, default=_default_db())
    verify_parser.add_argument("--rendered", type=Path, default=_repo_path("data/affix-db/v1/verification/rendered"))
    verify_parser.add_argument("--out", type=Path, default=_repo_path("data/affix-db/v1/verification"))
    verify_parser.add_argument("--slugs", nargs="*", default=[])
    verify_parser.set_defaults(func=_cmd_verify_display)

    crosscheck_parser = subparsers.add_parser("crosscheck-game", help="compare raw tiers with local game 1.5.3 evidence (offline)")
    crosscheck_parser.add_argument("--db", type=Path, default=_default_db())
    crosscheck_parser.add_argument(
        "--game-evidence",
        type=Path,
        default=None,
        help="本机游戏 1.5.3 侧范围证据 JSON（必须显式提供；公开版不随包分发该参考数据）",
    )
    crosscheck_parser.add_argument("--out", type=Path, default=_repo_path("data/affix-db/v1/verification/game-crosscheck.json"))
    crosscheck_parser.set_defaults(func=_cmd_crosscheck)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (QueryError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
