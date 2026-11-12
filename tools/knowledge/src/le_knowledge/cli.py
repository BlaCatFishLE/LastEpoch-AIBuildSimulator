# -*- coding: utf-8 -*-
"""``le.ps1 knowledge`` 的独立 CLI（也可用 ``python -m le_knowledge`` 直接调用）。

默认输出人可读短摘要；``--json`` 输出机器可读结果（便于验收脚本消费）。
退出码沿用仓库约定：0 成功 / 1 失败（校验、未找到） / 2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import api
from .errors import (
    KnowledgeError,
    KnowledgeIndexError,
    KnowledgeNotFoundError,
    KnowledgeQueryError,
    KnowledgeValidationError,
)
from .paths import DEFAULT_INDEX_PATH, KNOWN_NAMESPACES
from .query import QUERY_KINDS

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2


def _info(message: str) -> None:
    print(f"[信息] {message}", flush=True)


def _warn(message: str) -> None:
    print(f"[警告] {message}", flush=True)


def _fail(message: str) -> None:
    print(f"[失败] {message}", file=sys.stderr, flush=True)


def _heading(message: str) -> None:
    print(f"\n== {message} ==", flush=True)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _configure_stdout() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - best effort
        pass


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _load_query_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise KnowledgeQueryError(f"无法读取查询文件：{exc}", pointer="--query-json") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeQueryError(f"查询文件不是合法 JSON：{exc}", pointer="--query-json") from exc
    if not isinstance(document, dict):
        raise KnowledgeQueryError("查询文件必须是 le-mechanism-query-v1 JSON 对象", pointer="--query-json")
    return document


def _build_query(args: argparse.Namespace) -> dict[str, Any]:
    if args.query_json:
        if args.query:
            raise KnowledgeQueryError("--query-json 与 --query 不能同时使用", pointer="--query")
        query = _load_query_json(args.query_json)
        if args.limit is not None:
            query["limit"] = args.limit
        return query
    if not args.query and not any(
        [args.component, args.tag, args.node, args.condition, args.namespace]
    ):
        raise KnowledgeQueryError(
            "需要 --query <词或ID>，或至少一个过滤条件（--component/--tag/--node/--condition/--namespace）",
            pointer="$",
        )
    queries = []
    for index, term in enumerate(args.query):
        queries.append(
            {
                "query_id": f"q{index + 1}",
                "term": term,
                "kind": args.kind,
            }
        )
    filters = {
        "component": list(args.component or []),
        "tags": list(args.tag or []),
        "node_ids": list(args.node or []),
        "conditions": list(args.condition or []),
    }
    query: dict[str, Any] = {"queries": queries, "filters": filters}
    if args.namespace:
        namespaces = []
        for namespace in args.namespace:
            if namespace not in namespaces:
                namespaces.append(namespace)
        query["namespaces"] = namespaces
    if args.limit is not None:
        query["limit"] = args.limit
    return query


def _format_conditions(entry: dict[str, Any]) -> str:
    conditions = [item for item in entry.get("conditions") or [] if item != "none"]
    return ", ".join(conditions) if conditions else "—"


def _render_search_result(result: dict[str, Any]) -> None:
    summary = result.get("summary") or {}
    index_state = result.get("index_state") or {}
    _heading("le knowledge search — 知识查询（短摘要）")
    filters = summary.get("filters_applied") or {}
    filter_text = "；".join(
        f"{key}={'/'.join(map(str, value))}" for key, value in filters.items()
    )
    _info(
        f"查询 {summary.get('query_count')} 条"
        + (f"；过滤 {filter_text}" if filter_text else "")
        + f"；索引 {index_state.get('index_path') or DEFAULT_INDEX_PATH}"
        + f"（source_digest {result.get('index_digest') or '?'}，"
        f"keyword_backend={summary.get('keyword_backend')}）"
    )
    if index_state.get("rebuilt"):
        _info(f"索引已自动重建：{index_state.get('counts')}")
    for warning in result.get("warnings") or []:
        _warn(f"{warning.get('path')}：{warning.get('message')}")

    entries = result.get("entries") or []
    print()
    if not entries:
        print("（无命中条目）")
    else:
        print(f"命中条目（{len(entries)}/{summary.get('entry_count_total')}）：")
        for index, entry in enumerate(entries, start=1):
            kind = entry.get("entry_kind")
            title = entry.get("title") or {}
            title_text = title.get("zh") or title.get("en") or ""
            print(
                f"{index}. [{kind}] {entry.get('entry_id')}"
                + (f"  「{title_text}」" if title_text else "")
                + f"  rev={entry.get('revision')}"
            )
            applicability = entry.get("applicability") or {}
            missing = applicability.get("missing_conditions") or []
            missing_text = f"（缺 {', '.join(map(str, missing))}）" if missing else ""
            print(
                f"   状态：evidence={entry.get('evidence_state')} "
                f"implementation={entry.get('implementation_state')}；"
                f"适用性：{applicability.get('status')}{missing_text}"
            )
            records = entry.get("matched_queries") or []
            if records:
                parts = [
                    f"{record.get('query_id')}={record.get('match_quality')}"
                    f"（selector: {record.get('selector')}；reason: {record.get('match_reason')}）"
                    for record in records
                ]
                print("   命中：" + "；".join(parts))
            else:
                print("   命中：（过滤条件命中，无查询项）")
            components = ", ".join(map(str, entry.get("components") or [])) or "—"
            tags = ", ".join(map(str, entry.get("tags") or [])) or "—"
            print(f"   组件：{components}；标签：{tags}")
            selectors = entry.get("selectors") or []
            if selectors:
                shown = "、".join(map(str, selectors[:5]))
                suffix = f" … +{len(selectors) - 5}" if len(selectors) > 5 else ""
                print(f"   选择器：{shown}{suffix}")
            rule_refs = entry.get("rule_refs") or []
            if rule_refs:
                refs = [
                    f"{reference.get('kind')}:{reference.get('ref')}"
                    for reference in rule_refs
                    if reference.get("kind") or reference.get("ref")
                ]
                print(f"   规则：{'；'.join(map(str, refs))}")
            implementations = entry.get("implementation_refs") or []
            if implementations:
                refs = [
                    f"{item.get('module')}.{item.get('symbol')}"
                    if item.get("symbol")
                    else f"{item.get('module')}({item.get('block')})"
                    for item in implementations
                ]
                print(f"   实现：{'；'.join(map(str, refs))}")
            print(f"   来源：{(entry.get('source') or {}).get('path')}")
            print()

    unmatched = result.get("unmatched_queries") or []
    if unmatched:
        print(f"未匹配查询（{len(unmatched)}）：")
        for item in unmatched:
            print(f"  - {item.get('query_id')} {item.get('reason')}：{item.get('term')}")
        print()
    print(
        f"摘要：match_quality={summary.get('match_quality')}（仅摘要，不参与自动绑定）；"
        f"binding_eligible={summary.get('binding_eligible_count')}；"
        f"返回 {summary.get('entry_count')} 条（上限 {summary.get('limit')}）"
    )
    if summary.get("note"):
        print(f"说明：{summary['note']}")


def cmd_search(args: argparse.Namespace) -> int:
    query = _build_query(args)
    result = api.search(
        query,
        knowledge_dir=args.knowledge_dir,
        index_path=args.index,
        root=args.root,
    )
    if args.json:
        _print_json(result)
    else:
        _render_search_result(result)
    return EXIT_OK


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _render_show(detail: dict[str, Any]) -> None:
    _heading("le knowledge show — 条目详情")
    title = detail.get("title") or {}
    title_text = title.get("zh") or ""
    english = f" / {title['en']}" if title.get("en") else ""
    _info(
        f"{detail.get('entry_id')}（{detail.get('entry_kind')}，revision {detail.get('revision')}）"
    )
    print(f"标题：{title_text}{english}")
    states = detail.get("states") or {}
    applicability = detail.get("applicability") or {}
    missing = applicability.get("missing_conditions") or []
    print(
        f"状态：evidence={states.get('evidence_state')} implementation={states.get('implementation_state')}；"
        f"适用性（无查询上下文）：{applicability.get('status')}"
        + (f"（缺 {', '.join(map(str, missing))}）" if missing else "")
    )
    source = detail.get("source") or {}
    print(f"来源：{source.get('path')}（sha256 {str(source.get('sha256') or '')[:16]}…）")
    selectors = detail.get("selectors") or []
    print(
        "选择器："
        + ("；".join(f"{item.get('selector')}" for item in selectors) if selectors else "—")
    )
    aliases = detail.get("aliases") or []
    if aliases:
        print("别名：" + "；".join(map(str, aliases)))
    if detail.get("ability_id"):
        print(f"ability_id：{detail['ability_id']}")
    print(f"适用组件：{', '.join(map(str, detail.get('components') or [])) or '—'}")
    print(f"标签：{', '.join(map(str, detail.get('tags') or [])) or '—'}")
    print(f"条件：{_format_conditions(detail)}")
    required_nodes = detail.get("required_nodes") or []
    if required_nodes:
        print("必要节点：" + "；".join(map(str, required_nodes)))
    rule_refs = detail.get("rule_refs") or []
    if rule_refs:
        print("规则引用：")
        for reference in rule_refs:
            print(
                f"  - {reference.get('kind')}:{reference.get('ref')}"
                + (f"（{reference.get('path')}）" if reference.get("path") else "")
            )
    evidence_refs = detail.get("evidence_refs") or []
    if evidence_refs:
        print("证据引用：")
        for reference in evidence_refs:
            print(f"  - {reference.get('kind')}:{reference.get('locator')}")
    implementations = detail.get("implementation_refs") or []
    if implementations:
        print("实现入口：")
        for item in implementations:
            text = f"{item.get('module')}"
            if item.get("symbol"):
                text += f".{item['symbol']}"
            if item.get("block"):
                text += f"（block={item['block']}）"
            print(f"  - {text}")
    unresolved = detail.get("unresolved") or []
    if unresolved:
        print("未解决项：")
        for item in unresolved:
            print(f"  - {item}")
    case_scope = detail.get("case_scope") or []
    if case_scope:
        print("适用案例：" + "、".join(map(str, case_scope)))
    related_skills = detail.get("related_skills") or []
    if related_skills:
        print(
            "关联技能模板："
            + "；".join(
                f"{item.get('skill_id')}（{item.get('selector')}）" for item in related_skills
            )
        )
    related_mechanisms = detail.get("related_mechanisms") or []
    if related_mechanisms:
        print(
            "关联机制："
            + "；".join(
                f"{item.get('mechanism_id')}（{item.get('selector')}）"
                + ("" if item.get("exists", True) else "［缺失］")
                for item in related_mechanisms
            )
        )
    for warning in detail.get("warnings") or []:
        _warn(f"{warning.get('path')}：{warning.get('message')}")


def cmd_show(args: argparse.Namespace) -> int:
    detail = api.show(
        args.id,
        knowledge_dir=args.knowledge_dir,
        index_path=args.index,
        root=args.root,
    )
    if args.json:
        _print_json(detail)
    else:
        _render_show(detail)
    return EXIT_OK


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


def cmd_rebuild(args: argparse.Namespace) -> int:
    if args.check:
        state = api.index_state(
            knowledge_dir=args.knowledge_dir,
            index_path=args.index,
            root=args.root,
        )
        if args.json:
            _print_json(state)
        else:
            _heading("le knowledge rebuild --check — 索引状态检查")
            _info(
                f"状态：{state.get('status')}；索引 {state.get('index_path') or DEFAULT_INDEX_PATH}"
                f"（source_digest {state.get('source_digest')}）"
            )
            if state.get("status") != "ok":
                print("索引缺失、损坏或内容指纹已变化：需要运行 knowledge rebuild。")
        return EXIT_OK if state.get("status") == "ok" else EXIT_FAIL

    report = api.rebuild(
        knowledge_dir=args.knowledge_dir,
        index_path=args.index,
        root=args.root,
        use_fts=False if args.no_fts else None,
    )
    if args.json:
        _print_json(report)
        return EXIT_OK
    _heading("le knowledge rebuild — 重建知识索引")
    counts = report.get("counts") or {}
    _info(
        "已加载内容文件 "
        f"{counts.get('files')} 个（mechanisms {counts.get('mechanisms')} / "
        f"skills {counts.get('skill_templates')} / schema {counts.get('schema_files')}）；"
        f"条目 {counts.get('entries')}"
    )
    tables = report.get("tables") or {}
    _info(
        f"查询索引：{report['index']['path']}"
        f"（FTS5={'可用' if report['index']['fts'] else '不可用，已回退 LIKE'}；"
        f"selectors {tables.get('selectors')} / aliases {tables.get('aliases')} / terms {tables.get('terms')}）"
    )
    _info(
        f"Markdown 索引：{report['index_md']['path']}"
        f"（{'内容有变化，已写入' if report['index_md']['changed'] else '内容未变化，未改写'}）"
    )
    _info(f"source_digest={report['source_digest']}")
    for warning in report.get("warnings") or []:
        _warn(f"{warning.get('path')}：{warning.get('message')}")
    print("\nrebuild：OK")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser / main
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """向 ``knowledge`` 子命令挂载 search/show/rebuild 子解析器。"""
    sub = parser.add_subparsers(dest="knowledge_command")

    search = sub.add_parser(
        "search",
        help="按 ID/别名/关键词/组件/节点/条件查询机制与技能模板（默认短摘要）",
        description="查询知识索引；默认输出短摘要，详情用 knowledge show --id 获取。",
    )
    search.add_argument(
        "--query",
        action="append",
        default=[],
        metavar="TERM",
        help="查询词或带命名空间 ID（如 node:fr11mv:16；可重复表示批量查询）",
    )
    search.add_argument(
        "--query-json",
        default=None,
        metavar="FILE",
        help="完整 le-mechanism-query-v1 JSON 查询文件（与 --query 互斥）",
    )
    search.add_argument(
        "--kind",
        default="auto",
        choices=["auto", *QUERY_KINDS],
        help="查询类型（默认 auto：带命名空间按 ID 推断，否则 keyword）",
    )
    search.add_argument("--component", action="append", default=[], metavar="NAME", help="按作用组件过滤")
    search.add_argument("--tag", action="append", default=[], metavar="TAG", help="按标签过滤")
    search.add_argument(
        "--node", action="append", default=[], metavar="NS:ID", help="按所选节点过滤（必须带命名空间）"
    )
    search.add_argument(
        "--condition", action="append", default=[], metavar="CODE", help="按条件 code 过滤"
    )
    search.add_argument(
        "--namespace",
        action="append",
        default=[],
        choices=list(KNOWN_NAMESPACES),
        help="限制选择器命名空间（可重复）",
    )
    search.add_argument("--limit", type=int, default=None, metavar="N", help="返回条目上限（默认 50）")
    search.add_argument("--json", action="store_true", help="输出机器可读 JSON 结果")
    _add_path_arguments(search)
    search.set_defaults(handler=cmd_search)

    show = sub.add_parser(
        "show",
        help="按 ID 查看条目详情（规则/证据/实现/未解决项）",
        description="按 ID 查看机制或技能模板详情（完整权威 JSON 见 --json 输出的 document 字段）。",
    )
    show.add_argument("--id", required=True, metavar="ID", help="机制 ID 或技能模板 ID")
    show.add_argument("--json", action="store_true", help="输出机器可读 JSON 结果")
    _add_path_arguments(show)
    show.set_defaults(handler=cmd_show)

    rebuild = sub.add_parser(
        "rebuild",
        help="从 JSON 权威源重建 SQLite 索引与 INDEX.generated.md",
        description="重建 data/cache/knowledge.sqlite 与 knowledge/INDEX.generated.md；内容未变时幂等。",
    )
    rebuild.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    rebuild.add_argument(
        "--check",
        action="store_true",
        help="只检查索引是否需要重建（不写文件；需要重建时退出码 1）",
    )
    rebuild.add_argument(
        "--no-fts", action="store_true", help="强制使用 LIKE 回退（无 FTS5 环境的行为验证）"
    )
    _add_path_arguments(rebuild)
    rebuild.set_defaults(handler=cmd_rebuild)


def _add_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--index",
        default=None,
        metavar="PATH",
        help=f"显式 SQLite 索引路径（默认 {DEFAULT_INDEX_PATH}）",
    )
    parser.add_argument(
        "--knowledge-dir",
        default=None,
        metavar="DIR",
        help="显式知识资产根目录（默认 knowledge/）",
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help="显式仓库根（用于解析默认路径/显示相对路径；默认由配置或包位置推断）",
    )


def build_parser(prog: str = "le.ps1 knowledge") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="知识索引与查询：JSON 权威源 → 可重建 SQLite 索引 → 短摘要/详情（不依赖编排层）",
    )
    add_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdout()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 的参数错误/--help 统一转成退出码
        return int(exc.code) if isinstance(exc.code, int) else EXIT_ARGS
    if not getattr(args, "knowledge_command", None):
        parser.print_help()
        return EXIT_ARGS
    try:
        return int(args.handler(args))
    except KnowledgeQueryError as exc:
        _fail(str(exc))
        return EXIT_ARGS
    except KnowledgeValidationError as exc:
        _fail(exc._format().split("\n")[0])
        for item in exc.errors:
            print(
                f"  {item.get('path')}: {item.get('pointer')}: {item.get('message')}",
                file=sys.stderr,
            )
        return EXIT_FAIL
    except KnowledgeNotFoundError as exc:
        _fail(str(exc))
        return EXIT_FAIL
    except KnowledgeIndexError as exc:
        _fail(str(exc))
        return EXIT_FAIL
    except KnowledgeError as exc:
        _fail(str(exc))
        return EXIT_FAIL
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\n已中断。", file=sys.stderr)
        return EXIT_FAIL


__all__ = ["add_arguments", "build_parser", "main"]
