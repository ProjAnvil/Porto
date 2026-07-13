#!/usr/bin/env python3
"""
Porto Workflow Server – a lightweight HTTP server that serves all Porto
workflows with a browser-based viewer supporting real-time editing.

Usage:
  python porto_server.py [--port 8090] [--porto-home <path>]

The server reads markdown files directly from ~/.porto/workflows/ and
renders them in the browser using marked.js + mermaid.js.

Routes:
  GET  /                                    Workflow list (SPA)
  GET  /workflow/{id}                       Workflow detail (SPA)
  GET  /api/workflows                       List all workflows (JSON)
  GET  /api/workflows/{id}/step/{n}         Read step markdown
  GET  /api/workflows/{id}/step/4/{sub}     Read subsystem spec markdown
  POST /api/workflows/{id}/step/{n}         Save step markdown
  POST /api/workflows/{id}/step/4/{sub}     Save subsystem spec markdown
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Porto home helpers (mirrors porto_workflow.py)
# ---------------------------------------------------------------------------
_porto_home: Path | None = None


def _get_porto_home() -> Path:
    global _porto_home
    if _porto_home is not None:
        return _porto_home
    return Path(os.environ.get("PORTO_HOME", Path.home() / ".porto"))


def _set_porto_home(path: Path) -> None:
    global _porto_home
    _porto_home = path


def _get_workflows_dir() -> Path:
    return _get_porto_home() / "workflows"


# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------


def _extract_sections(md_text: str) -> list[dict[str, str]]:
    """Split markdown by ## headings, return list of {id, title, content_preview}."""
    sections: list[dict[str, str]] = []
    parts = re.split(r"(?m)^## +", md_text)
    for part in parts[1:]:  # skip preamble before first ##
        lines = part.split("\n", 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        sid = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        preview = body[:200] + ("..." if len(body) > 200 else "")
        sections.append({"id": sid, "title": title, "content_preview": preview})
    return sections


def _extract_subsystems_from_md(md_text: str) -> list[dict[str, Any]]:
    """Best-effort extraction of subsystem info from step2 markdown."""
    subsystems: list[dict[str, Any]] = []
    blocks = re.split(r"(?m)^### +", md_text)
    for block in blocks[1:]:
        lines = block.split("\n", 1)
        name = lines[0].strip().strip("`").strip()
        body = lines[1] if len(lines) > 1 else ""
        type_match = re.search(
            r"\|\s*\*?\*?Type\*?\*?\s*\|\s*(\w+)\s*\|", body, re.IGNORECASE
        )
        resp_match = re.search(
            r"\|\s*\*?\*?Responsibilit(?:y|ies)\*?\*?\s*\|\s*([^|]+?)\s*\|",
            body,
            re.IGNORECASE,
        )
        subsystems.append(
            {
                "name": name,
                "type": type_match.group(1) if type_match else "unknown",
                "responsibility": resp_match.group(1).strip() if resp_match else "",
            }
        )
    return subsystems


def _extract_subsystem_specs(step_dir: Path) -> list[dict[str, Any]]:
    """Read each subsystem spec from md/step4/{subsystem}/REQUIREMENTS.md."""
    specs: list[dict[str, Any]] = []
    if not step_dir.exists():
        return specs
    for child in sorted(step_dir.iterdir()):
        req = child / "REQUIREMENTS.md"
        if child.is_dir() and req.exists():
            text = req.read_text(encoding="utf-8")
            sections = _extract_sections(text)
            specs.append(
                {
                    "name": child.name,
                    "sections": [s["title"] for s in sections],
                }
            )
    return specs


def _html_escape(s: str) -> str:
    """Escape HTML special characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


# ---------------------------------------------------------------------------
# Workflow data helpers
# ---------------------------------------------------------------------------

STEP_MD_FILES = {
    1: "md/step1_understanding.md",
    2: "md/step2_subsystems.md",
    3: "md/step3_context.md",
}

STEP_NAMES = {
    1: "Business Understanding",
    2: "Subsystem Identification",
    3: "Context Generation",
    4: "Subsystem Specification",
}


def _load_workflow_json(wf_dir: Path) -> dict:
    wf_file = wf_dir / "workflow.json"
    if not wf_file.exists():
        return {}
    with open(wf_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _list_all_workflows() -> list[dict[str, Any]]:
    """List all workflows with summary info."""
    wf_root = _get_workflows_dir()
    if not wf_root.exists():
        return []
    workflows = []
    for d in sorted(wf_root.iterdir()):
        wf_file = d / "workflow.json"
        if d.is_dir() and wf_file.exists():
            try:
                wf = json.loads(wf_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            # Build step status summary
            steps_info = wf.get("steps", {})
            step_statuses = {}
            for n in range(1, 5):
                info = steps_info.get(str(n), {})
                step_statuses[n] = info.get("status", "not_started")
            completed = sum(1 for s in step_statuses.values() if s == "completed")
            workflows.append(
                {
                    "workflow_id": wf.get("workflow_id", d.name),
                    "project_name": wf.get("project_name", wf.get("name", d.name)),
                    "status": wf.get("status", "unknown"),
                    "created_at": wf.get("created_at", ""),
                    "step_statuses": step_statuses,
                    "completed_steps": completed,
                    "total_steps": 4,
                    "subsystems": [
                        s.get("name", s) if isinstance(s, dict) else s
                        for s in wf.get("subsystems", [])
                    ],
                }
            )
    return workflows


def _get_step_md_path(wf_dir: Path, step: int) -> Path | None:
    """Return the markdown file path for a step (1-3)."""
    rel = STEP_MD_FILES.get(step)
    if rel is None:
        return None
    p = wf_dir / rel
    return p if p.exists() else None


def _get_subsystem_spec_path(wf_dir: Path, subsystem: str) -> Path | None:
    """Return the REQUIREMENTS.md path for a subsystem."""
    # Security: reject path traversal
    if ".." in subsystem or "/" in subsystem or "\\" in subsystem:
        return None
    p = wf_dir / "md" / "step4" / subsystem / "REQUIREMENTS.md"
    return p if p.exists() else None


def _get_step_info(wf_dir: Path, workflow: dict, step: int) -> dict[str, Any]:
    """Build detailed info for a single step."""
    steps = workflow.get("steps", {})
    info = steps.get(str(step), {})
    result: dict[str, Any] = {
        "step": step,
        "name": STEP_NAMES.get(step, f"Step {step}"),
        "status": info.get("status", "not_started"),
        "started_at": info.get("started_at"),
        "completed_at": info.get("completed_at"),
    }
    if step <= 3:
        md_path = _get_step_md_path(wf_dir, step)
        result["has_content"] = md_path is not None
        if md_path is not None:
            text = md_path.read_text(encoding="utf-8")
            result["sections"] = _extract_sections(text)
            if step == 2:
                result["subsystems"] = _extract_subsystems_from_md(text)
    elif step == 4:
        step4_dir = wf_dir / "md" / "step4"
        specs = _extract_subsystem_specs(step4_dir)
        result["has_content"] = len(specs) > 0
        result["subsystems"] = specs
    return result


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------


class PortoHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the Porto workflow server."""

    def log_message(self, format, *args):
        # Suppress default logging
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # API routes
        if path == "/api/workflows":
            self._json_response(_list_all_workflows())
            return

        # GET /api/workflows/{id}/step/{n}
        m = re.match(r"^/api/workflows/([^/]+)/step/(\d+)$", path)
        if m:
            self._handle_get_step(m.group(1), int(m.group(2)))
            return

        # GET /api/workflows/{id}/step/4/{subsystem}
        m = re.match(r"^/api/workflows/([^/]+)/step/4/([^/]+)$", path)
        if m:
            self._handle_get_subsystem_spec(m.group(1), m.group(2))
            return

        # GET /api/workflows/{id}
        m = re.match(r"^/api/workflows/([^/]+)$", path)
        if m:
            self._handle_get_workflow(m.group(1))
            return

        # All other GET → SPA HTML
        self._serve_spa()

    def do_POST(self):
        # POST /api/workflows/{id}/step/4/{subsystem}
        m = re.match(r"^/api/workflows/([^/]+)/step/4/([^/]+)$", self.path)
        if m:
            self._handle_save_subsystem_spec(m.group(1), m.group(2))
            return

        # POST /api/workflows/{id}/step/{n}
        m = re.match(r"^/api/workflows/([^/]+)/step/(\d+)$", self.path)
        if m:
            self._handle_save_step(m.group(1), int(m.group(2)))
            return

        self._error(404, "Not found")

    # --- API handlers ---

    def _handle_get_workflow(self, wf_id: str):
        wf_dir = _get_workflows_dir() / wf_id
        if not wf_dir.is_dir():
            self._error(404, f"Workflow not found: {wf_id}")
            return
        workflow = _load_workflow_json(wf_dir)
        steps = [_get_step_info(wf_dir, workflow, n) for n in range(1, 5)]
        self._json_response(
            {
                "workflow_id": workflow.get("workflow_id", wf_id),
                "project_name": workflow.get(
                    "project_name", workflow.get("name", wf_id)
                ),
                "status": workflow.get("status", "unknown"),
                "created_at": workflow.get("created_at", ""),
                "subsystems": [
                    s.get("name", s) if isinstance(s, dict) else s
                    for s in workflow.get("subsystems", [])
                ],
                "steps": steps,
            }
        )

    def _handle_get_step(self, wf_id: str, step: int):
        wf_dir = _get_workflows_dir() / wf_id
        if not wf_dir.is_dir():
            self._error(404, f"Workflow not found: {wf_id}")
            return
        if step == 4:
            # Step 4: return list of subsystem names
            step4_dir = wf_dir / "md" / "step4"
            specs = _extract_subsystem_specs(step4_dir)
            self._json_response({"step": 4, "subsystems": specs})
            return
        if step < 1 or step > 3:
            self._error(400, f"Invalid step: {step}")
            return
        md_path = _get_step_md_path(wf_dir, step)
        if md_path is None:
            self._error(404, f"Step {step} not available")
            return
        content = md_path.read_text(encoding="utf-8")
        self._text_response(content)

    def _handle_get_subsystem_spec(self, wf_id: str, subsystem: str):
        wf_dir = _get_workflows_dir() / wf_id
        if not wf_dir.is_dir():
            self._error(404, f"Workflow not found: {wf_id}")
            return
        md_path = _get_subsystem_spec_path(wf_dir, subsystem)
        if md_path is None:
            self._error(404, f"Spec not found: {subsystem}")
            return
        content = md_path.read_text(encoding="utf-8")
        self._text_response(content)

    def _handle_save_step(self, wf_id: str, step: int):
        if step < 1 or step > 3:
            self._error(400, f"Invalid step: {step}")
            return
        wf_dir = _get_workflows_dir() / wf_id
        if not wf_dir.is_dir():
            self._error(404, f"Workflow not found: {wf_id}")
            return
        body = self._read_body()
        if body is None:
            return
        rel = STEP_MD_FILES.get(step)
        if rel is None:
            self._error(400, f"Invalid step: {step}")
            return
        target = wf_dir / rel
        self._safe_write(wf_dir, target, body)

    def _handle_save_subsystem_spec(self, wf_id: str, subsystem: str):
        if ".." in subsystem or "/" in subsystem or "\\" in subsystem:
            self._error(403, "Invalid subsystem name")
            return
        wf_dir = _get_workflows_dir() / wf_id
        if not wf_dir.is_dir():
            self._error(404, f"Workflow not found: {wf_id}")
            return
        body = self._read_body()
        if body is None:
            return
        target = wf_dir / "md" / "step4" / subsystem / "REQUIREMENTS.md"
        self._safe_write(wf_dir, target, body)

    # --- SPA ---

    def _serve_spa(self):
        html = _get_spa_html()
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- Helpers ---

    def _read_body(self) -> str | None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 10 * 1024 * 1024:  # 10MB limit
            self._error(413, "Payload too large")
            return None
        raw = self.rfile.read(content_length)
        return raw.decode("utf-8")

    def _safe_write(self, wf_dir: Path, target: Path, content: str):
        """Write content to target after path traversal check."""
        try:
            target.resolve().relative_to(wf_dir.resolve())
        except ValueError:
            self._error(403, "Path traversal detected")
            return
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._json_response({"ok": True})

    def _json_response(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text_response(self, text: str, status: int = 200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str):
        self._json_response({"error": msg}, status)


# ---------------------------------------------------------------------------
# SPA HTML template
# ---------------------------------------------------------------------------

_SPA_HTML_CACHE: str | None = None


def _get_spa_html() -> str:
    global _SPA_HTML_CACHE
    if _SPA_HTML_CACHE is not None:
        return _SPA_HTML_CACHE
    _SPA_HTML_CACHE = _build_spa_html()
    return _SPA_HTML_CACHE


def _build_spa_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en" data-theme="nord">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Porto Workflow Viewer</title>
<link href="https://cdn.jsdelivr.net/npm/daisyui@5" rel="stylesheet" type="text/css" />
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<style>
  /* ---- Timeline ---- */
  .timeline-step { position: relative; padding-left: 2.5rem; padding-bottom: 1.25rem; cursor: pointer; }
  .timeline-step:last-child { padding-bottom: 0; }
  .timeline-step::before {
    content: ''; position: absolute; left: 0.6875rem; top: 1.75rem; bottom: 0;
    width: 2px; background: oklch(var(--bc)/0.15);
  }
  .timeline-step:last-child::before { display: none; }
  .timeline-dot {
    position: absolute; left: 0; top: 0.25rem; width: 1.5rem; height: 1.5rem;
    border-radius: 9999px; display: flex; align-items: center; justify-content: center;
    font-size: 0.7rem; font-weight: 700; color: #fff; z-index: 1;
  }
  .timeline-step.active .timeline-dot { box-shadow: 0 0 0 3px oklch(var(--p)/0.25); }

  /* ---- Markdown ---- */
  .markdown-body { line-height: 1.7; color: oklch(var(--bc)); }
  .markdown-body h1 { font-size: 1.5rem; font-weight: 700; margin: 1.5rem 0 0.75rem; padding-bottom: 0.5rem; border-bottom: 1px solid oklch(var(--bc)/0.1); }
  .markdown-body h2 { font-size: 1.25rem; font-weight: 600; margin: 1.25rem 0 0.5rem; padding-bottom: 0.4rem; border-bottom: 1px solid oklch(var(--bc)/0.08); }
  .markdown-body h3 { font-size: 1.1rem; font-weight: 600; margin: 1rem 0 0.5rem; }
  .markdown-body h4 { font-size: 1rem; font-weight: 600; margin: 0.75rem 0 0.25rem; }
  .markdown-body p { margin: 0.5rem 0; }
  .markdown-body table { width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.9rem; }
  .markdown-body th, .markdown-body td { border: 1px solid oklch(var(--bc)/0.1); padding: 0.5rem 0.75rem; text-align: left; }
  .markdown-body th { background: oklch(var(--b2)); font-weight: 600; }
  .markdown-body tr:nth-child(even) { background: oklch(var(--b2)/0.5); }
  .markdown-body code { background: oklch(var(--b2)); padding: 0.15rem 0.4rem; border-radius: 0.25rem; font-size: 0.85em; font-family: ui-monospace, 'SF Mono', monospace; }
  .markdown-body pre { background: oklch(var(--b2)); padding: 1rem; border-radius: 0.5rem; overflow-x: auto; margin: 1rem 0; }
  .markdown-body pre code { background: transparent; padding: 0; font-size: 0.85rem; }
  .markdown-body blockquote { border-left: 3px solid oklch(var(--p)/0.3); padding-left: 1rem; color: oklch(var(--bc)/0.7); margin: 1rem 0; font-style: italic; }
  .markdown-body ul, .markdown-body ol { padding-left: 1.5rem; margin: 0.5rem 0; }
  .markdown-body ul { list-style: disc; }
  .markdown-body ol { list-style: decimal; }
  .markdown-body li { margin: 0.25rem 0; }
  .markdown-body a { color: oklch(var(--p)); text-decoration: underline; }
  .markdown-body hr { margin: 1.5rem 0; border-color: oklch(var(--bc)/0.1); }
  .markdown-body img { border-radius: 0.5rem; max-width: 100%; margin: 1rem 0; }
  .mermaid { margin: 1rem 0; display: flex; justify-content: center; }
  .mermaid svg { max-width: 100%; }
</style>
</head>
<body class="bg-base-200 min-h-screen">
<div id="app"></div>
<div id="toast-container" class="toast toast-end toast-bottom z-50 hidden">
  <div class="alert alert-success shadow-lg"><span id="toast-msg"></span></div>
</div>

<script>
(function() {
  'use strict';
  mermaid.initialize({ startOnLoad: false, theme: 'default' });

  /* ======== Utility ======== */
  function esc(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;').replace(/'/g,'&#039;');
  }

  function showToast(msg, type) {
    var t = document.getElementById('toast-container');
    var a = t.querySelector('.alert');
    a.className = 'alert shadow-lg ' + (type === 'error' ? 'alert-error' : 'alert-success');
    document.getElementById('toast-msg').textContent = msg;
    t.classList.remove('hidden');
    setTimeout(function() { t.classList.add('hidden'); }, 2500);
  }

  async function renderMd(el, text) {
    el.innerHTML = (typeof marked !== 'undefined') ? marked.parse(text) : '<pre>' + esc(text) + '</pre>';
    if (typeof mermaid !== 'undefined') {
      var codes = el.querySelectorAll('code.language-mermaid');
      for (var i = 0; i < codes.length; i++) {
        var pre = codes[i].parentElement;
        var div = document.createElement('div');
        div.className = 'mermaid';
        div.textContent = codes[i].textContent;
        pre.replaceWith(div);
      }
      try { await mermaid.run({ nodes: el.querySelectorAll('.mermaid') }); } catch(e) {}
    }
  }

  /* Status helpers */
  var STATUS = {
    completed:   { label: 'Completed',    cls: 'badge-success',  dot: 'bg-success text-success-content', icon: '✓' },
    in_progress: { label: 'In Progress',  cls: 'badge-warning',  dot: 'bg-warning text-warning-content', icon: '◌' },
    failed:      { label: 'Failed',       cls: 'badge-error',    dot: 'bg-error text-error-content',     icon: '✗' },
    not_started: { label: 'Not Started',  cls: 'badge-ghost',    dot: 'bg-base-300 text-base-content/50', icon: '○' },
  };
  function st(s) { return STATUS[s] || STATUS.not_started; }

  /* ======== Router ======== */
  function getRoute() {
    var h = location.hash.replace('#', '') || '/';
    var m = h.match(/^\/workflow\/([^/]+)\/step\/(\d+)$/);
    if (m) return { view: 'workflow', id: m[1], step: parseInt(m[2]) };
    m = h.match(/^\/workflow\/([^/]+)$/);
    if (m) return { view: 'workflow', id: m[1], step: null };
    return { view: 'list' };
  }

  /* ======== Workflow List View ======== */
  async function renderList() {
    var app = document.getElementById('app');
    app.innerHTML = '<div class="navbar bg-base-100 shadow-sm sticky top-0 z-40">' +
      '<div class="flex-1 px-4"><a class="text-xl font-bold tracking-tight" href="#/">\u2693 Porto</a></div></div>' +
      '<div class="flex justify-center p-12"><span class="loading loading-spinner loading-lg text-primary"></span></div>';

    var resp = await fetch('/api/workflows');
    if (!resp.ok) { app.querySelector('.loading').outerHTML = '<p class="text-center text-base-content/50 p-20">Failed to load workflows.</p>'; return; }
    var workflows = await resp.json();

    if (workflows.length === 0) {
      app.innerHTML = '<div class="navbar bg-base-100 shadow-sm"><div class="flex-1 px-4"><a class="text-xl font-bold tracking-tight" href="#/">\u2693 Porto</a></div></div>' +
        '<div class="hero min-h-[60vh]"><div class="hero-content text-center"><div>' +
        '<div class="text-6xl mb-4 opacity-20">\u{1F4CB}</div>' +
        '<h2 class="text-2xl font-bold mb-2">No Workflows Yet</h2>' +
        '<p class="text-base-content/60 max-w-sm">Create your first PRD decomposition workflow with <kbd class="kbd kbd-sm">/porto gen</kbd></p>' +
        '</div></div></div>';
      return;
    }

    var cards = workflows.map(function(wf) {
      var completedSteps = 0;
      var stepDots = [1,2,3,4].map(function(n) {
        var s = st(wf.step_statuses[n]);
        if (wf.step_statuses[n] === 'completed') completedSteps++;
        return '<div class="tooltip" data-tip="Step ' + n + ': ' + s.label + '">' +
               '<div class="w-8 h-1.5 rounded-full ' + s.dot.split(' ')[0] + '"></div></div>';
      }).join('');
      var pct = Math.round(completedSteps / 4 * 100);
      var subsText = wf.subsystems.length > 0 ? wf.subsystems.slice(0, 4).join(', ') + (wf.subsystems.length > 4 ? ' +' + (wf.subsystems.length - 4) : '') : '';

      return '<div class="card bg-base-100 shadow-sm hover:shadow-lg transition-all duration-200 cursor-pointer border border-base-300 hover:border-primary/30 hover:-translate-y-0.5" ' +
             'onclick="location.hash=\'#/workflow/' + esc(wf.workflow_id) + '\'">' +
        '<div class="card-body p-5 gap-3">' +
          '<div class="flex items-start justify-between">' +
            '<div><h3 class="card-title text-base leading-tight">' + esc(wf.project_name) + '</h3>' +
            '<p class="font-mono text-xs text-base-content/40 mt-0.5">' + esc(wf.workflow_id).substring(0, 20) + '</p></div>' +
            '<span class="badge ' + st(wf.status).cls + ' badge-sm shrink-0">' + st(wf.status).label + '</span>' +
          '</div>' +
          '<div>' +
            '<div class="flex items-center gap-2 mb-1"><span class="text-xs text-base-content/50">Progress</span><span class="text-xs font-semibold">' + pct + '%</span></div>' +
            '<div class="flex gap-1.5">' + stepDots + '</div>' +
          '</div>' +
          (subsText ? '<div class="flex flex-wrap gap-1">' + wf.subsystems.slice(0, 4).map(function(s) { return '<span class="badge badge-outline badge-xs">' + esc(s) + '</span>'; }).join('') + (wf.subsystems.length > 4 ? '<span class="badge badge-ghost badge-xs">+' + (wf.subsystems.length - 4) + '</span>' : '') + '</div>' : '') +
          (wf.created_at ? '<p class="text-xs text-base-content/40">' + esc(wf.created_at) + '</p>' : '') +
        '</div></div>';
    }).join('');

    app.innerHTML =
      '<div class="navbar bg-base-100 shadow-sm sticky top-0 z-40">' +
        '<div class="flex-1 px-4"><a class="text-xl font-bold tracking-tight" href="#/">\u2693 Porto</a></div>' +
        '<div class="flex-none px-4"><div class="badge badge-primary badge-outline">' + workflows.length + ' workflow' + (workflows.length > 1 ? 's' : '') + '</div></div>' +
      '</div>' +
      '<div class="max-w-6xl mx-auto p-6 sm:p-8">' +
        '<h1 class="text-2xl font-bold mb-1">Workflows</h1>' +
        '<p class="text-sm text-base-content/50 mb-6">Browse and manage your PRD decomposition workflows</p>' +
        '<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">' + cards + '</div>' +
      '</div>';
  }

  /* ======== Workflow Detail View ======== */
  var _wfData = null;
  var _currentStep = 1;
  var _editMode = false;
  var _currentMdContent = null;
  var _currentSubsystem = null;

  async function renderWorkflow(id, stepNum) {
    var app = document.getElementById('app');
    app.innerHTML =
      '<div class="navbar bg-base-100 shadow-sm sticky top-0 z-40"><div class="flex-1 px-4">' +
      '<a class="font-bold" href="#/">\u2693 Porto</a></div></div>' +
      '<div class="flex justify-center items-center p-16"><span class="loading loading-spinner loading-lg text-primary"></span></div>';

    var resp = await fetch('/api/workflows/' + encodeURIComponent(id));
    if (!resp.ok) { app.innerHTML += '<div class="text-center p-20 text-base-content/50">Workflow not found.</div>'; return; }
    _wfData = await resp.json();
    _editMode = false;
    _currentMdContent = null;
    _currentSubsystem = null;

    if (stepNum && stepNum >= 1 && stepNum <= 4) {
      _currentStep = stepNum;
    } else {
      /* Auto-select first step with content, or last completed */
      _currentStep = 1;
      for (var i = 0; i < _wfData.steps.length; i++) {
        if (_wfData.steps[i].has_content) { _currentStep = _wfData.steps[i].step; break; }
      }
    }
    _renderWorkflowUI(id);
    _loadStepContent(id, _currentStep);
  }

  function _renderWorkflowUI(id) {
    var wf = _wfData;

    /* Sidebar timeline */
    var timelineHtml = wf.steps.map(function(s) {
      var active = s.step === _currentStep;
      var info = st(s.status);
      return '<div class="timeline-step' + (active ? ' active' : '') + '" onclick="window._selectStep(\'' + esc(id) + '\',' + s.step + ')">' +
        '<div class="timeline-dot ' + info.dot + '">' + s.step + '</div>' +
        '<div class="' + (active ? 'bg-primary/5 -mx-2 px-2 py-1.5 rounded-lg' : 'py-1') + '">' +
          '<div class="text-sm font-semibold ' + (active ? 'text-primary' : '') + '">' + esc(s.name) + '</div>' +
          '<div class="flex items-center gap-1.5 mt-0.5">' +
            '<span class="badge ' + info.cls + '" style="font-size:0.65rem;height:1.1rem;min-height:1.1rem;padding:0 0.4rem;">' + info.label + '</span>' +
          '</div>' +
        '</div></div>';
    }).join('');

    /* Workflow info card in sidebar */
    var completedCount = wf.steps.filter(function(s) { return s.status === 'completed'; }).length;
    var sidebarInfo =
      '<div class="px-3 pt-4 pb-3 border-b border-base-300">' +
        '<h3 class="font-bold text-sm truncate" title="' + esc(wf.project_name) + '">' + esc(wf.project_name) + '</h3>' +
        '<p class="font-mono text-xs text-base-content/40 truncate mt-0.5" title="' + esc(wf.workflow_id) + '">' + esc(wf.workflow_id) + '</p>' +
        '<div class="flex items-center gap-2 mt-2">' +
          '<progress class="progress progress-primary w-full h-1.5" value="' + completedCount + '" max="4"></progress>' +
          '<span class="text-xs font-semibold text-base-content/60 shrink-0">' + completedCount + '/4</span>' +
        '</div>' +
      '</div>';

    document.getElementById('app').innerHTML =
      /* Navbar */
      '<div class="navbar bg-base-100 shadow-sm sticky top-0 z-40">' +
        '<div class="flex-1 gap-2 px-4">' +
          '<a class="font-bold" href="#/">\u2693 Porto</a>' +
          '<span class="text-base-content/20">/</span>' +
          '<span class="font-semibold truncate max-w-xs">' + esc(wf.project_name) + '</span>' +
        '</div>' +
        '<div class="flex-none px-4"><a class="btn btn-ghost btn-sm gap-1" href="#/">' +
          '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>' +
          'Back</a></div>' +
      '</div>' +
      /* Body */
      '<div class="flex h-[calc(100vh-64px)]">' +
        '<aside class="w-64 min-w-56 bg-base-100 border-r border-base-300 flex flex-col overflow-y-auto shrink-0">' +
          sidebarInfo +
          '<div class="p-3 flex-1">' +
            '<p class="text-xs font-semibold text-base-content/40 uppercase tracking-wider mb-3 px-1">Steps</p>' +
            timelineHtml +
          '</div>' +
        '</aside>' +
        '<main class="flex-1 overflow-y-auto" id="content">' +
          '<div class="flex justify-center items-center p-12"><span class="loading loading-spinner text-primary"></span></div>' +
        '</main>' +
      '</div>';
  }

  window._selectStep = function(id, num) {
    _currentStep = num;
    _editMode = false;
    _currentMdContent = null;
    _currentSubsystem = null;
    _renderWorkflowUI(id);
    _loadStepContent(id, num);
    history.replaceState(null, '', '#/workflow/' + id + '/step/' + num);
  };

  /* ======== Step Content Loading ======== */
  async function _loadStepContent(wfId, stepNum) {
    var el = document.getElementById('content');
    var stepInfo = _wfData.steps.find(function(s) { return s.step === stepNum; });

    if (!stepInfo || !stepInfo.has_content) {
      el.innerHTML =
        '<div class="flex flex-col items-center justify-center h-full text-base-content/40">' +
          '<div class="text-5xl mb-3 opacity-30">\u{1F4DD}</div>' +
          '<p class="text-lg font-medium">No content yet</p>' +
          '<p class="text-sm mt-1">This step hasn\'t been executed.</p>' +
        '</div>';
      return;
    }

    if (stepNum === 4) { _renderStep4(el, wfId, stepInfo); return; }

    try {
      var resp = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/step/' + stepNum);
      if (!resp.ok) { el.innerHTML = '<div class="p-8 text-center text-error">Failed to load content.</div>'; return; }
      _currentMdContent = await resp.text();
    } catch(e) { el.innerHTML = '<div class="p-8 text-center text-error">Error loading content.</div>'; return; }

    _renderStepContent(el, wfId, stepNum, stepInfo);
  }

  /* ======== Step Content Render ======== */
  function _renderStepContent(el, wfId, stepNum, stepInfo) {
    var title = 'Step ' + stepNum + ': ' + esc(stepInfo.name);
    var info = st(stepInfo.status);

    if (_editMode) {
      el.innerHTML =
        '<div class="p-6 lg:p-8 max-w-4xl mx-auto">' +
          '<div class="flex items-center justify-between mb-4">' +
            '<div class="flex items-center gap-3"><h2 class="text-xl font-bold">' + title + '</h2>' +
              '<span class="badge badge-warning badge-sm">Editing</span></div>' +
          '</div>' +
          '<textarea id="editor" class="textarea textarea-bordered w-full font-mono text-sm leading-relaxed" style="min-height:60vh;resize:vertical">' + esc(_currentMdContent) + '</textarea>' +
          '<div class="flex gap-2 mt-4">' +
            '<button class="btn btn-primary btn-sm gap-1" onclick="window._saveStep(\'' + esc(wfId) + '\',' + stepNum + ')">' +
              '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>Save</button>' +
            '<button class="btn btn-ghost btn-sm" onclick="window._cancelEdit(\'' + esc(wfId) + '\',' + stepNum + ')">Cancel</button>' +
          '</div>' +
        '</div>';
      return;
    }

    var html =
      '<div class="p-6 lg:p-8 max-w-4xl mx-auto">' +
        '<div class="flex items-center justify-between mb-5">' +
          '<div class="flex items-center gap-3">' +
            '<h2 class="text-xl font-bold">' + title + '</h2>' +
            '<span class="badge ' + info.cls + ' badge-sm">' + info.label + '</span>' +
          '</div>' +
          '<button class="btn btn-outline btn-sm gap-1" onclick="window._toggleEdit(\'' + esc(wfId) + '\',' + stepNum + ')">' +
            '<svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>Edit</button>' +
        '</div>' +
        '<div class="markdown-body" id="md-render"></div>';

    /* Step 2: subsystem summary cards */
    if (stepNum === 2 && stepInfo.subsystems && stepInfo.subsystems.length) {
      var cards = stepInfo.subsystems.map(function(s) {
        var typeColor = s.type === 'new' ? 'badge-info' : s.type === 'extend' ? 'badge-warning' : 'badge-ghost';
        return '<div class="card bg-base-100 border border-base-300 shadow-sm">' +
          '<div class="card-body p-4 gap-2">' +
            '<div class="flex items-center justify-between">' +
              '<h4 class="card-title text-sm">' + esc(s.name) + '</h4>' +
              '<span class="badge ' + typeColor + ' badge-xs uppercase">' + esc(s.type) + '</span>' +
            '</div>' +
            (s.responsibility ? '<p class="text-sm text-base-content/60 leading-snug">' + esc(s.responsibility) + '</p>' : '') +
          '</div></div>';
      }).join('');
      html += '<div class="divider text-xs text-base-content/40 uppercase tracking-wider mt-8">Identified Subsystems (' + stepInfo.subsystems.length + ')</div>' +
        '<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">' + cards + '</div>';
    }

    html += '</div>';
    el.innerHTML = html;
    renderMd(document.getElementById('md-render'), _currentMdContent);
  }

  /* ======== Step 4: Subsystem Specs ======== */
  function _renderStep4(el, wfId, stepInfo) {
    var specs = stepInfo.subsystems || [];
    var cards = specs.map(function(s) {
      var isActive = _currentSubsystem === s.name;
      return '<div class="card bg-base-100 border shadow-sm hover:shadow-md transition-all duration-200 cursor-pointer' +
        (isActive ? ' border-primary ring-2 ring-primary/20' : ' border-base-300 hover:border-primary/30') + '" ' +
        'onclick="window._viewSpec(\'' + esc(wfId) + '\',\'' + esc(s.name) + '\')">' +
        '<div class="card-body p-4 gap-1">' +
          '<div class="flex items-center gap-2">' +
            '<div class="w-2 h-2 rounded-full bg-primary/60 shrink-0"></div>' +
            '<h4 class="card-title text-sm">' + esc(s.name) + '</h4>' +
          '</div>' +
          '<p class="text-xs text-base-content/50 pl-4">' + (s.sections || []).length + ' section' + ((s.sections || []).length !== 1 ? 's' : '') + '</p>' +
        '</div></div>';
    }).join('');

    el.innerHTML =
      '<div class="p-6 lg:p-8 max-w-4xl mx-auto">' +
        '<div class="flex items-center justify-between mb-5">' +
          '<div class="flex items-center gap-3">' +
            '<h2 class="text-xl font-bold">Step 4: Subsystem Specification</h2>' +
            '<span class="badge badge-primary badge-outline badge-sm">' + specs.length + ' subsystem' + (specs.length !== 1 ? 's' : '') + '</span>' +
          '</div>' +
        '</div>' +
        '<p class="text-sm text-base-content/50 mb-4">Select a subsystem to view its REQUIREMENTS.md specification.</p>' +
        '<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">' + cards + '</div>' +
        '<div id="spec-content"></div>' +
      '</div>';
  }

  window._viewSpec = async function(wfId, subsystem) {
    _currentSubsystem = subsystem;
    /* Re-render step4 to highlight active card */
    var stepInfo = _wfData.steps.find(function(s) { return s.step === 4; });
    _renderStep4(document.getElementById('content'), wfId, stepInfo);

    var specEl = document.getElementById('spec-content');
    specEl.innerHTML = '<div class="flex justify-center p-8"><span class="loading loading-spinner text-primary"></span></div>';

    try {
      var resp = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/step/4/' + encodeURIComponent(subsystem));
      if (!resp.ok) { specEl.innerHTML = '<div class="alert alert-error mt-4"><span>Failed to load spec for ' + esc(subsystem) + '.</span></div>'; return; }
      _currentMdContent = await resp.text();
      specEl.innerHTML =
        '<div class="divider mt-8 mb-6"></div>' +
        '<div class="flex items-center justify-between mb-4">' +
          '<div class="flex items-center gap-2">' +
            '<div class="w-2.5 h-2.5 rounded-full bg-primary shrink-0"></div>' +
            '<h3 class="text-lg font-bold">' + esc(subsystem) + '</h3>' +
            '<span class="text-xs text-base-content/40 font-mono">REQUIREMENTS.md</span>' +
          '</div>' +
          '<button class="btn btn-outline btn-sm gap-1" onclick="window._editSpec(\'' + esc(wfId) + '\',\'' + esc(subsystem) + '\')">' +
            '<svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>Edit</button>' +
        '</div>' +
        '<div class="markdown-body" id="spec-render"></div>';
      renderMd(document.getElementById('spec-render'), _currentMdContent);
    } catch(e) { specEl.innerHTML = '<div class="alert alert-error mt-4"><span>Error loading spec.</span></div>'; }
  };

  window._editSpec = function(wfId, subsystem) {
    var specEl = document.getElementById('spec-content');
    specEl.innerHTML =
      '<div class="divider mt-8 mb-6"></div>' +
      '<div class="flex items-center gap-2 mb-3">' +
        '<h3 class="text-lg font-bold">' + esc(subsystem) + '</h3>' +
        '<span class="badge badge-warning badge-sm">Editing</span>' +
      '</div>' +
      '<textarea id="editor" class="textarea textarea-bordered w-full font-mono text-sm leading-relaxed" style="min-height:50vh;resize:vertical">' + esc(_currentMdContent) + '</textarea>' +
      '<div class="flex gap-2 mt-4">' +
        '<button class="btn btn-primary btn-sm gap-1" onclick="window._saveSpec(\'' + esc(wfId) + '\',\'' + esc(subsystem) + '\')">' +
          '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>Save</button>' +
        '<button class="btn btn-ghost btn-sm" onclick="window._viewSpec(\'' + esc(wfId) + '\',\'' + esc(subsystem) + '\')">Cancel</button>' +
      '</div>';
  };

  window._saveSpec = async function(wfId, subsystem) {
    var editor = document.getElementById('editor');
    if (!editor) return;
    try {
      var resp = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/step/4/' + encodeURIComponent(subsystem), {
        method: 'POST', headers: { 'Content-Type': 'text/plain; charset=utf-8' }, body: editor.value,
      });
      if (resp.ok) {
        _currentMdContent = editor.value;
        showToast('Saved successfully');
        window._viewSpec(wfId, subsystem);
      } else { showToast('Save failed', 'error'); }
    } catch(e) { showToast('Save failed', 'error'); }
  };

  window._toggleEdit = function(wfId, stepNum) {
    _editMode = true;
    var el = document.getElementById('content');
    var stepInfo = _wfData.steps.find(function(s) { return s.step === stepNum; });
    _renderStepContent(el, wfId, stepNum, stepInfo);
  };

  window._cancelEdit = function(wfId, stepNum) {
    _editMode = false;
    var el = document.getElementById('content');
    var stepInfo = _wfData.steps.find(function(s) { return s.step === stepNum; });
    _renderStepContent(el, wfId, stepNum, stepInfo);
  };

  window._saveStep = async function(wfId, stepNum) {
    var editor = document.getElementById('editor');
    if (!editor) return;
    try {
      var resp = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/step/' + stepNum, {
        method: 'POST', headers: { 'Content-Type': 'text/plain; charset=utf-8' }, body: editor.value,
      });
      if (resp.ok) {
        _currentMdContent = editor.value;
        _editMode = false;
        showToast('Saved successfully');
        var el = document.getElementById('content');
        var stepInfo = _wfData.steps.find(function(s) { return s.step === stepNum; });
        _renderStepContent(el, wfId, stepNum, stepInfo);
      } else { showToast('Save failed', 'error'); }
    } catch(e) { showToast('Save failed', 'error'); }
  };

  /* ======== Main Router ======== */
  async function route() {
    var r = getRoute();
    if (r.view === 'workflow') { await renderWorkflow(r.id, r.step); }
    else { await renderList(); }
  }

  window.addEventListener('hashchange', route);
  route();
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Porto Workflow Server — browse and edit workflows in your browser"
    )
    parser.add_argument(
        "--port", type=int, default=8090, help="Port to listen on (default: 8090)"
    )
    parser.add_argument("--porto-home", type=str, help="Override Porto home directory")
    args = parser.parse_args()

    if args.porto_home:
        _set_porto_home(Path(args.porto_home))

    porto_home = _get_porto_home()
    workflows_dir = _get_workflows_dir()
    if not workflows_dir.exists():
        workflows_dir.mkdir(parents=True, exist_ok=True)

    server = http.server.HTTPServer(("127.0.0.1", args.port), PortoHandler)
    print(f"Porto Workflow Server running at http://127.0.0.1:{args.port}")
    print(f"Porto home: {porto_home}")
    print(f"Workflows:  {workflows_dir}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
