# -*- coding: utf-8 -*-
"""``scripts/le_pipeline`` — P3 prepare 的输入路线与依赖描述。

公开 API（供 ``scripts/le_commands.py`` 的 prepare/doctor 接线；本包不修改
``le_commands.py``、不执行任何命令、不写文件）：

* :data:`ROUTES` / :func:`list_routes` / :func:`get_route` /
  :func:`resolve_route` / :func:`resolve_route_detail`：三条输入路线的描述与判定
  （save-tool-native-wire / save+planner 混合 / planner-payload）；
* :data:`DEPENDENCIES` / :func:`dependency_catalog` / :func:`get_dependency`：
  依赖目录（kind = python / repo_file / external_tool / local_config）；
* :func:`check_routes`：``check_routes(case_ref, root)`` 结构化检查（ready /
  needs_input / warnings），缺失依赖给可操作 hint，不静默换路线。

典型接线（详见 ``scripts/le_pipeline/README.md``）::

    from le_pipeline import check_routes, list_routes, resolve_route

    route = resolve_route(case)              # RouteSpec | None
    report = check_routes("examples/<case>/case.json", repo_root)
    if not report["ready"]:
        # 把 report["needs_input"] 原样交给调用方询问用户/补输入
        ...
    for stage in route.stage_sequence:       # 由 prepare 编排逐阶段执行
        ...
"""

from __future__ import annotations

from .dependencies import (
    DEPENDENCIES,
    ENV_NODE,
    ENV_TOOL_NATIVE_HARNESS,
    ENV_TOOL_NATIVE_HOME,
    KINDS,
    STATUSES,
    Dependency,
    check_routes,
    dependency_catalog,
    get_dependency,
)
from .routes import (
    CommandSpec,
    FALLBACK_POLICY_NEVER,
    PLACEHOLDER_TOKENS,
    PLANNER_ROUTE_ID,
    ROUTE_IDS,
    SAVE_PLANNER_ROUTE_ID,
    ROUTES,
    RouteSpec,
    SAVE_ROUTE_ID,
    StageStep,
    get_route,
    list_routes,
    resolve_route,
    resolve_route_detail,
    route_brief,
    route_from_source_kind,
    route_to_dict,
)

__all__ = [
    "CommandSpec",
    "DEPENDENCIES",
    "Dependency",
    "ENV_NODE",
    "ENV_TOOL_NATIVE_HARNESS",
    "ENV_TOOL_NATIVE_HOME",
    "FALLBACK_POLICY_NEVER",
    "KINDS",
    "PLACEHOLDER_TOKENS",
    "PLANNER_ROUTE_ID",
    "ROUTE_IDS",
    "ROUTES",
    "SAVE_PLANNER_ROUTE_ID",
    "RouteSpec",
    "SAVE_ROUTE_ID",
    "STATUSES",
    "StageStep",
    "check_routes",
    "dependency_catalog",
    "get_dependency",
    "get_route",
    "list_routes",
    "resolve_route",
    "resolve_route_detail",
    "route_brief",
    "route_from_source_kind",
    "route_to_dict",
]
