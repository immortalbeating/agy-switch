#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
// AGY·SWITCH — Antigravity 账号切换 + 额度监控（Tauri 2 桌面版）
// 架构：进程内嵌 tiny_http 本地服务（127.0.0.1:8791 起）承载 REST API，
// Tauri 原生窗口加载该地址；前端与网页版完全一致。

use base64::Engine;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tiny_http::{Header, Method, Response, Server};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
use tauri::Manager;
use windows_sys::Win32::Security::Credentials::{CredFree, CredReadW, CredWriteW, CREDENTIALW};

// ============================ 常量 ========================================

const APP_VERSION: &str = "1.0.0";
const UA: &str = "vscode/1.99.0 (Antigravity/4.3.0)";
const API_HOSTS: [&str; 3] = [
    "https://daily-cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
];
const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const USERINFO_URL: &str = "https://www.googleapis.com/oauth2/v2/userinfo";
const AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";

// OAuth 客户端仅从私有文件或环境变量加载，保持 antigravity 优先。
const OAUTH_CONFIG_ERROR: &str = "未配置 OAuth 客户端，请参考 README 创建 oauth_clients.local.json 或设置 AGY_* 环境变量";
struct OAuthClient {
    key: &'static str,
    client_id: String,
    client_secret: String,
}

fn oauth_config_paths() -> Vec<PathBuf> {
    if let Some(path) = std::env::var_os("AGY_OAUTH_CONFIG") {
        return vec![PathBuf::from(path)];
    }
    let mut paths = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for ancestor in dir.ancestors().take(5) {
                paths.push(ancestor.join("oauth_clients.local.json"));
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        paths.push(cwd.join("oauth_clients.local.json"));
        if let Some(parent) = cwd.parent() {
            paths.push(parent.join("oauth_clients.local.json"));
        }
    }
    paths
}

fn select_oauth_clients(config: &Value, env: impl Fn(&str) -> Option<String>) -> Vec<OAuthClient> {
    [("antigravity", "AGY_ANTIGRAVITY"), ("gemini-cli", "AGY_GEMINI")]
        .into_iter()
        .filter_map(|(key, prefix)| {
            let env_id = env(&format!("{prefix}_CLIENT_ID"));
            let env_secret = env(&format!("{prefix}_CLIENT_SECRET"));
            let (client_id, client_secret) = if env_id.is_some() || env_secret.is_some() {
                (env_id.unwrap_or_default(), env_secret.unwrap_or_default())
            } else {
                (config[key]["client_id"].as_str().unwrap_or_default().to_string(),
                 config[key]["client_secret"].as_str().unwrap_or_default().to_string())
            };
            let client_id = client_id.trim().to_string();
            let client_secret = client_secret.trim().to_string();
            if client_id.is_empty() || client_secret.is_empty() { return None; }
            Some(OAuthClient { key, client_id, client_secret })
        }).collect()
}

fn oauth_clients() -> Result<&'static [OAuthClient], String> {
    static CLIENTS: OnceLock<Vec<OAuthClient>> = OnceLock::new();
    let clients = CLIENTS.get_or_init(|| {
        let config = oauth_config_paths().into_iter()
            .find(|path| path.is_file())
            .and_then(|path| std::fs::read_to_string(path).ok())
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or(Value::Null);
        select_oauth_clients(&config, |name| std::env::var(name).ok())
    });
    if clients.is_empty() { Err(OAUTH_CONFIG_ERROR.to_string()) } else { Ok(clients) }
}

fn select_login_client(clients: &[OAuthClient]) -> Result<&OAuthClient, String> {
    clients.iter().find(|client| client.key == "antigravity")
        .ok_or_else(|| OAUTH_CONFIG_ERROR.to_string())
}

fn login_client() -> Result<&'static OAuthClient, String> {
    select_login_client(oauth_clients()?)
}
const LOGIN_SCOPES: &str = "openid https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/cclog https://www.googleapis.com/auth/experimentsandconfigs";

const NOTCH: [&str; 6] = [
    "#e8a33d", "#6da8e8", "#b78ae0", "#e07b9a", "#6dc8b8", "#c9b458",
];
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED_PROCESS: u32 = 0x0000_0008 | 0x0000_0200;

// ============================ 路径 ========================================

fn home() -> PathBuf {
    PathBuf::from(std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into()))
}
fn gemini_dir() -> PathBuf {
    home().join(".gemini")
}
fn live_creds() -> PathBuf {
    gemini_dir().join("oauth_creds.json")
}
fn live_accounts() -> PathBuf {
    gemini_dir().join("google_accounts.json")
}
fn legacy_creds() -> PathBuf {
    home().join(".antigravity").join("oauth_creds.json")
}
fn store_file() -> PathBuf {
    home().join(".agy-switch").join("accounts.json")
}

// 窗口主题/尺寸等偏好（~/.agy-switch/prefs.json）
fn prefs_file() -> PathBuf {
    home().join(".agy-switch").join("prefs.json")
}

fn load_prefs() -> Value {
    read_json(&prefs_file()).unwrap_or_else(|| json!({}))
}

fn save_prefs_key(key: &str, v: Value) {
    let mut p = load_prefs();
    p[key] = v;
    let _ = atomic_write_json(&prefs_file(), &p);
}

fn store_lock() -> &'static Mutex<()> {
    static L: OnceLock<Mutex<()>> = OnceLock::new();
    L.get_or_init(|| Mutex::new(()))
}

static APP_HANDLE: OnceLock<tauri::AppHandle> = OnceLock::new();

// ============================ 托盘 =========================================

fn build_tray_menu(app: &tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    let en = load_prefs().get("lang").and_then(|v| v.as_str()) == Some("en");
    let (s_show, s_quit, s_switch, s_start, s_stop, s_restart) = if en {
        ("Show Main Window", "Quit", "Switch Account", "Start Antigravity", "Close Antigravity", "Restart Antigravity")
    } else {
        ("显示主窗口", "退出", "切换账号", "启动 Antigravity", "关闭 Antigravity", "重启 Antigravity")
    };
    let show = MenuItem::with_id(app, "show", s_show, true, None::<&str>)?;
    let start = MenuItem::with_id(app, "start_ide", s_start, true, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop_ide", s_stop, true, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart_ide", s_restart, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", s_quit, true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let sep3 = PredefinedMenuItem::separator(app)?;

    // 账号快捷切换子菜单（● 当前 / ○ 其他）
    let sub = Submenu::with_id(app, "accounts", s_switch, true)?;
    let store = load_store();
    let current_id = store.get("current_id").and_then(|v| v.as_str()).unwrap_or("");
    for acc in store["accounts"].as_array().cloned().unwrap_or_default() {
        let id = acc["id"].as_str().unwrap_or("").to_string();
        let name = acc["label"].as_str().unwrap_or("").to_string();
        if id.is_empty() {
            continue;
        }
        let mark = if id == current_id { "● " } else { "○ " };
        let item = MenuItem::with_id(app, &format!("acc:{id}"), &format!("{mark}{name}"), true, None::<&str>)?;
        sub.append(&item)?;
    }

    Menu::with_items(app, &[&sub, &sep1, &start, &stop, &restart, &sep2, &show, &sep3, &quit])
}

/// 库变更后刷新托盘菜单（未建托盘/网页版时静默跳过）
fn refresh_tray() {
    if let Some(app) = APP_HANDLE.get() {
        if let Ok(menu) = build_tray_menu(app) {
            if let Some(tray) = app.tray_by_id("main") {
                let _ = tray.set_menu(Some(menu));
            }
        }
    }
}

fn app_log(msg: &str) {
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(home().join(".agy-switch").join("app.log"))
    {
        use std::io::Write;
        let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
        let _ = writeln!(f, "[{now}] {msg}");
    }
}

fn show_main_window(app: &tauri::AppHandle) {
    app_log("show_main_window called");
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_always_on_top(true);
        let _ = w.set_focus();
        let _ = w.set_always_on_top(false);
        app_log("show_main_window: window raised and focused");
    } else {
        app_log("show_main_window: 'main' window not found");
    }
}

/// 按账号 id 执行切换（REST 与托盘菜单共用）
fn do_switch_by_id(id: &str) -> Result<Option<String>, String> {
    let mut store = load_store();
    let idx = find_idx(&store, id).ok_or("账号不存在")?;
    let acc = store["accounts"][idx].clone();
    let prev = switch_to(&mut store, &acc);
    refresh_tray();
    prev
}

fn http_client() -> &'static reqwest::blocking::Client {
    static C: OnceLock<reqwest::blocking::Client> = OnceLock::new();
    C.get_or_init(|| {
        reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(25))
            .build()
            .expect("http client")
    })
}

// ==================== IDE 凭据（Windows 凭据管理器 keyring） ================
// Antigravity IDE 把登录凭据存在系统凭据管理器 `gemini:antigravity` 条目里，
// blob 为 UTF-8 JSON: {auth_method, id_token(JWT，含邮箱), token{access_token,
// token_type, refresh_token, expiry}}。~/.gemini/oauth_creds.json 只是 CLI 兼容层。

const KEYRING_TARGET: &str = "gemini:antigravity";
const KEYRING_USER: &str = "antigravity";
const CRED_TYPE_GENERIC: u32 = 1;
const CRED_PERSIST_LOCAL_MACHINE: u32 = 2;

fn keyring_read_blob() -> Option<String> {
    unsafe {
        let mut ptr: *mut CREDENTIALW = std::ptr::null_mut();
        let target: Vec<u16> = KEYRING_TARGET.encode_utf16().chain([0]).collect();
        if CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut ptr) == 0 || ptr.is_null() {
            return None;
        }
        let blob = ((*ptr).CredentialBlobSize, (*ptr).CredentialBlob);
        let out = if blob.0 > 0 && !blob.1.is_null() {
            let bytes = std::slice::from_raw_parts(blob.1, blob.0 as usize);
            String::from_utf8(bytes.to_vec()).ok()
        } else {
            None
        };
        CredFree(ptr as *const _);
        out
    }
}

fn keyring_write_blob(blob: &str) -> Result<(), String> {
    unsafe {
        let mut data = blob.as_bytes().to_vec();
        let target: Vec<u16> = KEYRING_TARGET.encode_utf16().chain([0]).collect();
        let user: Vec<u16> = KEYRING_USER.encode_utf16().chain([0]).collect();
        let cred = CREDENTIALW {
            Flags: 0,
            Type: CRED_TYPE_GENERIC,
            TargetName: target.as_ptr() as *mut u16,
            Comment: std::ptr::null_mut(),
            LastWritten: std::mem::zeroed(),
            CredentialBlobSize: data.len() as u32,
            CredentialBlob: data.as_mut_ptr(),
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount: 0,
            Attributes: std::ptr::null_mut(),
            TargetAlias: std::ptr::null_mut(),
            UserName: user.as_ptr() as *mut u16,
        };
        if CredWriteW(&cred, 0) == 0 {
            return Err(format!(
                "写入凭据管理器失败: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }
}

fn decode_id_token_email(jwt: &str) -> Option<String> {
    let payload_b64 = jwt.split('.').nth(1)?;
    let payload = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(payload_b64)
        .ok()?;
    let v: Value = serde_json::from_slice(&payload).ok()?;
    v.get("email").and_then(|e| e.as_str()).map(String::from)
}

/// keyring blob -> 工具内部凭据形状（保留原始 blob 用于字节级还原）
fn ide_creds_from_blob(blob: &str) -> Option<Value> {
    let v: Value = serde_json::from_str(blob).ok()?;
    let tok = v.get("token")?;
    let email = v
        .get("id_token")
        .and_then(|t| t.as_str())
        .and_then(decode_id_token_email)
        .unwrap_or_default();
    let expiry_ms = tok
        .get("expiry")
        .and_then(|e| e.as_str())
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.timestamp_millis());
    Some(json!({
        "access_token": tok.get("access_token").cloned().unwrap_or(json!("")),
        "refresh_token": tok.get("refresh_token").cloned().unwrap_or(json!("")),
        "token_type": tok.get("token_type").cloned().unwrap_or(json!("Bearer")),
        "expiry_date": expiry_ms.unwrap_or(0),
        "email": email,
        "plan_type": "",
        "_source": "ide",
        "_ide_blob": blob,
    }))
}

fn store_find_by_refresh(store: &Value, rt: &str) -> Option<usize> {
    if rt.is_empty() {
        return None;
    }
    store["accounts"].as_array()?.iter().position(|x| {
        x.get("creds")
            .and_then(|c| c.get("refresh_token"))
            .and_then(|v| v.as_str())
            == Some(rt)
    })
}

/// 凭据 -> keyring blob。fresh=true 时强制用当前 token 重建（刷新后），
/// 否则若带有收编时的原始 blob 则原样返回（切换时字节级保真）。
fn blob_from_creds(creds: &Value, fresh: bool) -> String {
    if !fresh {
        if let Some(b) = creds.get("_ide_blob").and_then(|v| v.as_str()) {
            return b.to_string();
        }
    }
    let expiry = creds
        .get("expiry_date")
        .and_then(|v| v.as_i64())
        .and_then(chrono::DateTime::from_timestamp_millis)
        .map(|d| d.with_timezone(&chrono::Local).to_rfc3339_opts(SecondsFormat::Micros, false))
        .unwrap_or_default();
    let mut token = json!({
        "access_token": creds.get("access_token").cloned().unwrap_or(json!("")),
        "token_type": creds.get("token_type").cloned().unwrap_or(json!("Bearer")),
        "refresh_token": creds.get("refresh_token").cloned().unwrap_or(json!("")),
        "expiry": expiry,
    });
    if let Some(idt) = creds.get("id_token") {
        if !idt.is_null() && idt.as_str().map(|s| !s.is_empty()).unwrap_or(false) {
            token["id_token"] = idt.clone();
        }
    }
    json!({"auth_method": "consumer", "token": token}).to_string()
}

/// IDE 当前登录凭据（keyring 正源），邮箱经三级回退解析：
/// id_token -> 账号库按 refresh_token 匹配 -> userinfo 接口。
/// （IDE 刷新 token 后重写 blob 会丢掉 id_token，故不能只靠它。）
fn ide_current_creds() -> Option<Value> {
    let blob = keyring_read_blob()?;
    let mut creds = ide_creds_from_blob(&blob)?;
    let mut email = creds["email"].as_str().filter(|s| !s.is_empty()).map(String::from);
    if email.is_none() {
        let rt = creds["refresh_token"].as_str().unwrap_or("");
        let store = load_store();
        if let Some(i) = store_find_by_refresh(&store, rt) {
            email = store["accounts"][i]
                .get("email")
                .and_then(|e| e.as_str())
                .filter(|s| !s.is_empty())
                .map(String::from);
            if creds["plan_type"].as_str().unwrap_or("").is_empty() {
                if let Some(pt) = store["accounts"][i].get("plan_type").and_then(|v| v.as_str()) {
                    creds["plan_type"] = json!(pt);
                }
            }
        }
    }
    if email.is_none() {
        let at = creds["access_token"].as_str().unwrap_or("");
        if !at.is_empty() {
            email = fetch_email(at);
        }
    }
    if let Some(e) = email {
        creds["email"] = json!(e);
    }
    Some(creds)
}

/// IDE 当前登录的账号
fn ide_email() -> Option<String> {
    ide_current_creds()?
        .get("email")
        .and_then(|e| e.as_str())
        .filter(|s| !s.is_empty())
        .map(String::from)
}

// ============================ 文件与存储 ===================================

fn read_json(p: &Path) -> Option<Value> {
    std::fs::read_to_string(p).ok().and_then(|s| serde_json::from_str(&s).ok())
}

/// 临时文件 + 原子替换，绝不写坏凭据文件
fn atomic_write_json(p: &Path, v: &Value) -> Result<(), String> {
    let dir = p.parent().ok_or("路径无父目录")?;
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let name = p.file_name().unwrap_or_default().to_string_lossy().to_string();
    let tmp = dir.join(format!(".{}.tmp-{}", name, uuid::Uuid::new_v4().simple()));
    let data = serde_json::to_string_pretty(v).map_err(|e| e.to_string())?;
    std::fs::write(&tmp, data).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, p).map_err(|e| e.to_string())
}

fn load_store() -> Value {
    read_json(&store_file()).unwrap_or_else(|| {
        json!({"version": 1, "current_id": null, "accounts": []})
    })
}

fn save_store(store: &Value) {
    let _ = atomic_write_json(&store_file(), store);
}

fn find_idx(store: &Value, id: &str) -> Option<usize> {
    store["accounts"]
        .as_array()?
        .iter()
        .position(|a| a.get("id").and_then(|v| v.as_str()) == Some(id))
}

fn clean_creds(creds: &Value) -> Value {
    match creds {
        Value::Object(m) => Value::Object(
            m.iter()
                .filter(|(k, _)| !k.starts_with('_'))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn read_live_creds() -> Option<Value> {
    read_json(&live_creds())
}

fn live_email() -> Option<String> {
    read_live_creds().and_then(|c| {
        c.get("email")
            .and_then(|e| e.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    })
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, false)
}

fn truncate(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.to_string()
    } else {
        let mut end = n;
        while !s.is_char_boundary(end) {
            end -= 1;
        }
        format!("{}…", &s[..end])
    }
}

/// 按 email 去重地把一份凭据收入账号库，返回账号
fn upsert_account(store: &mut Value, creds: Value, label: Option<&str>) -> Value {
    let email = creds
        .get("email")
        .and_then(|e| e.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let accounts = store["accounts"].as_array_mut().expect("accounts array");
    let mut found: Option<usize> = None;
    if !email.is_empty() {
        for (i, a) in accounts.iter().enumerate() {
            if a.get("email").and_then(|e| e.as_str()) == Some(email.as_str()) {
                found = Some(i);
                break;
            }
        }
    }
    match found {
        Some(i) => {
            let a = &mut accounts[i];
            a["creds"] = creds.clone();
            if let Some(pt) = creds.get("plan_type").and_then(|v| v.as_str()) {
                if !pt.is_empty() {
                    a["plan_type"] = json!(pt);
                }
            }
            if let Some(l) = label {
                if !l.is_empty() {
                    a["label"] = json!(l);
                }
            }
            a.clone()
        }
        None => {
            let n = accounts.len();
            let id = {
                let u = uuid::Uuid::new_v4().simple().to_string();
                u[..8].to_string()
            };
            // 默认备注用「账号 N」——邮箱已有单独一行，不做备注
            let default_label = format!("账号 {}", n + 1);
            let acc = json!({
                "id": id,
                "label": label.filter(|s| !s.is_empty()).unwrap_or(&default_label),
                "email": if email.is_empty() { Value::Null } else { json!(email) },
                "plan_type": creds.get("plan_type").cloned().unwrap_or(json!("")),
                "added_at": now_iso(),
                "notch": NOTCH[n % NOTCH.len()],
                "creds": creds,
                "quota": Value::Null,
            });
            accounts.push(acc.clone());
            acc
        }
    }
}

// ============================ Google OAuth ================================

fn refresh_access_token(creds: &mut Value) -> Result<(), String> {
    let clients = oauth_clients()?;
    let rt = creds
        .get("refresh_token")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or("没有 refresh_token，无法刷新（请重新登录该账号）")?
        .to_string();
    // refresh_token 只能由签发它的 client 续期：优先用上次成功的 client
    let pref = creds.get("_client_key").and_then(|v| v.as_str());
    let mut order: Vec<&OAuthClient> = clients.iter().collect();
    order.sort_by_key(|c| Some(c.key) != pref);
    let mut last = String::new();
    for client in order {
        let (key, cid, csec) = (client.key, client.client_id.as_str(), client.client_secret.as_str());
        let resp = http_client()
            .post(TOKEN_URL)
            .header("User-Agent", UA)
            .form(&[
                ("client_id", cid),
                ("client_secret", csec),
                ("refresh_token", rt.as_str()),
                ("grant_type", "refresh_token"),
            ])
            .send();
        match resp {
            Ok(r) if r.status().is_success() => {
                let tok: Value = r.json().map_err(|e| e.to_string())?;
                creds["access_token"] = tok["access_token"].clone();
                let exp = tok.get("expires_in").and_then(|v| v.as_i64()).unwrap_or(3600);
                creds["expiry_date"] = json!(now_ms() + exp * 1000);
                if let Some(nrt) = tok.get("refresh_token").and_then(|v| v.as_str()) {
                    if !nrt.is_empty() {
                        creds["refresh_token"] = json!(nrt);
                    }
                }
                creds["_client_key"] = json!(key);
                return Ok(());
            }
            Ok(r) => {
                let body = r.text().unwrap_or_default();
                if body.contains("unauthorized_client") || body.contains("invalid_client") {
                    last = body;
                    continue;
                }
                return Err(format!("refresh_token 被拒绝: {}", truncate(&body, 200)));
            }
            Err(e) => last = e.to_string(),
        }
    }
    Err(format!("所有已知 OAuth client 均无法刷新该凭据: {}", truncate(&last, 120)))
}

/// 返回可用的 access_token，必要时刷新并落盘
fn ensure_token(acc: &mut Value) -> Result<String, String> {
    let expiry = acc["creds"].get("expiry_date").and_then(|v| v.as_i64()).unwrap_or(0);
    if now_ms() <= expiry - 900_000 {
        return Ok(acc["creds"]["access_token"].as_str().unwrap_or_default().to_string());
    }
    refresh_access_token(&mut acc["creds"])?;
    {
        let email = acc.get("email").and_then(|e| e.as_str()).map(String::from);
        let _g = store_lock().lock();
        // 该账号正被 IDE 使用（keyring 正源）：同步 keyring，IDE 无缝拾取
        if email.as_deref() == ide_email().as_deref() {
            let _ = keyring_write_blob(&blob_from_creds(&acc["creds"], true));
        }
        // CLI 文件层同步
        if email.as_deref() == live_email().as_deref() {
            let _ = atomic_write_json(&live_creds(), &clean_creds(&acc["creds"]));
        }
        let mut store = load_store();
        if let Some(i) = find_idx(&store, acc["id"].as_str().unwrap_or("")) {
            store["accounts"][i]["creds"] = acc["creds"].clone();
            save_store(&store);
        }
    }
    Ok(acc["creds"]["access_token"].as_str().unwrap_or_default().to_string())
}

fn fetch_email(token: &str) -> Option<String> {
    http_client()
        .get(USERINFO_URL)
        .timeout(Duration::from_secs(15))
        .bearer_auth(token)
        .send()
        .ok()?
        .json::<Value>()
        .ok()?
        .get("email")
        .and_then(|e| e.as_str())
        .map(String::from)
}

// TokenInvalid 用前缀标记，调用方据其给出可操作提示
const TOKEN_INVALID: &str = "TokenInvalid|";

fn api_post(token: &str, method_name: &str, body: &Value) -> Result<Value, String> {
    let mut last = String::new();
    for host in API_HOSTS {
        let url = format!("{host}/v1internal:{method_name}");
        match http_client().post(&url).bearer_auth(token).header("User-Agent", UA).json(body).send() {
            Ok(resp) => {
                let st = resp.status();
                match resp.text() {
                    Ok(t) => {
                        if st.is_success() {
                            return serde_json::from_str(&t).map_err(|e| e.to_string());
                        }
                        if st.as_u16() == 401 || st.as_u16() == 403 {
                            return Err(format!("{TOKEN_INVALID}HTTP {}: token 无效或权限不足", st.as_u16()));
                        }
                        last = format!("HTTP {st}: {}", truncate(&t, 160));
                    }
                    Err(e) => last = e.to_string(),
                }
            }
            Err(e) => last = e.to_string(),
        }
    }
    Err(format!("{method_name} 三个端点均失败: {}", truncate(&last, 160)))
}

// ============================ 额度监控 =====================================

/// 解析 retrieveUserQuotaSummary 的分组（与官方 IDE 一致：
/// Gemini Models / Claude and GPT models × 每周 + 5 小时），每周在前
fn parse_quota_groups(summary: &Value) -> Vec<Value> {
    let mut groups: Vec<Value> = vec![];
    for g in summary.get("groups").and_then(|g| g.as_array()).cloned().unwrap_or_default() {
        let name = g
            .get("displayName")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut buckets: Vec<Value> = vec![];
        for b in g.get("buckets").and_then(|b| b.as_array()).cloned().unwrap_or_default() {
            let Some(frac) = b.get("remainingFraction").and_then(|v| v.as_f64()) else {
                continue;
            };
            let raw = format!(
                "{} {}",
                b.get("window").and_then(|v| v.as_str()).unwrap_or(""),
                b.get("bucketId").and_then(|v| v.as_str()).unwrap_or("")
            )
            .to_lowercase();
            let win = if raw.contains("5h") || raw.contains("hour") { "5h" } else { "week" };
            buckets.push(json!({
                "window": win,
                "pct": (frac * 100.0).round() as i64,
                "reset": b.get("resetTime").and_then(|v| v.as_str()).unwrap_or(""),
            }));
        }
        buckets.sort_by_key(|b| b["window"].as_str() != Some("week"));
        if !name.is_empty() && !buckets.is_empty() {
            groups.push(json!({"name": name, "buckets": buckets}));
        }
    }
    groups
}

fn refresh_quota(acc: &mut Value) {
    let result = (|| -> Result<Value, String> {
        let token = ensure_token(acc)?;
        let email_missing = acc
            .get("email")
            .and_then(|e| e.as_str())
            .map(|s| s.is_empty())
            .unwrap_or(true);
        if email_missing {
            if let Some(em) = fetch_email(&token) {
                acc["email"] = json!(em);
            }
        }

        // 项目 / 套餐（付费档位优先展示，如 Google AI Pro）
        let (mut project, mut tier) = (String::new(), String::new());
        if let Ok(info) = api_post(&token, "loadCodeAssist", &json!({"metadata": {"ideType": "ANTIGRAVITY"}})) {
            project = info
                .get("cloudaicompanionProject")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let tier_name = |t: Option<&Value>| -> String {
                t.and_then(|t| {
                    t.get("name")
                        .and_then(|v| v.as_str())
                        .or_else(|| t.get("id").and_then(|v| v.as_str()))
                })
                .unwrap_or("")
                .to_string()
            };
            let paid = tier_name(info.get("paidTier"));
            tier = if paid.is_empty() {
                tier_name(info.get("currentTier"))
            } else {
                paid
            };
        }

        let body = if project.is_empty() { json!({}) } else { json!({"project": project}) };
        let summary = api_post(&token, "retrieveUserQuotaSummary", &body)?;
        let groups = parse_quota_groups(&summary);
        if groups.is_empty() {
            return Err("额度接口未返回分组数据".into());
        }
        let tier_final = if tier.is_empty() {
            acc.get("plan_type").cloned().unwrap_or(json!(""))
        } else {
            json!(tier)
        };
        Ok(json!({
            "fetched_at": now_iso(),
            "tier": tier_final,
            "groups": groups,
            "error": "",
        }))
    })();

    acc["quota"] = match result {
        Ok(q) => q,
        Err(e) => {
            let msg = if let Some(rest) = e.strip_prefix(TOKEN_INVALID) {
                format!("凭据无法查询额度（{rest}）。该凭据由其他客户端签发（如 gemini-cli），IDE 后端只认 IDE 自己签发的凭据；请用「＋ 添加当前登录」收录 IDE 正在使用的账号，或 🔑 OAuth 登录添加")
            } else {
                e
            };
            json!({"fetched_at": now_iso(), "tier": "", "groups": [], "error": msg})
        }
    };
}

// ============================ 切换 ========================================

fn switch_to(store: &mut Value, acc: &Value) -> Result<Option<String>, String> {
    let prev = ide_email().or_else(live_email);
    // 1. 保全当前登录（IDE keyring 正源 + CLI 文件层，绝不丢号）
    if let Some(cur) = ide_current_creds() {
        let ce = cur.get("email").and_then(|e| e.as_str()).unwrap_or("").to_string();
        let target = acc.get("email").and_then(|e| e.as_str()).unwrap_or("");
        if !ce.is_empty() && ce != target {
            upsert_account(store, cur, None);
        }
    }
    if let Some(cur) = read_live_creds() {
        let ce = cur.get("email").and_then(|e| e.as_str()).unwrap_or("").to_string();
        let target = acc.get("email").and_then(|e| e.as_str()).unwrap_or("");
        if !ce.is_empty() && ce != target {
            upsert_account(store, cur, None);
        }
    }
    // 2. 目标凭据写回两个凭据源（keyring 带 _ide_blob 时字节级还原）
    keyring_write_blob(&blob_from_creds(&acc["creds"], false))?;
    atomic_write_json(&live_creds(), &clean_creds(&acc["creds"]))?;
    if legacy_creds().exists() {
        let _ = atomic_write_json(&legacy_creds(), &clean_creds(&acc["creds"]));
    }
    // 3. 更新活动账号记录
    if live_accounts().exists() {
        let mut ga = read_json(&live_accounts()).unwrap_or_else(|| json!({}));
        if let Some(em) = acc.get("email").and_then(|e| e.as_str()) {
            if !em.is_empty() {
                ga["active"] = json!(em);
            }
        }
        atomic_write_json(&live_accounts(), &ga)?;
    }
    store["current_id"] = acc["id"].clone();
    save_store(store);
    Ok(prev)
}

// ============================ IDE 进程 ====================================

fn find_ide_exe() -> Option<PathBuf> {
    if let Some(custom) = load_prefs().get("ide_path").and_then(|v| v.as_str()) {
        let trimmed = custom.trim();
        if !trimmed.is_empty() {
            let p = PathBuf::from(trimmed);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    let la = std::env::var("LOCALAPPDATA").ok()?;
    [
        PathBuf::from(&la).join(r"Programs\Antigravity\Antigravity.exe"),
        PathBuf::from(r"C:\Program Files\Antigravity\Antigravity.exe"),
        PathBuf::from(r"C:\Program Files (x86)\Antigravity\Antigravity.exe"),
    ]
    .into_iter()
    .find(|p| p.is_file())
}

fn ide_running() -> bool {
    std::process::Command::new("tasklist")
        .args(["/FI", "IMAGENAME eq Antigravity.exe", "/NH"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_uppercase().contains("ANTIGRAVITY.EXE"))
        .unwrap_or(false)
}

fn start_ide() -> (bool, String) {
    let Some(exe) = find_ide_exe() else {
        return (false, "未找到 Antigravity.exe，请在设置中指定程序路径".into());
    };
    if ide_running() {
        return (true, "Antigravity 已经在运行中".into());
    }
    let spawned = std::process::Command::new(&exe)
        .current_dir(exe.parent().unwrap_or(Path::new(".")))
        .creation_flags(DETACHED_PROCESS)
        .spawn();
    match spawned {
        Ok(_) => (true, "Antigravity 已启动".into()),
        Err(e) => (false, format!("启动失败: {e}")),
    }
}

fn stop_ide() -> (bool, String) {
    if !ide_running() {
        return (true, "Antigravity 当前未在运行".into());
    }
    let out = std::process::Command::new("taskkill")
        .args(["/F", "/IM", "Antigravity.exe"])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
    match out {
        Ok(_) => (true, "Antigravity 已关闭".into()),
        Err(e) => (false, format!("关闭失败: {e}")),
    }
}

fn restart_ide() -> (bool, String) {
    let Some(exe) = find_ide_exe() else {
        return (false, "未找到 Antigravity.exe，请在设置中指定程序路径".into());
    };
    if ide_running() {
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/IM", "Antigravity.exe"])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
        std::thread::sleep(Duration::from_millis(1500));
    }
    let spawned = std::process::Command::new(&exe)
        .current_dir(exe.parent().unwrap_or(Path::new(".")))
        .creation_flags(DETACHED_PROCESS)
        .spawn();
    match spawned {
        Ok(_) => (true, "Antigravity 已重启".into()),
        Err(e) => (false, format!("启动失败: {e}")),
    }
}

fn browse_ide_exe() -> Option<String> {
    let script = r#"[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; $f = New-Object System.Windows.Forms.OpenFileDialog; $f.Filter = 'Antigravity (Antigravity.exe)|Antigravity.exe|Executable (*.exe)|*.exe|All Files (*.*)|*.*'; $f.Title = '选择 Antigravity.exe 程序文件'; if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Output $f.FileName }"#;
    let out = std::process::Command::new("powershell")
        .args(["-NoProfile", "-Command", script])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .ok()?;
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if path.is_empty() { None } else { Some(path) }
}

// ============================ 视图 ========================================

fn account_view(acc: &Value, current_id: &str) -> Value {
    let q = acc.get("quota").cloned().unwrap_or(Value::Null);
    let tier = q
        .get("tier")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .or_else(|| acc.get("plan_type").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();
    json!({
        "id": acc.get("id").cloned().unwrap_or(Value::Null),
        "label": acc.get("label").cloned().unwrap_or(Value::Null),
        "email": acc.get("email").cloned().unwrap_or(Value::Null),
        "plan_type": acc.get("plan_type").cloned().unwrap_or(json!("")),
        "notch": acc.get("notch").cloned().unwrap_or(json!("#e8a33d")),
        "added_at": acc.get("added_at").cloned().unwrap_or(Value::Null),
        "current": acc.get("id").and_then(|v| v.as_str()) == Some(current_id),
        "quota": {
            "fetched_at": q.get("fetched_at").cloned().unwrap_or(Value::Null),
            "tier": tier,
            "error": q.get("error").cloned().unwrap_or(json!("")),
            "groups": q.get("groups").cloned().unwrap_or(json!([])),
        },
    })
}

fn state_view() -> Value {
    let store = load_store();
    let ide_em = ide_email();
    let file_em = live_email();
    let cur_email = ide_em.clone().or_else(|| file_em.clone());
    let mut current_id = store
        .get("current_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    match find_idx(&store, &current_id) {
        Some(i) => {
            let em = store["accounts"][i]
                .get("email")
                .and_then(|e| e.as_str())
                .unwrap_or("");
            if let Some(le) = &cur_email {
                if em != le {
                    current_id.clear();
                }
            }
        }
        None => current_id.clear(),
    }
    json!({
        "live_email": cur_email,
        "ide_email": ide_em,
        "creds_email": file_em,
        "ide_running": ide_running(),
        "ide_path": find_ide_exe().map(|p| p.to_string_lossy().to_string()).unwrap_or_default(),
        "creds_path": live_creds().to_string_lossy(),
        "current_id": if current_id.is_empty() { Value::Null } else { json!(current_id) },
        "accounts": store["accounts"]
            .as_array()
            .map(|a| a.iter().map(|acc| account_view(acc, &current_id)).collect::<Vec<_>>())
            .unwrap_or_default(),
    })
}

// ============================ OAuth 登录 ===================================

fn percent_encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() => {
                let hex = std::str::from_utf8(&b[i + 1..i + 3])
                    .ok()
                    .and_then(|h| u8::from_str_radix(h, 16).ok());
                match hex {
                    Some(v) => {
                        out.push(v);
                        i += 3;
                    }
                    None => {
                        out.push(b[i]);
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

fn parse_query(q: &str) -> HashMap<String, String> {
    q.split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (percent_decode(k), percent_decode(v))
        })
        .collect()
}

fn oauth_start() -> Result<String, String> {
    let client = login_client()?;
    let mut listener = None;
    let mut port = 0u16;
    for p in 8792..8802 {
        if let Ok(l) = TcpListener::bind(("127.0.0.1", p)) {
            listener = Some(l);
            port = p;
            break;
        }
    }
    let listener = listener.ok_or("8792-8801 端口均被占用，无法启动 OAuth 回调服务")?;
    let state = {
        let u = uuid::Uuid::new_v4().simple().to_string();
        u[..16].to_string()
    };
    let redirect_uri = format!("http://localhost:{port}/callback");
    let qs = [
        ("client_id", client.client_id.as_str()),
        ("redirect_uri", redirect_uri.as_str()),
        ("response_type", "code"),
        ("scope", LOGIN_SCOPES),
        ("access_type", "offline"),
        ("prompt", "consent"),
        ("include_granted_scopes", "true"),
        ("state", state.as_str()),
    ]
    .map(|(k, v)| format!("{k}={}", percent_encode(v)))
    .join("&");
    let auth_url = format!("{AUTH_URL}?{qs}");
    let st = state.clone();
    std::thread::spawn(move || oauth_callback_server(listener, st, redirect_uri));
    Ok(auth_url)
}

fn oauth_callback_server(listener: TcpListener, state: String, redirect_uri: String) {
    for stream in listener.incoming().flatten() {
        if handle_oauth_conn(stream, &state, &redirect_uri) {
            break;
        }
    }
}

fn write_page(stream: &mut TcpStream, ok: bool, text: &str) {
    let icon = if ok { "✓" } else { "✕" };
    let color = if ok { "#2f9e4d" } else { "#d64545" };
    let body = format!(
        "<!DOCTYPE html><meta charset='utf-8'><title>AGY·SWITCH</title><style>\
body{{margin:0;background:#f7f7f4;color:#26251f;font-family:'Segoe UI','Microsoft YaHei UI',sans-serif;\
display:flex;align-items:center;justify-content:center;height:100vh}}\
.sub{{color:#8f8d82;font-size:13px}}\
@media (prefers-color-scheme:dark){{\
body{{background:#16171b;color:#e9e7e1}}.sub{{color:#838794}}}}\
</style>\
<div style='text-align:center'><div style='font-size:52px;color:{color}'>{icon}</div>\
<p style='font-size:17px'>{text}</p>\
<p class='sub'>可关闭此窗口，回到 AGY·SWITCH 查看账号列表</p></div>"
    );
    let resp = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(resp.as_bytes());
    let _ = stream.flush();
}

/// 返回 true 表示 OAuth 流程结束（成功或失败），监听器可以退出
fn handle_oauth_conn(mut stream: TcpStream, state: &str, redirect_uri: &str) -> bool {
    let mut buf = [0u8; 8192];
    let n = stream.read(&mut buf).unwrap_or(0);
    let req = String::from_utf8_lossy(&buf[..n]).to_string();
    let line = req.lines().next().unwrap_or("");
    let target = line.split_whitespace().nth(1).unwrap_or("");
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if !path.starts_with("/callback") {
        write_page(&mut stream, false, "无效回调");
        return false;
    }
    let params = parse_query(query);
    if params.get("state").map(String::as_str) != Some(state) {
        write_page(&mut stream, false, "state 校验失败，请重试");
        return true;
    }
    if let Some(e) = params.get("error") {
        write_page(&mut stream, false, &format!("授权被取消: {e}"));
        return true;
    }
    let code = params.get("code").cloned().unwrap_or_default();
    match exchange_and_store(&code, redirect_uri) {
        Ok(email) => write_page(&mut stream, true, &format!("登录成功：{email}")),
        Err(e) => write_page(&mut stream, false, &format!("登录失败: {e}")),
    }
    true
}

fn exchange_and_store(code: &str, redirect_uri: &str) -> Result<String, String> {
    let client = login_client()?;
    let tok: Value = http_client()
        .post(TOKEN_URL)
        .header("User-Agent", UA)
        .form(&[
            ("code", code),
            ("client_id", client.client_id.as_str()),
            ("client_secret", client.client_secret.as_str()),
            ("redirect_uri", redirect_uri),
            ("grant_type", "authorization_code"),
        ])
        .send()
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| format!("授权码换取失败: {e}"))?
        .json()
        .map_err(|e| e.to_string())?;
    let refresh = tok
        .get("refresh_token")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or("未返回 refresh_token（请先在该账号下移除本应用授权后重试）")?;
    let access = tok["access_token"].as_str().unwrap_or_default();
    let email = fetch_email(access).unwrap_or_default();
    let exp = tok.get("expires_in").and_then(|v| v.as_i64()).unwrap_or(3600);
    let mut creds = json!({
        "access_token": access,
        "refresh_token": refresh,
        "token_type": tok.get("token_type").cloned().unwrap_or(json!("Bearer")),
        "expiry_date": now_ms() + exp * 1000,
        "scope": tok.get("scope").cloned().unwrap_or(json!(LOGIN_SCOPES)),
        "email": email,
    });
    if let Some(idt) = tok.get("id_token") {
        if !idt.is_null() {
            creds["id_token"] = idt.clone();
        }
    }
    let _g = store_lock().lock();
    let mut store = load_store();
    let acc = upsert_account(
        &mut store,
        creds,
        if email.is_empty() { None } else { Some(email.as_str()) },
    );
    let id = acc["id"].as_str().unwrap_or("").to_string();
    if let Some(i) = find_idx(&store, &id) {
        refresh_quota(&mut store["accounts"][i]);
    }
    save_store(&store);
    Ok(if email.is_empty() { "账号已添加".into() } else { email })
}

// ============================ 额度自动刷新 =================================

static AUTO_QUOTA_BUSY: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// 刷新"当前使用中"账号的额度并写回账号库（后台线程用）
fn refresh_current_quota() -> Result<(), String> {
    let store = load_store();
    let Some(id) = store.get("current_id").and_then(|v| v.as_str()).map(String::from) else {
        return Ok(());
    };
    let Some(idx) = find_idx(&store, &id) else { return Ok(()) };
    let mut acc = store["accounts"][idx].clone();
    refresh_quota(&mut acc);
    // 网络调用期间不持锁，回写前重读避免覆盖其他请求的变更
    let mut store = load_store();
    if let Some(i) = find_idx(&store, &id) {
        store["accounts"][i]["quota"] = acc["quota"].clone();
        save_store(&store);
    }
    Ok(())
}

fn auto_quota_thread() {
    loop {
        let mins = load_prefs()
            .get("quota_refresh_min")
            .and_then(|v| v.as_i64())
            .unwrap_or(5);
        if mins <= 0 {
            std::thread::sleep(Duration::from_secs(60)); // 关闭状态：每分钟看一眼设置有没有变
            continue;
        }
        if !AUTO_QUOTA_BUSY.swap(true, std::sync::atomic::Ordering::SeqCst) {
            let _ = refresh_current_quota();
            AUTO_QUOTA_BUSY.store(false, std::sync::atomic::Ordering::SeqCst);
        }
        std::thread::sleep(Duration::from_secs((mins * 60) as u64));
    }
}

// ============================ HTTP 路由 ====================================

const INDEX_HTML: &str = include_str!("../../web/index.html");

fn json_resp(code: u16, v: &Value) -> (u16, String) {
    (code, serde_json::to_string(v).unwrap_or_default())
}

fn err_resp(code: u16, msg: &str) -> (u16, String) {
    json_resp(code, &json!({"error": msg}))
}

fn route(method: &Method, path: &str, url: &str, body: &Value) -> (u16, String) {
    // 注意：这里不能持 store_lock 全程——ensure_token 内部也会拿锁（刷新落盘），
    // 持锁重入会死锁。各分支自己保证 load→save 的窗口尽量短。
    match (method, path) {
        // ---------- 页面 ----------
        (Method::Get, "/" | "/index.html") => (200, INDEX_HTML.to_string()),
        // ---------- 唤醒 ----------
        (Method::Post | Method::Get, "/api/show") => {
            if let Some(app) = APP_HANDLE.get() {
                show_main_window(app);
            }
            json_resp(200, &json!({"ok": true}))
        },
        // ---------- 查询 ----------
        (Method::Get, "/api/state") => json_resp(200, &state_view()),
        (Method::Get, "/api/quota") => {
            let query = url.split_once('?').map(|(_, q)| q).unwrap_or("");
            let id = parse_query(query).get("id").cloned().unwrap_or_default();
            let mut store = load_store();
            match find_idx(&store, &id) {
                Some(i) => {
                    refresh_quota(&mut store["accounts"][i]);
                    save_store(&store);
                    let cur = store["current_id"].as_str().unwrap_or("");
                    json_resp(200, &account_view(&store["accounts"][i], cur))
                }
                None => err_resp(404, "账号不存在"),
            }
        }
        // ---------- 账号操作 ----------
        (Method::Post, "/api/add") => {
            // 优先收编 IDE 正在使用的账号（keyring 正源），无则退回 CLI 文件层
            let creds = match keyring_read_blob().and_then(|b| ide_creds_from_blob(&b)) {
                Some(c) => Some(c),
                None => read_live_creds(),
            };
            let Some(creds) = creds else {
                return err_resp(400, "未找到当前登录凭据（IDE 未登录且无 CLI 凭据文件）");
            };
            let label = body.get("label").and_then(|v| v.as_str()).unwrap_or("");
            let mut store = load_store();
            let acc = upsert_account(&mut store, creds, Some(label));
            let id = acc["id"].as_str().unwrap_or("").to_string();
            if let Some(i) = find_idx(&store, &id) {
                refresh_quota(&mut store["accounts"][i]);
            }
            store["current_id"] = json!(id);
            save_store(&store);
            refresh_tray();
            let cur = store["current_id"].as_str().unwrap_or("");
            json_resp(200, &account_view(&store["accounts"][find_idx(&store, &id).unwrap()], cur))
        }
        (Method::Post, "/api/import") => {
            let creds = match body.get("creds") {
                Some(Value::String(s)) => match serde_json::from_str::<Value>(s) {
                    Ok(v) => v,
                    Err(_) => return err_resp(400, "凭据不是合法 JSON"),
                },
                Some(v @ Value::Object(_)) => v.clone(),
                _ => return err_resp(400, "凭据需包含 refresh_token"),
            };
            if creds.get("refresh_token").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
                return err_resp(400, "凭据需包含 refresh_token");
            }
            let mut creds = creds;
            if creds.get("email").and_then(|e| e.as_str()).unwrap_or("").is_empty() {
                if let Some(at) = creds.get("access_token").and_then(|v| v.as_str()) {
                    if let Some(em) = fetch_email(at) {
                        creds["email"] = json!(em);
                    }
                }
            }
            let label = body.get("label").and_then(|v| v.as_str()).unwrap_or("");
            let mut store = load_store();
            let acc = upsert_account(&mut store, creds, Some(label));
            let id = acc["id"].as_str().unwrap_or("").to_string();
            if let Some(i) = find_idx(&store, &id) {
                refresh_quota(&mut store["accounts"][i]);
            }
            save_store(&store);
            refresh_tray();
            let cur = store["current_id"].as_str().unwrap_or("");
            json_resp(200, &account_view(&store["accounts"][find_idx(&store, &id).unwrap()], cur))
        }
        (Method::Post, "/api/switch") => {
            let id = body.get("id").and_then(|v| v.as_str()).unwrap_or("");
            match do_switch_by_id(id) {
                Ok(prev) => json_resp(
                    200,
                    &json!({"ok": true, "prev_email": prev, "restart_needed": ide_running()}),
                ),
                Err(e) => err_resp(500, &e),
            }
        }
        (Method::Post, "/api/delete") => {
            let id = body.get("id").and_then(|v| v.as_str()).unwrap_or("");
            let mut store = load_store();
            let Some(idx) = find_idx(&store, id) else {
                return err_resp(404, "账号不存在");
            };
            store["accounts"].as_array_mut().unwrap().remove(idx);
            if store["current_id"].as_str() == Some(id) {
                store["current_id"] = Value::Null;
            }
            save_store(&store);
            refresh_tray();
            json_resp(200, &json!({"ok": true}))
        }
        (Method::Post, "/api/export") => {
            let id = body.get("id").and_then(|v| v.as_str()).unwrap_or("");
            let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("").trim();
            let store = load_store();
            let Some(idx) = find_idx(&store, id) else {
                return err_resp(404, "账号不存在");
            };
            let creds = clean_creds(&store["accounts"][idx]["creds"]);
            let content = match serde_json::to_string_pretty(&creds) {
                Ok(c) => c,
                Err(e) => return err_resp(500, &e.to_string()),
            };
            if path.is_empty() {
                // 无路径（网页版）：返回内容由前端触发下载
                return json_resp(200, &json!({"ok": true, "content": content}));
            }
            if !path.to_lowercase().ends_with(".json") {
                return err_resp(400, "导出路径需以 .json 结尾");
            }
            if let Err(e) = std::fs::write(path, &content) {
                return err_resp(500, &format!("写入失败: {e}"));
            }
            json_resp(200, &json!({"ok": true, "path": path}))
        }
        (Method::Get, "/api/prefs") => json_resp(200, &load_prefs()),
        (Method::Post, "/api/prefs") => {
            if let Some(t) = body.get("theme").and_then(|v| v.as_str()) {
                if t == "light" || t == "dark" {
                    save_prefs_key("theme", json!(t));
                }
            }
            if let Some(v) = body.get("close_to_tray").and_then(|v| v.as_bool()) {
                save_prefs_key("close_to_tray", json!(v));
            }
            if let Some(v) = body.get("quota_refresh_min").and_then(|v| v.as_i64()) {
                if (0..=60).contains(&v) {
                    save_prefs_key("quota_refresh_min", json!(v));
                }
            }
            if let Some(l) = body.get("lang").and_then(|v| v.as_str()) {
                if l == "zh" || l == "en" {
                    save_prefs_key("lang", json!(l));
                }
            }
            if let Some(p) = body.get("ide_path").and_then(|v| v.as_str()) {
                save_prefs_key("ide_path", json!(p.trim()));
            }
            json_resp(200, &json!({"ok": true}))
        }
        (Method::Post, "/api/rename") => {
            let id = body.get("id").and_then(|v| v.as_str()).unwrap_or("");
            let label = body.get("label").and_then(|v| v.as_str()).unwrap_or("").trim();
            let mut store = load_store();
            let Some(idx) = find_idx(&store, id) else {
                return err_resp(404, "账号不存在");
            };
            if !label.is_empty() {
                store["accounts"][idx]["label"] = json!(label);
                save_store(&store);
                refresh_tray();
            }
            let cur = store["current_id"].as_str().unwrap_or("");
            json_resp(200, &account_view(&store["accounts"][idx], cur))
        }
        (Method::Post, "/api/refresh_all") => {
            let mut store = load_store();
            if let Some(arr) = store["accounts"].as_array_mut() {
                for acc in arr.iter_mut() {
                    refresh_quota(acc);
                }
            }
            save_store(&store);
            json_resp(200, &state_view())
        }
        (Method::Post, "/api/oauth_start") => match oauth_start() {
            Ok(url) => json_resp(200, &json!({"ok": true, "auth_url": url})),
            Err(e) => err_resp(500, &e),
        },
        (Method::Post, "/api/start_ide") => {
            let (ok, message) = start_ide();
            json_resp(200, &json!({"ok": ok, "message": message}))
        }
        (Method::Post, "/api/stop_ide") => {
            let (ok, message) = stop_ide();
            json_resp(200, &json!({"ok": ok, "message": message}))
        }
        (Method::Post, "/api/restart_ide") => {
            let (ok, message) = restart_ide();
            json_resp(200, &json!({"ok": ok, "message": message}))
        }
        (Method::Post | Method::Get, "/api/browse_ide") => {
            if let Some(path) = browse_ide_exe() {
                json_resp(200, &json!({"ok": true, "path": path}))
            } else {
                json_resp(200, &json!({"ok": false, "cancelled": true}))
            }
        }
        _ => err_resp(404, "not found"),
    }
}

fn handle_request(mut req: tiny_http::Request) {
    let method = req.method().clone();
    let url = req.url().to_string();
    let path = url.split('?').next().unwrap_or("").to_string();
    let mut body = Value::Null;
    if method == Method::Post {
        let mut s = String::new();
        let _ = req.as_reader().read_to_string(&mut s);
        body = serde_json::from_str(&s).unwrap_or(Value::Null);
    }
    let (code, payload) = route(&method, &path, &url, &body);
    let ctype = if path.starts_with("/api") {
        "application/json; charset=utf-8"
    } else {
        "text/html; charset=utf-8"
    };
    let resp = Response::from_string(payload)
        .with_status_code(code)
        .with_header(Header::from_bytes(&b"Content-Type"[..], ctype.as_bytes()).unwrap())
        .with_header(Header::from_bytes(&b"Cache-Control"[..], &b"no-store"[..]).unwrap());
    let _ = req.respond(resp);
}

// ============================ 启动 ========================================

fn bootstrap_first_account() {
    let mut store = load_store();
    let empty = store["accounts"].as_array().map(|a| a.is_empty()).unwrap_or(true);
    let mut ide_id: Option<Value> = None;
    let mut dirty = false;
    // IDE 正源账号按 refresh_token 对齐：缺席则收编，已在库中则原位更新凭据、补邮箱
    if let Some(creds) = ide_current_creds() {
        let rt = creds["refresh_token"].as_str().unwrap_or("").to_string();
        match store_find_by_refresh(&store, &rt) {
            Some(i) => {
                store["accounts"][i]["creds"] = creds.clone();
                let cur_email = store["accounts"][i]
                    .get("email")
                    .and_then(|e| e.as_str())
                    .unwrap_or("");
                if cur_email.is_empty() {
                    if let Some(e) = creds.get("email").and_then(|e| e.as_str()) {
                        store["accounts"][i]["email"] = json!(e);
                    }
                }
                if store["current_id"].is_null() {
                    ide_id = store["accounts"][i].get("id").cloned();
                }
                dirty = true;
            }
            None => {
                let acc = upsert_account(&mut store, creds, None);
                ide_id = Some(acc["id"].clone());
                dirty = true;
            }
        }
    }
    let empty_after = store["accounts"].as_array().map(|a| a.is_empty()).unwrap_or(true);
    if empty_after {
        if let Some(creds) = read_live_creds() {
            upsert_account(&mut store, creds, None);
            dirty = true;
        }
    }
    // 空库或首次对齐 IDE 账号时，把当前账号指向 IDE 登录
    if empty || (ide_id.is_some() && store["current_id"].is_null()) {
        let target = ide_id.or_else(|| {
            store["accounts"]
                .as_array()
                .and_then(|a| a.first())
                .and_then(|x| x.get("id").cloned())
        });
        if let Some(id) = target {
            store["current_id"] = id;
        }
        dirty = true;
    }
    if dirty {
        save_store(&store);
    }
}

fn is_agy_server(port: u16) -> bool {
    http_client()
        .get(format!("http://127.0.0.1:{port}/api/state"))
        .timeout(Duration::from_secs(2))
        .send()
        .ok()
        .and_then(|r| r.json::<Value>().ok())
        .map(|v| v.get("accounts").is_some())
        .unwrap_or(false)
}

fn main() {
    app_log("=== AGY·SWITCH STARTUP ===");
    let _ = std::fs::create_dir_all(store_file().parent().unwrap());
    bootstrap_first_account();

    // 单实例检测：若已有 AGY·SWITCH 在运行（且驻留托盘/后台），唤醒其主窗口并直接退出，避免冲突
    if is_agy_server(8791) {
        app_log("Instance already running on 8791, calling /api/show to focus window");
        let _ = http_client().post("http://127.0.0.1:8791/api/show").send();
        app_log("Wake request sent, exiting second instance.");
        return;
    }

    // 绑定本地服务端口（8791-8800 取第一个可用者）
    let mut server_url: Option<String> = None;
    for p in 8791..=8800 {
        match Server::http(("127.0.0.1", p)) {
            Ok(server) => {
                let server = Arc::new(server);
                for _ in 0..4 {
                    let s = server.clone();
                    std::thread::spawn(move || loop {
                        match s.recv() {
                            Ok(r) => handle_request(r),
                            Err(_) => break,
                        }
                    });
                }
                server_url = Some(format!("http://127.0.0.1:{p}/"));
                break;
            }
            Err(_) => continue,
        }
    }
    let server_url = match server_url {
        Some(u) => u,
        None => {
            app_log("ERROR: Ports 8791-8800 are all unavailable!");
            eprintln!("[agy] 端口 8791-8800 均不可用");
            return;
        }
    };
    app_log(&format!("Server bound to {server_url}"));
    println!("[agy] AGY·SWITCH v{APP_VERSION} — {server_url}");

    // 后台自动刷新"当前使用中"账号的额度（间隔见设置 quota_refresh_min，默认 5 分钟）
    std::thread::spawn(auto_quota_thread);

    // 偏好：主题 + 上次窗口尺寸/位置
    let prefs = load_prefs();
    let dark = prefs.get("theme").and_then(|v| v.as_str()) == Some("dark");
    let win_prefs = prefs.get("win").cloned().unwrap_or(json!({}));
    let (w, h) = (
        win_prefs.get("w").and_then(|v| v.as_f64()).unwrap_or(410.0).clamp(360.0, 3840.0),
        win_prefs.get("h").and_then(|v| v.as_f64()).unwrap_or(800.0).clamp(520.0, 2160.0),
    );
    let pos: Option<(f64, f64)> = match (
        win_prefs.get("x").and_then(|v| v.as_f64()),
        win_prefs.get("y").and_then(|v| v.as_f64()),
    ) {
        (Some(x), Some(y)) => Some((x, y)),
        _ => None,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, None))
        .setup(move |app| {
            app_log("Tauri setup hook started");
            let _ = APP_HANDLE.set(app.handle().clone());
            let url = tauri::Url::parse(&server_url).expect("server url");
            let mut builder = tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::External(url))
                .title("AGY·SWITCH")
                .inner_size(w, h)
                .min_inner_size(360.0, 520.0)
                .visible(true)
                .theme(Some(if dark { tauri::Theme::Dark } else { tauri::Theme::Light }));
            if let Some((x, y)) = pos {
                if x >= 0.0 && y >= 0.0 {
                    builder = builder.position(x, y);
                } else {
                    builder = builder.center();
                }
            } else {
                builder = builder.center();
            }
            app_log("Calling builder.build()...");
            let win = match builder.build() {
                Ok(w) => {
                    app_log("builder.build() succeeded!");
                    w
                }
                Err(e) => {
                    app_log(&format!("ERROR: builder.build() failed: {e}"));
                    return Err(e.into());
                }
            };
            let _ = win.show();
            let _ = win.unminimize();
            let _ = win.set_focus();
            app_log("Window show/unminimize/set_focus completed");
            // 托盘：构建时直接挂菜单（账号快捷切换子菜单），右键弹出
            let tray_menu = build_tray_menu(app.handle())?;
            let _ = tauri::tray::TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("AGY·SWITCH")
                .menu(&tray_menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| {
                    let id = event.id().as_ref().to_string();
                    match id.as_str() {
                        "show" => show_main_window(app),
                        "quit" => app.exit(0),
                        "start_ide" => { let _ = start_ide(); }
                        "stop_ide" => { let _ = stop_ide(); }
                        "restart_ide" => { let _ = restart_ide(); }
                        _ => {
                            if let Some(acc_id) = id.strip_prefix("acc:") {
                                let _ = do_switch_by_id(acc_id);
                            }
                        }
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show_main_window(tray.app_handle());
                    }
                })
                .build(app.handle())?;
            // 关窗：记忆尺寸/位置；开启"驻留托盘"则隐藏而非退出
            let w2 = win.clone();
            win.on_window_event(move |e| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = e {
                    if let (Ok(sz), Ok(pos), Ok(sf)) = (w2.inner_size(), w2.outer_position(), w2.scale_factor()) {
                        let lg = sz.to_logical::<f64>(sf);
                        let pg = pos.to_logical::<f64>(sf);
                        save_prefs_key("win", json!({"w": lg.width, "h": lg.height, "x": pg.x, "y": pg.y}));
                    }
                    if load_prefs().get("close_to_tray").and_then(|v| v.as_bool()).unwrap_or(false) {
                        api.prevent_close();
                        let _ = w2.hide();
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod oauth_config_tests {
    use super::*;

    #[test]
    fn configuration_preserves_order_and_requires_complete_pairs() {
        let config = json!({
            "antigravity": {"client_id": "fake-a", "client_secret": "fake-secret-a"},
            "gemini-cli": {"client_id": "fake-g", "client_secret": "fake-secret-g"}
        });
        let clients = select_oauth_clients(&config, |_| None);
        assert_eq!(clients.iter().map(|c| c.key).collect::<Vec<_>>(), vec!["antigravity", "gemini-cli"]);
        let clients = select_oauth_clients(&config, |name| match name {
            "AGY_ANTIGRAVITY_CLIENT_SECRET" => Some(" ".into()),
            "AGY_GEMINI_CLIENT_ID" => Some(" fake-override ".into()),
            "AGY_GEMINI_CLIENT_SECRET" => Some("fake-override-secret".into()),
            _ => None,
        });
        assert_eq!(clients.len(), 1);
        assert_eq!(clients[0].key, "gemini-cli");
        assert_eq!(clients[0].client_id, "fake-override");
        assert!(select_oauth_clients(&Value::Null, |_| None).is_empty());
        let partial = select_oauth_clients(&config, |name| {
            (name == "AGY_ANTIGRAVITY_CLIENT_ID").then(|| "fake-partial".into())
        });
        assert_eq!(partial.len(), 1);
        assert_eq!(partial[0].key, "gemini-cli");
        assert!(select_login_client(&partial).is_err());
        let complete = select_oauth_clients(&config, |_| None);
        assert_eq!(select_login_client(&complete).unwrap().key, "antigravity");
    }
}
