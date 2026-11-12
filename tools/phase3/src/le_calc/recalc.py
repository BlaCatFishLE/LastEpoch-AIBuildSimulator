# -*- coding: utf-8 -*-
"""Shared local-recompute contract (``le-recalc-request-v1`` / ``le-recalc-response-v1``).

This module is the single implementation of the LIVE recompute semantics used by
both model services (:mod:`le_calc.serve` for Black Hole and
:mod:`le_calc.hit_serve` for Flame Reave) and by the CLI wiring
(``scripts/le_commands.py``).  It has no dependency on the damage engine, the
renderer or the CLI, so it can be unit-tested directly.

Contract references
-------------------

* :file:`docs/contracts/workflow-interface.md` §10.3 (full scenario request +
  two-way binding), §10.4 (3-A: endpoint source, report selection, cache
  boundary), §10.5 (numeric cache vs report cache).
* :file:`docs/contracts/player-report-ui.md` §3 LIVE-01..08.

Hard rules implemented here
---------------------------

* **Endpoint parameterisation (3-A)**: the recompute endpoint is computed from
  the *actual* bound port; both the initial page and every recompute response
  carry the actual endpoint.  The default ``127.0.0.1:8765/recompute`` is only a
  configuration default, never a JS constant.
* **Strict validation**: unknown request/scenario fields, illegal stack counts,
  wrong case/model/capabilities version, stale fingerprints and stale
  ``client_edit_revision`` are rejected with structured ``issues[]`` — they are
  never silently coerced to 0 or ignored.
* **Sequence guard (LIVE-07)**: :class:`RevisionTracker` rejects requests whose
  ``client_edit_revision`` is not strictly newer than the last accepted one and
  echoes ``accepted_revision`` so the front end can discard older responses.
* **Cache boundary**: :func:`numeric_cache_key` deliberately excludes the
  endpoint/port; :func:`report_cache_key` includes it.  Changing the port must
  only invalidate the report cache, never the numeric cache.
* **No research/report publication**: responses are computed in memory; the
  service never overwrites a historical run directory.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

SCHEMA_RECALC_REQUEST = "le-recalc-request-v1"
SCHEMA_RECALC_RESPONSE = "le-recalc-response-v1"
SCHEMA_RECALC_BINDINGS = "le-recalc-bindings-v1"
SCHEMA_REPORT_REF = "le-report-ref-v1"
SCHEMA_MODEL_CAPABILITIES = "le-model-capabilities-v1"
SCHEMA_REPORT_CONFIG = "le-report-config-v1"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RECOMPUTE_PATH = "/recompute"

COMMANDS = ("prepare", "compute", "recompute")

#: Page/editor session id: per page load, generated client-side (crypto.randomUUID).
#: Optional for backward compatibility; missing ids use an isolated one-shot scope.
CLIENT_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
LEGACY_SESSION_PREFIX = "legacy-"

#: Exact top-level request fields (unknown fields are rejected, not ignored).
REQUEST_FIELDS = (
    "schema_version",
    "request_id",
    "client_edit_revision",
    "client_session_id",
    "case_id",
    "model_id",
    "capabilities_version",
    "report_ref",
    "input_fingerprints",
    "scenario",
    "command",
)

#: Required fingerprint keys in every request (two-way binding, §10.3).
FINGERPRINT_KEYS = ("calc_input", "ruleset", "snapshot", "dataset")

#: Response fields written by this module.
RESPONSE_FIELDS = (
    "schema_version",
    "request_id",
    "client_edit_revision",
    "client_session_id",
    "client_session_legacy",
    "accepted_revision",
    "accepted",
    "issues",
    "case_id",
    "model_id",
    "capabilities_version",
    "input_fingerprints",
    "report_ref",
    "recompute_endpoint",
    "command",
    "applied_scenario",
    "applied_scenario_digest",
    "result_digest",
    "html",
    "computed_at",
)

_SEVERITIES = frozenset({"error", "warning", "info"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ENDPOINT_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[A-Za-z0-9._:\[\]-]+):(?P<port>\d{1,5})(?P<path>/[^\s?#]*)?$"
)


class RecalcError(ValueError):
    """Raised for malformed arguments that are programming errors (not requests)."""


class RecalcRejected(Exception):
    """A request/scenario was rejected; carries structured ``issues[]``."""

    def __init__(self, issues: Sequence[Mapping[str, Any]], *, reason: str = "") -> None:
        self.issues = [dict(issue) for issue in issues]
        self.reason = reason or (str(self.issues[0].get("message")) if self.issues else "请求被拒绝")
        super().__init__(self.reason)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_document(document: Any) -> str:
    return sha256_text(canonical_json(document))


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def make_issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    field: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    related_ids: Optional[Iterable[str]] = None,
    retryable: bool = False,
) -> dict[str, Any]:
    """Issue shape shared with the workflow contract / StageResult issues."""
    if severity not in _SEVERITIES:
        raise RecalcError(f"invalid issue severity: {severity!r}")
    payload = dict(details or {})
    if field:
        payload["field"] = field
    return {
        "code": code,
        "severity": severity,
        "message": f"{field}: {message}" if field else message,
        "related_ids": list(related_ids or []),
        "retryable": bool(retryable),
        "required_input": None,
        "details": payload,
    }


def _fingerprint_map(value: Any, field: str, issues: list[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        issues.append(make_issue("invalid_fingerprints", "input_fingerprints 必须是对象", field=field))
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key = str(key)
        if key not in FINGERPRINT_KEYS:
            issues.append(
                make_issue(
                    "unknown_fingerprint",
                    f"未知指纹字段 {key!r}（允许：{', '.join(FINGERPRINT_KEYS)}）",
                    field=f"{field}.{key}",
                )
            )
            continue
        if not is_sha256(item):
            issues.append(
                make_issue("invalid_fingerprint", "指纹必须是 64 位 sha256 十六进制", field=f"{field}.{key}")
            )
            continue
        result[key] = str(item)
    for key in FINGERPRINT_KEYS:
        if key not in result:
            issues.append(
                make_issue("missing_fingerprint", "缺少必需指纹", field=f"{field}.{key}")
            )
    return result


# ---------------------------------------------------------------------------
# endpoint helpers (3-A)
# ---------------------------------------------------------------------------


def loopback_endpoint(port: int, *, host: str = DEFAULT_HOST, path: str = RECOMPUTE_PATH) -> str:
    """Build the recompute endpoint for an actual bound loopback port."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RecalcError(f"invalid loopback port: {port!r}")
    if host not in _LOOPBACK_HOSTS:
        raise RecalcError(f"recompute service must bind loopback, got {host!r}")
    if not path.startswith("/"):
        raise RecalcError(f"endpoint path must start with '/': {path!r}")
    if path != RECOMPUTE_PATH:
        raise RecalcError(f"recompute endpoint path must be {RECOMPUTE_PATH!r}, got {path!r}")
    return f"http://{host}:{int(port)}{path}"


def parse_endpoint(endpoint: str) -> dict[str, Any]:
    """Parse and validate a loopback recompute endpoint; raises RecalcError."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise RecalcError("recompute endpoint must be a non-empty string")
    match = _ENDPOINT_RE.match(endpoint.strip())
    if not match:
        raise RecalcError(f"invalid recompute endpoint: {endpoint!r}")
    scheme = match.group("scheme")
    host = match.group("host").strip("[]")
    port = int(match.group("port"))
    path = match.group("path") or RECOMPUTE_PATH
    if scheme != "http":
        raise RecalcError(f"only http loopback endpoints are supported, got {scheme!r}")
    if host not in _LOOPBACK_HOSTS:
        raise RecalcError(f"only loopback endpoints are supported, got host {host!r}")
    if not 1 <= port <= 65535:
        raise RecalcError(f"invalid endpoint port: {port}")
    if not path.startswith("/"):
        raise RecalcError(f"invalid endpoint path: {path!r}")
    if path != RECOMPUTE_PATH:
        raise RecalcError(f"recompute endpoint path must be {RECOMPUTE_PATH!r}, got {path!r}")
    return {
        "url": f"http://{host}:{port}{path}",
        "scheme": scheme,
        "host": host,
        "port": port,
        "path": path,
    }


def endpoint_from_server_address(server_address: Sequence[Any], *, path: str = RECOMPUTE_PATH) -> str:
    """Compute the actual endpoint from a bound ``(host, port)`` server address."""
    host = str(server_address[0])
    if host in ("::", "0.0.0.0"):
        host = DEFAULT_HOST
    port = int(server_address[1])
    return loopback_endpoint(port, host=host, path=path)


def endpoint_port(endpoint: str) -> int:
    return int(parse_endpoint(endpoint)["port"])


# ---------------------------------------------------------------------------
# report_ref (3-A §10.4.3: bind run/case/model/fingerprints/endpoint)
# ---------------------------------------------------------------------------


def build_report_ref(
    *,
    case_id: str,
    model_id: str,
    run_id: str,
    run_dir: str,
    report_path: str,
    input_fingerprints: Mapping[str, str],
    recompute_endpoint: str,
    ui_contract_version: str,
    renderer_fingerprint: str,
    capabilities_path: Optional[str] = None,
    canonical: bool = False,
) -> dict[str, Any]:
    """Build a validated ``report_ref`` payload (all paths repository-relative)."""
    endpoint = parse_endpoint(recompute_endpoint)["url"]
    ref: dict[str, Any] = {
        "schema_version": SCHEMA_REPORT_REF,
        "case_id": str(case_id),
        "model_id": str(model_id),
        "run_id": str(run_id),
        "run_dir": str(run_dir).replace("\\", "/"),
        "report_path": str(report_path).replace("\\", "/"),
        "input_fingerprints": {str(k): str(v) for k, v in input_fingerprints.items()},
        "recompute_endpoint": endpoint,
        "ui_contract_version": str(ui_contract_version),
        "renderer_fingerprint": str(renderer_fingerprint),
        "capabilities_path": str(capabilities_path).replace("\\", "/") if capabilities_path else None,
        "canonical": bool(canonical),
    }
    validate_report_ref(ref)
    return ref


def validate_report_ref(ref: Any) -> dict[str, Any]:
    """Validate a ``report_ref``; raises :class:`RecalcRejected`."""
    issues: list[dict[str, Any]] = []
    if not isinstance(ref, Mapping):
        raise RecalcRejected([make_issue("invalid_report_ref", "report_ref 必须是对象", field="$.report_ref")])
    required = (
        "schema_version",
        "case_id",
        "model_id",
        "run_id",
        "run_dir",
        "report_path",
        "input_fingerprints",
        "recompute_endpoint",
        "ui_contract_version",
        "renderer_fingerprint",
    )
    for name in required:
        if name not in ref or ref[name] in (None, ""):
            issues.append(make_issue("invalid_report_ref", "缺少字段", field=f"$.report_ref.{name}"))
    if ref.get("schema_version") not in (None, SCHEMA_REPORT_REF):
        issues.append(
            make_issue(
                "invalid_report_ref",
                f"schema_version 应为 {SCHEMA_REPORT_REF}，实际 {ref.get('schema_version')!r}",
                field="$.report_ref.schema_version",
            )
        )
    for name in ("case_id", "model_id", "run_id", "ui_contract_version", "renderer_fingerprint"):
        value = ref.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(make_issue("invalid_report_ref", "必须是非空字符串", field=f"$.report_ref.{name}"))
    for name in ("run_dir", "report_path"):
        value = ref.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(make_issue("invalid_report_ref", "必须是非空相对路径", field=f"$.report_ref.{name}"))
        elif isinstance(value, str) and (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
            issues.append(
                make_issue("non_portable_path", "report_ref 路径必须是仓库相对路径", field=f"$.report_ref.{name}")
            )
    endpoint = ref.get("recompute_endpoint")
    if endpoint is not None:
        try:
            parse_endpoint(str(endpoint))
        except RecalcError as exc:
            issues.append(
                make_issue("invalid_endpoint", str(exc), field="$.report_ref.recompute_endpoint")
            )
    if ref.get("input_fingerprints") is not None:
        _fingerprint_map(ref.get("input_fingerprints"), "$.report_ref.input_fingerprints", issues)
    if issues:
        raise RecalcRejected(issues)
    return dict(ref)


def report_ref_digest(ref: Mapping[str, Any]) -> str:
    """Stable digest of the binding fields of a report_ref."""
    binding = {
        key: ref.get(key)
        for key in (
            "case_id",
            "model_id",
            "run_id",
            "run_dir",
            "report_path",
            "input_fingerprints",
            "ui_contract_version",
            "renderer_fingerprint",
        )
    }
    return digest_document(binding)


# ---------------------------------------------------------------------------
# page bindings (``le-recalc-bindings-v1``) — LIVE-04/07 data for the page JS
# ---------------------------------------------------------------------------

#: Exact keys embedded in ``<script id="le-recalc-bindings" type="application/json">``.
RECALC_BINDINGS_FIELDS = (
    "schema_version",
    "case_id",
    "model_id",
    "capabilities_version",
    "ui_contract_version",
    "renderer_fingerprint",
    "report_ref",
    "input_fingerprints",
    "recompute_endpoint",
)


def build_recalc_bindings(
    *,
    case_id: str,
    model_id: str,
    capabilities_version: str,
    ui_contract_version: str,
    renderer_fingerprint: str,
    report_ref: Mapping[str, Any],
    input_fingerprints: Mapping[str, str],
    recompute_endpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Build the page bindings payload consumed by the shared recompute JS.

    The page JS uses it to (a) build a complete ``le-recalc-request-v1`` body and
    (b) validate the response binding (LIVE-04); ``client_edit_revision`` /
    ``accepted_revision`` ordering (LIVE-07) is tracked client-side.  The same
    payload is embedded offline/with service so a shipped page always has the
    report identity it was generated from.
    """
    validated_ref = validate_report_ref(report_ref)
    endpoint = recompute_endpoint or validated_ref.get("recompute_endpoint")
    if endpoint is not None:
        endpoint = parse_endpoint(str(endpoint))["url"]
    # NOTE: report_ref.recompute_endpoint records the endpoint the report was
    # generated with; the page bindings always carry the *actual* bound endpoint
    # (a run can legitimately be served on a different port after explicit
    # re-render/selection, and the HTTP handler re-validates report_ref identity).
    fingerprints = {str(key): str(value) for key, value in dict(input_fingerprints).items()}
    issues: list[dict[str, Any]] = []
    for key in FINGERPRINT_KEYS:
        if not is_sha256(fingerprints.get(key)):
            issues.append(
                make_issue("invalid_fingerprint", "绑定指纹必须是 sha256", field=f"$.input_fingerprints.{key}")
            )
    ref_fingerprints = validated_ref.get("input_fingerprints") or {}
    if ref_fingerprints and fingerprints:
        for key in FINGERPRINT_KEYS:
            if str(ref_fingerprints.get(key)) != str(fingerprints.get(key)):
                issues.append(
                    make_issue(
                        "binding_mismatch",
                        f"入口指纹 {key} 与 report_ref.input_fingerprints 不一致",
                        field=f"$.input_fingerprints.{key}",
                    )
                )
    if str(validated_ref.get("case_id")) != str(case_id):
        issues.append(make_issue("binding_mismatch", "case_id 与 report_ref 不一致", field="$.case_id"))
    if str(validated_ref.get("model_id")) != str(model_id):
        issues.append(make_issue("binding_mismatch", "model_id 与 report_ref 不一致", field="$.model_id"))
    if str(validated_ref.get("ui_contract_version")) != str(ui_contract_version):
        issues.append(
            make_issue(
                "binding_mismatch",
                "ui_contract_version 与 report_ref 不一致",
                field="$.ui_contract_version",
            )
        )
    if str(validated_ref.get("renderer_fingerprint")) != str(renderer_fingerprint):
        issues.append(
            make_issue(
                "binding_mismatch",
                "renderer_fingerprint 与 report_ref 不一致",
                field="$.renderer_fingerprint",
            )
        )
    if not isinstance(capabilities_version, str) or not capabilities_version.strip():
        issues.append(
            make_issue("invalid_binding", "capabilities_version 必须是非空字符串", field="$.capabilities_version")
        )
    if not is_sha256(renderer_fingerprint):
        issues.append(
            make_issue("invalid_binding", "renderer_fingerprint 必须是 sha256", field="$.renderer_fingerprint")
        )
    if endpoint is None:
        issues.append(
            make_issue("invalid_binding", "缺少 recompute_endpoint", field="$.recompute_endpoint")
        )
    if issues:
        raise RecalcRejected(issues)
    return {
        "schema_version": SCHEMA_RECALC_BINDINGS,
        "case_id": str(case_id),
        "model_id": str(model_id),
        "capabilities_version": str(capabilities_version),
        "ui_contract_version": str(ui_contract_version),
        "renderer_fingerprint": str(renderer_fingerprint),
        "report_ref": dict(validated_ref),
        "input_fingerprints": {key: fingerprints[key] for key in FINGERPRINT_KEYS},
        "recompute_endpoint": endpoint,
    }


def validate_recalc_bindings(bindings: Any) -> dict[str, Any]:
    """Validate a ``le-recalc-bindings-v1`` payload; raises :class:`RecalcRejected`."""
    issues: list[dict[str, Any]] = []
    if not isinstance(bindings, Mapping):
        raise RecalcRejected([make_issue("invalid_bindings", "页面绑定必须是 JSON 对象")])
    if bindings.get("schema_version") != SCHEMA_RECALC_BINDINGS:
        issues.append(
            make_issue(
                "invalid_bindings",
                f"schema_version 必须是 {SCHEMA_RECALC_BINDINGS}",
                field="$.schema_version",
            )
        )
    for name in ("case_id", "model_id", "capabilities_version", "ui_contract_version"):
        value = bindings.get(name)
        if not isinstance(value, str) or not value.strip():
            issues.append(make_issue("invalid_bindings", "缺少必填字段", field=f"$.{name}"))
    if not is_sha256(bindings.get("renderer_fingerprint")):
        issues.append(make_issue("invalid_bindings", "必须是 sha256", field="$.renderer_fingerprint"))
    try:
        rebuild = build_recalc_bindings(
            case_id=str(bindings.get("case_id") or ""),
            model_id=str(bindings.get("model_id") or ""),
            capabilities_version=str(bindings.get("capabilities_version") or ""),
            ui_contract_version=str(bindings.get("ui_contract_version") or ""),
            renderer_fingerprint=str(bindings.get("renderer_fingerprint") or ""),
            report_ref=bindings.get("report_ref"),
            input_fingerprints=bindings.get("input_fingerprints") or {},
            recompute_endpoint=bindings.get("recompute_endpoint"),
        )
    except RecalcRejected as exc:
        issues.extend(exc.issues)
        rebuild = None
    if rebuild is not None and set(bindings) - set(RECALC_BINDINGS_FIELDS):
        issues.append(
            make_issue(
                "unknown_binding_field",
                "页面绑定包含未声明字段：" + ", ".join(sorted(set(bindings) - set(RECALC_BINDINGS_FIELDS))),
                field="$",
            )
        )
    if issues:
        raise RecalcRejected(issues)
    assert rebuild is not None
    return rebuild


def bindings_digest(bindings: Mapping[str, Any]) -> str:
    return digest_document(dict(bindings))


# ---------------------------------------------------------------------------
# cache keys / file cache (3-A §10.4.7 / §10.5)
# ---------------------------------------------------------------------------


def numeric_cache_key(
    *,
    model_id: str,
    capabilities_version: str,
    input_fingerprints: Mapping[str, str],
    scenario_digest: str,
    implementation_fingerprints: Optional[Mapping[str, str]] = None,
) -> str:
    """Numeric cache key: input/rules/implementation + scenario + capabilities.

    Deliberately **excludes** the endpoint/port: changing the port must not
    invalidate computed numbers.
    """
    payload = {
        "kind": "numeric",
        "model_id": str(model_id),
        "capabilities_version": str(capabilities_version),
        "input_fingerprints": {
            str(key): str(value) for key, value in sorted(dict(input_fingerprints).items())
        },
        "implementation_fingerprints": {
            str(key): str(value)
            for key, value in sorted(dict(implementation_fingerprints or {}).items())
        },
        "scenario_digest": str(scenario_digest),
    }
    return sha256_text(canonical_json(payload))


def report_cache_key(
    *,
    numeric_key: str,
    endpoint: str,
    ui_contract_version: str,
    renderer_fingerprint: str,
    capabilities_digest: Optional[str] = None,
    result_digest: Optional[str] = None,
    bindings_digest: Optional[str] = None,
) -> str:
    """Report cache key: numeric key + endpoint/port + UI/renderer/bindings fingerprints.

    ``result_digest`` covers the rendered document itself (run id / generated-at
    are part of the result), so two runs with the same numbers never share an
    HTML cache entry.  ``bindings_digest`` covers the embedded page binding block
    (report_ref/endpoint/capabilities version), so a page-binding change
    invalidates the HTML cache as well.  The endpoint/port is part of the
    *report* key only.
    """
    payload = {
        "kind": "report",
        "numeric_key": str(numeric_key),
        "endpoint": parse_endpoint(endpoint)["url"],
        "ui_contract_version": str(ui_contract_version),
        "renderer_fingerprint": str(renderer_fingerprint),
        "capabilities_digest": str(capabilities_digest or ""),
        "result_digest": str(result_digest or ""),
        "bindings_digest": str(bindings_digest or ""),
    }
    return sha256_text(canonical_json(payload))


def default_cache_root(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / "cache" / "reports"


class ReportCache:
    """Small generated-report cache (``data/cache/reports/<key>.html``).

    Only regenerable HTML lands here; historical run directories are never
    overwritten (the caller writes the run's own artifact separately).
    """

    def __init__(self, root: str | Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = bool(enabled)

    def path_for(self, key: str) -> Path:
        if not re.match(r"^[0-9a-f]{64}$", str(key)):
            raise RecalcError(f"invalid cache key: {key!r}")
        return self.root / f"{key}.html"

    def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        path = self.path_for(key)
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - cache is optional
            return None

    def put(self, key: str, html: str) -> Path:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(html, encoding="utf-8", newline="\n")
        temporary.replace(path)
        return path


# ---------------------------------------------------------------------------
# revision / sequence guard (LIVE-07)
# ---------------------------------------------------------------------------


class RevisionTracker:
    """Session-scoped accepted-revision guard (R8 / LIVE-07).

    * ``client_edit_revision`` must be a non-negative integer and strictly newer
      than the last accepted revision **within the same client session**; older
      or duplicate edits are rejected as ``stale_edit_revision``.
    * Sessions are page-load scoped ids (``client_session_id``), so two tabs, a
      refreshed page or two different reports never reject each other's edits.
    * Requests without a session id (old clients) get an isolated one-shot scope
      derived from ``request_id`` — they never share a global edit history.  The
      server marks such responses ``client_session_legacy: true``; LIVE-07's
      old-response protection then relies on the client upgrading.
    * Rejected requests do not advance the accepted revision.
    """

    #: Key used when neither session id nor request id is supplied (unit tests).
    DEFAULT_SCOPE = "__default__"

    def __init__(self, *, run_id: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self.run_id: Optional[str] = run_id
        self._accepted: dict[str, int] = {}
        self._last_request_id: dict[str, Optional[str]] = {}

    # -- scope resolution ------------------------------------------------
    @staticmethod
    def legacy_scope_id(request_id: Optional[str]) -> str:
        token = str(request_id or "anonymous")
        return LEGACY_SESSION_PREFIX + sha256_text(token)[:16]

    def resolve_scope(
        self,
        session_id: Optional[str],
        *,
        request_id: Optional[str] = None,
    ) -> tuple[str, str, bool]:
        """Return ``(scope_key, public_session_id, legacy)`` for one request."""
        if session_id:
            return str(session_id), str(session_id), False
        public_id = self.legacy_scope_id(request_id)
        return public_id, public_id, True

    # -- queries ---------------------------------------------------------
    @property
    def accepted_revision(self) -> Optional[int]:
        """Maximum accepted revision across all sessions (diagnostic only)."""
        with self._lock:
            return max(self._accepted.values()) if self._accepted else None

    @property
    def last_request_id(self) -> Optional[str]:
        with self._lock:
            if not self._last_request_id:
                return None
            latest = max(
                self._accepted.items(), key=lambda item: item[1]
            )[0]
            return self._last_request_id.get(latest)

    def accepted_revision_for(
        self,
        session_id: Optional[str],
        *,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        scope, _, _ = self.resolve_scope(session_id, request_id=request_id)
        with self._lock:
            return self._accepted.get(scope)

    def session_count(self) -> int:
        with self._lock:
            return len(self._accepted)

    # -- protocol --------------------------------------------------------
    def check(
        self,
        revision: Any,
        *,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return a rejection issue dict, or ``{}`` when the revision may proceed."""
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return make_issue(
                "invalid_edit_revision",
                "client_edit_revision 必须是 >= 0 的整数",
                field="$.client_edit_revision",
            )
        scope, public_id, legacy = self.resolve_scope(session_id, request_id=request_id)
        with self._lock:
            accepted = self._accepted.get(scope)
        if accepted is not None and revision <= accepted:
            details: dict[str, Any] = {
                "accepted_revision": accepted,
                "client_edit_revision": revision,
                "client_session_id": public_id,
            }
            if legacy:
                details["client_session_legacy"] = True
            return make_issue(
                "stale_edit_revision",
                f"client_edit_revision={revision} 不大于该会话（{public_id}）已接受的编辑版本 "
                f"{accepted}（旧响应可能覆盖新结果，已拒绝）",
                field="$.client_edit_revision",
                details=details,
                retryable=True,
            )
        return {}

    def accept(
        self,
        revision: int,
        *,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        scope, _, _ = self.resolve_scope(session_id, request_id=request_id)
        with self._lock:
            accepted = self._accepted.get(scope)
            if accepted is None or int(revision) > accepted:
                accepted = int(revision)
                self._accepted[scope] = accepted
            self._last_request_id[scope] = request_id
            return accepted

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sessions = {
                scope: {
                    "accepted_revision": revision,
                    "last_request_id": self._last_request_id.get(scope),
                }
                for scope, revision in self._accepted.items()
            }
            global_accepted = max(self._accepted.values()) if self._accepted else None
        return {
            "run_id": self.run_id,
            "scope": "run_id + client_session_id",
            "accepted_revision": global_accepted,
            "session_count": len(sessions),
            "sessions": sessions,
            "legacy_session_policy": "missing client_session_id => isolated request_id scope",
        }


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------


def _capabilities_version_of(capabilities: Mapping[str, Any]) -> Optional[str]:
    """Return the declared capabilities version (``recalculation.capabilities_version``)."""
    recalculation = capabilities.get("recalculation")
    if isinstance(recalculation, Mapping):
        value = recalculation.get("capabilities_version")
        if isinstance(value, str) and value:
            return value
    value = capabilities.get("capabilities_version")
    return value if isinstance(value, str) and value else None


def _capabilities_list(capabilities: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = capabilities.get(key)
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _allowed_state_keys(capabilities: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for control in _capabilities_list(capabilities, "buff_controls"):
        state_key = control.get("state_key") or control.get("state_id")
        if isinstance(state_key, str) and state_key:
            states[state_key] = dict(control)
    for field in _capabilities_list(capabilities, "target_fields"):
        field_id = field.get("field") or field.get("state_key")
        if isinstance(field_id, str) and field_id.startswith("target."):
            states.setdefault(field_id, {**dict(field), "kind": "bool", "target_field": True})
    return states


def _coerce_number(value: Any, kind: str) -> Optional[float]:
    if kind in ("int", "integer", "stacks", "number", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if kind in ("int", "integer", "stacks") and not float(value).is_integer():
            return None
        return float(value)
    return None


def _validate_value_against_declaration(
    value: Any,
    declaration: Mapping[str, Any],
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> None:
    kind = str(declaration.get("kind") or declaration.get("type") or "").lower()
    if kind in ("bool", "boolean", "toggle"):
        if not isinstance(value, bool):
            issues.append(make_issue("invalid_scenario_value", "该控件应为布尔值", field=field))
        return
    if kind in ("int", "integer", "stacks", "number", "float"):
        number = _coerce_number(value, kind)
        if number is None:
            issues.append(make_issue("invalid_scenario_value", f"该控件应为 {kind} 数值", field=field))
            return
        minimum = declaration.get("min", declaration.get("minimum"))
        maximum = declaration.get("max", declaration.get("maximum"))
        if isinstance(minimum, (int, float)) and number < minimum:
            issues.append(
                make_issue(
                    "invalid_stack_count" if kind == "stacks" else "out_of_range",
                    f"数值 {value!r} 小于下限 {minimum!r}",
                    field=field,
                    details={"min": minimum, "max": maximum},
                )
            )
        if isinstance(maximum, (int, float)) and number > maximum:
            issues.append(
                make_issue(
                    "invalid_stack_count" if kind == "stacks" else "out_of_range",
                    f"数值 {value!r} 超过上限 {maximum!r}",
                    field=field,
                    details={"min": minimum, "max": maximum},
                )
            )
        return
    if kind in ("enum", "string"):
        allowed = declaration.get("enum") or declaration.get("allowed")
        if isinstance(allowed, list) and value not in allowed:
            issues.append(
                make_issue("invalid_scenario_value", f"值 {value!r} 不在允许集合内", field=field, details={"allowed": allowed})
            )
        return
    if kind == "list":
        if not isinstance(value, list):
            issues.append(make_issue("invalid_scenario_value", "该控件应为数组", field=field))


def _validate_states(
    states: Any,
    capabilities: Mapping[str, Any],
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> None:
    if states is None:
        return
    if not isinstance(states, Mapping):
        issues.append(make_issue("invalid_scenario", "states 必须是对象", field=field))
        return
    declarations = _allowed_state_keys(capabilities)
    for key, value in states.items():
        key = str(key)
        pointer = f"{field}.{key}"
        declaration = declarations.get(key)
        if declaration is None:
            issues.append(
                make_issue(
                    "unknown_scenario_field",
                    f"未声明的 Buff/状态字段 {key!r}（不在 capabilities.buff_controls 中，拒绝静默忽略）",
                    field=pointer,
                )
            )
            continue
        if declaration.get("readonly"):
            default = declaration.get("default")
            if value != default:
                issues.append(
                    make_issue(
                        "readonly_state",
                        "该状态只读（由报告输入决定），不允许重算修改",
                        field=pointer,
                    )
                )
            continue
        _validate_value_against_declaration(value, declaration, field=pointer, issues=issues)


def _validate_target(
    target: Any,
    capabilities: Mapping[str, Any],
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> None:
    if target is None:
        return
    if not isinstance(target, Mapping):
        issues.append(make_issue("invalid_scenario", "target 必须是对象", field=field))
        return
    declarations = {
        str(item.get("field") or item.get("state_key")): dict(item)
        for item in _capabilities_list(capabilities, "target_fields")
        if item.get("field") or item.get("state_key")
    }
    for key, value in target.items():
        key = str(key)
        pointer = f"{field}.{key}"
        declaration = declarations.get(key)
        if declaration is None:
            issues.append(
                make_issue(
                    "unknown_scenario_field",
                    f"未声明的目标字段 {key!r}（拒绝静默忽略）",
                    field=pointer,
                )
            )
            continue
        _validate_value_against_declaration(value, declaration, field=pointer, issues=issues)


def _validate_model_parameters(
    parameters: Any,
    capabilities: Mapping[str, Any],
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> None:
    if parameters is None:
        return
    if not isinstance(parameters, Mapping):
        issues.append(make_issue("invalid_scenario", "model_parameters 必须是对象", field=field))
        return
    declarations = {
        str(item.get("key") or item.get("parameter")): dict(item)
        for item in _capabilities_list(capabilities, "model_parameters")
        if item.get("key") or item.get("parameter")
    }
    for key, value in parameters.items():
        key = str(key)
        pointer = f"{field}.{key}"
        declaration = declarations.get(key)
        if declaration is None:
            issues.append(
                make_issue(
                    "unknown_scenario_field",
                    f"未声明的模型参数 {key!r}（拒绝静默忽略）",
                    field=pointer,
                )
            )
            continue
        _validate_value_against_declaration(value, declaration, field=pointer, issues=issues)


def _validate_mode_policy(
    mode_policy: Any,
    capabilities: Mapping[str, Any],
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> None:
    if mode_policy is None:
        return
    if not isinstance(mode_policy, Mapping):
        issues.append(make_issue("invalid_scenario", "mode_policy 必须是对象", field=field))
        return
    allowed_keys = {"mode", "policy", "action_policy", "window_s", "windows_s", "dps_policy"}
    modes = [str(item) for item in capabilities.get("modes") or [] if isinstance(item, str)]
    windows = [item for item in capabilities.get("windows") or []]
    for key, value in mode_policy.items():
        key = str(key)
        pointer = f"{field}.{key}"
        if key not in allowed_keys:
            issues.append(
                make_issue("unknown_scenario_field", f"未声明的模式策略字段 {key!r}", field=pointer)
            )
            continue
        if key in ("mode", "policy", "action_policy", "dps_policy"):
            if modes and str(value) not in modes:
                issues.append(
                    make_issue(
                        "invalid_mode",
                        f"模式 {value!r} 不在能力声明 {modes} 内",
                        field=pointer,
                        details={"allowed": modes},
                    )
                )
        elif key in ("window_s", "windows_s"):
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                if windows and item not in windows:
                    issues.append(
                        make_issue(
                            "invalid_window",
                            f"窗口 {item!r} 不在能力声明 {windows} 内",
                            field=f"{pointer}[{index}]",
                            details={"allowed": windows},
                        )
                    )


def _declared_scenario_fields(capabilities: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return ``(required, allowed)`` top-level scenario fields."""
    declaration = capabilities.get("scenario_fields")
    required: list[str] = []
    allowed: list[str] = []
    if isinstance(declaration, Mapping):
        required = [str(item) for item in declaration.get("required") or []]
        allowed = [str(item) for item in declaration.get("allowed") or []]
    if not allowed:
        allowed = [str(item) for item in capabilities.get("scenario_field_names") or []]
    if not allowed:
        allowed = [
            "baseline_profile",
            "target",
            "states",
            "mode_policy",
            "model_parameters",
        ]
    return required, allowed


def _validate_legacy_field(
    name: str,
    value: Any,
    capabilities: Mapping[str, Any],
    *,
    field: str,
    issues: list[dict[str, Any]],
) -> None:
    """Type/range validation for the current report JS's legacy scenario keys."""
    declarations = capabilities.get("legacy_scenario_fields")
    spec = declarations.get(name) if isinstance(declarations, Mapping) else None
    kind = str((spec or {}).get("kind") or "object")
    if kind == "string":
        if not isinstance(value, str) or not value.strip():
            issues.append(make_issue("invalid_scenario_value", "必须是字符串", field=field))
        return
    if kind == "list":
        if not isinstance(value, list):
            issues.append(make_issue("invalid_scenario", f"{name} 必须是数组", field=field))
        return
    if kind == "effect_map":
        if not isinstance(value, Mapping):
            issues.append(make_issue("invalid_scenario", "effects 必须是对象", field=field))
            return
        for effect_id, edit in value.items():
            pointer = f"{field}.{effect_id}"
            if not isinstance(edit, Mapping):
                issues.append(make_issue("invalid_scenario_value", "效果编辑必须是对象", field=pointer))
                continue
            for key, item in edit.items():
                key = str(key)
                if key not in ("enabled", "stacks"):
                    issues.append(
                        make_issue("unknown_scenario_field", f"未声明效果字段 {key!r}", field=f"{pointer}.{key}")
                    )
                elif key == "enabled" and not isinstance(item, bool):
                    issues.append(make_issue("invalid_scenario_value", "enabled 必须是布尔值", field=f"{pointer}.{key}"))
                elif key == "stacks":
                    number = _coerce_number(item, "stacks")
                    if number is None or number < 0:
                        issues.append(
                            make_issue(
                                "invalid_stack_count",
                                "stacks 必须是 >= 0 的整数",
                                field=f"{pointer}.{key}",
                            )
                        )
        return
    if kind == "hit_override":
        if not isinstance(value, Mapping):
            issues.append(make_issue("invalid_scenario", "hit_override 必须是对象", field=field))
            return
        limits = {
            str(item.get("key") or item.get("parameter")): dict(item)
            for item in _capabilities_list(capabilities, "model_parameters")
        }
        hits: Optional[float] = None
        for key, item in value.items():
            pointer = f"{field}.{key}"
            key = str(key)
            if key == "hits_per_cast":
                declaration = limits.get("hits_per_cast")
                if declaration is None:
                    issues.append(
                        make_issue("unknown_scenario_field", "未声明的命中次数覆盖", field=pointer)
                    )
                    continue
                hits = _coerce_number(item, "int")
                if hits is None:
                    issues.append(make_issue("invalid_scenario_value", "hits_per_cast 必须是整数", field=pointer))
                else:
                    _validate_value_against_declaration(item, declaration, field=pointer, issues=issues)
            elif key == "damage_weights":
                if not isinstance(item, list) or not item:
                    issues.append(make_issue("invalid_scenario_value", "damage_weights 必须是非空数值数组", field=pointer))
                    continue
                for index, weight in enumerate(item):
                    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
                        issues.append(
                            make_issue(
                                "invalid_scenario_value",
                                "damage_weights 必须是非负数值数组",
                                field=f"{pointer}[{index}]",
                            )
                        )
            else:
                issues.append(make_issue("unknown_scenario_field", f"未声明字段 {key!r}", field=pointer))
        if hits is not None and isinstance(value.get("damage_weights"), list):
            if len(value["damage_weights"]) != int(hits):
                issues.append(
                    make_issue(
                        "invalid_scenario_value",
                        f"damage_weights 长度 {len(value['damage_weights'])} 与 hits_per_cast={int(hits)} 不一致",
                        field=f"{field}.damage_weights",
                    )
                )
        return
    if not isinstance(value, Mapping):
        issues.append(make_issue("invalid_scenario", f"{name} 必须是对象", field=field))
        return
    # F2: nested legacy sections carry editable state maps (e.g. single_hit.states);
    # validate them with the same buff_controls rules as top-level ``states`` so
    # unsupported fields are rejected before load instead of silently dropped.
    if name in ("single_hit", "single_cast", "dps"):
        nested_states = value.get("states")
        if nested_states is not None:
            _validate_states(
                nested_states,
                capabilities,
                field=f"{field}.states",
                issues=issues,
            )


def validate_scenario(
    scenario: Any,
    capabilities: Mapping[str, Any],
    *,
    field: str = "$.scenario",
    require_complete: bool = True,
) -> dict[str, Any]:
    """Validate a full scenario against model capabilities; returns a deep copy.

    Unknown fields, illegal stack counts/values and unsupported modes/windows are
    rejected (:class:`RecalcRejected`) — never coerced to 0 or dropped.
    """
    issues: list[dict[str, Any]] = []
    if not isinstance(scenario, Mapping):
        raise RecalcRejected([make_issue("invalid_scenario", "scenario 必须是完整场景对象", field=field)])
    required, allowed = _declared_scenario_fields(capabilities)
    if require_complete:
        for name in required:
            if name not in scenario:
                issues.append(
                    make_issue(
                        "incomplete_scenario",
                        f"完整场景缺少必需字段 {name!r}",
                        field=f"{field}.{name}",
                    )
                )
    for key in scenario:
        key = str(key)
        if key not in allowed:
            issues.append(
                make_issue(
                    "unknown_scenario_field",
                    f"未声明的场景字段 {key!r}（完整场景不允许未声明字段，拒绝静默忽略）",
                    field=f"{field}.{key}",
                )
            )

    baseline_profiles = [str(item) for item in capabilities.get("baseline_profiles") or [] if isinstance(item, str)]
    baseline = scenario.get("baseline_profile")
    if baseline is not None:
        if not isinstance(baseline, str):
            issues.append(
                make_issue("invalid_scenario_value", "baseline_profile 必须是字符串", field=f"{field}.baseline_profile")
            )
        elif baseline_profiles and baseline not in baseline_profiles:
            issues.append(
                make_issue(
                    "invalid_baseline_profile",
                    f"baseline_profile {baseline!r} 不在能力声明 {baseline_profiles} 内",
                    field=f"{field}.baseline_profile",
                    details={"allowed": baseline_profiles},
                )
            )

    _validate_target(scenario.get("target"), capabilities, field=f"{field}.target", issues=issues)
    _validate_states(scenario.get("states"), capabilities, field=f"{field}.states", issues=issues)
    _validate_model_parameters(
        scenario.get("model_parameters"),
        capabilities,
        field=f"{field}.model_parameters",
        issues=issues,
    )
    _validate_mode_policy(
        scenario.get("mode_policy"), capabilities, field=f"{field}.mode_policy", issues=issues
    )
    legacy_names = set()
    declarations = capabilities.get("legacy_scenario_fields")
    if isinstance(declarations, Mapping):
        legacy_names = {str(key) for key in declarations}
    for legacy_key in legacy_names:
        if legacy_key in scenario:
            _validate_legacy_field(
                legacy_key,
                scenario[legacy_key],
                capabilities,
                field=f"{field}.{legacy_key}",
                issues=issues,
            )
    if issues:
        raise RecalcRejected(issues)
    return json.loads(json.dumps(scenario))


def validate_request(
    request: Any,
    *,
    capabilities: Mapping[str, Any],
    report_ref: Mapping[str, Any],
    expected_input_fingerprints: Mapping[str, str],
    case_id: str,
    model_id: str,
    sequence: Optional[RevisionTracker] = None,
) -> dict[str, Any]:
    """Strictly validate one ``le-recalc-request-v1`` payload.

    Returns a normalised request dict on success; raises :class:`RecalcRejected`
    with structured ``issues[]`` on any contract/binding/staleness problem.
    """
    issues: list[dict[str, Any]] = []
    if not isinstance(request, Mapping):
        raise RecalcRejected([make_issue("invalid_request", "请求体必须是 JSON 对象", field="$")])
    for key in request:
        if key not in REQUEST_FIELDS:
            issues.append(
                make_issue(
                    "unknown_request_field",
                    f"未声明的请求字段 {key!r}（拒绝静默忽略）",
                    field=f"$.{key}",
                )
            )
    if request.get("schema_version") != SCHEMA_RECALC_REQUEST:
        issues.append(
            make_issue(
                "unsupported_schema_version",
                f"schema_version 必须是 {SCHEMA_RECALC_REQUEST}，实际 {request.get('schema_version')!r}",
                field="$.schema_version",
            )
        )

    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        issues.append(make_issue("invalid_request_id", "request_id 必须是非空字符串", field="$.request_id"))
        request_id = ""

    revision = request.get("client_edit_revision")
    client_session_id: Optional[str] = None
    client_session_legacy = True
    session_raw = request.get("client_session_id")
    if session_raw is not None:
        if not isinstance(session_raw, str) or not CLIENT_SESSION_ID_PATTERN.match(session_raw):
            issues.append(
                make_issue(
                    "invalid_client_session_id",
                    "client_session_id 必须是 8..128 位字母/数字/._:- 组成的页面会话 id"
                    "（缺失时按兼容策略使用隔离的 request_id 作用域）",
                    field="$.client_session_id",
                )
            )
        else:
            client_session_id = session_raw
            client_session_legacy = False
    resolved_public_session = client_session_id or RevisionTracker.legacy_scope_id(request_id)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        issues.append(
            make_issue(
                "invalid_edit_revision",
                "client_edit_revision 必须是 >= 0 的整数",
                field="$.client_edit_revision",
            )
        )
        revision = -1
    elif sequence is not None:
        stale = sequence.check(
            revision, request_id=request_id, session_id=client_session_id
        )
        if stale:
            issues.append(stale)

    if request.get("case_id") != case_id:
        issues.append(
            make_issue(
                "case_mismatch",
                f"请求案例 {request.get('case_id')!r} 与服务案例 {case_id!r} 不一致",
                field="$.case_id",
                related_ids=[case_id],
            )
        )
    if request.get("model_id") != model_id:
        issues.append(
            make_issue(
                "model_mismatch",
                f"请求模型 {request.get('model_id')!r} 与服务模型 {model_id!r} 不一致"
                "（serve 不得回落到默认 BH 适配器）",
                field="$.model_id",
                related_ids=[model_id],
            )
        )
    capabilities_version = _capabilities_version_of(capabilities)
    if request.get("capabilities_version") != capabilities_version:
        issues.append(
            make_issue(
                "capabilities_version_mismatch",
                f"请求能力版本 {request.get('capabilities_version')!r} 与服务能力版本 "
                f"{capabilities_version!r} 不一致",
                field="$.capabilities_version",
            )
        )

    command = request.get("command")
    if command not in COMMANDS:
        issues.append(
            make_issue(
                "invalid_command",
                f"command 必须是 {', '.join(COMMANDS)} 之一，实际 {command!r}",
                field="$.command",
            )
        )

    fingerprints = _fingerprint_map(request.get("input_fingerprints"), "$.input_fingerprints", issues)
    for key in FINGERPRINT_KEYS:
        expected = str(expected_input_fingerprints.get(key) or "")
        actual = fingerprints.get(key)
        if expected and actual and actual != expected:
            issues.append(
                make_issue(
                    "fingerprint_mismatch",
                    f"输入指纹过期：{key} 与服务器冻结输入不一致（请求 {actual[:12]}…，服务 {expected[:12]}…）",
                    field=f"$.input_fingerprints.{key}",
                    retryable=False,
                )
            )

    request_report_ref = request.get("report_ref")
    try:
        validated_ref = validate_report_ref(request_report_ref)
    except RecalcRejected as exc:
        issues.extend(exc.issues)
        validated_ref = {}
    if validated_ref:
        for key in ("case_id", "model_id"):
            if str(validated_ref.get(key)) != str(report_ref.get(key)):
                issues.append(
                    make_issue(
                        "report_ref_mismatch",
                        f"report_ref.{key}={validated_ref.get(key)!r} 与当前报告 "
                        f"{report_ref.get(key)!r} 不一致",
                        field=f"$.report_ref.{key}",
                    )
                )
        if validated_ref.get("run_id") != report_ref.get("run_id"):
            issues.append(
                make_issue(
                    "report_ref_mismatch",
                    f"report_ref.run_id={validated_ref.get('run_id')!r} 与当前服务报告 "
                    f"{report_ref.get('run_id')!r} 不一致",
                    field="$.report_ref.run_id",
                )
            )

    scenario: dict[str, Any] = {}
    try:
        scenario = validate_scenario(
            request.get("scenario"),
            capabilities,
            field="$.scenario",
            require_complete=command in ("compute", "recompute"),
        )
    except RecalcRejected as exc:
        issues.extend(exc.issues)

    if issues:
        raise RecalcRejected(issues)

    return {
        "schema_version": SCHEMA_RECALC_REQUEST,
        "request_id": request_id.strip(),
        "client_edit_revision": int(revision),
        "client_session_id": resolved_public_session,
        "client_session_legacy": client_session_legacy,
        "case_id": case_id,
        "model_id": model_id,
        "capabilities_version": capabilities_version,
        "report_ref": validated_ref,
        "input_fingerprints": dict(expected_input_fingerprints),
        "scenario": scenario,
        "command": command,
    }


# ---------------------------------------------------------------------------
# legacy payload adapter (historical reports posted raw scenario JSON)
# ---------------------------------------------------------------------------


def legacy_request_from_payload(
    payload: Any,
    *,
    case_id: str,
    model_id: str,
    capabilities_version: str,
    report_ref: Mapping[str, Any],
    input_fingerprints: Mapping[str, str],
    command: str = "compute",
) -> dict[str, Any]:
    """Wrap a legacy raw-scenario POST body into a request-shaped payload.

    Historical report HTML (generated before the shared contract) posts either
    the scenario object itself (BH) or ``{"scenario": {...}}`` (FR).  Those files
    must keep working without being rewritten, so the service wraps them with a
    deterministic request id and the current accepted revision.
    """
    if isinstance(payload, Mapping) and isinstance(payload.get("scenario"), Mapping):
        scenario = dict(payload["scenario"])
    elif isinstance(payload, Mapping):
        scenario = dict(payload)
    else:
        raise RecalcRejected(
            [make_issue("invalid_request", "旧格式请求体必须是场景 JSON 对象", field="$")]
        )
    body_digest = digest_document(scenario)[:16]
    return {
        "schema_version": SCHEMA_RECALC_REQUEST,
        "request_id": f"legacy-{body_digest}",
        "client_edit_revision": 0,
        "case_id": case_id,
        "model_id": model_id,
        "capabilities_version": capabilities_version,
        "report_ref": dict(report_ref),
        "input_fingerprints": dict(input_fingerprints),
        "scenario": scenario,
        "command": command,
    }


# ---------------------------------------------------------------------------
# response helpers
# ---------------------------------------------------------------------------


def build_response(
    *,
    request: Optional[Mapping[str, Any]],
    accepted: bool,
    case_id: str,
    model_id: str,
    capabilities_version: str,
    input_fingerprints: Mapping[str, str],
    report_ref: Mapping[str, Any],
    recompute_endpoint: str,
    accepted_revision: Optional[int],
    issues: Sequence[Mapping[str, Any]] = (),
    command: Optional[str] = None,
    applied_scenario: Optional[Mapping[str, Any]] = None,
    applied_scenario_digest: Optional[str] = None,
    result_digest: Optional[str] = None,
    html: Optional[str] = None,
) -> dict[str, Any]:
    """Build a validated ``le-recalc-response-v1`` payload."""
    request = request or {}
    session_id = request.get("client_session_id")
    session_legacy = request.get("client_session_legacy")
    if not session_id:
        session_id = RevisionTracker.legacy_scope_id(str(request.get("request_id") or ""))
        session_legacy = True
    response = {
        "schema_version": SCHEMA_RECALC_RESPONSE,
        "request_id": str(request.get("request_id") or ""),
        "client_edit_revision": request.get("client_edit_revision"),
        "client_session_id": str(session_id),
        "client_session_legacy": bool(session_legacy),
        "accepted_revision": accepted_revision,
        "accepted": bool(accepted),
        "issues": [dict(issue) for issue in issues],
        "case_id": case_id,
        "model_id": model_id,
        "capabilities_version": capabilities_version,
        "input_fingerprints": dict(input_fingerprints),
        "report_ref": dict(report_ref),
        "recompute_endpoint": parse_endpoint(recompute_endpoint)["url"],
        "command": command or request.get("command"),
        "applied_scenario": dict(applied_scenario) if applied_scenario is not None else None,
        "applied_scenario_digest": applied_scenario_digest,
        "result_digest": result_digest,
        "html": html,
        "computed_at": None,
    }
    if accepted:
        if response["applied_scenario"] is None or not response["applied_scenario_digest"]:
            raise RecalcError("accepted response must carry applied_scenario + applied_scenario_digest")
        if response["result_digest"] is not None and not is_sha256(response["result_digest"]):
            raise RecalcError("result_digest must be a sha256 hex digest")
        if not isinstance(response["accepted_revision"], int) or isinstance(
            response["accepted_revision"], bool
        ):
            raise RecalcError("accepted response must carry integer accepted_revision")
    else:
        if not response["issues"]:
            raise RecalcError("rejected response must carry at least one issue")
        response["html"] = None
    return response


def validate_response(response: Any, *, request: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Client-side response validation (two-way binding + ordering + session)."""
    issues: list[dict[str, Any]] = []
    if not isinstance(response, Mapping):
        raise RecalcRejected([make_issue("invalid_response", "响应必须是 JSON 对象")])
    if response.get("schema_version") != SCHEMA_RECALC_RESPONSE:
        issues.append(
            make_issue(
                "unsupported_schema_version",
                f"response.schema_version 必须是 {SCHEMA_RECALC_RESPONSE}，实际 "
                f"{response.get('schema_version')!r}",
                field="$.schema_version",
            )
        )
    for name in ("request_id", "case_id", "model_id", "capabilities_version", "recompute_endpoint"):
        value = response.get(name)
        if not isinstance(value, str) or not value.strip():
            issues.append(make_issue("invalid_response", "缺少必填字段", field=f"$.{name}"))
    session_value = response.get("client_session_id")
    if not isinstance(session_value, str) or not session_value.strip():
        issues.append(make_issue("invalid_response", "缺少 client_session_id", field="$.client_session_id"))
    if not isinstance(response.get("client_session_legacy"), bool):
        issues.append(
            make_issue(
                "invalid_response",
                "client_session_legacy 必须是布尔值",
                field="$.client_session_legacy",
            )
        )
    try:
        parse_endpoint(str(response.get("recompute_endpoint") or ""))
    except RecalcError as exc:
        issues.append(make_issue("invalid_endpoint", str(exc), field="$.recompute_endpoint"))
    if not isinstance(response.get("accepted"), bool):
        issues.append(make_issue("invalid_response", "accepted 必须是布尔值", field="$.accepted"))
    if response.get("accepted") is False and not response.get("issues"):
        issues.append(make_issue("invalid_response", "accepted=false 必须携带 issues", field="$.issues"))
    accepted_revision = response.get("accepted_revision")
    edit_revision = response.get("client_edit_revision")
    if accepted_revision is not None and (
        not isinstance(accepted_revision, int) or isinstance(accepted_revision, bool)
    ):
        issues.append(
            make_issue(
                "invalid_response",
                "accepted_revision 必须是整数或 null",
                field="$.accepted_revision",
            )
        )
    elif (
        isinstance(accepted_revision, int)
        and isinstance(edit_revision, int)
        and accepted_revision > edit_revision
    ):
        issues.append(
            make_issue(
                "invalid_accepted_revision",
                "accepted_revision 不得大于本次请求的 client_edit_revision（服务端编辑流异常）",
                field="$.accepted_revision",
            )
        )
    if request is not None:
        for key in ("request_id", "case_id", "model_id", "capabilities_version"):
            if response.get(key) != request.get(key):
                issues.append(
                    make_issue(
                        "binding_mismatch",
                        f"响应 {key}={response.get(key)!r} 与请求 {request.get(key)!r} 不一致",
                        field=f"$.{key}",
                    )
                )
        expected_session = request.get("client_session_id")
        expected_legacy = request.get("client_session_legacy")
        if expected_legacy:
            if response.get("client_session_legacy") is not True:
                issues.append(
                    make_issue(
                        "binding_mismatch",
                        "旧客户端（无 session）响应应标记 client_session_legacy=true",
                        field="$.client_session_legacy",
                    )
                )
            if not str(response.get("client_session_id") or "").startswith(LEGACY_SESSION_PREFIX):
                issues.append(
                    make_issue(
                        "binding_mismatch",
                        f"旧客户端响应应使用隔离的 {LEGACY_SESSION_PREFIX}<hash> 会话 id",
                        field="$.client_session_id",
                    )
                )
        elif isinstance(expected_session, str) and expected_session:
            if response.get("client_session_id") != expected_session:
                issues.append(
                    make_issue(
                        "binding_mismatch",
                        "响应 client_session_id 与请求不一致（跨会话/跨标签页响应不得应用）",
                        field="$.client_session_id",
                    )
                )
            if response.get("client_session_legacy") is not False:
                issues.append(
                    make_issue(
                        "binding_mismatch",
                        "请求带 client_session_id 时响应不得标记 legacy 会话",
                        field="$.client_session_legacy",
                    )
                )
        expected_revision = request.get("client_edit_revision")
        if expected_revision is not None and response.get("client_edit_revision") != expected_revision:
            issues.append(
                make_issue(
                    "binding_mismatch",
                    "响应 client_edit_revision 与请求不一致（旧响应不得应用）",
                    field="$.client_edit_revision",
                )
            )
        if response.get("accepted") and isinstance(accepted_revision, int):
            if accepted_revision != expected_revision:
                issues.append(
                    make_issue(
                        "accepted_revision_mismatch",
                        "接受的响应 accepted_revision 必须等于该会话本次编辑版本",
                        field="$.accepted_revision",
                    )
                )
        if response.get("accepted") and response.get("applied_scenario") is not None:
            digest = digest_document(response["applied_scenario"])
            if response.get("applied_scenario_digest") != digest:
                issues.append(
                    make_issue(
                        "digest_mismatch",
                        "applied_scenario_digest 与 applied_scenario 内容不一致",
                        field="$.applied_scenario_digest",
                    )
                )
    if issues:
        raise RecalcRejected(issues)
    return dict(response)


# ---------------------------------------------------------------------------
# shared report-config contract (R3 / LIVE-04): save/load validation
# ---------------------------------------------------------------------------

#: Top-level report-config fields (``schema`` accepted as legacy alias).
REPORT_CONFIG_FIELDS = ("schema_version", "schema", "binding", "scenario")

#: Stable error codes shared with the page JS validator (same order).
CONFIG_ISSUE_CODES = (
    "config_not_object",
    "config_invalid_json",
    "config_unsupported_schema",
    "config_schema_field_legacy",
    "config_unknown_field",
    "config_invalid_binding",
    "config_binding_unknown_field",
    "config_binding_not_checked",
    "config_case_mismatch",
    "config_model_mismatch",
    "config_capabilities_version_mismatch",
    "config_input_fingerprint_mismatch",
    "config_report_ref_mismatch",
    "config_ui_contract_mismatch",
    "config_renderer_fingerprint_mismatch",
    "config_missing_scenario",
    "config_invalid_scenario",
    "config_capabilities_unavailable",
    "legacy_config_no_binding",
    "config_internal_error",
)

_BINDING_FIELDS = (
    "schema_version",
    "case_id",
    "model_id",
    "capabilities_version",
    "ui_contract_version",
    "renderer_fingerprint",
    "report_ref",
    "input_fingerprints",
    "recompute_endpoint",
)


def _config_issue(
    code: str,
    message: str,
    *,
    field: Optional[str] = None,
    severity: str = "error",
    details: Optional[Mapping[str, Any]] = None,
    retryable: bool = False,
) -> dict[str, Any]:
    issue = make_issue(code, message, field=field, details=details, retryable=retryable)
    issue["severity"] = severity
    return issue


def _binding_mapping(value: Any) -> Optional[dict[str, Any]]:
    """Normalise a RecalcBinding / report-ref-like mapping for comparison."""
    if isinstance(value, RecalcBinding):
        return {
            "case_id": value.case_id,
            "model_id": value.model_id,
            "capabilities_version": value.capabilities_version,
            "report_ref": dict(value.report_ref),
            "input_fingerprints": dict(value.input_fingerprints),
            "recompute_endpoint": value.recompute_endpoint,
        }
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in _BINDING_FIELDS:
        if key in value:
            if key in ("report_ref", "input_fingerprints"):
                result[key] = dict(value[key]) if isinstance(value[key], Mapping) else value[key]
            else:
                result[key] = value[key]
    return result


def _report_ref_compare_fields() -> tuple[str, ...]:
    return (
        "case_id",
        "model_id",
        "run_id",
        "report_path",
        "ui_contract_version",
        "renderer_fingerprint",
        "input_fingerprints",
    )


def _compare_batch(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    prefix: str,
    issues: list[dict[str, Any]],
) -> None:
    for key in FINGERPRINT_KEYS:
        expected_value = str((expected.get("input_fingerprints") or {}).get(key) or "")
        actual_value = str((actual.get("input_fingerprints") or {}).get(key) or "")
        if expected_value and actual_value and expected_value != actual_value:
            issues.append(
                _config_issue(
                    "config_input_fingerprint_mismatch",
                    f"配置绑定输入指纹 {key} 与当前输入不一致"
                    f"（配置 {actual_value[:12]}…，当前 {expected_value[:12]}…）",
                    field=f"{prefix}.input_fingerprints.{key}",
                )
            )
    expected_ref = expected.get("report_ref") or {}
    actual_ref = actual.get("report_ref") or {}
    if isinstance(expected_ref, Mapping) and isinstance(actual_ref, Mapping):
        mismatched = [
            key
            for key in _report_ref_compare_fields()
            if expected_ref.get(key) != actual_ref.get(key)
        ]
        if mismatched:
            issues.append(
                _config_issue(
                    "config_report_ref_mismatch",
                    "配置报告的 report_ref 与当前报告不一致：" + ", ".join(mismatched),
                    field=f"{prefix}.report_ref",
                    details={"fields": mismatched},
                )
            )


def validate_report_config(
    payload: Any,
    *,
    current_bindings: Any = None,
    capabilities: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate one ``le-report-config-v1`` saved/loaded file (R3).

    Never raises for bad input: returns a structured result instead::

        {
          "schema_version": "le-report-config-v1",
          "ok": bool,                  # no error-severity issues
          "legacy": bool,              # old config without a binding block
          "legacy_reason": str | None,
          "binding_checked": bool,
          "issues": [{code, severity, message, field, details, retryable}],
          "scenario": dict | None,     # normalized scenario (None when invalid)
          "binding": dict | None,      # validated binding echo
        }

    The page JS validates with the same schema file and issue codes in the same
    order (see ``tools/phase3/schemas/le-report-config-v1.json`` and the shared
    examples file), so a config accepted in the page is accepted here.
    """
    issues: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema_version": SCHEMA_REPORT_CONFIG,
        "ok": False,
        "legacy": False,
        "legacy_reason": None,
        "binding_checked": False,
        "issues": issues,
        "scenario": None,
        "binding": None,
    }
    if not isinstance(payload, Mapping):
        issues.append(_config_issue("config_not_object", "配置必须是 JSON 对象", field="$"))
        return result
    for key in payload:
        if key not in REPORT_CONFIG_FIELDS:
            issues.append(
                _config_issue("config_unknown_field", f"未声明的配置字段 {key!r}", field=f"$.{key}")
            )
    schema_value = payload.get("schema_version")
    if schema_value is None and payload.get("schema") is not None:
        schema_value = payload.get("schema")
        issues.append(
            _config_issue(
                "config_schema_field_legacy",
                "配置使用旧字段 schema，应按 le-report-config-v1 写 schema_version（本轮兼容读取）",
                field="$.schema",
                severity="warning",
            )
        )
    if schema_value != SCHEMA_REPORT_CONFIG:
        issues.append(
            _config_issue(
                "config_unsupported_schema",
                f"配置 schema_version 必须是 {SCHEMA_REPORT_CONFIG}，实际 {schema_value!r}",
                field="$.schema_version",
            )
        )

    raw_binding = payload.get("binding")
    current = _binding_mapping(current_bindings)
    expected_version = (
        _capabilities_version_of(capabilities) if isinstance(capabilities, Mapping) else None
    )
    config_binding: Optional[dict[str, Any]] = None
    if raw_binding is None:
        result["legacy"] = True
        result["legacy_reason"] = "no_binding"
        issues.append(
            _config_issue(
                "legacy_config_no_binding",
                "旧格式配置没有 binding：明确拒绝自动应用（legacy 结论；无法校验案例/输入/规则兼容性，"
                "页面应提示重新保存为 le-report-config-v1 后载入）",
                field="$.binding",
                severity="error",
            )
        )
    else:
        if not isinstance(raw_binding, Mapping):
            issues.append(
                _config_issue("config_invalid_binding", "binding 必须是对象", field="$.binding")
            )
        else:
            config_binding = {}
            for key in raw_binding:
                if key not in _BINDING_FIELDS:
                    issues.append(
                        _config_issue(
                            "config_binding_unknown_field",
                            f"binding 含未声明字段 {key!r}",
                            field=f"$.binding.{key}",
                        )
                    )
            if raw_binding.get("schema_version") not in (None, SCHEMA_RECALC_BINDINGS):
                issues.append(
                    _config_issue(
                        "config_invalid_binding",
                        f"binding.schema_version 必须是 {SCHEMA_RECALC_BINDINGS}",
                        field="$.binding.schema_version",
                    )
                )
            for name in ("case_id", "model_id", "capabilities_version", "input_fingerprints", "report_ref"):
                if name not in raw_binding:
                    issues.append(
                        _config_issue(
                            "config_invalid_binding",
                            f"binding 缺少字段 {name!r}",
                            field=f"$.binding.{name}",
                        )
                    )
            if not isinstance(raw_binding.get("input_fingerprints"), Mapping):
                issues.append(
                    _config_issue(
                        "config_invalid_binding",
                        "binding.input_fingerprints 必须是对象",
                        field="$.binding.input_fingerprints",
                    )
                )
            try:
                config_binding["report_ref"] = validate_report_ref(raw_binding.get("report_ref"))
            except RecalcRejected as exc:
                issues.append(
                    _config_issue(
                        "config_invalid_binding",
                        "binding.report_ref 非法：" + "；".join(str(i.get("message")) for i in exc.issues),
                        field="$.binding.report_ref",
                    )
                )
                config_binding["report_ref"] = raw_binding.get("report_ref")
            config_binding.update(
                {
                    key: raw_binding.get(key)
                    for key in _BINDING_FIELDS
                    if key in raw_binding and key != "report_ref"
                }
            )
            result["binding"] = config_binding
            expected_binding = current or {}
            if current is None:
                result["binding_checked"] = False
                issues.append(
                    _config_issue(
                        "config_binding_not_checked",
                        "没有当前页面/服务绑定可比对：只做自洽校验，无法确认配置属于本报告",
                        field="$.binding",
                        severity="warning",
                    )
                )
            else:
                result["binding_checked"] = True
                if str(config_binding.get("case_id")) != str(expected_binding.get("case_id")):
                    issues.append(
                        _config_issue(
                            "config_case_mismatch",
                            f"配置案例 {config_binding.get('case_id')!r} 与当前案例 "
                            f"{expected_binding.get('case_id')!r} 不一致",
                            field="$.binding.case_id",
                        )
                    )
                if str(config_binding.get("model_id")) != str(expected_binding.get("model_id")):
                    issues.append(
                        _config_issue(
                            "config_model_mismatch",
                            f"配置模型 {config_binding.get('model_id')!r} 与当前模型 "
                            f"{expected_binding.get('model_id')!r} 不一致",
                            field="$.binding.model_id",
                        )
                    )
                expected_version = expected_version or str(
                    expected_binding.get("capabilities_version") or ""
                )
                actual_version = str(
                    config_binding.get("capabilities_version")
                    or (config_binding.get("report_ref") or {}).get("capabilities_version")
                    or ""
                )
                if expected_version and actual_version and actual_version != expected_version:
                    issues.append(
                        _config_issue(
                            "config_capabilities_version_mismatch",
                            f"配置能力版本 {actual_version!r} 与当前 {expected_version!r} 不一致",
                            field="$.binding.capabilities_version",
                        )
                    )
                expected_ui = str(
                    expected_binding.get("ui_contract_version")
                    or (expected_binding.get("report_ref") or {}).get("ui_contract_version")
                    or ""
                )
                actual_ui = str(
                    config_binding.get("ui_contract_version")
                    or (config_binding.get("report_ref") or {}).get("ui_contract_version")
                    or ""
                )
                if expected_ui and actual_ui and actual_ui != expected_ui:
                    issues.append(
                        _config_issue(
                            "config_ui_contract_mismatch",
                            f"配置 UI 契约版本 {actual_ui!r} 与当前 {expected_ui!r} 不一致",
                            field="$.binding.ui_contract_version",
                        )
                    )
                expected_renderer = str(
                    expected_binding.get("renderer_fingerprint")
                    or (expected_binding.get("report_ref") or {}).get("renderer_fingerprint")
                    or ""
                )
                actual_renderer = str(
                    config_binding.get("renderer_fingerprint")
                    or (config_binding.get("report_ref") or {}).get("renderer_fingerprint")
                    or ""
                )
                if expected_renderer and actual_renderer and actual_renderer != expected_renderer:
                    issues.append(
                        _config_issue(
                            "config_renderer_fingerprint_mismatch",
                            "配置 renderer_fingerprint 与当前渲染器不一致（报告 HTML/缓存需重新生成）",
                            field="$.binding.renderer_fingerprint",
                        )
                    )
                _compare_batch(expected_binding, config_binding, prefix="$.binding", issues=issues)

    raw_scenario = payload.get("scenario")
    if raw_scenario is None:
        issues.append(_config_issue("config_missing_scenario", "配置缺少 scenario", field="$.scenario"))
    elif not isinstance(raw_scenario, Mapping):
        issues.append(
            _config_issue("config_invalid_scenario", "scenario 必须是对象", field="$.scenario")
        )
    else:
        if isinstance(capabilities, Mapping):
            try:
                result["scenario"] = validate_scenario(
                    raw_scenario, capabilities, field="$.scenario", require_complete=True
                )
            except RecalcRejected as exc:
                issues.extend(
                    _config_issue(
                        issue.get("code") or "config_invalid_scenario",
                        str(issue.get("message") or ""),
                        field=issue.get("field") or "$.scenario",
                        details=issue.get("details"),
                    )
                    for issue in exc.issues
                )
        else:
            issues.append(
                _config_issue(
                    "config_capabilities_unavailable",
                    "缺少 capabilities：只做基础结构校验，无法确认字段/层数/预设合法",
                    field="$.scenario",
                    severity="warning",
                )
            )
            scenario_issues = _structural_scenario_issues(raw_scenario)
            issues.extend(scenario_issues)
            if not scenario_issues:
                result["scenario"] = dict(raw_scenario)

    result["ok"] = not any(issue.get("severity") == "error" for issue in issues)
    return result


def _structural_scenario_issues(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(scenario.get("baseline_profile"), str) or not scenario.get("baseline_profile"):
        issues.append(
            _config_issue(
                "config_invalid_scenario",
                "scenario.baseline_profile 必须是非空字符串",
                field="$.scenario.baseline_profile",
            )
        )
    if not isinstance(scenario.get("states"), Mapping):
        issues.append(
            _config_issue("config_invalid_scenario", "scenario.states 必须是对象", field="$.scenario.states")
        )
    if not isinstance(scenario.get("target"), Mapping):
        issues.append(
            _config_issue("config_invalid_scenario", "scenario.target 必须是对象", field="$.scenario.target")
        )
    return issues


def validate_report_config_file(
    path: str | Path,
    *,
    current_bindings: Any = None,
    capabilities: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Read + validate a saved config file (never raises for bad JSON)."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "schema_version": SCHEMA_REPORT_CONFIG,
            "ok": False,
            "legacy": False,
            "legacy_reason": None,
            "binding_checked": False,
            "issues": [_config_issue("config_invalid_json", f"无法读取配置：{exc}", field="$")],
            "scenario": None,
            "binding": None,
        }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "schema_version": SCHEMA_REPORT_CONFIG,
            "ok": False,
            "legacy": False,
            "legacy_reason": None,
            "binding_checked": False,
            "issues": [_config_issue("config_invalid_json", f"配置不是合法 JSON：{exc}", field="$")],
            "scenario": None,
            "binding": None,
        }
    return validate_report_config(
        payload, current_bindings=current_bindings, capabilities=capabilities
    )


# ---------------------------------------------------------------------------
# renderer wiring (kh-bh P3R-A interface: recalc_endpoint / capabilities kwargs)
# ---------------------------------------------------------------------------


def render_with_context(
    renderer: Callable[..., Any],
    payload: Any,
    *,
    recalc_endpoint: Optional[str] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    bindings: Optional[Mapping[str, Any]] = None,
    recalc_bindings: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Call a render function with ``recalc_endpoint`` / ``capabilities`` / ``bindings``.

    The shared renderer contract (P3R-A/P3R-B2) is: rendering functions accept the
    keyword arguments ``recalc_endpoint``, ``capabilities`` and ``bindings``; when
    they are not provided the report falls back to the offline default.  During
    the staged implementation the renderer may still have the old single-argument
    signature, so this adapter inspects the callable and only passes supported
    keywords.  That keeps the services working before and after the renderer
    change without duplicating rendering logic.

    ``bindings`` is the ``le-recalc-bindings-v1`` page payload; ``recalc_bindings``
    is accepted as an alias for the renderer kwarg of the same name.
    """
    binding_payload = bindings if bindings is not None else recalc_bindings
    if binding_payload is not None:
        binding_payload = validate_recalc_bindings(binding_payload)
    kwargs: dict[str, Any] = {}
    try:
        signature = inspect.signature(renderer)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        signature = None
    if signature is not None:
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        if accepts_kwargs or "recalc_endpoint" in parameters:
            if recalc_endpoint is not None:
                kwargs["recalc_endpoint"] = parse_endpoint(recalc_endpoint)["url"]
        if accepts_kwargs or "capabilities" in parameters:
            if capabilities is not None:
                kwargs["capabilities"] = dict(capabilities)
        if binding_payload is not None:
            if "bindings" in parameters:
                kwargs["bindings"] = dict(binding_payload)
            elif "recalc_bindings" in parameters:
                kwargs["recalc_bindings"] = dict(binding_payload)
            elif accepts_kwargs:
                kwargs["bindings"] = dict(binding_payload)
    return renderer(payload, **kwargs) if kwargs else renderer(payload)


# ---------------------------------------------------------------------------
# service side: contract request -> response (shared by BH / FR services)
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class RecalcBinding:
    """Everything a service must expose to the shared HTTP/contract layer."""

    case_id: str
    model_id: str
    capabilities: Mapping[str, Any]
    report_ref: Mapping[str, Any]
    input_fingerprints: Mapping[str, str]
    recompute_endpoint: str

    @property
    def capabilities_version(self) -> str:
        version = _capabilities_version_of(self.capabilities)
        if not version:
            raise RecalcError("capabilities.recalculation.capabilities_version 缺失")
        return version


class RecalcServiceMixin:
    """Shared contract handling for the two model services.

    Subclasses implement:

    * ``binding`` -> :class:`RecalcBinding` (identity, capabilities, report_ref,
      input fingerprints, actual endpoint);
    * ``prepare_scenario(scenario)`` -> engine-normalised full scenario;
    * ``compute_scenario(scenario)`` -> ``(result_document, html)``;
    * ``legacy_scenario(payload)`` -> scenario dict for historical report pages;
    * ``initial_html()`` -> HTML for ``GET /report`` (may be expensive).
    """

    tracker: RevisionTracker

    # -- hooks ----------------------------------------------------------
    @property
    def binding(self) -> RecalcBinding:  # pragma: no cover - abstract
        raise NotImplementedError

    def prepare_scenario(self, scenario: Mapping[str, Any]) -> dict[str, Any]:  # pragma: no cover
        return dict(scenario)

    def compute_scenario(self, scenario: Mapping[str, Any]) -> tuple[dict[str, Any], str]:  # pragma: no cover
        raise NotImplementedError

    def legacy_scenario(self, payload: Any) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def initial_html(self) -> str:  # pragma: no cover
        raise NotImplementedError

    # -- shared handling ------------------------------------------------
    @property
    def accepted_revision(self) -> Optional[int]:
        return self.tracker.accepted_revision

    @property
    def bindings(self) -> dict[str, Any]:
        """``le-recalc-bindings-v1`` payload for the served/initial page (LIVE-04)."""
        binding = self.binding
        return build_recalc_bindings(
            case_id=binding.case_id,
            model_id=binding.model_id,
            capabilities_version=binding.capabilities_version,
            ui_contract_version=str(binding.capabilities.get("ui_contract_version") or ""),
            renderer_fingerprint=self.renderer_fingerprint,
            report_ref=binding.report_ref,
            input_fingerprints=binding.input_fingerprints,
            recompute_endpoint=binding.recompute_endpoint,
        )

    @property
    def renderer_fingerprint(self) -> str:
        value = str(self.binding.capabilities.get("renderer_fingerprint") or "")
        if is_sha256(value):
            return value
        repo_root = getattr(self, "repo_root", None)
        if repo_root is not None:
            from . import report_fingerprint  # neutral module (O-C single source)

            return report_fingerprint.renderer_fingerprint(repo_root)
        raise RecalcError("缺少 renderer_fingerprint：无法构建页面重算绑定")

    @property
    def bindings_digest(self) -> str:
        return bindings_digest(self.bindings)

    def _rejected(
        self,
        request_payload: Any,
        issues: Sequence[Mapping[str, Any]],
        *,
        command: Optional[str] = None,
    ) -> dict[str, Any]:
        binding = self.binding
        request = request_payload if isinstance(request_payload, Mapping) else {}
        request_id = str(request.get("request_id") or "")
        session_raw = request.get("client_session_id")
        session_id = session_raw if isinstance(session_raw, str) and session_raw else None
        accepted_revision = (
            self.tracker.accepted_revision_for(session_id, request_id=request_id)
            if session_id is not None
            else self.tracker.accepted_revision_for(None, request_id=request_id)
        )
        return build_response(
            request={
                "request_id": request_id,
                "client_edit_revision": request.get("client_edit_revision"),
                "client_session_id": session_id,
                "client_session_legacy": session_id is None,
                "command": request.get("command") or command,
            },
            accepted=False,
            case_id=binding.case_id,
            model_id=binding.model_id,
            capabilities_version=binding.capabilities_version,
            input_fingerprints=binding.input_fingerprints,
            report_ref=binding.report_ref,
            recompute_endpoint=binding.recompute_endpoint,
            accepted_revision=accepted_revision,
            issues=issues,
            command=command or request.get("command"),
        )

    def handle_contract_request(self, payload: Any) -> tuple[int, dict[str, Any]]:
        """Validate + (prepare|compute|recompute); returns ``(http_status, response)``."""
        binding = self.binding
        try:
            normalized = validate_request(
                payload,
                capabilities=binding.capabilities,
                report_ref=binding.report_ref,
                expected_input_fingerprints=binding.input_fingerprints,
                case_id=binding.case_id,
                model_id=binding.model_id,
                sequence=self.tracker,
            )
        except RecalcRejected as exc:
            return 422, self._rejected(payload, exc.issues)

        try:
            scenario = self.prepare_scenario(normalized["scenario"])
        except RecalcRejected as exc:
            return 422, self._rejected(payload, exc.issues)
        except ValueError as exc:
            return 422, self._rejected(
                payload,
                [make_issue("invalid_scenario", str(exc), field="$.scenario")],
            )
        scenario_digest = digest_document(scenario)

        if normalized["command"] == "prepare":
            accepted_revision = self.tracker.accept(
                normalized["client_edit_revision"],
                request_id=normalized["request_id"],
                session_id=normalized["client_session_id"],
            )
            response = build_response(
                request=normalized,
                accepted=True,
                case_id=binding.case_id,
                model_id=binding.model_id,
                capabilities_version=binding.capabilities_version,
                input_fingerprints=binding.input_fingerprints,
                report_ref=binding.report_ref,
                recompute_endpoint=binding.recompute_endpoint,
                accepted_revision=accepted_revision,
                applied_scenario=scenario,
                applied_scenario_digest=scenario_digest,
                result_digest=None,
                html=None,
            )
            response["computed_at"] = utc_now()
            return 200, response

        try:
            result, html = self.compute_scenario(scenario)
        except RecalcRejected as exc:
            return 422, self._rejected(payload, exc.issues)
        except ValueError as exc:
            return 422, self._rejected(
                payload, [make_issue("compute_failed", str(exc)[:1200], field="$.scenario")]
            )
        except Exception as exc:  # noqa: BLE001 - reported, never a silent success
            return 422, self._rejected(
                payload,
                [make_issue("compute_failed", f"重算失败：{exc}"[:1200], field="$.scenario")],
            )
        accepted_revision = self.tracker.accept(
            normalized["client_edit_revision"],
            request_id=normalized["request_id"],
            session_id=normalized["client_session_id"],
        )
        response = build_response(
            request=normalized,
            accepted=True,
            case_id=binding.case_id,
            model_id=binding.model_id,
            capabilities_version=binding.capabilities_version,
            input_fingerprints=binding.input_fingerprints,
            report_ref=binding.report_ref,
            recompute_endpoint=binding.recompute_endpoint,
            accepted_revision=accepted_revision,
            applied_scenario=scenario,
            applied_scenario_digest=scenario_digest,
            result_digest=digest_document(result),
            html=html,
        )
        response["computed_at"] = utc_now()
        return 200, response

    def handle_legacy_request(self, payload: Any) -> str:
        """Historical report pages post raw scenario JSON and expect full HTML."""
        scenario = self.legacy_scenario(payload)
        prepared = self.prepare_scenario(scenario)
        _, html = self.compute_scenario(prepared)
        return html

    def health_payload(self) -> dict[str, Any]:
        binding = self.binding
        return {
            "ok": True,
            "service": "le_calc recalc",
            "case_id": binding.case_id,
            "model_id": binding.model_id,
            "port": parse_endpoint(binding.recompute_endpoint)["port"],
            "recompute_endpoint": binding.recompute_endpoint,
            "capabilities_version": binding.capabilities_version,
            "ui_contract_version": binding.capabilities.get("ui_contract_version"),
            "report_ref": dict(binding.report_ref),
            "accepted_revision": self.tracker.accepted_revision,
            "contracts": {
                "request": SCHEMA_RECALC_REQUEST,
                "response": SCHEMA_RECALC_RESPONSE,
                "bindings": SCHEMA_RECALC_BINDINGS,
                "report_config": SCHEMA_REPORT_CONFIG,
            },
            "bindings_digest": self.bindings_digest,
            "session_scoped_revisions": True,
            "revision_scope": "report run_id + client_session_id",
            "client_session": {
                "field": "client_session_id",
                "required": False,
                "pattern": CLIENT_SESSION_ID_PATTERN.pattern,
                "legacy_policy": (
                    "missing client_session_id => isolated per-request scope "
                    f"({LEGACY_SESSION_PREFIX}<hash(request_id)>), never a shared global history; "
                    "responses carry client_session_legacy=true and old-response protection "
                    "requires the client to upgrade"
                ),
            },
            "revision_tracker": self.tracker.snapshot(),
            "legacy_scenario_accepted": True,
            "legacy_note": (
                "旧版 raw-scenario POST 仍被兼容（响应头 X-Recompute-Legacy: 1），"
                "但页面应迁移到 le-recalc-request-v1。"
            ),
            "message": "POST a "
            f"{SCHEMA_RECALC_REQUEST} JSON to /recompute; historical raw-scenario pages receive full HTML.",
        }

    def capabilities_payload(self) -> dict[str, Any]:
        binding = self.binding
        return {
            "schema_version": SCHEMA_MODEL_CAPABILITIES,
            "case_id": binding.case_id,
            "model_id": binding.model_id,
            "recompute_endpoint": binding.recompute_endpoint,
            "capabilities_version": binding.capabilities_version,
            "capabilities": dict(binding.capabilities),
            "report_ref": dict(binding.report_ref),
            "bindings": self.bindings,
            "session_scoped_revisions": True,
            "client_session": {
                "field": "client_session_id",
                "required": False,
                "pattern": CLIENT_SESSION_ID_PATTERN.pattern,
                "legacy_policy": "missing id => isolated per-request scope (client must upgrade for LIVE-07)",
            },
        }


MAX_BODY_BYTES = 2 * 1024 * 1024


def make_recalc_handler(service: RecalcServiceMixin):
    """Build the loopback HTTP handler shared by the BH and FR services."""

    class Handler(BaseHTTPRequestHandler):  # pragma: no cover - network integration
        server_version = "le-calc-recalc/2.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
            sys.stderr.write("[le_calc recalc] " + (fmt % args) + "\n")

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, X-Request-Id, X-Client-Edit-Revision"
            )
            self.send_header(
                "Access-Control-Expose-Headers",
                "X-Request-Id, X-Client-Edit-Revision, X-Accepted-Revision, "
                "X-Recompute-Endpoint, X-Recompute-Case, X-Recompute-Model",
            )

        def _common_headers(
            self,
            *,
            request_id: str = "",
            revision: Any = None,
            legacy: bool = False,
            session_id: str = "",
            session_legacy: Optional[bool] = None,
        ) -> None:
            binding = service.binding
            self.send_header("X-Recompute-Case", str(binding.case_id))
            self.send_header("X-Recompute-Model", str(binding.model_id))
            self.send_header("X-Recompute-Endpoint", str(binding.recompute_endpoint))
            self.send_header("X-Recompute-Bindings-Schema", SCHEMA_RECALC_BINDINGS)
            if legacy:
                self.send_header("X-Recompute-Legacy", "1")
                self.send_header("X-Recompute-Contract", "raw-scenario")
            else:
                self.send_header("X-Recompute-Legacy", "0")
                self.send_header("X-Recompute-Contract", SCHEMA_RECALC_REQUEST)
                self.send_header("X-Client-Session-Policy", "session-scoped-edit-revision")
            if request_id:
                self.send_header("X-Request-Id", str(request_id))
            if revision is not None:
                self.send_header("X-Client-Edit-Revision", str(revision))
            if session_id:
                self.send_header("X-Client-Session-Id", str(session_id))
            if session_legacy is not None:
                self.send_header("X-Client-Session-Legacy", "1" if session_legacy else "0")
            accepted = (
                service.tracker.accepted_revision_for(
                    None, request_id=str(request_id) or None
                )
                if session_legacy is not False
                else service.tracker.accepted_revision_for(str(session_id))
            )
            if accepted is not None:
                self.send_header("X-Accepted-Revision", str(accepted))

        def _json(
            self,
            status: int,
            payload: Any,
            *,
            request_id: str = "",
            revision: Any = None,
            legacy: bool = False,
            session_id: str = "",
            session_legacy: Optional[bool] = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._common_headers(
                request_id=request_id,
                revision=revision,
                legacy=legacy,
                session_id=session_id,
                session_legacy=session_legacy,
            )
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _html(
            self,
            status: int,
            html: str,
            *,
            request_id: str = "",
            revision: Any = None,
            legacy: bool = False,
            session_id: str = "",
            session_legacy: Optional[bool] = None,
        ) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._common_headers(
                request_id=request_id,
                revision=revision,
                legacy=legacy,
                session_id=session_id,
                session_legacy=session_legacy,
            )
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = str(self.path).split("?", 1)[0]
            if path in ("/", "/health", "/healthz"):
                self._json(200, service.health_payload())
                return
            if path in ("/capabilities", "/capabilities.json"):
                self._json(200, service.capabilities_payload())
                return
            if path in ("/report", "/report.html"):
                try:
                    self._html(200, service.initial_html())
                except Exception as exc:  # noqa: BLE001 - never a silent success
                    self._json(
                        500,
                        {
                            "ok": False,
                            "issues": [
                                make_issue("report_render_failed", f"报告生成失败：{exc}"[:1200])
                            ],
                        },
                    )
                return
            self.send_error(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            path = str(self.path).split("?", 1)[0]
            if path not in ("/recompute", "/recalc"):
                self.send_error(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("请求体为空或超过大小限制。")
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - request parsing
                self._json(
                    400,
                    {
                        "schema_version": SCHEMA_RECALC_RESPONSE,
                        "accepted": False,
                        "issues": [make_issue("invalid_json", str(exc)[:600])],
                        "accepted_revision": service.tracker.accepted_revision,
                        "recompute_endpoint": service.binding.recompute_endpoint,
                        "report_ref": dict(service.binding.report_ref),
                    },
                )
                return

            if isinstance(payload, Mapping) and payload.get("schema_version") == SCHEMA_RECALC_REQUEST:
                status, response = service.handle_contract_request(payload)
                self._json(
                    status,
                    response,
                    request_id=str(response.get("request_id") or ""),
                    revision=response.get("client_edit_revision"),
                    session_id=str(response.get("client_session_id") or ""),
                    session_legacy=response.get("client_session_legacy"),
                )
                return

            # Historical report HTML (generated before the shared contract) posts
            # a raw scenario and expects the whole HTML back; keep it working but
            # mark the response as legacy (LIVE-04: migrated pages must not rely on it).
            try:
                html = service.handle_legacy_request(payload)
            except RecalcRejected as exc:
                self._json(
                    422,
                    {
                        "ok": False,
                        "legacy": True,
                        "error": "；".join(str(issue.get("message")) for issue in exc.issues)[:1200],
                        "issues": exc.issues,
                    },
                    legacy=True,
                )
                return
            except Exception as exc:  # noqa: BLE001 - keep old error shape
                self._json(422, {"ok": False, "legacy": True, "error": str(exc)[:1200]}, legacy=True)
                return
            self._html(200, html, legacy=True)

    return Handler


def run_loopback_service(service: RecalcServiceMixin, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    """Bind the loopback server on the requested port and serve the service.

    The endpoint embedded in every report/response is derived from the **actual**
    bound socket (so ``--port 0`` also yields the correct endpoint), never from a
    hardcoded default.
    """
    httpd = ThreadingHTTPServer((host, int(port)), make_recalc_handler(service))
    actual_endpoint = endpoint_from_server_address(httpd.server_address)
    return httpd, actual_endpoint



__all__ = [
    "CLIENT_SESSION_ID_PATTERN",
    "COMMANDS",
    "CONFIG_ISSUE_CODES",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "FINGERPRINT_KEYS",
    "MAX_BODY_BYTES",
    "RECALC_BINDINGS_FIELDS",
    "RECOMPUTE_PATH",
    "REQUEST_FIELDS",
    "RESPONSE_FIELDS",
    "RecalcBinding",
    "RecalcError",
    "RecalcRejected",
    "RecalcServiceMixin",
    "ReportCache",
    "RevisionTracker",
    "SCHEMA_MODEL_CAPABILITIES",
    "SCHEMA_RECALC_BINDINGS",
    "SCHEMA_RECALC_REQUEST",
    "SCHEMA_RECALC_RESPONSE",
    "SCHEMA_REPORT_CONFIG",
    "SCHEMA_REPORT_REF",
    "build_report_ref",
    "bindings_digest",
    "build_recalc_bindings",
    "build_response",
    "canonical_json",
    "default_cache_root",
    "digest_document",
    "endpoint_from_server_address",
    "endpoint_port",
    "is_sha256",
    "LEGACY_SESSION_PREFIX",
    "REPORT_CONFIG_FIELDS",
    "legacy_request_from_payload",
    "loopback_endpoint",
    "make_issue",
    "make_recalc_handler",
    "numeric_cache_key",
    "parse_endpoint",
    "render_with_context",
    "report_cache_key",
    "report_ref_digest",
    "run_loopback_service",
    "sha256_text",
    "utc_now",
    "validate_recalc_bindings",
    "validate_report_config",
    "validate_report_config_file",
    "validate_report_ref",
    "validate_request",
    "validate_response",
    "validate_scenario",
]
