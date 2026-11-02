#!/usr/bin/env python3
"""
Version A — Interactive Web Preview.

Lightweight Flask app that lets you:
  - Configure a return case (customer, items, receipt, images, store, etc.)
  - Run the pipeline end-to-end or step-by-step
  - See the state and audit log at each node
  - Act as Anouk/Robin in the HITL queue (approve / reject / escalate)
  - View the full resolution

Run:  python3 -m returns_pipeline.version_a.app
       (or:  flask --app returns_pipeline.version_a.app run --debug)
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from flask import Flask, Request, jsonify, render_template_string, request

from .state_models import (
    ConditionStatus,
    DeadLetterReason,
    NotificationStatus,
    PolicyEvalResult,
    ReceiptVerification,
    ResolutionType,
    ReturnCaseState,
    ReturnItem,
)
from .orchestrator import SupervisorOrchestrator
from .tools import (
    acquire_atomic_hold,
    analyze_shoe_condition,
    check_photo_quality_compliance,
    classify_structural_defect,
    create_interstore_transfer_manifest,
    federated_sales_search,
    generate_replacement_proof,
    notify_customer,
    payment_token_lookup,
    query_stock_matrix,
    release_expired_hold,
    submit_supplier_rma_claim,
    verify_receipt_data,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("preview")

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# ── CORS ────────────────────────────────────────────────────────────────────────
# The Arena preview proxy presents a different origin to the browser.
# Without CORS headers the browser blocks the fetch response.

def _cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, e2b-traffic-access-token"
    response.headers["Access-Control-Max-Age"] = "86400"
    # Prevent the browser from caching the HTML page — stale cached JS was
    # causing the button to get stuck on "⏳ Running..." with old bugs.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.after_request
def after_request(response):
    return _cors_headers(response)

@app.before_request
def before_request():
    if request.method == "OPTIONS":
        resp = app.response_class()  # fresh empty response — `response` is not in scope here
        return _cors_headers(resp), 204

# ── Templates ──────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sallie se Schoenen — Returns Agent Preview</title>
<style>
  :root {
    --bg: #f5f4f0;
    --panel: #ffffff;
    --ink: #1a1a1a;
    --muted: #6b6b6b;
    --line: #ddd9cf;
    --accent: #c0392b;
    --accent2: #1a5276;
    --accent3: #1e6f5c;
    --warm: #d35400;
    --gold: #b7950b;
    --hitl: #8e44ad;
    --shadow: 0 1px 3px rgba(0,0,0,0.07);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--ink);
    padding: 24px 20px 48px;
  }
  .wrap { max-width: 1100px; margin: 0 auto; }

  header {
    display: flex; align-items: center; gap: 14px;
    padding: 16px 20px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 8px;
    margin-bottom: 20px; box-shadow: var(--shadow);
  }
  .logo {
    width: 40px; height: 40px; border-radius: 8px;
    background: var(--accent2); color: white;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; font-weight: 700;
  }
  header h1 { font-size: 17px; font-weight: 700; letter-spacing: -0.01em; }
  header p  { font-size: 12.5px; color: var(--muted); margin-top: 2px; }

  .tabs {
    display: flex; gap: 0; margin-bottom: 16px;
    border-bottom: 2px solid var(--line); padding: 0 4px;
  }
  .tab {
    padding: 10px 18px; background: transparent; border: none;
    font-size: 13px; font-weight: 600; color: var(--muted);
    cursor: pointer; border-radius: 6px 6px 0 0;
    transition: all 0.15s; position: relative; top: 1px;
  }
  .tab.active {
    color: var(--accent2); background: var(--panel);
    box-shadow: 0 -2px 0 var(--accent2);
  }
  .tab:hover { color: var(--ink); }

  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 20px; box-shadow: var(--shadow);
    display: none;
  }
  .panel.active { display: block; }

  .section-label {
    font-size: 10.5px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.07em; color: var(--muted);
    margin-bottom: 8px;
  }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 8px; }
  .row.full { grid-template-columns: 1fr; }
  @media (max-width: 600px) { .row { grid-template-columns: 1fr; } }

  label {
    display: block; font-size: 12px; font-weight: 500;
    color: var(--ink); margin-bottom: 3px;
  }
  .hint { font-size: 11px; color: var(--muted); margin-bottom: 4px; }

  input, select, textarea {
    width: 100%; padding: 8px 10px; font-size: 13px;
    background: #faf9f6; border: 1px solid var(--line);
    border-radius: 5px; color: var(--ink);
    font-family: inherit;
  }
  input:focus, select:focus, textarea:focus {
    outline: none; border-color: var(--accent2); background: white;
  }
  textarea { resize: vertical; min-height: 60px; font-family: 'SF Mono', monospace; font-size: 12px; }

  .chip {
    display: inline-flex; align-items: center; gap: 4px;
    background: #f0ede8; border: 1px solid var(--line);
    padding: 3px 8px; border-radius: 12px; font-size: 11.5px;
    color: var(--muted);
  }
  .chip.active { background: #d6eaf8; border-color: #2980b9; color: var(--accent2); }

  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 9px 16px; font-size: 13px; font-weight: 600;
    border: none; border-radius: 6px; cursor: pointer;
    transition: all 0.15s; font-family: inherit;
  }
  .btn-primary {
    background: var(--accent2); color: white;
  }
  .btn-primary:hover { background: #154360; }
  .btn-secondary {
    background: #f0ede8; color: var(--ink); border: 1px solid var(--line);
  }
  .btn-secondary:hover { background: #e8e4dc; }
  .btn-hil {
    background: var(--hitl); color: white;
  }
  .btn-hil:hover { background: #7d3c98; }
  .btn-warn { background: var(--warm); color: white; }
  .btn-warn:hover { background: #a04000; }
  .btn-sm { padding: 5px 10px; font-size: 11.5px; }

  .status-bar {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; background: #f7f6f2;
    border: 1px solid var(--line); border-radius: 6px;
    margin-bottom: 14px; font-size: 12.5px;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    flex-shrink: 0;
  }
  .dot-pending { background: var(--gold); }
  .dot-hitl { background: var(--hitl); }
  .dot-closed { background: var(--accent3); }
  .dot-running { background: var(--accent2); animation: pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }

  .state-table {
    width: 100%; border-collapse: collapse; font-size: 12.5px;
    margin-bottom: 10px;
  }
  .state-table th {
    text-align: left; color: var(--muted); font-weight: 500;
    padding: 5px 8px; border-bottom: 1px solid var(--line);
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
  }
  .state-table td {
    padding: 5px 8px; border-bottom: 1px solid #eeeae3;
    vertical-align: top; font-family: 'SF Mono', monospace; font-size: 11.5px;
  }
  .state-table tr:last-child td { border-bottom: none; }
  .state-table td.label { color: var(--muted); font-family: inherit; font-size: 11px; }

  .audit-log {
    background: #f7f6f2; border: 1px solid var(--line);
    border-radius: 6px; padding: 10px 12px;
    max-height: 340px; overflow-y: auto; font-size: 12px;
    font-family: 'SF Mono', monospace; line-height: 1.6;
  }
  .audit-log .entry {
    padding: 4px 0; border-bottom: 1px solid #eeeae3;
  }
  .audit-log .entry:last-child { border-bottom: none; }
  .audit-log .node { color: var(--accent2); font-weight: 600; }
  .audit-log .decision { color: var(--ink); }
  .audit-log .meta { color: var(--muted); }
  .audit-log .flag-hitl { color: var(--hitl); font-weight: 600; }
  .audit-log .flag-close { color: var(--accent3); font-weight: 600; }

  .hitl-panel {
    background: #f5eef8; border: 2px solid var(--hitl);
    border-radius: 8px; padding: 16px; margin-top: 14px;
  }
  .hitl-title {
    font-size: 13px; font-weight: 700; color: var(--hitl);
    margin-bottom: 8px; display: flex; align-items: center; gap: 8px;
  }
  .hitl-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }

  .result-panel {
    background: #eafaf1; border: 2px solid var(--accent3);
    border-radius: 8px; padding: 14px 16px; margin-top: 14px;
  }
  .result-title { font-size: 13px; font-weight: 700; color: var(--accent3); margin-bottom: 6px; }
  .result-body { font-size: 13px; color: var(--ink); }

  .toast {
    position: fixed; bottom: 20px; right: 20px;
    background: var(--accent2); color: white; padding: 10px 16px;
    border-radius: 6px; font-size: 13px; font-weight: 600;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15); z-index: 100;
    opacity: 0; transform: translateY(8px);
    transition: all 0.25s;
  }
  .toast.show { opacity: 1; transform: translateY(0); }

  .node-vis {
    display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 12px;
  }
  .node-badge {
    padding: 4px 10px; border-radius: 14px; font-size: 11px; font-weight: 600;
    background: #f0ede8; color: var(--muted); border: 1px solid var(--line);
  }
  .node-badge.run { background: #d6eaf8; color: var(--accent2); border-color: #2980b9; }
  .node-badge.hitl { background: #f5eef8; color: var(--hitl); border-color: #7d3c98; }
  .node-badge.close { background: #eafaf1; color: var(--accent3); border-color: #1e6f5c; }

  /* ── Chatbot panel ── */
  .chat-messages {
    display: flex; flex-direction: column; gap: 8px;
    height: 380px; max-height: 420px; overflow-y: auto; overflow-x: hidden;
  }
  .chat-msg {
    max-width: 90%; padding: 8px 12px; border-radius: 10px;
    font-size: 13px; line-height: 1.5; word-wrap: break-word;
    flex-shrink: 0;
    box-sizing: border-box;
  }
  .chat-msg.chat-bot {
    align-self: flex-start; background: #f0f4f8; color: var(--text);
    border: 1px solid var(--line);
    flex-shrink: 0;
  }
  .chat-msg.chat-user {
    align-self: flex-end; background: var(--accent2); color: #fff;
    border: 1px solid var(--accent2);
    flex-shrink: 0;
  }
  .chat-msg.chat-system {
    align-self: center; background: #fef9e7; color: #7d6608;
    border: 1px solid #f0d060; font-size: 12px; max-width: 95%;
    flex-shrink: 0;
  }
  .chat-msg.chat-case {
    align-self: stretch; max-width: 100%; width: 100%;
    background: #eafaf1; color: var(--text);
    border: 1px solid #1e6f5c; border-radius: 8px;
    font-size: 12px; line-height: 1.5;
    flex-shrink: 0;
    box-sizing: border-box;
  }
  .chat-msg-label {
    font-size: 10.5px; font-weight: 700; color: #1e6f5c;
    margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .chat-msg-body {
    color: var(--text);
    width: 100%;
    overflow-x: auto;
  }
  .chat-msg-body code {
    background: #dde3ed; padding: 1px 5px; border-radius: 3px;
    font-family: 'Courier New', monospace; font-size: 12px; color: var(--accent2);
  }
  .chat-input {
    flex: 1; padding: 8px 12px; border: 1px solid var(--line);
    border-radius: 6px; font-size: 13px; background: #fff; color: var(--text);
  }
  .chat-quick {
    padding: 4px 10px; font-size: 12px; background: #eaf2f8; color: var(--accent2);
    border: 1px solid #b8d4e8; border-radius: 12px; cursor: pointer;
    transition: all 0.15s; white-space: nowrap;
  }
  .chat-quick:hover { background: var(--accent2); color: #fff; border-color: var(--accent2); }
  .chat-quick:active { transform: scale(0.97); }

  /* ── Floating chatbot widget ── */
  .chat-float-wrapper {
    position: fixed; bottom: 20px; right: 20px; z-index: 1000;
  }
  .chat-float {
    width: 440px; max-width: 95vw; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px;
    box-shadow: 0 4px 24px rgba(0,0,0,0.18);
    display: flex; flex-direction: column; overflow: hidden;
    font-family: var(--body-font);
    animation: chatFloatIn 0.2s ease-out;
  }
  @keyframes chatFloatIn {
    from { opacity: 0; transform: translateY(12px) scale(0.97); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }
  .chat-float-header {
    display: flex; align-items: center; gap: 8px;
    padding: 10px 14px; background: var(--accent2); color: white;
    cursor: default; flex-shrink: 0;
  }
  .chat-float-title { font-size: 13px; font-weight: 600; }
  .chat-float-sub {
    font-size: 10.5px; background: rgba(255,255,255,0.22);
    padding: 2px 8px; border-radius: 10px; margin-left: auto;
  }
  .chat-float-min-btn {
    background: rgba(255,255,255,0.2); border: none; color: white;
    width: 22px; height: 22px; border-radius: 50%;
    cursor: pointer; font-size: 13px; display: flex; align-items: center; justify-content: center;
    transition: background 0.15s;
  }
  .chat-float-min-btn:hover { background: rgba(255,255,255,0.38); }
  .chat-float-body {
    display: flex; flex-direction: column; padding: 12px;
    overflow: hidden; max-height: 560px;
  }
  .chat-float-collapsed {
    position: fixed; bottom: 20px; right: 20px; z-index: 1001;
    background: var(--accent2); color: white; border-radius: 50%;
    box-shadow: 0 2px 12px rgba(0,0,0,0.25);
    cursor: pointer; transition: transform 0.15s;
    display: flex; align-items: center; justify-content: center;
  }
  .chat-float-collapsed:hover { transform: scale(1.1); }
  .chat-float-toggler {
    width: 48px; height: 48px; border: none; background: transparent;
    color: white; font-size: 22px; cursor: pointer; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
  }
  .chat-float-collapsed:hover .chat-float-toggler { background: rgba(255,255,255,0.18); }
  @media (max-width: 480px) {
    .chat-float { width: 92vw; right: 4vw; bottom: 12px; }
    .chat-float-collapsed { right: 4vw; bottom: 12px; }
  }
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="logo">S</div>
  <div>
    <h1>Sallie se Schoenen — Returns Agent Preview</h1>
    <p>Version A · Single Orchestrator · 6 Nodes · Interactive Demo</p>
  </div>
  <div style="margin-left:auto; display:flex; gap:8px; align-items:center;">
    <a href="/workflow/a" target="_blank" style="text-decoration:none; color:var(--accent2); background:#eaf2f8; border:1px solid #2980b9; padding:4px 10px; border-radius:4px; font-size:12px; font-weight:600;">📊 Workflow A Diagram</a>
    <a href="/workflow/b" target="_blank" style="text-decoration:none; color:var(--hitl); background:#f5eef8; border:1px solid #7d3c98; padding:4px 10px; border-radius:4px; font-size:12px; font-weight:600;">📐 Workflow B Architecture</a>
    <span class="chip" id="status-chip">⏹ Idle</span>
    <span id="js-indicator" style="display:none; font-size:11px; color:var(--accent3); background:#eafaf1; padding:2px 8px; border-radius:10px; border:1px solid #1e6f5c;">✓ JS Live</span>
  </div>
</header>

<div class="tabs">
  <button class="tab active" onclick="switchTab('config')">📋 Case Config</button>
  <button class="tab" onclick="switchTab('run')">▶ Run Pipeline</button>
  <button class="tab" onclick="switchTab('hitl')">👁 HITL Queue</button>
  <button class="tab" onclick="switchTab('history')">📜 Case History</button>
</div>

<!-- ── CONFIG PANEL ── -->
<div class="panel active" id="panel-config">
  <div class="section-label">Case Identity</div>
  <div class="row">
    <div>
      <label>Case ID <span class="hint">auto-generated if left blank</span></label>
      <input type="text" id="cfg-case-id" placeholder="RC-CASE-001">
    </div>
    <div>
      <label>Origin Store</label>
      <select id="cfg-store">
        <option value="STORE-Utrecht">Sallie se Schoenen — Utrecht</option>
        <option value="STORE-Rotterdam">Sallie se Schoenen — Rotterdam</option>
        <option value="STORE-Amsterdam">Sallie se Schoenen — Amsterdam</option>
        <option value="STORE-Haarlem">Sallie se Schoenen — Haarlem</option>
      </select>
    </div>
  </div>
  <div class="row">
    <div>
      <label>Customer ID</label>
      <input type="text" id="cfg-customer-id" placeholder="CUST-001" value="CUST-PREVIEW-001">
    </div>
    <div>
      <label>Customer Email</label>
      <input type="email" id="cfg-email" placeholder="customer@mail.nl" value="preview@demo.nl">
    </div>
  </div>
  <div class="row">
    <div>
      <label>Customer Phone</label>
      <input type="text" id="cfg-phone" placeholder="+31612345678" value="+31612345678">
    </div>
    <div>
      <label>Preferred Resolution</label>
      <select id="cfg-resolution">
        <option value="">Auto-determine (from policy)</option>
        <option value="REFUND">Refund</option>
        <option value="EXCHANGE">Exchange</option>
        <option value="DEFECT_REMEDY">Defect Remedy</option>
      </select>
    </div>
  </div>

  <div class="section-label" style="margin-top:12px;">Return Item</div>
  <div class="row">
    <div>
      <label>SKU</label>
      <input type="text" id="cfg-sku" placeholder="SS-0042" value="SS-0042">
      <div class="hint">Try: SS-0042 (standard), SS-0087 (stock available at Rotterdam), SS-0091 (known defective), SS-0055 (worn), SS-0021 (no stock)</div>
    </div>
    <div>
      <label>Size</label>
      <input type="text" id="cfg-size" placeholder="EU-39" value="EU-39">
    </div>
  </div>
  <div class="row">
    <div>
      <label>Purchase Price (EUR)</label>
      <input type="number" id="cfg-price" step="0.01" min="0" value="89.95">
    </div>
    <div>
      <label>Condition Description</label>
      <input type="text" id="cfg-condition-desc" placeholder="Box opened, shoes clean" value="Box opened, shoes clean, no apparent wear">
    </div>
  </div>
  <div class="row">
    <div>
      <label>Is Worn? <span class="hint">explicit CV hint</span></label>
      <select id="cfg-is-worn">
        <option value="">Auto (CV simulation)</option>
        <option value="false">No — unworn</option>
        <option value="true">Yes — worn</option>
      </select>
    </div>
    <div>
      <label>Packaging Intact?</label>
      <select id="cfg-packaging">
        <option value="">Auto</option>
        <option value="true">Yes — intact</option>
        <option value="false">No — damaged</option>
      </select>
    </div>
  </div>

  <div class="section-label" style="margin-top:12px;">Proof of Purchase</div>
  <div class="row">
    <div>
      <label>Receipt ID</label>
      <input type="text" id="cfg-receipt-id" placeholder="REC-883921 (valid in mock ledger)" value="REC-883921">
      <div class="hint">Valid IDs in mock: REC-883921 (Utrecht, 7d), REC-910203 (Rotterdam, 12d), REC-778123 (Amsterdam, 22d), REC-661098 (Haarlem, 3d), REC-EC-44510 (Online, 19d)</div>
    </div>
    <div>
      <label>Images <span class="hint">comma-separated fake URLs</span></label>
      <input type="text" id="cfg-images" placeholder="shoe_top.jpg, shoe_side.jpg, shoe_sole.jpg" value="shoe_top.jpg, shoe_side.jpg, shoe_sole.jpg">
    </div>
  </div>

  <div class="section-label" style="margin-top:12px;">Actions</div>
  <div style="display:flex; gap:8px; flex-wrap:wrap;">
    <button class="btn btn-primary" onclick="buildAndRun()">▶ Build Case + Run Full Pipeline</button>
    <button class="btn btn-secondary" onclick="buildCase()">⚙ Build Case Only (see state)</button>
    <button class="btn btn-secondary" onclick="resetConfig()">↺ Reset Config</button>
  </div>
</div>

<!-- ── RUN PANEL ── -->
<div class="panel" id="panel-run">
  <div id="run-status"></div>
  <div id="run-node-vis" class="node-vis"></div>
  <div id="run-state"></div>
  <div id="run-audit"></div>
  <div id="run-hitl"></div>
  <div id="run-result"></div>
  <div style="margin-top:12px; display:flex; gap:8px;">
    <button class="btn btn-primary" id="btn-run-resume" style="display:none;" onclick="resumeHitL()">▶ Resume from HITL (Anouk decision)</button>
    <button class="btn btn-secondary" onclick="clearRun()">↺ Clear Run</button>
  </div>
</div>

<!-- ── HITL PANEL ── -->
<div class="panel" id="panel-hitl">
  <div class="section-label">Supervisor Queue (Anouk / Robin)</div>
  <p style="font-size:12.5px; color:var(--muted); margin-bottom:12px;">
    When a case pauses at HITL, you act as the supervisor. Choose a resolution to resume the pipeline.
  </p>
  <div id="hitl-queue">
    <div style="color:var(--muted); font-size:12.5px; padding:12px; background:#f7f6f2; border:1px solid var(--line); border-radius:6px;">
      No cases in HITL queue. Run a pipeline to see HITL cases here.
    </div>
  </div>
</div>

<!-- ── HISTORY PANEL ── -->
<div class="panel" id="panel-history">
  <div class="section-label">Case History (this session)</div>
  <div id="history-list">
    <div style="color:var(--muted); font-size:12.5px; padding:12px; background:#f7f6f2; border:1px solid var(--line); border-radius:6px;">
      No cases run yet.
    </div>
  </div>
</div>

<!-- ── FLOATING CHATBOT WIDGET ── -->
<div class="chat-float-wrapper" id="chat-float-wrapper">
  <div class="chat-float" id="chat-float">
    <div class="chat-float-header">
      <span class="chat-float-title">💬 Pipeline Chatbot</span>
      <span class="chat-float-sub">operator + ops assistant</span>
      <button class="chat-float-min-btn" onclick="minimizeChatFloat()" title="Minimize">✕</button>
    </div>
    <div class="chat-float-body" id="chat-float-body">
      <div style="margin-bottom:10px; display:flex; gap:6px; flex-wrap:wrap;">
        <button class="chat-quick" onclick="chatQuick('show case V1')">🔍 show case V1</button>
        <button class="chat-quick" onclick="chatQuick('show case V2')">🔍 show case V2</button>
        <button class="chat-quick" onclick="chatQuick('show case V3')">🔍 show case V3</button>
        <button class="chat-quick" onclick="chatQuick('show case V4')">🔍 show case V4</button>
        <button class="chat-quick" onclick="chatQuick('show case V5')">🔍 show case V5</button>
        <button class="chat-quick" onclick="chatQuick('list open cases')">📋 list open cases</button>
        <button class="chat-quick" onclick="chatQuick('show HITL queue')">🚨 show HITL queue</button>
        <button class="chat-quick" onclick="chatQuick('how does the pipeline work')">📖 how it works</button>
        <button class="chat-quick" onclick="chatQuick('what are the guardrails')">🛡️ guardrails</button>
      </div>
      <div class="chat-messages" id="chat-messages" style="height:380px; overflow-y:auto; overflow-x:hidden; background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:12px; margin-bottom:8px; display:flex; flex-direction:column; gap:8px;">
        <div class="chat-msg chat-bot">
          <div class="chat-msg-label">💬 Pipeline Bot</div>
          <div class="chat-msg-body">Welcome! I can help you with the returns pipeline. Try:<br>
            • <code>show case V1</code> — full state + audit for case V1 (REFUND)<br>
            • <code>show case V4</code> — full state + audit for case V4 (DEFECT)<br>
            • <code>list open cases</code> — all cases still open<br>
            • <code>show HITL queue</code> — cases awaiting supervisor<br>
            • <code>how does the pipeline work</code> — overview of the 6 nodes<br>
            • <code>what are the guardrails</code> — all guardrails explained<br>
            • <code>which stores have stock of SS-0087</code> — inventory query<br>
            • <code>what happened in V3</code> — audit trail narrative<br>
            • <code>total cases run</code> — session stats</div>
        </div>
      </div>
      <div style="display:flex; gap:6px;">
        <input id="chat-input" class="chat-input" placeholder="Ask anything about the pipeline…" autocomplete="off"
               style="flex:1; padding:8px 12px; border:1px solid var(--line); border-radius:6px; font-size:13px; background:#fff; color:var(--text);">
        <button class="btn btn-secondary" onclick="chatSend()" style="padding:8px 16px; font-size:13px;">Send</button>
      </div>
      <div style="font-size:11px; color:var(--muted); margin-top:6px; text-align:right;">
        chatbot v1 · queries the live pipeline state + operational knowledge base
      </div>
    </div>
    <div class="chat-float-collapsed" id="chat-float-collapsed" style="display:none;" onclick="expandChatFloat()">
      <button class="chat-float-toggler" title="Open chatbot">💬</button>
    </div>
  </div>
</div>

</div>

<div class="toast" id="toast"></div>

<script>
// Polyfills for legacy date/string formatting calls
if (!String.prototype.strftime) {
  String.prototype.strftime = function() { return this.toString(); };
}
if (!Date.prototype.strftime) {
  Date.prototype.strftime = function() { return this.toLocaleTimeString(); };
}

// ── JS loaded indicator + click feedback ──────────────────────────────────────
document.getElementById('js-indicator').style.display = 'inline-flex';
let buttonLoading = false;

// ── State ────────────────────────────────────────────────────────────────────
let currentCase = null;   // ReturnCaseState (serialised)
let hitlCaseId = null;    // case_id paused at HITL
let history = [];         // array of {case_id, resolution, summary}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
  document.getElementById('panel-' + name).classList.add('active');
  if (name === 'hitl') refreshHitL();
  if (name === 'history') renderHistory();
}

// ── Config helpers ─────────────────────────────────────────────────────────────
function getConfig() {
  const storeSelect = document.getElementById('cfg-store');
  const storeName = (storeSelect.selectedOptions && storeSelect.selectedOptions[0])
    ? storeSelect.selectedOptions[0].textContent
    : (storeSelect.value || 'Sallie se Schoenen — Utrecht');
  return {
    caseId: document.getElementById('cfg-case-id').value || '',
    storeId: storeSelect.value,
    storeName: storeName,
    customerId: document.getElementById('cfg-customer-id').value || 'CUST-PREVIEW-001',
    email: document.getElementById('cfg-email').value || 'preview@demo.nl',
    phone: document.getElementById('cfg-phone').value || '',
    resolution: document.getElementById('cfg-resolution').value || '',
    sku: document.getElementById('cfg-sku').value || 'SS-0042',
    size: document.getElementById('cfg-size').value || 'EU-39',
    price: parseFloat(document.getElementById('cfg-price').value) || 89.95,
    conditionDesc: document.getElementById('cfg-condition-desc').value || '',
    isWornHint: document.getElementById('cfg-is-worn').value === 'true' ? true
                : document.getElementById('cfg-is-worn').value === 'false' ? false : null,
    packagingHint: document.getElementById('cfg-packaging').value === 'true' ? true
                   : document.getElementById('cfg-packaging').value === 'false' ? false : null,
    receiptId: document.getElementById('cfg-receipt-id').value || '',
    imageUrls: document.getElementById('cfg-images').value
      .split(',').map(s => s.trim()).filter(s => s.length > 0),
  };
}

function resetConfig() {
  document.getElementById('cfg-case-id').value = '';
  document.getElementById('cfg-store').value = 'STORE-Utrecht';
  document.getElementById('cfg-customer-id').value = 'CUST-PREVIEW-001';
  document.getElementById('cfg-email').value = 'preview@demo.nl';
  document.getElementById('cfg-phone').value = '+31612345678';
  document.getElementById('cfg-resolution').value = '';
  document.getElementById('cfg-sku').value = 'SS-0042';
  document.getElementById('cfg-size').value = 'EU-39';
  document.getElementById('cfg-price').value = '89.95';
  document.getElementById('cfg-condition-desc').value = 'Box opened, shoes clean, no apparent wear';
  document.getElementById('cfg-is-worn').value = '';
  document.getElementById('cfg-packaging').value = '';
  document.getElementById('cfg-receipt-id').value = 'REC-883921';
  document.getElementById('cfg-images').value = 'shoe_top.jpg, shoe_side.jpg, shoe_sole.jpg';
}

// ── Build case (Python side) ──────────────────────────────────────────────────
function buildCase() {
  console.log('[buildCase] called');
  const btn = document.querySelector('button[onclick="buildAndRun()"]');
  console.log('[buildCase] btn found:', !!btn);
  if (btn) { btn.textContent = '⏳ Running...'; btn.disabled = true; btn.style.opacity = '0.7'; }

  const cfg = getConfig();
  console.log('[buildCase] config:', JSON.stringify(cfg));

  // AbortController for fetch timeout — prevents indefinite hang
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 15000); // 15s timeout

  const fetchPromise = fetch('/api/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
    signal: controller.signal,
  })
  .then(r => {
    console.log('[buildCase] fetch response status:', r.status, 'ok:', r.ok);
    if (!r.ok) { throw new Error('API returned ' + r.status); }
    return r.json();
  })
  .then(data => {
    console.log('[buildCase] JSON parsed, case_id:', data.state?.case_id);
    currentCase = data.state;
    hitlCaseId = data.hitlCaseId || null;
    showStatus('case-built', 'Case built — ' + data.state.case_id);
    showNodeVis(data.nodeVis);
    renderState(data.state);
    renderAudit(data.auditLog);
    document.getElementById('panel-run').classList.add('active');
    switchTab('run');
  })
  .catch(err => {
    console.error('[buildCase] ERROR:', err.name, err.message);
    // If the abort happened because of our timeout (not a real network error),
    // show a specific message. Otherwise show the generic error.
    if (err.name === 'AbortError') {
      showToast('Request timed out — server may be overloaded. Please try again.');
    } else {
      showToast('Error: ' + (err.message || 'request failed'));
    }
  })
  .finally(() => {
    console.log('[buildCase] finally — restoring button');
    clearTimeout(timeoutId);
    if (btn) {
      btn.textContent = '▶ Build Case + Run Full Pipeline';
      btn.disabled = false;
      btn.style.opacity = '1';
    }
  });
}

// ── Build + run ───────────────────────────────────────────────────────────────
function buildAndRun() {
  buildCase();  // builds and runs in one call
}

// ── Resume from HITL ───────────────────────────────────────────────────────────
function resumeHitL() {
  if (!hitlCaseId) { showToast('No case in HITL queue'); return; }
  const action = document.querySelector('input[name="hil-action"]:checked');
  if (!action) { showToast('Select a supervisor action first'); return; }

  fetch('/api/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ caseId: hitlCaseId, resolution: action.value }),
  })
  .then(r => r.json())
  .then(data => {
    currentCase = data.state;
    hitlCaseId = data.hitlCaseId || null;
    showStatus(data.state.closed ? 'closed' : 'case-run', data.message);
    showNodeVis(data.nodeVis);
    renderState(data.state);
    renderAudit(data.auditLog);
    if (data.state.closed) {
      document.getElementById('btn-run-resume').style.display = 'none';
      addHistory(data.state);
    }
    document.getElementById('panel-run').classList.add('active');
    switchTab('run');
    refreshHitL();
  })
  .catch(err => showToast('Error resuming: ' + err.message));
}

// ── Clear run ─────────────────────────────────────────────────────────────────
function clearRun() {
  currentCase = null;
  hitlCaseId = null;
  document.getElementById('run-status').innerHTML = '';
  document.getElementById('run-node-vis').innerHTML = '';
  document.getElementById('run-state').innerHTML = '';
  document.getElementById('run-audit').innerHTML = '';
  document.getElementById('run-hitl').innerHTML = '';
  document.getElementById('run-result').innerHTML = '';
  document.getElementById('btn-run-resume').style.display = 'none';
  showStatus('idle', 'Idle');
}

// ── Rendering ─────────────────────────────────────────────────────────────────
function showStatus(type, msg) {
  const chip = document.getElementById('status-chip');
  const bar = document.getElementById('run-status');
  const dotClass = {
    'idle': 'dot-pending',
    'case-built': 'dot-pending',
    'running': 'dot-running',
    'hitl': 'dot-hitl',
    'closed': 'dot-closed',
    'case-run': 'dot-closed',
  }[type] || 'dot-pending';

  chip.innerHTML = `<span class="status-dot ${dotClass}"></span> ${msg}`;
  bar.innerHTML = `
    <div class="status-bar">
      <span class="status-dot ${dotClass}"></span>
      <strong>${msg}</strong>
    </div>`;

  if (type === 'running') {
    chip.style.background = '#fef9e7';
  } else if (type === 'hitl') {
    chip.style.background = '#f5eef8';
  } else if (type === 'closed') {
    chip.style.background = '#eafaf1';
  } else {
    chip.style.background = '';
  }
}

function showNodeVis(nodeVis) {
  const el = document.getElementById('run-node-vis');
  if (!nodeVis || nodeVis.length === 0) { el.innerHTML = ''; return; }
  el.innerHTML = nodeVis.map(n => {
    let cls = 'node-badge';
    if (n.status === 'run') cls += ' run';
    if (n.status === 'hitl') cls += ' hitl';
    if (n.status === 'close') cls += ' close';
    if (n.status === 'skip') cls += '';
    return `<span class="${cls}">${n.name}</span>`;
  }).join('');
}

function renderState(state) {
  const el = document.getElementById('run-state');
  if (!state) { el.innerHTML = '<div style="color:var(--muted); font-size:12.5px;">No case state.</div>'; return; }

  const itemsHtml = (state.items || []).map((item, i) => {
    const imgs = item.image_urls || item.imageUrls || [];
    return `
    <tr>
      <td class="label">Item [${i+1}]</td>
      <td><code>${item.sku}</code> / <code>${item.size}</code> — €${item.purchase_price_eur}<br>
         <em>${item.condition_description}</em><br>
         ${imgs.length > 0 ? `📷 ${imgs.length} images` : '📷 no images'}
         ${item.is_worn ? '<br>⚠ <span style="color:var(--accent);">Worn</span>' : '<br>✓ Unworn'}
         ${!item.packaging_intact ? '<br>⚠ <span style="color:var(--warm);">Packaging not intact</span>' : '<br>✓ Packaging intact'}
      </td>
    </tr>`;
  }).join('');

  const receiptHtml = state.receipt_verification
    ? `<tr><td class="label">Receipt</td><td><code>${state.receipt_id}</code> — ${state.receipt_verification.verification_method}<br>
        Store: ${state.receipt_verification.store_name || '—'}<br>
        Days since purchase: ${state.receipt_verification.days_since_purchase}<br>
        Amount: €${state.receipt_verification.transaction_amount_eur}<br>
        Payment: ${state.receipt_verification.payment_method || '—'}</td></tr>`
    : `<tr><td class="label">Receipt</td><td><code>${state.receipt_id || '—'}</code> — ${state.proof_verified ? 'verified' : 'not verified'}</td></tr>`;

  const condStatus = state.condition_verdict?.condition_status?.value || state.condition_verdict?.condition_status || '—';
  const conditionHtml = state.condition_verdict
    ? `<tr><td class="label">Condition (CV)</td><td>
        Status: <strong>${condStatus}</strong><br>
        Is worn: ${state.condition_verdict.is_worn ? '⚠ Yes' : '✓ No'}<br>
        Packaging intact: ${state.condition_verdict.packaging_intact ? '✓ Yes' : '⚠ No'}<br>
        Defect suspected: ${state.condition_verdict.defect_suspected ? '⚠ Yes' : '✓ No'}<br>
        Confidence: ${typeof state.condition_verdict.defect_confidence === 'number' ? state.condition_verdict.defect_confidence.toFixed(2) : (state.condition_verdict.defect_confidence || '—')}<br>
        Fault type: ${state.condition_verdict.fault_type || '—'}<br>
        Mfg fault: ${state.condition_verdict.is_manufacturing_fault ? '⚠ Yes' : '✓ No'}<br>
        Photo quality: ${state.condition_verdict.photo_quality_passed ? '✓ Pass' : '⚠ Fail — missing: ' + (state.condition_verdict.missing_angles?.join(', ') || 'none')}</td></tr>`
    : `<tr><td class="label">Condition</td><td><em>Not yet assessed</em></td></tr>`;

  const policyHtml = state.policy_result
    ? `<tr><td class="label">Policy</td><td>
        Within 30 days: ${state.policy_result.within_30_day_window ? '✓ Yes' : '⚠ No'}<br>
        Is unworn: ${state.policy_result.is_unworn ? '✓ Yes' : '⚠ No'}<br>
        Refund amount: €${state.policy_result.refund_amount_eur}<br>
        Under €150 cap: ${state.policy_result.under_refund_cap ? '✓ Yes' : '⚠ No — requires supervisor'}<br>
        Escalation: ${state.policy_result.escalation_reason || 'none'}</td></tr>`
    : `<tr><td class="label">Policy</td><td><em>Not yet evaluated</em></td></tr>`;

  const holdExpiry = state.hold_reservation?.expires_at ? (typeof state.hold_reservation.expires_at === 'string' ? state.hold_reservation.expires_at : (state.hold_reservation.expires_at.strftime ? state.hold_reservation.expires_at.strftime('%H:%M %d %b') : state.hold_reservation.expires_at.toString())) : '—';
  const stockHtml = state.stock_matrix
    ? `<tr><td class="label">Stock Matrix</td><td>
        Sku: ${state.stock_matrix.sku} / ${state.stock_matrix.size}<br>
        Total available: ${state.stock_matrix.total_available}<br>
        ${state.stock_matrix.has_local_stock ? '✓ <span style="color:var(--accent3)">Local stock at origin</span>' : '✗ No stock at origin'}
        ${state.stock_matrix.has_network_stock ? '✓ <span style="color:var(--accent2)">Network stock available</span>' : ''}
        ${state.hold_reservation ? `<br><span style="color:var(--hitl)">🔒 Hold: ${state.hold_reservation.hold_id} at ${state.hold_reservation.store_name} — expires ${holdExpiry}</span>` : ''}
        ${state.transfer_manifest ? `<br><span style="color:var(--accent3)">📦 Manifest: ${state.transfer_manifest.manifest_id} → ${state.transfer_manifest.dest_store_name}</span>` : ''}
        <br><div style="margin-top:4px;">
        ${(state.stock_matrix.items || []).map(i => `<div style="font-size:11px; color:var(--muted);">${i.store_name}: ${i.quantity_available} ${i.is_verified ? '(verified)' : '(system-listed)'}</div>`).join('')}
        </div></td></tr>`
    : `<tr><td class="label">Stock</td><td><em>Not queried</em></td></tr>`;

  const rmaStatus = state.supplier_claim?.status?.value || state.supplier_claim?.status || '—';
  const defectHtml = state.supplier_claim
    ? `<tr><td class="label">Supplier RMA</td><td>
        Supplier: ${state.supplier_claim.supplier_name}<br>
        Claim ID: ${state.supplier_claim.claim_id || 'pending manual submission'}<br>
        Status: <strong style="color:${rmaStatus === 'SUBMITTED' ? 'var(--accent3)' : 'var(--warm)'}">${rmaStatus}</strong><br>
        Evidence: ${state.rma_evidence_package_ref || '—'}</td></tr>`
    : `<tr><td class="label">RMA</td><td><em>No RMA initiated</em></td></tr>`;

  const escTime = state.escalation_timestamp ? (typeof state.escalation_timestamp === 'string' ? state.escalation_timestamp : (state.escalation_timestamp.strftime ? state.escalation_timestamp.strftime('%H:%M:%S') : state.escalation_timestamp.toString())) : '—';
  const hitlHtml = state.escalated_to_supervisor
    ? `<tr><td class="label">HITL</td><td>
        ⚠️ <strong>Escalated to supervisor</strong><br>
        Reason: ${state.escalation_reason || state.escalation_reason_detail || '—'}<br>
        Escalated at: ${escTime}<br>
        Supervisor notes: ${state.supervisor_notes || '—'}</td></tr>`
    : `<tr><td class="label">HITL</td><td><em>Not escalated</em></td></tr>`;

  const notifStatus = state.customer_notification_status?.value || state.customer_notification_status || '—';
  const notifTime = state.notified_at ? (typeof state.notified_at === 'string' ? state.notified_at : (state.notified_at.strftime ? state.notified_at.strftime('%H:%M:%S') : state.notified_at.toString())) : '—';
  const notifyHtml = `<tr><td class="label">Notification</td><td>
    Status: <strong>${notifStatus}</strong><br>
    Channels: ${(state.notification_channels || []).join(', ') || '—'}<br>
    Notified at: ${notifTime}</td></tr>`;

  const closedTime = state.closed_at ? (typeof state.closed_at === 'string' ? state.closed_at : (state.closed_at.strftime ? state.closed_at.strftime('%H:%M:%S') : state.closed_at.toString())) : '';
  const deadLetter = state.dead_letter_reason?.value || state.dead_letter_reason || 'NONE';
  const finalHtml = `
    <tr><td class="label">Lifecycle</td><td>
      Closed: ${state.closed ? '✓ <span style="color:var(--accent3)">Yes</span>' : '✗ No'}
      ${closedTime ? '<br>Closed at: ' + closedTime : ''}
      <br>Dead letter: ${deadLetter}</td></tr>`;

  el.innerHTML = `
    <table class="state-table">
      <tbody>${itemsHtml}${receiptHtml}${conditionHtml}${policyHtml}${stockHtml}${defectHtml}${hitlHtml}${notifyHtml}${finalHtml}</tbody>
    </table>`;
}

function renderAudit(auditLog) {
  const el = document.getElementById('run-audit');
  if (!auditLog || auditLog.length === 0) {
    el.innerHTML = '<div style="color:var(--muted); font-size:12.5px;">No audit entries yet.</div>';
    return;
  }
  el.innerHTML = `
    <div class="section-label">Audit Log (${auditLog.length} entries)</div>
    <div class="audit-log">
      ${auditLog.map(e => {
        const conf = e.confidence != null ? ` conf=${e.confidence.toFixed(2)}` : '';
        const flag = e.guardrail_result ? ` <span class="flag-${e.guardrail_result.value === 'ESCALATE_HITL' ? 'hitl' : e.guardrail_result.value === 'LOOP_RECAPTURE' ? 'hitl' : 'hitl'}">[${e.guardrail_result.value}]</span>` : '';
        return `<div class="entry">
          <span class="node">[${e.node_name}]</span>
          <span class="decision">${e.decision}${conf}${flag}</span>
          ${e.output_summary ? '<br>' + e.output_summary.split('\n').map(l => '  <span class="meta">' + l + '</span>').join('<br>') : ''}
          ${e.note ? '<br><span class="meta">! ' + e.note + '</span>' : ''}
        </div>`;
      }).join('')}
    </div>`;
}

function renderHitL(state) {
  const el = document.getElementById('run-hitl');
  if (!state || !state.escalated_to_supervisor) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = `
    <div class="hitl-panel">
      <div class="hitl-title">👁 HITL — Supervisor Action Required</div>
      <p style="font-size:12.5px; color:var(--ink); margin-bottom:8px;">
        <strong>Case ${state.case_id}</strong> — ${state.resolution_type?.value || 'PENDING_REVIEW'}
      </p>
      <p style="font-size:12px; color:var(--muted); margin-bottom:10px;">
        ${state.escalation_reason || state.escalation_reason_detail || 'Supervisor review required'}
      </p>
      <div class="hitl-actions" id="hil-actions">
        ${renderHitLButtons(state)}
      </div>
    </div>`;
}

function renderHitLButtons(state) {
  const resolution = state.resolution_type?.value || '';
  const isDefect = resolution === 'DEFECT_REMEDY';
  const isWorn = state.condition_verdict?.is_worn;
  const isPackagingBad = !state.condition_verdict?.packaging_intact;

  let buttons = '';
  if (resolution === 'REFUND' || resolution === 'EXCHANGE' || resolution === '') {
    buttons += `<label class="chip active" style="cursor:pointer; padding:6px 12px;">
      <input type="radio" name="hil-action" value="APPROVE_REFUND" checked>
      ✅ Approve refund / exchange
    </label>`;
  }
  if (isWorn || isPackagingBad || resolution === 'PENDING_REVIEW') {
    buttons += `<label class="chip" style="cursor:pointer; padding:6px 12px;">
      <input type="radio" name="hil-action" value="REJECT_RETURN">
      ❌ Reject return (worn / policy)
    </label>`;
  }
  if (isDefect || state.condition_verdict?.defect_suspected) {
    buttons += `<label class="chip" style="cursor:pointer; padding:6px 12px;">
      <input type="radio" name="hil-action" value="FLAG_DEFECT_MANUAL">
      🔎 Confirm as defect — continue RMA
    </label>`;
  }
  buttons += `<label class="chip" style="cursor:pointer; padding:6px 12px;">
    <input type="radio" name="hil-action" value="ESCALATE_PURCHASING">
    📦 Escalate to Robin (purchasing)
  </label>`;
  return buttons;
}

function addHistory(state) {
  history.push({
    caseId: state.case_id,
    resolution: state.resolution_type?.value || 'UNKNOWN',
    closed: state.closed,
    summary: `${state.items[0]?.sku || '?'} / ${state.items[0]?.size || '?'} — €${state.items[0]?.purchase_price_eur || '0'} — ${state.resolution_type?.value || '—'}`,
  });
}

function renderHistory() {
  const el = document.getElementById('history-list');
  if (history.length === 0) {
    el.innerHTML = '<div style="color:var(--muted); font-size:12.5px; padding:12px; background:#f7f6f2; border:1px solid var(--line); border-radius:6px;">No cases run yet.</div>';
    return;
  }
  el.innerHTML = history.map(h => `
    <div style="padding:10px 12px; background:#faf9f6; border:1px solid var(--line); border-radius:6px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
      <div>
        <strong>${h.caseId}</strong>
        <div style="font-size:11.5px; color:var(--muted);">${h.summary}</div>
      </div>
      <div style="text-align:right;">
        <span style="display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;
          background:${h.closed ? '#eafaf1' : '#fef9e7'}; color:${h.closed ? 'var(--accent3)' : 'var(--gold)'};">
          ${h.closed ? 'CLOSED' : 'OPEN'}
        </span>
        <div style="font-size:11.5px; font-weight:600; color:var(--ink); margin-top:2px;">${h.resolution}</div>
      </div>
    </div>
  `).join('');
}

function refreshHitL() {
  // HitL state is shown in the run panel; the HITL panel shows the queue
  const el = document.getElementById('hitl-queue');
  if (!hitlCaseId) {
    el.innerHTML = '<div style="color:var(--muted); font-size:12.5px; padding:12px; background:#f7f6f2; border:1px solid var(--line); border-radius:6px;">No cases in HITL queue.</div>';
    return;
  }
  if (!currentCase) {
    el.innerHTML = '<div style="color:var(--muted); font-size:12.5px; padding:12px; background:#f7f6f2; border:1px solid var(--line); border-radius:6px;">No case loaded. Run a pipeline first.</div>';
    return;
  }
  const s = currentCase;
  el.innerHTML = `
    <div class="hitl-panel" style="margin-bottom:10px;">
      <div class="hitl-title">👁 HITL Queue — Case ${s.case_id}</div>
      <p style="font-size:12px; color:var(--muted); margin-bottom:8px;">
        ${s.resolution_type?.value || s.resolution_type || 'PENDING_REVIEW'} — ${s.escalation_reason || s.escalation_reason_detail || 'Supervisor review'}
      </p>
      <div style="font-size:12px;">
        <strong>Item:</strong> ${s.items[0]?.sku || '?'} / ${s.items[0]?.size || '?'} — €${s.items[0]?.purchase_price_eur || '0'}<br>
        <strong>Store:</strong> ${s.origin_store_name}<br>
        <strong>Customer:</strong> ${s.customer_id} (${s.customer_email || s.email || '—'})<br>
        <strong>Receipt:</strong> ${s.receipt_id || 'none'} — ${s.proof_verified ? 'verified' : 'not verified'}<br>
        ${s.condition_verdict ? `<strong>Condition:</strong> ${s.condition_verdict.condition_status?.value || s.condition_verdict.condition_status} | worn: ${s.condition_verdict.is_worn} | defect: ${s.condition_verdict.defect_suspected} | conf: ${typeof s.condition_verdict.defect_confidence === 'number' ? s.condition_verdict.defect_confidence.toFixed(2) : s.condition_verdict.defect_confidence}<br>` : ''}
        ${s.policy_result ? `<strong>Policy:</strong> within_30d: ${s.policy_result.within_30_day_window} | unworn: ${s.policy_result.is_unworn} | under_cap: ${s.policy_result.under_refund_cap}<br>` : ''}
      </div>
      <div class="hitl-actions">
        <label class="chip" style="cursor:pointer; padding:6px 12px;">
          <input type="radio" name="hil-queue-action" value="APPROVE_REFUND" checked onchange="document.getElementById('btn-run-resume').style.display='inline-flex';">
          ✅ Anouk: Approve refund/exchange
        </label>
        <label class="chip" style="cursor:pointer; padding:6px 12px;">
          <input type="radio" name="hil-queue-action" value="REJECT_RETURN" onchange="document.getElementById('btn-run-resume').style.display='inline-flex';">
          ❌ Anouk: Reject return
        </label>
        <label class="chip" style="cursor:pointer; padding:6px 12px;">
          <input type="radio" name="hil-queue-action" value="FLAG_DEFECT_MANUAL" onchange="document.getElementById('btn-run-resume').style.display='inline-flex';">
          🔎 Anouk: Confirm defect → continue RMA
        </label>
        <label class="chip" style="cursor:pointer; padding:6px 12px;">
          <input type="radio" name="hil-queue-action" value="ESCALATE_PURCHASING" onchange="document.getElementById('btn-run-resume').style.display='inline-flex';">
          📦 Robin: Escalate to purchasing
        </label>
      </div>
      <button class="btn btn-hil" onclick="doHitLAction()">▶ Apply & Resume Pipeline</button>
    </div>`;
}

function doHitLAction() {
  const selected = document.querySelector('input[name="hil-queue-action"]:checked');
  if (!selected) { showToast('Select a HITL action first'); return; }
  if (!hitlCaseId) { showToast('No case in HITL'); return; }
  fetch('/api/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ caseId: hitlCaseId, action: selected.value, resolution: selected.value }),
  })
  .then(r => r.json())
  .then(data => {
    currentCase = data.state;
    hitlCaseId = data.hitlCaseId || null;
    showStatus(data.state.closed ? 'closed' : 'case-run', data.message);
    showNodeVis(data.nodeVis);
    renderState(data.state);
    renderAudit(data.auditLog);
    renderHitL(data.state);
    document.getElementById('btn-run-resume').style.display = 'none';
    if (data.state.closed) addHistory(data.state);
    refreshHitL();
    document.getElementById('panel-run').classList.add('active');
    switchTab('run');
  })
  .catch(err => showToast('Error: ' + err.message));
}

// ── Toast ──────────────────────────────────────────────────────────────────────
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── Floating Chatbot Widget ─────────────────────────────────────────────────────
// The chatbot is a floating widget (bottom-right), always visible.
// Collapsed mode shows a 💬 toggle button; expanded mode shows the full chat UI.

function expandChatFloat() {
  const body = document.getElementById('chat-float-body');
  const collapsed = document.getElementById('chat-float-collapsed');
  if (body) body.style.display = 'flex';
  if (collapsed) collapsed.style.display = 'none';
}

function minimizeChatFloat() {
  const body = document.getElementById('chat-float-body');
  const collapsed = document.getElementById('chat-float-collapsed');
  if (body) body.style.display = 'none';
  if (collapsed) collapsed.style.display = 'flex';
}

// Auto-expand the chat widget on page load so it's always visible.
document.addEventListener('DOMContentLoaded', function() {
  expandChatFloat();
});

// ── Chatbot ────────────────────────────────────────────────────────────────────
const chatMessages = [];

function chatRender() {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  // Keep the welcome message as the first child; re-render the rest
  const existingWelcome = container.querySelector('.chat-msg.chat-bot:first-child');
  container.innerHTML = '';
  if (existingWelcome) container.appendChild(existingWelcome);
  chatMessages.forEach(msg => {
    const div = document.createElement('div');
    div.className = 'chat-msg ' + msg.type;
    if (msg.label) {
      const label = document.createElement('div');
      label.className = 'chat-msg-label';
      label.textContent = msg.label;
      div.appendChild(label);
    }
    if (msg.body) {
      const body = document.createElement('div');
      body.className = 'chat-msg-body';
      body.innerHTML = msg.body;
      div.appendChild(body);
    }
    if (msg.raw) {
      const pre = document.createElement('pre');
      pre.style.cssText = 'font-size:11px; background:#1a1a2e; color:#a0c4ff; padding:6px 10px; border-radius:6px; margin-top:4px; overflow-x:auto; font-family:Monaco,Consolas,monospace;';
      pre.textContent = msg.raw;
      div.appendChild(pre);
    }
    container.appendChild(div);
  });
  container.scrollTop = container.scrollHeight;
}

function chatAdd(type, label, body, raw) {
  chatMessages.push({ type, label, body, raw });
  chatRender();
}

function chatQuick(text) {
  document.getElementById('chat-input').value = text;
  chatSend();
}

function chatSend() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  chatAdd('chat-user', 'You', text);
  // Show a brief "thinking" indicator
  chatAdd('chat-system', 'Bot', '<span style="color:var(--accent2)">⏳ thinking…</span>');
  fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text })
  })
  .then(r => r.json())
  .then(data => {
    // Remove the thinking indicator from chatMessages
    if (chatMessages.length > 0 && chatMessages[chatMessages.length - 1].body && chatMessages[chatMessages.length - 1].body.includes('thinking')) {
      chatMessages.pop();
    }
    if (data.error) {
      chatAdd('chat-bot', '⚠ Error', data.error);
    } else {
      // Render the response
      if (data.tab) switchTab(data.tab);
      if (data.stateJson) {
        // Render unified state & audit card — fills width, cannot squish
        const sj = data.stateJson;
        let cardHtml = `
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; border-bottom:1px solid #b2d8c3; padding-bottom:5px;">
            <span style="font-size:12.5px; font-weight:700; color:#1e6f5c;">Case: ${sj.case_id}</span>
            <span style="font-size:10.5px; padding:2px 7px; border-radius:10px; font-weight:700; background:${sj.closed ? '#c8e6c0' : '#fef9e7'}; color:${sj.closed ? '#1e6f5c' : '#7d6608'};">${sj.closed ? 'CLOSED' : 'OPEN'}</span>
          </div>
          <div style="display:grid; grid-template-columns: 1fr 1fr; gap:4px 10px; font-size:11.5px; margin-bottom:8px;">
            <div><span style="color:#1e6f5c; font-weight:600;">Resolution:</span> <code>${sj.resolution_type || '—'}</code></div>
            <div><span style="color:#1e6f5c; font-weight:600;">Notified:</span> <code>${sj.customer_notification_status || '—'}</code></div>
            <div><span style="color:#1e6f5c; font-weight:600;">Store:</span> <code>${sj.origin_store_name || '—'}</code></div>
            <div><span style="color:#1e6f5c; font-weight:600;">Customer:</span> <code>${sj.customer_email || '—'}</code></div>
          </div>`;
        if (sj.stock_matrix && sj.stock_matrix.items) {
          cardHtml += '<div style="margin-top:6px; padding:6px 8px; background:#fff; border:1px solid #b2d8c3; border-radius:6px; font-size:11px; margin-bottom:8px;">';
          cardHtml += '<strong style="color:#1e6f5c;">Stock Matrix:</strong><br>';
          sj.stock_matrix.items.forEach(i => {
            cardHtml += `<span style="color:var(--text);">${i.store_name}:</span> <code>${i.quantity_available}</code> · `;
          });
          cardHtml = cardHtml.slice(0, -2);
          cardHtml += `<br><span style="color:var(--muted); font-size:10.5px;">Total: ${sj.stock_matrix.total_available} pairs · local: ${sj.stock_matrix.has_local_stock ? '✓' : '✗'} · network: ${sj.stock_matrix.has_network_stock ? '✓' : '✗'}</span>`;
          cardHtml += '</div>';
        }
        if (data.auditHtml) {
          cardHtml += '<div style="margin-top:8px; border-top:1px solid #b2d8c3; padding-top:6px;">';
          cardHtml += '<div style="font-size:11px; font-weight:700; color:#1e6f5c; text-transform:uppercase; letter-spacing:0.4px; margin-bottom:5px;">Audit Trail</div>';
          cardHtml += data.auditHtml;
          cardHtml += '</div>';
        }
        chatAdd('chat-case', '📋 Case ' + sj.case_id, cardHtml);
      } else {
        chatAdd('chat-bot', data.label || '💬 Pipeline Bot', data.body);
      }
    }
  })
  .catch(err => {
    if (chatMessages.length > 0 && chatMessages[chatMessages.length - 1].body && chatMessages[chatMessages.length - 1].body.includes('thinking')) {
      chatMessages.pop();
    }
    chatAdd('chat-bot', '⚠ Error', 'Request failed: ' + err.message);
  });
}

// Allow Enter key to send
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('chat-input');
  if (input) {
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); chatSend(); }
    });
  }
  chatRender();
});
</script>
</body>
</html>
"""

# ── API ─────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/workflow/a")
@app.route("/version_a_workflow.html")
def workflow_a():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    file_path = os.path.join(root, "version_a_workflow.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/workflow/b")
@app.route("/version_b_workflow.html")
def workflow_b():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    file_path = os.path.join(root, "version_b_workflow.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/build", methods=["POST"])
def api_build():
    """
    Build a ReturnCaseState from config and run the pipeline.
    Returns the state, node visits, and audit log.
    If the pipeline pauses at HITL, returns hitlCaseId.
    """
    cfg = request.get_json(force=True)

    state = build_state(cfg)
    return run_pipeline(state)


@app.route("/api/resume", methods=["POST"])
def api_resume():
    """
    Resume a HITL-paused case with a supervisor action.

    The frontend sends { caseId, resolution, notes }.
    resolution is one of: APPROVE_REFUND | APPROVE_EXCHANGE | REJECT_RETURN | FLAG_DEFECT_MANUAL
    """
    body = request.get_json(force=True)
    case_id = body.get("caseId")
    resolution = body.get("resolution") or body.get("action")  # support both frontend fields

    if not case_id:
        return jsonify({"error": "caseId required"}), 400
    if not resolution:
        return jsonify({"error": "resolution required"}), 400

    if resolution == "ESCALATE_PURCHASING":
        resolution = "FLAG_DEFECT_MANUAL"

    # Load state from session storage
    state = load_state(case_id)
    if not state:
        return jsonify({"error": f"Case {case_id} not found in session"}), 404

    # Validate resolution
    valid_resolutions = ["APPROVE_REFUND", "APPROVE_EXCHANGE", "REJECT_RETURN", "FLAG_DEFECT_MANUAL"]
    if resolution not in valid_resolutions:
        return jsonify({"error": f"resolution must be one of: {', '.join(valid_resolutions)}"}), 400

    orchestrator = SupervisorOrchestrator()
    notes = body.get("notes", f"Supervisor action via preview: {resolution}")
    # resume_from_hitl already runs the remaining nodes (or returns terminal state directly).
    # Do NOT call orchestrator.run() again — that would re-evaluate intake_eval/policy_check
    # and re-route to HITL, overwriting the supervisor's decision.
    result = orchestrator.resume_from_hitl(state, resolution, notes)

    save_state(result)
    if result.escalated_to_supervisor:
        hitl_store[result.case_id] = result
    else:
        hitl_store.pop(result.case_id, None)

    return jsonify({
        "state": serialise_state(result),
        "hitlCaseId": result.case_id if result.escalated_to_supervisor else None,
        "message": f"Case {result.case_id}: {result.resolution_type.value if result.resolution_type else 'RESOLVED'}",
        "nodeVis": build_node_vis(result),
        "auditLog": [e.__dict__ for e in result.audit_log],
    })


@app.route("/api/hitl-queue", methods=["GET"])
def api_hitl_queue():
    return jsonify({
        "queue": [
            {"caseId": cid, "state": serialise_state(s)}
            for cid, s in hitl_store.items()
        ]
    })


# ── Chatbot API ────────────────────────────────────────────────────────────────

# Knowledge base: answers to common operational questions
CHAT_KNOWLEDGE = {
    "how does the pipeline work": (
        "💬 <strong>How the Version A pipeline works</strong><br>"
        "The pipeline is a single-orchestrator state machine with 6 numbered nodes:<br>"
        "① <strong>Intake &amp; Condition</strong> — verify receipt (if present) and run CV condition assessment on images<br>"
        "② <strong>Cross-Store Proof</strong> — if receipt is missing, search all 4 Randstad stores + e-comm for a matching transaction<br>"
        "③ <strong>Policy Check</strong> — apply guardrails: 30-day window, unworn check, €150 refund cap, defect precedence<br>"
        "④a <strong>Stock &amp; Logistics</strong> — for exchanges: query stock across 4 stores + warehouse, acquire atomic hold, create transfer manifest<br>"
        "④b <strong>Defect Assess</strong> — for suspected defects: photo quality gate → classify structural defect → confidence ≥ 0.85 → auto-remedy + RMA<br>"
        "⑤a <strong>Till Refund</strong> — for refunds: cap check (€150) → authorize or HITL if over cap<br>"
        "⑤b <strong>Transfer</strong> — mark transfer manifest dispatched<br>"
        "⑤c <strong>Supplier RMA</strong> — assemble evidence package, submit to supplier (auto or manual)<br>"
        "⑥ <strong>Notify &amp; Close</strong> — send customer notification (SMS + email), close case<br><br>"
        "Cases can pause at <strong>HITL</strong> (Human-in-the-Loop) at any point where a guardrail is violated or a manual decision is needed. "
        "The supervisor (Anouk/Robin) then chooses: Approve Refund, Approve Exchange, Reject Return, or Flag Defect for Manual Review."
    ),
    "what are the guardrails": (
        "🛡️ <strong>Guardrails</strong><br>"
        "• <strong>30-day return window</strong> — purchases older than 30 days → HITL for change-of-mind returns.<br>"
        "• <strong>Unworn check</strong> — worn items (is_worn=True) → HITL for supervisor review.<br>"
        "• <strong>Refund cap €150</strong> — refunds over €150 → HITL for manual authorization.<br>"
        "• <strong>Photo quality gate</strong> — defect assessment needs ≥3 clear images from 5 angles. Insufficient photos → re-capture loop.<br>"
        "• <strong>Defect confidence threshold 0.85</strong> — below 0.85 → HITL for Anouk manual inspection.<br>"
        "• <strong>Atomic hold TTL 48h</strong> — expired holds → dead-letter.<br>"
        "• <strong>Defect precedence</strong> — if defect suspected, policy checks are bypassed and case goes to defect path."
    ),
    "what is the pipeline": (
        "💬 <strong>Version A — Single Orchestrator Returns Pipeline</strong><br>"
        "A single orchestrator runs a 6-node state machine over a shared <code>ReturnCaseState</code>. "
        "All 4 Randstad stores + e-comm are covered. Handles: refunds, exchanges (with inter-store stock transfer), "
        "defect remedy (with supplier RMA), and HITL pauses for policy violations, missing proof, worn items, and borderline defects.<br><br>"
        "<strong>Stores:</strong> Utrecht, Rotterdam, Amsterdam, Haarlem + E-comm<br>"
        "<strong>Mock data:</strong> receipt ledger, stock matrix, CV mock (deterministic for SS-0091 defective batch), supplier API (mock)"
    ),
    "who are the stores": (
        "🏪 <strong>Stores in the pipeline</strong><br>"
        "• <strong>Sallie se Schoenen — Utrecht</strong> (STORE-Utrecht)<br>"
        "• <strong>Sallie se Schoenen — Rotterdam</strong> (STORE-Rotterdam)<br>"
        "• <strong>Sallie se Schoenen — Amsterdam</strong> (STORE-Amsterdam)<br>"
        "• <strong>Sallie se Schoenen — Haarlem</strong> (STORE-Haarlem)<br>"
        "• <strong>Sallie se Schoenen — Online</strong> (STORE-Ecommerce)<br><br>"
        "Federated sales ledger covers all 4 physical stores + e-comm. Stock matrix covers the 4 physical stores + Central Warehouse."
    ),
    "default": (
        "🤔 I'm not sure I understand. Try one of these:<br>"
        "• <code>show case V1</code> — see full case state<br>"
        "• <code>list open cases</code> — see all open cases<br>"
        "• <code>show HITL queue</code> — see pending supervisor cases<br>"
        "• <code>how does the pipeline work</code> — overview<br>"
        "• <code>what are the guardrails</code> — all guardrails<br>"
        "• <code>which stores have stock of SS-0087</code> — inventory query"
    ),
}


def _find_case_in_session(case_id: str):
    """Search session_states and hitl_store for a case by ID. Supports fuzzy/case-insensitive and dynamic loading of demo cases."""
    cid_upper = case_id.strip().upper()
    for store in (session_states, hitl_store):
        if case_id in store:
            return store[case_id]
        if cid_upper in store:
            return store[cid_upper]
        for k, v in store.items():
            if k.upper() == cid_upper or k.upper().replace("-", "").endswith(cid_upper.replace("-", "")):
                return v

    # Dynamic fallback for demo cases V1-V5
    demo_map = {
        "V1": ("case_1", None),
        "1": ("case_1", None),
        "RC-CASE-001": ("case_1", None),
        "V2": ("case_2", None),
        "2": ("case_2", None),
        "RC-CASE-002": ("case_2", None),
        "V3": ("case_3", "APPROVE_REFUND"),
        "3": ("case_3", "APPROVE_REFUND"),
        "RC-CASE-003": ("case_3", "APPROVE_REFUND"),
        "V4": ("case_4", None),
        "4": ("case_4", None),
        "RC-CASE-004": ("case_4", None),
        "V5": ("case_5", "REJECT_RETURN"),
        "5": ("case_5", "REJECT_RETURN"),
        "RC-CASE-005": ("case_5", "REJECT_RETURN"),
    }
    if cid_upper in demo_map:
        fn_name, hitl_res = demo_map[cid_upper]
        try:
            from . import demo
            fn = getattr(demo, fn_name, None)
            if fn:
                init_state = fn()
                orch = SupervisorOrchestrator()
                res = orch.run(init_state)
                if res.escalated_to_supervisor:
                    hitl_store[res.case_id] = res
                    hitl_store[cid_upper] = res
                    if hitl_res and cid_upper not in ("V3", "3", "RC-CASE-003"):
                        res = orch.resume_from_hitl(res, supervisor_resolution=hitl_res, supervisor_notes="Supervisor actioned.")
                session_states[res.case_id] = res
                session_states[cid_upper] = res
                return res
        except Exception as e:
            logger.warning("_find_case_in_session error running demo: %s", e)

    return None


def _format_audit_html(audit_log):
    """Format an audit log as a compact card-based layout for narrow chat widgets."""
    if not audit_log:
        return '<div style="color:var(--muted);font-size:12px;padding:4px 0;">No audit entries.</div>'
    cards = ""
    for e in audit_log:
        if isinstance(e, dict):
            nn = e.get("node_name", "?")
            dec = e.get("decision", "")
            nxt = e.get("next_node", "")
            summ = e.get("output_summary", "")
        else:
            nn = getattr(e, "node_name", "?")
            dec = getattr(e, "decision", "")
            nxt = getattr(e, "next_node", "")
            summ = getattr(e, "output_summary", "")
        nxt_str = f" → <code>{nxt}</code>" if nxt else ""
        cards += f'''
        <div style="border:1px solid #d2ded7; border-radius:6px; padding:6px 9px; margin-bottom:5px; background:#ffffff;">
          <div style="font-size:11px; font-weight:600; color:var(--accent2); margin-bottom:2px;">
            {nn} {nxt_str}
          </div>
          <div style="font-size:11.5px; color:var(--text); margin-bottom:3px; line-height:1.45;">
            <strong>Decision:</strong> {dec}
          </div>
          <div style="font-size:11px; color:var(--muted); line-height:1.5; border-top:1px solid #eee; padding-top:3px;">
            {summ}
          </div>
        </div>'''
    return f'<div style="font-size:12px;">{cards}</div>'


def _format_state_html(state_dict):
    """Format a serialised state dict as a compact HTML table."""
    if not state_dict:
        return '<div style="color:var(--muted);font-size:12.5px;">No state available.</div>'
    rows = ""
    fields = [
        ("Case ID", "case_id"), ("Customer ID", "customer_id"),
        ("Email", "customer_email"), ("Phone", "customer_phone"),
        ("Store", "origin_store_name"), ("Store ID", "origin_store_id"),
        ("Intake at", "intake_timestamp"), ("Receipt", "receipt_id"),
        ("Proof verified", "proof_verified"), ("Purchase date", "purchase_date"),
        ("Resolution", "resolution_type"), ("Resolution amount", "resolution_amount_eur"),
        ("Closed", "closed"), ("Defect detected", "defect_detected"),
        ("Customer notified", "customer_notification_status"),
    ]
    for label, key in fields:
        val = state_dict.get(key, "—")
        if val is None: val = "—"
        if isinstance(val, bool): val = "✓ Yes" if val else "✗ No"
        rows += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 10px; width:150px; font-size:12px; color:var(--muted); background:#f7f6f2; white-space:nowrap;">{label}</td><td style="padding:3px 10px; font-size:12px;"><code>{val}</code></td></tr>'
    # Items
    items = state_dict.get("items", [])
    if items:
        rows += f'<tr style="background:#eef2f7;"><td colspan="2" style="padding:5px 10px; font-size:12px; font-weight:600; color:var(--accent2);">Items ({len(items)})</td></tr>'
        for idx, item in enumerate(items, 1):
            wrn = "⚠ Worn" if item.get("is_worn") else "✓ Unworn"
            pkg = "⚠ Packaging not intact" if not item.get("packaging_intact") else "✓ Packaging intact"
            rows += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 10px; width:150px; font-size:12px; color:var(--muted);">[{idx}] {item.get("sku","?")} / {item.get("size","?")}</td><td style="padding:3px 10px; font-size:12px;">€{item.get("purchase_price_eur","?")} · {item.get("condition_description","")}<br><span style="color:{"var(--accent)" if item.get("is_worn") else "var(--accent3)"};">{wrn}</span> · <span style="color:{"var(--warm)" if not item.get("packaging_intact") else "var(--accent3)"};">{pkg}</span> · {len(item.get("image_urls",[]))} images</td></tr>'
    # Stock matrix
    sm = state_dict.get("stock_matrix")
    if isinstance(sm, dict) and sm.get("items"):
        rows += f'<tr style="background:#eef2f7;"><td colspan="2" style="padding:5px 10px; font-size:12px; font-weight:600; color:var(--accent2);">Stock Matrix — {sm.get("sku","?")} / {sm.get("size","?")}</td></tr>'
        for item in sm.get("items", []):
            rows += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 10px; width:150px; font-size:12px; color:var(--muted);">{item.get("store_name","?")}</td><td style="padding:3px 10px; font-size:12px;">{item.get("quantity_available","?")} pairs · {"✓ verified" if item.get("is_verified") else "✗ system-listed"}</td></tr>'
        rows += f'<tr style="background:#f0ede8;"><td style="padding:3px 10px; font-size:12px; color:var(--muted);">Total</td><td style="padding:3px 10px; font-size:12px; font-weight:600;">{sm.get("total_available","?")} pairs · local: {"✓" if sm.get("has_local_stock") else "✗"} · network: {"✓" if sm.get("has_network_stock") else "✗"}</td></tr>'
    # RMA
    sc = state_dict.get("supplier_claim")
    if isinstance(sc, dict) and sc.get("claim_id"):
        rows += f'<tr style="background:#f5eef8;"><td colspan="2" style="padding:5px 10px; font-size:12px; font-weight:600; color:var(--hitl);">Supplier RMA</td></tr>'
        for lbl, key in [("Claim ID","claim_id"),("Supplier","supplier_name"),("Status","status"),("Evidence ref","evidence_package_ref")]:
            val = sc.get(key,"—")
            if val is None: val = "—"
            if key == "status" and isinstance(val, str):
                pass
            rows += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 10px; width:150px; font-size:12px; color:var(--muted);">{lbl}</td><td style="padding:3px 10px; font-size:12px;"><code>{val}</code></td></tr>'
    # Hold
    hold = state_dict.get("hold_reservation")
    if isinstance(hold, dict) and hold.get("hold_id"):
        rows += f'<tr style="background:#fef9e7;"><td colspan="2" style="padding:5px 10px; font-size:12px; font-weight:600; color:#7d6608;">Atomic Hold</td></tr>'
        for lbl, key in [("Hold ID","hold_id"),("Store","store_name"),("Expires","expires_at"),("Status","status"),("TTL","ttl_seconds")]:
            val = hold.get(key,"—")
            if val is None: val = "—"
            if key == "ttl_seconds" and isinstance(val, (int,float)):
                val = f"{int(val)//3600}h"
            rows += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 10px; width:150px; font-size:12px; color:var(--muted);">{lbl}</td><td style="padding:3px 10px; font-size:12px;"><code>{val}</code></td></tr>'
    # Transfer
    tm = state_dict.get("transfer_manifest")
    if isinstance(tm, dict) and tm.get("manifest_id"):
        rows += f'<tr style="background:#eafaf1;"><td colspan="2" style="padding:5px 10px; font-size:12px; font-weight:600; color:var(--accent3);">Transfer Manifest</td></tr>'
        for lbl, key in [("Manifest ID","manifest_id"),("From → To", None),("Status","status")]:
            if key is None:
                val = f"{tm.get('origin_store_name','?')} → {tm.get('dest_store_name','?')}"
            else:
                val = tm.get(key,"—")
            if val is None: val = "—"
            rows += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 10px; width:150px; font-size:12px; color:var(--muted);">{lbl}</td><td style="padding:3px 10px; font-size:12px;"><code>{val}</code></td></tr>'
    return f'<table style="width:100%; border-collapse:collapse; margin-top:6px;">{rows}</table>'


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Chatbot endpoint. Accepts {message: "..."} and returns a response."""
    body = request.get_json(force=True)
    raw_message = (body.get("message") or "").strip()          # original case — for case-ID regex
    message = raw_message.lower()                                # lowercased — for text matching

    if not message:
        return jsonify({"body": "Please type a message."})

    # Case lookup — use raw_message so case IDs preserve their original casing
    # Patterns (checked in order):
    #   "show case V1" / "check case V2" / "what happened in case V3" / "details for case V5"
    #   "show V1" / "check V2" / "visit V3"            (verb + ID, no 'case' word)
    #   "case V1" / "case FT5"                          (standalone 'case ID')
    case_id_match = re.search(
        r'(?:show|check|visit|get|what happened in|details for)\s+(?:case\s+)?([a-zA-Z0-9][a-zA-Z0-9_-]*)', raw_message
    )
    if not case_id_match:
        case_id_match = re.search(r'case\s+([a-zA-Z0-9][a-zA-Z0-9_-]*)', raw_message)
    if case_id_match:
        case_id = case_id_match.group(1)
        state = _find_case_in_session(case_id)
        if state is not None:
            state_dict = serialise_state(state) if not isinstance(state, dict) else state
            audit = state_dict.get("audit_log", []) if isinstance(state_dict, dict) else []
            audit_html = _format_audit_html(audit if isinstance(audit, list) else [])
            return jsonify({
                "body": f"📋 <strong>Case: {state_dict.get('case_id', case_id)}</strong>",
                "stateJson": state_dict,
                "auditHtml": audit_html,
                "label": f"Case {state_dict.get('case_id', case_id)}",
            })

    # List open cases
    if "list open" in message or "open cases" in message or "all cases" in message:
        open_cases = []
        for cid, s in session_states.items():
            if not s.closed:
                sd = serialise_state(s)
                open_cases.append({"caseId": cid, "state": sd,
                    "summary": f"{sd.get('resolution_type','?')} · {sd.get('origin_store_name','?')} · {sd.get('customer_email','?')}"})
        if not open_cases:
            return jsonify({"body": "No open cases in this session. All cases have been resolved."})
        html = '<div style="margin-top:8px;">'
        html += f'<div style="font-size:13px; font-weight:600; color:var(--accent2); margin-bottom:6px;">Open Cases ({len(open_cases)})</div>'
        for c in open_cases:
            html += f'''<div style="background:#f0f4f8; border:1px solid var(--line); border-radius:6px; padding:8px 12px; margin-bottom:6px;">
              <code style="color:var(--accent2); font-size:13px;">{c['caseId']}</code>
              <span style="color:var(--muted); font-size:12px;"> — {c['summary']}</span>
              <span style="float:right; font-size:11px; color:{'var(--warm)' if c['state'].get('closed') else 'var(--accent3)'};">
                {c['state'].get('resolution_type','?')} · {'closed' if c['state'].get('closed') else 'open'}
              </span>
            </div>'''
        html += '</div>'
        return jsonify({"body": html})

    # Show HITL queue
    if "hitl" in message and ("queue" in message or "pending" in message or "supervisor" in message):
        queue_data = api_hitl_queue().get_json()
        queue = queue_data.get("queue", [])
        if not queue:
            return jsonify({"body": "No cases in HITL queue."})
        html = '<div style="margin-top:8px;">'
        html += f'<div style="font-size:13px; font-weight:600; color:var(--hitl); margin-bottom:8px;">🚨 HITL Queue — Supervisor Action Required ({len(queue)})</div>'
        for entry in queue:
            s = entry.get("state", {})
            reason = s.get("escalation_reason") or s.get("escalation_reason_detail") or "No reason specified"
            html += f'''<div style="background:#f5eef8; border:1px solid #7d3c98; border-left:4px solid #7d3c98; border-radius:6px; padding:8px 12px; margin-bottom:8px;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <code style="color:var(--hitl); font-size:13px; font-weight:600;">{entry['caseId']}</code>
                <span style="font-size:11px; color:var(--muted);">{s.get('resolution_type','?')}</span>
              </div>
              <div style="font-size:12px; color:var(--text); margin-top:4px;">
                <strong>Store:</strong> {s.get('origin_store_name','?')}<br>
                <strong>Customer:</strong> {s.get('customer_email','?')}<br>
                <strong>Reason:</strong> {reason}
              </div>
              <div style="margin-top:6px; font-size:11px; color:var(--muted);">
                Nodes: {', '.join([e.get('node_name','?') for e in s.get('audit_log',[])])}
              </div>
            </div>'''
        html += '</div>'
        return jsonify({"body": html, "tab": "hitl"})

    # Total cases
    if "total" in message and ("case" in message or "run" in message):
        total_run = len(session_states)
        total_closed = sum(1 for s in session_states.values() if s.closed)
        total_hitl = len(hitl_store)
        return jsonify({"body": f"📊 <strong>Session Statistics</strong><br>• Total cases run: <strong>{total_run}</strong><br>• Cases closed: <strong>{total_closed}</strong><br>• Cases in HITL: <strong>{total_hitl}</strong>"})

    # Stock/inventory query
    # Match patterns like: "which stores have stock of SS-0087", "show stock SS-0087",
    # "get inventory of SS-0087", "list available SS-0087"
    # Use raw_message so SKU casing is preserved (MOCK_INVENTORY keys are uppercase).
    stock_match = re.search(
        r'(?:which|what|get|show|list).*?(?:stock|inventory|available)\s+(?:of\s+|for\s+)?([a-zA-Z0-9][a-zA-Z0-9_-]*)',
        raw_message,
    )
    if not stock_match:
        # Fallback: SKU directly followed by stock/inventory/available
        stock_match = re.search(r'([a-zA-Z0-9][a-zA-Z0-9_-]*)\s+(?:stock|inventory|available)', raw_message)
    if stock_match:
        sku = stock_match.group(1).upper()  # normalise to uppercase for MOCK_INVENTORY lookup
        from .tools import MOCK_INVENTORY
        stores = MOCK_INVENTORY.get(sku, {})
        if not stores:
            return jsonify({"body": f"❌ No stock data for SKU <code>{sku}</code> in mock inventory."})
        html = '<div style="margin-top:8px;">'
        html += f'<div style="font-size:13px; font-weight:600; color:var(--accent2); margin-bottom:6px;">📦 Stock for <code>{sku}</code></div>'
        total = sum(stores.values())
        html += f'<div style="font-size:12px; color:var(--muted); margin-bottom:8px;">Total: <strong>{total} pairs</strong></div>'
        html += '<table style="width:100%; border-collapse:collapse; font-size:12.5px;">'
        html += '<tr style="background:#eef2f7;"><th style="padding:4px 8px; text-align:left; color:var(--muted);">Store</th><th style="padding:4px 8px; text-align:right; color:var(--muted);">Qty</th></tr>'
        for sid, qty in stores.items():
            sn = {"STORE-Utrecht":"Utrecht","STORE-Rotterdam":"Rotterdam","STORE-Amsterdam":"Amsterdam","STORE-Haarlem":"Haarlem","WAREHOUSE":"Central Warehouse"}.get(sid, sid)
            html += f'<tr style="border-bottom:1px solid var(--line);"><td style="padding:3px 8px;">{sn}</td><td style="padding:3px 8px; text-align:right; font-weight:600;">{qty}</td></tr>'
        html += '</table></div>'
        return jsonify({"body": html})

    # Knowledge questions
    for key, answer in CHAT_KNOWLEDGE.items():
        if key in message:
            return jsonify({"body": answer})

    if "pipeline" in message and ("what" in message or "how" in message or "explain" in message):
        return jsonify({"body": CHAT_KNOWLEDGE.get("what is the pipeline", CHAT_KNOWLEDGE["default"])})

    if "store" in message and ("which" in message or "what" in message):
        return jsonify({"body": CHAT_KNOWLEDGE.get("who are the stores", CHAT_KNOWLEDGE["default"])})

    return jsonify({"body": CHAT_KNOWLEDGE["default"]})


# ── Helpers ─────────────────────────────────────────────────────────────────────

def build_state(cfg: Dict[str, Any]) -> ReturnCaseState:
    """
    Build a ReturnCaseState from the UI config.
    """
    now = datetime.utcnow()

    # Determine SKU-specific overrides for the mock
    sku = cfg.get("sku", "SS-0042")
    is_worn_hint = cfg.get("isWornHint")
    packaging_hint = cfg.get("packagingHint")

    # For known defective batch, force defect flag in CV (handled in tools.py)
    # For worn items, pass is_worn_hint=True

    items = [
        ReturnItem(
            sku=sku,
            size=cfg.get("size", "EU-39"),
            condition_description=cfg.get("conditionDesc") or f"Customer return — {sku}",
            image_urls=cfg.get("imageUrls") or [f"{sku.lower()}_top.jpg", f"{sku.lower()}_side.jpg", f"{sku.lower()}_sole.jpg"],
            is_worn=is_worn_hint if is_worn_hint is not None else False,
            packaging_intact=packaging_hint if packaging_hint is not None else True,
            purchase_price_eur=Decimal(str(cfg.get("price", 89.95))),
        )
    ]

    receipt_id = cfg.get("receiptId") or ""
    # Allow caller to override the purchase date (e.g. to simulate an
    # out-of-window return). When not provided, intake_eval will determine
    # it from the receipt ledger via verify_receipt_data.
    _purchase_date = None
    if cfg.get("purchaseDate"):
        try:
            _purchase_date = datetime.strptime(cfg["purchaseDate"], "%Y-%m-%d")
        except (ValueError, TypeError):
            _purchase_date = None
    resolution_type = (
        ResolutionType(cfg["resolution"])
        if cfg.get("resolution") and cfg["resolution"] in [e.value for e in ResolutionType]
        else None
    )

    return ReturnCaseState(
        case_id=cfg.get("caseId") or f"RC-PREV-{random.randint(1000, 9999)}",
        customer_id=cfg.get("customerId", "CUST-PREVIEW-001"),
        customer_email=cfg.get("email", "preview@demo.nl"),
        customer_phone=cfg.get("phone", "+31612345678"),
        origin_store_id=cfg.get("storeId", "STORE-Utrecht"),
        origin_store_name=cfg.get("storeName", "Sallie se Schoenen — Utrecht"),
        intake_timestamp=now,
        purchase_date=_purchase_date,
        receipt_id=receipt_id,
        proof_verified=False,
        receipt_verification=None,
        condition_verdict=None,
        policy_result=None,
        resolution_type=resolution_type,
        resolution_amount_eur=Decimal("0.00"),
        escalation_reason=None,
        items=items,
        stock_matrix=None,
        hold_reservation=None,
        transfer_manifest=None,
        alternative_offer_made=False,
        alternative_offer_details=None,
        defect_detected=False,
        supplier_claim=None,
        rma_evidence_package_ref=None,
        escalated_to_supervisor=False,
        escalation_timestamp=None,
        escalation_reason_detail=None,
        supervisor_notes=None,
        supervisor_resolved_at=None,
        customer_notification_status=NotificationStatus.NOT_SENT,
        notification_channels=[],
        notified_at=None,
        notification_message_ref=None,
        dead_letter_reason=DeadLetterReason.NONE,
        dead_letter_timestamp=None,
        closed=False,
        closed_at=None,
        audit_log=[],
    )


def run_pipeline(state: ReturnCaseState) -> Dict[str, Any]:
    """
    Run the full pipeline and return the final state + node visits.
    """
    random.seed(42)  # deterministic CV mock for preview

    orchestrator = SupervisorOrchestrator()
    result = orchestrator.run(state)

    save_state(result)
    if result.escalated_to_supervisor:
        hitl_store[result.case_id] = result
    else:
        hitl_store.pop(result.case_id, None)

    return {
        "state": serialise_state(result),
        "hitlCaseId": result.case_id if result.escalated_to_supervisor else None,
        "message": (
            f"Case {result.case_id}: CLOSED — {result.resolution_type.value if result.resolution_type else 'RESOLVED'}"
            if result.closed
            else f"Case {result.case_id}: PAUSED at HITL — {result.resolution_type.value if result.resolution_type else 'PENDING'}"
        ),
        "nodeVis": build_node_vis(result),
        "auditLog": [e.__dict__ for e in result.audit_log],
    }


def build_node_vis(state: ReturnCaseState) -> list:
    """
    Build a list of node visit records for display, based on the audit log.
    """
    nodes = [
        ("intake_eval", "① Intake & Condition"),
        ("cross_store_lookup", "② Cross-Store Proof"),
        ("policy_check", "③ Policy Check"),
        ("stock_resolve", "④a Stock & Logistics"),
        ("defect_assess", "④b Defect Assess"),
        ("execute_till_refund", "⑤a Till Refund"),
        ("transfer_dispatcher", "⑤b Transfer"),
        ("assemble_supplier_rma", "⑤c Supplier RMA"),
        ("notify_customer", "⑥ Notify & Close"),
    ]

    audit_nodes = {e.node_name for e in state.audit_log}
    hitl_paused = state.escalated_to_supervisor
    closed = state.closed

    vis = []
    for node_name, display in nodes:
        if node_name in audit_nodes:
            vis.append({"name": display, "status": "run"})
        else:
            vis.append({"name": display, "status": "skip"})

    if hitl_paused:
        vis.append({"name": "⏸ HITL — Supervisor", "status": "hitl"})
    if closed:
        vis.append({"name": "✓ Case Closed", "status": "close"})

    return vis


def serialise_state(state: ReturnCaseState) -> Dict[str, Any]:
    """
    Convert a ReturnCaseState to a JSON-serialisable dict.
    """
    def fmt_dt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None

    return {
        "case_id": state.case_id,
        "customer_id": state.customer_id,
        "customer_email": state.customer_email,
        "customer_phone": state.customer_phone,
        "origin_store_id": state.origin_store_id,
        "origin_store_name": state.origin_store_name,
        "intake_timestamp": fmt_dt(state.intake_timestamp),
        "purchase_date": fmt_dt(state.purchase_date),
        "receipt_id": state.receipt_id,
        "proof_verified": state.proof_verified,
        "receipt_verification": (
            {
                "receipt_id": state.receipt_verification.receipt_id,
                "valid": state.receipt_verification.valid,
                "days_since_purchase": state.receipt_verification.days_since_purchase,
                "store_id": state.receipt_verification.store_id,
                "store_name": state.receipt_verification.store_name,
                "transaction_amount_eur": str(state.receipt_verification.transaction_amount_eur),
                "payment_method": state.receipt_verification.payment_method,
                "purchase_date": fmt_dt(state.receipt_verification.purchase_date),
                "verification_method": state.receipt_verification.verification_method,
            }
            if state.receipt_verification else None
        ),
        "condition_verdict": (
            {
                "condition_status": state.condition_verdict.condition_status.value if state.condition_verdict.condition_status else None,
                "is_worn": state.condition_verdict.is_worn,
                "packaging_intact": state.condition_verdict.packaging_intact,
                "wear_evidence": state.condition_verdict.wear_evidence,
                "defect_suspected": state.condition_verdict.defect_suspected,
                "defect_confidence": state.condition_verdict.defect_confidence,
                "is_manufacturing_fault": state.condition_verdict.is_manufacturing_fault,
                "fault_type": state.condition_verdict.fault_type,
                "photo_quality_passed": state.condition_verdict.photo_quality_passed,
                "missing_angles": state.condition_verdict.missing_angles,
                "cv_model_version": state.condition_verdict.cv_model_version,
            }
            if state.condition_verdict else None
        ),
        "policy_result": (
            {
                "within_30_day_window": state.policy_result.within_30_day_window,
                "is_unworn": state.policy_result.is_unworn,
                "refund_amount_eur": str(state.policy_result.refund_amount_eur),
                "under_refund_cap": state.policy_result.under_refund_cap,
                "escalation_reason": state.policy_result.escalation_reason,
                "resolution_type": state.policy_result.resolution_type.value if state.policy_result.resolution_type else None,
            }
            if state.policy_result else None
        ),
        "resolution_type": state.resolution_type.value if state.resolution_type else None,
        "resolution_amount_eur": str(state.resolution_amount_eur),
        "escalation_reason": state.escalation_reason,
        "items": [
            {
                "sku": i.sku,
                "size": i.size,
                "condition_description": i.condition_description,
                "image_urls": i.image_urls or [],
                "imageUrls": i.image_urls or [],
                "is_worn": i.is_worn,
                "packaging_intact": i.packaging_intact,
                "purchase_price_eur": str(i.purchase_price_eur),
            }
            for i in state.items
        ],
        "stock_matrix": (
            {
                "sku": state.stock_matrix.sku,
                "size": state.stock_matrix.size,
                "total_available": state.stock_matrix.total_available,
                "has_local_stock": state.stock_matrix.has_local_stock,
                "has_network_stock": state.stock_matrix.has_network_stock,
                "items": [
                    {
                        "store_id": i.store_id,
                        "store_name": i.store_name,
                        "quantity_available": i.quantity_available,
                        "is_verified": i.is_verified,
                    }
                    for i in state.stock_matrix.items
                ],
            }
            if state.stock_matrix else None
        ),
        "hold_reservation": (
            {
                "hold_id": state.hold_reservation.hold_id,
                "sku": state.hold_reservation.sku,
                "size": state.hold_reservation.size,
                "store_id": state.hold_reservation.store_id,
                "store_name": state.hold_reservation.store_name,
                "quantity": state.hold_reservation.quantity,
                "ttl_seconds": state.hold_reservation.ttl_seconds,
                "acquired_at": fmt_dt(state.hold_reservation.acquired_at),
                "expires_at": fmt_dt(state.hold_reservation.expires_at),
                "status": state.hold_reservation.status,
            }
            if state.hold_reservation else None
        ),
        "transfer_manifest": (
            {
                "manifest_id": state.transfer_manifest.manifest_id,
                "origin_store_id": state.transfer_manifest.origin_store_id,
                "origin_store_name": state.transfer_manifest.origin_store_name,
                "dest_store_id": state.transfer_manifest.dest_store_id,
                "dest_store_name": state.transfer_manifest.dest_store_name,
                "sku": state.transfer_manifest.sku,
                "size": state.transfer_manifest.size,
                "quantity": state.transfer_manifest.quantity,
                "hold_id": state.transfer_manifest.hold_id,
                "status": state.transfer_manifest.status,
                "scheduled_collection_date": fmt_dt(state.transfer_manifest.scheduled_collection_date),
                "created_at": fmt_dt(state.transfer_manifest.created_at),
            }
            if state.transfer_manifest else None
        ),
        "alternative_offer_made": state.alternative_offer_made,
        "alternative_offer_details": state.alternative_offer_details,
        "defect_detected": state.defect_detected,
        "supplier_claim": (
            {
                "claim_id": state.supplier_claim.claim_id,
                "supplier_id": state.supplier_claim.supplier_id,
                "supplier_name": state.supplier_claim.supplier_name,
                "rma_number": state.supplier_claim.rma_number,
                "status": state.supplier_claim.status.value if state.supplier_claim.status else None,
                "evidence_package_ref": state.supplier_claim.evidence_package_ref,
                "submitted_at": fmt_dt(state.supplier_claim.submitted_at),
                "approved_at": fmt_dt(state.supplier_claim.approved_at),
                "credit_eur": str(state.supplier_claim.credit_eur) if state.supplier_claim.credit_eur else None,
            }
            if state.supplier_claim else None
        ),
        "rma_evidence_package_ref": state.rma_evidence_package_ref,
        "escalated_to_supervisor": state.escalated_to_supervisor,
        "escalation_timestamp": fmt_dt(state.escalation_timestamp),
        "escalation_reason_detail": state.escalation_reason_detail,
        "supervisor_notes": state.supervisor_notes,
        "supervisor_resolved_at": fmt_dt(state.supervisor_resolved_at),
        "customer_notification_status": state.customer_notification_status.value,
        "notification_channels": state.notification_channels,
        "notified_at": fmt_dt(state.notified_at),
        "notification_message_ref": state.notification_message_ref,
        "dead_letter_reason": state.dead_letter_reason.value,
        "dead_letter_timestamp": fmt_dt(state.dead_letter_timestamp),
        "closed": state.closed,
        "closed_at": fmt_dt(state.closed_at),
        "audit_log": [e.__dict__ for e in state.audit_log],
    }


# ── In-memory stores (session-scoped) ──────────────────────────────────────────

session_states: Dict[str, ReturnCaseState] = {}
hitl_store: Dict[str, ReturnCaseState] = {}


def load_state(case_id: str) -> Optional[ReturnCaseState]:
    return session_states.get(case_id)


def save_state(state: ReturnCaseState) -> None:
    session_states[state.case_id] = state


def seed_demo_cases():
    """Pre-seed representative cases so the chatbot, stats, and HITL queue have data on boot."""
    try:
        import random
        random.seed(42)
        from . import demo
        orch = SupervisorOrchestrator()

        # Case 1: Standard refund (closed)
        s1 = demo.case_1()
        r1 = orch.run(s1)
        save_state(r1)
        session_states["V1"] = r1

        # Case 2: Exchange with transfer (closed)
        s2 = demo.case_2()
        r2 = orch.run(s2)
        save_state(r2)
        session_states["V2"] = r2

        # Case 3: Missing receipt -> HITL pause (open in HITL queue)
        s3 = demo.case_3()
        r3 = orch.run(s3)
        save_state(r3)
        session_states["V3"] = r3
        if r3.escalated_to_supervisor:
            hitl_store[r3.case_id] = r3
            hitl_store["V3"] = r3

        # Case 4: Defect case (closed)
        s4 = demo.case_4()
        r4 = orch.run(s4)
        save_state(r4)
        session_states["V4"] = r4

        # Case 5: Worn out of window (closed after supervisor rejection)
        s5 = demo.case_5()
        r5 = orch.run(s5)
        if r5.escalated_to_supervisor:
            r5 = orch.resume_from_hitl(r5, supervisor_resolution="REJECT_RETURN", supervisor_notes="Supervisor reviewed and rejected.")
        save_state(r5)
        session_states["V5"] = r5
    except Exception as e:
        logger.warning("Could not pre-seed demo cases: %s", e)


seed_demo_cases()


# ── Run ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8765))
    print(f"\n  Sallie se Schoenen — Returns Agent Preview")
    print(f"  Running on http://0.0.0.0:{port}")
    print(f"  (Preview: https://{os.environ.get('SANDBOX_ID', 'localhost')}-8765.e2b.app)\n")
    app.run(host="0.0.0.0", port=port, debug=False)
