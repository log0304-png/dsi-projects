# -*- coding: utf-8 -*-
"""
發票請款 PWA —— 獨立模組（Blueprint）

設計原則：
  * 只新增、不修改 app.py 既有邏輯；LINE BOT 完全不受影響。
  * 重用 app.py 既有函式（_analyze_invoice / _upload_to_drive / _get_user_tab），
    採「函式內延遲 import」避免 app.py <-> invoice_pwa.py 循環載入。
  * app.py 以 try/except 註冊本 Blueprint，本檔案就算壞掉也不會拖垮 LINE BOT。

需要的環境變數（在 Render → Environment 設定，不要寫進 public repo）：
  INVOICE_PW  登入密碼。沒設定時 PWA 會顯示「尚未設定」而不開放登入。

新增網址（皆在 /invoice/ 之下，與既有路由不重疊）：
  GET  /invoice/                     手機頁面（未登入→登入頁，已登入→拍照頁）
  POST /invoice/login                驗證密碼 + 記住請款人姓名
  GET  /invoice/logout               登出
  POST /invoice/upload               收圖 → Gemini 辨識 → 存 Drive → 寫試算表
  GET  /invoice/manifest.webmanifest PWA 安裝資訊
  GET  /invoice/sw.js                Service Worker
  GET  /invoice/icon-180|192|512.png App icon
"""
import os
import re
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta

from flask import (
    Blueprint, request, redirect, make_response, jsonify, Response, send_file,
)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_TW   = timezone(timedelta(hours=8))
_HERE = Path(__file__).resolve().parent

# ── 設定 ─────────────────────────────────────────────
PASSWORD   = (os.environ.get("INVOICE_PW") or "").strip()   # 未設定 → PWA 不開放登入
COOKIE     = "inv_auth"
MAX_AGE    = 30 * 24 * 3600          # 登入狀態保留 30 天
MAX_UPLOAD = 15 * 1024 * 1024        # 單張上傳上限 15MB
_signer    = URLSafeTimedSerializer("dsi-invoice-pwa/" + PASSWORD, salt="inv-auth")

bp = Blueprint("invoice_pwa", __name__, url_prefix="/invoice")


# ── 登入狀態 ─────────────────────────────────────────
def _current_user():
    tok = request.cookies.get(COOKIE, "")
    if not tok:
        return None
    try:
        data = _signer.loads(tok, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    return (data.get("n") or "").strip() or None


def _set_auth_cookie(resp, name):
    resp.set_cookie(
        COOKIE, _signer.dumps({"n": name}),
        max_age=MAX_AGE,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
    )
    return resp


# ── 核心：辨識並寫入試算表（重用 app.py 既有函式）────────
def _process_invoice(image_bytes: bytes, requester: str) -> dict:
    from app import (
        _analyze_invoice, _upload_to_drive, _get_user_tab,
        EXPENSE_HEADERS, EXPENSE_COLS,
    )

    data = _analyze_invoice(image_bytes)
    if not data:
        return {"ok": False, "error": "辨識失敗，請確認圖片清晰後重試。"}

    now = datetime.now(_TW)
    expense_col = data.get("expense_col", "")
    if expense_col not in EXPENSE_COLS:
        expense_col = EXPENSE_COLS[0]

    safe = re.sub(r"\W", "", requester)[:12] or "web"
    filename = f"invoice_{now:%Y%m%d_%H%M%S}_{safe}.jpg"
    image_formula = _upload_to_drive(image_bytes, filename)

    row = [""] * 17
    row[0]  = data.get("summary", "")
    row[1]  = data.get("items", "")
    row[2]  = data.get("invoice_number", "")
    row[3]  = requester
    row[4]  = data.get("date") or now.strftime("%Y-%m-%d")
    row[EXPENSE_HEADERS.index(expense_col)] = data.get("amount", "")
    row[15] = data.get("notes", "")
    row[16] = image_formula

    ws = _get_user_tab(requester)
    ws.append_row(row, value_input_option="USER_ENTERED")

    return {
        "ok": True,
        "requester": requester,
        "date": row[4],
        "invoice_number": row[2] or "",
        "items": row[1] or "",
        "amount": data.get("amount", ""),
        "expense_col": expense_col,
        "summary": row[0] or "",
        "notes": row[15] or "",
    }


# ── 路由 ─────────────────────────────────────────────
@bp.route("/", methods=["GET"])
def home():
    if not PASSWORD:
        return Response(_SETUP_HTML, mimetype="text/html", status=503)
    user = _current_user()
    if not user:
        err = "密碼或姓名有誤，請再試一次。" if request.args.get("e") == "1" else ""
        return _LOGIN_HTML.replace("__ERR__", err)
    return _APP_HTML.replace("__NAME__", _esc(user))


@bp.route("/login", methods=["POST"])
def login():
    if not PASSWORD:
        return Response(_SETUP_HTML, mimetype="text/html", status=503)
    name = (request.form.get("name") or "").strip()
    pw   = request.form.get("pw") or ""
    if pw != PASSWORD or not name:
        return redirect("/invoice/?e=1")
    return _set_auth_cookie(make_response(redirect("/invoice/")), name[:40])


@bp.route("/logout", methods=["GET"])
def logout():
    resp = make_response(redirect("/invoice/"))
    resp.delete_cookie(COOKIE)
    return resp


@bp.route("/upload", methods=["POST"])
def upload():
    if not PASSWORD:
        return jsonify({"ok": False, "error": "尚未設定密碼。"}), 503
    user = _current_user()
    if not user:
        return jsonify({"ok": False, "error": "登入已逾時，請重新登入。"}), 401

    if request.content_length and request.content_length > MAX_UPLOAD:
        return jsonify({"ok": False, "error": "圖片太大（超過 15MB），請重拍。"}), 413

    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "error": "沒有收到圖片。"}), 400

    img = f.read()
    if len(img) < 1024:
        return jsonify({"ok": False, "error": "圖片內容異常，請重拍。"}), 400
    if len(img) > MAX_UPLOAD:
        return jsonify({"ok": False, "error": "圖片太大（超過 15MB），請重拍。"}), 413

    try:
        result = _process_invoice(img, user)
        return jsonify(result), (200 if result.get("ok") else 502)
    except Exception:
        traceback.print_exc()
        return jsonify({"ok": False, "error": "系統處理失敗，請稍後再試。"}), 500


@bp.route("/manifest.webmanifest", methods=["GET"])
def manifest():
    return Response(_MANIFEST, mimetype="application/manifest+json")


@bp.route("/sw.js", methods=["GET"])
def sw():
    return Response(_SW_JS, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@bp.route("/icon-<int:size>.png", methods=["GET"])
def icon(size):
    if size not in (180, 192, 512):
        return "not found", 404
    path = _HERE / f"icon-{size}.png"
    if not path.exists():
        return "not found", 404
    resp = make_response(send_file(path, mimetype="image/png"))
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# ── 小工具 ───────────────────────────────────────────
def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


# ── 尚未設定密碼時顯示 ───────────────────────────────
_SETUP_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>發票請款 · 尚未設定</title>
<style>body{font-family:-apple-system,"微軟正黑體",Arial,sans-serif;background:#0D1B34;color:#fff;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:28px;line-height:1.8}
.b{max-width:380px;background:#112040;border:1px solid #1A3A60;border-radius:14px;padding:26px;font-size:14px}
code{background:#0F2040;padding:2px 6px;border-radius:4px;color:#64DC82}</style></head>
<body><div class="b">
<b>⚙️ 發票請款 PWA 尚未啟用</b><br><br>
請到 Render 服務的 <b>Environment</b> 新增環境變數：<br><br>
<code>INVOICE_PW</code> = 你的登入密碼<br><br>
存檔後服務會自動重啟，這頁就會變成登入畫面。
</div></body></html>"""


# ── PWA manifest ─────────────────────────────────────
_MANIFEST = """{
  "name": "DSI 發票請款",
  "short_name": "發票請款",
  "description": "拍發票自動辨識並寫入請款表",
  "start_url": "/invoice/",
  "scope": "/invoice/",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0D1B34",
  "theme_color": "#0D1B34",
  "lang": "zh-Hant-TW",
  "icons": [
    {"src": "/invoice/icon-192.png", "sizes": "192x192", "type": "image/png"},
    {"src": "/invoice/icon-512.png", "sizes": "512x512", "type": "image/png"}
  ]
}"""


# ── Service Worker ───────────────────────────────────
_SW_JS = """
const CACHE = 'inv-pwa-v1';
const SHELL = ['/invoice/', '/invoice/manifest.webmanifest',
               '/invoice/icon-192.png', '/invoice/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                 // 上傳一律走網路
  const url = new URL(req.url);
  if (!url.pathname.startsWith('/invoice/')) return;

  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/invoice/')));
    return;
  }
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
      return res;
    }).catch(() => hit))
  );
});
"""


# ── 登入頁 ───────────────────────────────────────────
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DSI 發票請款 · 登入</title>
<link rel="manifest" href="/invoice/manifest.webmanifest">
<meta name="theme-color" content="#0D1B34">
<link rel="apple-touch-icon" href="/invoice/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="發票請款">
<style>
  :root{--bg:#0D1B34;--card:#112040;--panel:#0F2040;--blue:#0078C8;--bluedark:#005AA0;
        --white:#fff;--gray:#8CB4D2;--border:#1A3A60;--red:#FF6E64}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Microsoft JhengHei","微軟正黑體",-apple-system,Arial,sans-serif;
       background:var(--bg);color:var(--white);min-height:100vh;min-height:100dvh;
       display:flex;align-items:center;justify-content:center;padding:24px}
  .box{width:100%;max-width:360px;background:var(--card);border:1px solid var(--border);
       border-radius:14px;padding:28px 22px}
  .logo{width:56px;height:56px;display:block;margin:0 auto 14px;border-radius:12px}
  h1{font-size:17px;text-align:center;margin-bottom:4px}
  .sub{font-size:12px;color:var(--gray);text-align:center;margin-bottom:22px}
  label{display:block;font-size:12px;color:var(--gray);margin:14px 0 6px}
  input{width:100%;padding:12px 14px;font-size:16px;font-family:inherit;color:var(--white);
        background:var(--panel);border:1px solid var(--border);border-radius:8px}
  input:focus{outline:none;border-color:var(--blue)}
  button{width:100%;margin-top:22px;padding:13px;font-size:15px;font-weight:700;font-family:inherit;
         color:#fff;background:var(--blue);border:none;border-radius:8px;cursor:pointer}
  button:active{background:var(--bluedark)}
  .err{margin-top:14px;font-size:13px;color:var(--red);text-align:center;min-height:18px}
</style>
</head>
<body>
  <form class="box" method="POST" action="/invoice/login">
    <img class="logo" src="/invoice/icon-192.png" alt="">
    <h1>DSI 發票請款</h1>
    <div class="sub">拍發票，自動寫進請款表</div>
    <label for="name">你的名字（請款人）</label>
    <input id="name" name="name" autocomplete="name" required placeholder="例：王小明">
    <label for="pw">密碼</label>
    <input id="pw" name="pw" type="password" autocomplete="current-password" required>
    <button type="submit">登入</button>
    <div class="err">__ERR__</div>
  </form>
  <script>
    if ('serviceWorker' in navigator)
      navigator.serviceWorker.register('/invoice/sw.js').catch(function(){});
  </script>
</body>
</html>"""


# ── 拍照頁 ───────────────────────────────────────────
_APP_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>DSI 發票請款</title>
<link rel="manifest" href="/invoice/manifest.webmanifest">
<meta name="theme-color" content="#0D1B34">
<link rel="apple-touch-icon" href="/invoice/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="發票請款">
<style>
  :root{--bg:#0D1B34;--hdr:#07102A;--card:#112040;--panel:#0F2040;--blue:#0078C8;
        --bluedark:#005AA0;--white:#fff;--gray:#8CB4D2;--lightgray:#B0C8DC;
        --border:#1A3A60;--green:#64DC82;--greenbg:rgba(100,220,130,.12);
        --red:#FF6E64;--redbg:rgba(255,110,100,.12);--yellow:#FFB400}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Microsoft JhengHei","微軟正黑體",-apple-system,Arial,sans-serif;
       background:var(--bg);color:var(--white);min-height:100vh;min-height:100dvh;font-size:14px}
  .hdr{background:var(--hdr);border-bottom:1px solid var(--border);
       padding:calc(env(safe-area-inset-top) + 12px) 16px 12px;
       display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:5}
  .hdr img{width:26px;height:26px;border-radius:6px}
  .hdr h1{font-size:15px;font-weight:700;flex:1}
  .hdr .who{font-size:11px;color:var(--gray)}
  .hdr a{font-size:11px;color:var(--gray);text-decoration:none;border:1px solid var(--border);
         padding:4px 8px;border-radius:6px}
  .wrap{padding:18px 16px 40px;max-width:520px;margin:0 auto}

  .drop{border:2px dashed var(--border);border-radius:14px;background:var(--card);
        padding:40px 20px;text-align:center;cursor:pointer;display:block}
  .drop:active{border-color:var(--blue)}
  .drop .ico{font-size:44px;line-height:1}
  .drop .t{margin-top:12px;font-size:15px;font-weight:700}
  .drop .s{margin-top:4px;font-size:12px;color:var(--gray)}
  .drop input{display:none}

  .preview{display:none;background:var(--card);border:1px solid var(--border);
           border-radius:14px;overflow:hidden}
  .preview img{width:100%;display:block;max-height:60vh;object-fit:contain;background:#000}
  .bar{display:flex;gap:10px;padding:12px}
  .btn{flex:1;padding:13px;font-size:15px;font-weight:700;font-family:inherit;border:none;
       border-radius:9px;cursor:pointer}
  .btn-go{background:var(--blue);color:#fff}
  .btn-go:active{background:var(--bluedark)}
  .btn-re{background:var(--panel);color:var(--lightgray);border:1px solid var(--border)}
  .btn:disabled{opacity:.5}

  .load{display:none;text-align:center;padding:36px 0;color:var(--gray)}
  .spin{width:34px;height:34px;border:3px solid var(--border);border-top-color:var(--blue);
        border-radius:50%;margin:0 auto 14px;animation:sp 1s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  .result{display:none;border-radius:14px;padding:18px;margin-top:4px}
  .result.ok{background:var(--greenbg);border:1px solid rgba(100,220,130,.4)}
  .result.bad{background:var(--redbg);border:1px solid rgba(255,110,100,.4)}
  .result h2{font-size:16px;margin-bottom:12px}
  .result.ok h2{color:var(--green)}
  .result.bad h2{color:var(--red)}
  .kv{display:grid;grid-template-columns:76px 1fr;gap:8px 12px;font-size:14px}
  .kv .k{color:var(--gray)}
  .kv .v{color:var(--white);word-break:break-all}
  .amt{color:var(--yellow);font-weight:700}
  .again{margin-top:16px;width:100%;padding:13px;font-size:15px;font-weight:700;font-family:inherit;
         background:var(--blue);color:#fff;border:none;border-radius:9px;cursor:pointer}
  .hint{margin-top:18px;font-size:11px;color:var(--gray);line-height:1.7;text-align:center}
</style>
</head>
<body>
  <div class="hdr">
    <img src="/invoice/icon-192.png" alt="">
    <h1>發票請款</h1>
    <span class="who">__NAME__</span>
    <a href="/invoice/logout">登出</a>
  </div>

  <div class="wrap">
    <label class="drop" id="drop">
      <div class="ico">📷</div>
      <div class="t">拍發票 / 選照片</div>
      <div class="s">紙本、電子發票、收據都可以</div>
      <input type="file" id="file" accept="image/*" capture="environment">
    </label>

    <div class="preview" id="preview">
      <img id="pimg" alt="">
      <div class="bar">
        <button class="btn btn-re" id="btnRe">重拍</button>
        <button class="btn btn-go" id="btnGo">送出辨識</button>
      </div>
    </div>

    <div class="load" id="load">
      <div class="spin"></div>
      辨識中，請稍候…
    </div>

    <div class="result" id="result"></div>

    <div class="hint">
      辨識結果會寫進「__NAME__」的請款分頁，發票原圖會存到雲端硬碟。<br>
      金額或分類抓錯時，直接到 Google 試算表手動修正即可。
    </div>
  </div>

<script>
if ('serviceWorker' in navigator)
  navigator.serviceWorker.register('/invoice/sw.js').catch(function(){});

var fileEl = document.getElementById('file');
var drop   = document.getElementById('drop');
var prev   = document.getElementById('preview');
var pimg   = document.getElementById('pimg');
var load   = document.getElementById('load');
var result = document.getElementById('result');
var btnGo  = document.getElementById('btnGo');
var btnRe  = document.getElementById('btnRe');
var blob   = null;

fileEl.addEventListener('change', function(){
  var f = fileEl.files[0];
  if (!f) return;
  result.style.display = 'none';
  shrink(f).then(function(b){
    blob = b;
    pimg.src = URL.createObjectURL(b);
    drop.style.display = 'none';
    prev.style.display = 'block';
    btnGo.disabled = false;
  });
});

btnRe.addEventListener('click', reset);

btnGo.addEventListener('click', function(){
  if (!blob) return;
  prev.style.display = 'none';
  load.style.display = 'block';
  btnGo.disabled = true;

  var fd = new FormData();
  fd.append('photo', blob, 'invoice.jpg');

  fetch('/invoice/upload', {method:'POST', body:fd})
    .then(function(r){ return r.json().then(function(j){ return {s:r.status, j:j}; }); })
    .then(function(o){
      load.style.display = 'none';
      if (o.s === 401) { location.href = '/invoice/'; return; }
      if (o.j && o.j.ok) showOk(o.j); else showBad((o.j && o.j.error) || '辨識失敗，請重試。');
    })
    .catch(function(){
      load.style.display = 'none';
      showBad('連線失敗，請確認網路後重試。');
    });
});

function showOk(d){
  result.className = 'result ok';
  result.innerHTML =
    '<h2>✅ 已記錄</h2>' +
    '<div class="kv">' +
      row('請款人', d.requester) +
      row('日期', d.date) +
      row('發票號', d.invoice_number || '—') +
      row('品項', d.items || '—') +
      '<div class="k">金額</div><div class="v amt">NT$ ' + (d.amount || '—') + '</div>' +
      row('類別', d.expense_col) +
      (d.summary ? row('摘要', d.summary) : '') +
    '</div>' +
    '<button class="again" onclick="reset()">再傳一張</button>';
  result.style.display = 'block';
}

function showBad(msg){
  result.className = 'result bad';
  result.innerHTML = '<h2>⚠️ 沒有寫入</h2><div class="kv"><div class="k">原因</div>' +
                     '<div class="v">' + esc(msg) + '</div></div>' +
                     '<button class="again" onclick="reset()">重拍一張</button>';
  result.style.display = 'block';
}

function reset(){
  blob = null;
  fileEl.value = '';
  prev.style.display = 'none';
  load.style.display = 'none';
  result.style.display = 'none';
  drop.style.display = 'block';
  btnGo.disabled = false;
}

function row(k,v){ return '<div class="k">' + k + '</div><div class="v">' + esc(v||'') + '</div>'; }
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

// 上傳前把圖縮到最長邊 1600px、JPEG 0.82，省流量也避免逾時
function shrink(file){
  return new Promise(function(resolve){
    var img = new Image();
    img.onload = function(){
      var max = 1600, w = img.width, h = img.height;
      if (w > max || h > max){
        if (w >= h){ h = Math.round(h * max / w); w = max; }
        else { w = Math.round(w * max / h); h = max; }
      }
      var cv = document.createElement('canvas');
      cv.width = w; cv.height = h;
      cv.getContext('2d').drawImage(img, 0, 0, w, h);
      cv.toBlob(function(b){ resolve(b || file); }, 'image/jpeg', 0.82);
    };
    img.onerror = function(){ resolve(file); };
    img.src = URL.createObjectURL(file);
  });
}
</script>
</body>
</html>"""
