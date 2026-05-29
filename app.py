# -*- coding: utf-8 -*-
"""DSI 專案任務管理 - 獨立服務"""
import os
import re
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials

_TW    = timezone(timedelta(hours=8))
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

PROJECT_SHEET_ID = os.environ.get("PROJECT_SHEET_ID", "1sDrlLUehorFkH_379k3f_fvv0ZmlS9-aQ-wBWOqoCx4")

_gc          = None
_sh_project  = None

def _get_client():
    global _gc
    if _gc is None:
        creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _gc = gspread.authorize(creds)
    return _gc

def _get_project_sheet():
    global _sh_project
    if _sh_project is None:
        _sh_project = _get_client().open_by_key(PROJECT_SHEET_ID)
    return _sh_project

app = Flask(__name__)

# ── 色彩常數 ──────────────────────────────────────────
_COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.475}
_COL_EVEN   = {"red": 0.863, "green": 0.902, "blue": 0.945}
_COL_ODD    = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
_COL_MILE   = {"red": 1.0,   "green": 0.753, "blue": 0.0}
_COL_WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

TASK_HEADERS = ["工作名稱", "工期", "開始時間", "結束時間", "前置任務", "OWNER", "完成百分比", "里程碑", "建立時間"]


def _ensure_project_tab(project_name: str):
    sh     = _get_project_sheet()
    titles = [ws.title for ws in sh.worksheets()]
    if project_name in titles:
        ws = sh.worksheet(project_name)
        if ws.row_values(1) != TASK_HEADERS:
            ws.update("A1:I1", [TASK_HEADERS])
            ws.format("A1:I1", {
                "backgroundColor": _COL_HEADER,
                "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": _COL_WHITE},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            })
        return ws

    ws = sh.add_worksheet(title=project_name, rows=500, cols=10)
    ws.update("A1:I1", [TASK_HEADERS])
    ws.format("A1:I1", {
        "backgroundColor": _COL_HEADER,
        "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": _COL_WHITE},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
    })
    ws.freeze(rows=1)
    return ws


def _write_task_row(ws, task: dict, now: str):
    is_milestone = task.get("milestone", "") in ("是", "Y", "yes", "YES", "true", "★ 里程碑")
    row_data = [
        task.get("task_name", ""),
        task.get("duration", ""),
        task.get("start_date", ""),
        task.get("finish_date", ""),
        task.get("predecessors", ""),
        task.get("resource", ""),
        task.get("percent", "0%"),
        "★ 里程碑" if is_milestone else "",
        now,
    ]
    next_row = len(ws.col_values(1)) + 1
    if next_row < 2:
        next_row = 2
    ws.update(f"A{next_row}:I{next_row}", [row_data])
    row_range = f"A{next_row}:I{next_row}"
    if is_milestone:
        ws.format(row_range, {"backgroundColor": _COL_MILE, "textFormat": {"bold": True}})
    else:
        bg = _COL_EVEN if next_row % 2 == 0 else _COL_ODD
        ws.format(row_range, {"backgroundColor": bg})
    return next_row, is_milestone


# ── API ───────────────────────────────────────────────

@app.route("/api/project_tabs")
def api_project_tabs():
    try:
        sh   = _get_project_sheet()
        tabs = [ws.title for ws in sh.worksheets()]
        return jsonify({"ok": True, "tabs": tabs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/project_data")
def api_project_data():
    try:
        sh     = _get_project_sheet()
        result = []
        for ws in sh.worksheets():
            rows = ws.get_all_values()
            if not rows:
                continue
            header = rows[0]
            def ci(name):
                try: return header.index(name)
                except ValueError: return None
            ii = {k: ci(k) for k in TASK_HEADERS}
            tasks = []
            for row in rows[1:]:
                if not any(row): continue
                def g(k):
                    i = ii[k]
                    return row[i] if i is not None and i < len(row) else ""
                tasks.append({
                    "task_name":   g("工作名稱"),
                    "duration":    g("工期"),
                    "start_date":  g("開始時間"),
                    "finish_date": g("結束時間"),
                    "predecessors":g("前置任務"),
                    "owner":       g("OWNER"),
                    "percent":     g("完成百分比"),
                    "milestone":   g("里程碑"),
                    "created":     g("建立時間"),
                })
            result.append({"project": ws.title, "tasks": tasks})
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/add_task", methods=["POST"])
def api_add_task():
    try:
        body    = request.get_json(force=True)
        project = (body.get("project") or "").strip()
        if not project:
            return jsonify({"ok": False, "error": "專案名稱不能為空"}), 400
        task = {
            "task_name":    body.get("task_name", ""),
            "duration":     body.get("duration", ""),
            "start_date":   body.get("start_date", ""),
            "finish_date":  body.get("finish_date", ""),
            "predecessors": body.get("predecessors", ""),
            "resource":     body.get("owner", ""),
            "percent":      body.get("percent", "0%"),
            "milestone":    body.get("milestone", ""),
        }
        if not task["task_name"]:
            return jsonify({"ok": False, "error": "工作名稱不能為空"}), 400
        now = datetime.now(_TW).strftime("%Y-%m-%d %H:%M:%S")
        ws  = _ensure_project_tab(project)
        _write_task_row(ws, task, now)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/delete_task", methods=["POST"])
def api_delete_task():
    try:
        body    = request.get_json(force=True)
        project = (body.get("project") or "").strip()
        row_num = int(body.get("row", 0))
        if not project or row_num < 2:
            return jsonify({"ok": False, "error": "參數不正確"}), 400
        sh = _get_project_sheet()
        ws = sh.worksheet(project)
        ws.delete_rows(row_num)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 主頁 HTML ──────────────────────────────────────────

@app.route("/")
@app.route("/projects")
def projects():
    return """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSI 專案任務總覽</title>
<style>
:root{
  --bg:#0D1B34;--hdr:#07102A;--blue:#0078C8;--bluedark:#005AA0;--bluelight:#1A90D8;
  --panel:#0F2040;--card:#112040;--white:#FFFFFF;--gray:#8CB4D2;--lightgray:#B0C8DC;
  --green:#64DC82;--yellow:#FFB400;--red:#FF6E64;--border:#1A3A60;--border2:#0F2A50;
  --rowalt:rgba(255,255,255,0.02);--rowhover:rgba(0,120,200,0.08);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft JhengHei","微軟正黑體",Arial,sans-serif;background:var(--bg);color:var(--white);font-size:13px}
.header{background:var(--hdr);border-bottom:1px solid var(--border2);padding:0 24px;height:52px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}
.header h1{font-size:14px;font-weight:700;flex:1}
.btn{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;font-family:inherit;transition:background .15s;white-space:nowrap}
.btn-add{background:var(--blue);color:#fff}
.btn-add:hover{background:var(--bluedark)}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--gray)}
.btn-ghost:hover{border-color:var(--blue);color:var(--blue)}
.ts{font-size:11px;color:var(--gray)}
.content{padding:20px 24px}
.project-section{background:var(--card);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:18px}
.proj-header{background:var(--panel);border-bottom:1px solid var(--border);padding:11px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.proj-name{font-size:13px;font-weight:700;color:var(--lightgray)}
.proj-stat{font-size:10px;color:var(--gray);margin-left:auto}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
thead{background:var(--panel)}
th{text-align:left;padding:8px 14px;font-size:10px;color:var(--gray);font-weight:600;letter-spacing:.5px;text-transform:uppercase;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:8px 14px;font-size:12px;border-bottom:1px solid var(--border2);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:nth-child(even)>td{background:var(--rowalt)}
tr:hover>td{background:var(--rowhover)}
.mile-row>td{background:rgba(255,180,0,.08)!important;font-weight:600;color:var(--yellow)}
.mile-row:hover>td{background:rgba(255,180,0,.13)!important}
.owner-tag{display:inline-block;padding:2px 8px;border-radius:3px;background:rgba(0,120,200,.2);color:var(--bluelight);font-size:10px;font-weight:700}
.pbar-wrap{background:var(--border);border-radius:2px;height:4px;width:80px;display:inline-block;vertical-align:middle}
.pbar{height:4px;border-radius:2px}
.pct-label{font-size:10px;color:var(--gray);margin-left:5px}
.empty{padding:40px;text-align:center;color:var(--gray)}
.loading{padding:48px;text-align:center;color:var(--gray)}
/* Modal */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.overlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px #000a}
.modal h2{font-size:13px;font-weight:700;color:var(--lightgray);margin-bottom:18px;padding-bottom:10px;border-bottom:1px solid var(--border2)}
.form-row{margin-bottom:12px}
.form-row label{display:block;font-size:10px;font-weight:600;color:var(--gray);letter-spacing:.5px;text-transform:uppercase;margin-bottom:5px}
.form-row input,.form-row select{width:100%;padding:7px 10px;background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--white);font-size:12px;font-family:inherit;outline:none;transition:border-color .15s}
.form-row input:focus,.form-row select:focus{border-color:var(--blue)}
.form-row input::placeholder{color:var(--gray)}
.form-row select option{background:var(--panel)}
.form-row.half{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-row.half label{grid-column:1/-1}
.form-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px;padding-top:14px;border-top:1px solid var(--border2)}
/* Toast */
.toast{position:fixed;bottom:24px;right:24px;background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--green);color:var(--white);padding:12px 20px;border-radius:6px;font-size:12px;z-index:200;opacity:0;pointer-events:none;transition:opacity .3s}
.toast.show{opacity:1}
.toast.err{border-left-color:var(--red)}
</style>
</head>
<body>
<div class="header">
  <h1>📋 DSI 專案任務總覽</h1>
  <span class="ts" id="ts"></span>
  <button class="btn btn-add" onclick="openModal()">＋ 新增任務</button>
  <button class="btn btn-ghost" onclick="loadData()">↻ 重新整理</button>
</div>
<div class="content"><div id="content"><div class="loading">載入中...</div></div></div>

<!-- Modal -->
<div class="overlay" id="overlay" onclick="overlayClick(event)">
  <div class="modal">
    <h2>新增任務</h2>
    <div class="form-row">
      <label>專案 *</label>
      <select id="f-project"><option value="">選擇或輸入專案</option></select>
    </div>
    <div class="form-row">
      <label>新專案名稱（若上方沒有）</label>
      <input id="f-newproject" placeholder="輸入新專案名稱">
    </div>
    <div class="form-row">
      <label>工作名稱 *</label>
      <input id="f-task" placeholder="任務名稱">
    </div>
    <div class="form-row half">
      <label>開始時間 / 結束時間</label>
      <input id="f-start" type="date">
      <input id="f-end" type="date">
    </div>
    <div class="form-row half">
      <label>工期 / OWNER</label>
      <input id="f-dur" placeholder="例：5天">
      <input id="f-owner" placeholder="負責人">
    </div>
    <div class="form-row half">
      <label>完成百分比 / 前置任務</label>
      <input id="f-pct" placeholder="0%" value="0%">
      <input id="f-pre" placeholder="前置任務編號">
    </div>
    <div class="form-row">
      <label>里程碑</label>
      <select id="f-mile">
        <option value="">否</option>
        <option value="★ 里程碑">是（里程碑）</option>
      </select>
    </div>
    <div class="form-actions">
      <button class="btn btn-ghost" onclick="closeModal()">取消</button>
      <button class="btn btn-add" id="btn-submit" onclick="submitTask()">新增</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allTabs = [];

async function loadData() {
  document.getElementById('content').innerHTML = '<div class="loading">載入中...</div>';
  document.getElementById('ts').textContent = '';
  try {
    const [dRes, tRes] = await Promise.all([
      fetch('/api/project_data'),
      fetch('/api/project_tabs'),
    ]);
    const dJson = await dRes.json();
    const tJson = await tRes.json();
    if (!dJson.ok) throw new Error(dJson.error);
    renderProjects(dJson.data);
    document.getElementById('ts').textContent = '更新：' + new Date().toLocaleTimeString('zh-TW');
    if (tJson.ok) {
      allTabs = tJson.tabs;
      const sel = document.getElementById('f-project');
      sel.innerHTML = '<option value="">選擇或輸入專案</option>' +
        allTabs.map(n => `<option value="${n}">${n}</option>`).join('');
    }
  } catch(e) {
    document.getElementById('content').innerHTML = `<div class="empty">載入失敗：${e.message}</div>`;
  }
}

function renderProjects(data) {
  if (!data.length) {
    document.getElementById('content').innerHTML = '<div class="empty">目前沒有任何專案資料</div>';
    return;
  }
  let html = '';
  for (const proj of data) {
    const total = proj.tasks.length;
    const done  = proj.tasks.filter(t => (t.percent||'').replace('%','').trim() === '100').length;
    const miles = proj.tasks.filter(t => t.milestone && t.milestone.includes('★')).length;
    const rows  = proj.tasks.map(t => {
      const pctRaw = parseFloat((t.percent||'0').replace('%','')) || 0;
      const pct    = Math.min(100, Math.max(0, pctRaw));
      const pcolor = pct < 30 ? '#f59f00' : pct < 80 ? '#2196f3' : '#43a047';
      const isMile = t.milestone && t.milestone.includes('★');
      return `<tr class="${isMile ? 'mile-row' : ''}">
        <td>${isMile ? '★ ' : ''}${t.task_name||'─'}</td>
        <td>${t.duration||'─'}</td>
        <td>${t.start_date||'─'}</td>
        <td>${t.finish_date||'─'}</td>
        <td>${t.predecessors||'─'}</td>
        <td>${t.owner ? '<span class="owner-tag">'+t.owner+'</span>' : '─'}</td>
        <td>
          <div class="pbar-wrap"><div class="pbar" style="width:${pct}%;background:${pcolor}"></div></div>
          <span class="pct-label">${pct}%</span>
        </td>
        <td style="color:var(--gray);font-size:11px">${(t.created||'').slice(0,10)||'─'}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="8" style="text-align:center;color:var(--gray);padding:18px">尚無任務</td></tr>';

    html += `<div class="project-section">
      <div class="proj-header">
        <span class="proj-name">📁 ${proj.project}</span>
        <span class="proj-stat">共 ${total} 任務 &nbsp;·&nbsp; 完成 ${done} &nbsp;·&nbsp; 里程碑 ${miles}</span>
        <button class="btn btn-add" style="padding:4px 12px;font-size:11px;margin-left:8px"
          onclick="openModalFor('${proj.project.replace(/'/g,"\\'")}')">＋ 新增</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>工作名稱</th><th>工期</th><th>開始</th><th>結束</th>
            <th>前置任務</th><th>OWNER</th><th>進度</th><th>建立</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  }
  document.getElementById('content').innerHTML = html;
}

function openModal(project) {
  document.getElementById('overlay').classList.add('show');
  if (project) {
    document.getElementById('f-project').value = project;
    document.getElementById('f-newproject').value = '';
  }
}
function openModalFor(proj) { openModal(proj); }
function closeModal() { document.getElementById('overlay').classList.remove('show'); }
function overlayClick(e) { if (e.target === document.getElementById('overlay')) closeModal(); }

async function submitTask() {
  const project = document.getElementById('f-newproject').value.trim()
                || document.getElementById('f-project').value.trim();
  const task    = document.getElementById('f-task').value.trim();
  if (!project) { showToast('請選擇或輸入專案名稱', false); return; }
  if (!task)    { showToast('請填寫工作名稱', false); return; }

  const btn = document.getElementById('btn-submit');
  btn.disabled = true; btn.textContent = '新增中...';
  try {
    const res  = await fetch('/api/add_task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        project,
        task_name:    task,
        duration:     document.getElementById('f-dur').value,
        start_date:   document.getElementById('f-start').value,
        finish_date:  document.getElementById('f-end').value,
        predecessors: document.getElementById('f-pre').value,
        owner:        document.getElementById('f-owner').value,
        percent:      document.getElementById('f-pct').value || '0%',
        milestone:    document.getElementById('f-mile').value,
      })
    });
    const json = await res.json();
    if (!json.ok) throw new Error(json.error);
    closeModal();
    showToast('✅ 任務已新增');
    ['f-task','f-dur','f-start','f-end','f-pre','f-owner','f-newproject'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('f-pct').value = '0%';
    document.getElementById('f-mile').value = '';
    loadData();
  } catch(e) {
    showToast('新增失敗：' + e.message, false);
  } finally {
    btn.disabled = false; btn.textContent = '新增';
  }
}

let _toastTimer = null;
function showToast(msg, ok = true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (ok ? '' : ' err');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = 'toast'; }, 3500);
}

loadData();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False)
