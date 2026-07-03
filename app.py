# -*- coding: utf-8 -*-
"""DSI 專案任務管理 - 獨立服務"""
import os
import re
import json
import traceback
import requests
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
MEETING_TOKEN    = os.environ.get("MEETING_TOKEN", "")
MEETING_SHEET_ID = "1EmtsVvGnsNg7o27WpOpbv7BA4_rlwTWOtfopN1OJAEQ"
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")

EXPENSE_SHEET_ID = "1yf62_kTCfEPt0hYg5IoGsW7_EDddGG2Ft5yiisqLPhM"
EXPENSE_HEADERS  = ["摘要", "項目", "發票號碼", "請款人", "日期",
                    "研發相關", "加油費", "交通費", "房租", "行銷",
                    "郵寄費", "旅費", "餐費", "工程", "辦公室補給", "備註", "發票圖片"]
EXPENSE_COLS     = ["研發相關", "加油費", "交通費", "房租", "行銷",
                    "郵寄費", "旅費", "餐費", "工程", "辦公室補給"]
IMGBB_KEY        = os.environ.get("IMGBB_API_KEY", "2121c99497653d5d8b41486ed00aeb42")

_gc          = None
_sh_project  = None
_sh_meeting  = None
_sh_expense  = None

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

def _get_meeting_sheet():
    global _sh_meeting
    if _sh_meeting is None:
        _sh_meeting = _get_client().open_by_key(MEETING_SHEET_ID)
    return _sh_meeting

def _get_expense_sheet():
    global _sh_expense
    if _sh_expense is None:
        _sh_expense = _get_client().open_by_key(EXPENSE_SHEET_ID)
    return _sh_expense

def _get_user_tab(requester: str):
    sh       = _get_expense_sheet()
    existing = [ws.title for ws in sh.worksheets()]
    if requester not in existing:
        ws = sh.add_worksheet(title=requester, rows=1000, cols=17)
        ws.update("A1:Q1", [EXPENSE_HEADERS])
        ws.format("A1:Q1", {
            "backgroundColor": {"red": 0.122, "green": 0.306, "blue": 0.475},
            "textFormat": {
                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                "bold": True,
            },
            "horizontalAlignment": "CENTER",
        })
        ws.freeze(rows=1)
    return sh.worksheet(requester)

def _upload_to_imgbb(image_bytes: bytes, filename: str) -> str:
    import base64
    b64  = base64.b64encode(image_bytes).decode("utf-8")
    resp = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": IMGBB_KEY, "image": b64, "name": filename},
        timeout=30,
    )
    resp.raise_for_status()
    url = resp.json()["data"]["url"]
    return f'=HYPERLINK("{url}","📷 查看發票")'

def _analyze_invoice(image_bytes: bytes) -> dict:
    from google import genai as _genai
    from google.genai import types as _gt
    client   = _genai.Client(api_key=GEMINI_API_KEY)
    col_list = "、".join(EXPENSE_COLS)
    prompt = (
        "你是台灣公司請款AI。分析這張發票或收據圖片，只回傳JSON，不要解釋。\n"
        f"expense_col 必須從以下選一個：{col_list}\n"
        "{\n"
        '  "date": "YYYY-MM-DD（發票日期，若無則今天）",\n'
        '  "invoice_number": "發票號碼（若無則空字串）",\n'
        '  "amount": 金額數字（整數，台幣）,\n'
        '  "items": "品項描述（簡短）",\n'
        '  "expense_col": "費用欄位",\n'
        '  "summary": "摘要（一句話）",\n'
        '  "notes": "備註（若有特殊說明）"\n'
        "}"
    )
    image_part = _gt.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    resp = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[prompt, image_part],
        config=_gt.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(resp.text)

def handle_invoice_image(user_id: str, group_id: str, message_id: str, reply_token: str):
    _TW_now = datetime.now(_TW)
    try:
        img_resp = requests.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {MEETING_TOKEN}"},
            timeout=15,
        )
        if img_resp.status_code != 200:
            _reply_meeting(reply_token, "⚠️ 無法取得圖片，請重試。")
            return

        image_bytes = img_resp.content
        data        = _analyze_invoice(image_bytes)
        requester   = _get_display_name(group_id, user_id)
        expense_col = data.get("expense_col", "")
        if expense_col not in EXPENSE_COLS:
            expense_col = EXPENSE_COLS[0]

        filename      = f"invoice_{_TW_now.strftime('%Y%m%d_%H%M%S')}_{user_id[:6]}.jpg"
        image_formula = _upload_to_imgbb(image_bytes, filename)

        row = [""] * 17
        row[0]  = data.get("summary", "")
        row[1]  = data.get("items", "")
        row[2]  = data.get("invoice_number", "")
        row[3]  = requester
        row[4]  = data.get("date", _TW_now.strftime("%Y-%m-%d"))
        col_idx = EXPENSE_HEADERS.index(expense_col)
        row[col_idx] = data.get("amount", "")
        row[15] = data.get("notes", "")
        row[16] = image_formula

        ws = _get_user_tab(requester)
        ws.append_row(row, value_input_option="USER_ENTERED")

        _reply_meeting(reply_token, (
            f"✅ 發票已記錄\n"
            f"━━━━━━━━━━━━━━\n"
            f"請款人：{requester}\n"
            f"日　期：{row[4]}\n"
            f"發票號：{row[2] or '─'}\n"
            f"品　項：{row[1]}\n"
            f"金　額：NT$ {data.get('amount', '─')}\n"
            f"類　別：{expense_col}\n"
            f"圖　片：已同步至 Sheet"
        ))

    except Exception:
        print(traceback.format_exc(), flush=True)
        _reply_meeting(reply_token, "⚠️ 發票辨識失敗，請確認圖片清晰後重試。")

app = Flask(__name__)

# ── 色彩常數 ──────────────────────────────────────────
_COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.475}
_COL_EVEN   = {"red": 0.863, "green": 0.902, "blue": 0.945}
_COL_ODD    = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
_COL_MILE   = {"red": 1.0,   "green": 0.753, "blue": 0.0}
_COL_WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

TASK_HEADERS = ["工作名稱", "工期", "開始時間", "結束時間", "前置任務", "OWNER", "完成百分比", "里程碑", "建立時間", "Done"]


def _ensure_project_tab(project_name: str):
    sh     = _get_project_sheet()
    titles = [ws.title for ws in sh.worksheets()]
    if project_name in titles:
        ws = sh.worksheet(project_name)
        if ws.row_values(1) != TASK_HEADERS:
            ws.update("A1:J1", [TASK_HEADERS])
            ws.format("A1:J1", {
                "backgroundColor": _COL_HEADER,
                "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": _COL_WHITE},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            })
        return ws

    ws = sh.add_worksheet(title=project_name, rows=500, cols=11)
    ws.update("A1:J1", [TASK_HEADERS])
    ws.format("A1:J1", {
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
        "",  # Done
    ]
    next_row = len(ws.col_values(1)) + 1
    if next_row < 2:
        next_row = 2
    ws.update(f"A{next_row}:J{next_row}", [row_data])
    row_range = f"A{next_row}:J{next_row}"
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
            for row_idx, row in enumerate(rows[1:], start=2):
                if not any(row): continue
                def g(k, _row=row):
                    i = ii[k]
                    return _row[i] if i is not None and i < len(_row) else ""
                tasks.append({
                    "row":          row_idx,
                    "task_name":    g("工作名稱"),
                    "duration":     g("工期"),
                    "start_date":   g("開始時間"),
                    "finish_date":  g("結束時間"),
                    "predecessors": g("前置任務"),
                    "owner":        g("OWNER"),
                    "percent":      g("完成百分比"),
                    "milestone":    g("里程碑"),
                    "created":      g("建立時間"),
                    "done":         row[9] if len(row) > 9 else g("Done"),
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


@app.route("/api/done_task", methods=["POST"])
def api_done_task():
    try:
        body    = request.get_json(force=True)
        project = (body.get("project") or "").strip()
        row_num = int(body.get("row", 0))
        if not project or row_num < 2:
            return jsonify({"ok": False, "error": "參數不正確"}), 400
        sh = _get_project_sheet()
        ws = sh.worksheet(project)
        ws.update_cell(row_num, 10, "✓")
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
table{width:100%;border-collapse:collapse;table-layout:fixed}
thead{background:var(--panel)}
th{text-align:left;padding:8px 14px;font-size:10px;color:var(--gray);font-weight:600;letter-spacing:.5px;text-transform:uppercase;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden}
th:nth-child(1){width:22%}
th:nth-child(2){width:6%}
th:nth-child(3){width:8%}
th:nth-child(4){width:8%}
th:nth-child(5){width:7%}
th:nth-child(6){width:9%}
th:nth-child(7){width:11%}
th:nth-child(8){width:8%}
th:nth-child(9){width:7%}
th:nth-child(10){width:14%}
td{padding:8px 14px;font-size:12px;border-bottom:1px solid var(--border2);vertical-align:middle;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:nth-child(even)>td{background:var(--rowalt)}
tr:hover>td{background:var(--rowhover)}
.mile-row>td{background:rgba(255,180,0,.08)!important;font-weight:600;color:var(--yellow)}
.mile-row:hover>td{background:rgba(255,180,0,.13)!important}
.owner-tag{display:inline-block;padding:2px 8px;border-radius:3px;background:rgba(0,120,200,.2);color:var(--bluelight);font-size:10px;font-weight:700}
.pbar-wrap{background:var(--border);border-radius:2px;height:4px;width:80px;display:inline-block;vertical-align:middle}
.pbar{height:4px;border-radius:2px}
.pct-label{font-size:10px;color:var(--gray);margin-left:5px}
.done-badge{display:inline-block;padding:2px 8px;border-radius:3px;background:rgba(100,220,130,.18);color:var(--green);font-size:11px;font-weight:700}
.btn-done{background:transparent;border:1px solid var(--green);color:var(--green);padding:3px 9px;font-size:11px;border-radius:3px;cursor:pointer;font-family:inherit}
.btn-done:hover{background:rgba(100,220,130,.15)}
.btn-done:disabled{opacity:.4;cursor:default}
.btn-del{background:transparent;border:1px solid var(--red);color:var(--red);padding:3px 9px;font-size:11px;border-radius:3px;cursor:pointer;font-family:inherit;margin-left:4px}
.btn-del:hover{background:rgba(255,110,100,.12)}
.done-row>td{opacity:.6;background:rgba(40,160,80,0.25)!important}
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
      const isDone = t.done === '✓';
      const rowCls = isDone ? 'done-row' : isMile ? 'mile-row' : '';
      return `<tr class="${rowCls}">
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
        <td>${isDone ? '<span class="done-badge">✓ Done</span>' : '─'}</td>
        <td>
          <button class="btn-done" onclick="doneTask('${proj.project.replace(/'/g,"\\'")}',${t.row})" ${isDone?'disabled':''}>✓ Done</button>
          <button class="btn-del" onclick="deleteTask('${proj.project.replace(/'/g,"\\'")}',${t.row})">✕</button>
        </td>
      </tr>`;
    }).join('') || '<tr><td colspan="10" style="text-align:center;color:var(--gray);padding:18px">尚無任務</td></tr>';

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
            <th>Done</th><th>操作</th>
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

async function doneTask(project, row) {
  if (!confirm('確認將此任務標記為 Done？')) return;
  try {
    const res  = await fetch('/api/done_task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ project, row })
    });
    const json = await res.json();
    if (!json.ok) throw new Error(json.error);
    showToast('✅ 已標記為 Done');
    loadData();
  } catch(e) {
    showToast('失敗：' + e.message, false);
  }
}

async function deleteTask(project, row) {
  if (!confirm('確認刪除此任務？')) return;
  try {
    const res  = await fetch('/api/delete_task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ project, row })
    });
    const json = await res.json();
    if (!json.ok) throw new Error(json.error);
    showToast('已刪除');
    loadData();
  } catch(e) {
    showToast('失敗：' + e.message, false);
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


# ── 會議 BOT ──────────────────────────────────────
_meeting_sessions: dict = {}
_project_states:   dict = {}


def _reply_meeting(reply_token, text):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {MEETING_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def _push_to_group(group_id, text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {MEETING_TOKEN}", "Content-Type": "application/json"},
        json={"to": group_id, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def _get_display_name(group_id, user_id):
    try:
        if group_id:
            url = f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
        else:
            url = f"https://api.line.me/v2/bot/profile/{user_id}"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {MEETING_TOKEN}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("displayName", user_id)
    except Exception:
        pass
    return user_id


def _parse_task_msg(body: str) -> dict:
    key_map = {
        "工作名稱": "task_name",
        "工期":     "duration",
        "開始":     "start_date",
        "結束":     "finish_date",
        "前置任務": "predecessors",
        "OWNER":    "resource",
        "資源":     "resource",
        "進度":     "percent",
        "里程碑":   "milestone",
    }
    result = {}
    for line in body.splitlines():
        line = line.strip()
        for zh_key, field in key_map.items():
            if line.startswith(zh_key):
                parts = re.split(r"[：:]", line, 1)
                if len(parts) > 1:
                    result[field] = parts[1].strip()
                break
    return result


def handle_meeting(event):
    try:
        source      = event.get("source", {})
        group_id    = source.get("groupId", "")
        user_id     = source.get("userId", "")
        reply_token = event.get("replyToken", "")
        now         = datetime.now(_TW).strftime("%Y-%m-%d %H:%M:%S")

        if event.get("message", {}).get("type") != "text":
            return
        raw = event["message"]["text"].strip()

        def reply(text):
            _reply_meeting(reply_token, text)

        session_key = group_id if group_id else user_id

        # 使用說明
        if raw in ["使用說明", "說明", "help", "Help", "?", "？"]:
            reply("\n".join([
                "📖 會議BOT 使用說明",
                "─" * 22,
                "【新增任務】",
                "#專案名稱",
                "→ 依提示填寫任務內容送出",
                "",
                "【會議記錄】",
                "開始會議 主題名稱",
                "→ 開始記錄所有對話",
                "結束會議",
                "→ AI自動摘要行動事項",
                "  並寫入 Google Sheet",
                "─" * 22,
                "傳「使用說明」可再次查看",
            ]))
            return

        # 查行動事項（各專案未完成任務）
        if raw == "查行動事項":
            try:
                sh      = _get_project_sheet()
                pending = []
                for ws in sh.worksheets():
                    rows = ws.get_all_values()
                    if len(rows) <= 1:
                        continue
                    for row in rows[1:]:
                        if not any(row):
                            continue
                        done      = row[9] if len(row) > 9 else ""
                        task_name = row[0] if len(row) > 0 else ""
                        owner     = row[5] if len(row) > 5 else ""
                        end_date  = row[3] if len(row) > 3 else ""
                        if done == "✓" or not task_name:
                            continue
                        pending.append((ws.title, task_name, owner, end_date))
                if not pending:
                    reply("目前所有任務都已完成！")
                    return
                lines = [f"📋 未完成任務（{len(pending)} 筆）", ""]
                for proj, task, owner, end in pending[:15]:
                    lines.append(f"▸ [{proj}] {task}")
                    if owner:    lines.append(f"  負責：{owner}")
                    if end_date: lines.append(f"  結束：{end}")
                    lines.append("")
                if len(pending) > 15:
                    lines.append(f"…還有 {len(pending)-15} 筆，請至網頁查看")
                reply("\n".join(lines).strip())
            except Exception as e:
                reply(f"查詢失敗：{e}")
            return

        # 步驟一：#專案名稱 → 回覆表單
        if raw.startswith("#") and "\n" not in raw:
            project_name = raw[1:].strip()
            if not project_name:
                reply("請輸入專案名稱，例如：#SMD")
                return
            _project_states[session_key] = {"project": project_name}
            reply(
                f"請填寫 #{project_name} 的任務內容：\n\n"
                f"工作名稱：\n工期：\n開始：\n結束：\n前置任務：\nOWNER：\n進度：\n里程碑："
            )
            return

        # 步驟二：填完表單送出 → 寫入並確認
        if session_key in _project_states:
            project_name = _project_states.pop(session_key)["project"]
            task = _parse_task_msg(raw)
            if not task.get("task_name"):
                reply("請填寫「工作名稱：」欄位")
                return
            ws_proj         = _ensure_project_tab(project_name)
            _, is_milestone = _write_task_row(ws_proj, task, now)
            label           = "★ 里程碑" if is_milestone else "任務"
            reply(
                f"✅ 已新增{label}\n━━━━━━━━━━━━━━\n"
                f"專案：{project_name}\n工作：{task.get('task_name', '')}\n"
                f"工期：{task.get('duration', '-')}\n開始：{task.get('start_date', '-')}\n"
                f"結束：{task.get('finish_date', '-')}\nOWNER：{task.get('resource', '-')}\n"
                f"進度：{task.get('percent', '0%')}"
            )
            return

        # 開始會議
        m = re.match(r"^開始會議[：:\s]*(.*)$", raw)
        if m:
            topic = m.group(1).strip() or "未命名會議"
            _meeting_sessions[session_key] = {
                "topic": topic, "messages": [], "start_time": now
            }
            reply(f"會議已開始\n主題：{topic}\n\n開始記錄對話，輸入「結束會議」產生會議紀錄")
            return

        # 結束會議
        if raw == "結束會議":
            if session_key not in _meeting_sessions:
                reply("目前沒有進行中的會議")
                return
            session = _meeting_sessions.pop(session_key)
            if not session["messages"]:
                reply("沒有記錄到任何對話，會議紀錄未產生")
                return

            convo  = "\n".join(f"{s}: {t}" for s, t in session["messages"])
            prompt = (
                f"你是專業的工程專案會議記錄AI。\n"
                f"以下是工程團隊的會議對話，主題是「{session['topic']}」：\n\n{convo}\n\n"
                f"請從對話中提取行動事項，每個行動事項產生一筆JSON物件，放在陣列中回傳。\n"
                f"欄位：owner, subject, status(固定Open), description, next_step, due_date(YYYY-MM-DD), priority(High/Medium/Low)\n"
                f"只回傳JSON陣列，不要解釋。"
            )

            try:
                from google import genai as _genai
                from google.genai import types as _gtypes
                gc   = _genai.Client(api_key=GEMINI_API_KEY)
                resp = gc.models.generate_content(
                    model="gemini-2.0-flash-lite",
                    contents=prompt,
                    config=_gtypes.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                items = json.loads(resp.text)
                if not isinstance(items, list):
                    items = [items]
            except Exception as e:
                print(traceback.format_exc(), flush=True)
                reply(f"會議結束，但 AI 摘要失敗：{e}")
                return

            ws       = _get_meeting_sheet().worksheet("會議記錄")
            col_a    = ws.col_values(1)
            next_row = max(len(col_a) + 1, 2)
            rows_data = [
                [item.get("owner",""), item.get("subject", session["topic"]),
                 item.get("status","Open"), item.get("description",""),
                 item.get("next_step",""), item.get("due_date",""), "",
                 item.get("priority","Medium"), now]
                for item in items
            ]
            ws.update(f"A{next_row}:I{next_row + len(rows_data) - 1}", rows_data)

            lines = [f"會議紀錄 - {session['topic']}", f"時間：{session['start_time']}", ""]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.get('subject','')}")
                if item.get("owner"):     lines.append(f"   負責：{item['owner']}")
                if item.get("next_step"): lines.append(f"   下一步：{item['next_step']}")
                if item.get("due_date"):  lines.append(f"   截止：{item['due_date']}")
                lines.append(f"   優先度：{item.get('priority','Medium')}")
                lines.append("")
            lines.append(f"共 {len(items)} 個行動事項，已寫入 Google Sheet")
            reply("\n".join(lines))
            return

        # 記錄進行中的對話
        if session_key in _meeting_sessions:
            speaker = _get_display_name(group_id, user_id)
            _meeting_sessions[session_key]["messages"].append((speaker, raw))

    except Exception as e:
        print(traceback.format_exc(), flush=True)
        try:
            _reply_meeting(event.get("replyToken",""), f"處理失敗：{e}")
        except Exception:
            pass


@app.route("/meeting", methods=["POST"])
def webhook_meeting():
    events = request.json.get("events", [])
    for event in events:
        if event.get("type") != "message":
            continue
        source      = event.get("source", {})
        user_id     = source.get("userId", "")
        group_id    = source.get("groupId", "")
        reply_token = event.get("replyToken", "")
        msg_type    = event["message"].get("type")

        if msg_type == "image":
            handle_invoice_image(user_id, group_id, event["message"]["id"], reply_token)
        elif msg_type == "text":
            handle_meeting(event)
    return "OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False)
