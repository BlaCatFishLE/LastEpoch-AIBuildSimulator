"""Read-only inspection helpers for the local Last Epoch Unity assets.

Phase-1 workguide section 10 restricts local game-resource work to read-only
trials: open only the containers relevant to a concrete question, record the
tool version and command, keep container path / object path ids / type names
with the raw result, and never write anything into the game installation.

The game is an IL2CPP Unity 6.4 build, so serialized files carry no type
trees.  This tool therefore separates what is provable from what is not:

* container / object enumeration, built-in Unity classes, ``TextAsset`` text
  and ``MonoScript`` class names are readable directly;
* ``MonoBehaviour`` *fields* need a generated type tree (UnityPy's optional
  ``TypeTreeGeneratorAPI`` fed with ``GameAssembly.dll`` + ``global-metadata.dat``
  or Il2CppDumper/Cpp2IL output).  Without it the tool records the failure
  instead of guessing field meanings.

Examples (run with the project venv, see README.md)::

    .venv\\Scripts\\python.exe tools\\extract-game\\inspect_assets.py info
    .venv\\Scripts\\python.exe tools\\extract-game\\inspect_assets.py types
    .venv\\Scripts\\python.exe tools\\extract-game\\inspect_assets.py monoscripts --out data\\raw\\game\\game-build-26087601\\monoscripts.classes.json
    .venv\\Scripts\\python.exe tools\\extract-game\\inspect_assets.py scripts --bundle <path.bundle>
    .venv\\Scripts\\python.exe tools\\extract-game\\inspect_assets.py sample --bundle <path.bundle> --path-id 123
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

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


def game_paths(game_root: Path | str) -> tuple[Path, Path, Path]:
    game_root = require_game_root(game_root)
    data_dir = game_root / "Last Epoch_Data"
    bundles = data_dir / "StreamingAssets" / "LEAssetBundles"
    return data_dir, bundles, bundles / "monoscripts.bundle"


def _data_dir_of(path: Path) -> Path:
    """Return the ``*_Data`` directory containing ``path`` (game install)."""
    for parent in Path(path).parents:
        if parent.name.endswith("_Data") and parent.is_dir():
            return parent
    return game_paths(DEFAULT_GAME_ROOT)[0]


def import_unitypy():
    try:
        import UnityPy  # noqa: WPS433 - deliberate lazy import for a clear error
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SystemExit(
            "UnityPy is not installed. Create the project venv and install it:\n"
            "  <python> -m venv .venv\n"
            "  .venv\\Scripts\\python.exe -m pip install UnityPy\n"
            f"original error: {exc}"
        )
    return UnityPy


def load_container(container: Path, data_dir: Path):
    """Load a serialized file / AssetBundle and let external references resolve.

    ``Environment.path`` is set to ``<game>\\Last Epoch_Data`` so that PPtrs to
    ``globalgamemanagers.assets`` / ``sharedassets0.assets`` can be followed
    without copying anything out of the game directory.
    """
    UnityPy = import_unitypy()
    env = UnityPy.load(str(container))
    env.path = str(data_dir)
    return env


def iter_serialized_files(env):
    """Yield every SerializedFile in an Environment (also inside AssetBundles).

    ``SerializedFile.files`` is an alias of its object dict, so detection is by
    the header attributes instead of by the presence of ``files``.
    """
    seen = set()

    def walk(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if hasattr(node, "unity_version") and hasattr(node, "externals"):
            yield node
            return
        files = getattr(node, "files", None)
        if isinstance(files, dict):
            for child in files.values():
                yield from walk(child)

    for node in env.files.values():
        yield from walk(node)


def resolve_monoscripts_cab(env, monoscripts: Path):
    """Register the shared ``CAB-monoscripts`` serialized file on ``env``.

    Asset bundles reference scripts as ``archive:/CAB-monoscripts/CAB-monoscripts``.
    Loading the game's ``monoscripts.bundle`` and registering its inner
    SerializedFile under that name makes ``PPtr`` resolution work.
    """
    mono_env = load_container(monoscripts, _data_dir_of(monoscripts))
    mono_sf = next(iter(iter_serialized_files(mono_env)))
    env.register_cab(mono_sf.name, mono_sf)
    return mono_sf


def read_script_class(obj) -> tuple[str | None, str | None, str | None]:
    """Return (namespace, class_name, assembly) of a MonoBehaviour's script."""
    head = obj.parse_monobehaviour_head()
    script = head.m_Script.deref_parse_as_object()
    return script.m_Namespace, script.m_ClassName, script.m_AssemblyName


def script_full_name(obj) -> str:
    namespace, class_name, assembly = read_script_class(obj)
    if namespace:
        return f"{namespace}.{class_name}"
    return class_name or f"<unnamed@{assembly}>"


def safe_json(value: Any, _depth: int = 0) -> Any:
    """Best-effort conversion of UnityPy values to JSON-serializable data."""
    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, dict):
        return {str(k): safe_json(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_json(v, _depth + 1) for v in value]
    if hasattr(value, "__dict__"):
        return {
            str(k): safe_json(v, _depth + 1)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    if hasattr(value, "__slots__"):
        return {
            str(k): safe_json(getattr(value, k), _depth + 1)
            for k in getattr(value, "__slots__", ())
        }
    return str(value)


def object_summary(obj) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path_id": obj.path_id,
        "type": obj.type.name,
        "class_id": int(getattr(obj.type, "value", 0)),
    }
    if obj.type.name == "MonoBehaviour":
        try:
            namespace, class_name, assembly = read_script_class(obj)
            summary["script_class"] = f"{namespace}.{class_name}" if namespace else class_name
            summary["script_assembly"] = assembly
        except Exception as exc:  # noqa: BLE001 - record instead of hiding
            summary["script_class_error"] = f"{type(exc).__name__}: {exc}"
        try:
            head = obj.parse_monobehaviour_head()
            summary["m_Name"] = head.m_Name
            summary["m_Enabled"] = head.m_Enabled
        except Exception as exc:  # noqa: BLE001
            summary["m_Name_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def cmd_info(args) -> dict[str, Any]:
    data_dir, _bundles, _mono = game_paths(args.game_root)
    container = Path(args.container)
    env = load_container(container, data_dir)
    files = list(iter_serialized_files(env))
    result: dict[str, Any] = {
        "container": str(container),
        "size": container.stat().st_size,
        "serialized_files": [],
    }
    for sf in files:
        result["serialized_files"].append(
            {
                "name": sf.name,
                "unity_version": str(sf.unity_version),
                "serialized_file_version": str(sf.version),
                "endian": str(getattr(sf.header, "endian", "?")),
                "object_count": len(sf.objects),
                "enable_type_tree": bool(sf._enable_type_tree),
                "external_count": len(sf.externals),
                "externals": [e.path for e in sf.externals][:20],
            }
        )
    return result


def cmd_types(args) -> dict[str, Any]:
    data_dir, _bundles, _mono = game_paths(args.game_root)
    container = Path(args.container)
    env = load_container(container, data_dir)
    counter: collections.Counter[str] = collections.Counter()
    for sf in iter_serialized_files(env):
        for obj in sf.objects.values():
            counter[obj.type.name] += 1
    rows = [
        {"type": name, "count": count}
        for name, count in counter.most_common(args.top)
    ]
    return {
        "container": str(container),
        "object_count": sum(counter.values()),
        "distinct_types": len(counter),
        "top": rows,
    }


def cmd_monoscripts(args) -> dict[str, Any]:
    data_dir, _bundles, monoscripts = game_paths(args.game_root)
    path = Path(args.monoscripts) if args.monoscripts else monoscripts
    env = load_container(path, data_dir)
    rows = []
    pattern = re.compile(args.filter) if args.filter else None
    for sf in iter_serialized_files(env):
        for obj in sf.objects.values():
            if obj.type.name != "MonoScript":
                continue
            data = obj.read()
            full = f"{data.m_Namespace}.{data.m_ClassName}" if data.m_Namespace else data.m_ClassName
            if pattern and not pattern.search(full):
                continue
            rows.append(
                {
                    "path_id": obj.path_id,
                    "namespace": data.m_Namespace,
                    "class_name": data.m_ClassName,
                    "assembly": data.m_AssemblyName,
                    "full_name": full,
                }
            )
    rows.sort(key=lambda row: row["full_name"])
    return {
        "container": str(path),
        "monoscript_count": len(rows),
        "assemblies": dict(
            collections.Counter(row["assembly"] for row in rows).most_common()
        ),
        "scripts": rows,
    }


def cmd_textassets(args) -> dict[str, Any]:
    data_dir, _bundles, _mono = game_paths(args.game_root)
    container = Path(args.container)
    env = load_container(container, data_dir)
    dump_dir = Path(args.dump) if args.dump else None
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for sf in iter_serialized_files(env):
        for obj in sf.objects.values():
            if obj.type.name != "TextAsset":
                continue
            if len(rows) >= args.limit:
                break
            data = obj.read()
            text = data.m_Script
            row = {
                "path_id": obj.path_id,
                "name": data.m_Name,
                "char_count": len(text),
                "sha256_of_text": _sha256_text(text),
                "preview": text[: args.preview],
            }
            rows.append(row)
            if dump_dir:
                target = dump_dir / f"{obj.path_id:08d}_{_slug(data.m_Name)}.txt"
                target.write_text(text, encoding="utf-8", errors="replace")
    return {"container": str(container), "textasset_count": len(rows), "samples": rows}


def cmd_scripts(args) -> dict[str, Any]:
    data_dir, bundles, default_mono = game_paths(args.game_root)
    container = Path(args.bundle)
    env = load_container(container, data_dir)
    monoscripts = Path(args.monoscripts) if args.monoscripts else default_mono
    resolve_monoscripts_cab(env, monoscripts)
    counter: collections.Counter[str] = collections.Counter()
    errors: collections.Counter[str] = collections.Counter()
    rows = []
    for sf in iter_serialized_files(env):
        container_paths = _container_paths(sf)
        for obj in sf.objects.values():
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                name = script_full_name(obj)
                counter[name] += 1
                if len(rows) < args.limit:
                    head = obj.parse_monobehaviour_head()
                    rows.append(
                        {
                            "path_id": obj.path_id,
                            "script_class": name,
                            "m_Name": head.m_Name,
                            "container_paths": container_paths[:5],
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - keep the failure visible
                errors[f"{type(exc).__name__}: {exc}"] += 1
    return {
        "bundle": str(container),
        "monoscripts": str(monoscripts),
        "monobehaviour_count": sum(counter.values()) + sum(errors.values()),
        "script_class_counts": dict(counter.most_common(args.top)),
        "errors": dict(errors.most_common(20)),
        "samples": rows,
    }


def cmd_sample(args) -> dict[str, Any]:
    data_dir, bundles, default_mono = game_paths(args.game_root)
    container = Path(args.bundle)
    env = load_container(container, data_dir)
    if args.path_id is not None:
        target = None
        for sf in iter_serialized_files(env):
            if args.path_id in sf.objects:
                target = sf.objects[args.path_id]
                break
        if target is None:
            raise SystemExit(f"path_id {args.path_id} not found in {container}")
    else:
        target = next(
            (obj for sf in iter_serialized_files(env) for obj in sf.objects.values()),
            None,
        )
        if target is None:
            raise SystemExit(f"container {container} has no objects")
    if target.type.name == "MonoBehaviour":
        # Register the shared script container before resolving m_Script.
        resolve_monoscripts_cab(env, default_mono)
    result: dict[str, Any] = {
        "container": str(container),
        "container_paths": _container_paths_for_object(env, target),
        "object": object_summary(target),
    }
    # Attempt the real typetree read; keep the exact error as evidence.
    try:
        data = target.read_typetree()
        result["typetree"] = safe_json(data)
    except Exception as exc:  # noqa: BLE001
        result["typetree_error"] = f"{type(exc).__name__}: {exc}"
    raw = target.get_raw_data()
    result["raw_byte_size"] = len(raw)
    result["raw_hex_prefix"] = raw[: args.raw_bytes].hex()
    return result


def cmd_settings(args) -> dict[str, Any]:
    """Read PlayerSettings from globalgamemanagers (version/branding evidence).

    UnityPy 1.25.4's bundled typetree for ``PlayerSettings`` on Unity 6000.4 is
    4 bytes short, so a strict read raises ``ValueError: Expected to read 916
    bytes, but only read 912 bytes``.  The known fields are still aligned, so a
    non-strict read is used and the discrepancy is recorded verbatim.
    """
    data_dir, _bundles, _mono = game_paths(args.game_root)
    env = load_container(data_dir / "globalgamemanagers", data_dir)
    ps = next((o for o in env.objects if o.type.name == "PlayerSettings"), None)
    if ps is None:
        raise SystemExit("PlayerSettings not found in globalgamemanagers")
    result: dict[str, Any] = {"container": str(data_dir / "globalgamemanagers")}
    strict_error = None
    data = None
    try:
        data = ps.read()
    except Exception as exc:  # noqa: BLE001 - keep the exact mismatch as evidence
        strict_error = f"{type(exc).__name__}: {exc}"
        data = ps.read(check_read=False)
    if strict_error:
        result["strict_read_error"] = strict_error
    wanted = (
        "companyName",
        "productName",
        "bundleVersion",
        "organizationId",
        "projectName",
        "cloudProjectId",
        "targetPlatform",
    )
    result["fields"] = {key: getattr(data, key, None) for key in wanted}
    return result


def _container_paths(sf) -> list[str]:
    try:
        container = getattr(sf, "container", None)
        return list(container.keys()) if container is not None else []
    except Exception:  # noqa: BLE001 - container is optional metadata
        return []


def _container_paths_for_object(env, obj) -> list[str]:
    sf = obj.assets_file
    return _container_paths(sf)[:10]


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _slug(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    return safe[:80] or "unnamed"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--game-root",
        default=str(DEFAULT_GAME_ROOT),
        help=f"Last Epoch install root (default: {DEFAULT_GAME_ROOT})",
    )
    common.add_argument(
        "--out",
        help="write the JSON result to this path (also printed to stdout)",
    )

    parser = argparse.ArgumentParser(
        description="Read-only inspection of the local Last Epoch Unity assets.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", parents=[common], help="show serialized-file headers/version")
    info.add_argument("--container", default=str(game_paths(DEFAULT_GAME_ROOT)[0] / "globalgamemanagers"))
    info.set_defaults(func=cmd_info)

    settings = sub.add_parser("settings", parents=[common], help="read PlayerSettings (game version evidence)")
    settings.set_defaults(func=cmd_settings)

    types = sub.add_parser("types", parents=[common], help="count object types in a container")
    types.add_argument("--container", default=str(game_paths(DEFAULT_GAME_ROOT)[0] / "resources.assets"))
    types.add_argument("--top", type=int, default=40)
    types.set_defaults(func=cmd_types)

    monos = sub.add_parser("monoscripts", parents=[common], help="list MonoScript class names")
    monos.add_argument("--monoscripts", default=None)
    monos.add_argument("--filter", default=None, help="regex filter on full class name")
    monos.set_defaults(func=cmd_monoscripts)

    text = sub.add_parser("textassets", parents=[common], help="list/read TextAsset contents")
    text.add_argument("--container", default=str(game_paths(DEFAULT_GAME_ROOT)[0] / "resources.assets"))
    text.add_argument("--limit", type=int, default=200)
    text.add_argument("--preview", type=int, default=120)
    text.add_argument("--dump", default=None, help="dump full TextAsset text into this dir")
    text.set_defaults(func=cmd_textassets)

    scripts = sub.add_parser("scripts", parents=[common], help="resolve script classes of MonoBehaviours in a bundle")
    scripts.add_argument("--bundle", required=True)
    scripts.add_argument("--monoscripts", default=None)
    scripts.add_argument("--limit", type=int, default=50)
    scripts.add_argument("--top", type=int, default=40)
    scripts.set_defaults(func=cmd_scripts)

    sample = sub.add_parser("sample", parents=[common], help="export one object by path id (raw + typetree attempt)")
    sample.add_argument("--bundle", required=True)
    sample.add_argument("--path-id", type=int, default=None)
    sample.add_argument("--raw-bytes", type=int, default=512)
    sample.set_defaults(func=cmd_sample)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = args.func(args)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if getattr(args, "out", None):
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {target} ({len(text)} bytes)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
