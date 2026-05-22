#!/usr/bin/env python3
import argparse
import io
import json
import mimetypes
import os
import posixpath
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import zipfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "ftp_data"
DOWNLOAD_DIR = DATA_DIR / "download"
UPLOAD_DIR = DATA_DIR / "upload"
CONFIG_FILE = DATA_DIR / "config.json"
META_FILE = DATA_DIR / "metadata.json"

DEFAULT_CONFIG = {
    "admin_password": "admin123",
    "file_size_limit_mb": 5,
}

STATE_LOCK = threading.RLock()
ADMIN_SESSIONS = set()


def ensure_data_dirs():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    if not META_FILE.exists():
        META_FILE.write_text("{}", encoding="utf-8")


def load_config():
    ensure_data_dirs()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    cfg = DEFAULT_CONFIG.copy()
    cfg.update({k: v for k, v in data.items() if k in cfg})
    try:
        cfg["file_size_limit_mb"] = max(1, int(cfg["file_size_limit_mb"]))
    except (TypeError, ValueError):
        cfg["file_size_limit_mb"] = DEFAULT_CONFIG["file_size_limit_mb"]
    return cfg


def load_meta():
    ensure_data_dirs()
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_meta(meta):
    tmp = META_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_FILE)


def now_text(ts=None):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or time.time()))


def file_size(path):
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def fmt_disposition(filename):
    quoted = urllib.parse.quote(filename.encode("utf-8"))
    return f"attachment; filename*=UTF-8''{quoted}"


def clean_rel_path(raw, allow_nested):
    raw = (raw or "").replace("\\", "/").strip()
    raw = raw.lstrip("/")
    raw = posixpath.normpath(raw)
    if raw in ("", ".") or raw.startswith("../") or raw == "..":
        raise ValueError("非法路径")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not allow_nested and len(parts) != 1:
        raw = parts[-1]
    for part in parts:
        if part in ("", ".", "..") or "\x00" in part:
            raise ValueError("非法路径")
    return "/".join(parts)


def unique_path(base, rel):
    target = base / rel
    if not target.exists():
        return target, rel
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    for idx in range(1, 10000):
        candidate = parent / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            cand_rel = candidate.relative_to(base).as_posix()
            return candidate, cand_rel
    raise RuntimeError("无法生成不冲突的文件名")


def meta_key(area, rel):
    return f"{area}:{rel}"


def remove_meta_for_path(meta, area, rel):
    prefix = meta_key(area, rel.rstrip("/") + "/")
    exact = meta_key(area, rel)
    for key in list(meta):
        if key == exact or key.startswith(prefix):
            del meta[key]


def is_owned_by(meta, client_id, area, rel, target):
    if not client_id:
        return False
    if target.is_file():
        return meta.get(meta_key(area, rel), {}).get("owner") == client_id
    prefix = rel.rstrip("/") + "/"
    owned = False
    for key, value in meta.items():
        if key.startswith(meta_key(area, prefix)):
            owned = True
            if value.get("owner") != client_id:
                return False
    return owned


def owner_matches(meta, client_id, area, rel, target):
    if not client_id:
        return False
    if target.is_file():
        return meta.get(meta_key(area, rel), {}).get("owner") == client_id
    prefix = meta_key(area, rel.rstrip("/") + "/")
    seen = False
    for key, value in meta.items():
        if key.startswith(prefix):
            seen = True
            if value.get("owner") != client_id:
                return False
    return seen


def list_area(area, client_id, is_admin):
    base = DOWNLOAD_DIR if area == "download" else UPLOAD_DIR
    meta = load_meta()
    items = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if area == "download" and not child.is_file():
            continue
        rel = child.name
        info = meta.get(meta_key(area, rel), {})
        uploaded_at = info.get("uploaded_at")
        if not uploaded_at:
            uploaded_at = now_text(child.stat().st_mtime)
        owner = owner_matches(meta, client_id, area, rel, child)
        can_delete = is_admin or (area == "upload" and is_owned_by(meta, client_id, area, rel, child))
        items.append({
            "name": child.name,
            "path": rel,
            "kind": "folder" if child.is_dir() else "file",
            "size": file_size(child),
            "uploaded_at": uploaded_at,
            "owner": owner,
            "can_delete": can_delete,
        })
    return items


def parse_multipart(handler):
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0") or 0)
    body = handler.rfile.read(length)
    pseudo = (
        f"Content-Type: {content_type}\r\n"
        f"MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8") + body
    msg = BytesParser(policy=email_policy).parsebytes(pseudo)
    files = []
    for part in msg.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        files.append((filename, payload))
    return files


def html_page():
    return HTML


class FileServiceHandler(BaseHTTPRequestHandler):
    server_version = "OIFileService/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s - %s" % (now_text(), self.address_string(), fmt % args))

    def get_cookies(self):
        jar = cookies.SimpleCookie()
        header = self.headers.get("Cookie")
        if header:
            jar.load(header)
        return jar

    def identity(self):
        jar = self.get_cookies()
        client_id = jar.get("client_id")
        session = jar.get("admin_session")
        new_client = None
        if client_id:
            cid = client_id.value
        else:
            cid = secrets.token_urlsafe(18)
            new_client = cid
        admin = bool(session and session.value in ADMIN_SESSIONS)
        return cid, admin, new_client

    def send_headers(self, status=200, content_type="application/json; charset=utf-8", extra=None, client_id=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        if client_id:
            self.send_header("Set-Cookie", f"client_id={client_id}; Path=/; SameSite=Lax; Max-Age=31536000")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def json(self, payload, status=200, new_client=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_headers(status, extra={"Content-Length": str(len(body))}, client_id=new_client)
        self.wfile.write(body)

    def error_json(self, message, status=400, new_client=None):
        self.json({"ok": False, "error": message}, status=status, new_client=new_client)

    def do_GET(self):
        ensure_data_dirs()
        parsed = urllib.parse.urlparse(self.path)
        client_id, is_admin, new_client = self.identity()
        if parsed.path == "/":
            body = html_page().encode("utf-8")
            self.send_headers(200, "text/html; charset=utf-8", {"Content-Length": str(len(body))}, new_client)
            self.wfile.write(body)
            return
        if parsed.path == "/api/state":
            cfg = load_config()
            with STATE_LOCK:
                payload = {
                    "ok": True,
                    "is_admin": is_admin,
                    "file_size_limit_mb": cfg["file_size_limit_mb"],
                    "download": list_area("download", client_id, is_admin),
                    "upload": list_area("upload", client_id, is_admin),
                }
            self.json(payload, new_client=new_client)
            return
        if parsed.path == "/api/download":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                rel = clean_rel_path(qs.get("path", [""])[0], allow_nested=False)
            except ValueError as exc:
                self.error_json(str(exc), 400, new_client)
                return
            target = DOWNLOAD_DIR / rel
            if not target.is_file():
                self.error_json("文件不存在", 404, new_client)
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            extra = {
                "Content-Length": str(target.stat().st_size),
                "Content-Disposition": fmt_disposition(target.name),
            }
            self.send_headers(200, ctype, extra, new_client)
            with target.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        if parsed.path == "/api/download-uploads":
            if not is_admin:
                self.error_json("需要管理员权限", 403, new_client)
                return
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            tmp_path = Path(tmp.name)
            tmp.close()
            try:
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for item in UPLOAD_DIR.rglob("*"):
                        if item.is_file():
                            zf.write(item, item.relative_to(UPLOAD_DIR).as_posix())
                filename = f"submissions_{time.strftime('%Y%m%d_%H%M%S')}.zip"
                extra = {
                    "Content-Length": str(tmp_path.stat().st_size),
                    "Content-Disposition": fmt_disposition(filename),
                }
                self.send_headers(200, "application/zip", extra, new_client)
                with tmp_path.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            finally:
                tmp_path.unlink(missing_ok=True)
            return
        self.error_json("接口不存在", 404, new_client)

    def do_POST(self):
        ensure_data_dirs()
        parsed = urllib.parse.urlparse(self.path)
        client_id, is_admin, new_client = self.identity()
        if parsed.path == "/api/login":
            length = int(self.headers.get("Content-Length", "0") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if data.get("password") != load_config()["admin_password"]:
                self.error_json("管理员密码错误", 403, new_client)
                return
            token = secrets.token_urlsafe(24)
            ADMIN_SESSIONS.add(token)
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"admin_session={token}; Path=/; SameSite=Lax; HttpOnly")
            if new_client:
                self.send_header("Set-Cookie", f"client_id={new_client}; Path=/; SameSite=Lax; Max-Age=31536000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/logout":
            jar = self.get_cookies()
            session = jar.get("admin_session")
            if session:
                ADMIN_SESSIONS.discard(session.value)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", "admin_session=; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if parsed.path == "/api/delete":
            length = int(self.headers.get("Content-Length", "0") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            area = data.get("area")
            if area not in ("download", "upload"):
                self.error_json("区域不存在", 400, new_client)
                return
            try:
                rel = clean_rel_path(data.get("path", ""), allow_nested=False)
            except ValueError as exc:
                self.error_json(str(exc), 400, new_client)
                return
            base = DOWNLOAD_DIR if area == "download" else UPLOAD_DIR
            target = base / rel
            if not target.exists():
                self.error_json("文件不存在", 404, new_client)
                return
            with STATE_LOCK:
                meta = load_meta()
                if not (is_admin or (area == "upload" and is_owned_by(meta, client_id, area, rel, target))):
                    self.error_json("没有删除权限", 403, new_client)
                    return
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                remove_meta_for_path(meta, area, rel)
                save_meta(meta)
            self.json({"ok": True}, new_client=new_client)
            return
        if parsed.path == "/api/upload":
            qs = urllib.parse.parse_qs(parsed.query)
            area = qs.get("area", [""])[0]
            if area not in ("download", "upload"):
                self.error_json("区域不存在", 400, new_client)
                return
            if area == "download" and not is_admin:
                self.error_json("需要管理员权限", 403, new_client)
                return
            cfg = load_config()
            limit = cfg["file_size_limit_mb"] * 1024 * 1024
            content_len = int(self.headers.get("Content-Length", "0") or 0)
            if content_len > limit + 2 * 1024 * 1024:
                self.error_json(f"上传内容超过 {cfg['file_size_limit_mb']} MB 限制", 413, new_client)
                return
            try:
                files = parse_multipart(self)
            except Exception:
                self.error_json("无法解析上传内容", 400, new_client)
                return
            total = sum(len(payload) for _, payload in files)
            if not files:
                self.error_json("没有收到文件", 400, new_client)
                return
            if total > limit:
                self.error_json(f"上传内容超过 {cfg['file_size_limit_mb']} MB 限制", 413, new_client)
                return
            base = DOWNLOAD_DIR if area == "download" else UPLOAD_DIR
            saved = []
            with STATE_LOCK:
                meta = load_meta()
                for filename, payload in files:
                    rel = clean_rel_path(filename, allow_nested=(area == "upload"))
                    target, final_rel = unique_path(base, rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("wb") as f:
                        f.write(payload)
                    uploaded_at = now_text()
                    meta[meta_key(area, final_rel)] = {
                        "owner": client_id,
                        "uploaded_at": uploaded_at,
                        "size": len(payload),
                    }
                    saved.append(final_rel)
                save_meta(meta)
            self.json({"ok": True, "saved": saved}, new_client=new_client)
            return
        self.error_json("接口不存在", 404, new_client)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>文件收发</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7f5;
      --ink: #17211b;
      --muted: #65736b;
      --line: #dbe4de;
      --panel: #ffffff;
      --teal: #007f73;
      --teal-soft: #e2f5f1;
      --amber: #b56a00;
      --red: #c7362f;
      --shadow: 0 18px 45px rgba(23, 33, 27, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: linear-gradient(180deg, #eef7f1 0%, var(--bg) 38%, #f8faf8 100%);
      color: var(--ink);
    }
    button, input { font: inherit; }
    .app {
      max-width: 1360px;
      margin: 0 auto;
      padding: 22px 18px 28px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
      margin-bottom: 16px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: clamp(24px, 4vw, 38px);
      letter-spacing: 0;
    }
    .sub {
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .gear {
      width: 44px;
      height: 44px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      cursor: pointer;
      box-shadow: var(--shadow);
      font-size: 22px;
    }
    .gear.admin { color: #fff; background: var(--teal); border-color: var(--teal); }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 20px;
    }
    .panel {
      background: rgba(255, 255, 255, .92);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
      min-height: calc(100vh - 150px);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .panel.dragover {
      border-color: var(--teal);
      box-shadow: 0 0 0 4px var(--teal-soft), var(--shadow);
    }
    .panel-head {
      padding: 16px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      border-bottom: 1px solid var(--line);
    }
    h2 {
      margin: 0 0 4px;
      font-size: 19px;
      letter-spacing: 0;
    }
    .hint {
      color: var(--muted);
      font-size: 13px;
    }
    .tools { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .btn {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      min-height: 34px;
      padding: 0 12px;
      border-radius: 9px;
      cursor: pointer;
      transition: .16s ease;
      white-space: nowrap;
    }
    .btn.primary { background: var(--teal); border-color: var(--teal); color: #fff; }
    .btn.warn { color: var(--amber); }
    .btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 8px 18px rgba(23, 33, 27, .1); }
    .btn:disabled {
      opacity: .38;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    .list {
      flex: 1;
      overflow: auto;
      padding: 10px;
    }
    .row {
      width: 100%;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto 42px;
      align-items: center;
      gap: 10px;
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 11px 10px;
      min-height: 54px;
      transition: .14s ease;
    }
    .row.downloadable { cursor: pointer; }
    .row:hover {
      background: #f1f8f5;
      border-color: #d2e7de;
    }
    .name {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 9px;
      font-weight: 650;
    }
    .name-text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .del {
      height: 30px;
      border-radius: 8px;
      border: 1px solid #f0c7c4;
      color: var(--red);
      background: #fff8f7;
      cursor: pointer;
    }
    .del:disabled {
      opacity: .3;
      cursor: not-allowed;
      filter: grayscale(1);
    }
    .empty {
      color: var(--muted);
      height: 100%;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 35px;
      line-height: 1.7;
    }
    .status {
      min-height: 28px;
      margin-top: 14px;
      color: var(--muted);
      font-size: 14px;
    }
    .modal {
      position: fixed;
      inset: 0;
      background: rgba(13, 20, 17, .34);
      display: none;
      place-items: center;
      padding: 20px;
    }
    .modal.show { display: grid; }
    .dialog {
      width: min(390px, 100%);
      background: #fff;
      border-radius: 14px;
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      padding: 18px;
    }
    .dialog h3 { margin: 0 0 12px; font-size: 20px; }
    .dialog input {
      width: 100%;
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 0 11px;
      margin-bottom: 12px;
    }
    .dialog-actions { display: flex; gap: 8px; justify-content: flex-end; }
    @media (max-width: 820px) {
      .app { padding: 16px 10px 22px; }
      .grid { grid-template-columns: 1fr; }
      .panel { min-height: 460px; }
      .row { grid-template-columns: minmax(0, 1fr) 42px; }
      .row .meta { display: none; }
      header { align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div>
        <h1>文件收发</h1>
        <p class="sub" id="serverHint">连接同一局域网后，在浏览器打开本机 IP 和端口即可使用。</p>
      </div>
      <button class="gear" id="adminBtn" title="管理员模式">⚙</button>
    </header>

    <main class="grid">
      <section class="panel" id="downloadPanel">
        <div class="panel-head">
          <div>
            <h2>文件下载</h2>
            <div class="hint">管理员上传文件，其他用户点击文件名下载</div>
          </div>
          <div class="tools">
            <button class="btn primary" id="downloadUploadBtn">上传</button>
          </div>
        </div>
        <div class="list" id="downloadList"></div>
      </section>

      <section class="panel" id="uploadPanel">
        <div class="panel-head">
          <div>
            <h2>文件上传</h2>
            <div class="hint">可拖入文件或文件夹，保留目录结构</div>
          </div>
          <div class="tools">
            <button class="btn" id="uploadFileBtn">文件</button>
            <button class="btn primary" id="uploadFolderBtn">文件夹</button>
            <button class="btn warn" id="downloadAllBtn">下载</button>
          </div>
        </div>
        <div class="list" id="uploadList"></div>
      </section>
    </main>
    <div class="status" id="status"></div>
  </div>

  <input id="downloadInput" type="file" hidden>
  <input id="uploadFileInput" type="file" multiple hidden>
  <input id="uploadFolderInput" type="file" webkitdirectory directory multiple hidden>

  <div class="modal" id="modal">
    <div class="dialog">
      <h3 id="modalTitle">管理员登录</h3>
      <input id="passwordInput" type="password" placeholder="输入管理员密码" autocomplete="current-password">
      <div class="dialog-actions">
        <button class="btn" id="cancelLogin">取消</button>
        <button class="btn primary" id="confirmLogin">登录</button>
      </div>
    </div>
  </div>

  <script>
    const state = { isAdmin: false, limitMB: 5, download: [], upload: [] };
    const $ = (id) => document.getElementById(id);
    const statusEl = $("status");

    function setStatus(text, error = false) {
      statusEl.textContent = text || "";
      statusEl.style.color = error ? "#c7362f" : "#65736b";
    }
    function formatSize(bytes) {
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / 1024 / 1024).toFixed(2) + " MB";
    }
    function icon(kind) { return kind === "folder" ? "📁" : "📄"; }
    async function api(url, options) {
      const res = await fetch(url, options);
      const type = res.headers.get("Content-Type") || "";
      if (type.includes("application/json")) {
        const data = await res.json();
        if (!res.ok || data.ok === false) throw new Error(data.error || "操作失败");
        return data;
      }
      if (!res.ok) throw new Error("操作失败");
      return res;
    }
    async function refresh() {
      const data = await api("/api/state");
      state.isAdmin = data.is_admin;
      state.limitMB = data.file_size_limit_mb;
      state.download = data.download;
      state.upload = data.upload;
      render();
    }
    function renderList(area, items) {
      const list = $(area + "List");
      list.innerHTML = "";
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = area === "download" ? "暂无可下载文件" : "暂无上传内容，拖入文件或文件夹即可上传";
        list.appendChild(empty);
        return;
      }
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "row" + (area === "download" ? " downloadable" : "");
        row.title = area === "download" ? "点击下载" : item.name;
        row.innerHTML = `
          <div class="name"><span>${icon(item.kind)}</span><span class="name-text"></span></div>
          <div class="meta">${item.uploaded_at}</div>
          <div class="meta">${formatSize(item.size)}</div>
          <button class="del" title="删除">del</button>
        `;
        row.querySelector(".name-text").textContent = item.name;
        if (area === "download") {
          row.addEventListener("click", (ev) => {
            if (ev.target.closest("button")) return;
            window.location.href = "/api/download?path=" + encodeURIComponent(item.path);
          });
        }
        const del = row.querySelector(".del");
        del.disabled = !item.can_delete;
        del.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          if (!item.can_delete) return;
          if (!confirm("确定删除 " + item.name + " 吗？")) return;
          try {
            await api("/api/delete", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ area, path: item.path })
            });
            setStatus("已删除：" + item.name);
            await refresh();
          } catch (err) {
            setStatus(err.message, true);
          }
        });
        list.appendChild(row);
      }
    }
    function render() {
      $("adminBtn").classList.toggle("admin", state.isAdmin);
      $("adminBtn").title = state.isAdmin ? "已进入管理员模式，点击退出" : "管理员模式";
      $("downloadUploadBtn").disabled = !state.isAdmin;
      $("downloadAllBtn").disabled = !state.isAdmin;
      renderList("download", state.download);
      renderList("upload", state.upload);
    }
    function totalSize(files) {
      return files.reduce((sum, f) => sum + f.size, 0);
    }
    function appendFiles(form, files) {
      for (const file of files) {
        const rel = file.relativePath || file.webkitRelativePath || file.name;
        form.append("files", file, rel);
      }
    }
    async function uploadFiles(area, files) {
      if (!files.length) return;
      const total = totalSize(files);
      if (total > state.limitMB * 1024 * 1024) {
        setStatus(`上传内容超过 ${state.limitMB} MB 限制`, true);
        return;
      }
      const form = new FormData();
      appendFiles(form, files);
      setStatus("正在上传 " + files.length + " 个文件...");
      try {
        await api("/api/upload?area=" + area, { method: "POST", body: form });
        setStatus("上传完成");
        await refresh();
      } catch (err) {
        setStatus(err.message, true);
      }
    }
    async function readEntry(entry, prefix = "") {
      if (entry.isFile) {
        return new Promise((resolve, reject) => {
          entry.file((file) => {
            file.relativePath = prefix + file.name;
            resolve([file]);
          }, reject);
        });
      }
      if (entry.isDirectory) {
        const reader = entry.createReader();
        const all = [];
        async function readBatch() {
          const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!entries.length) return;
          for (const child of entries) {
            all.push(...await readEntry(child, prefix + entry.name + "/"));
          }
          await readBatch();
        }
        await readBatch();
        return all;
      }
      return [];
    }
    async function filesFromDrop(ev) {
      const items = [...ev.dataTransfer.items || []];
      if (items.length && items[0].webkitGetAsEntry) {
        const files = [];
        for (const item of items) {
          const entry = item.webkitGetAsEntry();
          if (entry) files.push(...await readEntry(entry));
        }
        return files;
      }
      return [...ev.dataTransfer.files || []];
    }
    $("downloadUploadBtn").addEventListener("click", () => {
      if (state.isAdmin) $("downloadInput").click();
    });
    $("downloadInput").addEventListener("change", (ev) => uploadFiles("download", [...ev.target.files]).then(() => ev.target.value = ""));
    $("uploadFileBtn").addEventListener("click", () => $("uploadFileInput").click());
    $("uploadFolderBtn").addEventListener("click", () => $("uploadFolderInput").click());
    $("uploadFileInput").addEventListener("change", (ev) => uploadFiles("upload", [...ev.target.files]).then(() => ev.target.value = ""));
    $("uploadFolderInput").addEventListener("change", (ev) => uploadFiles("upload", [...ev.target.files]).then(() => ev.target.value = ""));
    $("downloadAllBtn").addEventListener("click", () => {
      if (state.isAdmin) window.location.href = "/api/download-uploads";
    });
    $("adminBtn").addEventListener("click", async () => {
      if (state.isAdmin) {
        await api("/api/logout", { method: "POST" });
        setStatus("已退出管理员模式");
        await refresh();
        return;
      }
      $("modal").classList.add("show");
      $("passwordInput").focus();
    });
    $("cancelLogin").addEventListener("click", () => $("modal").classList.remove("show"));
    $("confirmLogin").addEventListener("click", async () => {
      try {
        await api("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: $("passwordInput").value })
        });
        $("passwordInput").value = "";
        $("modal").classList.remove("show");
        setStatus("已进入管理员模式");
        await refresh();
      } catch (err) {
        setStatus(err.message, true);
      }
    });
    $("passwordInput").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") $("confirmLogin").click();
      if (ev.key === "Escape") $("modal").classList.remove("show");
    });
    const uploadPanel = $("uploadPanel");
    ["dragenter", "dragover"].forEach(name => uploadPanel.addEventListener(name, (ev) => {
      ev.preventDefault();
      uploadPanel.classList.add("dragover");
    }));
    ["dragleave", "drop"].forEach(name => uploadPanel.addEventListener(name, (ev) => {
      ev.preventDefault();
      if (name === "drop") return;
      uploadPanel.classList.remove("dragover");
    }));
    uploadPanel.addEventListener("drop", async (ev) => {
      uploadPanel.classList.remove("dragover");
      try {
        const files = await filesFromDrop(ev);
        await uploadFiles("upload", files);
      } catch (err) {
        setStatus("浏览器未能读取拖入的文件夹", true);
      }
    });
    refresh().catch(err => setStatus(err.message, true));
  </script>
</body>
</html>
"""


def find_lan_ips():
    import socket
    ips = []
    hostname = socket.gethostname()
    try:
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = item[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except socket.gaierror:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip not in ips:
            ips.append(ip)
        sock.close()
    except OSError:
        pass
    return ips


def main():
    parser = argparse.ArgumentParser(description="轻量文件收发 Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", default=8080, type=int, help="监听端口，默认 8080")
    args = parser.parse_args()
    ensure_data_dirs()
    server = ThreadingHTTPServer((args.host, args.port), FileServiceHandler)
    print("文件收发服务已启动")
    print(f"本机访问：http://127.0.0.1:{args.port}")
    for ip in find_lan_ips():
        print(f"局域网访问：http://{ip}:{args.port}")
    print(f"数据目录：{DATA_DIR}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
