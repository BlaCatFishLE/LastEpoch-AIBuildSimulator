"""Render tunklab affix pages with headless Chrome for display verification.

This is an *optional, network-using* evidence step (separate from the offline
build).  It snapshots the post-hydration DOM so the "Scaled Values" table can be
compared cell-by-cell with the database (see :mod:`affix_db.verify_display`).

Input pages are untrusted; the DOM is only parsed as text.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .page_parse import parse_scaled_values_table

DEFAULT_CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


@dataclass
class RenderResult:
    slug: str
    url: str
    dom_path: Path
    screenshot_path: Path | None
    headers: list[str]
    row_count: int
    sha256: str
    rendered_at: str
    chrome: str
    window: str
    error: str | None = None


def find_chrome(explicit: str | None = None) -> Path:
    for candidate in (explicit,) + DEFAULT_CHROME_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    raise FileNotFoundError(
        "headless Chrome/Edge not found; set CHROME_PATH or install Chrome "
        "(this step is optional -- the offline build does not need a browser)"
    )


def _chrome_version(chrome: Path) -> str:
    try:
        result = subprocess.run([str(chrome), "--version"], capture_output=True, timeout=30)
        return (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _run_chrome(chrome: Path, args: list[str], profile_dir: Path, timeout: float) -> bytes:
    command = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--disable-extensions",
        "--user-data-dir=" + str(profile_dir),
        *args,
    ]
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode != 0 and not result.stdout:
        raise RuntimeError(
            f"headless chrome failed (exit {result.returncode}): "
            + (result.stderr.decode("utf-8", "replace")[-1500:])
        )
    return result.stdout


def render_pages(
    out_dir: Path | str,
    base_url: str = "https://lastepoch.tunklab.com",
    slugs: tuple[str, ...] = (),
    *,
    window: str = "1920,1080",
    virtual_time_budget_ms: int = 20000,
    timeout_seconds: float = 180.0,
    chrome_path: str | None = None,
    capture_screenshot_for: tuple[str, ...] = ("fire_resistance",),
    progress=print,
) -> dict:
    """Render each affix page, store DOM (+optional PNG) and a render manifest."""
    output = Path(out_dir)
    rendered_dir = output / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome(chrome_path)
    version = _chrome_version(chrome)
    results: list[RenderResult] = []
    for slug in slugs:
        url = f"{base_url}/affix/{slug}"
        profile_dir = Path(tempfile.mkdtemp(prefix=".chrome-profile-", dir=rendered_dir))
        try:
            progress(f"[render] {url}")
            try:
                dom_bytes = _run_chrome(
                    chrome,
                    [
                        f"--window-size={window}",
                        f"--virtual-time-budget={virtual_time_budget_ms}",
                        "--timeout=90000",
                        "--dump-dom",
                        url,
                    ],
                    profile_dir,
                    timeout_seconds,
                )
                dom_path = rendered_dir / f"{slug}.dom.html"
                dom_path.write_bytes(dom_bytes)
                sha = hashlib.sha256(dom_bytes).hexdigest()
                table = parse_scaled_values_table(dom_bytes.decode("utf-8", "replace"))
                headers = list(table.headers) if table else []
                row_count = len(table.rows) if table else 0
                screenshot_path = None
                if slug in capture_screenshot_for:
                    screenshot_path = (rendered_dir / f"{slug}.png").resolve()
                    _run_chrome(
                        chrome,
                        [
                            f"--window-size={window}",
                            f"--virtual-time-budget={virtual_time_budget_ms}",
                            "--timeout=90000",
                            f"--screenshot={screenshot_path}",
                            url,
                        ],
                        profile_dir,
                        timeout_seconds,
                    )
                results.append(
                    RenderResult(
                        slug=slug,
                        url=url,
                        dom_path=dom_path,
                        screenshot_path=screenshot_path,
                        headers=headers,
                        row_count=row_count,
                        sha256=sha,
                        rendered_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                        chrome=version,
                        window=window,
                    )
                )
                progress(f"[render] {slug}: {len(headers)} columns, {row_count} rows")
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                results.append(
                    RenderResult(
                        slug=slug,
                        url=url,
                        dom_path=rendered_dir / f"{slug}.dom.html",
                        screenshot_path=None,
                        headers=[],
                        row_count=0,
                        sha256="",
                        rendered_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                        chrome=version,
                        window=window,
                        error=str(exc),
                    )
                )
                progress(f"[render] ERROR {slug}: {exc}")
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)

    manifest = {
        "generator": "tools/affix_db/render_pages.py",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "chrome": version,
        "window": window,
        "virtual_time_budget_ms": virtual_time_budget_ms,
        "pages": [
            {
                "slug": result.slug,
                "url": result.url,
                "dom": result.dom_path.name,
                "dom_sha256": result.sha256,
                "screenshot": result.screenshot_path.name if result.screenshot_path else None,
                "tier_columns": [header for header in result.headers if header.lower().startswith("tier")],
                "row_count": result.row_count,
                "rendered_at": result.rendered_at,
                "error": result.error,
            }
            for result in results
        ],
        "errors": [result.slug for result in results if result.error],
    }
    (rendered_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
