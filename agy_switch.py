#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGY·SWITCH — Antigravity 账号切换 + 额度监控（本地精简版）

纯 Python 标准库实现，无任何第三方依赖。数据全部保存在本地 ~/.agy-switch/。

用法:
    python agy_switch.py [--port 8791] [--no-browser]

工作原理:
    - 凭据主体: ~/.gemini/oauth_creds.json (+ ~/.antigravity/oauth_creds.json 若存在)
    - 活动账号: ~/.gemini/google_accounts.json 的 "active" 字段
    - 切换 = 把当前登录快照进账号库，再把目标账号的凭据原子写回凭据文件
    - 额度 = 用账号 access_token 调用 Antigravity 后端 (cloudcode-pa.googleapis.com)
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
GEMINI_DIR = HOME / ".gemini"
LIVE_CREDS = GEMINI_DIR / "oauth_creds.json"
LIVE_ACCOUNTS = GEMINI_DIR / "google_accounts.json"
LEGACY_CREDS = HOME / ".antigravity" / "oauth_creds.json"

STORE_DIR = HOME / ".agy-switch"
STORE_FILE = STORE_DIR / "accounts.json"

WEB_DIR = Path(__file__).resolve().parent / "web"

IDE_EXE_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity" / "Antigravity.exe",
    Path("C:/Program Files/Antigravity/Antigravity.exe"),
    Path("C:/Program Files (x86)/Antigravity/Antigravity.exe"),
]

# ---- Antigravity 后端 API ------------------------------------------------
# Antigravity-Manager 同款的 daily -> sandbox -> prod 回退链
_API_HOSTS = [
    "https://daily-cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
]
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Antigravity 授权端点校验 token 的签发 client，额度查询必须用 Antigravity client
# （与 Antigravity-Manager 一致）签发的凭据；gemini-cli client 的凭据仅可用于切换。
_OAUTH_CLIENT_ENV = {
    "antigravity": ("AGY_ANTIGRAVITY_CLIENT_ID", "AGY_ANTIGRAVITY_CLIENT_SECRET"),
    "gemini-cli": ("AGY_GEMINI_CLIENT_ID", "AGY_GEMINI_CLIENT_SECRET"),
}
_OAUTH_CONFIG_ERROR = (
    "未配置 OAuth 客户端，请参考 README 创建 oauth_clients.local.json "
    "或成对设置 AGY_* 环境变量"
)


def _load_oauth_clients(config_path=None, environ=None):
    """Load complete client pairs without embedding credentials in public source."""
    env = os.environ if environ is None else environ
    if config_path is not None:
        candidates = [Path(config_path)]
    elif "AGY_OAUTH_CONFIG" in env:
        configured = env["AGY_OAUTH_CONFIG"].strip()
        candidates = [Path(configured)] if configured else []
    else:
        candidates = [
            Path(__file__).resolve().parent / "oauth_clients.local.json",
            Path.cwd() / "oauth_clients.local.json",
            Path.cwd().parent / "oauth_clients.local.json",
        ]
    config = {}
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            try:
                value = json.loads(candidate.read_text(encoding="utf-8-sig"))
                config = value if isinstance(value, dict) else {}
            except (OSError, ValueError, UnicodeError):
                config = {}
            break
    clients = []
    for key, (id_var, secret_var) in _OAUTH_CLIENT_ENV.items():
        if id_var in env or secret_var in env:
            client_id = env.get(id_var, "").strip()
            client_secret = env.get(secret_var, "").strip()
        else:
            entry = config.get(key, {})
            entry = entry if isinstance(entry, dict) else {}
            client_id = entry.get("client_id", "")
            client_secret = entry.get("client_secret", "")
            client_id = client_id.strip() if isinstance(client_id, str) else ""
            client_secret = client_secret.strip() if isinstance(client_secret, str) else ""
        if client_id and client_secret:
            clients.append((key, client_id, client_secret))
    return clients


OAUTH_CLIENTS = _load_oauth_clients()
LOGIN_CLIENT = next((client for client in OAUTH_CLIENTS if client[0] == "antigravity"), None)


def _login_client():
    if LOGIN_CLIENT is None:
        raise RuntimeError(_OAUTH_CONFIG_ERROR)
    return LOGIN_CLIENT
LOGIN_SCOPES = "openid https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/cclog https://www.googleapis.com/auth/experimentsandconfigs"
UA = "vscode/1.99.0 (Antigravity/4.3.0)"
TOKEN_REFRESH_SKEW = 900  # 提前 15 分钟视为过期

KEEP_PREFIXES = ("gemini", "claude", "gpt", "image", "imagen")
_NOTCH = ["#e8a33d", "#6da8e8", "#b78ae0", "#e07b9a", "#6dc8b8", "#c9b458"]

_LOCK = threading.RLock()


# ============================ 存储层 =======================================

def _atomic_write(path: Path, data, indent=2):
    """临时文件 + 原子替换，绝不写坏凭据文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def load_store():
    if not STORE_FILE.exists():
        return {"version": 1, "current_id": None, "accounts": []}
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        backup = STORE_FILE.with_suffix(".json.corrupt")
        STORE_FILE.replace(backup)
        return {"version": 1, "current_id": None, "accounts": []}


def save_store(store):
    _atomic_write(STORE_FILE, store)


def read_live_creds():
    if not LIVE_CREDS.exists():
        return None
    try:
        return json.loads(LIVE_CREDS.read_text(encoding="utf-8"))
    except Exception:
        return None


def clean_creds(creds):
    """剔除内部字段后返回可写回 IDE 凭据文件的副本。"""
    return {k: v for k, v in creds.items() if not k.startswith("_")}


def live_email():
    creds = read_live_creds()
    if not creds:
        return None
    return creds.get("email")


def find_account(store, **kw):
    for i, acc in enumerate(store["accounts"]):
        if all(acc.get(k) == v for k, v in kw.items()):
            return i, acc
    return -1, None


def upsert_account(store, creds, label=None):
    """按 email 去重地把一份凭据收入账号库，返回账号。"""
    email = (creds.get("email") or "").strip()
    idx, acc = find_account(store, email=email) if email else (-1, None)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if acc is None:
        acc = {
            "id": uuid.uuid4().hex[:8],
            "label": label or email or f"账号 {datetime.now().strftime('%m-%d %H:%M')}",
            "email": email or None,
            "plan_type": creds.get("plan_type") or "",
            "added_at": now,
            "notch": _NOTCH[len(store["accounts"]) % len(_NOTCH)],
            "creds": creds,
            "quota": None,
        }
        store["accounts"].append(acc)
    else:
        acc["creds"] = creds
        if creds.get("plan_type"):
            acc["plan_type"] = creds["plan_type"]
        if label:
            acc["label"] = label
    return acc


# ============================ Google OAuth ================================

def refresh_access_token(creds):
    """用 refresh_token 换新 access_token，就地更新 creds。

    refresh_token 只能由签发它的 client 续期：优先用上次成功的 client，
    其余 client 在 unauthorized_client/invalid_client 时依次回退。
    """
    if not creds.get("refresh_token"):
        raise RuntimeError("没有 refresh_token，无法刷新（请重新登录该账号）")
    if not OAUTH_CLIENTS:
        raise RuntimeError(_OAUTH_CONFIG_ERROR)
    order = list(OAUTH_CLIENTS)
    preferred = creds.get("_client_key")
    if preferred:
        order.sort(key=lambda c: c[0] != preferred)
    last_err = None
    for key, cid, sec in order:
        body = urllib.parse.urlencode({
            "client_id": cid,
            "client_secret": sec,
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if "unauthorized_client" in detail or "invalid_client" in detail:
                last_err = e
                continue  # 换下一个 client
            raise RuntimeError(f"refresh_token 被拒绝: {detail[:200]}") from e
        creds["access_token"] = resp["access_token"]
        creds["expiry_date"] = int(time.time() * 1000) + int(resp.get("expires_in", 3600)) * 1000
        if resp.get("refresh_token"):
            creds["refresh_token"] = resp["refresh_token"]
        creds["_client_key"] = key
        return creds
    raise RuntimeError(f"所有已知 OAuth client 均无法刷新该凭据: {last_err}")


def ensure_token(acc):
    """返回可用的 access_token，必要时刷新并落盘。"""
    creds = acc["creds"]
    expiry = creds.get("expiry_date") or 0
    if time.time() * 1000 <= expiry - TOKEN_REFRESH_SKEW * 1000:
        return creds["access_token"]
    refresh_access_token(creds)
    with _LOCK:
        # 该账号正被 IDE 使用时，同步刷新凭据文件，避免 IDE 拿到过期 token
        if acc.get("email") and acc["email"] == live_email():
            _atomic_write(LIVE_CREDS, clean_creds(creds))
        store = load_store()
        _, a = find_account(store, id=acc["id"])
        if a:
            a["creds"] = acc["creds"]
            save_store(store)
    return creds["access_token"]


class TokenInvalid(Exception):
    pass


def api_post(token, method, body, timeout=20):
    """带 daily/sandbox/prod 回退链的 v1internal POST。"""
    data = json.dumps(body).encode()
    last_err = None
    for host in _API_HOSTS:
        req = urllib.request.Request(f"{host}/v1internal:{method}", data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise TokenInvalid(f"HTTP {e.code}: token 无效或权限不足") from e
            last_err = e
        except Exception as e:  # 网络错误 -> 试下一个域名
            last_err = e
    raise RuntimeError(f"{method} 三个端点均失败: {last_err}")


def fetch_email(token):
    try:
        req = urllib.request.Request(USERINFO_URL)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")).get("email")
    except Exception:
        return None


# ============================ 额度监控 =====================================

def _fuse_bucket(buckets, want_3p):
    """按 Antigravity-Manager 的规则：取 5 小时与每周窗口中更受限的那个。"""
    h = w = None
    for b in buckets:
        text = f"{b.get('bucketId', '')} {b.get('window', '')} {b.get('displayName', '')}".lower()
        frac = b.get("remainingFraction")
        if frac is None:
            continue
        if h is None and ("5h" in text or "hour" in text):
            h = b
        elif w is None and ("week" in text or "7d" in text):
            w = b
    chosen, win = None, ""
    if w is not None and h is None:
        chosen, win = w, "week"
    elif h is not None or w is not None:
        if w is not None and w.get("remainingFraction", 1) <= 0.001:
            chosen, win = w, "week"
        elif h is not None and (w is None or h.get("remainingFraction", 1) <= w.get("remainingFraction", 1)):
            chosen, win = h, "5h"
        elif w is not None:
            chosen, win = w, "week"
    return chosen, win


def _match_group(model_id, groups):
    want_3p = model_id.lower().startswith(("claude", "gpt"))
    for g in groups:
        name = str(g.get("displayName", "")).lower()
        if want_3p and any(k in name for k in ("claude", "gpt", "3p")):
            return g
        if not want_3p and ("gemini" in name or not any(k in name for k in ("claude", "gpt", "3p"))):
            return g
    return groups[0] if groups else None


def _sort_key(m):
    mid = m["id"].lower()
    if mid.startswith("gemini-3-pro"):
        return (0, mid)
    if mid.startswith("gemini-3-flash"):
        return (1, mid)
    if mid.startswith("claude"):
        return (2, mid)
    if mid.startswith(("image", "imagen")):
        return (3, mid)
    return (4, mid)


def refresh_quota(acc):
    """拉取一个账号的额度，写入 acc['quota'] 并返回。失败时写 error。"""
    try:
        token = ensure_token(acc)
        if not acc.get("email"):
            acc["email"] = fetch_email(token) or acc.get("email")

        # 项目 / 套餐
        project, tier = "", ""
        try:
            info = api_post(token, "loadCodeAssist", {"metadata": {"ideType": "ANTIGRAVITY"}})
            project = info.get("cloudaicompanionProject") or ""
            t = info.get("currentTier") or info.get("paidTier") or {}
            tier = t.get("displayName") or t.get("id") or ""
        except Exception:
            pass

        body = {"project": project} if project else {}
        models_resp = api_post(token, "fetchAvailableModels", body)
        models_map = models_resp.get("models") or {}

        summary_groups = []
        try:
            summary = api_post(token, "retrieveUserQuotaSummary", body)
            summary_groups = summary.get("groups") or []
        except Exception:
            pass

        models = []
        for mid, m in models_map.items():
            if not mid.lower().startswith(KEEP_PREFIXES):
                continue
            qi = m.get("quotaInfo") or {}
            frac = qi.get("remainingFraction")
            if frac is None:
                continue
            pct = round(float(frac) * 100)
            reset = qi.get("resetTime") or ""
            win = ""
            g = _match_group(mid, summary_groups)
            if g:
                bucket, win = _fuse_bucket(g.get("buckets") or [], mid.lower().startswith(("claude", "gpt")))
                if bucket is not None:
                    pct = round(float(bucket.get("remainingFraction", 0)) * 100)
                    reset = bucket.get("resetTime") or reset
            models.append({
                "id": mid,
                "name": m.get("displayName") or mid,
                "pct": pct,
                "reset": reset,
                "window": win,
            })
        models.sort(key=_sort_key)
        acc["quota"] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tier": tier or acc.get("plan_type") or "",
            "models": models,
            "error": "" if models else "未返回任何带额度的模型",
        }
    except TokenInvalid as e:
        acc["quota"] = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "tier": "", "models": [],
                        "error": f"凭据无法查询额度（{e}）。该凭据可能由其他客户端签发（如 gemini-cli），"
                                 f"切换可用；查额度请用「🔑 OAuth 登录」重新添加此账号"}
    except Exception as e:
        acc["quota"] = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "tier": "", "models": [], "error": str(e)}
    return acc["quota"]


# ============================ OAuth 登录 ==================================

class OAuthCallback(BaseHTTPRequestHandler):
    """一次性回调服务器：接住 Google 重定向，换 token、入账、刷新额度后自毁。"""

    server_version = "AGYSwitch/1.0"

    def log_message(self, fmt, *args):
        pass

    def _page(self, ok, text):
        icon = "✓" if ok else "✕"
        color = "#62c370" if ok else "#e05252"
        body = (f"<!DOCTYPE html><meta charset='utf-8'><title>AGY·SWITCH</title>"
                f"<body style='background:#16171b;color:#e9e7e1;font-family:Segoe UI,Microsoft YaHei UI;"
                f"display:flex;align-items:center;justify-content:center;height:100vh'>"
                f"<div style='text-align:center'><div style='font-size:52px;color:{color}'>{icon}</div>"
                f"<p style='font-size:17px'>{text}</p>"
                f"<p style='color:#838794;font-size:13px'>可关闭此窗口，回到 AGY·SWITCH 查看账号列表</p></div>")
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global _oauth_cb
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "/callback" not in self.path:
            return self._page(False, "无效回调")
        if qs.get("state", [""])[0] != _oauth_cb.get("state"):
            return self._page(False, "state 校验失败，请重试")
        if "error" in qs:
            return self._page(False, f"授权被取消: {qs['error'][0]}")

        try:
            client = _login_client()
            code = qs["code"][0]
            body = urllib.parse.urlencode({
                "code": code,
                "client_id": client[1],
                "client_secret": client[2],
                "redirect_uri": _oauth_cb["redirect_uri"],
                "grant_type": "authorization_code",
            }).encode()
            req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                tok = json.loads(r.read().decode("utf-8"))
            if not tok.get("refresh_token"):
                raise RuntimeError("未返回 refresh_token（请先在该账号下移除本应用授权后重试）")
            email = fetch_email(tok["access_token"]) or ""
            creds = {
                "access_token": tok["access_token"],
                "refresh_token": tok["refresh_token"],
                "token_type": tok.get("token_type", "Bearer"),
                "expiry_date": int(time.time() * 1000) + int(tok.get("expires_in", 3600)) * 1000,
                "scope": tok.get("scope", LOGIN_SCOPES),
                "email": email,
            }
            if tok.get("id_token"):
                creds["id_token"] = tok["id_token"]
            with _LOCK:
                store = load_store()
                acc = upsert_account(store, creds, label=email or None)
                refresh_quota(acc)
                save_store(store)
            self._page(True, f"登录成功：{email or '账号已添加'}")
        except Exception as e:
            self._page(False, f"登录失败: {e}")
        finally:
            self.server.shutdown()


_oauth_cb = {"state": None, "redirect_uri": None, "server": None, "timer": None}


def oauth_start():
    """生成授权链接，启动回调服务器。返回 (auth_url, redirect_uri)。"""
    client = _login_client()
    if _oauth_cb.get("server"):
        try:
            _oauth_cb["server"].shutdown()
        except Exception:
            pass
    port = None
    for p in range(8792, 8802):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), OAuthCallback)
            port = p
            break
        except OSError:
            continue
    if port is None:
        raise RuntimeError("8792-8801 端口均被占用，无法启动 OAuth 回调服务")
    state = uuid.uuid4().hex
    redirect_uri = f"http://localhost:{port}/callback"
    qs = urllib.parse.urlencode({
        "client_id": client[1],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": LOGIN_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    _oauth_cb.update({"state": state, "redirect_uri": redirect_uri, "server": srv})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 5 分钟无人完成授权则自动关闭回调服务器
    def _expire():
        if _oauth_cb.get("server") is srv:
            try:
                srv.shutdown()
            except Exception:
                pass
    t = threading.Timer(300, _expire)
    t.daemon = True
    t.start()
    return f"{AUTH_URL}?{qs}"


# ============================ 切换 ========================================

def switch_to(store, acc):
    prev_email = live_email()

    # 1. 保全当前登录（绝不丢号）
    cur = read_live_creds()
    if cur and cur.get("email") and cur.get("email") != acc.get("email"):
        upsert_account(store, cur)

    # 2. 目标凭据原子写回
    _atomic_write(LIVE_CREDS, clean_creds(acc["creds"]))
    if LEGACY_CREDS.exists():
        _atomic_write(LEGACY_CREDS, clean_creds(acc["creds"]))

    # 3. 更新活动账号记录
    if LIVE_ACCOUNTS.exists():
        try:
            ga = json.loads(LIVE_ACCOUNTS.read_text(encoding="utf-8"))
        except Exception:
            ga = {}
        ga["active"] = acc.get("email") or ga.get("active")
        _atomic_write(LIVE_ACCOUNTS, ga)

    store["current_id"] = acc["id"]
    save_store(store)
    return prev_email


# ============================ IDE 进程 ====================================

def find_ide_exe():
    for p in IDE_EXE_CANDIDATES:
        if p.is_file():
            return p
    return None


def ide_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Antigravity.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout.upper()
        return "ANTIGRAVITY.EXE" in out
    except Exception:
        return False


def restart_ide():
    exe = find_ide_exe()
    if not exe:
        return False, "未找到 Antigravity.exe"
    if ide_running():
        subprocess.run(["taskkill", "/F", "/IM", "Antigravity.exe"],
                       capture_output=True, text=True, timeout=15)
        time.sleep(1.5)
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True,
                     creationflags=DETACHED)
    return True, "Antigravity 已重启"


# ============================ HTTP 服务 ===================================

def account_view(acc, current_id):
    q = acc.get("quota") or {}
    return {
        "id": acc["id"], "label": acc.get("label"), "email": acc.get("email"),
        "plan_type": acc.get("plan_type"), "notch": acc.get("notch"),
        "added_at": acc.get("added_at"), "current": acc["id"] == current_id,
        "quota": {
            "fetched_at": q.get("fetched_at"), "tier": q.get("tier") or acc.get("plan_type"),
            "error": q.get("error"), "models": q.get("models") or [],
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AGYSwitch/1.0"

    def log_message(self, fmt, *args):
        pass  # 精简：不刷屏

    # -- 基础 --
    def _json(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- 路由 --
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            f = WEB_DIR / "index.html"
            if not f.exists():
                return self._json({"error": "web/index.html 缺失"}, 500)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/state":
            self._json(self.state())
        elif path == "/api/quota":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            acc_id = (qs.get("id") or [""])[0]
            with _LOCK:
                store = load_store()
                _, acc = find_account(store, id=acc_id)
                if not acc:
                    return self._json({"error": "账号不存在"}, 404)
                refresh_quota(acc)
                save_store(store)
                self._json(account_view(acc, store["current_id"]))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        with _LOCK:
            store = load_store()
            try:
                if path == "/api/add":
                    creds = read_live_creds()
                    if not creds:
                        return self._json({"error": "未找到当前登录凭据"}, 400)
                    acc = upsert_account(store, creds, label=(body.get("label") or "").strip() or None)
                    refresh_quota(acc)
                    store["current_id"] = acc["id"]
                    save_store(store)
                    return self._json(account_view(acc, store["current_id"]))

                if path == "/api/import":
                    creds = body.get("creds")
                    if isinstance(creds, str):
                        try:
                            creds = json.loads(creds)
                        except Exception:
                            return self._json({"error": "凭据不是合法 JSON"}, 400)
                    if not isinstance(creds, dict) or not creds.get("refresh_token"):
                        return self._json({"error": "凭据需包含 refresh_token"}, 400)
                    if not creds.get("email"):
                        if creds.get("access_token"):
                            creds["email"] = fetch_email(creds["access_token"]) or ""
                    acc = upsert_account(store, creds, label=(body.get("label") or "").strip() or None)
                    refresh_quota(acc)
                    save_store(store)
                    return self._json(account_view(acc, store["current_id"]))

                if path == "/api/switch":
                    _, acc = find_account(store, id=body.get("id"))
                    if not acc:
                        return self._json({"error": "账号不存在"}, 404)
                    prev = switch_to(store, acc)
                    return self._json({"ok": True, "prev_email": prev, "restart_needed": ide_running()})

                if path == "/api/delete":
                    idx, acc = find_account(store, id=body.get("id"))
                    if idx < 0:
                        return self._json({"error": "账号不存在"}, 404)
                    store["accounts"].pop(idx)
                    if store["current_id"] == body.get("id"):
                        store["current_id"] = None
                    save_store(store)
                    return self._json({"ok": True})

                if path == "/api/rename":
                    _, acc = find_account(store, id=body.get("id"))
                    if not acc:
                        return self._json({"error": "账号不存在"}, 404)
                    label = (body.get("label") or "").strip()
                    if label:
                        acc["label"] = label
                        save_store(store)
                    return self._json(account_view(acc, store["current_id"]))

                if path == "/api/refresh_all":
                    for acc in store["accounts"]:
                        refresh_quota(acc)
                    save_store(store)
                    return self._json(self.state())

                if path == "/api/oauth_start":
                    url = oauth_start()
                    return self._json({"ok": True, "auth_url": url})

                if path == "/api/restart_ide":
                    ok, msg = restart_ide()
                    return self._json({"ok": ok, "message": msg})

                if path == "/api/ide_running":
                    return self._json({"running": ide_running()})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def state(self):
        store = load_store()
        cur_email = live_email()
        # live 凭据属于库中哪个账号（按 email 对齐，库可能比 live 更新）
        current_id = store.get("current_id")
        _, cur_acc = find_account(store, id=current_id) if current_id else (-1, None)
        if cur_acc and cur_email and cur_acc.get("email") != cur_email:
            current_id = None
        return {
            "live_email": cur_email,
            "ide_running": ide_running(),
            "ide_path": str(find_ide_exe() or ""),
            "creds_path": str(LIVE_CREDS),
            "current_id": current_id,
            "accounts": [account_view(a, current_id) for a in store["accounts"]],
        }


# ============================ 启动 ========================================

def bootstrap_first_account():
    """首次运行：把当前登录自动收进账号库。"""
    with _LOCK:
        store = load_store()
        creds = read_live_creds()
        if creds and not store["accounts"]:
            acc = upsert_account(store, creds)
            store["current_id"] = acc["id"]
            save_store(store)
            print(f"[agy] 已导入当前登录: {acc.get('email') or acc['label']}")


def main():
    ap = argparse.ArgumentParser(description="AGY·SWITCH — Antigravity 账号切换与额度监控")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    bootstrap_first_account()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[agy] AGY·SWITCH 已启动: {url}  (Ctrl+C 退出)")
    print(f"[agy] 凭据: {LIVE_CREDS}")
    print(f"[agy] 账号库: {STORE_FILE}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[agy] 已退出")


if __name__ == "__main__":
    main()
