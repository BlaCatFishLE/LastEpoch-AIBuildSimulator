"""Loopback recompute service for the Black Hole player report (event model).

The static HTML report stays a single offline file.  When the user explicitly
presses "计算 DPS" the page posts either

* the shared ``le-recalc-request-v1`` contract payload (new reports), or
* the historical raw scenario JSON (reports generated before 3-A),

to ``http://<actual-bound-loopback>/recompute`` and receives a full HTML report
re-rendered with the actual endpoint (or, for the new contract, a
``le-recalc-response-v1`` JSON payload containing that HTML).

3-A wiring (``docs/contracts/workflow-interface.md`` §10.4):

* the recompute endpoint comes from the **actual bound socket**, never a
  hardcoded ``127.0.0.1:8765`` constant;
* the model capability document (``report_capabilities``) drives the strict
  request validation (target fields, Buff controls, stack limits, modes/windows);
* ``client_edit_revision`` / ``accepted_revision`` guard against stale responses;
* only the report cache key includes the endpoint/port (``le_calc.recalc``).

The service binds loopback only, reads frozen inputs once and writes no files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import model, recalc, report_capabilities
from .conditional import build_context
from .conditional.registry import load_registry
from .conditional.recompute import (
    compute_scenario_outputs,
    prepare_scenario,
    recompute_scenario,
)
from .report import build_result
from .report_ui import render
from .ruleset import load_ruleset

DEFAULT_PORT = recalc.DEFAULT_PORT


def _repo_root() -> Path:
    env = os.environ.get("LE_REPO_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    candidate = Path(__file__).resolve().parents[4]
    if (candidate / "config" / "project.json").is_file():
        return candidate
    return Path.cwd()


def _utc_now() -> str:
    return recalc.utc_now()


def _env_json(name: str) -> Optional[dict[str, Any]]:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _env_report_result(repo_root: Path) -> Optional[dict[str, Any]]:
    """Load the selected run's result document passed via ``LE_REPORT_RESULT``."""
    raw = os.environ.get("LE_REPORT_RESULT")
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if not candidate.is_file():
        return None
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _file_sha256(path: Optional[str]) -> str:
    if not path:
        return "0" * 64
    candidate = Path(path)
    if not candidate.is_file():
        return "0" * 64
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ScenarioReportService(recalc.RecalcServiceMixin):
    """Holds the frozen BH inputs and builds a scenario-applied report per request."""

    def __init__(
        self,
        *,
        snapshot: model.Snapshot,
        ruleset,
        ruleset_path: Optional[str],
        calc_input_path: Optional[str],
        build_path: Optional[str],
        registry_path: Optional[str],
        monster_level: Optional[int],
        run_id: str,
        port: int = DEFAULT_PORT,
        case_id: str = "black-hole",
        model_id: str = "black-hole",
        capabilities: Optional[dict[str, Any]] = None,
        report_ref: Optional[dict[str, Any]] = None,
        input_fingerprints: Optional[dict[str, str]] = None,
        dataset_fingerprint: Optional[str] = None,
        recalc_endpoint: Optional[str] = None,
        report_result: Optional[dict[str, Any]] = None,
        repo_root: Optional[str | Path] = None,
    ) -> None:
        self.snapshot = snapshot
        self.ruleset = ruleset
        self.ruleset_path = ruleset_path or getattr(ruleset, "path", None)
        self.calc_input_path = calc_input_path
        self.build_path = build_path
        self.registry_path = registry_path
        self.registry = load_registry(registry_path)
        self.monster_level = monster_level
        self.run_id = run_id
        self.port = int(port)
        self.case_id = str(case_id)
        self.model_id = str(model_id)
        self.repo_root = Path(repo_root or _repo_root()).resolve()
        self.report_result = report_result or _env_report_result(self.repo_root)
        self.tracker = recalc.RevisionTracker()
        self._configured_endpoint = recalc.parse_endpoint(recalc_endpoint)["url"] if recalc_endpoint else None
        self._bound_endpoint: Optional[str] = None
        self._prepared: dict[str, dict[str, Any]] = {}

        env_caps = _env_json("LE_REPORT_CAPABILITIES")
        self.capabilities = capabilities or env_caps or report_capabilities.capabilities_for_model(
            self.model_id,
            repo_root=self.repo_root,
            registry=getattr(self.registry, "data", None),
            result=self.report_result,
        )
        env_fingerprints = _env_json("LE_RECALC_FINGERPRINTS")
        self.input_fingerprints = (
            input_fingerprints
            or env_fingerprints
            or self._compute_input_fingerprints(dataset_fingerprint)
        )
        env_ref = _env_json("LE_REPORT_REF")
        self.report_ref = report_ref or env_ref or recalc.build_report_ref(
            case_id=self.case_id,
            model_id=self.model_id,
            run_id=self.run_id,
            run_dir=f"reports/{self.case_id}/{self.run_id}",
            report_path=f"reports/{self.case_id}/{self.run_id}/black-hole-damage.html",
            input_fingerprints=self.input_fingerprints,
            recompute_endpoint=self.recompute_endpoint,
            ui_contract_version=report_capabilities.UI_CONTRACT_VERSION,
            renderer_fingerprint=report_capabilities.renderer_fingerprint(self.repo_root),
        )
        self.tracker.run_id = str(self.report_ref.get("run_id") or self.run_id)

    # -- binding / endpoint ---------------------------------------------
    @property
    def recompute_endpoint(self) -> str:
        if self._bound_endpoint:
            return self._bound_endpoint
        if self._configured_endpoint:
            return self._configured_endpoint
        if int(self.port) <= 0:
            # Not bound yet (unit tests construct before binding): placeholder only.
            return recalc.loopback_endpoint(DEFAULT_PORT)
        return recalc.loopback_endpoint(int(self.port))

    def bind_endpoint(self, endpoint: str) -> None:
        self._bound_endpoint = recalc.parse_endpoint(endpoint)["url"]

    @property
    def binding(self) -> recalc.RecalcBinding:
        return recalc.RecalcBinding(
            case_id=self.case_id,
            model_id=self.model_id,
            capabilities=self.capabilities,
            report_ref=self.report_ref,
            input_fingerprints=self.input_fingerprints,
            recompute_endpoint=self.recompute_endpoint,
        )

    def _compute_input_fingerprints(
        self, dataset_fingerprint: Optional[str] = None
    ) -> dict[str, str]:
        return {
            "calc_input": _file_sha256(self.calc_input_path),
            "ruleset": _file_sha256(self.ruleset_path),
            "snapshot": recalc.digest_document(
                self.snapshot.to_dict()
                if hasattr(self.snapshot, "to_dict")
                else getattr(self.snapshot, "__dict__", {})
            ),
            "dataset": dataset_fingerprint or "0" * 64,
        }

    # -- engine path ----------------------------------------------------
    def _sanitize_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        """Drop control-kind-specific keys that the strict engine validator rejects."""
        import copy as _copy

        out = _copy.deepcopy(scenario)
        effects = out.get("effects")
        if not isinstance(effects, dict):
            return out
        kinds: dict[str, str] = {}
        for effect in getattr(self.registry, "effects", []) or []:
            if not isinstance(effect, dict) or not effect.get("effect_id"):
                continue
            try:
                control = self.registry.control_binding(effect)
            except Exception:  # pragma: no cover - defensive
                control = {}
            kinds[str(effect["effect_id"])] = str((control or {}).get("kind") or "none")
        for effect_id, entry in list(effects.items()):
            if not isinstance(entry, dict):
                continue
            kind = kinds.get(str(effect_id), "")
            if kind == "stacks":
                entry.pop("enabled", None)
            elif kind in ("toggle", "cast_state", "none"):
                entry.pop("stacks", None)
        return out

    def _context(self, scenario: Optional[dict[str, Any]]) -> dict[str, Any]:
        return build_context(
            self.snapshot,
            scenario=scenario,
            ruleset=self.ruleset,
            ruleset_path=self.ruleset_path,
            calc_input_path=self.calc_input_path,
            build_path=self.build_path,
            registry_path=self.registry_path,
            repo_root=str(self.repo_root),
        )

    def prepare_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        sanitized = self._sanitize_scenario(dict(scenario))
        context = self._context(sanitized)
        validation = ((context.get("evaluation") or {}).get("validation") or {})
        errors = validation.get("errors") or []
        if context.get("status") != "ready" or errors:
            issues = [
                recalc.make_issue(
                    "invalid_scenario",
                    str(item.get("message") or item),
                    field="$.scenario",
                )
                for item in errors
            ]
            if not issues:
                issues = [
                    recalc.make_issue(
                        "invalid_scenario",
                        f"场景校验失败：{context.get('status')!r}",
                        field="$.scenario",
                    )
                ]
            raise recalc.RecalcRejected(issues)
        normalized = dict(context.get("scenario") or sanitized)
        self._prepared[recalc.digest_document(normalized)] = context
        return normalized

    def compute_scenario(self, scenario: dict[str, Any]) -> tuple[dict[str, Any], str]:
        digest = recalc.digest_document(scenario)
        context = self._prepared.get(digest)
        if context is None:
            context = self._context(scenario)
            self._prepared[digest] = context
        prepared = prepare_scenario(
            self.snapshot,
            self.ruleset,
            scenario=context["scenario"],
            registry=self.registry,
            catalog=context.get("catalog") or {},
            evaluation=context.get("evaluation") or {},
            monster_level=self.monster_level,
            include_auto=None,
        )
        outputs = compute_scenario_outputs(prepared)
        recompute = recompute_scenario(
            self.snapshot,
            self.ruleset,
            scenario=context["scenario"],
            registry=self.registry,
            catalog=context.get("catalog") or {},
            evaluation=context.get("evaluation") or {},
            monster_level=self.monster_level,
            include_auto=None,
            precomputed_outputs=outputs,
        )
        result = build_result(
            prepared["snapshot_single_cast"],
            prepared["ruleset"],
            run_id=f"{self.run_id}-scenario",
            generated_at=_utc_now(),
            ruleset_path=self.ruleset_path,
            monster_level=int((prepared.get("target") or {}).get("monster_level") or 0),
            scenario_override=prepared["engine_scenario"],
            precomputed_outputs=outputs["report"],
            repro_command=(
                "$env:PYTHONPATH='tools/phase3/src'; & .\\.venv\\Scripts\\python.exe "
                "-m le_calc serve  # then press 应用条件并重算 DPS"
            ),
        )
        context["recompute"] = recompute
        context["scenario_applied"] = True
        context["scenario_target"] = prepared.get("scenario_target") or prepared.get("target")
        context["application_states"] = recompute.get("application_states") or {}
        context["assumptions_applied"] = recompute.get("assumptions_applied") or []
        evaluation = context.get("evaluation")
        if isinstance(evaluation, dict):
            evaluation["dps_recomputed"] = True
            evaluation["report_mode"] = "scenario_applied"
        result["conditional"] = context
        html = recalc.render_with_context(
            render,
            result,
            recalc_endpoint=self.recompute_endpoint,
            capabilities=self.capabilities,
            bindings=self.bindings,
        )
        return result, html

    # -- legacy/public API ----------------------------------------------
    def build_html(self, scenario: dict[str, Any]) -> str:
        """Legacy entry point: apply one raw scenario and return the full HTML."""
        context = self._context(self._sanitize_scenario(scenario))
        validation = ((context.get("evaluation") or {}).get("validation") or {})
        errors = validation.get("errors") or []
        if context.get("status") != "ready" or errors:
            raise ValueError(
                "场景校验失败：" + "；".join(str(item.get("message") or item) for item in errors)[:1200]
            )
        normalized = dict(context.get("scenario") or scenario)
        self._prepared[recalc.digest_document(normalized)] = context
        _, html = self.compute_scenario(normalized)
        return html

    def legacy_scenario(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("scenario"), dict):
            return dict(payload["scenario"])
        if not isinstance(payload, dict):
            raise recalc.RecalcRejected(
                [recalc.make_issue("invalid_request", "旧格式请求体必须是场景 JSON 对象")]
            )
        return dict(payload)

    def initial_html(self) -> str:
        if isinstance(self.report_result, dict):
            return report_capabilities.render_report(
                self.report_result,
                model_id=self.model_id,
                recalc_endpoint=self.recompute_endpoint,
                capabilities=self.capabilities,
                bindings=self.bindings,
            )
        scenario = dict((self._context(None).get("scenario") or {}))
        self._prepared[recalc.digest_document(scenario)] = self._context(scenario)
        _, html = self.compute_scenario(scenario)
        return html


def load_service(args: argparse.Namespace) -> ScenarioReportService:
    from .cli import _load_snapshot

    snapshot = _load_snapshot(args.snapshot)
    ruleset = load_ruleset(args.ruleset) if args.ruleset else load_ruleset()
    return ScenarioReportService(
        snapshot=snapshot,
        ruleset=ruleset,
        ruleset_path=args.ruleset or getattr(ruleset, "path", None),
        calc_input_path=args.calc_input,
        build_path=args.build,
        registry_path=args.registry,
        monster_level=args.monster_level,
        run_id=args.run_id or "save-build-bh-affix-db",
        port=int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT),
        recalc_endpoint=os.environ.get("LE_RECALC_ENDPOINT") or None,
        case_id=os.environ.get("LE_CASE_ID") or (_env_json("LE_REPORT_REF") or {}).get("case_id") or "black-hole",
    )


def serve(args: argparse.Namespace) -> int:
    service = load_service(args)
    requested_port = int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT)
    httpd = ThreadingHTTPServer(("127.0.0.1", requested_port), recalc.make_recalc_handler(service))
    actual_endpoint = recalc.endpoint_from_server_address(httpd.server_address)
    service.bind_endpoint(actual_endpoint)
    print(f"条件重算服务已启动：{actual_endpoint}（仅本机回环，Ctrl+C 停止）", flush=True)
    print(f"健康检查：{recalc.parse_endpoint(actual_endpoint)['url'].rsplit('/recompute', 1)[0]}/health")
    print("在报告的 Buff 设置页点击“计算 DPS”即可整页重算（离线页面不会自动连接）。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止条件重算服务。")
    finally:
        httpd.server_close()
    return 0


__all__ = ["DEFAULT_PORT", "ScenarioReportService", "load_service", "serve"]
