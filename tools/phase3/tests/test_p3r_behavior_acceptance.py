# -*- coding: utf-8 -*-
"""P3R 行为回归验收（R1–R8 / O-A，独立验证方）。

这些测试把 ``docs/reviews/p3r-lead-review-2026-11-08.zh-CN.md`` 中的反例变成长期
可重跑的行为验收：每次都生成**新 run** 的离线 HTML，用真实页面 JS（共享
``recalc_contract.js`` + 模型交互脚本）在 Node ``vm`` + 最小 DOM/fetch 适配器中执行，
经过真实本机 ``serve`` 领域计算，再把响应结果与 CLI 同场景数值对齐。

覆盖：
* R1 Buff 编辑不被预设覆盖（控件 → 请求 scene → 响应结果 → CLI 同场景）；
* R2 hits_per_cast=1 全链一致（请求 / 报告 / 控件 / 再次计算）；
* R3 配置绑定/未知字段/无绑定旧格式拒绝（FR 与 BH 各自格式）；
* R4 在途目标编辑使旧响应不写 HTML；
* R5 错误响应绑定不被应用、合法响应应用、错误可见；
* R6 file:// 加载零请求、显式点击发请求、连接失败保留旧结果/编辑值、非默认端口；
* R7 命中权重不因 readScenario 重建丢失、控件类型与 capabilities 一致；
* R8 双页面会话 revision 隔离、刷新后正常计算；
* O-A FR 状态机（已更改/计算中/已应用/失败保留旧结果）；
* 跨模型完整链路（BH 目标编辑 → 请求 → 引擎 → 报告 → 再次操作）与 CLI 数值对齐。

不修改产品实现；证据 JSON 写入 ``reports/p3r-fix-acceptance/``。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import traceback
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tools" / "phase3" / "tests" / "p3r_js" / "harness.js"
ACCEPT_ROOT = ROOT / "reports" / "p3r-fix-acceptance"
PYTHON = sys.executable
NODE_CANDIDATES = [ROOT / "tools" / "node" / "node.exe", pathlib.Path("node")]
ABS_PATH_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
TOLERANCE = 1e-9

FR_CASE = "examples/flame-reave/case.legacy-hit.json"
BH_CASE = "examples/black-hole/case.legacy.json"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def find_node() -> pathlib.Path:
    for candidate in NODE_CANDIDATES:
        if candidate.is_absolute() and candidate.is_file():
            return candidate
        if not candidate.is_absolute():
            return candidate  # resolved by PATH at exec time
    raise RuntimeError("未找到 Node.js（tools/node/node.exe 或 PATH）")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def embedded_json(html: str, script_id: str) -> dict:
    for match in re.finditer(r"<script([^>]*)>([\s\S]*?)</script>", html):
        if re.search(r'\bid="%s"' % re.escape(script_id), match.group(1)):
            return json.loads(match.group(2))
    raise AssertionError("HTML 中缺少内嵌 JSON：%s" % script_id)


def read_result(html_path: pathlib.Path) -> dict:
    return embedded_json(html_path.read_text(encoding="utf-8"), "result-data")


def decode_download(download: dict) -> dict:
    href = str(download.get("href") or "")
    if href.startswith("data:"):
        return json.loads(urllib.parse.unquote(href.split(",", 1)[1]))
    if download.get("content"):
        return json.loads(download["content"])
    raise AssertionError("下载项不是可解析的配置文件：%r" % (href[:80],))


def run_process(args, *, cwd=ROOT, env=None, timeout=600, what=""):
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        [str(arg) for arg in args],
        cwd=str(cwd),
        env=merged_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "%s 失败（退出码 %s）\nstdout:\n%s\nstderr:\n%s"
            % (what or str(args[0]), completed.returncode, completed.stdout[-4000:], completed.stderr[-4000:])
        )
    return completed


def wait_http(url: str, *, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:  # pragma: no cover - acceptance runs on Windows
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def file_sha256(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



# ---------------------------------------------------------------------------
# 模块级会话：一次生成两模型的新 run + serve，两个测试类共享；结束时统一写证据
# ---------------------------------------------------------------------------
_SESSION: dict = {}
_TEST_RECORDS: list = []


def build_model(kind: str, work_dir: pathlib.Path, stamp: str) -> dict:
    """Generate a fresh run and start its loopback serve; retry on port races.

    Dynamic free ports can still race with another concurrent acceptance run
    (they are bound and released before ``serve`` starts).  A failed bind then
    shows up as an unhealthy service; retry the whole run+serve pair on a new
    port/run-id instead of failing the acceptance suite spuriously.
    """
    case_rel = FR_CASE if kind == "fr" else BH_CASE
    last_error = ""
    for attempt in range(3):
        suffix = "" if attempt == 0 else "-retry%d" % attempt
        port = free_port()
        run_dir = work_dir / "runs" / (kind + suffix)
        run_id = "p3r-acceptance-%s-%s%s" % (kind, stamp, suffix)
        cmd = [
            PYTHON,
            "scripts/le_commands.py",
            "run",
            "--case",
            case_rel,
            "--out",
            str(run_dir),
            "--run-id",
            run_id,
            "--port",
            str(port),
            "--no-report-cache",
        ]
        log = work_dir / "logs" / ("run-%s%s.txt" % (kind, suffix))
        completed = subprocess.run(
            [str(arg) for arg in cmd],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        log.write_text(
            (completed.stdout or "") + "\n--- stderr ---\n" + (completed.stderr or ""),
            encoding="utf-8",
        )
        if completed.returncode != 0:
            last_error = "生成新 run 失败（%s，退出码 %s），见 %s\n%s" % (
                kind,
                completed.returncode,
                log.relative_to(ROOT).as_posix(),
                completed.stderr[-2000:],
            )
            continue
        html_name = "flame-reave-damage.html" if kind == "fr" else "black-hole-damage.html"
        html_path = run_dir / html_name
        if not html_path.is_file():
            last_error = "新 run 缺少离线报告：%s" % html_path
            continue
        endpoint = "http://127.0.0.1:%d/recompute" % port
        serve_log = work_dir / "logs" / ("serve-%s%s.txt" % (kind, suffix))
        serve_err = work_dir / "logs" / ("serve-%s%s.err.txt" % (kind, suffix))
        proc = subprocess.Popen(
            [
                PYTHON,
                "scripts/le_commands.py",
                "serve",
                "--case",
                case_rel,
                "--port",
                str(port),
                "--report",
                str(run_dir),
            ],
            cwd=str(ROOT),
            stdout=serve_log.open("w", encoding="utf-8"),
            stderr=serve_err.open("w", encoding="utf-8"),
        )
        health = "http://127.0.0.1:%d/health" % port
        if not wait_http(health, timeout_s=40.0):
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            last_error = "本机重算服务未就绪（%s，第 %d 次尝试），见 %s" % (
                health,
                attempt + 1,
                serve_log.relative_to(ROOT).as_posix(),
            )
            continue
        return {
            "kind": kind,
            "case": case_rel,
            "run_id": run_id,
            "run_dir": run_dir,
            "html": html_path,
            "port": port,
            "endpoint": endpoint,
            "proc": proc,
            "serve_log": serve_log,
            "run_result": json.loads((run_dir / "result.json").read_text(encoding="utf-8")),
            "html_sha256": file_sha256(html_path),
        }
    raise RuntimeError("搭建新 run/服务失败（重试 3 次）：%s" % last_error)


def model_evidence(model: dict) -> dict:
    def rel(path: pathlib.Path) -> str:
        try:
            return path.relative_to(ROOT).as_posix()
        except ValueError:  # pragma: no cover - outside repo
            return path.name

    return {
        "case": model["case"],
        "run_id": model["run_id"],
        "run_dir": rel(model["run_dir"]),
        "html": rel(model["html"]),
        "html_sha256": model["html_sha256"],
        "port": model["port"],
        "endpoint": model["endpoint"],
    }


def setUpModule() -> None:  # noqa: N802 - unittest hook
    from _author_assets import require_author_assets
    require_author_assets()
    node = find_node()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    work_dir = ACCEPT_ROOT / "sessions" / stamp
    (work_dir / "runs").mkdir(parents=True, exist_ok=True)
    (work_dir / "tmp").mkdir(parents=True, exist_ok=True)
    (work_dir / "logs").mkdir(parents=True, exist_ok=True)
    _SESSION.update(
        {
            "node": node,
            "stamp": stamp,
            "work_dir": work_dir,
            "node_relative": node.relative_to(ROOT).as_posix() if node.is_absolute() else "node",
            "fr": build_model("fr", work_dir, stamp),
            "bh": build_model("bh", work_dir, stamp),
        }
    )
    _SESSION["evidence"] = {
        "schema": "p3r-behavior-acceptance-v1",
        "session": stamp,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_root": ".",
        "node": _SESSION["node_relative"],
        "harness": HARNESS.relative_to(ROOT).as_posix(),
        "models": {
            "fr": model_evidence(_SESSION["fr"]),
            "bh": model_evidence(_SESSION["bh"]),
        },
        "review": "docs/reviews/p3r-lead-review-2026-11-08.zh-CN.md",
        "coverage": {
            "R1": "test_r1_buff_edits_not_overwritten",
            "R2": "test_r2_hits_one_full_chain",
            "R3": "test_r3_fr_config_validation / test_bh_config_validation",
            "R4": "test_r4_inflight_target_edit",
            "R5": "test_r5_response_binding_rejected",
            "R6": "test_r6_file_mode",
            "R7": "test_r7_weights_and_control_types",
            "R8": "test_r8_sessions_and_refresh",
            "O-A": "test_oa_fr_status_machine",
            "cross_model": "test_bh_cross_model_chain_and_cli / test_bh_file_mode_and_bad_response",
            "D10": "test_d10_shared_components_across_models",
            "LIVE-05_state": "test_tab_window_state_after_compute",
            "F1": "test_f1_hit_policy_roundtrip / test_f1_custom_weights_roundtrip",
            "F2": "test_f2_full_scenario_roundtrip",
            "F3": "test_f3_page_validation_matches_python / test_f3_page_rejects_before_apply",
            "P2": "test_p2_reset_and_policy_note",
        },
        "not_in_scope": [
            "D10 真实浏览器视觉（结构/共享入口由 test_d10 覆盖，像素级视觉不在本套件）",
            "O-B 工程注记移除 / O-C 指纹单一来源（由实现方单元测试与 Lead 验收覆盖；本套件不重复断言）",
            "真实浏览器视觉/打印/焦点/读屏验收",
            "真实网络乱序与多浏览器并发",
        ],
        "tests": [],
    }


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    for model in (_SESSION.get("fr"), _SESSION.get("bh")):
        if model and model.get("proc") is not None:
            kill_tree(model["proc"].pid)
            try:
                model["proc"].wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
    evidence = _SESSION.get("evidence")
    if not evidence:
        return
    evidence["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    evidence["tests"] = _TEST_RECORDS
    passed = sum(1 for record in _TEST_RECORDS if record["status"] == "pass")
    evidence["summary"] = {
        "pass": passed,
        "fail": sum(1 for record in _TEST_RECORDS if record["status"] == "fail"),
        "total": len(_TEST_RECORDS),
    }
    path = ACCEPT_ROOT / "behavior-acceptance.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n[P3R acceptance] 证据：%s" % path.relative_to(ROOT).as_posix(), flush=True)


# ---------------------------------------------------------------------------
# 验收基类：新 run + 真实 serve + Node 行为探针
# ---------------------------------------------------------------------------
class P3RAcceptanceBase(unittest.TestCase):
    session_stamp = ""
    work_dir: pathlib.Path
    node: pathlib.Path
    fr: dict
    bh: dict
    evidence: dict
    test_records: list

    @classmethod
    def setUpClass(cls) -> None:
        # 两模型的新 run + serve 由模块级 setUpModule 统一创建，两个测试类共享。
        cls.node = _SESSION["node"]
        cls.session_stamp = _SESSION["stamp"]
        cls.work_dir = _SESSION["work_dir"]
        cls.fr = _SESSION["fr"]
        cls.bh = _SESSION["bh"]
        cls.evidence = _SESSION["evidence"]

    # -- harness helpers -------------------------------------------------
    def _spec_path(self, name: str) -> pathlib.Path:
        return self.work_dir / "tmp" / ("spec-%s.json" % name)

    def _obs_path(self, name: str) -> pathlib.Path:
        return self.work_dir / "tmp" / ("obs-%s.json" % name)

    def run_harness(self, name: str, model: dict, steps: list, *, pages: dict | None = None,
                    protocol: str = "http", endpoint: str | None = None) -> dict:
        main_config = {
            "html": str(model["html"].resolve()),
            "protocol": protocol,
            "servedUrl": "http://127.0.0.1:%d/" % model["port"],
        }
        if endpoint:
            main_config["endpoint"] = endpoint
        page_configs = {"main": main_config}
        if pages:
            for page_name, config in pages.items():
                page_configs[page_name] = dict(config)
                page_configs[page_name].setdefault("protocol", protocol)
        obs_path = self._obs_path(name)
        spec = {
            "pages": page_configs,
            "steps": steps,
            "output": str(obs_path.resolve()),
        }
        spec_path = self._spec_path(name)
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        completed = subprocess.run(
            [str(self.node), str(HARNESS), str(spec_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        stderr = (completed.stderr or "").strip()
        if not obs_path.is_file():
            raise AssertionError("行为探针未产出结果（退出码 %s）：%s\n%s" % (completed.returncode, spec_path, stderr[-3000:]))
        obstacles = json.loads(obs_path.read_text(encoding="utf-8"))
        if not obstacles.get("ok"):
            raise AssertionError(
                "行为探针执行失败：%s\npage errors: %s\nstderr: %s"
                % (
                    obstacles.get("error"),
                    json.dumps(
                        {name: page.get("errors") for name, page in (obstacles.get("pages") or {}).items()},
                        ensure_ascii=False,
                    )[:3000],
                    stderr[-2000:],
                )
            )
        for page in obstacles.get("pages", {}).values():
            if page.get("errors"):
                raise AssertionError("页面 JS 运行错误：%s" % json.dumps(page["errors"], ensure_ascii=False)[:3000])
        return obstacles

    @staticmethod
    def snapshot(obs: dict, name: str) -> dict:
        for step in obs.get("steps", []):
            if step.get("op") == "snapshot" and (step.get("snapshot") or {}).get("name") == name:
                return step["snapshot"]
        raise AssertionError("探针缺少 snapshot：%s" % name)

    @staticmethod
    def page_obs(obs: dict, name: str = "main") -> dict:
        return (obs.get("pages") or {}).get(name) or {}

    @staticmethod
    def requests(obs: dict, page: str = "main") -> list:
        return list(P3RAcceptanceBase.page_obs(obs, page).get("requests") or [])

    @staticmethod
    def last_request(obs: dict, page: str = "main") -> dict:
        requests = P3RAcceptanceBase.requests(obs, page)
        if not requests:
            raise AssertionError("页面没有发出重算请求")
        return requests[-1]["body"]

    @staticmethod
    def status_of(snapshot: dict, attrs: tuple[str, ...] = ("data-cond-status", "data-cond-live-status", "data-cond-import-status", "data-cond-import-error")) -> tuple[str, str]:
        for item in snapshot.get("statuses") or []:
            item_attrs = item.get("attrs") or {}
            if any(name in item_attrs for name in attrs):
                return (str(item.get("text") or ""), str(item_attrs.get("data-status-state") or ""))
        return ("", "")

    @staticmethod
    def control_values(snapshot: dict, attr: str) -> list[dict]:
        found = []
        for control in snapshot.get("controls") or []:
            if attr in control.get("attrs", {}):
                found.append(control)
        return found

    @staticmethod
    def page_buffs_state_controls(snapshot: dict) -> dict:
        states: dict[str, object] = {}
        for control in snapshot.get("controls") or []:
            attrs = control.get("attrs") or {}
            if "data-state" not in attrs or not control.get("inPageBuffs"):
                continue
            if attrs.get("type") == "checkbox":
                states[attrs["data-state"]] = bool(control.get("checked"))
            else:
                states[attrs["data-state"]] = int(control.get("value") or 0)
        return states

    @staticmethod
    def written_result(path: pathlib.Path) -> dict:
        return read_result(path)

    @staticmethod
    def hits_input_values(html_path: pathlib.Path) -> list[str]:
        html = html_path.read_text(encoding="utf-8")
        return re.findall(r'<input[^>]*data-cond-hits[^>]*value="([^"]*)"', html)

    # -- CLI cross-check -------------------------------------------------
    def cli_hit_run(self, scenario: dict, name: str) -> dict:
        out_dir = self.work_dir / "tmp" / ("cli-hit-" + name)
        out_dir.mkdir(parents=True, exist_ok=True)
        scenario_path = self.work_dir / "tmp" / ("scenario-hit-%s.json" % name)
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run_process(
            [
                PYTHON,
                "-m",
                "le_calc",
                "hit-run",
                "--case",
                FR_CASE,
                "--snapshot",
                "examples/flame-reave/snapshot/input-snapshot.hit.json",
                "--ruleset",
                "tools/phase3/rules/ruleset-fr-hit-stage1-v2.json",
                "--scenario",
                str(scenario_path),
                "--out",
                str(out_dir),
                "--run-id",
                "cli-hit-" + name,
                "--no-html",
            ],
            env={"PYTHONPATH": "tools/phase3/src"},
            what="CLI hit-run (%s)" % name,
        )
        return json.loads((out_dir / "result.json").read_text(encoding="utf-8"))

    def cli_bh_run(self, scenario: dict, name: str) -> dict:
        out_dir = self.work_dir / "tmp" / ("cli-bh-" + name)
        out_dir.mkdir(parents=True, exist_ok=True)
        scenario_path = self.work_dir / "tmp" / ("scenario-bh-%s.json" % name)
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run_process(
            [
                PYTHON,
                "-m",
                "le_calc",
                "run",
                "--snapshot",
                "examples/black-hole/snapshot/input-snapshot.affix-db.json",
                "--ruleset",
                "tools/phase3/rules/ruleset-bh-stage1-v4.json",
                "--calc-input",
                "examples/black-hole/inputs/calculation-input.affix-db.json",
                "--build",
                "examples/black-hole/inputs/bd.json",
                "--monster-level",
                "100",
                "--scenario",
                str(scenario_path),
                "--out",
                str(out_dir),
                "--run-id",
                "cli-bh-" + name,
                "--no-html",
            ],
            env={"PYTHONPATH": "tools/phase3/src"},
            what="CLI le_calc run (%s)" % name,
        )
        return json.loads((out_dir / "result.json").read_text(encoding="utf-8"))

    # -- assertions ------------------------------------------------------
    def assert_close(self, actual, expected, *, what: str, tolerance: float = TOLERANCE) -> None:
        actual_f = float(actual)
        expected_f = float(expected)
        scale = max(1.0, abs(expected_f))
        self.assertLessEqual(abs(actual_f - expected_f), tolerance * scale, "%s: %.15g != %.15g" % (what, actual_f, expected_f))

    def assert_no_abs_path(self, payload: object) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIsNone(ABS_PATH_RE.search(text), "结果中出现机器绝对路径：%s" % text[:400])

    # -- evidence --------------------------------------------------------
    def tearDown(self) -> None:
        record = {
            "id": self._testMethodName,
            "status": "pass",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        details = getattr(self, "_details", None)
        if details:
            record["details"] = details
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        if result is not None:
            failures = [
                (test, message)
                for test, message in (result.failures + result.errors)
                if test is self
            ]
            if failures:
                record["status"] = "fail"
                record["message"] = failures[-1][1]
        _TEST_RECORDS.append(record)

    def record(self, **details) -> None:
        self._details = details


# ---------------------------------------------------------------------------
# FR：R1/R2/O-A/R4/R5/R6/R7/R8 + R3
# ---------------------------------------------------------------------------
class FRBehaviorAcceptanceTests(P3RAcceptanceBase):
    def _compute_steps(self, name: str, *, hits: str | None = None, checked: list[tuple[str, bool]] | None = None) -> list:
        steps: list = []
        if hits is not None:
            steps.append({"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": hits})
        for selector, value in checked or []:
            steps.append({"page": "main", "op": "set_checked", "selector": selector, "checked": value})
        steps += [
            {"page": "main", "op": "snapshot", "name": "edited"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / ("written-%s.html" % name)).resolve())},
            {"page": "main", "op": "snapshot", "name": "applied"},
        ]
        return steps

    def test_r1_buff_edits_not_overwritten(self) -> None:
        enemy = '#page-buffs input[data-state="enemy_high_health"]'
        frenzy = '#page-buffs input[data-state="frenzy_active"]'
        obs_off = self.run_harness(
            "r1-enemy-off",
            self.fr,
            self._compute_steps("r1-enemy-off", checked=[(enemy, False)]),
        )
        written_off = self.work_dir / "tmp" / "written-r1-enemy-off.html"
        scenario_off = self.last_request(obs_off)["scenario"]
        edited = self.snapshot(obs_off, "edited")
        self.assertEqual(self.page_buffs_state_controls(edited)["enemy_high_health"], False)
        self.assertEqual(scenario_off["states"]["enemy_high_health"], False)
        result_off = self.written_result(written_off)
        single_off = result_off["single_hit"]["totals"]["single_hit_damage"]
        self.assert_close(single_off, 34786.28114736705, what="R1 关闭高血量单次")
        cli_off = self.cli_hit_run(scenario_off, "r1-enemy-off")
        self.assert_close(single_off, cli_off["single_hit"]["totals"]["single_hit_damage"], what="R1 页面 vs CLI 单次")
        self.assert_close(result_off["dps"]["damage_per_second"], cli_off["dps"]["damage_per_second"], what="R1 页面 vs CLI DPS")
        self.assert_no_abs_path(obs_off)

        obs_on = self.run_harness(
            "r1-frenzy-on",
            self.fr,
            self._compute_steps("r1-frenzy-on", checked=[(frenzy, True)]),
        )
        written_on = self.work_dir / "tmp" / "written-r1-frenzy-on.html"
        scenario_on = self.last_request(obs_on)["scenario"]
        self.assertEqual(scenario_on["states"]["frenzy_active"], True)
        result_on = self.written_result(written_on)
        single_on = result_on["single_hit"]["totals"]["single_hit_damage"]
        self.assert_close(single_on, 53994.58692875591, what="R1 打开 Frenzy 单次")
        cli_on = self.cli_hit_run(scenario_on, "r1-frenzy-on")
        self.assert_close(single_on, cli_on["single_hit"]["totals"]["single_hit_damage"], what="R1 页面 vs CLI 单次（Frenzy）")
        self.record(r1_enemy_off=single_off, r1_frenzy_on=single_on, cli_match=True)

    def test_r2_hits_one_full_chain(self) -> None:
        steps = [
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "1"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r2-first.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "first"},
            {"page": "main", "op": "reload_written"},
            {"page": "main", "op": "snapshot", "name": "reloaded"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r2-second.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "second"},
        ]
        obs = self.run_harness("r2-hits-one", self.fr, steps)
        first_html = self.work_dir / "tmp" / "written-r2-first.html"
        second_html = self.work_dir / "tmp" / "written-r2-second.html"
        request_1 = self.requests(obs)[0]["body"]
        self.assertEqual(request_1["scenario"]["hit_override"]["hits_per_cast"], 1)
        self.assertEqual(request_1["scenario"]["hit_override"]["damage_weights"], [1])
        first = self.written_result(first_html)
        self.assertEqual(first["hits_per_cast"]["per_cast"], 1)
        self.assertEqual(first["hits_per_cast"]["returns"], 0)
        self.assertEqual(first["hits_per_cast"]["effective_hits_per_cast"], 1.0)
        self.assertEqual(first["hits_per_cast"]["damage_weights"], [1.0])
        self.assert_close(first["dps"]["damage_per_second"], 48531.616325667164, what="R2 hits=1 DPS")
        hits_first = self.hits_input_values(first_html)
        self.assertTrue(hits_first, "报告缺少 hits 输入框")
        self.assertEqual(set(hits_first), {"1"})
        cli = self.cli_hit_run(request_1["scenario"], "r2-one-hit")
        self.assert_close(first["single_hit"]["totals"]["single_hit_damage"], cli["single_hit"]["totals"]["single_hit_damage"], what="R2 页面 vs CLI 单次")
        self.assert_close(first["dps"]["damage_per_second"], cli["dps"]["damage_per_second"], what="R2 页面 vs CLI DPS")
        self.assertEqual(first["dps"]["damage_weights"], [1.0])
        second = self.written_result(second_html)
        second_requests = self.requests(obs)
        self.assertEqual(second_requests[-1]["body"]["scenario"]["hit_override"]["hits_per_cast"], 1)
        self.assertEqual(second_requests[-1]["body"]["scenario"]["hit_override"]["damage_weights"], [1])
        self.assertEqual(second["hits_per_cast"]["per_cast"], 1)
        self.assertEqual(second["hits_per_cast"]["effective_hits_per_cast"], 1.0)
        self.assert_close(second["dps"]["damage_per_second"], 48531.616325667164, what="R2 再次计算 DPS")
        hits_second = self.hits_input_values(second_html)
        self.assertTrue(hits_second)
        self.assertEqual(set(hits_second), {"1"})
        self.record(hits_per_cast=1, dps=first["dps"]["damage_per_second"], reapply_same=True, cli_match=True)

    def test_oa_fr_status_machine(self) -> None:
        compute = "#page-buffs [data-cond-compute]"
        steps = [
            {"page": "main", "op": "snapshot", "name": "initial"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "2"},
            {"page": "main", "op": "snapshot", "name": "changed"},
            {"page": "main", "op": "set_fetch", "mode": "hold"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_fetch", "count": 1},
            {"page": "main", "op": "snapshot", "name": "busy"},
            {"page": "main", "op": "release_fetch"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-oa-applied.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "applied"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "3"},
            {"page": "main", "op": "set_fetch", "mode": "fail", "message": "本机服务连接失败"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
            {"page": "main", "op": "snapshot", "name": "failed"},
        ]
        obs = self.run_harness("oa-status", self.fr, steps)
        initial = self.snapshot(obs, "initial")
        changed = self.snapshot(obs, "changed")
        busy = self.snapshot(obs, "busy")
        applied = self.snapshot(obs, "applied")
        failed = self.snapshot(obs, "failed")
        initial_status = self.status_of(initial)
        self.assertIn(initial_status[1], {"applied", "changed"})
        changed_status = self.status_of(changed)
        self.assertEqual(changed_status[1], "changed", changed_status)
        self.assertIn("更改", changed_status[0])
        busy_status = self.status_of(busy)
        self.assertEqual(busy_status[1], "busy", busy_status)
        self.assertIn("计算中", busy_status[0])
        applied_status = self.status_of(applied)
        self.assertEqual(applied_status[1], "applied", applied_status)
        applied_writes = applied["writeCount"]
        self.assertGreaterEqual(applied_writes, 1)
        failed_status = self.status_of(failed)
        self.assertEqual(failed_status[1], "error", failed_status)
        self.assertIn("计算失败", failed_status[0])
        self.assertIn("旧结果", failed_status[0])
        self.assertEqual(failed["writeCount"], applied_writes, "失败不得替换旧结果")
        self.assertEqual(self.control_values(failed, "data-cond-hits")[-1].get("value"), "3", "失败后编辑值应保留")
        written = self.written_result(self.work_dir / "tmp" / "written-oa-applied.html")
        self.assertEqual(written["hits_per_cast"]["per_cast"], 2)
        self.record(statuses=[initial_status, changed_status, busy_status, applied_status, failed_status], failure_kept_old_result=True)

    def test_r4_inflight_target_edit(self) -> None:
        compute = "#page-buffs [data-cond-compute]"
        steps = [
            {"page": "main", "op": "set_value", "selector": "[data-cond-fire-res]", "value": "0"},
            {"page": "main", "op": "set_fetch", "mode": "hold"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_fetch", "count": 1},
            {"page": "main", "op": "set_value", "selector": "[data-cond-fire-res]", "value": "20"},
            {"page": "main", "op": "wait", "ms": 120},
            {"page": "main", "op": "release_fetch"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 30000},
            {"page": "main", "op": "snapshot", "name": "stale"},
            {"page": "main", "op": "set_fetch", "mode": "real"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r4.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "applied"},
        ]
        obs = self.run_harness("r4-inflight", self.fr, steps)
        requests = self.requests(obs)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["body"]["scenario"]["target"]["fire_resistance"], 0.0)
        self.assertEqual(requests[1]["body"]["scenario"]["target"]["fire_resistance"], 0.2)
        self.assertGreater(requests[1]["body"]["client_edit_revision"], requests[0]["body"]["client_edit_revision"])
        stale = self.snapshot(obs, "stale")
        self.assertEqual(stale["writeCount"], 0, "旧响应不得写入 HTML")
        stale_status = self.status_of(stale)
        self.assertEqual(stale_status[1], "changed", stale_status)
        self.assertIn("忽略旧结果", stale_status[0])
        applied = self.snapshot(obs, "applied")
        self.assertEqual(applied["writeCount"], 1)
        self.assertEqual(self.status_of(applied)[1], "applied")
        written = self.written_result(self.work_dir / "tmp" / "written-r4.html")
        self.assertEqual(written["scenario"]["target"]["fire_resistance"], 0.2)
        self.record(revision_first=requests[0]["body"]["client_edit_revision"],
                    revision_second=requests[1]["body"]["client_edit_revision"],
                    stale_writes=stale["writeCount"], applied_writes=applied["writeCount"])

    def test_r5_response_binding_rejected(self) -> None:
        compute = "#page-buffs [data-cond-compute]"
        mutations = {
            "schema": {"set": {"schema_version": "le-recalc-response-v0"}, "delete": []},
            "request_id": {"set": {"request_id": "wrong-request"}, "delete": []},
            "edit_revision": {"set": {"client_edit_revision": 0}, "delete": []},
            "accepted_missing": {"set": {}, "delete": ["accepted_revision"]},
            "case": {"set": {"case_id": "foreign-case"}, "delete": []},
            "model": {"set": {"model_id": "foreign-model"}, "delete": []},
            "capabilities": {"set": {"capabilities_version": "cap:v0"}, "delete": []},
            "calc_input": {"set": {"input_fingerprints.calc_input": "0" * 64}, "delete": []},
            "report_ref": {"set": {"report_ref.run_id": "other-run"}, "delete": []},
            "endpoint": {"set": {"recompute_endpoint": "http://127.0.0.1:19999/recompute"}, "delete": []},
            "html": {"set": {}, "delete": ["html"]},
            "accepted_false": {"set": {"accepted": False}, "delete": []},
        }
        steps = [{"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "2"}]
        for name, mutate in mutations.items():
            steps += [
                {"page": "main", "op": "set_fetch", "mode": "mutate", "mutate": mutate},
                {"page": "main", "op": "click", "selector": compute},
                {"page": "main", "op": "wait_idle", "timeoutMs": 20000},
                {"page": "main", "op": "snapshot", "name": "after-" + name},
            ]
        steps += [
            {"page": "main", "op": "set_fetch", "mode": "real"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r5-valid.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "valid"},
        ]
        obs = self.run_harness("r5-bindings", self.fr, steps)
        rejected = {}
        for name in mutations:
            snapshot = self.snapshot(obs, "after-" + name)
            self.assertEqual(snapshot["writeCount"], 0, "%s 错误响应不得应用" % name)
            text, state = self.status_of(snapshot)
            self.assertEqual(state, "error", "%s 应显示错误提示：%r" % (name, text))
            self.assertTrue(text, "%s 错误提示为空" % name)
            rejected[name] = text[:160]
        valid = self.snapshot(obs, "valid")
        self.assertEqual(valid["writeCount"], 1)
        self.assertEqual(self.status_of(valid)[1], "applied")
        written = self.written_result(self.work_dir / "tmp" / "written-r5-valid.html")
        self.assertEqual(written["hits_per_cast"]["per_cast"], 2)
        self.assertEqual(set(self.hits_input_values(self.work_dir / "tmp" / "written-r5-valid.html")), {"2"})
        self.record(rejected=rejected, valid_applied=True)

    def test_r6_file_mode(self) -> None:
        compute = "#page-buffs [data-cond-compute]"
        steps = [
            {"page": "main", "op": "snapshot", "name": "load"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "2"},
            {"page": "main", "op": "set_fetch", "mode": "hold"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_fetch", "count": 1},
            {"page": "main", "op": "release_fetch"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r6-file.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "applied"},
        ]
        obs = self.run_harness("r6-file", self.fr, steps)
        page = self.page_obs(obs)
        self.assertEqual(page.get("loadFetchCount"), 0, "file:// 加载不得发请求")
        self.assertEqual(len(self.requests(obs)), 1)
        self.assertEqual(page.get("writeCount"), 1)
        self.assertNotEqual(self.fr["port"], 8765)
        bindings = json.loads(re.search(r'<script[^>]*id="le-recalc-bindings"[^>]*>([\s\S]*?)</script>', self.fr["html"].read_text(encoding="utf-8")).group(1))
        self.assertIn(":%d/" % self.fr["port"], bindings["recompute_endpoint"])

        fail_steps = [
            {"page": "main", "op": "snapshot", "name": "load"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "2"},
            {"page": "main", "op": "set_fetch", "mode": "fail", "message": "本机服务连接失败"},
            {"page": "main", "op": "click", "selector": compute},
            {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
            {"page": "main", "op": "snapshot", "name": "failed"},
        ]
        obs_fail = self.run_harness("r6-file-fail", self.fr, fail_steps, protocol="file", endpoint="http://127.0.0.1:19999/recompute")
        load = self.snapshot(obs_fail, "load")
        self.assertEqual(load["writeCount"], 0)
        self.assertEqual(len(self.requests(obs_fail)), 1)
        failed = self.snapshot(obs_fail, "failed")
        self.assertEqual(failed["writeCount"], 0)
        text, state = self.status_of(failed)
        self.assertEqual(state, "error")
        self.assertIn("计算失败", text)
        self.assertIn("旧结果", text)
        self.assertEqual(self.control_values(failed, "data-cond-hits")[-1].get("value"), "2")
        written = self.written_result(self.work_dir / "tmp" / "written-r6-file.html")
        self.assertEqual(written["hits_per_cast"]["per_cast"], 2)
        self.record(file_load_requests=0, file_compute_requests=1, fail_kept_edited_value=True)

    def test_r7_weights_and_control_types(self) -> None:
        valid = self._fr_valid_config()
        config = json.loads(json.dumps(valid))
        config["scenario"]["hit_override"] = {"hits_per_cast": 3, "damage_weights": [1, 0.25, 0.75]}
        config_path = self.work_dir / "tmp" / "config-r7-custom-weights.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        steps = [
            {"page": "main", "op": "load_config", "selector": "#page-buffs [data-cond-load]", "contentPath": str(config_path), "filename": "custom-weights.json"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r7.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "computed"},
            {"page": "main", "op": "reload_written"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-r7-second.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "second"},
        ]
        obs = self.run_harness("r7-weights", self.fr, steps)
        requests = self.requests(obs)
        self.assertEqual(len(requests), 2)
        for request in requests:
            override = request["body"]["scenario"]["hit_override"]
            self.assertEqual(override["hits_per_cast"], 3)
            self.assertEqual([float(x) for x in override["damage_weights"]], [1.0, 0.25, 0.75])
        first = self.written_result(self.work_dir / "tmp" / "written-r7.html")
        second = self.written_result(self.work_dir / "tmp" / "written-r7-second.html")
        self.assertEqual([float(x) for x in first["hits_per_cast"]["damage_weights"]], [1.0, 0.25, 0.75])
        self.assertEqual([float(x) for x in second["hits_per_cast"]["damage_weights"]], [1.0, 0.25, 0.75])
        self.assertEqual(first["hits_per_cast"]["per_cast"], 3)
        self.assertEqual(second["hits_per_cast"]["per_cast"], 3)
        self.assertEqual(set(self.hits_input_values(self.work_dir / "tmp" / "written-r7.html")), {"3"})
        # 控件类型与 capabilities 一致
        capabilities = embedded_json(self.fr["html"].read_text(encoding="utf-8"), "report-capabilities")
        declarations = {item["state_key"]: item for item in (capabilities.get("capabilities") or capabilities).get("buff_controls") or []}
        computed = self.snapshot(obs, "computed")
        mismatch = []
        for control in computed.get("controls") or []:
            attrs = control.get("attrs") or {}
            state = attrs.get("data-state")
            if not state or state not in declarations:
                continue
            kind = str(declarations[state].get("kind") or "toggle").lower()
            declared_type = "checkbox" if kind in ("toggle", "bool", "boolean") else "number"
            if attrs.get("type") != declared_type:
                mismatch.append({"state": state, "kind": kind, "type": attrs.get("type")})
        self.assertEqual(mismatch, [], "控件类型与 capabilities 不一致")
        self.record(weights_preserved=[1.0, 0.25, 0.75], weights_after_reload=True, control_type_mismatches=mismatch)

    def test_r8_sessions_and_refresh(self) -> None:
        html = str(self.fr["html"].resolve())
        pages = {
            "A": {"html": html, "protocol": "http", "servedUrl": "http://127.0.0.1:%d/" % self.fr["port"]},
            "B": {"html": html, "protocol": "http", "servedUrl": "http://127.0.0.1:%d/" % self.fr["port"]},
        }
        compute = "#page-buffs [data-cond-compute]"
        steps = [
            {"page": "A", "op": "click", "selector": compute}, {"page": "A", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "A", "op": "snapshot", "name": "A1"},
            {"page": "B", "op": "click", "selector": compute}, {"page": "B", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "B", "op": "snapshot", "name": "B1"},
            {"page": "A", "op": "click", "selector": compute}, {"page": "A", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "A", "op": "snapshot", "name": "A2"},
            {"page": "A", "op": "reload_written"},
            {"page": "A", "op": "click", "selector": compute}, {"page": "A", "op": "wait_idle", "timeoutMs": 40000},
            {"page": "A", "op": "snapshot", "name": "A3"},
        ]
        obs = self.run_harness("r8-sessions", self.fr, steps, pages=pages)
        for page_name in ("A", "B"):
            responses = self.page_obs(obs, page_name).get("responses") or []
            self.assertTrue(responses, "%s 没有响应" % page_name)
            for response in responses:
                self.assertTrue(response.get("ok"), "%s 响应失败：%s" % (page_name, response.get("error")))
                self.assertEqual(response.get("status"), 200, page_name)
        a_requests = self.requests(obs, "A")
        b_requests = self.requests(obs, "B")
        self.assertGreaterEqual(len(a_requests), 3)
        self.assertEqual(len(b_requests), 1)
        a_sessions = {req["body"].get("client_session_id") for req in a_requests[:2]}
        self.assertEqual(len(a_sessions), 1, "同一页面会话应复用 client_session_id")
        self.assertNotEqual(a_requests[0]["body"].get("client_session_id"), b_requests[0]["body"].get("client_session_id"))
        self.assertNotEqual(a_requests[2]["body"].get("client_session_id"), a_requests[0]["body"].get("client_session_id"))
        self.assertGreater(a_requests[1]["body"]["client_edit_revision"], a_requests[0]["body"]["client_edit_revision"])
        a1 = self.snapshot(obs, "A1")
        b1 = self.snapshot(obs, "B1")
        a2 = self.snapshot(obs, "A2")
        a3 = self.snapshot(obs, "A3")
        self.assertEqual(a1["writeCount"], 1)
        self.assertEqual(b1["writeCount"], 1)
        self.assertEqual(a2["writeCount"], 2, "A 的第二次合法请求不得被 stale 拒绝")
        self.assertEqual(a3["writeCount"], 1, "刷新后新页面可正常计算")
        for snapshot in (a1, b1, a2, a3):
            self.assertEqual(self.status_of(snapshot)[1], "applied")
        self.record(a_sessions=len(a_sessions), b_session_distinct=True, stale_rejected=False, refresh_accepted=True)

    def test_tab_window_state_after_compute(self) -> None:
        """LIVE-05：在非总览页计算后页签/窗口保持、状态 applied、导出 JSON 仍可用（两模型）。"""
        cases = (
            ("FR", self.fr, '[data-page-btn="buffs"]', '#page-buffs [data-cond-compute]', None),
            ("BH", self.bh, '[data-page-btn="conditions"]', "[data-cond-live-recompute]", '[data-window-btn="5"]'),
        )
        for label, model, tab_selector, compute_selector, window_selector in cases:
            target_page = "buffs" if label == "FR" else "conditions"
            steps = [
                {"page": "main", "op": "click", "selector": tab_selector},
                {"page": "main", "op": "snapshot", "name": "before"},
            ]
            if window_selector:
                steps += [
                    {"page": "main", "op": "click", "selector": window_selector},
                    {"page": "main", "op": "snapshot", "name": "window-selected"},
                ]
            written = self.work_dir / "tmp" / ("written-%s-tab-state.html" % label.lower())
            steps += [
                {"page": "main", "op": "click", "selector": compute_selector},
                {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
                {"page": "main", "op": "save_written", "path": str(written.resolve())},
                {"page": "main", "op": "snapshot", "name": "after-compute"},
                {"page": "main", "op": "reload_written"},
                {"page": "main", "op": "snapshot", "name": "after-reload"},
                {"page": "main", "op": "click", "selector": "[data-export-json]"},
                {"page": "main", "op": "snapshot", "name": "exported"},
            ]
            obs = self.run_harness("live05-%s" % label.lower(), model, steps)
            before = self.snapshot(obs, "before")
            self.assertEqual(before["uiState"]["activePage"], target_page, "%s 页签未切换" % label)
            after_compute = self.snapshot(obs, "after-compute")
            self.assertGreaterEqual(after_compute["writeCount"], 1, "%s 计算未应用" % label)
            self.assertEqual(after_compute["uiState"]["activePage"], target_page, "%s 计算后页签丢失" % label)
            applied_states = [item["state"] for item in after_compute["uiState"].get("statusStates") or []]
            self.assertIn("applied", applied_states, "%s 计算后状态不是 applied：%s" % (label, applied_states))
            if window_selector:
                selected = self.snapshot(obs, "window-selected")
                self.assertEqual(selected["uiState"]["activeWindow"], "5", "%s 窗口选择未生效" % label)
                self.assertEqual(after_compute["uiState"]["activeWindow"], "5", "%s 计算后窗口选择丢失" % label)
            after_reload = self.snapshot(obs, "after-reload")
            self.assertEqual(after_reload["uiState"]["activePage"], target_page, "%s 刷新后页签丢失" % label)
            reload_states = [item["state"] for item in after_reload["uiState"].get("statusStates") or []]
            self.assertIn("applied", reload_states, "%s 刷新后状态不是 applied：%s" % (label, reload_states))
            if window_selector:
                self.assertEqual(after_reload["uiState"]["activeWindow"], "5", "%s 刷新后窗口选择丢失" % label)
            exported = self.snapshot(obs, "exported")
            downloads = exported.get("downloads") or []
            result_download = next(
                (item for item in downloads if item.get("content") and str(item.get("download") or "").endswith(".json")),
                None,
            )
            self.assertIsNotNone(result_download, "%s 导出结果 JSON 未生成" % label)
            payload = json.loads(result_download["content"])
            self.assertIn("consistency", payload, "%s 导出 JSON 不是结果数据" % label)
            self.assertTrue((payload.get("consistency") or {}).get("all_pass"), "%s 导出结果一致性未通过" % label)
            self.assertIn("run", payload)
            self.assertIn("input", payload)
            self.record(result_json_export_chars=len(result_download["content"]), active_page=target_page)

    def test_d10_shared_components_across_models(self) -> None:
        """D10：两模型使用同一来源表/效果卡/操作栏共享实现，且审查脚本依赖的 DOM 未丢失。"""
        fr_html = self.fr["html"].read_text(encoding="utf-8")
        bh_html = self.bh["html"].read_text(encoding="utf-8")
        shared_markers = (
            'data-shared-component="source-table"',
            'data-shared-component="action-bar"',
            'data-shared-component="effect-card"',
        )
        source_header = '<thead><tr><th scope="col">来源</th><th scope="col">效果</th><th scope="col">状态</th></tr></thead>'
        for label, html in (("FR", fr_html), ("BH", bh_html)):
            for marker in shared_markers:
                self.assertIn(marker, html, "%s 未使用共享组件 %s" % (label, marker))
            self.assertIn(source_header, html, "%s 来源表不是共享三列结构" % label)
            self.assertIn("data-source-group", html)
            self.assertIn("data-source-row", html)

        def shared_actions_digest(html: str) -> str:
            for match in re.finditer(r"<script([^>]*)>([\s\S]*?)</script>", html):
                body = match.group(2)
                if re.search(r"window\.ReportActions\s*=", body):
                    return hashlib.sha256(body.encode("utf-8")).hexdigest()
            raise AssertionError("HTML 缺少共享 ReportActions 实现")

        fr_digest = shared_actions_digest(fr_html)
        bh_digest = shared_actions_digest(bh_html)
        self.assertEqual(fr_digest, bh_digest, "两模型的共享配置/状态实现不一致")

        fr_dom = ("data-cond", "data-state=", "data-cond-hits", "data-cond-compute",
                  "data-cond-load", "data-cond-preset", "data-cond-status", "data-apply-scenario")
        bh_dom = ("data-cond", "data-cond-live-recompute", "data-cond-import", "data-cond-preset",
                  "data-cond-status", "data-scenario-fire-res", "data-apply-scenario")
        for label, html, markers in (("FR", fr_html, fr_dom), ("BH", bh_html, bh_dom)):
            for marker in markers:
                self.assertIn(marker, html, "%s 丢失审查脚本依赖的 DOM 约定：%s" % (label, marker))
        self.assertIn('id="page-buffs"', fr_html)
        self.assertIn('id="page-conditions"', bh_html)

        fr_boot = self.run_harness("d10-boot-fr", self.fr, [
            {"page": "main", "op": "snapshot", "name": "boot"},
        ])
        bh_boot = self.run_harness("d10-boot-bh", self.bh, [
            {"page": "main", "op": "snapshot", "name": "boot"},
        ])
        for label, obs in (("FR", fr_boot), ("BH", bh_boot)):
            page = self.page_obs(obs)
            self.assertTrue(page.get("hasLeRecalc"), "%s 未加载共享重算客户端" % label)
            self.assertTrue(page.get("hasReportActions"), "%s 未加载共享操作/配置实现" % label)
            self.assertEqual(page.get("loadFetchCount"), 0, "%s 加载时不应发请求" % label)
            self.assertEqual(self.snapshot(obs, "boot")["writeCount"], 0)
        self.record(shared_actions_sha256=fr_digest, shared_markers=list(shared_markers),
                    fr_dom_preserved=True, bh_dom_preserved=True)

    # -- round-2 (F1/F2/F3/P2) helpers -----------------------------------
    @staticmethod
    def _visible_text(html: str) -> str:
        text = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text)

    def _embedded(self, html: str, script_id: str) -> dict:
        return embedded_json(html, script_id)

    def _fr_caps(self) -> dict:
        cached = getattr(self.__class__, "_fr_caps_cache", None)
        if cached is None:
            payload = self._embedded(self.fr["html"].read_text(encoding="utf-8"), "report-capabilities")
            cached = payload.get("capabilities") or payload
            self.__class__._fr_caps_cache = cached
        return cached

    def _fr_bindings(self) -> dict:
        cached = getattr(self.__class__, "_fr_bindings_cache", None)
        if cached is None:
            cached = self._embedded(self.fr["html"].read_text(encoding="utf-8"), "le-recalc-bindings")
            self.__class__._fr_bindings_cache = cached
        return cached

    def _python_config_validation(self, payload: dict, *, current_binding: dict | None = None,
                                  capabilities: dict | None = None) -> dict:
        import sys

        calc_src = str(ROOT / "tools" / "phase3" / "src")
        if calc_src not in sys.path:
            sys.path.insert(0, calc_src)
        from le_calc import recalc

        return recalc.validate_report_config(
            payload,
            current_bindings=current_binding if current_binding is not None else self._fr_bindings(),
            capabilities=capabilities if capabilities is not None else self._fr_caps(),
        )

    def _page_config_validation(self, payload: dict, *, current_binding: dict,
                                capabilities: dict, name: str) -> dict:
        obs = self.run_harness(
            name,
            self.fr,
            [
                {
                    "page": "main",
                    "op": "call",
                    "expression": "window.ReportActions.validateEnvelope",
                    "args": [payload, {"currentBinding": current_binding, "capabilities": capabilities}],
                },
            ],
        )
        step = next(item for item in obs["steps"] if item.get("op") == "call")
        return step.get("result") or {}

    def _save_config_from_obs(self, obs: dict, snapshot_name: str = "saved") -> dict:
        snapshot = self.snapshot(obs, snapshot_name)
        downloads = snapshot.get("downloads") or []
        self.assertTrue(downloads, "保存配置未产生下载")
        return decode_download(downloads[-1])

    def _load_config_steps(self, path: pathlib.Path, name: str = "loaded") -> list:
        return [
            {"page": "main", "op": "load_config", "selector": "#page-buffs [data-cond-load]",
             "contentPath": str(path.resolve()), "filename": path.name},
            {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
            {"page": "main", "op": "snapshot", "name": name},
        ]

    def _fr_config_path(self, name: str, payload: dict) -> pathlib.Path:
        path = self.work_dir / "tmp" / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    # -- F1 --------------------------------------------------------------
    def test_f1_hit_policy_roundtrip(self) -> None:
        steps = [
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "1"},
            {"page": "main", "op": "snapshot", "name": "one-hit"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-f1-one.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "applied-one"},
            {"page": "main", "op": "reload_written"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "4"},
            {"page": "main", "op": "snapshot", "name": "back-four"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-f1-four.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "applied-four"},
        ]
        obs = self.run_harness("f1-policy", self.fr, steps)
        requests = self.requests(obs)
        self.assertEqual(len(requests), 2, requests)
        one = requests[0]["body"]["scenario"]["hit_override"]
        self.assertEqual(one.get("hits_per_cast"), 1, one)
        self.assertEqual([float(x) for x in one.get("damage_weights") or []], [1.0], one)
        four = requests[1]["body"]["scenario"]["hit_override"]
        self.assertEqual(four.get("hits_per_cast"), 4, four)
        self.assertEqual([float(x) for x in four.get("damage_weights") or []], [1.0, 0.5, 0.5, 0.5])
        result = self.written_result(self.work_dir / "tmp" / "written-f1-four.html")
        self.assert_close(result["dps"]["damage_per_second"], 121329.04081416794, what="F1 4→1→4 恢复默认政策 DPS")
        self.assertFalse(abs(float(result["dps"]["damage_per_second"]) - 194126.46530266866) < 1.0,
                         "F1 恢复默认政策后不应得到 [1,1,1,1] 的 194,126.47")
        self.assertEqual([float(x) for x in result["hits_per_cast"]["damage_weights"]], [1.0, 0.5, 0.5, 0.5])
        cli = self.cli_hit_run(requests[1]["body"]["scenario"], "f1-restored")
        self.assert_close(result["dps"]["damage_per_second"], cli["dps"]["damage_per_second"], what="F1 页面 vs CLI DPS")
        self.record(default_weights=[1.0, 0.5, 0.5, 0.5], dps=result["dps"]["damage_per_second"])

    def test_f1_custom_weights_roundtrip(self) -> None:
        valid = self._fr_valid_config()
        config = copy.deepcopy(valid)
        config["scenario"]["hit_override"] = {"hits_per_cast": 4, "damage_weights": [1, 1, 1, 1]}
        path = self._fr_config_path("f1-custom-config.json", config)
        steps = self._load_config_steps(path)
        steps += [
            {"page": "main", "op": "snapshot", "name": "loaded"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "1"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-f1-custom-one.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "one"},
            {"page": "main", "op": "set_value", "selector": "#page-buffs [data-cond-hits]", "value": "4"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-f1-custom-four.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "four"},
        ]
        obs = self.run_harness("f1-custom", self.fr, steps)
        requests = self.requests(obs)
        self.assertEqual(len(requests), 2, requests)
        self.assertEqual(requests[0]["body"]["scenario"]["hit_override"]["hits_per_cast"], 1)
        self.assertEqual([float(x) for x in requests[0]["body"]["scenario"]["hit_override"]["damage_weights"]], [1.0])
        four = requests[1]["body"]["scenario"]["hit_override"]
        self.assertEqual(four["hits_per_cast"], 4)
        self.assertEqual([float(x) for x in four["damage_weights"]], [1.0, 1.0, 1.0, 1.0])
        result = self.written_result(self.work_dir / "tmp" / "written-f1-custom-four.html")
        self.assertEqual([float(x) for x in result["hits_per_cast"]["damage_weights"]], [1.0, 1.0, 1.0, 1.0])
        cli = self.cli_hit_run(requests[1]["body"]["scenario"], "f1-custom")
        self.assert_close(result["dps"]["damage_per_second"], cli["dps"]["damage_per_second"], what="F1 自定义权重页面 vs CLI")
        self.record(custom_weights=[1.0, 1.0, 1.0, 1.0], restored=True, dps=result["dps"]["damage_per_second"])

    # -- F2 --------------------------------------------------------------
    def test_f2_full_scenario_roundtrip(self) -> None:
        valid = self._fr_valid_config()
        section = copy.deepcopy(valid)
        section["scenario"]["single_hit"] = {"states": {"frenzy_active": True}}
        section_path = self._fr_config_path("f2-section-config.json", section)
        steps = self._load_config_steps(section_path)
        steps += [
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-save]"},
            {"page": "main", "op": "snapshot", "name": "saved"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-f2-section.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "computed"},
        ]
        obs = self.run_harness("f2-section", self.fr, steps)
        loaded = self.snapshot(obs, "loaded")
        loaded_text, loaded_state = self.status_of(loaded)
        self.assertEqual(loaded_state, "changed", loaded_text)
        saved = self._save_config_from_obs(obs)
        saved_single = (saved.get("scenario") or {}).get("single_hit")
        self.assertEqual(saved_single, {"states": {"frenzy_active": True}},
                         "F2 载入后再次保存丢失 single_hit 段")
        request = self.last_request(obs)
        self.assertEqual(request["scenario"].get("single_hit"), {"states": {"frenzy_active": True}})
        result = self.written_result(self.work_dir / "tmp" / "written-f2-section.html")
        self.assert_close(result["single_hit"]["totals"]["single_hit_damage"], 53994.58692875591,
                          what="F2 single_hit frenzy 单次")
        self.assert_close(result["dps"]["damage_per_second"], 126392.32890239611,
                          what="F2 single_hit frenzy DPS")
        cli = self.cli_hit_run(request["scenario"], "f2-section")
        self.assert_close(result["single_hit"]["totals"]["single_hit_damage"],
                          cli["single_hit"]["totals"]["single_hit_damage"], what="F2 页面 vs CLI 单次")
        self.assert_close(result["dps"]["damage_per_second"], cli["dps"]["damage_per_second"],
                          what="F2 页面 vs CLI DPS")

        hidden = copy.deepcopy(valid)
        hidden["scenario"]["states"]["enchant_weapon_active"] = False
        hidden_path = self._fr_config_path("f2-hidden-config.json", hidden)
        hidden_steps = self._load_config_steps(hidden_path)
        hidden_steps += [
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-save]"},
            {"page": "main", "op": "snapshot", "name": "saved"},
        ]
        hidden_obs = self.run_harness("f2-hidden", self.fr, hidden_steps)
        hidden_saved = self._save_config_from_obs(hidden_obs)
        self.assertIs(hidden_saved["scenario"]["states"].get("enchant_weapon_active"), False,
                      "F2 未渲染但声明支持的 state 在往返中丢失")

        unsupported = copy.deepcopy(valid)
        unsupported["scenario"]["single_hit"] = {"states": {"totally_unknown_state": True}}
        unsupported_path = self._fr_config_path("f2-unsupported-config.json", unsupported)
        unsupported_steps = [
            {"page": "main", "op": "set_value", "selector": "[data-cond-fire-res]", "value": "55"},
            {"page": "main", "op": "snapshot", "name": "before"},
        ] + self._load_config_steps(unsupported_path)
        unsupported_obs = self.run_harness("f2-unsupported", self.fr, unsupported_steps)
        before = self.snapshot(unsupported_obs, "before")
        after = self.snapshot(unsupported_obs, "loaded")
        text, state = self.status_of(after)
        self.assertEqual(state, "error", text)
        before_fire = self.control_values(before, "data-cond-fire-res")[-1].get("value")
        after_fire = self.control_values(after, "data-cond-fire-res")[-1].get("value")
        self.assertEqual(after_fire, before_fire, "F2 不支持字段被接受后才丢弃")
        py = self._python_config_validation(unsupported)
        self.assertFalse(py["ok"])
        self.assertIn("unknown_scenario_field", [issue["code"] for issue in py["issues"]])
        self.record(single_hit_preserved=True, hidden_state_preserved=True, unsupported_rejected=True,
                    single=result["single_hit"]["totals"]["single_hit_damage"],
                    dps=result["dps"]["damage_per_second"])

    # -- P2 --------------------------------------------------------------
    def test_p2_reset_and_policy_note(self) -> None:
        valid = self._fr_valid_config()
        config = copy.deepcopy(valid)
        config["scenario"]["hit_override"] = {"hits_per_cast": 4, "damage_weights": [1, 1, 1, 1]}
        path = self._fr_config_path("p2-custom-config.json", config)
        steps = self._load_config_steps(path)
        steps += [
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-reset]"},
            {"page": "main", "op": "wait", "ms": 50},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-save]"},
            {"page": "main", "op": "snapshot", "name": "reset-saved"},
            {"page": "main", "op": "snapshot", "name": "reset"},
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-p2-reset.html").resolve())},
        ]
        obs = self.run_harness("p2-reset", self.fr, steps)
        saved = self._save_config_from_obs(obs, "reset-saved")
        override = saved["scenario"]["hit_override"]
        self.assertEqual(override["hits_per_cast"], 4, override)
        self.assertEqual([float(x) for x in override["damage_weights"]], [1.0, 0.5, 0.5, 0.5],
                         "P2 重置后权重必须回到不可变初始政策")
        request = self.last_request(obs)
        self.assertEqual([float(x) for x in request["scenario"]["hit_override"]["damage_weights"]],
                         [1.0, 0.5, 0.5, 0.5])
        result = self.written_result(self.work_dir / "tmp" / "written-p2-reset.html")
        self.assertEqual([float(x) for x in result["hits_per_cast"]["damage_weights"]], [1.0, 0.5, 0.5, 0.5])
        note_text = self._visible_text(
            (self.work_dir / "tmp" / "written-p2-reset.html").read_text(encoding="utf-8")
        )
        self.assertIn("后续命中按 50%", note_text, "P2 默认政策文案缺失")
        # 自定义权重的文案不得照抄默认 50% 政策
        custom_steps = self._load_config_steps(path)
        custom_steps += [
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-compute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-p2-custom.html").resolve())},
        ]
        custom_obs = self.run_harness("p2-custom-note", self.fr, custom_steps)
        custom_text = self._visible_text(
            (self.work_dir / "tmp" / "written-p2-custom.html").read_text(encoding="utf-8")
        )
        self.assertNotIn("后续命中按 50%", custom_text, "P2 自定义权重文案仍写死默认 50% 政策")
        self.record(default_reset_weights=[1.0, 0.5, 0.5, 0.5], custom_note_dynamic=True)

    # -- F3 --------------------------------------------------------------
    def _f3_adjusted_capabilities(self, fixture_binding: dict) -> dict:
        caps = copy.deepcopy(self._fr_caps())
        version = (fixture_binding.get("recalculation") or {}).get("capabilities_version") \
            if isinstance(fixture_binding.get("recalculation"), dict) else None
        version = version or fixture_binding.get("capabilities_version")
        recalculation = caps.setdefault("recalculation", {})
        if version:
            recalculation["capabilities_version"] = version
        return caps

    def test_f3_page_validation_matches_python(self) -> None:
        examples_path = ROOT / "tools" / "phase3" / "schemas" / "le-report-config-v1.examples.json"
        examples = json.loads(examples_path.read_text(encoding="utf-8"))
        cases = examples.get("cases") or []
        self.assertTrue(cases, "examples 文件缺少 cases")
        mismatches = []
        for index, case in enumerate(cases):
            payload = case["payload"]
            current_binding = case["current_binding"]
            capabilities = self._f3_adjusted_capabilities(current_binding)
            py = self._python_config_validation(payload, current_binding=current_binding,
                                                capabilities=capabilities)
            page = self._page_config_validation(
                payload, current_binding=current_binding, capabilities=capabilities,
                name="f3-example-%02d-%s" % (index, case.get("id", "case")),
            )
            py_codes = [issue["code"] for issue in py["issues"] if issue.get("severity") == "error"]
            expect = case.get("expect") or {}
            self.assertEqual(bool(py["ok"]), bool(expect.get("ok")), "case %s Python ok 与 expect 不一致" % case.get("id"))
            if py_codes:
                expected_codes = expect.get("error_codes") or []
                if expected_codes:
                    self.assertEqual(sorted(py_codes), sorted(expected_codes), "case %s Python codes" % case.get("id"))
            self.assertEqual(bool(page.get("ok")), bool(py["ok"]),
                             "case %s 页面/Python ok 不一致：page=%r py=%r" % (case.get("id"), page, py_codes))
            if not py["ok"]:
                page_code = page.get("code")
                self.assertIn(page_code, py_codes,
                              "case %s 页面 code=%r 不在 Python codes=%r" % (case.get("id"), page_code, py_codes))
                if page_code != py_codes[0]:
                    mismatches.append((case.get("id"), page_code, py_codes))
        self.assertEqual(mismatches, [], "F3 页面/Python 首个错误码顺序不一致：%r" % mismatches)

        # 审查反例：同一批语义同时驱动 Python 与页面 JS
        fixture_binding = (examples.get("fixtures") or {}).get("binding") or cases[0]["current_binding"]
        fixture_scenario = copy.deepcopy((examples.get("fixtures") or {}).get("scenario") or cases[0]["payload"]["scenario"])
        base_payload = {"schema_version": "le-report-config-v1", "binding": copy.deepcopy(fixture_binding),
                        "scenario": fixture_scenario}
        required = {
            "invalid_fire": (lambda c: c["scenario"]["target"].__setitem__("fire_resistance", 2.0), "out_of_range"),
            "numeric_string": (lambda c: c["scenario"]["states"].__setitem__("ambition_stacks", "5"), "invalid_scenario_value"),
            "nested_ref": (lambda c: c["binding"]["report_ref"]["input_fingerprints"].__setitem__("ruleset", "0" * 64),
                           "config_report_ref_mismatch"),
        }
        caps = self._f3_adjusted_capabilities(fixture_binding)
        required_results = {}
        for name, (mutate, expected_code) in required.items():
            payload = copy.deepcopy(base_payload)
            mutate(payload)
            py = self._python_config_validation(payload, current_binding=fixture_binding, capabilities=caps)
            page = self._page_config_validation(payload, current_binding=fixture_binding,
                                                capabilities=caps, name="f3-required-" + name)
            py_codes = [issue["code"] for issue in py["issues"] if issue.get("severity") == "error"]
            self.assertIn(expected_code, py_codes, "%s Python code 缺失：%r" % (name, py_codes))
            self.assertFalse(py["ok"], name)
            self.assertFalse(page.get("ok"), "%s 页面未拒绝：%r" % (name, page))
            self.assertEqual(page.get("code"), py_codes[0], "%s 页面/Python code 不一致" % name)
            required_results[name] = {"python_codes": py_codes, "page_code": page.get("code")}
        valid = copy.deepcopy(base_payload)
        py_valid = self._python_config_validation(valid, current_binding=fixture_binding, capabilities=caps)
        page_valid = self._page_config_validation(valid, current_binding=fixture_binding,
                                                  capabilities=caps, name="f3-required-valid")
        self.assertTrue(py_valid["ok"], py_valid["issues"])
        self.assertTrue(page_valid.get("ok"), page_valid)
        self.record(example_count=len(cases), code_order_mismatches=mismatches,
                    required=required_results)

    def test_f3_page_rejects_before_apply(self) -> None:
        valid = self._fr_valid_config()
        bindings = self._fr_bindings()
        caps = self._fr_caps()
        cases = {}
        invalid_fire = copy.deepcopy(valid)
        invalid_fire["scenario"]["target"]["fire_resistance"] = 2.0
        cases["invalid_fire"] = (invalid_fire, "out_of_range")
        numeric_string = copy.deepcopy(valid)
        numeric_string["scenario"]["states"]["ambition_stacks"] = "5"
        cases["numeric_string"] = (numeric_string, "invalid_scenario_value")
        nested_ref = copy.deepcopy(valid)
        nested_ref["binding"]["report_ref"]["input_fingerprints"]["ruleset"] = "0" * 64
        cases["nested_ref"] = (nested_ref, "config_report_ref_mismatch")
        base_steps = [
            {"page": "main", "op": "set_value", "selector": "[data-cond-fire-res]", "value": "55"},
            {"page": "main", "op": "snapshot", "name": "before"},
        ]
        payloads = {}
        for name, (payload, expected_code) in cases.items():
            py = self._python_config_validation(payload)
            py_codes = [issue["code"] for issue in py["issues"] if issue.get("severity") == "error"]
            self.assertFalse(py["ok"], name)
            self.assertEqual(py_codes[0], expected_code, "%s Python code" % name)
            page = self._page_config_validation(payload, current_binding=bindings, capabilities=caps,
                                                name="f3-atomic-call-" + name)
            self.assertFalse(page.get("ok"), name)
            self.assertEqual(page.get("code"), expected_code, "%s 页面 code" % name)
            path = self._fr_config_path("f3-atomic-%s.json" % name, payload)
            base_steps += self._load_config_steps(path, name="after-" + name)
        valid_path = self._fr_config_path("f3-atomic-valid.json", valid)
        base_steps += self._load_config_steps(valid_path, name="after-valid")
        obs = self.run_harness("f3-atomic", self.fr, base_steps)
        before = self.snapshot(obs, "before")
        before_fire = self.control_values(before, "data-cond-fire-res")[-1].get("value")
        for name in cases:
            snapshot = self.snapshot(obs, "after-" + name)
            text, state = self.status_of(snapshot)
            self.assertEqual(state, "error", "%s 页面未拒绝：%s" % (name, text))
            after_fire = self.control_values(snapshot, "data-cond-fire-res")[-1].get("value")
            self.assertEqual(after_fire, before_fire, "%s 页面拒绝前修改了控件" % name)
        valid_after = self.snapshot(obs, "after-valid")
        valid_text, valid_state = self.status_of(valid_after)
        self.assertEqual(valid_state, "changed", valid_text)
        self.assertEqual(self.control_values(valid_after, "data-cond-fire-res")[-1].get("value"), "0")
        self.record(atomic_rejected=sorted(cases), valid_applied=True)

    def _fr_valid_config(self) -> dict:
        cached = getattr(self.__class__, "_fr_config_cache", None)
        if cached is not None:
            return cached
        obs = self.run_harness("fr-save-config", self.fr, [
            {"page": "main", "op": "click", "selector": "#page-buffs [data-cond-save]"},
            {"page": "main", "op": "snapshot", "name": "saved"},
        ])
        snapshot = self.snapshot(obs, "saved")
        downloads = snapshot.get("downloads") or []
        self.assertTrue(downloads, "保存配置未产生下载")
        config = decode_download(downloads[0])
        self.__class__._fr_config_cache = config
        return config

    def test_r3_fr_config_validation(self) -> None:
        valid = self._fr_valid_config()
        cases = {}

        def mutate(name, patch) -> None:
            config = json.loads(json.dumps(valid))
            patch(config)
            cases[name] = config

        mutate("foreign_case", lambda c: c["binding"].__setitem__("case_id", "foreign-build"))
        mutate("zero_ruleset", lambda c: c["binding"]["input_fingerprints"].__setitem__("ruleset", "0" * 64))
        mutate("unknown_state", lambda c: c["scenario"]["states"].__setitem__("totally_unknown_buff", True))
        mutate("unknown_top", lambda c: c.__setitem__("totally_unknown_top", 1))
        mutate("old_no_binding", lambda c: c.pop("binding"))
        fire = "[data-cond-fire-res]"
        load = "#page-buffs [data-cond-load]"
        steps = [{"page": "main", "op": "set_value", "selector": fire, "value": "55"},
                 {"page": "main", "op": "snapshot", "name": "before"}]
        for name, config in cases.items():
            path = self.work_dir / "tmp" / ("config-r3-%s.json" % name)
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            steps += [
                {"page": "main", "op": "load_config", "selector": load, "contentPath": str(path), "filename": name + ".json"},
                {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
                {"page": "main", "op": "snapshot", "name": "after-" + name},
            ]
        valid_path = self.work_dir / "tmp" / "config-r3-valid.json"
        valid_path.write_text(json.dumps(valid, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        steps += [
            {"page": "main", "op": "load_config", "selector": load, "contentPath": str(valid_path), "filename": "valid.json"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
            {"page": "main", "op": "snapshot", "name": "after-valid"},
        ]
        obs = self.run_harness("r3-fr-config", self.fr, steps)
        before = self.snapshot(obs, "before")
        before_fire = self.control_values(before, "data-cond-fire-res")[-1].get("value")
        rejected = {}
        for name in cases:
            snapshot = self.snapshot(obs, "after-" + name)
            text, state = self.status_of(snapshot)
            self.assertEqual(state, "error", "%s 应被拒绝：%r" % (name, text))
            self.assertIn("载入失败", text)
            self.assertIn("未修改当前配置", text)
            after_fire = self.control_values(snapshot, "data-cond-fire-res")[-1].get("value")
            self.assertEqual(after_fire, before_fire, "%s 不得修改控件" % name)
            self.assertEqual(snapshot["writeCount"], 0)
            rejected[name] = text[:180]
        valid_after = self.snapshot(obs, "after-valid")
        valid_text, valid_state = self.status_of(valid_after)
        self.assertEqual(valid_state, "changed")
        self.assertIn("配置已载入", valid_text)
        valid_fire = self.control_values(valid_after, "data-cond-fire-res")[-1].get("value")
        self.assertEqual(str(valid_fire), "0")
        self.record(rejected=rejected, valid_accepted=True, fire_before=before_fire)


# ---------------------------------------------------------------------------
# BH：跨模型完整链路 + 配置校验 + file/响应绑定
# ---------------------------------------------------------------------------
class BHBehaviorAcceptanceTests(P3RAcceptanceBase):
    def _bh_scenario_steps(self, name: str, *, fire: str, compute_after_apply: bool = True) -> list:
        steps = [
            {"page": "main", "op": "set_value", "selector": "[data-scenario-fire-res]", "value": fire},
            {"page": "main", "op": "click", "selector": "[data-apply-scenario]"},
            {"page": "main", "op": "wait", "ms": 120},
        ]
        if compute_after_apply:
            steps += [
                {"page": "main", "op": "click", "selector": "[data-cond-live-recompute]"},
                {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
                {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / ("written-%s.html" % name)).resolve())},
                {"page": "main", "op": "snapshot", "name": "applied"},
            ]
        return steps

    def test_bh_cross_model_chain_and_cli(self) -> None:
        baseline_result = self.bh["run_result"]
        baseline_single = baseline_result["single_cast"]["totals"]["single_cast_total"]
        initial_html = self.bh["html"].read_text(encoding="utf-8")
        initial_page_result = embedded_json(initial_html, "result-data")
        self.assert_close(
            initial_page_result["single_cast"]["totals"]["single_cast_total"],
            baseline_single,
            what="BH 初始页面 vs CLI run",
        )
        obs = self.run_harness("bh-chain-fire20", self.bh, self._bh_scenario_steps("bh-fire20", fire="20"))
        requests = self.requests(obs)
        self.assertTrue(requests)
        scenario_1 = requests[-1]["body"]["scenario"]
        self.assertEqual(scenario_1["target"]["fire_resistance"], 0.2)
        first = self.written_result(self.work_dir / "tmp" / "written-bh-fire20.html")
        first_single = first["single_cast"]["totals"]
        cli_1 = self.cli_bh_run(scenario_1, "bh-fire20")
        cli_single = cli_1["conditional"]["recompute"]["scenario"]["single"]
        for key in ("single_cast_total", "body_total", "ignite_total", "ignite_tail_damage"):
            self.assert_close(first_single[key], cli_single[key], what="BH 页面 vs CLI %s（火抗 20%%）" % key)
        self.assertEqual(first["scenario"]["target"]["resistances"]["fire"], 0.2)

        # 再次操作：刷新本次报告后把火抗改回 0 再计算
        second_steps = [
            {"page": "main", "op": "set_value", "selector": "[data-scenario-fire-res]", "value": "0"},
            {"page": "main", "op": "click", "selector": "[data-apply-scenario]"},
            {"page": "main", "op": "wait", "ms": 120},
            {"page": "main", "op": "click", "selector": "[data-cond-live-recompute]"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
            {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-bh-fire0.html").resolve())},
            {"page": "main", "op": "snapshot", "name": "second"},
        ]
        pages = {"main": {"html": str((self.work_dir / "tmp" / "written-bh-fire20.html").resolve()),
                          "protocol": "http",
                          "servedUrl": "http://127.0.0.1:%d/" % self.bh["port"]}}
        obs_2 = self.run_harness("bh-chain-fire0", self.bh, second_steps, pages=pages)
        scenario_2 = self.last_request(obs_2)["scenario"]
        self.assertEqual(scenario_2["target"]["fire_resistance"], 0.0)
        second = self.written_result(self.work_dir / "tmp" / "written-bh-fire0.html")
        second_single = second["single_cast"]["totals"]
        cli_2 = self.cli_bh_run(scenario_2, "bh-fire0")
        cli_single_2 = cli_2["conditional"]["recompute"]["scenario"]["single"]
        for key in ("single_cast_total", "body_total", "ignite_total", "ignite_tail_damage"):
            self.assert_close(second_single[key], cli_single_2[key], what="BH 页面 vs CLI %s（火抗 0%%）" % key)
        self.record(baseline_single=baseline_single, fire20_single=first_single["single_cast_total"],
                    fire0_single=second_single["single_cast_total"], cli_match=True)

    def test_bh_config_validation(self) -> None:
        obs = self.run_harness("bh-save-config", self.bh, [
            {"page": "main", "op": "click", "selector": "[data-cond-export]"},
            {"page": "main", "op": "snapshot", "name": "saved"},
        ])
        saved = self.snapshot(obs, "saved")
        downloads = saved.get("downloads") or []
        self.assertTrue(downloads, "BH 保存配置未产生下载")
        valid = decode_download(downloads[0])
        self.assertEqual(valid.get("schema_version"), "le-report-config-v1")
        scenario = valid.get("scenario")
        self.assertIsInstance(scenario, dict, "BH 导出配置缺少 scenario envelope")
        cases = {}

        def mutate(name, patch) -> None:
            config = json.loads(json.dumps(valid))
            patch(config)
            cases[name] = config

        mutate("unknown_top", lambda c: c.__setitem__("totally_unknown_top", 1))
        mutate("bad_config_schema", lambda c: c.__setitem__("schema_version", "le-report-config-v0"))
        mutate("foreign_case", lambda c: c["binding"].__setitem__("case_id", "foreign-build"))
        mutate("missing_binding", lambda c: c.pop("binding", None))
        mutate("unknown_state", lambda c: c["scenario"]["states"].__setitem__("totally_unknown_state", True))
        mutate("unknown_effect", lambda c: c["scenario"]["effects"].__setitem__("totally_unknown_effect", {"enabled": True}))
        mutate("unknown_cast", lambda c: c["scenario"]["single_cast"].__setitem__("totally_unknown_cast", True))
        mutate("bad_scenario_schema", lambda c: c["scenario"].__setitem__("schema", "bh-conditional-scenario-v2"))
        steps = [
            {"page": "main", "op": "set_value", "selector": "[data-scenario-fire-res]", "value": "15"},
            {"page": "main", "op": "click", "selector": "[data-apply-scenario]"},
            {"page": "main", "op": "snapshot", "name": "before"},
        ]
        for name, config in cases.items():
            path = self.work_dir / "tmp" / ("bh-config-%s.json" % name)
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            steps += [
                {"page": "main", "op": "load_config", "selector": "[data-cond-import]", "contentPath": str(path.resolve()), "filename": name + ".json"},
                {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
                {"page": "main", "op": "snapshot", "name": "after-" + name},
            ]
        valid_path = self.work_dir / "tmp" / "bh-config-valid.json"
        valid_path.write_text(json.dumps(valid, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        steps += [
            {"page": "main", "op": "load_config", "selector": "[data-cond-import]", "contentPath": str(valid_path.resolve()), "filename": "valid.json"},
            {"page": "main", "op": "wait_idle", "timeoutMs": 15000},
            {"page": "main", "op": "snapshot", "name": "after-valid"},
        ]
        obs = self.run_harness("bh-config", self.bh, steps)
        before = self.snapshot(obs, "before")
        before_fire = self.control_values(before, "data-scenario-fire-res")[-1].get("value")
        rejected = {}
        for name in cases:
            snapshot = self.snapshot(obs, "after-" + name)
            messages = [
                item.get("text") or "" for item in snapshot.get("statuses") or []
                if any(key in (item.get("attrs") or {}) for key in ("data-cond-import-error", "data-cond-import-status"))
            ]
            message = next((text for text in messages if text.strip()), "")
            self.assertTrue(message, "%s 未被拒绝/无错误提示" % name)
            self.assertTrue(
                any(word in message for word in ("未知", "不一致", "缺少", "必须", "失败", "旧格式", "不匹配", "不能")),
                "%s 错误提示不可操作：%r" % (name, message),
            )
            after_fire = self.control_values(snapshot, "data-scenario-fire-res")[-1].get("value")
            self.assertEqual(after_fire, before_fire, "%s 不得修改目标场景" % name)
            self.assertEqual(snapshot["writeCount"], 0)
            rejected[name] = message[:180]
        valid_after = self.snapshot(obs, "after-valid")
        messages = [
            item.get("text") or "" for item in valid_after.get("statuses") or []
            if "data-cond-import-status" in (item.get("attrs") or {})
        ]
        self.assertTrue(any("已载入" in text for text in messages), messages)
        valid_fire = self.control_values(valid_after, "data-scenario-fire-res")[-1].get("value")
        self.assertEqual(str(valid_fire), "0", "合法导出配置应能往返载入")
        self.record(rejected=rejected, valid_accepted=True, roundtrip=True)

    def test_bh_file_mode_and_bad_response(self) -> None:
        compute = "[data-cond-live-recompute]"
        obs = self.run_harness(
            "bh-file",
            self.bh,
            [
                {"page": "main", "op": "snapshot", "name": "load"},
                {"page": "main", "op": "set_value", "selector": "[data-scenario-fire-res]", "value": "20"},
                {"page": "main", "op": "click", "selector": "[data-apply-scenario]"},
                {"page": "main", "op": "wait", "ms": 120},
                {"page": "main", "op": "set_fetch", "mode": "mutate", "mutate": {"set": {"case_id": "foreign-case"}, "delete": []}},
                {"page": "main", "op": "click", "selector": compute},
                {"page": "main", "op": "wait_idle", "timeoutMs": 30000},
                {"page": "main", "op": "snapshot", "name": "rejected"},
                {"page": "main", "op": "set_fetch", "mode": "real"},
                {"page": "main", "op": "click", "selector": compute},
                {"page": "main", "op": "wait_idle", "timeoutMs": 60000},
                {"page": "main", "op": "save_written", "path": str((self.work_dir / "tmp" / "written-bh-file.html").resolve())},
                {"page": "main", "op": "snapshot", "name": "applied"},
            ],
            protocol="file",
        )
        page = self.page_obs(obs)
        load = self.snapshot(obs, "load")
        self.assertEqual(load["writeCount"], 0)
        self.assertEqual(len(self.requests(obs)), 2)
        rejected = self.snapshot(obs, "rejected")
        self.assertEqual(rejected["writeCount"], 0, "错误响应不得整页替换")
        text, _state = self.status_of(rejected, attrs=("data-cond-live-status", "data-cond-status"))
        self.assertIn("已丢弃响应", text)
        applied = self.snapshot(obs, "applied")
        self.assertEqual(applied["writeCount"], 1)
        applied_text, _ = self.status_of(applied, attrs=("data-cond-live-status", "data-cond-status"))
        self.assertIn("计算完成", applied_text)
        self.assertNotEqual(self.bh["port"], 8765)
        self.record(file_load_requests=0, bad_response_rejected=True, valid_applied=True)


if __name__ == "__main__":
    unittest.main()
