"""Implementation of the unified ``scripts/le.ps1`` command surface.

``le.ps1`` is a very small PowerShell launcher: it derives the project root from
its own location, selects the Python interpreter and delegates here.  All real
logic lives in this file so it can be tested and maintained as ordinary Python.

Commands (see ``le.ps1 --help``)::

    doctor | reproduce | run | serve | import | validate | test | package | knowledge

Global conventions:

* every path is resolved from the project root (``scripts/le_commands.py`` →
  parents[1]), never from the current working directory;
* child Python processes get an explicit ``PYTHONPATH`` built from the module
  layout (the user's own ``PYTHONPATH`` is deliberately not inherited, so an
  unpacked copy behaves the same as the development tree);
* generated run artefacts are portable: ``reproduce`` passes repository-relative
  paths to child tools and normalises any machine absolute path that frozen
  source data would otherwise leak into the run directory;
* exit codes: 0 = ok, 1 = failure/validation mismatch, 2 = argument error,
  3 = partial (some optional inputs missing).
"""

from __future__ import annotations
import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import (Any, Iterable, Sequence)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline'))
from _common import (TEXT_SCAN_SUFFIXES, PipelineError, calculation_input_counts, case_counts, case_path, load_case, load_json, optional_case_path, portable_document, portable_text, relative, resolve, sha256_file)
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_PARTIAL = 3
MODULE_PATHS = {'le_calc': ROOT / 'tools' / 'phase3' / 'src', 'le_build': ROOT / 'tools' / 'build_import' / 'src', 'le_save_import': ROOT / 'tools' / 'save_import' / 'src', 'le_data': ROOT / 'tools' / 'ingest' / 'src', 'affix_db': ROOT / 'tools'}
SUITES: 'dict[str, dict[str, Any]]' = {'ingest': {'label': 'ingest（数据提取/规范化）', 'pythonpath': [MODULE_PATHS['le_data']], 'cwd': ROOT / 'tools' / 'ingest', 'args': ['-m', 'unittest', 'discover', '-s', 'tests', '-t', 'tests']}, 'build_import': {'label': 'build_import（planner/BuildSpec 导入）', 'pythonpath': [MODULE_PATHS['le_build']], 'cwd': ROOT / 'tools' / 'build_import', 'args': ['-m', 'unittest', 'discover', '-s', 'tests', '-t', 'tests']}, 'save_import': {'label': 'save_import（存档解析）', 'pythonpath': [MODULE_PATHS['le_save_import']], 'cwd': ROOT, 'args': ['-m', 'unittest', 'discover', '-s', 'tools/save_import/tests']}, 'affix_db': {'label': 'affix_db（词缀库）', 'pythonpath': [MODULE_PATHS['affix_db']], 'cwd': ROOT, 'args': ['-m', 'unittest', 'discover', '-s', 'tools/affix_db/tests']}, 'phase3': {'label': 'phase3（计算/条件/报告）', 'pythonpath': [MODULE_PATHS['le_calc']], 'cwd': ROOT, 'args': ['-m', 'unittest', 'discover', '-s', 'tools/phase3/tests']}, 'knowledge': {'label': 'knowledge（机制/技能知识索引与查询）', 'pythonpath': [ROOT / 'tools' / 'knowledge' / 'src'], 'cwd': ROOT, 'args': ['-m', 'unittest', 'discover', '-s', 'tools/knowledge/tests', '-t', 'tools/knowledge/tests']}}
_MACHINE_PATH_RE = re.compile('(?<![A-Za-z0-9])[A-Za-z]:[\\\\/][^\\s"\'<>|,;)]*')

def info(message: 'str') -> 'None':
    print(f'[信息] {message}', flush=True)

def warn(message: 'str') -> 'None':
    print(f'[警告] {message}', flush=True)

def fail(message: 'str') -> 'None':
    print(f'[失败] {message}', file=sys.stderr, flush=True)

def heading(message: 'str') -> 'None':
    print(f'\n== {message} ==', flush=True)

def print_checks(checks: 'Sequence[dict[str, Any]]') -> 'None':
    width = max((len(str(check['name'])) for check in checks), default=8)
    for check in checks:
        status = str(check['status'])
        detail = f'  {check['detail']}' if check.get('detail') else ''
        print(f'  {status:<4}  {str(check['name']):<{width}}{detail}')

def _status_ok(checks: 'Sequence[dict[str, Any]]') -> 'bool':
    return all((str(check.get('status')) in ('PASS', 'INFO', 'SKIP') for check in checks))

def load_config() -> 'dict[str, Any]':
    path = ROOT / 'config' / 'project.json'
    if not path.is_file():
        return {}
    return load_json(path, what='project configuration')

def _pythonpath(extra: 'Iterable[Path]') -> 'str':
    parts = []
    for item in extra:
        value = str(Path(item).resolve())
        if value not in parts:
            parts.append(value)
    return os.pathsep.join(parts)

def run_child(cmd: 'Sequence[str]', *, pythonpath: 'Iterable[Path]'=(), cwd: 'Path | None'=None, capture: 'bool'=False, env_extra: 'dict[str, str] | None'=None, echo: 'bool'=True) -> 'subprocess.CompletedProcess':
    '''Run one child process with an explicit, non-inherited PYTHONPATH.'''

    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    if pythonpath:
        env['PYTHONPATH'] = _pythonpath(pythonpath)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    env.setdefault('PYTHONUNBUFFERED', '1')
    if env_extra:
        env.update(env_extra)
    if echo:
        print(f'+ {subprocess.list2cmdline([str(part) for part in cmd])}', flush=True)
    if capture:
        return subprocess.run([str(part) for part in cmd], cwd=str(cwd or ROOT), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return subprocess.run([str(part) for part in cmd], cwd=str(cwd or ROOT), env=env, text=True, encoding='utf-8', errors='replace')

def _python() -> 'str':
    return sys.executable

def _default_case(args: 'argparse.Namespace') -> 'Path':
    """显式 --case 才允许运行；不读取 config.default_case/全局 latest。"""
    requested = getattr(args, 'case', None)
    if not requested:
        raise PipelineError(
            '必须显式指定 --case；不读取 config/project.json.default_case 或任何全局 latest 案例'
        )
    return resolve(ROOT, requested)

def _load_case(args: 'argparse.Namespace'):
    return load_case(_default_case(args), root=ROOT)

def _run_root(case: 'dict[str, Any]', args: 'argparse.Namespace') -> 'Path':
    override = os.environ.get('LE_OUT_ROOT') or getattr(args, 'out_root', None)
    configured = (case.get('output') or {}).get('root') or 'reports'
    if override:
        return resolve(ROOT, override)
    return resolve(ROOT, configured)

def _fresh_run_id(case: 'dict[str, Any]') -> 'str':
    template = str((case.get('output') or {}).get('run_id_template') or '{case_id}-{utcstamp}')
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return template.replace('{case_id}', str(case['case_id'])).replace('{utcstamp}', stamp)

def _case_model(case: 'dict[str, Any]') -> 'str':
    '''Damage model selector: ``black-hole`` (default) or ``hit`` (Flame Reave).'''

    return str(case.get('model') or 'black-hole').strip().lower()

_KNOWN_CASE_MODELS = ('black-hole', 'hit')

def _require_known_model(case: 'dict[str, Any]', *, command: 'str') -> 'str':
    '''Explicitly reject unknown ``model`` values instead of默认按 BH 处理（D9）.'''

    model = _case_model(case)
    if model not in _KNOWN_CASE_MODELS:
        raise PipelineError(f'未知 damage model：{case.get('model')!r}（{command} 支持 ' + ' / '.join(_KNOWN_CASE_MODELS) + f'；案例：{case.get('case_id') or '?'}）')
    return model

_REPORT_CAPABILITIES_FILE = 'report-capabilities.json'

def _le_calc_modules():
    '''Lazily import the P3R-B contract helpers (kept out of the child CLIs).'''

    source = str(MODULE_PATHS['le_calc'])
    if source not in sys.path:
        sys.path.insert(0, source)
    from le_calc import recalc as recalc_module
    from le_calc import report_capabilities as capabilities_module
    return recalc_module, capabilities_module

def _recompute_endpoint(args: 'argparse.Namespace') -> 'str':
    recalc_module, _ = _le_calc_modules()
    explicit = getattr(args, 'recalc_endpoint', None)
    if explicit:
        return recalc_module.parse_endpoint(str(explicit))['url']
    port = int(getattr(args, 'port', None) or recalc_module.DEFAULT_PORT)
    return recalc_module.loopback_endpoint(port)

def _sync_run_manifest_files(run_dir: 'Path', names: 'Iterable[str]') -> 'None':
    """Recompute generated-file hashes after post-processing a report.

    P3R-B re-renders the report HTML with the actual recompute endpoint and adds
    ``report-capabilities.json``; the child-written ``run-manifest.json`` must be
    kept byte-accurate or ``validate_result``/structure checks fail.  Only the
    current run's manifest is touched (historical runs are never modified).
    """

    manifest_path = run_dir / 'run-manifest.json'
    if not manifest_path.is_file():
        return
    try:
        manifest = load_json(manifest_path, what='run manifest')
    except PipelineError:
        return
    files = manifest.get('files')
    if not isinstance(files, dict):
        return
    changed = False
    for name in names:
        path = run_dir / name
        if path.is_file():
            digest = sha256_file(path)
            if files.get(name) != digest:
                files[name] = digest
                changed = True
    if changed:
        from _common import write_json
        write_json(manifest_path, manifest)
        info(f'已同步 run-manifest 文件哈希：{', '.join(sorted(set(names)))}')

_STAGING_DIRNAME = ".staging"
_STAGED_TEXT_SUFFIXES = frozenset({".json", ".html", ".md", ".txt", ".csv"})


def _workflow_commit():
    """Lazy import of the P4 commit library (kh-bh task-40)."""
    calc_src = str(MODULE_PATHS["le_calc"])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import commit  # noqa: PLC0415

    return commit


def _staged_case_input_hashes(case: dict[str, Any]) -> dict[str, str]:
    """Actual sha256 of declared run inputs (not expected values)."""
    rows: dict[str, str] = {}
    try:
        rows["case"] = sha256_file(Path(case["_path"]))
    except (OSError, KeyError):
        rows["case"] = ""
    declared = {
        "calculation_input": "inputs.calculation_input",
        "snapshot": "inputs.snapshot",
        "bd": "inputs.bd",
        "scenario": "inputs.scenario",
        "ruleset": "ruleset.path",
        "conditional_registry": "conditional_registry.path",
        "build_spec": "build.build_spec",
    }
    for role, dotted in declared.items():
        try:
            path = case_path(case, dotted)
        except PipelineError:
            rows[role] = ""
            continue
        rows[role] = sha256_file(path) if path.is_file() else ""
    return rows


def _staged_run_input_fingerprint(case: dict[str, Any], run_id: str, args: argparse.Namespace) -> str:
    """Canonical operation input: case inputs + run parameters (no paths/time)."""
    try:
        route = _route_for_case(case)
    except Exception:
        route = {}
    if route.get("engine") == "unified_dps":
        return _unified_input_fingerprint(case, route)
    payload = {
        "schema": "le-run-publish-input-v1",
        "case_id": str(case.get("case_id")),
        "model_id": _case_model(case),
        "run_id": run_id,
        "parameters": {
            "monster_level": int(case.get("monster_level") or 100),
            "recalc_endpoint": _recompute_endpoint(args),
            "no_report_cache": bool(
                getattr(args, "no_report_cache", False) or getattr(args, "no_cache", False)
            ),
        },
        "inputs": {key: value for key, value in sorted(_staged_case_input_hashes(case).items())},
        "declared_fingerprints": {
            key: value for key, value in sorted(_case_input_fingerprints(case).items())
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_STAGING_LEASE_DIRNAME = ".staging-locks"
_STAGING_LEASE_TTL_SECONDS = 120.0
_STAGING_HEARTBEAT_SECONDS = 2.0


def _staging_lease_path(staging_root: Path, final_dir: Path) -> Path:
    try:
        target = relative(final_dir, ROOT)
    except PipelineError:
        target = str(final_dir.resolve())
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
    return staging_root.parent / _STAGING_LEASE_DIRNAME / f"{digest}.lease"


def _read_staging_lease(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_staging_lease(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _process_alive(pid: int) -> bool:
    """Best-effort live-process check (never signals the target)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 - fall back to tasklist on exotic hosts
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return str(int(pid)) in output
        except Exception:  # noqa: BLE001
            return False


def _staging_lease_active(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    try:
        heartbeat = float(payload.get("heartbeat_at") or payload.get("started_at") or 0)
    except (TypeError, ValueError):
        heartbeat = 0.0
    age = max(0.0, time.time() - heartbeat)
    if age > _STAGING_LEASE_TTL_SECONDS:
        return False
    import socket

    if str(payload.get("host") or "") != socket.gethostname():
        # Cannot inspect a remote PID; only heartbeat expiry can prove staleness.
        return True
    return _process_alive(int(payload.get("pid") or 0))


def _staging_owner_marker(staging_root: Path, attempt_id: str) -> Path:
    return staging_root / f"{attempt_id}.owner.json"


def _remove_staging_candidate(staging_root: Path, candidate: Path, marker: Path | None = None) -> bool:
    try:
        candidate_root = staging_root.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(candidate_root)
    except (OSError, ValueError):
        return False
    if resolved.name != candidate.name:
        return False
    removed = False
    if candidate.is_dir():
        try:
            import shutil as _shutil

            _shutil.rmtree(candidate)
            removed = True
        except OSError:
            return False
    if marker is not None:
        try:
            marker.unlink()
        except OSError:
            pass
    return removed


def _cleanup_inactive_owned_candidates(staging_root: Path, final_dir: Path) -> int:
    """Remove leftovers that provably belong to *this target* and no live lease.

    Called only while holding the target lifecycle lease, so no active process
    for the same target can own these candidates.  Different targets sharing a
    run-id are never touched (owner marker records the target).
    """
    if not staging_root.is_dir():
        return 0
    try:
        target = relative(final_dir, ROOT)
    except PipelineError:
        target = str(final_dir.resolve())
    removed = 0
    for marker in sorted(staging_root.glob("*.owner.json")):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("target") or "") != target:
            continue
        attempt_id = str(payload.get("attempt_id") or "")
        if not attempt_id:
            continue
        candidate = staging_root / attempt_id
        if _remove_staging_candidate(staging_root, candidate, marker):
            removed += 1
    if removed:
        warn(f"已清理 {removed} 个已失活目标的 staging 遗留候选：{relative(staging_root, ROOT)}")
    return removed


def _reclaim_stale_lease(staging_root: Path, lease_path: Path,
                         stale: dict[str, Any]) -> bool:
    """Reclaim a lease only when its recorded owner is provably inactive."""
    current = _read_staging_lease(lease_path)
    if current is not None and str(current.get("attempt_id")) != str(stale.get("attempt_id")):
        return False  # another process replaced it between checks
    attempt_id = str(stale.get("attempt_id") or "")
    candidate = staging_root / attempt_id
    marker = _staging_owner_marker(staging_root, attempt_id) if attempt_id else None
    if attempt_id:
        _remove_staging_candidate(staging_root, candidate, marker)
        warn(f"已清理 1 个中断遗留的 staging 候选目录：{relative(staging_root, ROOT)}")
    try:
        lease_path.unlink()
    except OSError:
        pass
    return True


class _StagingLease:
    """Lifecycle lease for one final staging target (candidate -> commit done)."""

    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.payload = payload
        self._stop = None
        self._thread = None
        import threading

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        import threading

        assert isinstance(self._stop, threading.Event)
        while not self._stop.wait(_STAGING_HEARTBEAT_SECONDS):
            current = _read_staging_lease(self.path)
            if current is None or str(current.get("attempt_id")) != str(self.payload.get("attempt_id")):
                return
            self.payload["heartbeat_at"] = time.time()
            try:
                _write_staging_lease(self.path, self.payload)
            except OSError:
                return

    def release(self) -> None:
        try:
            if self._stop is not None:
                self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            current = _read_staging_lease(self.path)
            if current is not None and str(current.get("attempt_id")) == str(self.payload.get("attempt_id")):
                self.path.unlink()
        except OSError:
            pass
        try:
            parent = self.path.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


def _acquire_staging_lease(
    staging_root: Path,
    final_dir: Path,
    *,
    run_id: str,
    operation_id: str,
    attempt_id: str,
    candidate: Path,
    timeout: float = 180.0,
    wait: bool = True,
):
    """Acquire the per-target lifecycle lease.

    ``wait=False`` is a non-blocking probe used by ``_staged_plan``: an active
    owner returns ``{"active": True, "current": ...}`` instead of deleting,
    raising, or blocking. ``wait=True`` retains the F4 CLI behavior.
    """
    lease_path = _staging_lease_path(staging_root, final_dir)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    try:
        target = relative(final_dir, ROOT)
    except PipelineError:
        target = str(final_dir.resolve())
    while True:
        payload = {
            "schema_version": "le-staging-lease-v1",
            "run_id": run_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "candidate": relative(candidate, ROOT),
            "target": target,
            "pid": os.getpid(),
            "host": __import__("socket").gethostname(),
            "started_at": time.time(),
            "heartbeat_at": time.time(),
        }
        try:
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lease_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current = _read_staging_lease(lease_path)
            if current is None:
                # Unreadable/partial lease: only remove when it is clearly old.
                try:
                    age = time.time() - lease_path.stat().st_mtime
                except OSError:
                    age = _STAGING_LEASE_TTL_SECONDS + 1
                if age > _STAGING_LEASE_TTL_SECONDS:
                    try:
                        lease_path.unlink()
                    except OSError:
                        pass
                else:
                    time.sleep(0.2)
                continue
            if not _staging_lease_active(current):
                if _reclaim_stale_lease(staging_root, lease_path, current):
                    continue
                time.sleep(0.1)
                continue
            if not wait:
                return {"active": True, "current": dict(current)}
            if time.time() >= deadline:
                raise PipelineError(
                    "另一个 staged run 正在执行同一目标（活跃租约 "
                    f"{current.get('attempt_id')}，pid={current.get('pid')}）；"
                    "请等待其完成或确认其失活后重试"
                )
            time.sleep(0.2)
            continue
        except OSError as exc:
            raise PipelineError(f"无法创建 staging 生命周期锁：{lease_path}：{exc}") from exc
        try:
            os.write(fd, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return _StagingLease(lease_path, payload)


def _release_staging_plan(plan: dict[str, Any] | None) -> None:
    if not plan:
        return
    lease = plan.get("lease")
    if lease is not None:
        try:
            lease.release()
        except Exception:  # noqa: BLE001 - release must never mask command result
            pass


def _cleanup_stale_staging(staging_root: Path, run_id: str) -> int:
    """Compatibility shim: prefix-only cleanup is intentionally disabled.

    Active-candidate safety (P4-F4) is provided by ``_StagingLease``; leftover
    candidates are reclaimed by exact owner marker/target while holding the lease.
    """
    return 0


def _staged_logical_run_id(final_dir: Path, case: dict[str, Any]) -> str:
    """Stable logical run id for ``--publish staged`` retries.

    When ``--out`` is given without ``--run-id``, the final directory basename is
    the logical identity: the same command must map to the same operation_id /
    input_fingerprint so a retry replays instead of creating a new auto-timestamp
    identity. The name is sanitized to the existing run-id safe character set.
    """
    import re as _re

    name = final_dir.name
    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name)).strip(".-_")
    if not safe or safe in (".", ".."):
        safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", str(case.get("case_id") or "run")).strip(".-_")
    return (safe[:120] or "run")


def _staged_operation_id(case_id: str, run_id: str) -> str:
    """Windows-safe operation id (``:`` is illegal in NTFS file names)."""
    import re as _re

    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", f"run-{case_id}-{run_id}").strip("-")
    return safe or "run"


def _staged_plan(case: dict[str, Any], args: argparse.Namespace, *, run_id: str,
                 final_dir: Path) -> dict[str, Any] | None:
    """Return the staged-publish plan, or ``None`` for the default direct path."""
    if str(getattr(args, "publish", "none") or "none") != "staged":
        return None
    staging_root = final_dir.parent / _STAGING_DIRNAME
    attempt_id = f"{run_id}-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    candidate = staging_root / attempt_id
    operation_id = _staged_operation_id(str(case.get("case_id")), run_id)
    operations_root = getattr(args, "operations_root", None) or os.environ.get(
        "LE_OPERATIONS_ROOT", "data/cache/operations"
    )
    input_fingerprint = _staged_run_input_fingerprint(case, run_id, args)
    probe = _acquire_staging_lease(
        staging_root,
        final_dir,
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        candidate=candidate,
        wait=False,
    )
    wait_required = isinstance(probe, dict) and bool(probe.get("active"))
    lease = None if wait_required else probe
    plan = {
        "mode": "staged",
        "case_id": str(case.get("case_id")),
        "run_id": run_id,
        "candidate": candidate,
        "final_dir": final_dir,
        "staging_root": staging_root,
        "attempt_id": attempt_id,
        "operation_id": operation_id,
        "input_fingerprint": input_fingerprint,
        "operations_root": str(operations_root),
        "lease": lease,
        "wait_required": wait_required,
        "lease_owner": (probe.get("current") if wait_required else (lease.payload if lease else None)),
    }
    if wait_required:
        # Do not create/delete any candidate here: the active owner must be
        # allowed to finish.  cmd_run waits for the lease and prepares files.
        return plan
    # NOTE: candidate directory creation/owner marker belong to cmd_run after
    # the lifecycle lease is held (keeps _staged_plan a pure planning probe).
    return plan


def _wait_staged_lease(plan: dict[str, Any], *, timeout: float = 180.0) -> dict[str, Any]:
    """Wait for an active target lease, then acquire it and prepare the candidate."""
    staging_root = plan["staging_root"]
    final_dir = plan["final_dir"]
    lease_path = _staging_lease_path(staging_root, final_dir)
    deadline = time.time() + timeout
    owner = plan.get("lease_owner") or {}
    info(
        "另一个 staged run 正在执行同一目标（活跃租约 "
        f"{owner.get('attempt_id')}，pid={owner.get('pid')}）；等待其完成…"
    )
    while True:
        current = _read_staging_lease(lease_path)
        if current is None or not _staging_lease_active(current):
            break
        if time.time() >= deadline:
            raise PipelineError(
                "等待另一个 staged run 释放目标租约超时（"
                f"{timeout:.0f}s，活跃租约 {owner.get('attempt_id')}，"
                f"pid={owner.get('pid')}）；请确认其状态后重试"
            )
        time.sleep(0.2)
    lease = _acquire_staging_lease(
        staging_root,
        final_dir,
        run_id=str(plan["run_id"]),
        operation_id=str(plan["operation_id"]),
        attempt_id=str(plan["attempt_id"]),
        candidate=plan["candidate"],
        timeout=timeout,
        wait=True,
    )
    plan["lease"] = lease
    plan["wait_required"] = False
    plan["lease_owner"] = lease.payload
    _prepare_staged_plan_files(plan)
    return plan


def _prepare_staged_plan_files(plan: dict[str, Any]) -> None:
    """Create the candidate + owner marker after the target lease is held."""
    staging_root = plan["staging_root"]
    final_dir = plan["final_dir"]
    candidate = plan["candidate"]
    attempt_id = str(plan["attempt_id"])
    lease = plan.get("lease")
    _cleanup_inactive_owned_candidates(staging_root, final_dir)
    candidate.mkdir(parents=True, exist_ok=True)
    try:
        target = relative(final_dir, ROOT)
    except PipelineError:
        target = str(final_dir.resolve())
    marker = _staging_owner_marker(staging_root, attempt_id)
    try:
        marker.write_text(
            json.dumps(
                {
                    "schema_version": "le-staging-owner-v1",
                    "run_id": plan.get("run_id"),
                    "operation_id": plan.get("operation_id"),
                    "attempt_id": attempt_id,
                    "candidate": relative(candidate, ROOT),
                    "target": target,
                    "pid": os.getpid(),
                    "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    if lease is not None:
        lease.payload.update(
            {"candidate": relative(candidate, ROOT), "target": target, "heartbeat_at": time.time()}
        )
        try:
            _write_staging_lease(lease.path, lease.payload)
        except OSError:
            pass


def _path_forms(raw: str) -> dict[str, str]:
    """Textual forms a generated path can take (posix, backslash, JSON escapes)."""
    posix = raw.replace("\\", "/")
    back = raw.replace("/", "\\")
    return {
        "plain": raw,
        "posix": posix,
        "back": back,
        "json_back": back.replace("\\", "\\\\"),
        "json_posix": posix.replace("/", "\\\\"),
    }


def _rewrite_staged_paths(candidate: Path, final_dir: Path) -> int:
    """Rewrite candidate/staging paths to the final run directory in text files."""
    old_bases: list[tuple[str, str]] = []
    try:
        old_bases.append((str(candidate), str(final_dir)))
        old_bases.append((str(candidate.resolve()), str(final_dir.resolve())))
        old_bases.append((relative(candidate, ROOT), relative(final_dir, ROOT)))
    except Exception:  # noqa: BLE001 - outside-root paths are not expected
        pass
    pairs: list[tuple[str, str]] = []
    for old_base, new_base in old_bases:
        old_forms = _path_forms(old_base)
        new_forms = _path_forms(new_base)
        for kind, old_value in old_forms.items():
            new_value = new_forms.get(kind) or new_base
            if old_value:
                pairs.append((old_value, new_value))
    pairs = sorted(dict(pairs).items(), key=lambda item: len(item[0]), reverse=True)
    changed = 0
    for path in sorted(candidate.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _STAGED_TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = text
        for old_value, new_value in pairs:
            if old_value and old_value in updated:
                updated = updated.replace(old_value, new_value)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed += 1
    return changed

def _sync_all_run_manifest_files(run_dir: Path) -> None:
    manifest_path = run_dir / "run-manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = load_json(manifest_path, what="run manifest")
    except PipelineError:
        return
    names = sorted((manifest.get("files") or {}).keys())
    if names:
        _sync_run_manifest_files(run_dir, names)


def _verify_staged_tree(run_dir: Path, *, staging_marker: str | None) -> dict[str, Any]:
    """Validate manifest hashes + final-path semantics (no staging leakage)."""
    report: dict[str, Any] = {"ok": False, "problems": [], "file_count": 0}
    manifest_path = run_dir / "run-manifest.json"
    if not manifest_path.is_file():
        report["problems"].append("run-manifest.json 缺失")
        return report
    try:
        manifest = load_json(manifest_path, what="run manifest")
    except PipelineError as exc:
        report["problems"].append(f"run-manifest 不可读：{exc}")
        return report
    files = manifest.get("files") or {}
    for name, expected in sorted(files.items()):
        path = run_dir / name
        if not path.is_file():
            report["problems"].append(f"发布产物缺失：{name}")
            continue
        if sha256_file(path) != str(expected):
            report["problems"].append(f"发布产物 sha 不一致：{name}")
    report["file_count"] = len(files)
    if staging_marker:
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _STAGED_TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if staging_marker in text or _STAGING_DIRNAME in text:
                report["problems"].append(
                    f"产物泄漏 staging 路径：{path.relative_to(run_dir).as_posix()}"
                )
    report["ok"] = not report["problems"]
    return report


def _atomic_write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _pipeline_state_document(case_id: str, run_id: str) -> dict[str, Any]:
    """Minimal valid ``le-pipeline-state-v1`` for a staged run."""
    calc_src = str(MODULE_PATHS["le_calc"])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import contract as workflow_contract  # noqa: PLC0415

    return workflow_contract.PipelineState(case_id=case_id, run_id=run_id).to_dict()


def _prepare_candidate_pipeline_state(candidate: Path, plan: dict[str, Any]) -> None:
    """Add the commit record to the candidate pipeline-state without losing stages.

    ``--publish staged`` owns the final tree, so last_commit is written before
    publish (a post-publish rewrite would break commit replay tree verification).
    Existing stage entries written by FileStateStore are preserved.
    """
    calc_src = str(MODULE_PATHS["le_calc"])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import contract as workflow_contract  # noqa: PLC0415

    state_path = candidate / "pipeline-state.json"
    if state_path.is_file():
        try:
            state = workflow_contract.PipelineState.from_dict(load_json(state_path, what="pipeline-state"))
        except (PipelineError, workflow_contract.ContractError):
            state = workflow_contract.PipelineState(
                case_id=str(plan["case_id"]), run_id=str(plan["run_id"])
            )
    else:
        state = workflow_contract.PipelineState(
            case_id=str(plan["case_id"]), run_id=str(plan["run_id"])
        )
    manifest_path = candidate / "run-manifest.json"
    file_count = 0
    if manifest_path.is_file():
        try:
            manifest = load_json(manifest_path, what="run manifest")
            file_count = len(manifest.get("files") or {})
        except PipelineError:
            file_count = 0
    state.last_commit = {
        "operation_id": str(plan["operation_id"]),
        "status": "committed",
        "attempt_id": str(plan["attempt_id"]),
        "input_fingerprint": str(plan["input_fingerprint"]),
        "committed_refs": [
            {"path": relative(plan["final_dir"], ROOT), "file_count": file_count}
        ],
        "recorded_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    _atomic_write_json_file(state_path, state.to_dict())
    if manifest_path.is_file():
        try:
            manifest = load_json(manifest_path, what="run manifest")
            files = manifest.setdefault("files", {})
            files["pipeline-state.json"] = sha256_file(state_path)
            from _common import write_json as _write_json  # noqa: PLC0415

            _write_json(manifest_path, manifest)
        except PipelineError:
            pass

def _record_pipeline_last_commit(final_dir: Path, plan: dict[str, Any],
                                 document: dict[str, Any]) -> dict[str, Any]:
    """Atomically record the commit result in the published run pipeline-state.

    ``publish_run_dir`` owns run-directory atomicity; this is a small post-publish
    metadata update (state + manifest hash) using temp-file + ``os.replace``.
    """
    calc_src = str(MODULE_PATHS["le_calc"])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import contract as workflow_contract  # noqa: PLC0415

    state_path = final_dir / "pipeline-state.json"
    if state_path.is_file():
        try:
            state = workflow_contract.PipelineState.from_dict(
                load_json(state_path, what="pipeline-state")
            )
        except (PipelineError, workflow_contract.ContractError):
            state = workflow_contract.PipelineState(
                case_id=str(plan["case_id"]), run_id=str(plan["run_id"])
            )
    else:
        state = workflow_contract.PipelineState(
            case_id=str(plan["case_id"]), run_id=str(plan["run_id"])
        )
    committed_refs = []
    for ref in document.get("committed_refs") or []:
        if isinstance(ref, dict):
            committed_refs.append(
                {"path": ref.get("path"), "sha256": ref.get("sha256"), "bytes": ref.get("bytes")}
            )
    state.last_commit = {
        "operation_id": str(document.get("operation_id") or plan["operation_id"]),
        "status": str(document.get("status")),
        "attempt_id": str(document.get("attempt_id") or plan["attempt_id"]),
        "input_fingerprint": str(plan["input_fingerprint"]),
        "committed_refs": committed_refs,
        "recorded_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    payload = state.to_dict()
    _atomic_write_json_file(state_path, payload)
    manifest_path = final_dir / "run-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = load_json(manifest_path, what="run manifest")
            files = manifest.setdefault("files", {})
            files["pipeline-state.json"] = sha256_file(state_path)
            from _common import write_json as _write_json  # noqa: PLC0415

            _write_json(manifest_path, manifest)
        except PipelineError:
            pass
    return payload


def _verify_published_last_commit(final_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """The published pipeline-state must record this staged commit."""
    problems: list[str] = []
    state_path = final_dir / "pipeline-state.json"
    if not state_path.is_file():
        return {"ok": False, "problems": ["pipeline-state.json 缺失"]}
    try:
        state = load_json(state_path, what="pipeline-state")
    except PipelineError as exc:
        return {"ok": False, "problems": [str(exc)]}
    last_commit = state.get("last_commit") or {}
    if str(last_commit.get("operation_id") or "") != str(plan["operation_id"]):
        problems.append("operation_id 不一致")
    if str(last_commit.get("input_fingerprint") or "") != str(plan["input_fingerprint"]):
        problems.append("input_fingerprint 不一致")
    if str(last_commit.get("status") or "") not in ("committed", "replayed"):
        problems.append("status 不是 committed/replayed")
    return {"ok": not problems, "problems": problems}


def _verify_operation_record(plan: dict[str, Any], final_dir: Path) -> dict[str, Any]:
    """Compare the operation record's published tree sha with the final directory."""
    problems: list[str] = []
    try:
        commit = _workflow_commit()
        from le_calc.workflow import atomic_io  # noqa: PLC0415

        record = commit.read_operation_record(
            str(plan["operation_id"]),
            (ROOT / str(plan["operations_root"])).resolve()
            if not Path(str(plan["operations_root"])).is_absolute()
            else plan["operations_root"],
        )
        if record is None:
            problems.append("操作记录不存在")
        else:
            refs = record.get("published_refs") or []
            target = relative(final_dir, ROOT)
            matched = [ref for ref in refs if str(ref.get("path")) == target]
            if not matched:
                problems.append("操作记录缺少发布目标")
            else:
                expected = str(matched[0].get("sha256") or "")
                actual = atomic_io.tree_sha256(final_dir)
                if expected and expected != actual:
                    problems.append("发布目录 tree sha 不一致")
    except Exception as exc:  # noqa: BLE001 - 校验失败必须明确
        problems.append(f"{type(exc).__name__}: {exc}")
    return {"ok": not problems, "problems": problems}


def _publish_staged_run(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Rewrite paths, sync hashes, atomically publish; return commit document."""
    candidate = plan["candidate"]
    final_dir = plan["final_dir"]
    try:
        rewritten = _rewrite_staged_paths(candidate, final_dir)
        _prepare_candidate_pipeline_state(candidate, plan)
        _sync_all_run_manifest_files(candidate)
        pre = _verify_staged_tree(candidate, staging_marker=str(plan["candidate"]))
        if not pre["ok"]:
            fail("staged 候选路径/哈希校验失败：" + "；".join(pre["problems"][:5]))
            return None
        commit = _workflow_commit()
        result = commit.publish_run_dir(
            candidate,
            relative(final_dir, ROOT),
            operation_id=plan["operation_id"],
            attempt_id=plan["attempt_id"],
            input_fingerprint=plan["input_fingerprint"],
            operations_root=plan["operations_root"],
            root=ROOT,
        )
        document = result.to_dict()
    except Exception as exc:  # noqa: BLE001 - 发布失败必须明确、非 0
        fail(f"staged 发布失败：{type(exc).__name__}: {exc}")
        return None
    status = str(document.get("status"))
    if status not in ("committed", "replayed"):
        issues = document.get("issues") or []
        detail = "；".join(
            f"{issue.get('code')}: {issue.get('message')}" for issue in issues[:4]
        )
        fail(f"staged 发布未成功（{status}）：{detail or '无 issue'}")
        return None
    post = _verify_staged_tree(final_dir, staging_marker=str(plan["candidate"]))
    if not post["ok"]:
        fail("发布后校验失败：" + "；".join(post["problems"][:5]))
        return None
    state_check = _verify_published_last_commit(final_dir, plan)
    if not state_check["ok"]:
        fail("pipeline-state.last_commit 校验失败：" + "；".join(state_check["problems"][:3]))
        return None
    if status == "committed":
        record_check = _verify_operation_record(plan, final_dir)
        if not record_check["ok"]:
            fail("操作记录校验失败：" + "；".join(record_check["problems"][:3]))
            return None
    try:
        import shutil as _shutil

        _shutil.rmtree(candidate)
    except OSError:
        warn(f"暂未清理 staging 候选：{relative(candidate, ROOT)}")
    try:
        marker = _staging_owner_marker(plan["staging_root"], str(plan["attempt_id"]))
        if marker.is_file():
            marker.unlink()
    except OSError:
        pass
    try:
        staging_root = plan["staging_root"]
        if staging_root.is_dir() and not any(staging_root.iterdir()):
            staging_root.rmdir()
    except OSError:
        pass
    info(
        f"staged 发布：{status}（operation_id={plan['operation_id']}，"
        f"重写路径文件 {rewritten} 个，校验 {post['file_count']} 个 manifest 文件 sha）"
    )
    return document


def _route_for_case(case: 'dict[str, Any]') -> 'dict[str, Any]':
    """与 workflow 共享的计算路由（统一路径与旧路径的唯一判定）。"""
    import sys as _sys
    calc_src = str(MODULE_PATHS['le_calc'])
    if calc_src not in _sys.path:
        _sys.path.insert(0, calc_src)
    from le_calc import calculation_route  # noqa: PLC0415

    return calculation_route.resolve_calculation_route(dict(case), root=ROOT)


def _route_summary(route: 'dict[str, Any]') -> str:
    return (
        f"engine={route.get('engine')} status={route.get('status')} "
        f"route={route.get('route_id')} origin={route.get('origin')}"
    )


def _unified_input_fingerprint(case: 'dict[str, Any]', route: 'dict[str, Any]') -> str:
    from le_calc import calculation_route, plan_handlers  # noqa: PLC0415

    try:
        case_sha = sha256_file(resolve(ROOT, case['_path']))
    except Exception:
        case_sha = None
    actual_inputs = [
        [str(ref.get('artifact_id') or ''), str(ref.get('path') or ''), str(ref.get('sha256') or '')]
        for ref in _unified_input_refs(case, route)
    ]
    payload = {
        'schema': 'le-unified-run-input-v2',
        'case_id': case.get('case_id'),
        'case_sha256': case_sha,
        'actual_inputs': actual_inputs,
        'route': {
            key: route.get(key)
            for key in ('engine', 'status', 'route_id', 'origin', 'source')
        },
        'compiler': plan_handlers.implementation_fingerprint(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _unified_plan_ref(run_dir: Path) -> 'dict[str, Any] | None':
    plan_path = run_dir / 'calculation-plan.json'
    if not plan_path.is_file():
        return None
    try:
        plan = load_json(plan_path, what='calculation-plan')
    except PipelineError:
        return None
    return {
        'schema': plan.get('schema'),
        'plan_id': plan.get('plan_id'),
        'plan_sha256': plan.get('plan_sha256'),
        'path': relative(plan_path, ROOT),
    }


def _unified_input_refs(case: 'dict[str, Any]', route: 'dict[str, Any]') -> 'list[dict[str, Any]]':
    refs = [dict(ref) for ref in _prepare_input_refs(case, resolve(ROOT, case['_path']))]
    seen = {str(ref.get('path')) for ref in refs}
    # profile 内声明的属性转换规则文件属于实际编译依赖
    source = route.get('source') or {}
    if source.get('source_kind') == 'profile_v2' and source.get('path'):
        try:
            profile = load_json(resolve(ROOT, str(source['path'])), what='dps-profile')
        except Exception:
            profile = {}
        conversion = profile.get('attribute_conversion') or {}
        rules_path = conversion.get('rules') if isinstance(conversion, dict) else None
        if isinstance(rules_path, str) and rules_path.strip() and (ROOT / rules_path).is_file():
            if rules_path not in seen:
                seen.add(rules_path)
                refs.append(
                    {
                        'artifact_id': 'profile.attribute_conversion.rules',
                        'path': rules_path,
                        'sha256': sha256_file(ROOT / rules_path),
                    }
                )
    # dataset 指针 + SQLite 数据库实际内容属于编译/发布依赖（不能只看 dataset 指针 sha）
    dataset_value = case.get('dataset')
    dataset_path_value = dataset_value.get('path') if isinstance(dataset_value, dict) else dataset_value
    if isinstance(dataset_path_value, str) and dataset_path_value.strip():
        try:
            dataset_doc = load_json(resolve(ROOT, dataset_path_value), what='dataset')
        except Exception:
            dataset_doc = {}
        database_value = dataset_doc.get('database') if isinstance(dataset_doc, dict) else None
        if isinstance(database_value, str) and database_value.strip() and (ROOT / database_value).is_file():
            if database_value not in seen:
                seen.add(database_value)
                refs.append(
                    {
                        'artifact_id': 'dataset_content',
                        'path': database_value,
                        'sha256': sha256_file(ROOT / database_value),
                        'schema_version': None,
                    }
                )
    for ref in (route.get('source') or {}).get('input_refs') or []:
        path_value = ref.get('path')
        sha = ref.get('sha256')
        if not path_value or not sha or path_value in seen:
            continue
        seen.add(path_value)
        refs.append(
            {
                'artifact_id': f"route:{ref.get('role') or 'source'}",
                'path': path_value,
                'sha256': sha,
                'schema_version': None,
            }
        )
    return refs


def _run_unified_case(case: 'dict[str, Any]', route: 'dict[str, Any]', args: 'argparse.Namespace') -> int:
    """统一路径 run：compile_plan → simulate → validate_result → render_report → commit。"""
    engine = str(route.get('engine'))
    if engine in ('needs_input', 'needs_research'):
        print(f"run：PARTIAL（{route.get('status')}；无统一计算来源，不生成伪 DPS）")
        for issue in route.get('diagnostics') or []:
            print(f"  - {issue.get('code')}: {issue.get('message')}")
        return EXIT_PARTIAL
    if engine != 'unified_dps':
        fail(f"run：计算来源冲突或不受支持（{_route_summary(route)}）；不回落旧计算器")
        for issue in route.get('diagnostics') or []:
            print(f"  - {issue.get('code')}: {issue.get('message')}")
        return EXIT_FAIL
    run_root = _run_root(case, args)
    run_id = args.run_id or _fresh_run_id(case)
    final_dir = resolve(ROOT, args.out) if getattr(args, 'out', None) else run_root / run_id
    staged = str(getattr(args, 'publish', 'none') or 'none') == 'staged'
    if final_dir.exists() and any(final_dir.iterdir()) and not staged:
        fail(f'输出目录非空，拒绝覆盖：{relative(final_dir, ROOT)}')
        return EXIT_FAIL
    stage_module = _workflow_stage()
    statestore = _workflow_statestore()
    from le_calc.workflow import contract as workflow_contract  # noqa: PLC0415
    from le_calc.workflow import unified  # noqa: PLC0415

    plan = None
    plan_input_fingerprint = _unified_input_fingerprint(case, route)
    if staged:
        plan = _staged_plan(case, args, run_id=run_id, final_dir=final_dir)
        plan['input_fingerprint'] = plan_input_fingerprint
        if plan.get('wait_required'):
            try:
                _wait_staged_lease(plan)
            except PipelineError as exc:
                fail(str(exc))
                return EXIT_FAIL
        if not plan['candidate'].is_dir():
            try:
                _prepare_staged_plan_files(plan)
            except Exception as exc:  # noqa: BLE001
                fail(f'staged 候选准备失败：{exc}')
                return EXIT_FAIL
        work_dir = plan['candidate']
        info(f'staged 发布模式：候选 {relative(work_dir, ROOT)} → 正式 {relative(final_dir, ROOT)}')
    else:
        work_dir = final_dir
    cache = _prepare_stage_cache(args)
    context = workflow_contract.StageContext(
        repo_root=ROOT,
        output_root=work_dir,
        adapters={'stage_cache': cache},
    )
    store = statestore.FileStateStore(work_dir, root=ROOT)
    state = workflow_contract.PipelineState(case_id=str(case.get('case_id')), run_id=run_id)
    endpoint = _recompute_endpoint(args)
    result_holder: dict[str, Any] = {}

    def commit_adapter(request, context):  # noqa: ANN001
        validation_path = work_dir / 'validation-result.json'
        result_path = work_dir / 'result.json'
        plan_path = work_dir / 'calculation-plan.json'
        problems: list[str] = []
        try:
            validation = load_json(validation_path, what='validation-result')
        except PipelineError as exc:
            validation = {}
            problems.append(f'validation-result 不可读：{exc}')
        if not validation.get('passed'):
            problems.append('validate_result 未通过（passed=false）')
        if not list(validation.get('accepted') or []):
            problems.append('ValidationResult.accepted 为空')
        if result_path.is_file():
            if str(validation.get('result_sha256') or '') != sha256_file(result_path):
                problems.append('validation-result 未绑定当前 result sha')
        else:
            problems.append('result.json 缺失')
        plan_doc: dict = {}
        if not plan_path.is_file():
            problems.append('calculation-plan.json 缺失')
        else:
            try:
                plan_doc = load_json(plan_path, what='calculation-plan')
                if str(validation.get('plan_sha256') or '') != str(plan_doc.get('plan_sha256')):
                    problems.append('validation-result 未绑定当前 plan sha')
            except PipelineError as exc:
                problems.append(f'计划/场景不可读：{exc}')
        # 不信任候选目录里的 validation-result：重新做一次只读校验并核对必需检查集合
        try:
            fresh_validation = unified.validate_stage(case, root=ROOT, run_dir=work_dir, write=False)
        except Exception as exc:  # noqa: BLE001 - 门禁必须显式失败
            fresh_validation = {'passed': False, 'payload': {'errors': [{'message': str(exc)}]}}
        fresh_payload = fresh_validation.get('payload') or {}
        if not fresh_validation.get('passed'):
            problems.append('只读复验未通过')
        required = list(unified.required_validation_checks(plan_doc.get("schema")))
        checks_by_id = {str(item.get('id')): item for item in fresh_payload.get('checks') or []}
        missing_checks = [item for item in required if item not in checks_by_id]
        failing_checks = [
            item
            for item in required
            if item in checks_by_id and checks_by_id[item].get('status') != 'PASS'
        ]
        if missing_checks:
            problems.append('缺少必需检查：' + ', '.join(missing_checks[:4]))
        if failing_checks:
            problems.append('必需检查未通过：' + ', '.join(failing_checks[:4]))
        if str(fresh_payload.get('plan_sha256') or '') != str(validation.get('plan_sha256') or ''):
            problems.append('只读复验 plan sha 与候选 validation-result 不一致')
        if str(fresh_payload.get('result_sha256') or '') != str(validation.get('result_sha256') or ''):
            problems.append('只读复验 result sha 与候选 validation-result 不一致')
        if problems:
            return {
                'execution_status': workflow_contract.EXECUTION_FAILED,
                'readiness': workflow_contract.PARTIAL,
                'outputs': [],
                'issues': [
                    workflow_contract.make_issue(
                        'commit_gate_rejected',
                        '；'.join(problems[:4]),
                        severity=workflow_contract.SEVERITY_ERROR,
                    )
                ],
                'metrics': {'commit_status': 'rejected'},
            }
        # manifest 必须包含本轮全部产物；staged 发布前由同一 manifest 驱动校验。
        unified.write_manifest(
            case=case,
            root=ROOT,
            run_dir=work_dir,
            display_dir=final_dir,
        )
        if plan is not None:
            document = _publish_staged_run(plan)
            if document is None:
                return {
                    'execution_status': workflow_contract.EXECUTION_FAILED,
                    'readiness': workflow_contract.PARTIAL,
                    'outputs': [],
                    'issues': [
                        workflow_contract.make_issue(
                            'commit_publish_failed',
                            'commit 阶段 staged 发布失败（候选与意图已保留）',
                            severity=workflow_contract.SEVERITY_ERROR,
                        )
                    ],
                    'metrics': {'commit_status': 'failed'},
                }
            return {
                'execution_status': workflow_contract.EXECUTION_COMPLETED,
                'readiness': workflow_contract.READY,
                'outputs': [],
                'issues': [],
                'metrics': {'commit_status': document.get('status'), 'operation_id': document.get('operation_id')},
            }
        try:
            persisted = store.load() or state
            manifest = load_json(work_dir / 'run-manifest.json', what='run manifest')
            persisted.last_commit = {
                'operation_id': _staged_operation_id(str(case.get('case_id')), run_id),
                'status': 'committed',
                'attempt_id': run_id,
                'input_fingerprint': plan_input_fingerprint,
                'committed_refs': [{'path': relative(final_dir, ROOT), 'file_count': len(manifest.get('files') or {})}],
                'recorded_at': _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
            }
            store.save(persisted)
        except Exception:  # noqa: BLE001 - state metadata is best effort
            pass
        return {
            'execution_status': workflow_contract.EXECUTION_COMPLETED,
            'readiness': workflow_contract.READY,
            'outputs': [],
            'issues': [],
            'metrics': {'commit_status': 'committed_in_place'},
        }

    context.adapters['commit'] = commit_adapter
    base_params = {'case_ref': relative(case['_path'], ROOT)}
    sequence = ('compile_plan', 'simulate', 'validate_result', 'render_report', 'commit')
    results = []
    dependency_chain: dict[str, str] = {}
    for stage_id in sequence:
        params = dict(base_params)
        if stage_id == 'simulate':
            plan_ref = _unified_plan_ref(work_dir)
            if plan_ref is None:
                fail('simulate：缺少 calculation-plan.json（compile_plan 未完成）')
                return EXIT_FAIL
            params['render_html'] = False
            params['plan_ref'] = {
                'schema': plan_ref.get('schema'),
                'plan_id': plan_ref.get('plan_id'),
                'plan_sha256': plan_ref.get('plan_sha256'),
            }
        elif stage_id == 'render_report':
            params.update(
                {
                    'recalc_endpoint': endpoint,
                    'target_dir': relative(final_dir, ROOT),
                }
            )
        elif stage_id == 'commit':
            params.update(
                {
                    'operation_id': plan['operation_id'] if plan is not None else _staged_operation_id(str(case.get('case_id')), run_id),
                    'attempt_id': plan['attempt_id'] if plan is not None else run_id,
                    'input_fingerprint': plan_input_fingerprint,
                    'target': relative(final_dir, ROOT),
                    'publish_mode': 'staged' if plan is not None else 'none',
                }
            )
        request = workflow_contract.StageRequest(
            stage_id=stage_id,
            case_id=str(case.get('case_id')),
            run_id=run_id,
            input_refs=_unified_input_refs(case, route),
            dependency_fingerprints={'dependencies': dict(dependency_chain)},
            parameters=params,
        )
        result = stage_module.execute_stage(request, context)
        results.append(result)
        cache_key = (result.cache or {}).get('key') or (result.metrics or {}).get('cache_key')
        if cache_key:
            dependency_chain[stage_id] = str(cache_key)
        if stage_id != 'commit':
            store.record(state, result)
            store.save(state)
        if result.execution_status in (
            workflow_contract.EXECUTION_FAILED,
            workflow_contract.EXECUTION_WAITING,
        ):
            heading('阶段状态')
            for entry in results:
                print(f'  {entry.stage_id:<16} {entry.execution_status:<9} readiness={entry.readiness}')
                for issue in entry.issues or []:
                    print(f"      [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}")
            if plan is not None:
                warn(f'staged 候选保留：{relative(work_dir, ROOT)}（正式目录未发布）')
            if result.execution_status == workflow_contract.EXECUTION_WAITING:
                return EXIT_PARTIAL
            return EXIT_FAIL
        if plan is not None and stage_id not in ('compile_plan',):
            _refresh_stage_output_refs(result, work_dir, final_dir)
    result_doc = load_json(final_dir / 'result.json', what='result') if (final_dir / 'result.json').is_file() else {}
    validation = load_json(final_dir / 'validation-result.json', what='validation-result') if (final_dir / 'validation-result.json').is_file() else {}
    print(f"运行目录：{relative(final_dir, ROOT)}")
    print(f"HTML 报告：{relative(final_dir / unified.HTML_NAME, ROOT)}")
    print(f"重算端点：{endpoint}")
    heading('阶段状态')
    for entry in results:
        print(f'  {entry.stage_id:<16} {entry.execution_status:<9} readiness={entry.readiness}')
    if not validation.get('passed', False):
        fail('validation 未通过：结果未提交')
        return EXIT_FAIL
    coverage = (result_doc.get('coverage') or {})
    print(f"覆盖={coverage.get('status')} 缺口={coverage.get('gap_count')} 计划={result_doc.get('plan_sha256')}")
    if coverage.get('status') == 'partial':
        for gap in (coverage.get('gaps') or [])[:10]:
            print(f"  - {gap.get('code')}：{gap.get('detail')}")
        return EXIT_PARTIAL
    return EXIT_OK


def _run_unified_prepare(
    case: 'dict[str, Any]', route: 'dict[str, Any]', args: 'argparse.Namespace', *, run_id: str, run_dir: Path
) -> int:
    """统一 prepare：冻结输入已合法时直接 compile_plan + min_validate。"""
    from le_calc.workflow import contract as workflow_contract  # noqa: PLC0415
    from le_calc.workflow import unified  # noqa: PLC0415

    engine = str(route.get('engine'))
    if engine in ('needs_input', 'needs_research'):
        print(f"prepare：PARTIAL（{route.get('status')}；无统一计算来源，未生成计划）")
        for issue in route.get('diagnostics') or []:
            print(f"  - {issue.get('code')}: {issue.get('message')}")
        return EXIT_PARTIAL
    if engine != 'unified_dps':
        fail(f"prepare：计算来源冲突或不受支持（{_route_summary(route)}）")
        return EXIT_FAIL
    run_dir.mkdir(parents=True, exist_ok=True)
    stage_module = _workflow_stage()
    statestore = _workflow_statestore()
    context = workflow_contract.StageContext(repo_root=ROOT, output_root=run_dir, adapters={})
    store = statestore.FileStateStore(run_dir, root=ROOT)
    state = workflow_contract.PipelineState(case_id=str(case.get('case_id')), run_id=run_id)
    base_params = {'case_ref': relative(case['_path'], ROOT)}
    results = []
    for stage_id in ('compile_plan', 'min_validate'):
        request = workflow_contract.StageRequest(
            stage_id=stage_id,
            case_id=str(case.get('case_id')),
            run_id=run_id,
            input_refs=[dict(ref) for ref in _prepare_input_refs(case, resolve(ROOT, case['_path']))],
            parameters=dict(base_params),
        )
        result = stage_module.execute_stage(request, context)
        results.append(result)
        store.record(state, result)
        store.save(state)
        if result.execution_status in (
            workflow_contract.EXECUTION_FAILED,
            workflow_contract.EXECUTION_WAITING,
        ):
            break
    try:
        from _common import write_json as _write_json
        _write_json(
            run_dir / 'prepare-context.json',
            {
                'schema_version': 'le-prepare-context-v1',
                'case_id': case.get('case_id'),
                'run_id': run_id,
                'mode': 'unified',
                'route': route,
                'prepare_stages': ['compile_plan', 'min_validate'],
                'stage_results': [entry.to_dict() for entry in results],
            },
        )
    except Exception:
        pass
    heading('阶段状态')
    for entry in results:
        print(f'  {entry.stage_id:<16} {entry.execution_status:<9} readiness={entry.readiness}')
        for issue in entry.issues or []:
            print(f"      [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}")
    print(f'计划产物：{relative(run_dir / "calculation-plan.json", ROOT)}')
    if any(entry.execution_status == workflow_contract.EXECUTION_FAILED for entry in results):
        fail('prepare：FAIL（统一计划编译/门禁未通过）')
        return EXIT_FAIL
    if any(entry.execution_status == workflow_contract.EXECUTION_WAITING for entry in results):
        print('prepare：PARTIAL（waiting；需要补齐计算定义/输入）')
        return EXIT_PARTIAL
    if results and results[0].readiness == 'partial':
        print('prepare：PASS（readiness=partial；计划已生成，覆盖缺口保留）')
        return EXIT_OK
    print('prepare：PASS（统一计划编译与门禁通过）')
    return EXIT_OK


_RUN_STAGE_SEQUENCE = ("simulate", "validate_result", "render_report", "commit")


_P4_RUN_ID_TOKEN = "__LE_P4_RUN_ID_PLACEHOLDER__"
_P4_OUT_DIR_TOKEN = "__LE_P4_OUT_DIR_PLACEHOLDER__"


def _bind_stage_placeholders(
    work_dir: Path, run_id: str, final_dir: Path
) -> int:
    """Replace only the unique run-id/out-dir placeholder tokens.

    Cache entries are generated with these tokens (not the real run id), so a
    cache hit across run-ids/base strings is metadata rebinding only: never a
    business-string replacement.  Tokens cannot collide with model fields.
    """
    try:
        out_display = relative(final_dir, ROOT)
    except PipelineError:
        out_display = str(final_dir.resolve())
    replacements = {
        _P4_RUN_ID_TOKEN: run_id,
        _P4_OUT_DIR_TOKEN: out_display,
    }
    changed = 0
    for path in sorted(work_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _STAGED_TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = text
        for token, value in replacements.items():
            if token in updated:
                updated = updated.replace(token, value)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed += 1
    if changed:
        _sync_all_run_manifest_files(work_dir)
    return changed


def _refresh_stage_output_refs(
    result, work_dir: Path, final_dir: Path
) -> None:
    """Recompute stage output refs against current bytes and final paths.

    ``run-manifest.json`` and ``pipeline-state.json`` are excluded because later
    stages intentionally rewrite them (manifest gets html/caps/state hashes),
    which would create a state<->manifest circular hash.
    """
    try:
        final_display = relative(final_dir, ROOT)
    except PipelineError:
        final_display = str(final_dir.resolve())
    refs: list[dict[str, Any]] = []
    for ref in list(result.output_refs or []):
        path_value = str((ref or {}).get("path") or "")
        if not path_value:
            continue
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        try:
            relative_candidate = candidate.resolve().relative_to(work_dir.resolve())
        except (ValueError, OSError):
            continue
        rel = relative_candidate.as_posix()
        if rel in ("run-manifest.json", "pipeline-state.json"):
            continue
        if not candidate.is_file():
            continue
        new_ref = dict(ref)
        new_ref["path"] = f"{final_display}/{rel}".replace("//", "/")
        new_ref["sha256"] = sha256_file(candidate)
        new_ref["bytes"] = candidate.stat().st_size
        refs.append(new_ref)
    result.output_refs = refs


def _run_staged_stage_pipeline(
    case: dict[str, Any],
    *,
    run_id: str,
    plan: dict[str, Any] | None,
    args: argparse.Namespace,
    is_hit: bool,
    final_dir: Path | None = None,
) -> int:
    """Formal run stages: simulate -> validate_result -> render_report -> commit.

    ``plan`` is set for ``--publish staged`` (candidate + lease + atomic publish);
    ``plan is None`` runs the same stages in the final directory (``--publish
    none``: path/exit semantics preserved, no operation record).
    """
    stage_module = _workflow_stage()
    statestore = _workflow_statestore()
    cache = _prepare_stage_cache(args)
    from le_calc.workflow import contract as workflow_contract  # noqa: PLC0415

    in_place = plan is None
    if final_dir is None:
        final_dir = plan["final_dir"] if plan is not None else Path(".")
    work_dir = final_dir if in_place else plan["candidate"]
    context = workflow_contract.StageContext(
        repo_root=ROOT,
        output_root=work_dir,
        adapters={"stage_cache": cache},
    )
    store = statestore.FileStateStore(work_dir, root=ROOT)
    state = workflow_contract.PipelineState(
        case_id=str(case.get("case_id")), run_id=run_id
    )
    # NOTE: do not write pipeline-state before simulate: domain.simulate requires
    # an empty output directory (it protects historical runs).
    case_file = resolve(ROOT, case["_path"])
    base_refs = _prepare_input_refs(case, case_file)
    try:
        expected_path = case_path(case, "expected")
    except PipelineError:
        expected_path = None
    endpoint = _recompute_endpoint(args)
    _, capabilities_module = _le_calc_modules()
    renderer_fp = capabilities_module.renderer_fingerprint(ROOT)
    report_cache = not (
        getattr(args, "no_report_cache", False) or getattr(args, "no_cache", False)
    )
    model = _case_model(case)
    html_name = "flame-reave-damage.html" if model == "hit" else "black-hole-damage.html"

    def _path_ref(path: Path | None, role: str) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            rel = relative(path, ROOT)
        except PipelineError:
            return None
        if not path.is_file():
            return None
        return {
            "artifact_id": role,
            "path": rel,
            "asset_id": None,
            "schema_version": None,
            "sha256": sha256_file(path),
        }

    def render_adapter(request, context):
        params = dict(request.parameters or {})
        used_endpoint = str(params.get("recalc_endpoint") or endpoint)
        finalize = _finalize_run_report(
            case,
            work_dir,
            endpoint=used_endpoint,
            use_cache=bool(params.get("report_cache", report_cache)),
            display_dir=final_dir,
        )
        html_path = work_dir / html_name
        caps_path = work_dir / _REPORT_CAPABILITIES_FILE
        outputs = [str(path) for path in (html_path, caps_path) if path.is_file()]
        if not html_path.is_file():
            return {
                "execution_status": workflow_contract.EXECUTION_FAILED,
                "readiness": workflow_contract.PARTIAL,
                "outputs": outputs,
                "issues": [
                    workflow_contract.make_issue(
                        "report_not_rendered",
                        "render_report 未能生成离线 HTML 报告",
                        severity=workflow_contract.SEVERITY_ERROR,
                    )
                ],
                "metrics": {"endpoint": used_endpoint},
            }
        return {
            "execution_status": workflow_contract.EXECUTION_COMPLETED,
            "readiness": workflow_contract.READY,
            "outputs": outputs,
            "issues": [],
            "metrics": {
                "endpoint": used_endpoint,
                "renderer_fingerprint": params.get("renderer_fingerprint"),
                "report_key": (finalize or {}).get("report_key"),
            },
        }

    def commit_adapter(request, context):
        validation_path = work_dir / "validation-result.json"
        result_path = work_dir / "result.json"
        problems: list[str] = []
        try:
            validation = load_json(validation_path, what="validation-result")
        except PipelineError as exc:
            validation = {}
            problems.append(f"validation-result 不可读：{exc}")
        if not validation.get("passed"):
            problems.append("validate_result 未通过（ValidationResult.passed=false）")
        accepted = list(validation.get("accepted") or [])
        if not accepted:
            problems.append("ValidationResult.accepted 为空")
        if result_path.is_file():
            actual_sha = stage_module.stable_result_digest(result_path)
            if str(validation.get("result_sha256") or "") != actual_sha:
                problems.append("validation-result 未绑定当前 result sha（结果已变化或未校验）")
        else:
            problems.append("result.json 缺失")
        if problems:
            return {
                "execution_status": workflow_contract.EXECUTION_FAILED,
                "readiness": workflow_contract.PARTIAL,
                "outputs": [],
                "issues": [
                    workflow_contract.make_issue(
                        "commit_gate_rejected",
                        "；".join(problems[:4]),
                        severity=workflow_contract.SEVERITY_ERROR,
                        related_ids=[request.stage_id],
                    )
                ],
                "metrics": {"commit_status": "rejected"},
            }
        # Record the commit stage in the candidate state before publishing.
        try:
            fingerprints = stage_module.stage_fingerprints(request, context)
            persisted = store.load() or state
            commit_result = workflow_contract.StageResult(
                stage_id="commit",
                execution_status=workflow_contract.EXECUTION_COMPLETED,
                readiness=workflow_contract.READY,
                output_refs=[],
                metrics={
                    "input_fingerprint": fingerprints.get("input_fingerprint"),
                    "implementation_fingerprint": fingerprints.get("implementation_fingerprint"),
                    "parameters_fingerprint": fingerprints.get("parameters_fingerprint"),
                    "cache_key": fingerprints.get("cache_key"),
                    "cache_status": workflow_contract.CACHE_DISABLED,
                },
                cache=workflow_contract.make_cache_info(
                    workflow_contract.CACHE_DISABLED,
                    key=fingerprints.get("cache_key"),
                    reason="stage_not_cacheable",
                ),
            )
            store.record(persisted, commit_result)
            store.save(persisted)
        except Exception:  # noqa: BLE001 - state metadata is best effort
            pass
        if plan is not None:
            document = _publish_staged_run(plan)
            if document is None:
                published = plan["final_dir"].exists()
                detail = (
                    "候选保留；正式目录已发布，但记录未完成（意图已保留，可重试恢复）"
                    if published
                    else "候选保留，正式目录未发布"
                )
                return {
                    "execution_status": workflow_contract.EXECUTION_FAILED,
                    "readiness": workflow_contract.PARTIAL,
                    "outputs": [],
                    "issues": [
                        workflow_contract.make_issue(
                            "commit_publish_failed",
                            f"commit 阶段发布失败（{detail}）",
                            severity=workflow_contract.SEVERITY_ERROR,
                        )
                    ],
                    "metrics": {"commit_status": "failed", "published": published},
                }
            return {
                "execution_status": workflow_contract.EXECUTION_COMPLETED,
                "readiness": workflow_contract.READY,
                "outputs": [],
                "issues": [],
                "metrics": {
                    "commit_status": document.get("status"),
                    "operation_id": document.get("operation_id"),
                },
            }
        # --publish none: keep the final directory path, record in-place state
        try:
            persisted = store.load() or state
            manifest_path = work_dir / "run-manifest.json"
            file_count = 0
            if manifest_path.is_file():
                try:
                    file_count = len((load_json(manifest_path, what="run manifest").get("files") or {}))
                except PipelineError:
                    file_count = 0
            persisted.last_commit = {
                "operation_id": _staged_operation_id(str(case.get("case_id")), run_id),
                "status": "committed",
                "attempt_id": str(plan.get("attempt_id") if plan else run_id),
                "input_fingerprint": _staged_run_input_fingerprint(case, run_id, args),
                "committed_refs": [{"path": relative(final_dir, ROOT), "file_count": file_count}],
                "recorded_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
            }
            store.save(persisted)
        except Exception:  # noqa: BLE001 - in-place state is best effort
            pass
        return {
            "execution_status": workflow_contract.EXECUTION_COMPLETED,
            "readiness": workflow_contract.READY,
            "outputs": [],
            "issues": [],
            "metrics": {"commit_status": "committed_in_place"},
        }

    context.adapters["render_report"] = render_adapter
    context.adapters["commit"] = commit_adapter
    # Display paths belong to the current target but must not enter simulate/
    # validate cache keys; cached artifacts are rebased on hit.
    context.adapters["run_output_display"] = _P4_OUT_DIR_TOKEN
    context.adapters["run_result_display"] = f"{_P4_OUT_DIR_TOKEN}/result.json"
    dependency_chain: dict[str, str] = {}
    results = []
    for stage_id in _RUN_STAGE_SEQUENCE:
        params: dict[str, Any] = {"case_ref": relative(case["_path"], ROOT)}
        refs = [dict(ref) for ref in base_refs]
        if stage_id == "simulate":
            params["render_html"] = False
        elif stage_id == "validate_result":
            ref = _path_ref(expected_path, "case.expected")
            if ref:
                refs.append(ref)
            # result_ref display comes from the run context adapter so the
            # validate cache key stays independent of the target path.
        elif stage_id == "render_report":
            params.update(
                {
                    "recalc_endpoint": endpoint,
                    "renderer_fingerprint": renderer_fp,
                    "report_cache": report_cache,
                    "target_dir": relative(final_dir, ROOT),
                }
            )
        elif stage_id == "commit":
            validation_ref = _path_ref(work_dir / "validation-result.json", "stage.validation")
            if validation_ref:
                refs.append(validation_ref)
            if plan is not None:
                params.update(
                    {
                        "operation_id": plan["operation_id"],
                        "attempt_id": plan["attempt_id"],
                        "input_fingerprint": plan["input_fingerprint"],
                        "target": relative(final_dir, ROOT),
                        "publish_mode": "staged",
                    }
                )
            else:
                params.update(
                    {
                        "operation_id": _staged_operation_id(
                            str(case.get("case_id")), run_id
                        ),
                        "attempt_id": run_id,
                        "input_fingerprint": _staged_run_input_fingerprint(
                            case, run_id, args
                        ),
                        "target": relative(final_dir, ROOT),
                        "publish_mode": "none",
                    }
                )
        request = workflow_contract.StageRequest(
            stage_id=stage_id,
            case_id=str(case.get("case_id")),
            run_id=(_P4_RUN_ID_TOKEN if stage_id in ("simulate", "validate_result") else run_id),
            input_refs=refs,
            parameters=params,
            dependency_fingerprints={"dependencies": dict(dependency_chain)},
        )
        result = stage_module.execute_stage(request, context)
        if stage_id in ("simulate", "validate_result"):
            _bind_stage_placeholders(work_dir, run_id, final_dir)
        results.append(result)
        cache_key = (result.cache or {}).get("key") or (result.metrics or {}).get("cache_key")
        if cache_key:
            dependency_chain[stage_id] = str(cache_key)
        if stage_id != "commit":
            _refresh_stage_output_refs(result, work_dir, final_dir)
            store.record(state, result)
            store.save(state)
        if result.execution_status in (
            workflow_contract.EXECUTION_FAILED,
            workflow_contract.EXECUTION_WAITING,
        ):
            heading("阶段状态")
            width = max(len(entry.stage_id) for entry in results)
            for entry in results:
                cache_status = (entry.cache or {}).get("status", "")
                elapsed = (entry.metrics or {}).get("elapsed_ms", 0)
                print(
                    f"  {entry.stage_id:<{width}}  {entry.execution_status:<9}"
                    f"  cache={cache_status:<9}  {float(elapsed):8.1f}ms"
                )
                for issue in entry.issues or []:
                    print(f"      [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}")
            if plan is not None and final_dir.exists():
                warn(
                    f"staged 候选保留：{relative(work_dir, ROOT)}"
                    f"（正式目录已发布，但记录未完成；意图保留，可重试恢复）"
                )
            elif plan is not None:
                warn(f"staged 候选保留：{relative(work_dir, ROOT)}（正式目录未发布）")
            else:
                warn(f"运行在 {result.stage_id} 阶段停止；诊断保留：{relative(work_dir, ROOT)}")
            return EXIT_FAIL
    # commit adapter published the candidate; final run dir is authoritative
    try:
        validation = load_json(final_dir / "validation-result.json", what="validation-result")
    except PipelineError:
        validation = {}
    print(f"运行目录：{relative(final_dir, ROOT)}")
    print(f"HTML 报告：{relative(final_dir / html_name, ROOT)}")
    print(f"重算端点：{endpoint}")
    heading("阶段状态")
    width = max(len(entry.stage_id) for entry in results)
    for entry in results:
        cache_status = (entry.cache or {}).get("status", "")
        elapsed = (entry.metrics or {}).get("elapsed_ms", 0)
        print(
            f"  {entry.stage_id:<{width}}  {entry.execution_status:<9}"
            f"  cache={cache_status:<9}  {float(elapsed):8.1f}ms"
        )
    heading("数值核对")
    checks = [
        {
            "name": str(check.get("id") or check.get("name") or "check"),
            "status": str(check.get("status") or "FAIL"),
            "detail": check.get("detail"),
        }
        for check in validation.get("checks") or []
    ]
    if checks:
        print_checks(checks)
    if not validation.get("passed", False):
        fail("validation 未通过：结果未提交")
        return EXIT_FAIL
    return EXIT_OK


def _report_fingerprints(manifest: 'dict[str, Any]') -> 'dict[str, str]':
    '''Normalise run-manifest fingerprints to the recalc contract key set.'''

    run = manifest.get('run') or {}
    ruleset = run.get('ruleset') or {}
    input_block = manifest.get('input') or {}
    calc_input = input_block.get('calc_input') or {}
    dataset = input_block.get('dataset') or {}
    files = manifest.get('files') or {}
    snapshot_sha = str(files.get('input-snapshot.json') or '')
    if not snapshot_sha:
        snapshot_sha = str((input_block.get('snapshot') or {}).get('sha256') or '')
    fingerprints = {'calc_input': str(calc_input.get('sha256') or ''), 'ruleset': str(ruleset.get('sha256') or ''), 'snapshot': snapshot_sha, 'dataset': str(dataset.get('logical_content_sha256') or dataset.get('file_sha256') or '')}
    return fingerprints

def _case_input_fingerprints(case: 'dict[str, Any]') -> 'dict[str, str]':
    '''Declared case input fingerprints (fallback when a manifest key is absent).'''

    declared = case.get('input_sha256') or {}
    dataset_expected = case.get('dataset_expected') or {}
    return {'calc_input': str(declared.get('calculation_input') or declared.get('calc_input') or ''), 'ruleset': str(declared.get('ruleset') or (case.get('ruleset') or {}).get('sha256') or ''), 'snapshot': str(declared.get('snapshot') or ''), 'dataset': str(dataset_expected.get('logical_content_sha256') or dataset_expected.get('file_sha256') or '')}

def _report_run_dir(case: 'dict[str, Any]', value: 'str | Path') -> 'Path':
    '''Resolve ``--report`` to an existing run directory (rejects silently wrong runs).'''

    run_root = resolve(ROOT, (case.get('output') or {}).get('root') or 'reports')
    candidate = Path(value)
    options = []
    if candidate.is_absolute():
        options.append(candidate.resolve())
    else:
        options.extend([(ROOT / candidate).resolve(), (run_root / candidate).resolve()])
    for option in options:
        if option.is_dir() and (option / 'result.json').is_file():
            return option
        if option.is_file() and option.name == 'result.json':
            return option.parent
        if not option.is_file():
            continue
        if option.suffix.lower() != '.json':
            continue
        return option.parent
    raise PipelineError(f'--report 指向的运行目录不可用：{value}（需要包含 result.json 的 run 目录，例如 {relative(run_root, ROOT)}/<run-id>）')

def _select_report(case: 'dict[str, Any]', args: 'argparse.Namespace') -> 'tuple[Path, bool]':
    '''Resolve the report selected for ``serve``; returns ``(run_dir, canonical)``.'''

    requested = getattr(args, 'report', None)
    run_root = resolve(ROOT, (case.get('output') or {}).get('root') or 'reports')
    if requested:
        return _report_run_dir(case, requested), False
    canonical_id = str((case.get('output') or {}).get('canonical_run_id') or '').strip()
    if not canonical_id:
        raise PipelineError('案例没有 output.canonical_run_id；请用 --report <run-id|目录> 显式选择报告')
    canonical = run_root / canonical_id
    if not (canonical / 'result.json').is_file():
        raise PipelineError(f'默认规范 run 不存在：{relative(canonical, ROOT)}（请先用 le.ps1 run 生成，或用 --report 显式选择其它 run）')
    return canonical, True

def _build_report_ref(case: 'dict[str, Any]', run_dir: 'Path', *, endpoint: 'str', fingerprints: 'dict[str, str]', renderer_fp: 'str', model_id: 'str | None'=None, canonical: 'bool'=False, display_dir: 'Path | None'=None) -> 'dict[str, Any]':
    '''Build the ``le-report-ref-v1`` binding used by bindings/serve/requests.'''

    recalc_module, capabilities_module = _le_calc_modules()
    model_id = model_id or _case_model(case)
    html_name = 'flame-reave-damage.html' if model_id == 'hit' else 'black-hole-damage.html'
    path_dir = display_dir or run_dir
    return recalc_module.build_report_ref(case_id=str(case.get('case_id')), model_id=model_id, run_id=path_dir.name, run_dir=relative(path_dir, ROOT), report_path=relative(path_dir / html_name, ROOT), input_fingerprints=fingerprints, recompute_endpoint=endpoint, ui_contract_version=capabilities_module.UI_CONTRACT_VERSION, renderer_fingerprint=renderer_fp, canonical=canonical)

def _report_scenario_doc(case: 'dict[str, Any]') -> 'dict[str, Any] | None':
    '''Load the case scenario document (FR has ``profiles`` → ``profile_states``).'''

    try:
        scenario_path = optional_case_path(case, 'inputs.scenario')
    except PipelineError:
        return
    if scenario_path is None or not scenario_path.is_file():
        return
    try:
        document = load_json(scenario_path, what='case scenario')
    except PipelineError:
        return
    if isinstance(document, dict):
        return document

def _case_ref_for_acceptance(case: 'dict[str, Any]') -> 'str | None':
    '''Repo-relative case.json path for acceptance refs (None when outside the tree).'''

    raw = case.get('_path')
    if not isinstance(raw, (str, Path)):
        return None
    try:
        return Path(raw).resolve().relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return None

def _write_report_capabilities(case: 'dict[str, Any]', run_dir: 'Path', *, endpoint: 'str', cache: 'dict[str, Any] | None'=None, bindings: 'dict[str, Any] | None'=None, scenario_doc: 'dict[str, Any] | None'=None, display_dir: 'Path | None'=None) -> 'tuple[Path, dict[str, Any]]':
    '''Build/write ``report-capabilities.json`` next to the run report.'''

    recalc_module, capabilities_module = _le_calc_modules()
    model_id = _case_model(case)
    path_dir = display_dir or run_dir
    result_path = run_dir / 'result.json'
    result = load_json(result_path, what='run result')
    registry_path = None
    registry_spec = case.get('conditional_registry') or {}
    if isinstance(registry_spec, dict) and registry_spec.get('path'):
        registry_path = resolve(ROOT, str(registry_spec['path']))
    document = capabilities_module.build_report_capabilities(case_id=str(case.get('case_id')), model_id=model_id, repo_root=ROOT, run_id=path_dir.name, report_path=None, result=result, registry=load_json(registry_path, what='conditional registry') if registry_path and registry_path.is_file() else None, scenario_doc=scenario_doc, bindings=bindings, generated_at=None, case_ref=_case_ref_for_acceptance(case))
    html_name = 'flame-reave-damage.html' if model_id == 'hit' else 'black-hole-damage.html'
    html_path = path_dir / html_name
    if html_path.is_file() or bindings is not None:
        document['report_path'] = relative(html_path, ROOT)
    if cache is not None:
        document['cache'] = dict(cache)
    capabilities_module.validate_report_capabilities(document, repo_root=ROOT)
    path = capabilities_module.write_report_capabilities(capabilities_module.capabilities_path_for_run(run_dir), document)
    return path, document

def _finalize_run_report(case: 'dict[str, Any]', run_dir: 'Path', *, endpoint: 'str', use_cache: 'bool'=True, display_dir: 'Path | None'=None) -> 'dict[str, Any] | None':
    '''Attach the actual endpoint + capabilities to a freshly generated report.

    The numeric result is untouched: only the HTML is re-rendered through the
    shared renderer with ``recalc_endpoint``/``capabilities`` (P3R-A interface),
    and a report cache entry keyed by endpoint + UI/renderer fingerprint is
    written.  Failures are reported as warnings and never invalidate the numeric
    run (the original report stays on disk).
    '''

    recalc_module, capabilities_module = _le_calc_modules()
    model_id = _case_model(case)
    result_path = run_dir / 'result.json'
    manifest_path = run_dir / 'run-manifest.json'
    html_name = 'flame-reave-damage.html' if model_id == 'hit' else 'black-hole-damage.html'
    html_path = run_dir / html_name
    if not result_path.is_file():
        warn(f'未找到 result.json，跳过报告端点接线：{relative(run_dir, ROOT)}')
        return
    try:
        result = load_json(result_path, what='run result')
        manifest = load_json(manifest_path, what='run manifest') if manifest_path.is_file() else {}
        fingerprints = _report_fingerprints(manifest)
        declared = _case_input_fingerprints(case)
        for key, value in declared.items():
            if fingerprints.get(key):
                continue
            if value:
                fingerprints[key] = value
        registry_path = None
        registry_spec = case.get('conditional_registry') or {}
        if isinstance(registry_spec, dict) and registry_spec.get('path'):
            registry_path = resolve(ROOT, str(registry_spec['path']))
        capabilities = capabilities_module.capabilities_for_model(model_id, repo_root=ROOT, result=result, registry=load_json(registry_path, what='conditional registry') if registry_path and registry_path.is_file() else None, scenario_doc=_report_scenario_doc(case))
        renderer_fp = capabilities_module.renderer_fingerprint(ROOT)
        scenario_doc = result.get('scenario') or {}
        scenario_digest = recalc_module.digest_document(scenario_doc)
        report_ref = _build_report_ref(case, run_dir, endpoint=endpoint, fingerprints=fingerprints, renderer_fp=renderer_fp, model_id=model_id, display_dir=display_dir)
        bindings = capabilities_module.recalc_bindings_for_report(capabilities=capabilities, report_ref=report_ref, input_fingerprints=fingerprints, renderer_fingerprint=renderer_fp, recompute_endpoint=endpoint)
        numeric_key = recalc_module.numeric_cache_key(model_id=model_id, capabilities_version=capabilities['recalculation']['capabilities_version'], input_fingerprints=fingerprints, scenario_digest=scenario_digest)
        report_key = recalc_module.report_cache_key(numeric_key=numeric_key, endpoint=endpoint, ui_contract_version=capabilities_module.UI_CONTRACT_VERSION, renderer_fingerprint=renderer_fp, capabilities_digest=capabilities_module.capabilities_digest(capabilities), result_digest=recalc_module.digest_document(result), bindings_digest=recalc_module.bindings_digest(bindings))
        cache = recalc_module.ReportCache(recalc_module.default_cache_root(ROOT), enabled=use_cache)
        cached = cache.get(report_key)
        if cached is not None and html_path.is_file():
            info(f'报告缓存命中：{report_key[:16]}…（端点 {endpoint}）')
            html_path.write_text(cached, encoding='utf-8', newline='\n')
        else:
            html = capabilities_module.render_report(result, model_id=model_id, recalc_endpoint=endpoint, capabilities=capabilities, bindings=bindings)
            # G1 render_report may be the first HTML writer (simulate no longer renders).
            html_path.write_text(html, encoding='utf-8', newline='\n')
            cache.put(report_key, html)
            info(f'报告已按实际端点重渲染：{relative(html_path, ROOT)}（端点 {endpoint}）')
        metadata = {'numeric_key': numeric_key, 'report_key': report_key, 'recompute_endpoint': endpoint, 'ui_contract_version': capabilities_module.UI_CONTRACT_VERSION, 'renderer_fingerprint': renderer_fp, 'bindings_digest': recalc_module.bindings_digest(bindings)}
        path, document = _write_report_capabilities(case, run_dir, endpoint=endpoint, cache=metadata, bindings=bindings, scenario_doc=_report_scenario_doc(case), display_dir=display_dir)
        _sync_run_manifest_files(run_dir, [html_name, _REPORT_CAPABILITIES_FILE])
        info(f'能力元数据：{relative(path, ROOT)}（capabilities_version={document['capabilities']['recalculation']['capabilities_version']}）')
        info(f'缓存键：数值 {numeric_key[:16]}…（不含端口）；报告 {report_key[:16]}…（含端口）')
        return {'html_path': html_path, 'capabilities_path': path, 'numeric_key': numeric_key, 'report_key': report_key, 'endpoint': endpoint}
    except Exception as exc:
        warn(f'报告端点/能力元数据接线失败（数值结果不受影响）：{exc}')

def _report_metadata(run_dir: 'Path') -> 'dict[str, Any]':
    '''Read the report binding metadata for serve startup validation.'''

    capabilities_path = run_dir / _REPORT_CAPABILITIES_FILE
    if capabilities_path.is_file():
        try:
            return load_json(capabilities_path, what='report-capabilities')
        except PipelineError:
            return {}

def _serve_report_checks(case: 'dict[str, Any]', run_dir: 'Path', *, endpoint: 'str', canonical: 'bool') -> 'list[str]':
    '''3-A §10.4.4 startup checks: report metadata vs case/model/fingerprints/port.'''

    recalc_module, _ = _le_calc_modules()
    model_id = _case_model(case)
    problems = []
    metadata = _report_metadata(run_dir)
    if metadata:
        if str(metadata.get('case_id')) != str(case.get('case_id')):
            problems.append(f'报告 case_id={metadata.get('case_id')!r} 与案例 {case.get('case_id')!r} 不一致')
        if str(metadata.get('model_id')) != model_id:
            problems.append(f'报告 model_id={metadata.get('model_id')!r} 与案例模型 {model_id!r} 不一致')
        cache = metadata.get('cache') or {}
        report_endpoint = cache.get('recompute_endpoint')
        if report_endpoint:
            try:
                report_port = recalc_module.endpoint_port(str(report_endpoint))
                serve_port = recalc_module.endpoint_port(endpoint)
            except Exception:
                problems.append(f'报告缓存中的重算端点非法：{report_endpoint!r}')
            else:
                if report_port != serve_port:
                    problems.append(f'报告端点端口 {report_port} 与 CLI/serve 端口 {serve_port} 不一致')
        bindings = metadata.get('bindings') or {}
        if bindings:
            if str(bindings.get('case_id')) != str(case.get('case_id')):
                problems.append('报告页面绑定 case_id 与案例不一致')
            if str(bindings.get('model_id')) != model_id:
                problems.append('报告页面绑定 model_id 与案例模型不一致')
            binding_endpoint = bindings.get('recompute_endpoint')
            if binding_endpoint:
                try:
                    binding_port = recalc_module.endpoint_port(str(binding_endpoint))
                    serve_port = recalc_module.endpoint_port(endpoint)
                except Exception:
                    problems.append(f'报告页面绑定的重算端点非法：{binding_endpoint!r}')
                if binding_port != serve_port:
                    problems.append(f'报告页面绑定端口 {binding_port} 与 CLI/serve 端口 {serve_port} 不一致')
                    return problems
        return problems
    warn(f'报告缺少 {_REPORT_CAPABILITIES_FILE}：无法校验端点/能力元数据（建议先 le.ps1 run 生成）')
    return problems

def _json_pointer(document: 'Any', pointer: 'str') -> 'Any':
    node = document
    for raw in [part for part in str(pointer).split('/') if part]:
        part = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node

def _canonical(value: 'Any') -> 'str':
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def _rel_error(actual: 'float', expected: 'float') -> 'float':
    if expected == 0:
        return abs(actual - expected)
    return abs(actual - expected) / abs(expected)

def _check_module(kind: 'str', module_dir: 'Path') -> 'dict[str, Any]':
    result = run_child([_python(), '-c', f'import {kind}'], pythonpath=[module_dir], capture=True, echo=False)
    ok = result.returncode == 0
    detail = '可导入' if ok else (result.stderr or result.stdout or '').strip().splitlines()[-1:]
    if isinstance(detail, str):
        return {'name': f'模块导入 {kind}', 'status': 'PASS' if ok else 'FAIL', 'detail': detail}
    return {'name': None, 'status': None, 'detail': ' '.join(detail)}

def _dataset_checks(pointer: 'Path') -> 'list[dict[str, Any]]':
    checks = []
    if not pointer.is_file():
        return [{'name': 'dataset 指针', 'status': 'FAIL', 'detail': f'缺少 {relative(pointer, ROOT)}'}]
    config = load_json(pointer, what='dataset pointer')
    database = resolve(ROOT, config.get('database') or '')
    manifest_path = resolve(ROOT, config.get('manifest') or '')
    checks.append({'name': 'dataset 指针', 'status': 'PASS', 'detail': f'{config.get('dataset_id')} → {relative(database, ROOT)}'})
    checks.append({'name': 'dataset 数据库可读', 'status': 'PASS' if database.is_file() and os.access(database, os.R_OK) else 'FAIL', 'detail': relative(database, ROOT) if database.is_file() else f'缺少 {database}'})
    if database.is_file():
        actual = sha256_file(database)
        expected = str(config.get('file_sha256') or '')
        checks.append({'name': 'dataset 文件 sha256', 'status': 'PASS' if actual == expected else 'FAIL', 'detail': f'{actual[:16]}… == {expected[:16]}…' if actual == expected else f'实际 {actual[:16]}… 期望 {expected[:16]}…'})
        try:
            with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as con:
                count = con.execute('select count(*) from sqlite_master').fetchone()[0]
            checks.append({'name': 'dataset SQLite 可读', 'status': 'PASS' if count > 0 else 'FAIL', 'detail': f'{count} 个 schema 对象'})
        except sqlite3.Error as exc:
            checks.append({'name': 'dataset SQLite 可读', 'status': 'FAIL', 'detail': str(exc)})
    checks.append({'name': 'dataset manifest', 'status': 'PASS' if manifest_path.is_file() else 'FAIL', 'detail': relative(manifest_path, ROOT) if manifest_path.is_file() else '缺少 manifest'})
    return checks

def _git_info() -> 'dict[str, Any]':
    config = load_config()
    candidates = [os.environ.get('LE_GIT')] if os.environ.get('LE_GIT') else []
    candidates += list((config.get('git') or {}).get('candidates') or ['git'])
    for candidate in candidates:
        if candidate:
            executable = candidate
            if not Path(executable).is_absolute():
                import shutil
                found = shutil.which(executable)
                if found:
                    executable = found
            if Path(executable).is_file():
                result = run_child([executable, '--version'], capture=True, echo=False)
                if result.returncode == 0:
                    return {'available': True, 'executable': executable, 'version': result.stdout.strip()}
    return {'available': False, 'executable': None, 'version': None}

def cmd_doctor(args: 'argparse.Namespace') -> 'int':
    heading('le doctor — 环境与数据自检')
    checks = []
    version = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    version_ok = sys.version_info >= (3, 12)
    checks.append({'name': 'Python 解释器', 'status': 'PASS' if version_ok else 'FAIL', 'detail': f'{sys.executable}（{version}，要求 ≥3.12）'})
    config_path = ROOT / 'config' / 'project.json'
    checks.append({'name': '配置模板', 'status': 'PASS' if config_path.is_file() else 'FAIL', 'detail': relative(config_path, ROOT) if config_path.is_file() else '缺少 config/project.json'})
    checks.append({'name': '项目根目录', 'status': 'PASS', 'detail': str(ROOT)})
    for kind, module_dir in MODULE_PATHS.items():
        checks.append(_check_module(kind, module_dir))
    try:
        config = load_config()
        pointer = resolve(ROOT, (config.get('dataset') or {}).get('pointer') or 'data/current.json')
    except PipelineError:
        pointer = ROOT / 'data' / 'current.json'
    checks.extend(_dataset_checks(pointer))
    explicit_case = getattr(args, 'case', None)
    case = None
    if explicit_case:
        try:
            case_path_value = _default_case(args)
            case = load_case(case_path_value, root=ROOT)
            checks.append({'name': '案例配置', 'status': 'PASS', 'detail': relative(case_path_value, ROOT)})
            checks.extend(_case_checks(case, include_expected=True))
        except PipelineError as exc:
            checks.append({'name': '案例配置', 'status': 'FAIL', 'detail': str(exc)})
    else:
        checks.append({'name': '案例配置', 'status': 'INFO',
                       'detail': '未指定 --case；doctor 只检查共享环境，不选默认案例/全局 latest'})
    git = _git_info()
    checks.append({'name': 'Git（仅版本信息）', 'status': 'INFO' if not git['available'] else 'PASS', 'detail': git['version'] or '未在 PATH/LE_GIT 找到；不影响计算、测试与打包（manifest 记为 unknown）'})
    for tool, path in ('Node', ROOT / 'tools' / 'node'), ('JDK 21', ROOT / 'tools' / 'jdk-21'), ('Ghidra', ROOT / 'tools' / 'ghidra_12.1.4_PUBLIC'):
        checks.append({'name': f'{tool}（可选研究工具）', 'status': 'INFO' if not path.exists() else 'PASS', 'detail': '未随包分发；普通计算/测试不需要' if not path.exists() else '已存在'})
    doctor_case = case
    checks.extend(_pipeline_dependency_checks(doctor_case))
    heading('检查结果')
    print_checks(checks)
    ok = _status_ok(checks)
    print()
    if ok:
        print('doctor：PASS（所有运行必需项可用）')
        return EXIT_OK
    print('doctor：FAIL（存在阻塞运行的问题，见上方 FAIL 项）', file=sys.stderr)
    return EXIT_FAIL

def _case_checks(case: 'dict[str, Any]', *, include_expected: 'bool'=True) -> 'list[dict[str, Any]]':
    '''Structural/binding checks for one case (no computation).'''

    checks = []
    def add(name: 'str', ok: 'bool', detail: 'str'='', status: 'str | None'=None) -> 'None':
        checks.append({'name': name, 'status': status or ('PASS' if ok else 'FAIL'), 'detail': detail})

    dataset_pointer = case_path(case, 'dataset')
    if not dataset_pointer.is_file():
        add('dataset 指针', False, f'缺少 {relative(dataset_pointer, ROOT)}')
    else:
        current = load_json(dataset_pointer, what='dataset pointer')
        expected = case.get('dataset_expected') or {}
        add('dataset id', current.get('dataset_id') == expected.get('dataset_id'), f'{current.get('dataset_id')} / 期望 {expected.get('dataset_id')}')
        add('dataset 逻辑指纹', current.get('logical_content_sha256') == expected.get('logical_content_sha256'), str(current.get('logical_content_sha256'))[:16] + '…')
        database = resolve(ROOT, current.get('database') or '')
        add('dataset 数据库', database.is_file() and os.access(database, os.R_OK), relative(database, ROOT))
        if database.is_file():
            add('dataset 文件 sha256', sha256_file(database) == current.get('file_sha256'), f'{sha256_file(database)[:16]}…')
    affix_db = case_path(case, 'affix_db')
    ok = affix_db.is_file() and os.access(affix_db, os.R_OK)
    detail = relative(affix_db, ROOT)
    if ok:
        try:
            with sqlite3.connect(f'file:{affix_db}?mode=ro', uri=True) as con:
                count = con.execute('select count(*) from sqlite_master').fetchone()[0]
            ok = count > 0
            detail += f'（{count} 个 schema 对象）'
        except sqlite3.Error as exc:
            ok = False
            detail = str(exc)
    add('affix DB', ok, detail)
    ruleset_path = case_path(case, 'ruleset.path')
    ruleset_sha = str((case.get('ruleset') or {}).get('sha256') or '')
    add('ruleset 文件', ruleset_path.is_file(), relative(ruleset_path, ROOT))
    if ruleset_path.is_file():
        actual = sha256_file(ruleset_path)
        add('ruleset sha256', actual == ruleset_sha, f'{actual[:16]}… / 期望 {ruleset_sha[:16]}…')
    registry_path = case_path(case, 'conditional_registry.path')
    registry_sha = str((case.get('conditional_registry') or {}).get('sha256') or '')
    add('条件效果注册表', registry_path.is_file(), relative(registry_path, ROOT))
    if registry_path.is_file():
        actual = sha256_file(registry_path)
        add('注册表 sha256', actual == registry_sha, f'{actual[:16]}… / 期望 {registry_sha[:16]}…')
    build = case.get('build') or {}
    build_dir = case_path(case, 'build.build_dir')
    revision_id = str(build.get('revision_id') or '')
    build_json = build_dir / 'revisions' / revision_id / 'build.json'
    add('构筑 build.json', build_json.is_file(), relative(build_json, ROOT))
    if build_json.is_file():
        spec_sha = sha256_file(build_json)
        expected_sha = str(build.get('build_spec_sha256') or '')
        add('构筑 BuildSpec sha256', spec_sha == expected_sha, f'{spec_sha[:16]}… / 期望 {expected_sha[:16]}…')
        build_doc = load_json(build_json, what='build revision')
        meta = build_doc.get('meta') or {}
        add('构筑 build_id', meta.get('build_id') == build.get('build_id'), f'{meta.get('build_id')} / 期望 {build.get('build_id')}')
        add('构筑 content_sha256', (meta.get('revision') or {}).get('content_sha256') == build.get('content_sha256'), str((meta.get('revision') or {}).get('content_sha256'))[:16] + '…')
        add('构筑 revision 目录', (build_dir / 'revisions' / revision_id).is_dir(), revision_id)
        current_pointer = build_dir / 'current.json'
        if current_pointer.is_file():
            current = load_json(current_pointer, what='build current pointer')
            add('构筑 current 指针', current.get('revision_id') == revision_id, f'{current.get('revision_id')} / 案例固定 {revision_id}（只报信息，不覆盖）', status='PASS' if current.get('revision_id') == revision_id else 'INFO')
    input_sha = case.get('input_sha256') or {}
    for group in ('inputs', 'source'):
        block = case.get(group) or {}
        for key, value in block.items():
            if key == 'comment':
                continue
            if isinstance(value, str):
                path = resolve(ROOT, value)
                if not path.is_file():
                    add(f'输入文件 {key}', False, f'缺少 {relative(path, ROOT)}')
                    continue
                expected = str(input_sha.get(key) or '')
                actual = sha256_file(path)
                add(f'输入指纹 {key}', bool(expected) and actual == expected, f'{actual[:16]}… / 期望 {expected[:16]}…' if expected else '案例未声明 sha256')
    try:
        snapshot_path = case_path(case, 'inputs.snapshot')
        calc_input_path = case_path(case, 'inputs.calculation_input')
        if snapshot_path.is_file() and calc_input_path.is_file():
            snapshot = load_json(snapshot_path, what='snapshot')
            calc = load_json(calc_input_path, what='calculation input')
            meta = snapshot.get('build_meta') or {}
            snap_build = meta.get('build') or {}
            snap_calc = meta.get('calc_input') or {}
            snap_dataset = meta.get('dataset') or {}
            add('快照 build_id', snap_build.get('build_id') == build.get('build_id'), f'{snap_build.get('build_id')}')
            add('快照 revision_id', snap_build.get('revision_id') == revision_id, f'{snap_build.get('revision_id')}')
            add('快照 content_sha256', snap_build.get('content_sha256') == build.get('content_sha256'), str(snap_build.get('content_sha256'))[:16] + '…')
            add('快照 dataset_id', snap_dataset.get('dataset_id') == (case.get('dataset_expected') or {}).get('dataset_id'), str(snap_dataset.get('dataset_id')))
            add('快照 calc_input sha256', snap_calc.get('sha256') == sha256_file(calc_input_path), f'{str(snap_calc.get('sha256'))[:16]}… / 计算输入 {sha256_file(calc_input_path)[:16]}…')
            declared = case_counts(case)
            actual_counts = calculation_input_counts(calc)
            add('快照结构计数', all((actual_counts[key] == declared[key] for key in declared)), ', '.join((f'{key}={actual_counts[key]}' for key in sorted(declared))))
            add('快照 mod_count 一致', snap_calc.get('mod_count') == declared['mods'], f'{snap_calc.get('mod_count')} / 声明 {declared['mods']}')
    except PipelineError as exc:
        add('快照绑定', False, str(exc))
    try:
        corrections_path = case_path(case, 'inputs.corrections')
        corrections = load_json(corrections_path, what='corrections')
        add('人工补值文件 schema', corrections.get('schema') == 'le-case-corrections-v1', str(corrections.get('schema')))
        refraction = corrections.get('refraction') or {}
        selection_path = case_path(case, 'inputs.idol_refraction_selection')
        selection = load_json(selection_path, what='idol refraction selection')
        add('折射选择与人工补值一致', list(refraction.get('refracted_idols') or []) == list(selection.get('refracted_idols') or []) and float(refraction.get('altar_prefix_effect', -1)) == float(selection.get('altar_prefix_effect', -2)) and float(refraction.get('altar_suffix_effect', -1)) == float(selection.get('altar_suffix_effect', -2)), 'refracted_idols / altar 倍率')
        calc = load_json(calc_input_path, what='calculation input')
    except PipelineError as exc:
        add('人工补值文件', False, str(exc))
    try:
        mod_ids = {mod.get('mod_id') for mod in calc.get('mods') or []}
        target_ids = []
        for entry in corrections.get('corrections') or []:
            target_ids.extend(entry.get('mod_ids') or [])
            if entry.get('mod_id'):
                target_ids.append(entry['mod_id'])
            if entry.get('mod'):
                target_ids.append(entry['mod'].get('mod_id'))
        missing = sorted((mod_id for mod_id in target_ids if mod_id if mod_id not in mod_ids))
        add('人工补值目标 mod 存在', not missing, f'缺失 {missing}' if missing else f'{len(target_ids)} 个目标')
    except PipelineError as exc:
        add('人工补值文件', False, str(exc))
    try:
        scenario_path = case_path(case, 'inputs.scenario')
        scenario = load_json(scenario_path, what='conditional scenario')
        binding = scenario.get('binding') or {}
        add('场景 registry_id', binding.get('registry_id') == (case.get('conditional_registry') or {}).get('id'), str(binding.get('registry_id')))
        add('场景 registry sha256', binding.get('registry_sha256') == (case.get('conditional_registry') or {}).get('sha256'), str(binding.get('registry_sha256'))[:16] + '…')
        add('场景 ruleset sha256', binding.get('ruleset_sha256') == (case.get('ruleset') or {}).get('sha256'), str(binding.get('ruleset_sha256'))[:16] + '…')
        add('场景 build content sha256', binding.get('build_content_sha256') == build.get('content_sha256'), str(binding.get('build_content_sha256'))[:16] + '…')
        add('场景 calc_input sha256', binding.get('calc_input_sha256') == sha256_file(calc_input_path), str(binding.get('calc_input_sha256'))[:16] + '…')
        add('场景 dataset_id', binding.get('dataset_id') == (case.get('dataset_expected') or {}).get('dataset_id'), str(binding.get('dataset_id')))
        add('场景预设', scenario.get('preset_id') == 'build_base', str(scenario.get('preset_id')))
    except PipelineError as exc:
        add('条件场景', False, str(exc))
    if include_expected:
        try:
            expected_path = case_path(case, 'expected')
            expected = load_json(expected_path, what='expected summary')
            add('预期摘要 schema', expected.get('schema') == 'le-case-expected-v1', str(expected.get('schema')))
            add('预期摘要 case_id', expected.get('case_id') == case.get('case_id'), str(expected.get('case_id')))
            add('预期摘要容差', float(expected.get('tolerance_rel') or 0) > 0, f'tolerance_rel={expected.get('tolerance_rel')}')
            add('预期摘要检查项', bool(expected.get('checks')), f'{len(expected.get('checks') or [])} 项')
        except PipelineError as exc:
            add('预期摘要', False, str(exc))
        return checks
    return checks

def _hit_case_checks(case: 'dict[str, Any]') -> 'list[dict[str, Any]]':
    '''Structural/binding checks for a ``case.model == "hit"`` case (no computation).

    Deliberately separate from :func:`_case_checks` (the Black Hole contract):
    the hit model has no conditional registry / refraction selection and its
    ``snapshot_expectations`` block uses hit-specific keys.
    '''

    checks = []
    snapshot_expectations = case.get('snapshot_expectations') or {}
    def add(name: 'str', ok: 'bool', detail: 'str'='', status: 'str | None'=None) -> 'None':
        checks.append({'name': name, 'status': status or ('PASS' if ok else 'FAIL'), 'detail': detail})

    add('案例 schema', case.get('schema') == 'le-case-v1', str(case.get('schema')))
    add('案例 model', _case_model(case) == 'hit', str(case.get('model')))
    main_skill = case.get('main_skill') or {}
    add('主技能 ability_id', bool(main_skill.get('ability_id')), str(main_skill.get('ability_id')))
    add('输出根目录', bool((case.get('output') or {}).get('root')), str((case.get('output') or {}).get('root')))
    try:
        dataset_pointer = case_path(case, 'dataset')
        if not dataset_pointer.is_file():
            add('dataset 指针', False, f'缺少 {relative(dataset_pointer, ROOT)}')
        else:
            current = load_json(dataset_pointer, what='dataset pointer')
            expected = case.get('dataset_expected') or {}
            add('dataset id', current.get('dataset_id') == expected.get('dataset_id'), f'{current.get('dataset_id')} / 期望 {expected.get('dataset_id')}')
            add('dataset 逻辑指纹', current.get('logical_content_sha256') == expected.get('logical_content_sha256'), str(current.get('logical_content_sha256'))[:16] + '…')
            database = resolve(ROOT, current.get('database') or '')
            add('dataset 数据库', database.is_file() and os.access(database, os.R_OK), relative(database, ROOT))
            if database.is_file():
                actual = sha256_file(database)
                add('dataset 文件 sha256', actual == current.get('file_sha256'), f'{actual[:16]}… / 期望 {str(current.get('file_sha256'))[:16]}…')
    except PipelineError as exc:
        add('dataset 指针', False, str(exc))
    build = case.get('build') or {}
    try:
        build_dir = case_path(case, 'build.build_dir')
        revision_id = str(build.get('revision_id') or '')
        build_json = build_dir / 'revisions' / revision_id / 'build.json'
        add('构筑 revision 目录', (build_dir / 'revisions' / revision_id).is_dir(), revision_id)
        add('构筑 build.json', build_json.is_file(), relative(build_json, ROOT))
        if build_json.is_file():
            spec_sha = sha256_file(build_json)
            expected_sha = str(build.get('build_spec_sha256') or '')
            if not expected_sha or expected_sha == 'PLACEHOLDER':
                add('构筑 BuildSpec sha256', True, f'{spec_sha[:16]}…（案例未声明，记录实际值）', status='INFO')
            else:
                add('构筑 BuildSpec sha256', spec_sha == expected_sha, f'{spec_sha[:16]}… / 期望 {expected_sha[:16]}…')
            build_doc = load_json(build_json, what='build revision')
            meta = build_doc.get('meta') or {}
            add('构筑 build_id', meta.get('build_id') == build.get('build_id'), f'{meta.get('build_id')} / 期望 {build.get('build_id')}')
            add('构筑 content_sha256', (meta.get('revision') or {}).get('content_sha256') == build.get('content_sha256'), str((meta.get('revision') or {}).get('content_sha256'))[:16] + '…')
    except PipelineError as exc:
        add('构筑 revision', False, str(exc))
    calc_input = None
    calc_document = None
    try:
        calc_input = case_path(case, 'inputs.calculation_input')
        add('计算输入文件', calc_input.is_file(), relative(calc_input, ROOT))
        if calc_input.is_file():
            calc_document = load_json(calc_input, what='calculation input')
            counts = calculation_input_counts(calc_document)
            declared = {key: int(snapshot_expectations[key]) for key in ('items', 'mods', 'passive_nodes', 'passive_points', 'specializations')
                 if key in snapshot_expectations}
            add('计算输入结构计数', all((counts[key] == value for key, value in declared.items())), ', '.join((f'{key}={counts[key]}' + ('' if counts[key] == declared[key] else f'(!={declared[key]})') for key in sorted(declared))))
            specializations = (calc_document.get('skills') or {}).get('specializations') or []
            if 'fr11mv_points_total' in snapshot_expectations:
                fr_points = 0
                for spec in specializations:
                    tree = str((spec.get('tree') or {}).get('entity_id') or '')
                    if not tree.endswith(':fr11mv'):
                        continue
                    fr_points = int(spec.get('points_total') or 0)
                add('Flame Reave 技能点', fr_points == int(snapshot_expectations['fr11mv_points_total']), f'{fr_points} / 声明 {snapshot_expectations['fr11mv_points_total']}')
            if 'skill_tree_nodes_total' in snapshot_expectations:
                node_total = sum((len(spec.get('nodes') or []) for spec in specializations))
                add('技能树节点总数', node_total == int(snapshot_expectations['skill_tree_nodes_total']), f'{node_total} / 声明 {snapshot_expectations['skill_tree_nodes_total']}')
            actual_sha = sha256_file(calc_input)
            declared_sha = str((case.get('input_sha256') or {}).get('calculation_input') or '')
            if declared_sha:
                add('计算输入 sha256', actual_sha == declared_sha, f'{actual_sha[:16]}… / 期望 {declared_sha[:16]}…')
            else:
                add('计算输入 sha256', True, f'{actual_sha[:16]}…（案例未声明）', status='INFO')
    except PipelineError as exc:
        add('计算输入文件', False, str(exc))
    ruleset_path = None
    try:
        ruleset_path = case_path(case, 'ruleset.path')
        add('ruleset 文件', ruleset_path.is_file(), relative(ruleset_path, ROOT))
        if ruleset_path.is_file():
            ruleset_doc = load_json(ruleset_path, what='hit ruleset')
            ruleset_block = case.get('ruleset') or {}
            add('ruleset schema', ruleset_doc.get('schema') == 'le-ruleset-hit-v1', str(ruleset_doc.get('schema')))
            add('ruleset id', ruleset_doc.get('ruleset_id') == ruleset_block.get('id'), f'{ruleset_doc.get('ruleset_id')} / 期望 {ruleset_block.get('id')}')
            actual = sha256_file(ruleset_path)
            declared = str(ruleset_block.get('sha256') or '')
            if declared:
                add('ruleset sha256', actual == declared, f'{actual[:16]}… / 期望 {declared[:16]}…')
            else:
                add('ruleset sha256', True, f'{actual[:16]}…（案例未声明）', status='INFO')
    except PipelineError as exc:
        add('ruleset 文件', False, str(exc))
    try:
        snapshot_path = case_path(case, 'inputs.snapshot')
        if not snapshot_path.is_file():
            add('hit 快照', True, f'尚未生成：{relative(snapshot_path, ROOT)}（run 时自动生成）', status='SKIP')
        else:
            snapshot = load_json(snapshot_path, what='hit snapshot')
            meta = snapshot.get('build_meta') or {}
            snap_build = meta.get('build') or {}
            snap_calc = meta.get('calc_input') or {}
            snap_dataset = meta.get('dataset') or {}
            add('快照 build_id', snap_build.get('build_id') == build.get('build_id'), str(snap_build.get('build_id')))
            add('快照 revision_id', snap_build.get('revision_id') == build.get('revision_id'), str(snap_build.get('revision_id')))
            add('快照 content_sha256', snap_build.get('content_sha256') == build.get('content_sha256'), str(snap_build.get('content_sha256'))[:16] + '…')
            add('快照 dataset_id', snap_dataset.get('dataset_id') == (case.get('dataset_expected') or {}).get('dataset_id'), str(snap_dataset.get('dataset_id')))
            if calc_input is not None and calc_input.is_file():
                calc_sha = sha256_file(calc_input)
                add('快照 calc_input sha256', snap_calc.get('sha256') == calc_sha, f'{str(snap_calc.get('sha256'))[:16]}… / 计算输入 {calc_sha[:16]}…')
            derived = snapshot.get('derived') or {}
            add('快照 model', derived.get('model') == 'hit', str(derived.get('model')))
            contributions = snapshot.get('contributions') or []
            add('快照 contributions', bool(contributions), f'{len(contributions)} 条')
    except PipelineError as exc:
        add('hit 快照', False, str(exc))
    try:
        corrections_path = case_path(case, 'inputs.corrections')
        corrections = load_json(corrections_path, what='corrections')
        add('人工补值 schema', corrections.get('schema') == 'le-case-corrections-v1', str(corrections.get('schema')))
    except PipelineError as exc:
        add('人工补值文件', False, str(exc))
    try:
        refraction = optional_case_path(case, 'inputs.idol_refraction_selection')
        add('折射选择', True, relative(refraction, ROOT) if refraction is not None else '未声明（hit 案例可无）', status='INFO')
    except PipelineError as exc:
        add('折射选择', False, str(exc))
    try:
        scenario_path = optional_case_path(case, 'inputs.scenario')
        if scenario_path is None:
            add('hit 场景', True, '未声明（baseline；条件由 ruleset.effects 定义）', status='INFO')
        elif not scenario_path.is_file():
            add('hit 场景', True, f'未生成：{relative(scenario_path, ROOT)}（可选）', status='SKIP')
        else:
            add('hit 场景', True, relative(scenario_path, ROOT))
    except PipelineError as exc:
        add('hit 场景', False, str(exc))
    try:
        expected_path = optional_case_path(case, 'expected')
        if expected_path is None:
            add('预期摘要', True, '案例未声明', status='INFO')
            return checks
    except PipelineError as exc:
        add('预期摘要', False, str(exc))
        return checks
    try:
        if not expected_path.is_file():
            add('预期摘要', True, '尚未生成（Lead 后续补）', status='SKIP')
            return checks
    except PipelineError as exc:
        add('预期摘要', False, str(exc))
        return checks
    try:
        expected = load_json(expected_path, what='expected summary')
        add('预期摘要 schema', expected.get('schema') == 'le-case-expected-v1', str(expected.get('schema')))
        add('预期摘要 case_id', expected.get('case_id') == case.get('case_id'), str(expected.get('case_id')))
    except PipelineError as exc:
        add('预期摘要', False, str(exc))
    return checks

_PREPARE_STAGES = ('freeze_input', 'parse_input', 'normalize_build', 'export_calc_input', 'apply_affix_ranges', 'resolve_skills', 'prepare_snapshot', 'min_validate')


def _has_raw_prepare_input(case: 'dict[str, Any]', args: 'argparse.Namespace') -> 'bool':
    if any(getattr(args, key, None) for key in ('save', 'planner', 'stash')):
        return True
    source = case.get('source') if isinstance(case.get('source'), dict) else {}
    return bool(source.get('character_save') or source.get('planner_build_response'))


def _prepare_stages_for(case: 'dict[str, Any]', args: 'argparse.Namespace') -> 'tuple[str, ...]':
    """按计算路由选择准备阶段；统一分支跳过旧 prepare_snapshot，改 compile_plan。"""
    route = _route_for_case(case)
    engine = route.get('engine')
    has_raw = _has_raw_prepare_input(case, args)
    if engine == 'unified_dps' and route.get('status') == 'ready':
        if has_raw:
            return tuple(
                'compile_plan' if stage_id == 'prepare_snapshot' else stage_id
                for stage_id in _PREPARE_STAGES
            )
        # 已冻结输入：只做知识解析 + 计划编译/门禁，不重跑存档导入。
        return ('resolve_skills', 'compile_plan', 'min_validate')
    if engine in ('needs_research', 'needs_input') and not has_raw:
        return ()
    return _PREPARE_STAGES
_READINESS_ORDER = {'ready': 0, 'partial': 1, 'needs_research': 2, 'needs_input': 3}

def _workflow_stage():
    '''Lazy import of the stage/statestore layer (single implementation with CLI).'''

    calc_src = str(MODULE_PATHS['le_calc'])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import stage
    return stage

def _workflow_statestore():
    calc_src = str(MODULE_PATHS['le_calc'])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import statestore
    return statestore

def _pipeline_modules():
    '''Lazy import of the read-only route/dependency description library (kh-fr).'''

    pipeline_dir = ROOT / 'scripts'
    if not pipeline_dir.is_dir():
        return (None, None)
    added = False
    if str(pipeline_dir) not in sys.path:
        sys.path.insert(0, str(pipeline_dir))
        added = True
    try:
        from le_pipeline import routes
        try:
            from le_pipeline import dependencies
        except Exception:
            dependencies = None
    finally:
        if added:
            try:
                sys.path.remove(str(pipeline_dir))
            except ValueError:
                pass
    return routes, dependencies

def _pipeline_dependency_checks(case: 'dict[str, Any] | None'=None) -> 'list[dict[str, Any]]':
    '''Optional dependency description library delivered by the kh-fr workstream.

    Read-only integration: when ``scripts/le_pipeline`` is not present we report
    INFO (never fail doctor or silently switch parse routes); when present we list
    the resolved route and, if ``le_pipeline.dependencies`` exposes a check entry
    point, surface its missing dependencies.  The library owns the dependency
    definitions — this module never re-implements them.
    '''

    checks = []
    pipeline_dir = ROOT / 'scripts' / 'le_pipeline'
    if not pipeline_dir.is_dir():
        checks.append({'name': '解析依赖描述库（可选）', 'status': 'INFO', 'detail': 'scripts/le_pipeline 未交付；prepare 仍走已支持的两条输入路线'})
        return checks
    routes, dependencies = _pipeline_modules()
    if routes is None:
        checks.append({'name': '解析依赖描述库', 'status': 'INFO', 'detail': 'scripts/le_pipeline 存在但无法导入；prepare 不做依赖门禁'})
        return checks
    checks.append({'name': '解析依赖描述库', 'status': 'PASS', 'detail': 'le_pipeline.routes 可导入：' + ', '.join((route['route_id'] for route in routes.list_routes()))})
    if case is not None:
        try:
            detail = routes.resolve_route_detail(case)
        except Exception as exc:
            checks.append({'name': '输入路线判定', 'status': 'INFO', 'detail': f'路线判定失败：{exc}'})
        else:
            route_obj = detail.get('route')
            route = routes.route_to_dict(route_obj) if route_obj is not None else None
            route = route or {}
            checks.append({'name': '输入路线判定', 'status': 'PASS' if route else 'INFO', 'detail': f'{route.get('route_id')}（{route.get('label')}）' if route else f'未判定路线：{detail.get('reason') or detail}'})
    if dependencies is not None and case is not None:
        try:
            checker = getattr(dependencies, 'check_routes', None) or getattr(dependencies, 'check_route', None)
            report = checker(case, root=ROOT) if checker else None
        except TypeError:
            try:
                report = checker(case, root=ROOT) if checker else None
            except Exception as exc:
                report = None
                checks.append({'name': '解析依赖检查', 'status': 'INFO', 'detail': f'依赖检查不可用：{exc}'})
        except Exception as exc:
            report = None
            checks.append({'name': '解析依赖检查', 'status': 'INFO', 'detail': f'依赖检查不可用：{exc}'})
        if report is not None:
            missing = _dependency_report_missing(report)
            input_missing = [item for item in missing if _is_input_need(item)]
            detail = _dependency_report_detail(report)
            if input_missing and not report.get('ready'):
                detail = 'needs_input：' + detail
            hard_missing = [item for item in missing if not _is_input_need(item)]
            if hard_missing or input_missing and not report.get('ready'):
                status = 'FAIL' if hard_missing else 'INFO'
                checks.append({'name': '解析依赖检查', 'status': status, 'detail': detail})
                return checks
            checks.append({'name': '解析依赖检查', 'status': 'PASS', 'detail': _dependency_report_detail(report)})
    return checks

def _is_input_need(item: 'Any') -> 'bool':
    '''True when the missing dependency is a *prepare input* (not a missing tool).'''

    if isinstance(item, dict):
        ids = item.get('dependency_ids') or []
        if ids:
            if all((str(dep).startswith('input.') or str(dep).startswith('case.') for dep in ids)):
                return True
        return str(item.get('kind') or '') in ('local_config', 'input')
    text = str(item)
    return text.startswith('input.') or 'required' in text or text.startswith('case.')

def _dependency_report_missing(report: 'Any') -> 'list[Any]':
    '''Extract missing inputs/dependencies from ``le_pipeline.dependencies.check_routes``.

    The dependency library owns the report shape (``ready``/``needs_input``/
    ``dependencies[]``); this adapter only reads it, never re-derives it.
    '''

    if isinstance(report, dict):
        needs = report.get('needs_input')
        if isinstance(needs, list) and needs:
            return list(needs)
        for key in ('missing', 'missing_dependencies', 'errors'):
            value = report.get(key)
            if not isinstance(value, list):
                continue
            if value:
                return list(value)
        if report.get('ready') is False:
            return [str(report.get('route_reason') or 'route_not_ready')]
    return []

def _dependency_item_label(item: 'Any') -> 'str':
    if isinstance(item, dict):
        for key in ('field', 'id', 'name', 'code', 'reason'):
            if item.get(key):
                return str(item[key])
        return str(item)
    return str(item)

def _dependency_report_detail(report: 'Any') -> 'str':
    if isinstance(report, dict):
        missing = _dependency_report_missing(report)
        if missing:
            return '缺少输入/依赖：' + ', '.join((_dependency_item_label(item) for item in missing[:6]))
        dependencies = report.get('dependencies') or []
        if report.get('ready'):
            return f'依赖齐全（{len(dependencies)} 项），路线 {report.get('route_id') or 'unknown'}'
        return str(report.get('notes') or report.get('route_reason') or '依赖状态未知')
    return str(report)

def _prepare_route(case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the input route via ``le_pipeline.routes`` (read-only)."""
    routes, dependencies = _pipeline_modules()
    if routes is None:
        return {"route": None, "source_kind": None, "missing": [], "note": "le_pipeline unavailable"}
    source_kind = None
    if getattr(args, "save", None):
        source_kind = "character_save"
    elif getattr(args, "planner", None):
        source_kind = "planner_payload"
    try:
        detail = routes.resolve_route_detail(case, source_kind=source_kind)
    except Exception as exc:  # noqa: BLE001 - route resolution must not break prepare
        return {"route": None, "source_kind": source_kind, "missing": [], "note": f"route error: {exc}"}
    route_obj = detail.get("route")
    route = routes.route_to_dict(route_obj) if route_obj is not None else None
    route = route or {}
    missing: list[str] = []
    report = None
    if dependencies is not None:
        checker = getattr(dependencies, "check_routes", None) or getattr(dependencies, "check_route", None)
        if checker is not None:
            try:
                report = checker(
                    case,
                    root=ROOT,
                    source_kind=source_kind,
                    main_skill=getattr(args, "main_skill", None),
                )
            except TypeError:
                try:
                    report = checker(case, root=ROOT)
                except Exception as exc:  # noqa: BLE001 - optional dependency library
                    report = {"status": "unknown", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - optional dependency library
                report = {"status": "unknown", "error": str(exc)}
            try:
                missing = [
                    _dependency_item_label(item)
                    for item in _dependency_report_missing(report)
                ]
            except Exception:  # noqa: BLE001 - best-effort view only
                missing = []
    return {
        "route": route,
        "route_id": route.get("route_id"),
        "source_kind": source_kind,
        "missing": missing,
        "report": report,
        "note": "",
    }


def _case_main_skill(case: 'dict[str, Any]') -> 'str | None':
    '''Confirmed main skill declared by the case (``case.main_skill.ability_id``).'''

    block = case.get('main_skill') or {}
    if isinstance(block, dict):
        if str(block.get('ability_id') or '').strip():
            return str(block['ability_id']).strip()

def _resolve_main_skill(case: 'dict[str, Any]', args: 'argparse.Namespace') -> 'tuple[str | None, str | None]':
    '''Resolve the confirmed main skill: explicit ``--main-skill`` > case declaration.

    Heuristic inference from the calculation input is **never** treated as a
    confirmed value (review D1): it is only used as a hint in the needs_input
    message when neither source provides one.
    '''

    requested = str(getattr(args, 'main_skill', None) or '').strip()
    if requested:
        return requested, 'cli'
    declared = _case_main_skill(case)
    if declared:
        return declared, 'case'
    return (None, None)

def _infer_main_skill(case: 'dict[str, Any]', model: 'str') -> 'str | None':
    """Best-effort main-skill inference from the case's calculation input trees."""

    calc = case_path(case, 'inputs.calculation_input')
    if not calc.is_file():
        return
    try:
        document = load_json(calc, what='calculation input')
    except PipelineError:
        return
    trees = [str((spec.get('tree') or {}).get('name') or '') for spec in (document.get('skills') or {}).get('specializations') or []]
    trees = [name for name in trees if name]
    if not trees:
        return
    haystack = ' '.join((str(case.get(key) or '') for key in ('case_id', 'title', 'description'))).casefold()
    def tokens(name: 'str') -> 'list[str]':
        return [part for part in re.split('[^a-z0-9]+', name.casefold()) if len(part) > 2]

    scored = [(sum((1 for token in tokens(name) if token in haystack)), name) for name in trees]
    best = max((score for score, _ in scored))
    winners = [name for score, name in scored if score == best if best > 0]
    if len(winners) != 1:
        return
    name = winners[0]
    for spec in (document.get('skills') or {}).get('specializations') or []:
        if str((spec.get('tree') or {}).get('name') or '') != name:
            continue
        for source in ('tree', 'owner_skill'):
            value = str((spec.get(source) or {}).get('entity_id') or '')
            if ':' in value:
                return value.rsplit(':', 1)[-1]

def _prepare_context(run_dir: 'Path', *, captures: 'list[dict[str, Any]]'):
    from le_calc.workflow import contract as workflow_contract
    def run_child_stage(cmd, *, pythonpath=(), cwd=None):
        completed = run_child(cmd, pythonpath=pythonpath, cwd=cwd, capture=True, echo=False)
        captures.append({'command': subprocess.list2cmdline([str(part) for part in cmd]), 'returncode': completed.returncode})
        return {'returncode': completed.returncode, 'stdout': completed.stdout or '', 'stderr': completed.stderr or ''}

    return workflow_contract.StageContext(repo_root=ROOT, output_root=run_dir, adapters={'run_child': run_child_stage})

def _workflow_stage_cache():
    '''Lazy import of the P4 stage cache module.'''

    calc_src = str(MODULE_PATHS['le_calc'])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import stage_cache
    return stage_cache

def _prepare_stage_cache(args: 'argparse.Namespace'):
    stage_cache = _workflow_stage_cache()
    return stage_cache.StageCache(ROOT, cache_root=getattr(args, 'cache_root', None), enabled=not getattr(args, 'no_cache', False))

def _prepare_input_refs(case: 'dict[str, Any]', case_path: 'Path') -> 'list[dict[str, Any]]':
    '''Declared preparation input files (repository-relative artifact refs).'''

    candidates = {'case': case_path}
    for group in ('inputs', 'source'):
        block = case.get(group)
        if isinstance(block, dict):
            for key, value in block.items():
                if not isinstance(value, str):
                    continue
                if value.strip():
                    candidates[f'{group}.{key}'] = resolve(ROOT, value)
    for key in ('ruleset', 'conditional_registry', 'build', 'dataset'):
        block = case.get(key)
        path_value = block.get('path') if isinstance(block, dict) else block
        if not isinstance(path_value, str):
            continue
        if path_value.strip():
            candidates[key] = resolve(ROOT, path_value)
    if isinstance(case.get('affix_db'), str):
        candidates['affix_db'] = resolve(ROOT, case['affix_db'])
    refs = []
    seen = set()
    for role, path in sorted(candidates.items()):
        try:
            rel = relative(path, ROOT)
        except PipelineError:
            continue
        if rel in seen or not path.is_file():
            continue
        seen.add(rel)
        refs.append({'artifact_id': role, 'path': rel, 'asset_id': None, 'schema_version': None, 'sha256': sha256_file(path)})
    return refs

def _prepare_override_input_refs(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Actual CLI override inputs (save/planner/stash) as cache input refs.

    The cache key previously saw only the path string, so replacing the file at
    the same path reused stale results (P4-F2).  Always include the actual content
    sha; missing/directory states use sentinel sha values so create/delete/
    replace transitions each change the fingerprint.  Only freeze/parse consume
    these inputs; downstream stages cascade through their dependency keys.
    """
    refs: list[dict[str, Any]] = []
    for key in ("save", "planner", "stash"):
        value = params.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            path = ROOT / path
        try:
            rel = relative(path, ROOT)
        except PipelineError:
            continue
        if path.is_file():
            sha = sha256_file(path)
            exists = True
        elif path.is_dir():
            sha = "directory"
            exists = True
        else:
            sha = "missing"
            exists = False
        refs.append(
            {
                "artifact_id": f"override.{key}",
                "path": rel,
                "asset_id": None,
                "schema_version": None,
                "sha256": sha,
                "exists": exists,
            }
        )
    return refs


def _prepare_stage_input_refs(
    stage_id: str,
    base_refs: list[dict[str, Any]],
    override_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Only freeze/parse consume CLI override inputs; later stages cascade by deps."""
    if stage_id in ("freeze_input", "parse_input") and override_refs:
        return [dict(ref) for ref in base_refs] + [dict(ref) for ref in override_refs]
    return [dict(ref) for ref in base_refs]


def _prepare_params(args: 'argparse.Namespace') -> 'dict[str, Any]':
    params = {}
    for key in ('save', 'planner', 'stash'):
        value = getattr(args, key, None)
        if value:
            params[key] = str(resolve(ROOT, value))
    return params

def _prepare_stage_parameters(
    stage_id: 'str', params: 'dict[str, Any]', run_dir: 'Path | None' = None
) -> 'dict[str, Any]':
    """Only pass parameters that actually affect this stage (per-stage cascade).

    ``save``/``planner``/``stash`` are consumed by freeze/parse; ``main_skill``
    first affects ``resolve_skills``.  Filtering keeps unrelated upstream stages
    cache-warm when only a later stage's parameter changes.
    """

    filtered = {}
    if stage_id == 'compile_plan':
        if run_dir is not None:
            for candidate in ('steps/04-calculation-input.corrected.json', 'steps/03-calculation-input.affix-db.json', 'steps/02-calculation-input.json'):
                path = run_dir / candidate
                if path.is_file():
                    filtered['calc_input_ref'] = relative(path, ROOT)
                    filtered['calc_input_sha256'] = sha256_file(path)
                    break
        if params.get('main_skill'):
            filtered['main_skill'] = params['main_skill']
        return filtered
    index = _PREPARE_STAGES.index(stage_id) if stage_id in _PREPARE_STAGES else 0
    parse_index = _PREPARE_STAGES.index('parse_input')
    resolve_index = _PREPARE_STAGES.index('resolve_skills')
    if index <= parse_index:
        for key in ('save', 'planner', 'stash'):
            if params.get(key):
                filtered[key] = params[key]
    if index >= resolve_index and params.get('main_skill'):
        filtered['main_skill'] = params['main_skill']
    return filtered

def _prepare_request(stage_id: 'str', *, case: 'dict[str, Any]', run_id: 'str', params: 'dict[str, Any]', input_refs: 'list[dict[str, Any]]', dependencies: 'dict[str, str]', run_dir: 'Path | None' = None):
    from le_calc.workflow import contract as workflow_contract
    return workflow_contract.StageRequest(stage_id=stage_id, case_id=str(case.get('case_id')), run_id=run_id, input_refs=[dict(ref) for ref in input_refs], parameters={'case_ref': relative(case['_path'], ROOT), **_prepare_stage_parameters(stage_id, params, run_dir)}, dependency_fingerprints={'dependencies': dict(dependencies)})

def _recover_corrupt_state(statestore, store, *, reason: 'str') -> 'dict[str, Any]':
    '''Move an unreadable pipeline-state aside and let the pipeline rebuild.'''

    stamp = time.strftime('%Y%m%dT%H%M%S')
    backup = store.path.with_name(f'pipeline-state.corrupt-{stamp}.json')
    counter = 1
    while backup.exists():
        backup = store.path.with_name(f'pipeline-state.corrupt-{stamp}-{counter}.json')
        counter += 1
    recovered = {'backup': relative(backup, ROOT), 'reason': reason}
    try:
        store.path.replace(backup)
    except OSError as exc:
        warn(f'损坏的 pipeline-state 无法备份：{exc}')
        recovered['backup'] = None
    warn(f'pipeline-state 不可用（{reason}），已备份并按新 pipeline 重建：{relative(backup, ROOT)}')
    return recovered

def _clean_unreferenced_outputs(run_dir: 'Path', state) -> 'list[str]':
    '''Remove partial/unreferenced files before a resumed stage execution.

    A killed stage can leave files that are not referenced by any completed
    state entry; keeping them would make the rerun's output snapshot incomplete
    (files already on disk look "unchanged" and would be missing from the cache
    entry).  Only generated run files are touched; pipeline-state,
    prepare-context and corrupt-state backups are preserved.
    '''

    referenced = set()
    for entry in state.stages:
        for ref in entry.output_refs or []:
            path_value = str((ref or {}).get('path') or '')
            if path_value:
                referenced.add(path_value)
    removed = []
    if not run_dir.is_dir():
        return removed
    for path in sorted(run_dir.rglob('*'), reverse=True):
        if path.is_file():
            relative_path = relative(path, ROOT)
            name = path.name
            if relative_path in referenced:
                continue
            if name in ('pipeline-state.json', 'prepare-context.json'):
                continue
            if name.startswith('pipeline-state.corrupt-'):
                continue
            try:
                path.unlink()
                removed.append(relative_path)
            except OSError:
                continue
    for directory in sorted(run_dir.rglob('*'), reverse=True):
        if not directory.is_dir():
            continue
        if any(directory.iterdir()):
            continue
        try:
            directory.rmdir()
        except OSError:
            continue
    return removed

def _prepare_run(case: 'dict[str, Any]', *, run_id: 'str', run_dir: 'Path', args: 'argparse.Namespace') -> 'dict[str, Any]':
    '''Run the preparation stages through the shared stage executor (P4 cache-aware).'''

    stage_module = _workflow_stage()
    statestore = _workflow_statestore()
    stage_cache_module = _workflow_stage_cache()
    store = statestore.FileStateStore(run_dir, root=ROOT)
    cache = _prepare_stage_cache(args)
    state = None
    resume_report = None
    resume_recovered = None
    if getattr(args, 'resume', False):
        try:
            state = store.load()
        except statestore.StateStoreError as exc:
            resume_recovered = _recover_corrupt_state(statestore, store, reason=str(exc))
            state = None
        if state is None and resume_recovered is None:
            warn(f'pipeline-state 缺失：{relative(run_dir / 'pipeline-state.json', ROOT)}；按新 pipeline 重建（已存在的阶段产物会按缓存/输出校验复用）')
            resume_recovered = {'backup': None, 'reason': 'missing_state'}
    from le_calc.workflow import contract as workflow_contract
    if state is None:
        state = workflow_contract.PipelineState(case_id=str(case.get('case_id')), run_id=run_id)
    captures = []
    context = _prepare_context(run_dir, captures=captures)
    context.adapters['stage_cache'] = cache
    stages = _prepare_stages_for(case, args)
    main_skill = getattr(args, 'main_skill', None) or _case_main_skill(case)
    params = _prepare_params(args)
    if main_skill:
        params['main_skill'] = main_skill
    base_input_refs = _prepare_input_refs(case, resolve(ROOT, case['_path']) if case.get('_path') else ROOT)
    override_refs = _prepare_override_input_refs(params)
    forced = not cache.enabled
    if forced:
        info('缓存已禁用：强制执行全部准备阶段（--no-cache / LE_PIPELINE_CACHE_DISABLE）')

    def stage_input_refs(stage_id: str) -> list[dict[str, Any]]:
        return _prepare_stage_input_refs(stage_id, base_input_refs, override_refs)

    state.pending_input = []
    current_fingerprints = {}
    dependency_chain = {}
    if state.stages and not forced:
        by_id = {entry.stage_id: entry for entry in state.stages}
        for stage_id in stages:
            if stage_id not in by_id:
                break
            request = _prepare_request(stage_id, case=case, run_id=run_id, params=params, input_refs=stage_input_refs(stage_id), dependencies=dependency_chain, run_dir=run_dir)
            fingerprints = stage_module.stage_fingerprints(request, context)
            current_fingerprints[stage_id] = fingerprints
            dependency_chain[stage_id] = str(fingerprints['cache_key'])
        resume_report = store.resume_plan(state, current_fingerprints=current_fingerprints)
        if resume_report['invalidated'] or resume_report['downstream_invalidated']:
            invalid = [item['stage_id'] for item in resume_report['invalidated']] + list(resume_report['downstream_invalidated'])
            store.invalidate(state, invalid)
            state.stages = [entry for entry in state.stages if entry.stage_id in resume_report['valid']]
            store.save(state)
    if forced:
        # --no-cache / env-disabled 必须覆盖 StateStore 快速路径：清空记录并清理
        # 未引用旧产物，保证所有阶段实际执行且 cache 条目输出集完整刷新。
        state.stages = []
        store.save(state)
        _clean_unreferenced_outputs(run_dir, state)
        resume_report = dict(resume_report or {})
        resume_report.update({'forced_rerun': True, 'disabled': True, 'valid': [], 'invalidated': [], 'downstream_invalidated': [], 'restart_from': stages[0] if stages else None})
    elif getattr(args, 'resume', False):
        _clean_unreferenced_outputs(run_dir, state)
    valid_entries = {} if forced else {entry.stage_id: entry for entry in state.stages}
    results = []
    worst = 'ready'
    dependency_chain = {}
    for stage_id in stages:
        if stage_id in valid_entries:
            entry = valid_entries[stage_id]
            cache_key = entry.cache_key or current_fingerprints.get(stage_id, {}).get('cache_key')
            if cache_key:
                dependency_chain[stage_id] = str(cache_key)
            results.append({'stage_id': stage_id, 'execution_status': 'cached', 'readiness': entry.readiness, 'issues': [], 'metrics': {'input_fingerprint': entry.input_fingerprint, 'implementation_fingerprint': entry.implementation_fingerprint, 'parameters_fingerprint': entry.parameters_fingerprint, 'cache_key': entry.cache_key, 'cache_status': entry.cache_status or 'hit', 'cache_hit': True}, 'cache': {'status': entry.cache_status or 'hit', 'key': entry.cache_key, 'reason': 'state_verified'}, 'output_refs': [dict(ref) for ref in entry.output_refs]})
            if entry.readiness:
                worst = max(worst, entry.readiness, key=(lambda item: _READINESS_ORDER.get(item, 0)))
            continue
        request = _prepare_request(stage_id, case=case, run_id=run_id, params=params, input_refs=stage_input_refs(stage_id), dependencies=dependency_chain, run_dir=run_dir)
        result = stage_module.execute_stage(request, context)
        store.record(state, result)
        store.save(state)
        entry = result.to_dict()
        results.append(entry)
        cache_key = (result.cache or {}).get('key') or (result.metrics or {}).get('cache_key')
        if cache_key:
            dependency_chain[stage_id] = str(cache_key)
        if result.execution_status == workflow_contract.EXECUTION_WAITING:
            for issue in result.issues or []:
                required = issue.get('required_input')
                if not required:
                    continue
                if required not in state.pending_input:
                    state.pending_input.append(dict(required))
        if result.readiness:
            worst = max(worst, result.readiness, key=(lambda item: _READINESS_ORDER.get(item, 0)))
        if result.execution_status not in (workflow_contract.EXECUTION_FAILED, workflow_contract.EXECUTION_WAITING):
            continue
        break
    store.save(state)
    for entry in state.stages:
        if entry.readiness:
            worst = max(worst, entry.readiness, key=(lambda item: _READINESS_ORDER.get(item, 0)))
    resume_report = dict(resume_report or {})
    if resume_recovered:
        resume_report['recovered'] = resume_recovered
    return {'state': state.to_dict(), 'state_ref': relative(store.path, ROOT), 'results': results, 'readiness': worst, 'waiting': any((str(entry.get('execution_status')) == 'waiting' for entry in results)), 'captures': captures, 'resume': resume_report or None, 'cache': cache.stats(), 'pipeline_state': state}

def cmd_prepare(args: 'argparse.Namespace') -> 'int':
    try:
        case = _load_case(args)
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_ARGS
    run_root = _run_root(case, args)
    resume = bool(getattr(args, 'resume', False))
    run_id = getattr(args, 'run_id', None)
    if resume and not run_id:
        fail('--resume 需要同时提供 --run-id（用于定位既有 pipeline-state）')
        return EXIT_ARGS
    run_id = run_id or _fresh_run_id(case)
    run_dir = run_root / run_id
    if not resume:
        if run_dir.exists() and any(run_dir.iterdir()):
            fail(f'运行目录非空，拒绝覆盖：{relative(run_dir, ROOT)}（请换 --run-id 或使用 --resume）')
            return EXIT_FAIL
    heading(f'le prepare — {case.get('case_id')}（阶段化准备：原始输入 → 计划/分析产物）')
    info(f'运行目录：{relative(run_dir, ROOT)}')
    calculation_route = _route_for_case(case)
    info(f"计算路由：{_route_summary(calculation_route)}")
    if calculation_route.get('engine') in ('needs_research', 'needs_input') and not _has_raw_prepare_input(case, args):
        run_dir.mkdir(parents=True, exist_ok=True)
        readiness = 'needs_research' if calculation_route.get('engine') == 'needs_research' else 'needs_input'
        try:
            from _common import write_json as _write_json
            _write_json(run_dir / 'prepare-context.json', {
                'schema_version': 'le-prepare-context-v1',
                'case_id': case.get('case_id'),
                'run_id': run_id,
                'calculation_route': calculation_route,
                'readiness': readiness,
                'prepare_stages': [],
                'stage_results': [],
                'note': '无可用统一计算来源且没有原始输入；等待研究/输入，不生成伪 DPS',
            })
        except Exception:
            pass
        print(f"prepare：PARTIAL（{readiness}；无统一计算来源且没有原始输入，已写 prepare-context.json）")
        for issue in calculation_route.get('diagnostics') or []:
            print(f"  - {issue.get('code')}: {issue.get('message')}")
        return EXIT_PARTIAL
    main_skill, main_skill_source = _resolve_main_skill(case, args)
    if not main_skill:
        candidate = _infer_main_skill(case, _case_model(case))
        hint = f'从计算输入推断的候选为 {candidate}，请显式传 --main-skill {candidate} 确认' if candidate else '请显式传 --main-skill <ability-id>'
        print(f'主技能：未声明（needs_input）——案例没有 case.main_skill.ability_id，调用方也没有传 --main-skill；{hint}。')
        print('prepare：PARTIAL（needs_input；未执行任何阶段，未生成 snapshot/分析产物）')
        return EXIT_PARTIAL
    args.main_skill = main_skill
    args._main_skill_source = main_skill_source
    info(f'主技能（{'命令行显式' if main_skill_source == 'cli' else '案例声明'}）：{main_skill}')
    run_dir.mkdir(parents=True, exist_ok=True)
    for check in _pipeline_dependency_checks():
        if check['status'] == 'FAIL':
            fail(f'依赖检查失败：{check['detail']}')
            return EXIT_FAIL
        info(f'{check['name']}：{check['detail']}')
    route = _prepare_route(case, args)
    if route.get('route_id'):
        info(f'输入路线：{route['route_id']}（{route['route'].get('label')}）')
    else:
        info('输入路线：未判定（le_pipeline 未交付或案例缺少来源字段）；继续按案例声明执行')
    if route.get('missing'):
        fail('依赖检查未通过（不静默改用其它解析路线）：' + ', '.join(route['missing'][:8]))
        return EXIT_PARTIAL
    try:
        outcome = _prepare_run(case, run_id=run_id, run_dir=run_dir, args=args)
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_FAIL
    except Exception as exc:
        fail(f'prepare 失败：{exc}')
        traceback.print_exc()
        return EXIT_FAIL
    try:
        from _common import write_json as _write_json
    except Exception:
        pass
    try:
        _write_json(run_dir / 'prepare-context.json', {'schema_version': 'le-prepare-context-v1', 'case_id': case.get('case_id'), 'run_id': run_id, 'calculation_route': _route_for_case(case), 'route': route.get('route_id'), 'route_detail': {'label': (route.get('route') or {}).get('label'), 'stage_sequence': (route.get('route') or {}).get('stage_sequence'), 'fallback_policy': (route.get('route') or {}).get('fallback_policy')}, 'main_skill': getattr(args, 'main_skill', None), 'main_skill_source': getattr(args, '_main_skill_source', None) or _resolve_main_skill(case, args)[1], 'readiness': outcome['readiness'], 'prepare_stages': list(_prepare_stages_for(case, args)), 'cache': outcome.get('cache'), 'stage_results': [{'stage_id': entry.get('stage_id'), 'execution_status': entry.get('execution_status'), 'cache_status': (entry.get('cache') or {}).get('status'), 'cache_key': (entry.get('cache') or {}).get('key'), 'input_fingerprint': (entry.get('metrics') or {}).get('input_fingerprint'), 'implementation_fingerprint': (entry.get('metrics') or {}).get('implementation_fingerprint'), 'parameters_fingerprint': (entry.get('metrics') or {}).get('parameters_fingerprint')} for entry in outcome['results']], 'child_call_count': len(outcome.get('captures') or [])})
    except Exception:
        pass
    try:
        context_path = run_dir / 'prepare-context.json'
        if context_path.is_file():
            context_doc = json.loads(context_path.read_text(encoding='utf-8'))
            stages = list(_prepare_stages_for(case, args))
            context_doc['prepare_stages'] = stages
            route_detail = context_doc.get('route_detail') or {}
            if isinstance(route_detail, dict):
                route_detail['stage_sequence'] = stages
            calculation_route_doc = context_doc.get('calculation_route') or {}
            if isinstance(calculation_route_doc, dict) and calculation_route_doc.get('case_ref'):
                case_value = str(calculation_route_doc['case_ref'])
                case_path = Path(case_value)
                if not case_path.is_absolute():
                    case_path = ROOT / case_path
                calculation_route_doc['case_ref'] = relative(case_path, ROOT)
            artifacts: dict[str, Any] = {}
            plan_path = run_dir / 'calculation-plan.json'
            if plan_path.is_file():
                plan_doc = json.loads(plan_path.read_text(encoding='utf-8'))
                refs = {str(item.get('role')): item for item in plan_doc.get('input_refs') or []}
                artifacts['calculation_plan'] = {
                    'path': relative(plan_path, ROOT),
                    'plan_sha256': plan_doc.get('plan_sha256'),
                }
                for role in ('calculation_input', 'build_spec', 'profile', 'attribute_conversion_rules'):
                    if role in refs:
                        artifacts[role] = {
                            'path': refs[role].get('path'),
                            'sha256': refs[role].get('sha256'),
                        }
            for name in ('analysis-plan.json', 'coverage.json', 'research-queue.json'):
                candidate = run_dir / name
                if candidate.is_file():
                    artifacts[name.replace('.json', '').replace('-', '_')] = {
                        'path': relative(candidate, ROOT),
                        'sha256': sha256_file(candidate),
                    }
            context_doc['artifacts'] = artifacts
            context_doc['input_handoff'] = {
                'mode': 'explicit_artifact_refs',
                'compiled_from': (artifacts.get('calculation_input') or {}).get('path'),
                'plan': (artifacts.get('calculation_plan') or {}).get('path'),
            }
            context_path.write_text(
                json.dumps(context_doc, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
    except Exception:
        pass
    heading('阶段状态')
    width = max((len(str(entry['stage_id'])) for entry in outcome['results']))
    failed = False
    cache_counts = {'hit': 0, 'miss': 0, 'disabled': 0, 'cached': 0}
    for entry in outcome['results']:
        status = str(entry.get('execution_status'))
        stage_id = str(entry['stage_id'])
        readiness = entry.get('readiness') or ''
        cache_status = str((entry.get('cache') or {}).get('status') or '')
        cache_key = str((entry.get('cache') or {}).get('key') or '')
        if status == 'cached':
            cache_counts['cached'] += 1
        else:
            cache_counts[cache_status if cache_status in cache_counts else 'cached'] += 1
        print(f'  {stage_id:<{width}}  {status:<9}  cache={cache_status:<9}  {cache_key[:12] + '…' if cache_key else '':<14} {readiness}')
        for issue in entry.get('issues') or []:
            print(f'      [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}')
        if status != 'failed':
            continue
        failed = True
        continue
    print()
    artifacts_written = [name for name in ('analysis-plan.json', 'coverage.json', 'research-queue.json')
         if (run_dir / name).is_file()]
    print(f'产物：{', '.join((relative(run_dir / name, ROOT) for name in artifacts_written)) or '无'}')
    print(f'状态：{relative(run_dir / 'pipeline-state.json', ROOT)}')
    plan_path = run_dir / 'calculation-plan.json'
    if plan_path.is_file():
        print(f'计划产物：{relative(plan_path, ROOT)}')
    cache_summary = outcome.get('cache') or {}
    print(f'阶段缓存：hit={cache_counts['hit']} miss={cache_counts['miss']} disabled={cache_counts['disabled']} state_cached={cache_counts['cached']}（root={cache_summary.get('cache_root', 'data/cache/pipeline')}，entries={cache_summary.get('entries', 0)}）')
    print(f'子进程调用：{len(outcome.get('captures') or [])} 次')
    readiness = outcome['readiness']
    if failed:
        fail(f'prepare：FAIL（readiness={readiness}）')
        return EXIT_FAIL
    if outcome.get('waiting'):
        print(f'prepare：PARTIAL（waiting；readiness={readiness}；前置数据准备已完成，后续阶段需要补齐输入/映射）')
        return EXIT_PARTIAL
    if readiness in ('needs_research', 'needs_input'):
        print(f'prepare：PARTIAL（{readiness}；数据准备完成，技能映射/研究或输入待补齐）')
        return EXIT_PARTIAL
    print(f'prepare：PASS（readiness={readiness}；本次未执行 simulate）')
    return EXIT_OK

def cmd_validate(args: 'argparse.Namespace') -> 'int':
    try:
        case = _load_case(args)
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_ARGS
    heading(f'le validate — {case.get('case_id')}')
    print(f'案例配置：{relative(case['_path'], ROOT)}')
    route = _route_for_case(case)
    if route.get('engine') != 'legacy':
        info(f"计算路由：{_route_summary(route)}")
        run_dir_value = getattr(args, 'run_dir', None)
        if run_dir_value:
            run_dir = resolve(ROOT, run_dir_value)
            if not run_dir.is_dir():
                fail(f'run 目录不存在：{run_dir_value}')
                return EXIT_FAIL
            from le_calc.workflow import unified  # noqa: PLC0415
            result = unified.validate_stage(case, root=ROOT, run_dir=run_dir, write=False)
            checks = [
                {'name': item['id'], 'status': item['status'], 'detail': item.get('detail')}
                for item in (result.get('payload') or {}).get('checks') or []
            ]
            print_checks(checks)
            if result.get('passed'):
                print('validate：PASS（计划/场景/结果/账本绑定一致；未重新计算）')
                return EXIT_OK
            print('validate：FAIL（见上方 FAIL 项）', file=sys.stderr)
            return EXIT_FAIL
        if route.get('engine') in ('needs_input', 'needs_research'):
            print(f"validate：PARTIAL（{route.get('status')}；无统一计划可校验）")
            for issue in route.get('diagnostics') or []:
                print(f"  - {issue.get('code')}: {issue.get('message')}")
            return EXIT_PARTIAL
        if route.get('status') != 'ready':
            print(f"validate：FAIL（计算路由不可用：{_route_summary(route)}）", file=sys.stderr)
            return EXIT_FAIL
        print('validate：PASS（数据结构与统一计算路由合法；未校验数值产物，可用 --run-dir 校验指定 run）')
        return EXIT_OK
    if _case_model(case) == 'research':
        from le_pipeline.research import validate_data
        checks = validate_data(case, ROOT)
        print_checks(checks)
        ok = _status_ok(checks)
        print('validate：' + ('PASS' if ok else 'FAIL') + '（研究案例：仅数据结构与绑定；未验收伤害/网页）')
        return EXIT_OK if ok else EXIT_FAIL
    if _case_model(case) == 'hit':
        checks = _hit_case_checks(case)
    else:
        checks = _case_checks(case, include_expected=True)
    print_checks(checks)
    ok = _status_ok(checks)
    print()
    if ok:
        print('validate：PASS（结构、指纹与绑定关系一致）')
        return EXIT_OK
    print('validate：FAIL（见上方 FAIL 项）', file=sys.stderr)
    return EXIT_FAIL

def _drop_nonsemantic(node: 'Any', parent_key: 'str | None'=None) -> 'Any':
    '''Recursively drop timestamp fields and provenance-only paths.

    ``affix_db.path`` / ``affix_db_path`` are provenance records of where the
    affix DB lived; a rebuilt BD legitimately records the repository-relative
    path while the frozen sample records the original absolute path.  They are
    excluded from the semantic rebuild comparison (no numeric/list difference is
    hidden: every other field is compared).
    '''

    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key in ('generated_at', 'generated', 'updated_at', 'fetched_at'):
                continue
            if key == 'display_rarity':
                continue
            if parent_key == 'affix_db' and key == 'path':
                continue
            if parent_key == 'roll_semantics' and key == 'affix_db':
                continue
            if key == 'affix_db_path':
                continue
            result[key] = _drop_nonsemantic(value, key)
        return result
    if isinstance(node, list):
        return [_drop_nonsemantic(value, parent_key) for value in node]
    return node

def _normalize_bd(build: 'dict[str, Any]') -> 'dict[str, Any]':
    '''Drop the non-semantic fields that legitimately differ on rebuild.'''

    copy = _drop_nonsemantic(json.loads(json.dumps(build)))
    provenance = copy.get('provenance')
    if isinstance(provenance, dict) and isinstance(provenance.get('generator_tool'), str):
        from _common import portable_text
        provenance['generator_tool'] = portable_text(provenance['generator_tool'], ROOT)[0]
    source = copy.get('source')
    if isinstance(source, dict):
        source.pop('path', None)
    extract_meta = copy.get('extract_meta')
    if isinstance(extract_meta, dict):
        extract_meta.pop('path', None)
        extract_meta.pop('source_path', None)
    return copy

def _semantic_calc_input(document: 'dict[str, Any]') -> 'dict[str, Any]':
    '''Projection of a calculation input that must be identical across runs.'''

    mods = []
    for mod in document.get('mods') or []:
        mods.append({key: mod.get(key) for key in ('mod_id', 'item_instance_id', 'source', 'channel', 'kind', 'property', 'tags', 'special_tag', 'modifier_type', 'value_numeric', 'display_value', 'value_status', 'value_source')} | {'stat_name': (mod.get('stat') or {}).get('name')})
    items = [{'instance_id': item.get('instance_id'), 'role': item.get('role'), 'slot': item.get('slot'), 'mods': [mod.get('mod_id') for mod in item.get('mods') or []]} for item in document.get('items') or []]
    return {'character': document.get('character'), 'passive_tree': {'points_total': (document.get('passive_tree') or {}).get('points_total'), 'nodes': [{'node_id': node.get('node_id'), 'points': node.get('points')} for node in (document.get('passive_tree') or {}).get('nodes') or []]}, 'specializations': [{'tree': (spec.get('tree') or {}).get('entity_id'), 'points_total': spec.get('points_total'), 'nodes': [{'node_id': node.get('node_id'), 'points': node.get('points')} for node in spec.get('nodes') or []]} for spec in (document.get('skills') or {}).get('specializations') or []], 'mods': mods, 'items': items}

def _diff_keys(left: 'Any', right: 'Any', prefix: 'str'='') -> 'list[str]':
    diffs = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                diffs.append(f'{prefix}/{key}')
            else:
                diffs.extend(_diff_keys(left[key], right[key], f'{prefix}/{key}'))
        return diffs
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            diffs.append(f'{prefix}#len({len(left)}!={len(right)})')
        for index, (a, b) in enumerate(zip(left, right)):
            diffs.extend(_diff_keys(a, b, f'{prefix}[{index}]'))
        return diffs
    if left != right:
        diffs.append(prefix or '/')
    return diffs

def _workflow_portable():
    '''Lazy import of the shared portable-artefact normaliser (workflow.portable).'''

    calc_src = str(MODULE_PATHS['le_calc'])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import portable
    return portable

def _sanitize_directory(directory: 'Path', root: 'Path') -> 'list[dict[str, Any]]':
    '''Normalise machine absolute paths in generated (not frozen) artefacts.

    Delegates to ``le_calc.workflow.portable.sanitize_directory`` so reproduce
    and prepare share one implementation (D3 / review D6 lesson).
    '''

    return _workflow_portable().sanitize_directory(directory, root)

def _scan_run_dir(run_dir: 'Path') -> 'list[dict[str, Any]]':
    '''Return machine absolute path hits in every text artefact of a run dir.'''

    hits = []
    for path in sorted(run_dir.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in TEXT_SCAN_SUFFIXES:
            continue
        content = path.read_text(encoding='utf-8', errors='replace')
        matches = _MACHINE_PATH_RE.findall(content)
        if matches:
            hits.append({'path': relative(path, ROOT), 'count': len(matches), 'samples': sorted(set(matches))[:3]})
    return hits

_STEP_DURATIONS_S: 'list[float]' = []

def _reproduce_step(name: 'str', cmd: 'Sequence[str]', *, pythonpath: 'Iterable[Path]') -> 'None':
    heading(f'步骤 · {name}')
    step_started = time.perf_counter()
    result = run_child(cmd, pythonpath=pythonpath)
    elapsed_s = round(time.perf_counter() - step_started, 3)
    if result.returncode != 0:
        raise PipelineError(f'步骤失败（退出码 {result.returncode}）：{name}')
    _STEP_DURATIONS_S.append(elapsed_s)

def _workflow_expected_check():
    '''Lazy import of the shared expected-check implementation (le_calc.workflow).

    The implementation lives in ``le_calc.workflow.expected_check`` and is shared
    with ``le_calc.workflow.domain.validate_result``; ``scripts/`` must never grow
    a second copy of the comparison semantics.
    '''

    calc_src = str(MODULE_PATHS['le_calc'])
    if calc_src not in sys.path:
        sys.path.insert(0, calc_src)
    from le_calc.workflow import expected_check
    return expected_check

def _load_expected(case: 'dict[str, Any]') -> 'dict[str, Any]':
    return _workflow_expected_check().load_expected(case, root=ROOT)

def _check_expected(case: 'dict[str, Any]', run_dir: 'Path') -> 'tuple[list[dict[str, Any]], bool]':
    """Compare a produced BH run against the case's expected summary.

    Delegates to the shared implementation; check names / pointers / tolerance /
    ``run_expectations`` / generated-scenario comparison are frozen (parity).
    """

    return _workflow_expected_check().compare_expected(case, run_dir, root=ROOT)

def _write_reproduce_report(case: 'dict[str, Any]', run_dir: 'Path', steps: 'list[dict[str, Any]]', checks: 'list[dict[str, Any]]', portability: 'dict[str, Any]') -> 'Path':
    from _common import write_json
    report = {'schema': 'le-reproduce-report-v1', 'case_id': case.get('case_id'), 'run_id': run_dir.name, 'run_dir': relative(run_dir, ROOT), 'generated_at': _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(), 'case_path': relative(case['_path'], ROOT), 'ok': _status_ok(checks), 'steps': steps, 'checks': checks, 'portability': portability, 'excluded_non_semantic_fields': _load_expected(case).get('excluded_non_semantic_fields') or []}
    path = run_dir / 'reproduce-report.json'
    write_json(path, report)
    return path

def _normalize_snapshot(snapshot: 'dict[str, Any]', *, calc_input_path: 'str'='') -> 'dict[str, Any]':
    '''Projection of a hit snapshot that is stable across reproduce runs.

    The only legitimately changing fields are the calculator-input *path*
    (reproduce writes the rebuilt calc input under ``reports/<case>/<run>/steps``
    while the frozen case snapshot points at ``examples/<case>/inputs``) and the
    provenance locators that embed that path.
    '''

    copy = json.loads(json.dumps(snapshot))
    meta = copy.get('build_meta')
    if isinstance(meta, dict):
        calc_input = meta.get('calc_input')
        if isinstance(calc_input, dict):
            calc_input.pop('path', None)
    wanted = str(calc_input_path or '')
    def scrub(value: 'Any') -> 'Any':
        if isinstance(value, str):
            if wanted and wanted in value:
                return value.replace(wanted, '<calc_input>')
            name = value.split('#', 1)[0].replace('\\', '/').rsplit('/', 1)[-1]
            if 'calculation-input' in name and name.endswith('.json'):
                return '<calc_input>'
            return value
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    return scrub(copy)

def _check_expected_hit(case: 'dict[str, Any]', run_dir: 'Path') -> 'tuple[list[dict[str, Any]], bool]':
    """Compare a produced hit run against the case's expected summary (shared)."""

    return _workflow_expected_check().compare_expected(case, run_dir, root=ROOT)

def _reproduce_hit_case(case: 'dict[str, Any]', args: 'argparse.Namespace') -> 'int':
    '''``le.ps1 reproduce`` for ``case.model == "hit"``.

    Reuses the same pipeline stages as the Black Hole case: raw save -> BD,
    pinned BuildSpec revision -> calculator export, affix-db recalculation,
    explicit corrections, frozen snapshot, damage run + report, then the
    ``expected-summary`` comparison.
    '''

    started = time.time()
    heading(f'le reproduce — {case.get('case_id')}（hit 模型：原始存档 → 快照 → 命中/DPS → 与预期摘要比对）')
    run_root = _run_root(case, args)
    run_id = args.run_id or _fresh_run_id(case)
    run_dir = run_root / run_id
    if run_dir.exists():
        fail(f'运行目录已存在，拒绝覆盖：{relative(run_dir, ROOT)}（请换 --run-id）')
        return EXIT_FAIL
    steps_dir = run_dir / 'steps'
    steps_dir.mkdir(parents=True)
    info(f'运行目录：{relative(run_dir, ROOT)}')
    steps = []
    _STEP_DURATIONS_S.clear()
    sanitized = []
    def record(key: 'str', description: 'str', outputs: 'Iterable[str]') -> 'None':
        elapsed_s = _STEP_DURATIONS_S.pop(0) if _STEP_DURATIONS_S else None
        steps.append({'step': key, 'description': description, 'status': 'ok', 'elapsed_s': elapsed_s, 'outputs': [relative(run_dir / item, ROOT) for item in outputs]})

    try:
        raw_save = case_path(case, 'source.character_save')
        affix_db = case_path(case, 'affix_db')
        save_out = steps_dir / '01-save-import'
        _reproduce_step('1/6 raw save → BD（le_save_import build）', [_python(), '-m', 'le_save_import', 'build', '--save', relative(raw_save, ROOT), '--out', relative(save_out, ROOT), '--affix-db', relative(affix_db, ROOT)], pythonpath=[MODULE_PATHS['le_save_import']])
        rebuilt_bd = save_out / 'bd.json'
        if not rebuilt_bd.is_file():
            raise PipelineError('le_save_import build 未生成 bd.json')
        shipped_bd = case_path(case, 'inputs.bd')
        diffs = _diff_keys(_normalize_bd(load_json(rebuilt_bd, what='rebuilt bd.json')), _normalize_bd(load_json(shipped_bd, what='case bd.json')))
        if diffs:
            raise PipelineError('从原始存档重建的 bd.json 与案例固定 bd.json 语义不一致：' + ', '.join(diffs[:8]))
        sanitized.extend(_sanitize_directory(save_out, ROOT))
        for item in sanitized:
            info(f'可移植性归一：{item['path']} 替换 {item['replacements']} 处机器路径')
        record('1-import', 'raw save → bd.json（与案例 bd.json 语义一致；路径已归一为仓库相对）', ['steps/01-save-import/bd.json'])
        build = case.get('build') or {}
        build_spec_path = resolve(ROOT, str(build.get('build_spec') or ''))
        revision = load_json(build_spec_path, what='build revision')
        revision_meta = (revision.get('meta') or {}).get('revision') or {}
        source_payload = (revision.get('meta') or {}).get('source_payload') or {}
        if revision_meta.get('revision_id') != build.get('revision_id'):
            raise PipelineError(f'build revision_id 不一致：{revision_meta.get('revision_id')!r} / 案例 {build.get('revision_id')!r}')
        if revision_meta.get('content_sha256') != build.get('content_sha256'):
            raise PipelineError('build content_sha256 与案例声明不一致')
        wire_path = case_path(case, 'source.tool_native_wire')
        wire_sha = sha256_file(wire_path)
        if wire_sha != source_payload.get('raw_sha256'):
            raise PipelineError(f'归档 wire sha256 与 BuildSpec revision 的 source_payload.raw_sha256 不一致：{wire_sha[:16]}… / {str(source_payload.get('raw_sha256'))[:16]}…')
        export_out = steps_dir / '02-calculation-input.json'
        _reproduce_step('2/6 BuildSpec revision → calculator export（le_build export）', [_python(), '-m', 'le_build', 'export', '--build', str(build.get('build_id')), '--revision', str(build.get('revision_id')), '--out', str(export_out)], pythonpath=[MODULE_PATHS['le_build']])
        record('2-export', '固定 BuildSpec revision → calculation-input-v1（并核对归档 wire sha256）', ['steps/02-calculation-input.json'])
        affix_out = steps_dir / '03-calculation-input.affix-db.json'
        affix_report = steps_dir / '03-affix-db-recompute.json'
        _reproduce_step('3/6 affix-db 重算（run_affix_db_pipeline.py）', [_python(), str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline' / 'run_affix_db_pipeline.py'), '--case', str(case['_path']), '--calc-input', str(export_out), '--out', str(affix_out), '--report', str(affix_report)], pythonpath=[MODULE_PATHS['le_calc'], MODULE_PATHS['le_save_import']])
        record('3-affix-db', '按 affix-db 每基底范围重算', ['steps/03-calculation-input.affix-db.json'])
        corrected_out = steps_dir / '04-calculation-input.corrected.json'
        corrections_report = steps_dir / '04-save-build-corrections.json'
        _reproduce_step('4/6 人工补值（apply_corrections.py）', [_python(), str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline' / 'apply_corrections.py'), '--case', str(case['_path']), '--input', str(affix_out), '--out', str(corrected_out), '--report', str(corrections_report)], pythonpath=[MODULE_PATHS['le_calc'], MODULE_PATHS['le_save_import']])
        shipped_calc = case_path(case, 'inputs.calculation_input')
        drift = _diff_keys(_semantic_calc_input(load_json(corrected_out, what='corrected calculation input')), _semantic_calc_input(load_json(shipped_calc, what='case calculation input')))
        if drift:
            raise PipelineError('重建的计算输入与案例固定计算输入语义不一致：' + ', '.join(drift[:8]))
        record('4-corrections', '人工补值（本案例为空）→ 与案例计算输入数值一致', ['steps/04-calculation-input.corrected.json'])
        snapshot_out = steps_dir / '05-input-snapshot.hit.json'
        _reproduce_step('5/6 hit 快照（le_calc hit-snapshot）', [_python(), '-m', 'le_calc', 'hit-snapshot', '--case', str(case['_path']), '--calc-input', str(corrected_out), '--out', str(snapshot_out)], pythonpath=[MODULE_PATHS['le_calc']])
        shipped_snapshot = case_path(case, 'inputs.snapshot')
        snapshot_diff = _diff_keys(_normalize_snapshot(load_json(snapshot_out, what='rebuilt hit snapshot'), calc_input_path=relative(corrected_out, ROOT)), _normalize_snapshot(load_json(shipped_snapshot, what='case hit snapshot'), calc_input_path=relative(shipped_calc, ROOT)))
        if snapshot_diff:
            raise PipelineError('重建的 hit 快照与案例固定快照语义不一致：' + ', '.join(snapshot_diff[:8]))
        record('5-snapshot', 'hit 快照（与案例快照语义一致，含 build/dataset/calc_input 指纹）', ['steps/05-input-snapshot.hit.json'])
        ruleset_path = case_path(case, 'ruleset.path')
        scenario_path = case_path(case, 'inputs.scenario')
        _reproduce_step('6/6 命中计算与报告（le_calc hit-run）', [_python(), '-m', 'le_calc', 'hit-run', '--case', relative(case['_path'], ROOT), '--snapshot', relative(snapshot_out, ROOT), '--ruleset', relative(ruleset_path, ROOT), '--scenario', relative(scenario_path, ROOT), '--out', relative(run_dir, ROOT), '--run-id', run_id], pythonpath=[MODULE_PATHS['le_calc']])
        record('6-hit-run', 'le_calc hit-run：result.json + buff-screening + 离线 HTML 报告', ['result.json', 'buff-screening.json', 'flame-reave-damage.html', 'run-manifest.json'])
        checks, ok = _check_expected_hit(case, run_dir)
        portability = {'machine_path_hits': _scan_run_dir(run_dir), 'sanitized_steps': sanitized}
        report_path = _write_reproduce_report(case, run_dir, steps, checks, portability)
    except PipelineError as exc:
        fail(str(exc))
        fail(f'部分产物保留在：{relative(run_dir, ROOT)}')
        return EXIT_FAIL
    _finalize_run_report(case, run_dir, endpoint=_recompute_endpoint(args), use_cache=not (getattr(args, 'no_report_cache', False) or getattr(args, 'no_cache', False)))
    duration = time.time() - started
    heading('检查结果')
    for check in checks:
        print(f'  {check['status']:4s}  {check['name']:36s} {check.get('detail', '')}')
    print()
    print(f'reproduce：{'PASS' if ok else 'FAIL'}（{len(checks)} 项检查，{duration:.1f}s）')
    print(f'报告：{relative(report_path, ROOT)}')
    print(f'运行目录：{relative(run_dir, ROOT)}')
    if ok:
        return EXIT_OK
    return EXIT_FAIL

def cmd_reproduce(args: 'argparse.Namespace') -> 'int':
    try:
        case = _load_case(args)
        if _route_for_case(case).get('engine') == 'unified_dps':
            heading(f'le reproduce — {case.get("case_id")}')
            fail(
                'reproduce：暂不支持统一计划案例的原始存档链重建（unsupported_operation）；'
                '请使用 prepare/run 生成计划与报告，或等待后续版本'
            )
            return EXIT_FAIL
        _require_known_model(case, command='reproduce')
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_ARGS
    if _case_model(case) == 'hit':
        return _reproduce_hit_case(case, args)
    started = time.time()
    heading(f'le reproduce — {case.get('case_id')}（从原始输入重建整条链）')
    run_root = _run_root(case, args)
    run_id = args.run_id or _fresh_run_id(case)
    run_dir = run_root / run_id
    if run_dir.exists():
        fail(f'运行目录已存在，拒绝覆盖：{relative(run_dir, ROOT)}（请换 --run-id）')
        return EXIT_FAIL
    steps_dir = run_dir / 'steps'
    steps_dir.mkdir(parents=True)
    info(f'运行目录：{relative(run_dir, ROOT)}')
    steps = []
    _STEP_DURATIONS_S.clear()
    sanitized = []
    def record(key: 'str', description: 'str', outputs: 'Iterable[str]') -> 'None':
        elapsed_s = _STEP_DURATIONS_S.pop(0) if _STEP_DURATIONS_S else None
        steps.append({'step': key, 'description': description, 'status': 'ok', 'elapsed_s': elapsed_s, 'outputs': [relative(run_dir / item, ROOT) for item in outputs]})

    try:
        raw_save = case_path(case, 'source.character_save')
        stash = optional_case_path(case, 'source.stash')
        planner = optional_case_path(case, 'source.planner_build_response')
        affix_db = case_path(case, 'affix_db')
        save_out = steps_dir / '01-save-import'
        import_cmd = [_python(), '-m', 'le_save_import', 'build', '--save', relative(raw_save, ROOT), '--out', relative(save_out, ROOT)]
        # 与首接的来源组合一致；旧混合路线保留 affix-db 参数。
        if planner is not None:
            import_cmd += ['--planner', relative(planner, ROOT), '--affix-db', relative(affix_db, ROOT)]
        if stash is not None:
            import_cmd += ['--stash', relative(stash, ROOT)]
        _reproduce_step('1/7 raw save → BD（le_save_import build）', import_cmd, pythonpath=[MODULE_PATHS['le_save_import']])
        if planner is None and optional_case_path(case, 'source.tool_native_wire') is not None:
            build_binding = case.get('build') or {}
            spec_path = case_path(case, 'build.build_spec')
            spec = load_json(spec_path)
            meta = spec.get('meta') or {}
            revision = meta.get('revision') or {}
            if (sha256_file(spec_path) != build_binding.get('build_spec_sha256')
                    or revision.get('revision_id') != build_binding.get('revision_id')
                    or revision.get('content_sha256') != build_binding.get('content_sha256')
                    or sha256_file(case_path(case, 'source.tool_native_wire')) != (meta.get('source_payload') or {}).get('raw_sha256')):
                raise PipelineError('归档 wire / BuildSpec revision 绑定不一致')
            from le_pipeline import save_adapter
            _le_calc_modules()
            wire_tick = time.perf_counter()
            wire = save_adapter.decode(sys.modules[__name__], raw_save, steps_dir / '01-tool-native')
            if load_json(wire) != load_json(case_path(case, 'source.tool_native_wire')):
                raise PipelineError('从冻结存档重建的 tool-native wire 与案例归档不一致')
            steps.append({'step': '1-wire', 'description': '冻结存档重新解码并归一 wire，与归档语义比较', 'status': 'ok', 'elapsed_s': time.perf_counter() - wire_tick, 'outputs': [relative(wire, ROOT)]})
        rebuilt_bd = save_out / 'bd.json'
        if not rebuilt_bd.is_file():
            raise PipelineError('le_save_import build 未生成 bd.json')
        shipped_bd = case_path(case, 'inputs.bd')
        diffs = _diff_keys(_normalize_bd(load_json(rebuilt_bd, what='rebuilt bd.json')), _normalize_bd(load_json(shipped_bd, what='case bd.json')))
        if diffs:
            raise PipelineError('从原始存档重建的 bd.json 与案例固定 bd.json 语义不一致：' + ', '.join(diffs[:8]))
        sanitized.extend(_sanitize_directory(save_out, ROOT))
        for item in sanitized:
            info(f'可移植性归一：{item['path']} 替换 {item['replacements']} 处机器路径')
        record('1-import', 'raw save + planner + stash → bd.json（与案例 bd.json 语义一致；路径已归一为仓库相对）', ['steps/01-save-import/bd.json'])
        export_out = steps_dir / '02-calculation-input.json'
        build = case.get('build') or {}
        _reproduce_step('2/7 BuildSpec revision → calculator export（le_build export）', [_python(), '-m', 'le_build', 'export', '--build', str(build.get('build_id')), '--revision', str(build.get('revision_id')), '--out', str(export_out)], pythonpath=[MODULE_PATHS['le_build']])
        base_counts = calculation_input_counts(load_json(export_out, what='calculator export'))
        info(f'calculator export 结构：{base_counts}')
        record('2-export', '固定 BuildSpec revision → calculation-input-v1', ['steps/02-calculation-input.json'])
        affix_out = steps_dir / '03-calculation-input.affix-db.json'
        affix_report = steps_dir / '03-affix-db-recompute.json'
        _reproduce_step('3/7 affix-db 重算 + 折射神像（run_affix_db_pipeline.py）', [_python(), str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline' / 'run_affix_db_pipeline.py'), '--case', str(case['_path']), '--calc-input', str(export_out), '--out', str(affix_out), '--report', str(affix_report)], pythonpath=[MODULE_PATHS['le_calc'], MODULE_PATHS['le_save_import']])
        record('3-affix-db', '按 affix-db 每基底范围重算 + 折射神像前后缀放大', ['steps/03-calculation-input.affix-db.json', 'steps/03-affix-db-recompute.json'])
        corrected_out = steps_dir / '04-calculation-input.corrected.json'
        corrections_report = steps_dir / '04-save-build-corrections.json'
        _reproduce_step('4/7 人工补值（apply_corrections.py，数据来自 corrections.json）', [_python(), str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline' / 'apply_corrections.py'), '--case', str(case['_path']), '--input', str(affix_out), '--out', str(corrected_out), '--report', str(corrections_report)], pythonpath=[MODULE_PATHS['le_calc'], MODULE_PATHS['le_save_import']])
        shipped_calc = case_path(case, 'inputs.calculation_input')
        drift = _diff_keys(_semantic_calc_input(load_json(corrected_out, what='corrected calculation input')), _semantic_calc_input(load_json(shipped_calc, what='case calculation input')))
        if drift:
            raise PipelineError('重建的计算输入与案例固定计算输入语义不一致：' + ', '.join(drift[:8]))
        record('4-corrections', '人工补值（任务属性、胸甲火抗、神像 corrupted 全抗）→ 与案例计算输入数值一致', ['steps/04-calculation-input.corrected.json', 'steps/04-save-build-corrections.json'])
        snapshot_out = steps_dir / '05-input-snapshot.json'
        _reproduce_step('5/7 输入快照（make_snapshot.py）', [_python(), str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline' / 'make_snapshot.py'), '--case', str(case['_path']), '--calc-input', str(corrected_out), '--out', str(snapshot_out), '--compare', str(case_path(case, 'inputs.snapshot'))], pythonpath=[MODULE_PATHS['le_calc'], MODULE_PATHS['le_save_import']])
        record('5-snapshot', '静态属性快照（与案例快照语义一致，含案例级 EXPECTED_* 固定）', ['steps/05-input-snapshot.json'])
        ruleset_path = case_path(case, 'ruleset.path')
        _reproduce_step('6/7 计算与报告（le_calc run）', [_python(), '-m', 'le_calc', 'run', '--snapshot', relative(snapshot_out, ROOT), '--ruleset', relative(ruleset_path, ROOT), '--monster-level', str(int(case.get('monster_level') or 100)), '--calc-input', relative(corrected_out, ROOT), '--build', relative(rebuilt_bd, ROOT), '--out', relative(run_dir, ROOT), '--run-id', run_id], pythonpath=[MODULE_PATHS['le_calc']])
        record('6-calc', 'le_calc run：result.json + 条件重算 + HTML 报告', ['result.json', 'conditional-recompute.json', 'conditional-scenario.json', 'run-manifest.json', 'black-hole-damage.html'])
        _reproduce_step('7/7 BD HTML 视图（gen_bd_html.py）', [_python(), str(ROOT / 'tools' / 'save_import' / 'scripts' / 'pipeline' / 'gen_bd_html.py'), '--case', str(case['_path']), '--bd', str(shipped_bd), '--out', str(run_dir / 'bd-view.html')], pythonpath=[MODULE_PATHS['le_build'], MODULE_PATHS['le_save_import']])
        record('7-bd-view', '玩家可读 BD HTML 视图（修正后的词缀值）', ['bd-view.html'])
        heading(f'数值核对（与 {case.get("expected")} 比较）')
        checks, ok = _check_expected(case, run_dir)
        print_checks(checks)
        heading('可移植性检查（运行目录内不得出现机器绝对路径）')
        hits = _scan_run_dir(run_dir)
        if hits:
            for item in hits:
                fail(f'{item['path']}: {item['count']} 处，例如 {item['samples']}')
            raise PipelineError('运行目录仍包含机器绝对路径（见上方列表）')
        print(f'  PASS  运行目录内无机器绝对路径（扫描 {len(TEXT_SCAN_SUFFIXES)} 类文本文件）')
        portability = {'policy': 'reproduce 向子工具传仓库相对路径；仅对生成的运行产物做机器路径归一（冻结输入不改写）', 'sanitized_files': sanitized, 'machine_path_hits': []}
        report_path = _write_reproduce_report(case, run_dir, steps, checks, portability)
    except PipelineError as exc:
        fail(str(exc))
        fail(f'已保留部分产物：{relative(run_dir, ROOT)}')
        return EXIT_FAIL
    except Exception:
        traceback.print_exc()
        fail(f'reproduce 发生未预期错误；已保留部分产物：{relative(run_dir, ROOT)}')
        return EXIT_FAIL
    _finalize_run_report(case, run_dir, endpoint=_recompute_endpoint(args), use_cache=not (getattr(args, 'no_report_cache', False) or getattr(args, 'no_cache', False)))
    elapsed = time.time() - started
    print()
    print(f'运行目录：{relative(run_dir, ROOT)}')
    print(f'HTML 报告：{relative(run_dir / 'black-hole-damage.html', ROOT)}')
    print(f'BD 视图：  {relative(run_dir / 'bd-view.html', ROOT)}')
    print(f'复现记录：{relative(report_path, ROOT)}')
    print(f'耗时：{elapsed:.1f}s')
    if ok:
        print('reproduce：PASS（整条链从原始输入重建，关键数值在容差内一致）')
        return EXIT_OK
    print('reproduce：FAIL（数值或结构核对未通过，见上方 FAIL 项）', file=sys.stderr)
    return EXIT_FAIL

def cmd_run(args: 'argparse.Namespace') -> 'int':
    try:
        case = _load_case(args)
        route = _route_for_case(case)
        if route.get('engine') != 'legacy':
            _le_calc_modules()
            return _run_unified_case(case, route, args)
        _require_known_model(case, command='run')
        run_root = _run_root(case, args)
        run_id = args.run_id or _fresh_run_id(case)
        final_dir = resolve(ROOT, args.out) if args.out else run_root / run_id
        staged = str(getattr(args, 'publish', 'none') or 'none') == 'staged'
        if staged and not args.run_id and args.out:
            run_id = _staged_logical_run_id(final_dir, case)
            info(f"staged：未提供 --run-id，逻辑 run-id 取输出目录名：{run_id}")
        if final_dir.exists() and any(final_dir.iterdir()) and not staged:
            fail(f'输出目录非空，拒绝覆盖：{relative(final_dir, ROOT)}')
            return EXIT_FAIL
        is_hit = _case_model(case) == 'hit'
        if is_hit:
            snapshot = case_path(case, 'inputs.snapshot')
            calc_input = case_path(case, 'inputs.calculation_input')
            ruleset = case_path(case, 'ruleset.path')
        else:
            snapshot = case_path(case, 'inputs.snapshot')
            calc_input = case_path(case, 'inputs.calculation_input')
            build_doc = case_path(case, 'inputs.bd')
            ruleset = case_path(case, 'ruleset.path')
        plan = _staged_plan(case, args, run_id=run_id, final_dir=final_dir) if staged else None
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_ARGS
    try:
        work_dir = plan['candidate'] if plan is not None else final_dir
        if plan is not None:
            if plan.get('wait_required'):
                try:
                    _wait_staged_lease(plan)
                except PipelineError as exc:
                    fail(str(exc))
                    return EXIT_FAIL
            if not plan['candidate'].is_dir():
                try:
                    _prepare_staged_plan_files(plan)
                except Exception as exc:  # noqa: BLE001 - setup failure must be explicit
                    fail(f'staged 候选准备失败：{exc}')
                    return EXIT_FAIL
            info(f'staged 发布模式：候选 {relative(work_dir, ROOT)} → 正式 {relative(final_dir, ROOT)}')
            return _run_staged_stage_pipeline(
                case, run_id=run_id, plan=plan, args=args, is_hit=is_hit
            )
        if is_hit:
            code = _run_hit_case(
                case,
                run_id=run_id,
                out_dir=work_dir,
                snapshot=snapshot,
                calc_input=calc_input,
                ruleset=ruleset,
                recalc_endpoint=_recompute_endpoint(args),
                use_report_cache=(
                    not getattr(args, 'no_report_cache', False)
                    and not getattr(args, 'no_cache', False)
                    and plan is None
                ),
                staged_plan=plan,
                stage_pipeline=plan is None,
                pipeline_args=args,
            )
            return code
        # --publish none: same formal stage graph in place; exit/output semantics
        # remain the historical direct run path (no staging / operation record).
        return _run_staged_stage_pipeline(
            case, run_id=run_id, plan=None, args=args, is_hit=False, final_dir=final_dir
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        heading(f'le run — {case.get('case_id')}（冻结输入，仅计算）')
        cmd = [
            _python(), '-m', 'le_calc', 'run',
            '--snapshot', relative(snapshot, ROOT),
            '--ruleset', relative(ruleset, ROOT),
            '--monster-level', str(int(case.get('monster_level') or 100)),
            '--calc-input', relative(calc_input, ROOT),
            '--build', relative(build_doc, ROOT),
            '--out', relative(work_dir, ROOT),
            '--run-id', run_id,
        ]
        result = run_child(cmd, pythonpath=[MODULE_PATHS['le_calc']])
        if result.returncode != 0:
            fail(f'计算失败（退出码 {result.returncode}）')
            if plan is not None:
                warn(f'staged 候选保留以便排查：{relative(work_dir, ROOT)}（正式目录未创建）')
            return EXIT_FAIL
        print()
        finalize = _finalize_run_report(
            case,
            work_dir,
            endpoint=_recompute_endpoint(args),
            use_cache=(
                not getattr(args, 'no_report_cache', False)
                and not getattr(args, 'no_cache', False)
                and plan is None
            ),
        )
        checks: list[dict[str, Any]] = []
        ok = True
        try:
            checks, ok = _check_expected(case, work_dir)
        except PipelineError as exc:
            warn(f'未做预期值核对：{exc}')
            if plan is None:
                return EXIT_OK
        if plan is not None:
            if not ok:
                fail('staged 运行数值核对未通过，拒绝发布')
                warn(f'staged 候选保留：{relative(work_dir, ROOT)}（正式目录未创建）')
                return EXIT_FAIL
            document = _publish_staged_run(plan)
            if document is None:
                warn(f'staged 候选保留以便处理：{relative(work_dir, ROOT)}')
                return EXIT_FAIL
            print(f'运行目录：{relative(final_dir, ROOT)}')
            print(f"HTML 报告：{relative(final_dir / 'black-hole-damage.html', ROOT)}")
            if finalize is not None:
                print(f"重算端点：{finalize['endpoint']}")
            heading('数值核对')
            print_checks(checks)
            return EXIT_OK
        print(f'运行目录：{relative(final_dir, ROOT)}')
        print(f"HTML 报告：{relative(final_dir / 'black-hole-damage.html', ROOT)}")
        if finalize is not None:
            print(f"重算端点：{finalize['endpoint']}")
        heading('数值核对')
        print_checks(checks)
        if ok:
            return EXIT_OK
        return EXIT_FAIL


    finally:
        _release_staging_plan(plan)

def _hit_case_artifacts(out_dir: 'Path') -> 'list[tuple[str, Path]]':
    return [('运行结果', out_dir / 'result.json'), ('离线报告', out_dir / 'flame-reave-damage.html'), ('运行清单', out_dir / 'run-manifest.json'), ('说明文档', out_dir / 'README.zh-CN.md'), ('条件 Buff 筛查', out_dir / 'buff-screening.json')]

def _run_hit_case(case: 'dict[str, Any]', *, run_id: 'str', out_dir: 'Path', snapshot: 'Path', calc_input: 'Path', ruleset: 'Path', recalc_endpoint: 'str | None'=None, use_report_cache: 'bool'=True, staged_plan: dict[str, Any] | None = None, stage_pipeline: 'bool'=False, pipeline_args: 'argparse.Namespace | None'=None) -> 'int':
    '''``le.ps1 run`` for ``case.model == "hit"``: hit model extension of the same run.

    The hit model reuses the existing pipeline contracts (case.json, BuildSpec,
    calculation-input, model.Snapshot, ledgers, run-manifest, offline report);
    ``hit-snapshot`` / ``hit-run`` below are internal steps, not a second CLI.
    '''

    if stage_pipeline and staged_plan is None and pipeline_args is not None:
        return _run_staged_stage_pipeline(
            case,
            run_id=run_id,
            plan=None,
            args=pipeline_args,
            is_hit=True,
            final_dir=out_dir,
        )
    heading(f'le run — {case.get('case_id')}（hit 模型：现有 run 管线的技能模型扩展）')
    calc_input_ok = calc_input.is_file()
    print(f'计算输入：{relative(calc_input, ROOT)}')
    print(f'规则包：  {relative(ruleset, ROOT)}')
    if not calc_input_ok:
        fail(f'计算输入不存在：{relative(calc_input, ROOT)}')
        return EXIT_FAIL
    if not ruleset.is_file():
        fail(f'hit 规则包不存在：{relative(ruleset, ROOT)}（需要 fr-engine 的 ruleset-fr-hit-stage1-v2.json）')
        return EXIT_FAIL
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_snapshot = False
    if not snapshot.is_file():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        heading('步骤 · hit 快照（le_calc hit-snapshot）')
        cmd = [_python(), '-m', 'le_calc', 'hit-snapshot', '--case', relative(case['_path'], ROOT), '--calc-input', relative(calc_input, ROOT), '--out', relative(snapshot, ROOT)]
        result = run_child(cmd, pythonpath=[MODULE_PATHS['le_calc']])
        if result.returncode != 0:
            fail(f'hit 快照生成失败（退出码 {result.returncode}）')
            return EXIT_FAIL
        generated_snapshot = True
    else:
        info(f'复用已有 hit 快照：{relative(snapshot, ROOT)}')
    scenario = None
    try:
        candidate = optional_case_path(case, 'inputs.scenario')
    except PipelineError:
        candidate = None
    if candidate is not None and candidate.is_file():
        scenario = candidate
    heading('步骤 · hit 计算与报告（le_calc hit-run）')
    cmd = [_python(), '-m', 'le_calc', 'hit-run', '--case', relative(case['_path'], ROOT), '--snapshot', relative(snapshot, ROOT), '--ruleset', relative(ruleset, ROOT), '--out', relative(out_dir, ROOT), '--run-id', run_id]
    if scenario is not None:
        cmd += ['--scenario', relative(scenario, ROOT)]
    result = run_child(cmd, pythonpath=[MODULE_PATHS['le_calc']])
    if result.returncode != 0:
        fail(f'hit 计算失败（退出码 {result.returncode}）；已保留部分产物：{relative(out_dir, ROOT)}')
        return EXIT_FAIL
    print()
    if recalc_endpoint is None:
        recalc_module, _ = _le_calc_modules()
        recalc_endpoint = recalc_module.loopback_endpoint(recalc_module.DEFAULT_PORT)
    finalize = _finalize_run_report(case, out_dir, endpoint=recalc_endpoint, use_cache=use_report_cache)
    if staged_plan is None:
        print(f'运行目录：{relative(out_dir, ROOT)}')
        for label, path in _hit_case_artifacts(out_dir):
            if path.is_file():
                print(f'{label}：{relative(path, ROOT)}')
    if generated_snapshot:
        info(f'本轮生成了 hit 快照：{relative(snapshot, ROOT)}')
    if finalize is not None and staged_plan is None:
        print(f'重算端点：{finalize['endpoint']}')
    try:
        manifest = load_json(out_dir / 'run-manifest.json', what='hit run-manifest')
    except PipelineError as exc:
        fail(f'run-manifest 不可读：{exc}')
        return EXIT_FAIL
    consistency = manifest.get('consistency') or {}
    run_block = manifest.get('run') or {}
    if not consistency.get('all_pass'):
        failed = sorted((key for key, value in consistency.items() if value is False))
        fail(f'hit run 一致性校验未通过：{', '.join(failed)}')
        return EXIT_FAIL
    if run_block.get('status') == 'assumption':
        warn('status=assumption：规则包仍含假设/未解决项（见 result.json 的 rule_ledger / issues）')
    if staged_plan is not None:
        document = _publish_staged_run(staged_plan)
        if document is None:
            warn(f'staged 候选保留以便处理：{relative(out_dir, ROOT)}')
            return EXIT_FAIL
        final_dir = staged_plan['final_dir']
        print(f'运行目录：{relative(final_dir, ROOT)}')
        for label, path in _hit_case_artifacts(final_dir):
            if path.is_file():
                print(f'{label}：{relative(path, ROOT)}')
        if finalize is not None:
            print(f'重算端点：{finalize['endpoint']}')
    print(f'输入指纹：{json.dumps(manifest.get('input', {}).get('fingerprints') or {}, ensure_ascii=False)}')
    return EXIT_OK

def cmd_serve(args: 'argparse.Namespace') -> 'int':
    try:
        case = _load_case(args)
        calculation_route = _route_for_case(case)
        if case.get('dps_profile') or calculation_route.get('engine') != 'legacy':
            _le_calc_modules()
            from le_calc import dps_report
            report_arg = getattr(args, 'report', None) or case.get('output', {}).get('canonical_run_id')
            if not report_arg:raise PipelineError('请用 --report 指定要打开的报告')
            report = resolve(ROOT, report_arg) if '/' in report_arg or '\\' in report_arg else ROOT / case['output']['root'] / report_arg
            dps_report.serve(ROOT, case, report, int(args.port or 8765))
            return EXIT_OK
        _require_known_model(case, command='serve')
        port = int(args.port or 8765)
        if not 1 <= port <= 65535:
            raise PipelineError(f'端口不合法：{port}')
        endpoint = _recompute_endpoint(args)
        recalc_module, _capabilities_module = _le_calc_modules()
        serve_port = recalc_module.endpoint_port(endpoint)
        run_dir, canonical = _select_report(case, args)
        snapshot = case_path(case, 'inputs.snapshot')
        calc_input = case_path(case, 'inputs.calculation_input')
        build_doc = case_path(case, 'inputs.bd')
        ruleset = case_path(case, 'ruleset.path')
        scenario_path = optional_case_path(case, 'inputs.scenario')
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_ARGS
    model_id = _case_model(case)
    print(f'正在启动本地条件重算服务（仅回环 127.0.0.1:{serve_port}，Ctrl+C 停止）……', flush=True)
    print(f'所选报告：{relative(run_dir, ROOT)}（{'按案例默认规范 run 选择' if canonical else '按 --report 显式选择'}）')
    print(f'重算端点：{endpoint}')
    problems = _serve_report_checks(case, run_dir, endpoint=endpoint, canonical=canonical)
    if problems:
        for problem in problems:
            fail(problem)
        fail(f'报告元数据与本次服务绑定不一致。可选处理：（1）按报告原端口启动：le.ps1 serve --case {relative(case['_path'], ROOT)} --report {run_dir.name} --port <报告原端口>；（2）生成新 run（绑定本次端口）：le.ps1 run --case {relative(case['_path'], ROOT)} --port {serve_port}')
        return EXIT_FAIL
    metadata = _report_metadata(run_dir)
    manifest_path = run_dir / 'run-manifest.json'
    manifest = load_json(manifest_path, what='run manifest') if manifest_path.is_file() else {}
    fingerprints = _report_fingerprints(manifest)
    declared = _case_input_fingerprints(case)
    for key, value in declared.items():
        if fingerprints.get(key):
            continue
        if value:
            fingerprints[key] = value
    env_extra = {'LE_RECALC_ENDPOINT': endpoint, 'LE_RECALC_FINGERPRINTS': json.dumps(fingerprints, ensure_ascii=False), 'LE_REPO_ROOT': str(ROOT)}
    result_path = run_dir / 'result.json'
    if result_path.is_file():
        env_extra['LE_REPORT_RESULT'] = relative(result_path, ROOT)
    capabilities_path = run_dir / _REPORT_CAPABILITIES_FILE
    try:
        _recalc_module, capabilities_module = _le_calc_modules()
        renderer_fp = str((metadata.get('renderer_fingerprint') if metadata else None) or capabilities_module.renderer_fingerprint(ROOT))
        report_ref = _build_report_ref(case, run_dir, endpoint=endpoint, fingerprints=fingerprints, renderer_fp=renderer_fp, model_id=model_id, canonical=canonical)
        env_extra['LE_REPORT_REF'] = json.dumps(report_ref, ensure_ascii=False)
        env_extra['LE_CASE_ID'] = str(case['case_id'])
        if metadata.get('capabilities'):
            env_extra['LE_REPORT_CAPABILITIES'] = json.dumps(metadata['capabilities'], ensure_ascii=False)
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_FAIL
    if model_id == 'hit':
        cmd = [_python(), '-m', 'le_calc', 'hit-serve', '--case', str(args.case), '--snapshot', str(snapshot), '--ruleset', str(ruleset), '--run-id', str(case.get('output', {}).get('canonical_run_id') or 'serve'), '--port', str(port)]
        if scenario_path is not None and scenario_path.is_file():
            cmd += ['--scenario', str(scenario_path)]
    else:
        cmd = [_python(), '-m', 'le_calc', 'serve', '--snapshot', str(snapshot), '--ruleset', str(ruleset), '--calc-input', str(calc_input), '--build', str(build_doc), '--monster-level', str(int(case.get('monster_level') or 100)), '--registry', str(case_path(case, 'conditional_registry.path')), '--run-id', run_dir.name, '--port', str(port)]
    result = run_child(cmd, pythonpath=[MODULE_PATHS['le_calc']], env_extra=env_extra)
    return int(result.returncode)

def _write_case_skeleton(case_dir: 'Path', payload: 'dict[str, Any]') -> 'Path':
    from _common import write_json
    path = case_dir / 'case.json'
    write_json(path, payload)
    return path

def cmd_import(args: 'argparse.Namespace') -> 'int':
    started = time.perf_counter()
    complete_import = bool(getattr(args, 'main_skill', None) or getattr(args, 'model', None))
    if complete_import:
        from le_pipeline import onboarding
        try:
            onboarding.preflight(sys.modules[__name__], args)
        except (PipelineError, OSError, ValueError) as exc:
            fail(str(exc))
            return EXIT_ARGS
    try:
        target = resolve(ROOT, args.case)
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_ARGS
    if target.exists():
        fail(f'目标案例已存在，拒绝覆盖：{relative(target, ROOT)}')
        return EXIT_FAIL
    case_dir = target.parent
    if case_dir.exists() and any(case_dir.iterdir()):
        fail(f'目标案例目录非空，拒绝覆盖：{relative(case_dir, ROOT)}')
        return EXIT_FAIL
    if not args.save and not args.planner:
        fail('import 需要 --save（角色存档）或 --planner（planner payload）作为来源')
        return EXIT_ARGS
    try:
        case_dir.mkdir(parents=True, exist_ok=not complete_import)
    except FileExistsError:
        fail('目标案例目录已存在，拒绝并发覆盖')
        return EXIT_FAIL
    inputs_dir = case_dir / 'inputs'
    inputs_dir.mkdir(exist_ok=True)
    if complete_import:
        import shutil
        frozen = case_dir / 'source'
        frozen.mkdir()
        args = argparse.Namespace(**vars(args))
        for field in ('save', 'stash'):
            value = getattr(args, field, None)
            if value:
                original = resolve(ROOT, value)
                copied = frozen / (field + '-' + original.name)
                shutil.copyfile(original, copied)
                setattr(args, field, relative(copied, ROOT))
    config = load_config()
    case_id = target.parent.name
    source = {}
    partial = False
    try:
        if args.save:
            save_path = resolve(ROOT, args.save)
            if args.planner:
                planner_copy = inputs_dir / 'planner-build-response.json'
                import shutil
                shutil.copyfile(resolve(ROOT, args.planner), planner_copy)
                source['planner_build_response'] = relative(planner_copy, ROOT)
            cmd = [_python(), '-m', 'le_save_import', 'build', '--save', str(save_path), '--out', str(inputs_dir)]
            if args.planner:
                cmd += ['--planner', str(resolve(ROOT, args.planner))]
            if args.stash:
                cmd += ['--stash', str(resolve(ROOT, args.stash))]
                source['stash'] = relative(resolve(ROOT, args.stash), ROOT)
            source['character_save'] = relative(save_path, ROOT)
            result = run_child(cmd, pythonpath=[MODULE_PATHS['le_save_import']])
            if result.returncode != 0:
                raise PipelineError(f'le_save_import build 失败（退出码 {result.returncode}）')
        else:
            planner_copy = inputs_dir / 'planner-build-response.json'
            import shutil
            shutil.copyfile(resolve(ROOT, args.planner), planner_copy)
            source['planner_build_response'] = relative(planner_copy, ROOT)
            partial = True
    except PipelineError as exc:
        fail(str(exc))
        fail(f'部分案例目录已创建：{relative(case_dir, ROOT)}')
        return EXIT_FAIL
    bd_path = inputs_dir / 'bd.json'
    payload = {'schema': 'le-case-v1', 'case_id': case_id, 'title': f'{case_id}（待完善）', 'description': '由 le.ps1 import 生成的案例骨架：补齐计算输入、快照、人工补值、场景与预期摘要后再运行。', 'created': _dt.date.today().isoformat(), 'game_version': '1.5.2', 'monster_level': 100, 'dataset': (config.get('dataset') or {}).get('pointer') or 'data/current.json', 'dataset_expected': {}, 'build': {}, 'affix_db': config.get('affix_db') or 'data/affix-db/v1/normalized/affix-db.sqlite', 'ruleset': {'path': config.get('ruleset') or ''} if complete_import and args.model == 'black-hole' else {}, 'conditional_registry': {'path': config.get('conditional_registry') or ''} if complete_import and args.model == 'black-hole' else {}, 'inputs': {'bd': relative(bd_path, ROOT) if bd_path.is_file() else None, 'calculation_input': None, 'corrections': None, 'idol_refraction_selection': None, 'snapshot': None, 'scenario': None}, 'source': source, 'input_sha256': {}, 'snapshot_expectations': {}, 'output': {'root': f'reports/{case_id}', 'run_id_template': '{case_id}-{utcstamp}'}, 'expected': None, 'notes': ['这是新 BD 的案例骨架；旧样例的 corrections.json 补值与折射选择不得直接复用。', '下一步：发布/选择 BuildSpec revision → le_build export → 人工补值 → make_snapshot → 填写 snapshot_expectations 与 expected/expected-summary.json。']}
    case_file = _write_case_skeleton(case_dir, payload)
    if complete_import:
        return onboarding.complete(sys.modules[__name__], args, started=started)
    heading('le import — 新案例目录')
    print(f'案例目录：{relative(case_dir, ROOT)}')
    print(f'案例配置：{relative(case_file, ROOT)}')
    if (inputs_dir / 'bd.json').is_file():
        print(f'BD 文档：{relative(inputs_dir / 'bd.json', ROOT)}')
    print()
    if partial:
        print('import：PARTIAL（仅复制了 planner payload；需要用 le_build import 发布 BuildSpec revision，再补齐案例字段）')
        return EXIT_PARTIAL
    print('import：OK（已生成 BD 文档；请按 docs/workflow.md 补齐 BuildSpec revision、计算输入、快照与预期摘要）')
    return EXIT_OK

_RAN_RE = re.compile('Ran (\\d+) tests? in')
_SKIP_RE = re.compile('skipped=(\\d+)')
_FAILED_RE = re.compile('FAILED \\(([^)]*)\\)')

def _run_suite(name: 'str') -> 'dict[str, Any]':
    spec = SUITES[name]
    started = time.time()
    result = run_child([_python(), *spec['args']], pythonpath=spec['pythonpath'], cwd=spec['cwd'], capture=True)
    output = (result.stdout or '') + (result.stderr or '')
    ran = _RAN_RE.search(output)
    skipped = _SKIP_RE.search(output)
    failed = _FAILED_RE.search(output)
    ok = result.returncode == 0 and 'OK' in output and not failed
    return {'suite': name, 'label': spec['label'], 'status': 'PASS' if ok else 'FAIL', 'ran': int(ran.group(1)) if ran else None, 'skipped': int(skipped.group(1)) if skipped else 0, 'failed': failed.group(1) if failed else '', 'seconds': time.time() - started, 'output': output}

def cmd_test(args: 'argparse.Namespace') -> 'int':
    requested = args.suite or 'all'
    names = list(SUITES) if requested == 'all' else [requested]
    if any((name not in SUITES for name in names)):
        fail(f'未知测试套件：{requested}（可选：{', '.join(SUITES)}，或 all）')
        return EXIT_ARGS
    heading('le test — 回归测试')
    results = []
    for name in names:
        info(f'运行 {SUITES[name]['label']} ……')
        results.append(_run_suite(name))
        entry = results[-1]
        print(f'  {'PASS' if entry['status'] == 'PASS' else 'FAIL'}  {name}: ran={entry['ran']} skipped={entry['skipped']} ({entry['seconds']:.1f}s){' ' + entry['failed'] if entry['failed'] else ''}')
        if entry['status'] != 'PASS':
            tail = [line for line in entry['output'].splitlines() if line.strip()][-25:]
            print('  ---- 失败输出（末 25 行）----')
            for line in tail:
                print(f'  {line}')
    print()
    failed_suites = [entry['suite'] for entry in results if entry['status'] != 'PASS']
    print('汇总：' + '，'.join((f'{entry['suite']}={'PASS' if entry['status'] == 'PASS' else 'FAIL'}({entry['ran']} tests, skipped {entry['skipped']})' for entry in results)))
    if failed_suites:
        print(f'test：FAIL（失败套件：{', '.join(failed_suites)}）', file=sys.stderr)
        return EXIT_FAIL
    print('test：PASS（全部套件通过；跳过项见上）')
    return EXIT_OK

def cmd_package(args: 'argparse.Namespace') -> 'int':
    out = args.out
    zip_path = args.zip_path
    cmd = [_python(), str(ROOT / 'scripts' / 'package.py')]
    if out:
        candidate = Path(out)
        if candidate.suffix.lower() == '.zip':
            cmd += ['--out', str(resolve(ROOT, candidate.with_suffix('')))]
            if not zip_path:
                zip_path = str(candidate)
        else:
            cmd += ['--out', str(resolve(ROOT, candidate))]
    if zip_path:
        cmd += ['--zip', str(resolve(ROOT, zip_path))]
    result = run_child(cmd, pythonpath=[])
    return int(result.returncode)

_KNOWLEDGE_SRC = ROOT / 'tools' / 'knowledge' / 'src'

def _knowledge_cli():
    '''延迟导入独立知识查询 CLI（引擎不依赖本编排模块的全局状态）。'''

    if str(_KNOWLEDGE_SRC) not in sys.path:
        sys.path.insert(0, str(_KNOWLEDGE_SRC))
    from le_knowledge import cli as knowledge_cli
    return knowledge_cli

def cmd_knowledge(args: 'argparse.Namespace') -> 'int':
    '''``le.ps1 knowledge search|show|rebuild`` —— 转发给 tools/knowledge 的独立 CLI。'''

    argv = list(getattr(args, 'knowledge_args', None) or [])
    try:
        knowledge_cli = _knowledge_cli()
    except ImportError as exc:
        fail(f'无法导入 tools/knowledge/src/le_knowledge：{exc}')
        return EXIT_FAIL
    return int(knowledge_cli.main(argv))

def cmd_editor(args):
    """Launch the independent local BD editor without mutating calculator runs."""
    import importlib.util
    editor_path = ROOT / 'tools' / 'bd_editor' / 'service.py'
    spec = importlib.util.spec_from_file_location('le_bd_editor_service', editor_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.serve(root=ROOT, port=args.port, tool_home=args.tool_home or os.environ.get('LE_TOOL_NATIVE_HOME'))
    return EXIT_OK


COMMANDS = ('editor', 'doctor', 'reproduce', 'run', 'serve', 'prepare', 'import', 'validate', 'test', 'package', 'knowledge')

def build_parser() -> 'argparse.ArgumentParser':
    parser = argparse.ArgumentParser(prog='le.ps1', description='LastEpoch-BuildSimulator 统一操作入口（统一由 scripts/le_commands.py 实现）')
    sub = parser.add_subparsers(dest='command')
    doctor = sub.add_parser('doctor', help='检查环境、模块导入与运行必需数据')
    doctor.add_argument('--case', default=None, help='用于自检的案例配置')
    doctor.set_defaults(func=cmd_doctor)
    reproduce = sub.add_parser('reproduce', help='从原始输入重建整条链并核对关键数值（仅旧 profile/hit 兼容；unified definitions_v3/plan-v4 为 unsupported_operation）')
    reproduce.add_argument('--case', default=None, help='案例配置（必须显式指定；不回退 default_case）')
    reproduce.add_argument('--out', dest='out_root', default=None, help='运行输出根目录（默认取案例 output.root）')
    reproduce.add_argument('--run-id', default=None, help='运行 id（默认按案例模板生成新的时间戳 id）')
    reproduce.add_argument('--port', type=int, default=8765, help='报告内嵌的本机重算端口（默认 8765；报告缓存键含端口）')
    reproduce.add_argument('--recalc-endpoint', default=None, help='显式重算端点（默认由 --port 生成 http://127.0.0.1:<port>/recompute）')
    reproduce.add_argument('--no-report-cache', action='store_true', help='不使用 data/cache/reports 报告缓存')
    reproduce.set_defaults(func=cmd_reproduce)
    run = sub.add_parser('run', help='仅从冻结输入执行计算（快速路径）')
    run.add_argument('--case', default=None, help='案例配置')
    run.add_argument('--out', default=None, help='输出目录（默认 reports/<case-id>/<run-id>）')
    run.add_argument('--run-id', default=None, help='运行 id')
    run.add_argument('--port', type=int, default=8765, help='报告内嵌的本机重算端口（默认 8765；报告缓存键含端口）')
    run.add_argument('--recalc-endpoint', default=None, help='显式重算端点（默认由 --port 生成 http://127.0.0.1:<port>/recompute）')
    run.add_argument('--no-report-cache', action='store_true', help='不使用 data/cache/reports 报告缓存')
    run.add_argument('--no-cache', action='store_true', help='禁用缓存：同时禁用阶段缓存（simulate/validate_result/render_report 强制执行）与报告缓存')
    run.add_argument('--publish', choices=('none', 'staged'), default='none', help='发布模式：none=在最终目录原地执行阶段图（默认，无 operation 记录）；staged=同卷候选执行阶段图后用 commit_operation 原子发布（幂等/可 replay）')
    run.add_argument('--operations-root', default=None, help='staged 发布操作记录根目录（默认 data/cache/operations）')
    run.set_defaults(func=cmd_run)
    prepare = sub.add_parser('prepare', help='阶段化准备管线（原始输入 → 快照 + 分析产物）')
    prepare.add_argument('--case', default=None, help='案例配置（必须显式指定；不回退 default_case）')
    prepare.add_argument('--out', dest='out_root', default=None, help='运行输出根目录（默认取案例 output.root）')
    prepare.add_argument('--run-id', default=None, help='运行 id（默认按案例模板生成；--resume 时必须提供）')
    prepare.add_argument('--resume', action='store_true', help='按 pipeline-state 恢复：校验产物/指纹/缓存，失效阶段及下游重跑')
    prepare.add_argument('--no-cache', action='store_true', help='P4：跳过阶段缓存读取（强制执行），成功后仍刷新写缓存；PIPELINE_CACHE_DISABLE 环境变量同义')
    prepare.add_argument('--cache-root', default=None, help='P4：阶段缓存根目录（默认 data/cache/pipeline；也可用 LE_PIPELINE_CACHE_ROOT 环境变量）')
    prepare.add_argument('--save', default=None, help='角色存档路径（覆盖案例 source.character_save）')
    prepare.add_argument('--planner', default=None, help='planner payload 路径（覆盖案例 source.planner_build_response）')
    prepare.add_argument('--stash', default=None, help='STASH 存档路径（覆盖案例 source.stash）')
    prepare.add_argument('--main-skill', default=None, help='主技能 ability id（未知技能允许：只做数据准备 + readiness）')
    prepare.set_defaults(func=cmd_prepare)
    serve = sub.add_parser('serve', help='启动本机回环条件重算服务')
    serve.add_argument('--case', default=None, help='案例配置')
    serve.add_argument('--port', type=int, default=8765, help='回环端口（默认 8765）')
    serve.add_argument('--report', default=None, help='显式选择要服务的报告（run-id 或 run 目录）；默认取案例 output.canonical_run_id 并输出所选报告')
    serve.add_argument('--recalc-endpoint', default=None, help='显式重算端点（默认由 --port 生成；须与报告元数据一致）')
    serve.set_defaults(func=cmd_serve)
    import_cmd = sub.add_parser('import', help='为另一个 BD 创建/准备案例目录（拒绝覆盖）')
    import_cmd.add_argument('--case', required=True, help='新案例配置路径（必须不存在）')
    import_cmd.add_argument('--save', default=None, help='角色存档路径')
    import_cmd.add_argument('--planner', default=None, help='planner payload 路径')
    import_cmd.add_argument('--stash', default=None, help='STASH 存档路径（可选）')
    import_cmd.add_argument('--main-skill', default=None, help='用户确认的主技能 ID；须配合 --model，未知技能用 research')
    import_cmd.add_argument('--model', choices=('black-hole', 'research'), default=None, help='显式选择已有模型并自动接入到报告；不传则保留骨架导入')
    import_cmd.add_argument('--tool-home', default=None, help='tool-native 安装目录（或 LE_TOOL_NATIVE_HOME）')
    import_cmd.add_argument('--port', type=int, default=8765, help='完整首接报告的重算端口')
    import_cmd.add_argument('--run-id', default=None, help='完整首接的最终报告运行 id')
    import_cmd.set_defaults(func=cmd_import)
    validate = sub.add_parser('validate', help='结构/绑定校验（不重新计算）')
    validate.add_argument('--case', default=None, help='案例配置')
    validate.add_argument('--run-dir', default=None, help='统一路径：校验指定 run 的计划/场景/结果/账本绑定')
    validate.set_defaults(func=cmd_validate)
    test = sub.add_parser('test', help='运行回归测试套件')
    test.add_argument('--suite', default='all', help='ingest|build_import|save_import|affix_db|phase3|knowledge|all')
    test.set_defaults(func=cmd_test)
    package = sub.add_parser('package', help='按显式清单打包公开版发布包（含 manifest 与 zip；排除案例/报告/构筑/dataset 大文件）')
    package.add_argument('--out', default=None, help='输出目录（默认 dist/<name>-<version>；也接受 .zip 路径）')
    package.add_argument('--zip', dest='zip_path', default=None, help='zip 路径（默认与输出目录同名 .zip）')
    package.set_defaults(func=cmd_package)
    knowledge = sub.add_parser('knowledge', help='知识索引与查询（search/show/rebuild；实现位于 tools/knowledge）', description='知识索引与查询：search（短摘要）/ show（详情）/ rebuild（重建索引）。')
    knowledge.add_argument('knowledge_args', nargs=argparse.REMAINDER, help='子命令与参数，例如 search --query "Flame Reave"（用 knowledge search --help 查看）')
    knowledge.set_defaults(func=cmd_knowledge)
    editor = sub.add_parser('editor', help='启动本机 BD 配置工作台（装备/完整节点/多配置）')
    editor.add_argument('--port', type=int, default=8766, help='本机编辑平台端口（默认 8766）')
    editor.add_argument('--tool-home', default=None, help='兼容旧参数；编辑器统一显示英文游戏原文')
    editor.set_defaults(func=cmd_editor)
    return parser

def main(argv: 'list[str] | None'=None) -> 'int':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.print_help()
        return EXIT_ARGS
    try:
        return int(args.func(args))
    except PipelineError as exc:
        fail(str(exc))
        return EXIT_FAIL
    except KeyboardInterrupt:
        print('\n已中断。', file=sys.stderr)
        return EXIT_FAIL

if __name__ == '__main__':
    raise SystemExit(main())
