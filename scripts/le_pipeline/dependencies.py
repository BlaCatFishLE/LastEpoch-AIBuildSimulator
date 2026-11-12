# -*- coding: utf-8 -*-
"""P3 输入路线的外部依赖目录与检查（只读、stdlib）。

提供（三条输入路线：save-tool-native-wire / save+planner / planner-payload）：

* :data:`DEPENDENCIES`：依赖描述目录（JSON 可序列化），字段满足
  ``{id, kind, required_for, resolve(root, config), check()}`` 约定；
* :func:`check_routes`：``check_routes(case_ref, root)`` 的实际检查入口，
  返回 ``{route_id, dependencies[], ready, needs_input[]}``（含 ``warnings``、
  ``fallback_policy`` 等附加字段），**不抛未捕获异常**；
* :func:`dependency_catalog`：给 doctor/prepare/文档消费的纯 JSON 描述。

状态语义（``status``）：

* ``ok``：已确认可用；
* ``missing``：确认缺失（路径不存在/字段缺失/版本不满足）；
* ``undetermined``：无法判定（如可选外部运行时未配置、证据不足）——**不得当作 ok**。

``ready`` 为真仅当路线已判定、且所有 **required** 依赖都是 ``ok``；可选依赖
缺失或 undetermined 只进 ``warnings``，不阻塞 ``ready``。任何 required 依赖
非 ok 都进入 ``needs_input[]``（可操作 hint），并且**不静默换路线**：
``fallback_policy`` 恒为 ``never_fallback``。

所有输出只含仓库相对路径或 ``<external>/<basename>``；不输出个人机器绝对路径，
不读写任何文件（只读取检查所需 JSON/路径元数据），不启动子进程。
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable

from . import routes

KIND_PYTHON = "python"
KIND_REPO_FILE = "repo_file"
KIND_EXTERNAL_TOOL = "external_tool"
KIND_LOCAL_CONFIG = "local_config"
KINDS: tuple[str, ...] = (KIND_PYTHON, KIND_REPO_FILE, KIND_EXTERNAL_TOOL, KIND_LOCAL_CONFIG)

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNDETERMINED = "undetermined"
STATUSES: tuple[str, ...] = (STATUS_OK, STATUS_MISSING, STATUS_UNDETERMINED)

ROUTE_ALL = "*"

#: 外部运行时/工具的环境变量覆盖（不写进 config 也能临时指定）
ENV_NODE = "LE_NODE"
ENV_TOOL_NATIVE_HARNESS = "LE_TOOL_NATIVE_HARNESS"
ENV_TOOL_NATIVE_HOME = "LE_TOOL_NATIVE_HOME"

#: tool-native harness 是实现债务的一种可选外部路线：公开版不随包分发该工具，
#: 也不内置任何作者案例证据；只有接收方显式配置外部工具时才可判定为可用。
_TOOL_NATIVE_HINT = (
    "tool-native harness 属于可选外部工具，不随公开版分发；"
    "如确实需要该路线，请按 docs/contracts/save-onboarding.md 配置 "
    "config/project.json 的 external_tools（或 LE_NODE / LE_TOOL_NATIVE_HARNESS / "
    "LE_TOOL_NATIVE_HOME）；未配置时 prepare 返回 needs_input，不静默改用 planner 路线。"
)

_EXTERNAL_SAVE_IDS = {
    "external.tool_native.node",
    "external.tool_native.harness",
    "external.tool_native.install",
}

_CONFIG_HINT = (
    "在 config/project.json 增加（路径写仓库相对或由接收方配置，勿写个人绝对路径）："
    "external_tools = {node: {path: <node 可执行文件>}, "
    "tool_native: {harness_script: <run_tool_native.js>, tool_home: <lastepochsupertools v1.3.1 目录>}}；"
    "也可用环境变量 LE_NODE / LE_TOOL_NATIVE_HARNESS / LE_TOOL_NATIVE_HOME 临时覆盖。"
)

_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|,;)]*")


def _sanitize_text(text: str) -> str:
    """去掉可能泄漏的个人机器绝对路径（错误信息/CWD 等）。"""

    return _ABS_PATH_RE.sub("<path>", str(text))


def _display(path: pathlib.Path, root: pathlib.Path) -> str:
    """仓库相对显示；仓库外路径只显示 ``<external>/<basename>``。"""

    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return "<external>/" + path.name


def _config_path(value: Any, root: pathlib.Path) -> pathlib.Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = pathlib.Path(value.strip())
    return candidate if candidate.is_absolute() else root / candidate


def _source_field(case: dict[str, Any], key: str) -> str | None:
    source = case.get("source") if isinstance(case, dict) else None
    if not isinstance(source, dict):
        return None
    value = source.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _case_field(case: dict[str, Any], dotted: str) -> Any:
    current: Any = case
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _main_skill_value(case: dict[str, Any], main_skill: str | None) -> str | None:
    if isinstance(main_skill, str) and main_skill.strip():
        return main_skill.strip()
    value = case.get("main_skill")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        ability_id = value.get("ability_id")
        if isinstance(ability_id, str) and ability_id.strip():
            return ability_id.strip()
    return None


def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _module_available(root: pathlib.Path, src_rel: str, module: str) -> tuple[bool, str]:
    src = root / src_rel
    if not src.is_dir():
        return False, "缺少源码目录 %s" % _display(src, root)
    inserted = False
    try:
        sys.path.insert(0, str(src))
        inserted = True
        spec = importlib.util.find_spec(module)
    except (ImportError, AttributeError, ValueError, ModuleNotFoundError):
        spec = None
    finally:
        if inserted:
            try:
                sys.path.remove(str(src))
            except ValueError:
                pass
    if spec is None:
        return False, "源码目录存在但模块 %s 不可导入" % module
    return True, "模块 %s 可导入（%s）" % (module, src_rel)


# ---------------------------------------------------------------------------
# 依赖描述
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dependency:
    """一个可检查的外部依赖（描述 + resolve/check 行为）。"""

    id: str
    kind: str
    required_for: str
    label: str
    hint: str
    routes: tuple[str, ...] = (ROUTE_ALL,)
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "required_for": self.required_for,
            "label": self.label,
            "required": self.required,
            "routes": list(self.routes),
            "hint": self.hint,
        }

    def resolve(
        self,
        root: str | pathlib.Path,
        config: dict[str, Any] | None = None,
        case: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """解析该依赖在当前 root/config/case 下的目标（显示值，无绝对路径）。"""

        root_path = pathlib.Path(root)
        try:
            resolved = _RESOLVERS[self.id](root_path, config or {}, case or {})
            return {"id": self.id, "kind": self.kind, "resolved": resolved}
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "id": self.id,
                "kind": self.kind,
                "resolved": {},
                "error": _sanitize_text("%s: %s" % (type(exc).__name__, exc)),
            }

    def check(
        self,
        *,
        root: str | pathlib.Path,
        config: dict[str, Any] | None = None,
        case: dict[str, Any] | None = None,
        case_path: pathlib.Path | None = None,
        main_skill: str | None = None,
        route_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = _CHECKERS[self.id](
                self,
                pathlib.Path(root),
                config or {},
                case or {},
                case_path,
                main_skill,
                route_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            result = {
                "status": STATUS_UNDETERMINED,
                "detail": _sanitize_text(
                    "检查失败（%s: %s）" % (type(exc).__name__, exc)
                ),
                "hint": self.hint,
            }
        result = dict(result)
        result.setdefault("status", STATUS_UNDETERMINED)
        result.setdefault("detail", "")
        if result["status"] != STATUS_OK:
            result.setdefault("hint", self.hint)
        return result


# -- resolvers --------------------------------------------------------------

def _resolve_config(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return {"path": "config/project.json", "exists": (root / "config" / "project.json").is_file()}


def _resolve_module(src_rel: str) -> Callable[[pathlib.Path, dict[str, Any], dict[str, Any]], dict[str, Any]]:
    def resolver(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        return {"source_dir": src_rel, "exists": (root / src_rel).is_dir()}

    return resolver


def _resolve_dataset(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    raw = (config.get("dataset") or {}).get("pointer") if isinstance(config.get("dataset"), dict) else None
    raw = raw or case.get("dataset") or "data/current.json"
    path = _config_path(raw, root)
    return {
        "config": config.get("dataset") or {},
        "pointer": _display(path, root) if path else None,
        "exists": bool(path and path.is_file()),
    }


def _resolve_affix_db(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    raw = case.get("affix_db") or config.get("affix_db") or "data/affix-db/v1/normalized/affix-db.sqlite"
    path = _config_path(raw, root)
    return {"path": _display(path, root) if path else None, "exists": bool(path and path.is_file())}


_PIPELINE_SCRIPTS = (
    "tools/save_import/scripts/pipeline/run_affix_db_pipeline.py",
    "tools/save_import/scripts/pipeline/apply_corrections.py",
    "tools/save_import/scripts/pipeline/make_snapshot.py",
)


def _resolve_pipeline_scripts(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return {
        "scripts": {
            name: (root / name).is_file() for name in _PIPELINE_SCRIPTS
        }
    }


def _resolve_case_file(
    field: str,
) -> Callable[[pathlib.Path, dict[str, Any], dict[str, Any]], dict[str, Any]]:
    def resolver(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        raw = _source_field(case, field)
        path = _config_path(raw, root)
        return {
            "case_field": "source." + field,
            "value": raw,
            "path": _display(path, root) if path else None,
            "exists": bool(path and path.is_file()),
        }

    return resolver


def _resolve_node(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    candidates, source = _node_candidates(root, config)
    for label, path in candidates:
        if path.is_file():
            return {"path": _display(path, root), "source": label, "exists": True}
    return {
        "candidates": [{"source": label, "path": _display(path, root)} for label, path in candidates],
        "exists": False,
    }


def _resolve_tool_native_harness(
    root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any]:
    raw = _tool_native_config(config).get("harness_script") or os.environ.get(ENV_TOOL_NATIVE_HARNESS)
    path = _config_path(raw, root)
    return {
        "configured": bool(raw),
        "source": "config" if _tool_native_config(config).get("harness_script") else ("env" if raw else None),
        "path": _display(path, root) if path else None,
        "exists": bool(path and path.is_file()),
    }


def _resolve_tool_native_install(
    root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any]:
    raw = _tool_native_config(config).get("tool_home") or os.environ.get(ENV_TOOL_NATIVE_HOME)
    path = _config_path(raw, root)
    return {
        "configured": bool(raw),
        "source": "config" if _tool_native_config(config).get("tool_home") else ("env" if raw else None),
        "path": _display(path, root) if path else None,
        "exists": bool(path and path.is_dir()),
    }


def _resolve_planner_payload(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    raw = _source_field(case, "planner_build_response")
    path = _config_path(raw, root)
    return {"case_field": "source.planner_build_response", "path": _display(path, root) if path else None,
            "exists": bool(path and path.is_file())}


def _resolve_main_skill(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    value = _main_skill_value(case, None)
    return {"from_case": value, "declared": value is not None}


def _resolve_run_summary(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    raw = _source_field(case, "tool_native_run_summary")
    path = _config_path(raw, root)
    return {"path": _display(path, root) if path else None, "exists": bool(path and path.is_file())}


def _resolve_case_config(root: pathlib.Path, config: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return {"case_loaded": bool(case), "root": "."}


# -- checkers ---------------------------------------------------------------

def _ok(detail: str, **extra: Any) -> dict[str, Any]:
    result = {"status": STATUS_OK, "detail": detail}
    result.update(extra)
    return result


def _missing(detail: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    result = {"status": STATUS_MISSING, "detail": detail}
    if hint:
        result["hint"] = hint
    result.update(extra)
    return result


def _undetermined(detail: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    result = {"status": STATUS_UNDETERMINED, "detail": detail}
    if hint:
        result["hint"] = hint
    result.update(extra)
    return result


def _min_version(config: dict[str, Any]) -> tuple[int, int]:
    raw = str((config.get("python") or {}).get("min_version") or "3.12")
    parts = raw.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return (3, 12)


def _check_interpreter(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    required = _min_version(config)
    version = "%d.%d.%d" % (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    if sys.version_info[:2] >= required:
        return _ok("Python %s（要求 ≥ %d.%d）" % (version, required[0], required[1]))
    return _missing(
        "Python %s 低于要求 ≥ %d.%d" % (version, required[0], required[1]),
        "使用仓库 .venv 的 Python（.\\.venv\\Scripts\\python.exe），或设置 LE_PYTHON 指向 ≥ %d.%d 的解释器"
        % (required[0], required[1]),
    )


def _check_module(module: str, src_rel: str):
    def checker(
        dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
        case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
    ) -> dict[str, Any]:
        ok, detail = _module_available(root, src_rel, module)
        if ok:
            return _ok(detail)
        return _missing(
            detail,
            "确认仓库布局完整（PYTHONPATH 应包含 %s）；不要依赖系统已安装的同名包" % src_rel,
        )

    return checker


def _check_project_config(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    path = root / "config" / "project.json"
    if not path.is_file():
        return _missing("缺少 config/project.json", "恢复仓库配置模板 config/project.json（所有路径仓库相对）")
    try:
        parsed = _read_json(path)
    except (OSError, ValueError) as exc:
        return _missing(
            "config/project.json 不可解析（%s）" % _sanitize_text(str(exc)),
            "修正 config/project.json 的 JSON 语法；不要用本机绝对路径",
        )
    if not isinstance(parsed, dict):
        return _missing("config/project.json 不是 JSON object", "恢复项目配置模板")
    return _ok("config/project.json（schema=%s）" % parsed.get("schema", "?"), config=parsed)


def _check_dataset_pointer(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = (config.get("dataset") or {}).get("pointer") if isinstance(config.get("dataset"), dict) else None
    raw = raw or case.get("dataset") or "data/current.json"
    path = _config_path(raw, root)
    display = _display(path, root) if path else "?"
    if not path or not path.is_file():
        return _missing(
            "dataset 指针不存在：%s" % display,
            "检查 config.project.json 的 dataset.pointer 与 data/current.json 是否存在；dataset 只读，不要改写",
        )
    try:
        parsed = _read_json(path)
    except (OSError, ValueError) as exc:
        return _missing(
            "dataset 指针不可解析：%s（%s）" % (display, _sanitize_text(str(exc))),
            "恢复 data/current.json 指针或通过 publish 生成新 dataset 指针",
        )
    if not isinstance(parsed, dict) or not (parsed.get("dataset_id") or parsed.get("database")):
        return _missing(
            "dataset 指针缺少 dataset_id/database 字段：%s" % display,
            "检查 data/current.json 结构（dataset_id、database、logical_content_sha256）",
        )
    return _ok("dataset_id=%s（%s）" % (parsed.get("dataset_id", "?"), display))


def _check_affix_db(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = case.get("affix_db") or config.get("affix_db") or "data/affix-db/v1/normalized/affix-db.sqlite"
    path = _config_path(raw, root)
    display = _display(path, root) if path else "?"
    if path and path.is_file():
        return _ok("affix-db：%s" % display)
    return _missing(
        "affix-db 不存在：%s" % display,
        "检查 case.affix_db / config.affix_db；缺失时按既有流程重建 affix-db（不得跳过词缀范围修正）",
    )


def _check_pipeline_scripts(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    missing = [name for name in _PIPELINE_SCRIPTS if not (root / name).is_file()]
    if missing:
        return _missing(
            "缺少 pipeline 脚本：%s" % ", ".join(missing),
            "恢复 tools/save_import/scripts/pipeline/ 下的既有脚本；prepare 只调用它们，不另写解析路线",
        )
    return _ok("pipeline 脚本齐全（%d 个）" % len(_PIPELINE_SCRIPTS))


def _check_le_commands(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    path = root / "scripts" / "le_commands.py"
    if path.is_file():
        return _ok("scripts/le_commands.py 存在（validate 入口）")
    return _missing(
        "缺少 scripts/le_commands.py",
        "恢复 scripts/le_commands.py；本任务不修改该文件（由 P3 Owner 接线）",
    )


def _check_case_config(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    if not case:
        return _missing("case 未加载", "提供可读的 case.json（仓库相对路径）且为 JSON object")
    if case_path is not None and not case_path.is_file():
        return _missing(
            "case 文件不存在：%s" % _display(case_path, root),
            "提供存在的 case.json（仓库相对路径）；本检查只读，不创建案例",
        )
    if case_path is None:
        return _ok("case 对象（inline dict）")
    return _ok("case：%s" % _display(case_path, root))


def _check_main_skill(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    value = _main_skill_value(case, main_skill)
    if value:
        return _ok("main_skill=%s" % value)
    return _missing(
        "未声明主技能",
        "在 case.main_skill.ability_id 声明，或 prepare 时传 --main-skill <ability-id>；"
        "不得按技能栏位置猜主技能",
    )


def _check_character_save(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = _source_field(case, "character_save")
    if not raw:
        return _missing(
            "case.source.character_save 未声明",
            "在 case.source.character_save 写离线存档路径（1CHARACTERSLOT_BETA_*，仓库相对）",
        )
    path = _config_path(raw, root)
    if path and path.is_file():
        return _ok("存档：%s" % _display(path, root))
    return _missing(
        "存档文件不存在：%s" % (_display(path, root) if path else raw),
        "检查 case.source.character_save（只读快照；不要改写原始存档）",
    )


def _check_stash(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = _source_field(case, "stash")
    if not raw:
        return _missing("未提供 stash（可选）")
    path = _config_path(raw, root)
    if path and path.is_file():
        return _ok("stash：%s" % _display(path, root))
    return _missing(
        "stash 文件不存在：%s" % (_display(path, root) if path else raw),
        "移除 case.source.stash 或提供存在的 STASH_* 文件（可选输入）",
    )


def _check_tool_native_wire(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = _source_field(case, "tool_native_wire")
    if not raw:
        return _missing(
            "未提供归档 tool-native wire（save 路线可由 harness 生成）",
            "为已归档案例提供 case.source.tool_native_wire；新存档输入则配置 tool-native harness（见 external.tool_native.*）",
        )
    path = _config_path(raw, root)
    if path and path.is_file():
        return _ok("tool-native wire：%s" % _display(path, root))
    return _missing(
        "tool-native wire 不存在：%s" % (_display(path, root) if path else raw),
        "检查 case.source.tool_native_wire；不得用 planner payload 冒充 wire",
    )


def _check_run_summary(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = _source_field(case, "tool_native_run_summary")
    if not raw:
        return _missing("未提供 tool-native run-summary（可选证据）")
    path = _config_path(raw, root)
    if not path or not path.is_file():
        return _missing(
            "tool-native run-summary 不存在：%s" % (_display(path, root) if path else raw),
            "检查 case.source.tool_native_run_summary（可选；用于 harness 工具/DB 版本证据）",
        )
    label = _tool_native_label_from_summary(path)
    return _ok("run-summary：%s%s" % (_display(path, root), "（tool=%s）" % label if label else ""))


def _tool_native_config(config: dict[str, Any]) -> dict[str, Any]:
    tools = config.get("external_tools") if isinstance(config.get("external_tools"), dict) else {}
    tool_native = tools.get("tool_native") if isinstance(tools.get("tool_native"), dict) else {}
    return tool_native


def _node_candidates(
    root: pathlib.Path, config: dict[str, Any]
) -> tuple[list[tuple[str, pathlib.Path]], str]:
    candidates: list[tuple[str, pathlib.Path]] = []
    tools = config.get("external_tools") if isinstance(config.get("external_tools"), dict) else {}
    node_cfg = tools.get("node")
    if isinstance(node_cfg, str) and node_cfg.strip():
        candidates.append(("config.external_tools.node", _config_path(node_cfg, root)))  # type: ignore[arg-type]
    elif isinstance(node_cfg, dict):
        node_path = node_cfg.get("path")
        if isinstance(node_path, str) and node_path.strip():
            candidates.append(("config.external_tools.node.path", _config_path(node_path, root)))  # type: ignore[arg-type]
    env_node = os.environ.get(ENV_NODE)
    if env_node:
        candidates.append((ENV_NODE, _config_path(env_node, root)))  # type: ignore[arg-type]
    bundled = root / "tools" / "node" / "node.exe"
    if bundled.is_file():
        candidates.append(("repo tools/node/node.exe", bundled))
    return [(label, path) for label, path in candidates if path is not None], "explicit"


def _check_node(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    candidates, _ = _node_candidates(root, config)
    for label, path in candidates:
        if path.is_file():
            return _ok("Node 运行时：%s（来源：%s）" % (_display(path, root), label))
    return _missing(
        "未找到 Node.js 运行时（检查 config.external_tools.node / LE_NODE / 仓库 tools/node/node.exe）",
        _CONFIG_HINT,
    )


def _check_tool_native_harness(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    tool_cfg = _tool_native_config(config)
    raw = tool_cfg.get("harness_script") or os.environ.get(ENV_TOOL_NATIVE_HARNESS)
    if not raw:
        return _undetermined(
            "未配置 tool-native harness 脚本路径（无法判定本机是否有已验证 harness）",
            _CONFIG_HINT + " " + _TOOL_NATIVE_HINT,
        )
    path = _config_path(raw, root)
    if path and path.is_file():
        return _ok("harness：%s" % _display(path, root))
    return _missing(
        "tool-native harness 不存在：%s" % (_display(path, root) if path else str(raw)),
        "检查 config.external_tools.tool_native.harness_script；harness 用 Node vm 沙箱加载工具 planner.js/db。"
        + _TOOL_NATIVE_HINT,
    )


def _tool_native_label_from_summary(path: pathlib.Path) -> str | None:
    try:
        parsed = _read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    tool = parsed.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return None
    # 只取 basename，避免把证据里的个人机器绝对路径带进结果
    return pathlib.PurePath(tool.replace("\\", "/")).name


def _tool_native_expected_relatives(
    root: pathlib.Path, case: dict[str, Any]
) -> tuple[list[str], str | None]:
    """从 run-summary 证据推导 tool_home 下需要的相对文件。

    返回 (相对路径列表, 证据标签)。证据缺失时返回 ([], None)，由调用方降级为
    ``data/*/db/js/*.js`` 目录检查。绝不输出/使用机器绝对路径：只取工具安装
    目录 basename 之后的相对尾巴。
    """

    raw = _source_field(case, "tool_native_run_summary")
    path = _config_path(raw, root) if raw else None
    if path is None or not path.is_file():
        return [], None
    try:
        parsed = _read_json(path)
    except (OSError, ValueError):
        return [], None
    if not isinstance(parsed, dict):
        return [], None
    tool = parsed.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return [], None
    tool_dir = pathlib.PurePath(tool.replace("\\", "/")).name
    relatives: list[str] = []
    db_files = parsed.get("dbFiles")
    if isinstance(db_files, list):
        for item in db_files:
            if not isinstance(item, str):
                continue
            normalized = item.replace("\\", "/")
            marker = "/" + tool_dir + "/"
            if marker in normalized:
                relative = normalized.split(marker, 1)[1]
            else:
                relative = normalized.rsplit("/", 1)[-1]
            if relative and relative not in relatives:
                relatives.append(relative)
    return relatives, tool_dir


def _check_tool_native_install(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = _tool_native_config(config).get("tool_home") or os.environ.get(ENV_TOOL_NATIVE_HOME)
    if not raw:
        return _undetermined(
            "未配置 lastepochsupertools 安装目录（无法判定外部 tool 是否可用）",
            _CONFIG_HINT
            + " 需要的外部产物：planner/js/planner.js 与 data/<version>/db/js/*.js"
            "（路径由该工具安装目录决定；公开版不随包分发该工具）。",
        )
    home = _config_path(raw, root)
    if home is None or not home.is_dir():
        return _missing(
            "tool-native 安装目录不存在：%s" % (_display(home, root) if home else str(raw)),
            "检查 config.external_tools.tool_native.tool_home（lastepochsupertools v1.3.1 安装目录）",
        )
    relatives, label = _tool_native_expected_relatives(root, case)
    expected = ["planner/js/planner.js"] + relatives
    missing = [item for item in expected if not (home / item).is_file()]
    if not relatives:
        # 无 run-summary 证据时至少要求存在一个版本化 DB js，不能凭“目录存在”判 ok
        db_js = sorted(home.glob("data/*/db/js/*.js"))
        if not db_js:
            return _undetermined(
                "tool-native 安装目录存在，但缺少 data/<version>/db/js/*.js 且无 run-summary 证据可核对：%s"
                % _display(home, root),
                "提供 case.source.tool_native_run_summary 证据或确认 tool_home 完整（planner/js/planner.js + data/<version>/db/js/*.js）",
            )
    if missing:
        return _missing(
            "tool-native 安装目录缺少所需文件：%s" % ", ".join(missing[:6]),
            "检查 config.external_tools.tool_native.tool_home 指向完整工具目录；"
            "期望文件由 case.source.tool_native_run_summary 证据推导（示例：planner/js/planner.js + data/<version>/db/js/*.js）。",
        )
    suffix = "（tool=%s）" % label if label else ""
    return _ok("tool-native 安装目录：%s%s" % (_display(home, root), suffix))


def _check_planner_payload(
    dep: Dependency, root: pathlib.Path, config: dict[str, Any], case: dict[str, Any],
    case_path: pathlib.Path | None, main_skill: str | None, route_id: str | None,
) -> dict[str, Any]:
    raw = _source_field(case, "planner_build_response")
    if not raw:
        return _missing(
            "case.source.planner_build_response 未声明",
            "在 case.source.planner_build_response 写归档 planner payload（LE Tools build_data 响应），"
            "或 prepare 显式传 --planner；不得 fallback 到存档路线",
        )
    path = _config_path(raw, root)
    if not path or not path.is_file():
        return _missing(
            "planner payload 不存在：%s" % (_display(path, root) if path else raw),
            "检查 case.source.planner_build_response（离线归档 payload，不重新联网抓取）",
        )
    try:
        parsed = _read_json(path)
    except (OSError, ValueError) as exc:
        return _missing(
            "planner payload 不是合法 JSON：%s（%s）"
            % (_display(path, root), _sanitize_text(str(exc))),
            "提供归档的 LE Tools 构建响应 JSON（--offline 重放，不联网）",
        )
    if not isinstance(parsed, dict):
        return _missing(
            "planner payload 不是 JSON object：%s" % _display(path, root),
            "提供归档的 planner 响应（{data: ...} 结构）",
        )
    keys = ", ".join(sorted(parsed.keys())[:5])
    return _ok("planner payload：%s（keys: %s）" % (_display(path, root), keys or "-"))


_RESOLVERS: dict[str, Callable[[pathlib.Path, dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "python.interpreter": lambda root, config, case: {
        "interpreter_version": "%d.%d.%d" % (sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        "min_version": (config.get("python") or {}).get("min_version") or "3.12",
    },
    "python.module.le_save_import": _resolve_module("tools/save_import/src"),
    "python.module.le_build": _resolve_module("tools/build_import/src"),
    "python.module.le_calc": _resolve_module("tools/phase3/src"),
    "repo.project_config": _resolve_config,
    "repo.dataset_pointer": _resolve_dataset,
    "repo.affix_db": _resolve_affix_db,
    "repo.pipeline_scripts": _resolve_pipeline_scripts,
    "repo.le_commands": lambda root, config, case: {
        "path": "scripts/le_commands.py",
        "exists": (root / "scripts" / "le_commands.py").is_file(),
    },
    "input.case_config": _resolve_case_config,
    "input.main_skill": _resolve_main_skill,
    "input.character_save": _resolve_case_file("character_save"),
    "input.stash": _resolve_case_file("stash"),
    "input.tool_native_wire": _resolve_case_file("tool_native_wire"),
    "input.tool_native_run_summary": _resolve_run_summary,
    "input.planner_payload": _resolve_planner_payload,
    "external.tool_native.node": _resolve_node,
    "external.tool_native.harness": _resolve_tool_native_harness,
    "external.tool_native.install": _resolve_tool_native_install,
}


def _check_module_dep(module: str, src_rel: str) -> Callable[..., dict[str, Any]]:
    return _check_module(module, src_rel)


_CHECKERS: dict[str, Callable[..., dict[str, Any]]] = {
    "python.interpreter": _check_interpreter,
    "python.module.le_save_import": _check_module_dep("le_save_import", "tools/save_import/src"),
    "python.module.le_build": _check_module_dep("le_build", "tools/build_import/src"),
    "python.module.le_calc": _check_module_dep("le_calc", "tools/phase3/src"),
    "repo.project_config": _check_project_config,
    "repo.dataset_pointer": _check_dataset_pointer,
    "repo.affix_db": _check_affix_db,
    "repo.pipeline_scripts": _check_pipeline_scripts,
    "repo.le_commands": _check_le_commands,
    "input.case_config": _check_case_config,
    "input.main_skill": _check_main_skill,
    "input.character_save": _check_character_save,
    "input.stash": _check_stash,
    "input.tool_native_wire": _check_tool_native_wire,
    "input.tool_native_run_summary": _check_run_summary,
    "input.planner_payload": _check_planner_payload,
    "external.tool_native.node": _check_node,
    "external.tool_native.harness": _check_tool_native_harness,
    "external.tool_native.install": _check_tool_native_install,
}


def _dep(
    dep_id: str,
    kind: str,
    required_for: str,
    label: str,
    hint: str,
    *,
    routes: tuple[str, ...] = (ROUTE_ALL,),
    required: bool = True,
) -> Dependency:
    return Dependency(
        id=dep_id,
        kind=kind,
        required_for=required_for,
        label=label,
        hint=hint,
        routes=routes,
        required=required,
    )


#: 依赖目录（顺序稳定；route.dependency_ids 引用这里的 id）
DEPENDENCIES: tuple[Dependency, ...] = (
    _dep("python.interpreter", KIND_PYTHON, "route", "Python 解释器（≥ config.python.min_version）",
         "使用仓库 .venv：.\\.venv\\Scripts\\python.exe；或设置 LE_PYTHON 指向兼容版本"),
    _dep("python.module.le_save_import", KIND_PYTHON, "parse_input", "Python 模块 le_save_import",
         "确认 tools/save_import/src/le_save_import 存在，且子进程 PYTHONPATH 含 tools/save_import/src"),
    _dep("python.module.le_build", KIND_PYTHON, "normalize_build", "Python 模块 le_build",
         "确认 tools/build_import/src/le_build 存在，且子进程 PYTHONPATH 含 tools/build_import/src"),
    _dep("python.module.le_calc", KIND_PYTHON, "prepare_snapshot", "Python 模块 le_calc",
         "确认 tools/phase3/src/le_calc 存在，且子进程 PYTHONPATH 含 tools/phase3/src"),
    _dep("repo.project_config", KIND_REPO_FILE, "route", "项目配置 config/project.json",
         "恢复 config/project.json（路径仓库相对；不要写个人绝对路径）"),
    _dep("repo.dataset_pointer", KIND_LOCAL_CONFIG, "export_calc_input", "dataset 指针（config.dataset.pointer）",
         "检查 data/current.json 与其 dataset_id/database；dataset 只读，不要改写"),
    _dep("repo.affix_db", KIND_LOCAL_CONFIG, "apply_affix_ranges", "affix-db 数据库（case.affix_db）",
         "检查 case.affix_db / config.affix_db 指向的 SQLite；缺失时按既有流程重建"),
    _dep("repo.pipeline_scripts", KIND_REPO_FILE, "apply_affix_ranges", "既有 save-import pipeline 脚本",
         "恢复 tools/save_import/scripts/pipeline/ 下既有脚本；prepare 不另写解析路线"),
    _dep("repo.le_commands", KIND_REPO_FILE, "min_validate", "scripts/le_commands.py（validate 入口）",
         "恢复 scripts/le_commands.py；本任务不改它（P3 Owner 接线）"),
    _dep("input.case_config", KIND_LOCAL_CONFIG, "route", "case.json（可读、JSON object）",
         "提供存在的 case.json（仓库相对路径）；不得用缺字段的骨架继续运行"),
    _dep("input.main_skill", KIND_LOCAL_CONFIG, "resolve_skills", "主技能 ability id",
         "在 case.main_skill.ability_id 声明或 prepare --main-skill <ability-id>；不得按技能栏位置猜"),
    _dep("input.character_save", KIND_LOCAL_CONFIG, "parse_input", "离线存档 case.source.character_save",
         "检查 case.source.character_save 指向存在的 1CHARACTERSLOT_BETA_* 只读快照"),
    _dep("input.stash", KIND_LOCAL_CONFIG, "parse_input", "stash 存档（可选）",
         "移除 case.source.stash 或提供存在的 STASH_* 文件（可选输入）", required=False),
    _dep("input.tool_native_wire", KIND_LOCAL_CONFIG, "normalize_build", "归档 tool-native wire（case.source.tool_native_wire）",
         "提供归档 wire 或配置 external_tools.tool_native 由存档生成；不得用 planner payload 冒充 wire",
         routes=(routes.SAVE_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)),
    _dep("input.tool_native_run_summary", KIND_LOCAL_CONFIG, "normalize_build",
         "tool-native harness 运行证据（可选 case.source.tool_native_run_summary）",
         "提供 run-summary 证据（tool/dbFiles），用于核对 harness 工具/DB 版本",
         routes=(routes.SAVE_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID), required=False),
    _dep("external.tool_native.node", KIND_EXTERNAL_TOOL, "normalize_build", "Node.js 运行时（tool-native harness）",
         _CONFIG_HINT, routes=(routes.SAVE_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)),
    _dep("external.tool_native.harness", KIND_EXTERNAL_TOOL, "normalize_build", "tool-native harness 脚本",
         _CONFIG_HINT, routes=(routes.SAVE_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)),
    _dep("external.tool_native.install", KIND_EXTERNAL_TOOL, "normalize_build", "lastepochsupertools v1.3.1 安装目录",
         _CONFIG_HINT, routes=(routes.SAVE_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)),
    _dep("input.planner_payload", KIND_LOCAL_CONFIG, "normalize_build", "planner payload（case.source.planner_build_response）",
         "提供归档 planner payload，或 prepare --planner；不得 fallback 到存档路线",
         routes=(routes.PLANNER_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)),
)

_DEPENDENCY_BY_ID: dict[str, Dependency] = {dep.id: dep for dep in DEPENDENCIES}


def get_dependency(dep_id: str) -> Dependency | None:
    return _DEPENDENCY_BY_ID.get(str(dep_id))


def dependency_catalog() -> list[dict[str, Any]]:
    """返回依赖目录的 JSON 描述（doctor / prepare / 文档消费）。"""

    return [dep.to_dict() for dep in DEPENDENCIES]


# ---------------------------------------------------------------------------
# 动态 required 判定 + check_routes
# ---------------------------------------------------------------------------


def _wire_archived(case: dict[str, Any]) -> bool:
    return bool(_source_field(case, "tool_native_wire"))


def _is_required(dep: Dependency, route: routes.RouteSpec, case: dict[str, Any]) -> bool:
    rid = route.route_id
    if dep.id == "input.case_config":
        return True
    if dep.id == "input.main_skill":
        return route.requires_main_skill
    if dep.id == "input.planner_payload":
        # planner-payload 与混合来源 save+planner 都需要 planner payload
        return rid in (routes.PLANNER_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)
    if dep.id == "input.character_save":
        # save 与混合来源都需要存档；planner-payload 单来源不含存档（可选）
        return rid in (routes.SAVE_ROUTE_ID, routes.SAVE_PLANNER_ROUTE_ID)
    if dep.id == "input.stash":
        return False
    if dep.id == "input.tool_native_wire":
        return _wire_archived(case)
    if dep.id == "input.tool_native_run_summary":
        return False
    if dep.id in _EXTERNAL_SAVE_IDS:
        # 只有 save 路线且归档 wire 缺失时才需要 harness；
        # 混合来源用 planner payload 出 BuildSpec，harness 一律可选（保持 undetermined 语义）。
        return rid == routes.SAVE_ROUTE_ID and not _wire_archived(case)
    return dep.required


def _load_case_ref(
    case_ref: Any, root: pathlib.Path
) -> tuple[dict[str, Any] | None, pathlib.Path | None, str | None]:
    if isinstance(case_ref, dict):
        return case_ref, None, None
    if isinstance(case_ref, (str, bytes)) or hasattr(case_ref, "__fspath__"):
        path = pathlib.Path(case_ref)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            return None, path, "case 文件不存在：%s" % _display(path, root)
        try:
            parsed = _read_json(path)
        except (OSError, ValueError) as exc:
            return None, path, "case.json 不可解析：%s（%s）" % (
                _display(path, root), _sanitize_text(str(exc))
            )
        if not isinstance(parsed, dict):
            return None, path, "case.json 不是 JSON object：%s" % _display(path, root)
        return parsed, path, None
    return None, None, "case_ref 必须是 case 字典或仓库相对 case.json 路径"


def _case_display(case_ref: Any, root: pathlib.Path) -> str:
    if isinstance(case_ref, dict):
        case_id = case_ref.get("case_id")
        return "inline:%s" % (case_id if isinstance(case_id, str) and case_id else "<case>")
    if isinstance(case_ref, (str, bytes)) or hasattr(case_ref, "__fspath__"):
        path = pathlib.Path(case_ref)
        if not path.is_absolute():
            path = root / path
        return _display(path, root)
    return "<case>"


def _route_notes(route: routes.RouteSpec) -> list[str]:
    return [
        "stage_sequence（准备阶段）：%s" % " → ".join(route.stage_sequence),
        "fallback_policy=%s：缺少依赖时返回 needs_input，不得静默改用另一条输入路线。" % route.fallback_policy,
    ]


def check_routes(
    case_ref: Any,
    root: str | pathlib.Path | None = None,
    *,
    source_kind: str | None = None,
    main_skill: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """检查一条 case 的输入路线与外部依赖，返回结构化结果（不抛异常）。

    参数
    ----
    case_ref:
        case 字典，或仓库相对的 ``case.json`` 路径。
    root:
        仓库根（``str``/``Path``）；省略时取本文件推导的仓库根。
    source_kind:
        可选的显式路线提示：``character_save`` / ``planner_payload``（也接受
        ``save`` / ``planner``）。用于 case 尚未含归档来源字段的新输入。
    main_skill:
        可选显式主技能 ability id（``prepare --main-skill``）；缺省时读
        ``case.main_skill.ability_id``。
    config:
        可选已加载的 ``config/project.json`` 内容；缺省时从 root 读取。

    返回（``le-pipeline-route-check-v1``）::

        {
          "schema": "le-pipeline-route-check-v1",
          "case_ref": "examples/<case>/case.json",
          "case_id": "flame-reave",
          "route_id": "save-tool-native-wire" | None,
          "route": {route_id, label, input_kind, requires_main_skill,
                     required_input_fields, stage_sequence, fallback_policy} | None,
          "route_reason": "...",
          "ready": true/false,
          "dependencies": [{id, kind, required_for, label, required,
                             status, detail, hint?}],
          "needs_input": [{code, kind, dependency_ids, message, hint, required}],
          "warnings": [{code, dependency_id, message}],
          "fallback_policy": "never_fallback",
          "notes": [...]
        }
    """

    root_path = pathlib.Path(root) if root is not None else pathlib.Path(__file__).resolve().parents[2]
    result: dict[str, Any] = {
        "schema": "le-pipeline-route-check-v1",
        "case_ref": _case_display(case_ref, root_path),
        "case_id": None,
        "route_id": None,
        "route": None,
        "route_reason": None,
        "ready": False,
        "dependencies": [],
        "needs_input": [],
        "warnings": [],
        "fallback_policy": routes.FALLBACK_POLICY_NEVER,
        "notes": [
            "检查只读：不执行命令、不写文件、不联网；结果中只出现仓库相对路径或 <external>/<basename>。",
        ],
    }
    try:
        case, case_path, case_error = _load_case_ref(case_ref, root_path)
        if case is not None:
            case_id = case.get("case_id")
            if isinstance(case_id, str):
                result["case_id"] = case_id
        detail = routes.resolve_route_detail(case if case is not None else case_ref, source_kind=source_kind)
        route = detail["route"]
        result["route_reason"] = detail["reason"]
        if route is None:
            candidates = detail.get("candidates") or list(routes.ROUTE_IDS)
            if case_error:
                result["needs_input"].append(
                    {
                        "code": "case_unreadable",
                        "kind": "local_config",
                        "dependency_ids": ["input.case_config"],
                        "message": case_error,
                        "hint": "提供可读的 case.json（仓库相对路径、JSON object）；不要用缺字段骨架继续运行",
                        "required": True,
                    }
                )
            else:
                result["needs_input"].append(
                    {
                        "code": "route_selection",
                        "kind": "local_config",
                        "dependency_ids": ["input.case_config"],
                        "message": "无法从 case 判定输入路线（reason=%s）" % detail["reason"],
                        "hint": (
                            "在 case.source 声明受支持来源（character_save / planner_build_response / "
                            "tool_native_wire），或 prepare 显式指定 --save/--planner（source_kind）；"
                            "候选路线：%s。不得按优先级猜。"
                            % ", ".join(candidates)
                        ),
                        "required": True,
                    }
                )
            result["notes"].extend(_route_notes_for_unknown())
            return result

        result["route_id"] = route.route_id
        result["route"] = routes.route_brief(route)
        result["notes"].extend(_route_notes(route))

        if config is None:
            config = _load_config_or_empty(root_path)

        required_issues = 0
        for dep_id in route.dependency_ids:
            dep = _DEPENDENCY_BY_ID.get(dep_id)
            if dep is None:  # pragma: no cover - catalog 自检会在测试覆盖
                result["warnings"].append(
                    {
                        "code": "unknown_dependency_id",
                        "dependency_id": dep_id,
                        "message": "route.dependency_ids 引用了未登记的依赖 id：%s" % dep_id,
                    }
                )
                continue
            required = _is_required(dep, route, case or {})
            checked = dep.check(
                root=root_path,
                config=config,
                case=case or {},
                case_path=case_path,
                main_skill=main_skill,
                route_id=route.route_id,
            )
            item = {
                "id": dep.id,
                "kind": dep.kind,
                "required_for": dep.required_for,
                "label": dep.label,
                "required": required,
                "status": checked.get("status", STATUS_UNDETERMINED),
                "detail": checked.get("detail", ""),
            }
            if item["status"] != STATUS_OK:
                item["hint"] = checked.get("hint", dep.hint)
            result["dependencies"].append(item)

            if item["status"] == STATUS_OK:
                continue
            message = "%s：%s" % (dep.label, item["detail"])
            if required:
                required_issues += 1
                code = "missing_dependency"
                if item["status"] == STATUS_UNDETERMINED:
                    code = "undetermined_dependency"
                if dep.id == "input.main_skill":
                    code = "main_skill_required"
                result["needs_input"].append(
                    {
                        "code": code,
                        "kind": dep.kind,
                        "dependency_ids": [dep.id],
                        "message": message,
                        "hint": item.get("hint", dep.hint),
                        "required": True,
                    }
                )
            else:
                result["warnings"].append(
                    {
                        "code": (
                            "optional_dependency_missing"
                            if item["status"] == STATUS_MISSING
                            else "optional_dependency_undetermined"
                        ),
                        "dependency_id": dep.id,
                        "message": message,
                    }
                )
        result["ready"] = required_issues == 0 and not result["needs_input"]
        if not result["ready"]:
            result["notes"].append(
                "未 ready：处理 needs_input 后重跑 prepare；不提供自动降级路线。"
            )
        return result
    except Exception as exc:  # pragma: no cover - defensive: 检查失败不得抛未捕获异常
        result["ready"] = False
        result["needs_input"].append(
            {
                "code": "internal_error",
                "kind": "local_config",
                "dependency_ids": [],
                "message": _sanitize_text("依赖检查内部错误：%s: %s" % (type(exc).__name__, exc)),
                "hint": "记录该错误并检查 root/config/case 输入；不得据此改用其它输入路线",
                "required": True,
            }
        )
        return result


def _route_notes_for_unknown() -> list[str]:
    return [
        "路线未判定：prepare 必须要求显式来源（--save/--planner 或 case.source 归档字段），"
        "不得静默选择某一条路线。",
    ]


def _load_config_or_empty(root: pathlib.Path) -> dict[str, Any]:
    path = root / "config" / "project.json"
    if not path.is_file():
        return {}
    try:
        parsed = _read_json(path)
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "DEPENDENCIES",
    "Dependency",
    "ENV_NODE",
    "ENV_TOOL_NATIVE_HARNESS",
    "ENV_TOOL_NATIVE_HOME",
    "KINDS",
    "STATUSES",
    "check_routes",
    "dependency_catalog",
    "get_dependency",
]
