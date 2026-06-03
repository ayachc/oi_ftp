#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import mimetypes
import posixpath
import re
import struct
import secrets
import shutil
import socket
import subprocess
import sys
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


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
STATIC_INDEX = ASSET_ROOT / "static" / "index.html"
DATA_DIR = ROOT / "ftp_data"
DOWNLOAD_DIR = DATA_DIR / "download"
UPLOAD_DIR = DATA_DIR / "upload"
CONFIG_FILE = DATA_DIR / "config.json"
META_FILE = DATA_DIR / "metadata.json"

DEFAULT_CONFIG = {
    "admin_password": "admin123",
    "file_size_limit_mb": 5,
    "port": 5000,
    "local_ips": [],
    "heartbeat_interval_seconds": 3,
}

STATE_LOCK = threading.RLock()
ADMIN_SESSIONS = set()
ONLINE_USERS = {}
KICKED_USERS = set()
OFFLINE_HISTORY_DEBOUNCE_SECONDS = 30


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
    try:
        port = int(cfg["port"])
        cfg["port"] = port if 1 <= port <= 65535 else DEFAULT_CONFIG["port"]
    except (TypeError, ValueError):
        cfg["port"] = DEFAULT_CONFIG["port"]
    if not isinstance(cfg["local_ips"], list):
        cfg["local_ips"] = []
    try:
        cfg["heartbeat_interval_seconds"] = max(1, int(cfg["heartbeat_interval_seconds"]))
    except (TypeError, ValueError):
        cfg["heartbeat_interval_seconds"] = DEFAULT_CONFIG["heartbeat_interval_seconds"]
    return cfg


def save_config(config):
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def prepare_runtime_config(cli_port=None):
    cfg = load_config()
    if cli_port is not None:
        cfg["port"] = cli_port
    cfg["local_ips"] = find_lan_ips()
    save_config(cfg)
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


def datetime_text(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


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


def remove_meta_for_area(meta, area):
    prefix = f"{area}:"
    for key in list(meta):
        if key.startswith(prefix):
            del meta[key]


def clear_directory(path):
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


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
    try:
        return STATIC_INDEX.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"静态页面不存在：{STATIC_INDEX}") from exc


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8") or "{}")


def validate_display_name(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("名字不能为空")
    if len(name) > 20:
        raise ValueError("名字最多 20 个字符")
    return name


def online_thresholds():
    interval = load_config()["heartbeat_interval_seconds"]
    return interval, interval * 3, interval * 9


def online_status(user, now=None):
    now = now or time.time()
    _, lag_seconds, offline_seconds = online_thresholds()
    age = now - user["last_seen"]
    if age >= offline_seconds:
        return "offline"
    if age >= lag_seconds:
        return "lagging"
    return "online"


def close_short_offline_gap(user, now):
    gap = now - user["last_seen"]
    if gap < OFFLINE_HISTORY_DEBOUNCE_SECONDS:
        return
    offline_start = user["last_seen"]
    history = user["history"]
    if history and history[-1]["status"] == "online" and history[-1].get("end") is None:
        history[-1]["end"] = offline_start
    history.append({"status": "offline", "start": offline_start, "end": now})
    history.append({"status": "online", "start": now, "end": None})


def online_history_view(user, now=None):
    now = now or time.time()
    history = [dict(item) for item in user["history"]]
    if now - user["last_seen"] >= OFFLINE_HISTORY_DEBOUNCE_SECONDS:
        if history and history[-1]["status"] == "online" and history[-1].get("end") is None:
            history[-1]["end"] = user["last_seen"]
            history.append({"status": "offline", "start": user["last_seen"], "end": None})
        elif history and history[-1]["status"] == "offline":
            history[-1]["end"] = None
    return [
        {
            "status": item["status"],
            "start": datetime_text(item["start"]),
            "end": datetime_text(item["end"]) if item.get("end") else None,
        }
        for item in history
    ]


def online_user_view(user, include_history=False, now=None):
    now = now or time.time()
    data = {
        "id": user["id"],
        "name": user["name"],
        "status": online_status(user, now),
        "last_seen": datetime_text(user["last_seen"]),
    }
    if include_history:
        data["history"] = online_history_view(user, now)
    return data


def online_state(client_id, is_admin):
    now = time.time()
    interval = load_config()["heartbeat_interval_seconds"]
    users = [
        online_user_view(user, now=now)
        for user in sorted(ONLINE_USERS.values(), key=lambda item: item["name"].lower())
    ]
    return {
        "ok": True,
        "is_admin": is_admin,
        "heartbeat_interval_seconds": interval,
        "registered": client_id in ONLINE_USERS,
        "self": online_user_view(ONLINE_USERS[client_id], include_history=True, now=now) if client_id in ONLINE_USERS else None,
        "users": users,
    }


def websocket_accept_key(key):
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def websocket_send(conn, opcode, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.extend([126])
        header.extend(struct.pack("!H", length))
    else:
        header.extend([127])
        header.extend(struct.pack("!Q", length))
    conn.sendall(bytes(header) + payload)


def websocket_read_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def websocket_read_frame(conn, timeout):
    conn.settimeout(timeout)
    try:
        head = websocket_read_exact(conn, 2)
        if not head:
            return None
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", websocket_read_exact(conn, 2) or b"\x00\x00")[0]
        elif length == 127:
            length = struct.unpack("!Q", websocket_read_exact(conn, 8) or b"\x00" * 8)[0]
        mask = websocket_read_exact(conn, 4) if masked else b""
        payload = websocket_read_exact(conn, length) if length else b""
        if payload is None:
            return None
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload
    except socket.timeout:
        return "timeout"
    except OSError:
        return None


def websocket_reject_payload(message):
    return json.dumps({"ok": False, "rejected": True, "error": message}, ensure_ascii=False)


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

    def handle_online_websocket(self, client_id, new_client=None):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.error_json("缺少 WebSocket 握手信息", 400, new_client)
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", websocket_accept_key(key))
        if new_client:
            self.send_header("Set-Cookie", f"client_id={new_client}; Path=/; SameSite=Lax; Max-Age=31536000")
        self.end_headers()

        conn = self.connection
        interval = load_config()["heartbeat_interval_seconds"]
        try:
            while True:
                with STATE_LOCK:
                    if client_id in KICKED_USERS:
                        websocket_send(conn, 0x1, websocket_reject_payload("登记已被管理员删除"))
                        websocket_send(conn, 0x8)
                        return
                    user = ONLINE_USERS.get(client_id)
                    if not user:
                        websocket_send(conn, 0x1, websocket_reject_payload("尚未登记"))
                        websocket_send(conn, 0x8)
                        return
                    now = time.time()
                    close_short_offline_gap(user, now)
                    user["last_seen"] = now
                    websocket_send(conn, 0x1, json.dumps({"ok": True, "type": "online", "last_seen": datetime_text(now)}, ensure_ascii=False))
                websocket_send(conn, 0x9, b"")
                deadline = time.time() + interval
                while time.time() < deadline:
                    frame = websocket_read_frame(conn, max(0.2, min(1.0, deadline - time.time())))
                    if frame == "timeout":
                        continue
                    if frame is None:
                        return
                    opcode, payload = frame
                    if opcode == 0x8:
                        websocket_send(conn, 0x8, payload)
                        return
                    if opcode == 0x9:
                        websocket_send(conn, 0xA, payload)
        except OSError:
            return
        finally:
            self.close_connection = True

    def do_GET(self):
        ensure_data_dirs()
        parsed = urllib.parse.urlparse(self.path)
        client_id, is_admin, new_client = self.identity()
        if parsed.path == "/api/online-ws":
            self.handle_online_websocket(client_id, new_client)
            return
        if parsed.path == "/":
            try:
                body = html_page()
            except RuntimeError as exc:
                body = str(exc).encode("utf-8")
                self.send_headers(
                    500,
                    "text/plain; charset=utf-8",
                    {"Content-Length": str(len(body))},
                    new_client,
                )
                self.wfile.write(body)
                return
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
                    "heartbeat_interval_seconds": cfg["heartbeat_interval_seconds"],
                    "download": list_area("download", client_id, is_admin),
                    "upload": list_area("upload", client_id, is_admin),
                }
            self.json(payload, new_client=new_client)
            return
        if parsed.path == "/api/online-state":
            with STATE_LOCK:
                payload = online_state(client_id, is_admin)
            self.json(payload, new_client=new_client)
            return
        if parsed.path == "/api/online-user":
            qs = urllib.parse.parse_qs(parsed.query)
            user_id = qs.get("id", [""])[0]
            with STATE_LOCK:
                user = ONLINE_USERS.get(user_id)
                if not user:
                    self.error_json("用户不存在", 404, new_client)
                    return
                payload = {"ok": True, "user": online_user_view(user, include_history=True)}
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
        if parsed.path == "/api/online-register":
            try:
                data = read_json_body(self)
                name = validate_display_name(data.get("name"))
            except (json.JSONDecodeError, ValueError) as exc:
                self.error_json(str(exc), 400, new_client)
                return
            with STATE_LOCK:
                for uid, user in ONLINE_USERS.items():
                    if uid != client_id and user["name"] == name:
                        self.error_json("名字不能重复", 409, new_client)
                        return
                now = time.time()
                KICKED_USERS.discard(client_id)
                if client_id in ONLINE_USERS:
                    user = ONLINE_USERS[client_id]
                    close_short_offline_gap(user, now)
                    user["name"] = name
                    user["last_seen"] = now
                else:
                    ONLINE_USERS[client_id] = {
                        "id": client_id,
                        "name": name,
                        "last_seen": now,
                        "history": [{"status": "online", "start": now, "end": None}],
                    }
                payload = {"ok": True, "user": online_user_view(ONLINE_USERS[client_id], include_history=True)}
            self.json(payload, new_client=new_client)
            return
        if parsed.path == "/api/online-heartbeat":
            try:
                data = read_json_body(self)
            except json.JSONDecodeError as exc:
                self.error_json(str(exc), 400, new_client)
                return
            with STATE_LOCK:
                if client_id in KICKED_USERS:
                    self.json({"ok": False, "rejected": True, "error": "登记已被管理员删除"}, status=409, new_client=new_client)
                    return
                user = ONLINE_USERS.get(client_id)
                if not user:
                    self.json({"ok": False, "rejected": True, "error": "尚未登记"}, status=409, new_client=new_client)
                    return
                name = (data.get("name") or user["name"]).strip()
                if name and name != user["name"]:
                    try:
                        name = validate_display_name(name)
                    except ValueError as exc:
                        self.error_json(str(exc), 400, new_client)
                        return
                    for uid, other in ONLINE_USERS.items():
                        if uid != client_id and other["name"] == name:
                            self.error_json("名字不能重复", 409, new_client)
                            return
                    user["name"] = name
                now = time.time()
                close_short_offline_gap(user, now)
                user["last_seen"] = now
                payload = {"ok": True, "user": online_user_view(user)}
            self.json(payload, new_client=new_client)
            return
        if parsed.path == "/api/online-delete":
            if not is_admin:
                self.error_json("需要管理员权限", 403, new_client)
                return
            try:
                data = read_json_body(self)
            except json.JSONDecodeError as exc:
                self.error_json(str(exc), 400, new_client)
                return
            user_id = data.get("id")
            with STATE_LOCK:
                if not user_id or user_id not in ONLINE_USERS:
                    self.error_json("用户不存在", 404, new_client)
                    return
                del ONLINE_USERS[user_id]
                KICKED_USERS.add(user_id)
            self.json({"ok": True}, new_client=new_client)
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
        if parsed.path == "/api/clear-uploads":
            if not is_admin:
                self.error_json("需要管理员权限", 403, new_client)
                return
            with STATE_LOCK:
                clear_directory(UPLOAD_DIR)
                meta = load_meta()
                remove_meta_for_area(meta, "upload")
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
            if area == "download" and len(files) != 1:
                self.error_json("左侧下载列表只能上传单个文件", 400, new_client)
                return
            if total > limit:
                self.error_json(f"上传内容超过 {cfg['file_size_limit_mb']} MB 限制", 413, new_client)
                return
            base = DOWNLOAD_DIR if area == "download" else UPLOAD_DIR
            saved = []
            with STATE_LOCK:
                meta = load_meta()
                for filename, payload in files:
                    if area == "download" and ("/" in filename or "\\" in filename):
                        self.error_json("左侧下载列表只能上传单个文件", 400, new_client)
                        return
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


def find_lan_ips():
    ips = []

    def add(ip):
        if not ip or ip in ips:
            return
        if ip.startswith(("127.", "0.", "169.254.")):
            return
        parts = ip.split(".")
        if len(parts) != 4:
            return
        try:
            if any(not 0 <= int(part) <= 255 for part in parts):
                return
        except ValueError:
            return
        ips.append(ip)

    hostname = socket.gethostname()
    try:
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            add(item[4][0])
    except socket.gaierror:
        pass
    for target in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 80))
                add(sock.getsockname()[0])
        except OSError:
            pass
    commands = [
        (["hostname", "-I"], r"\b([0-9]{1,3}(?:\.[0-9]{1,3}){3})\b"),
        (["ip", "-o", "-4", "addr", "show", "scope", "global"], r"\binet\s+([0-9.]+)/"),
        (["ifconfig"], r"\binet\s(?:addr:)?([0-9.]+)"),
        (["ipconfig"], r"IPv4[^\r\n:]*:\s*([0-9.]+)"),
    ]
    for cmd, pattern in commands:
        try:
            output = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for ip in re.findall(pattern, output):
            add(ip)
    fib_trie = Path("/proc/net/fib_trie")
    try:
        for ip in re.findall(r"\|--\s*([0-9.]+)\s*\n\s*/32 host LOCAL", fib_trie.read_text()):
            add(ip)
    except OSError:
        pass
    return ips


def main():
    parser = argparse.ArgumentParser(description="轻量文件收发 Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", default=None, type=int, help="监听端口；未指定时读取 config.json，仍没有则默认 5000")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    ensure_data_dirs()
    cfg = prepare_runtime_config(args.port)
    port = cfg["port"]
    server = ThreadingHTTPServer((args.host, port), FileServiceHandler)
    print("文件收发服务已启动")
    print(f"本机访问：http://127.0.0.1:{port}")
    for ip in cfg["local_ips"]:
        print(f"局域网访问：http://{ip}:{port}")
    print(f"数据目录：{DATA_DIR}")
    print(f"配置文件：{CONFIG_FILE}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
