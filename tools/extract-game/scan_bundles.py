"""Scan the game's asset-bundle folder for MonoBehaviours of interest.

The Last Epoch install ships 26k+ ``*.bundle`` files (UnityFS, Unity 6000.4.8f1)
under ``StreamingAssets/LEAssetBundles``.  Bundle file names are hashes and the
custom ``Catalog.bin`` (LABC v4) is not parsed yet, so class-name scanning is the
practical way to locate data assets (skills, trees, affixes, buffs...).

Game bundles keep serialized type trees, therefore ``MonoBehaviour`` fields are
readable without any IL2CPP type-tree reconstruction.  Script *class names* need
the shared ``monoscripts.bundle`` registered as ``CAB-monoscripts``.

Read-only: only opens files; writes its JSON result to the given ``--out`` path.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

# The Last Epoch install directory is machine-specific: it must be supplied
# explicitly through ``--game-root`` or the ``LE_GAME_ROOT`` environment variable.
# No machine-local absolute path is hard-coded here (see README, docs/version-control.md).
DEFAULT_GAME_ROOT = Path(os.environ.get("LE_GAME_ROOT", ""))


def require_game_root(game_root) -> Path:
    """Return the configured game root or fail with an actionable message."""
    root = Path(str(game_root or "").strip())
    if not str(root).strip():
        raise SystemExit(
            "未配置 Last Epoch 安装目录：请传 --game-root <目录> 或设置环境变量 LE_GAME_ROOT"
            "（例如 <Steam 库>\\steamapps\\common\\Last Epoch）。"
        )
    return root


def serialized_files(env):
    out = []

    def walk(node):
        if hasattr(node, "unity_version") and hasattr(node, "externals"):
            out.append(node)
            return
        files = getattr(node, "files", None)
        if isinstance(files, dict):
            for child in files.values():
                walk(child)

    for root in env.files.values():
        walk(root)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game-root",
        default=str(DEFAULT_GAME_ROOT),
        help="Last Epoch 安装目录；未给则读环境变量 LE_GAME_ROOT",
    )
    parser.add_argument(
        "--pattern",
        default=r"SkillTree|SkillTreeNode|AffixList|CharacterClass|SkillTreeData",
        help="regex matched against the MonoBehaviour script class name",
    )
    parser.add_argument("--max-size-mb", type=float, default=0, help="skip bundles above this size (0 = no limit)")
    parser.add_argument("--stop-after", type=int, default=25, help="stop after this many matches")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--out", help="write JSON hits/progress here")
    args = parser.parse_args(argv)

    import UnityPy  # noqa: WPS433 - lazy import so --help works without venv deps

    game_root = require_game_root(args.game_root)
    data_dir = game_root / "Last Epoch_Data"
    bundles_dir = data_dir / "StreamingAssets" / "LEAssetBundles"
    monoscripts = bundles_dir / "monoscripts.bundle"

    env0 = UnityPy.load(str(monoscripts))
    mono_sf = next(iter(serialized_files(env0)))
    pattern = re.compile(args.pattern)

    files = [p for p in bundles_dir.glob("*.bundle") if p.name != "monoscripts.bundle"]
    files.sort(key=lambda p: p.stat().st_size)
    skipped_large = 0
    if args.max_size_mb:
        limit = args.max_size_mb * 1024 * 1024
        kept = []
        for path in files:
            if path.stat().st_size > limit:
                skipped_large += 1
            else:
                kept.append(path)
        files = kept

    started = time.time()
    hits: list[dict] = []
    errors: collections.Counter[str] = collections.Counter()
    class_counts: collections.Counter[str] = collections.Counter()
    scanned = 0
    for path in files:
        scanned += 1
        try:
            env = UnityPy.load(str(path))
            env.path = str(data_dir)
            env.register_cab(mono_sf.name, mono_sf)
            for sf in serialized_files(env):
                for obj in sf.objects.values():
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        head = obj.parse_monobehaviour_head()
                        script = head.m_Script.deref_parse_as_object()
                    except Exception as exc:  # noqa: BLE001 - record, keep scanning
                        errors[f"{type(exc).__name__}: {exc}"] += 1
                        continue
                    class_counts[script.m_ClassName] += 1
                    if pattern.search(script.m_ClassName):
                        hits.append(
                            {
                                "bundle": str(path),
                                "bundle_size": path.stat().st_size,
                                "path_id": obj.path_id,
                                "script_class": script.m_ClassName,
                                "m_Name": head.m_Name,
                                "container_paths": _container_paths(sf)[:10],
                            }
                        )
        except Exception as exc:  # noqa: BLE001 - corrupted/unreadable bundle
            errors[f"bundle {type(exc).__name__}: {exc}"] += 1
        if args.progress_every and scanned % args.progress_every == 0:
            elapsed = time.time() - started
            print(
                f"scanned {scanned}/{len(files)} bundles, hits={len(hits)}, "
                f"errors={sum(errors.values())}, {elapsed:.0f}s"
            )
            sys.stdout.flush()
        if len(hits) >= args.stop_after:
            print(f"stop-after reached after {scanned} bundles")
            break

    result = {
        "bundles_dir": str(bundles_dir),
        "pattern": args.pattern,
        "bundles_total": len(files),
        "bundles_scanned": scanned,
        "skipped_large": skipped_large,
        "elapsed_seconds": round(time.time() - started, 1),
        "hits": hits,
        "class_counts_top": dict(class_counts.most_common(60)),
        "errors_top": dict(errors.most_common(20)),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(text)
    return 0


def _container_paths(sf) -> list[str]:
    try:
        return list(sf.container.keys())
    except Exception:  # noqa: BLE001 - container metadata is optional
        return []


if __name__ == "__main__":
    raise SystemExit(main())
