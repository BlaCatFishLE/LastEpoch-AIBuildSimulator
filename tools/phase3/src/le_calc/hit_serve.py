"""Loopback recompute service for the Flame Reave player report (hit model).

Same 3-A contract as the Black Hole service (:mod:`le_calc.serve`):

* ``le-recalc-request-v1`` / ``le-recalc-response-v1`` shared contract with
  strict scenario/capability validation and ``client_edit_revision`` /
  ``accepted_revision`` ordering (§10.3);
* the recompute endpoint embedded in the report and in every response comes from
  the actual bound loopback socket (§10.4);
* model routing never falls back to the BH adapter (§10.2): this service loads
  the hit snapshot/ruleset/scenario only;
* historical report pages (pre-3-A) that post ``{"scenario": {...}}`` and expect
  full HTML keep working;

The service reads frozen inputs only and writes no files.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import hit_buffs, hit_damage, hit_inputs, hit_report, hit_ruleset, hit_simulate
from . import model, recalc, report_capabilities

DEFAULT_PORT = recalc.DEFAULT_PORT


def _repo_root() -> Path:
    env = os.environ.get("LE_REPO_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    candidate = Path(__file__).resolve().parents[4]
    if (candidate / "config" / "project.json").is_file():
        return candidate
    return Path.cwd()


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
    return recalc.sha256_text(candidate.read_bytes().hex())


def _utc_now() -> str:
    return recalc.utc_now()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _default_case() -> dict[str, Any]:
    return {
        "case_id": "flame-reave",
        "model": "hit",
        "main_skill": {"ability_id": "fr11mv", "name": "Flame Reave"},
    }


class HitReportService(recalc.RecalcServiceMixin):
    """Frozen FR case + scenario -> recomputed result/HTML per request."""

    def __init__(
        self,
        *,
        case: dict[str, Any],
        snapshot: model.Snapshot,
        ruleset: Any,
        base_scenario: dict[str, Any],
        ruleset_path: Optional[str] = None,
        scenario_path: Optional[str] = None,
        run_id: str = "serve",
        port: int = DEFAULT_PORT,
        case_id: Optional[str] = None,
        model_id: str = "hit",
        capabilities: Optional[dict[str, Any]] = None,
        report_ref: Optional[dict[str, Any]] = None,
        input_fingerprints: Optional[dict[str, str]] = None,
        recalc_endpoint: Optional[str] = None,
        report_result: Optional[dict[str, Any]] = None,
        repo_root: Optional[str | Path] = None,
        dataset_fingerprint: Optional[str] = None,
        calc_input_path: Optional[str] = None,
    ) -> None:
        self.case = case
        self.snapshot = snapshot
        self.ruleset = ruleset
        self.base_scenario = dict(base_scenario or {})
        self.ruleset_path = ruleset_path
        self.scenario_path = scenario_path
        self.run_id = run_id
        self.port = int(port)
        self.case_id = str(case_id or case.get("case_id") or "flame-reave")
        self.model_id = str(model_id)
        self.repo_root = Path(repo_root or _repo_root()).resolve()
        self.report_result = report_result or _env_report_result(self.repo_root)
        self.calc_input_path = calc_input_path
        self.tracker = recalc.RevisionTracker()
        self._configured_endpoint = (
            recalc.parse_endpoint(recalc_endpoint)["url"] if recalc_endpoint else None
        )
        self._bound_endpoint: Optional[str] = None
        self._prepared: dict[str, dict[str, Any]] = {}
        env_caps = _env_json("LE_REPORT_CAPABILITIES")
        self.capabilities = capabilities or env_caps or report_capabilities.capabilities_for_model(
            self.model_id,
            repo_root=self.repo_root,
            result=self.report_result,
            scenario_doc=self.base_scenario,
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
            report_path=f"reports/{self.case_id}/{self.run_id}/flame-reave-damage.html",
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
        snapshot_document = (
            self.snapshot.to_dict() if hasattr(self.snapshot, "to_dict") else getattr(self.snapshot, "__dict__", {})
        )
        return {
            "calc_input": _file_sha256(self.calc_input_path),
            "ruleset": _file_sha256(self.ruleset_path),
            "snapshot": recalc.digest_document(snapshot_document),
            "dataset": dataset_fingerprint or "0" * 64,
        }

    # -- engine path ----------------------------------------------------
    def merged_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        merged = _deep_merge(getattr(self, "base_scenario", {}), dict(scenario or {}))
        for key in ("states", "hit_override", "target", "single_hit", "dps", "baseline_profile", "effects"):
            if key in (scenario or {}):
                merged[key] = copy.deepcopy(scenario[key])
        return merged

    def prepare_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        merged = self.merged_scenario(scenario)
        self._prepared[recalc.digest_document(merged)] = merged
        return merged

    def _outputs(self, scenario: dict[str, Any]) -> dict[str, Any]:
        single = hit_damage.compute_single_hit(self.snapshot, self.ruleset, scenario)
        simulation = hit_simulate.simulate(self.snapshot, self.ruleset, scenario)
        screening = hit_buffs.screen_buffs(self.snapshot, self.ruleset, scenario)
        baselines: dict[str, Any] = {}
        sensitivity: list[dict[str, Any]] = []
        try:
            profiles = (scenario or {}).get("profiles") or {}
            for profile_id in profiles:
                # The two baseline rows are profile reference values: the profile's
                # own ``states`` must win over the page's current control states,
                # otherwise both rows collapse to the currently-selected profile
                # (browser acceptance found 51,831 / 51,831 for FR).
                profile_scenario = copy.deepcopy(scenario)
                profile_scenario["baseline_profile"] = profile_id
                profile_scenario.pop("states", None)
                if isinstance(profile_scenario.get("single_hit"), dict):
                    profile_scenario["single_hit"].pop("states", None)
                if isinstance(profile_scenario.get("dps"), dict):
                    profile_scenario["dps"].pop("states", None)
                baselines[profile_id] = {
                    "single_hit": hit_damage.compute_single_hit(
                        self.snapshot, self.ruleset, profile_scenario
                    ),
                    "simulate": hit_simulate.simulate(
                        self.snapshot, self.ruleset, profile_scenario
                    ),
                }
            from . import cli as _cli

            for variant_id, label, states in _cli._HIT_SENSITIVITY_VARIANTS:
                variant_scenario = _cli._hit_scenario_with_states(scenario, states)
                sensitivity.append(
                    {
                        "id": variant_id,
                        "label": label,
                        "states": dict(states),
                        "single_hit": hit_damage.compute_single_hit(
                            self.snapshot, self.ruleset, variant_scenario
                        ),
                        "simulate": hit_simulate.simulate(
                            self.snapshot, self.ruleset, variant_scenario
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover - main result remains usable
            import sys as _sys

            print(f"[hit serve] extra baselines/sensitivity failed: {exc}", file=_sys.stderr)
        return {
            "single_hit": single,
            "simulate": simulation,
            "buff_screening": screening,
            "baselines": baselines,
            "buff_sensitivity": sensitivity,
        }

    def compute_scenario(self, scenario: dict[str, Any]) -> tuple[dict[str, Any], str]:
        merged = self._prepared.get(recalc.digest_document(scenario)) or self.merged_scenario(scenario)
        outputs = self._outputs(merged)
        result = hit_report.build_hit_result(
            self.snapshot,
            self.ruleset,
            self.case,
            run_id=f"{self.run_id}-scenario",
            generated_at=_utc_now(),
            ruleset_path=self.ruleset_path,
            scenario=merged,
            scenario_path=self.scenario_path,
            engine_outputs=outputs,
        )
        html = recalc.render_with_context(
            hit_report.render_hit_html,
            result,
            recalc_endpoint=self.recompute_endpoint,
            capabilities=self.capabilities,
            bindings=self.bindings,
        )
        return result, html

    # -- legacy/public API ----------------------------------------------
    def build_html(self, scenario: dict[str, Any]) -> str:
        """Legacy entry point (historical tests): apply one scenario, return HTML."""
        merged = self.merged_scenario(scenario)
        self._prepared[recalc.digest_document(merged)] = merged
        _, html = self.compute_scenario(merged)
        return html

    def legacy_scenario(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("scenario"), dict):
            return dict(payload["scenario"])
        if not isinstance(payload, dict):
            raise recalc.RecalcRejected(
                [recalc.make_issue("invalid_request", "旧格式请求体必须是场景对象")]
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
        merged = self.merged_scenario({})
        self._prepared[recalc.digest_document(merged)] = merged
        _, html = self.compute_scenario(merged)
        return html


def _load_service_from_args(args: argparse.Namespace) -> HitReportService:
    case_path = Path(args.case) if args.case else None
    case = (
        json.loads(case_path.read_text(encoding="utf-8"))
        if case_path and case_path.is_file()
        else _default_case()
    )
    snapshot_raw = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    snapshot = model.Snapshot.from_dict(snapshot_raw)
    ruleset = hit_ruleset.load_ruleset(args.ruleset)
    base_scenario = (
        json.loads(Path(args.scenario).read_text(encoding="utf-8")) if args.scenario else {}
    )
    case_repo_root = Path(os.environ.get("LE_REPO_ROOT") or _repo_root()).resolve()
    calc_input_path: Optional[str] = None
    try:
        candidate = ((case.get("inputs") or {}).get("calculation_input")) or ""
        if candidate:
            resolved = (case_repo_root / str(candidate)).resolve()
            if resolved.is_file():
                calc_input_path = str(resolved)
    except Exception:  # pragma: no cover - optional binding detail
        calc_input_path = None
    return HitReportService(
        case=case,
        snapshot=snapshot,
        ruleset=ruleset,
        base_scenario=base_scenario,
        ruleset_path=args.ruleset,
        scenario_path=args.scenario,
        run_id=getattr(args, "run_id", None) or "serve",
        port=int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT),
        recalc_endpoint=os.environ.get("LE_RECALC_ENDPOINT") or None,
        repo_root=case_repo_root,
        calc_input_path=calc_input_path,
    )


def serve(args: argparse.Namespace) -> int:
    service = _load_service_from_args(args)
    requested_port = int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT)
    httpd = ThreadingHTTPServer(("127.0.0.1", requested_port), recalc.make_recalc_handler(service))
    actual_endpoint = recalc.endpoint_from_server_address(httpd.server_address)
    service.bind_endpoint(actual_endpoint)
    print(
        f"正在启动 Flame Reave 本地重算服务（仅回环 {actual_endpoint}，Ctrl+C 停止）……",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="le_calc hit-serve")
    parser.add_argument("--case")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--ruleset", required=True)
    parser.add_argument("--scenario")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--run-id", default="serve")
    return serve(parser.parse_args(argv))


__all__ = ["DEFAULT_PORT", "HitReportService", "main", "serve"]
