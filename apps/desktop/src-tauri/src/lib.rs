//! MAS 桌面壳（13.A / #74）：启动 Python sidecar，窗口加载带令牌的本机页面，关窗停服务。
//! 13.D（#77）：WebView 只许导航到 sidecar 同源地址（其余拦下并记 security.desktop_navigation_blocked）；
//! release 构建关 devtools；IPC 只有 desktop_info，capabilities 不开任何插件权限。
use serde::Deserialize;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct HostState {
    child: Mutex<Option<Child>>,
    base: Mutex<Option<String>>,
    token: Mutex<Option<String>>,
    server_pid: Mutex<Option<u32>>,
}

#[derive(Debug, Deserialize)]
struct ReadyInfo {
    url: String,
    base: String,
    #[allow(dead_code)]
    port: u16,
    server_pid: u32,
}

fn looks_like_repo(root: &Path) -> bool {
    root.join("apps/desktop/host.py").is_file() && root.join("apps/web/server.py").is_file()
}

fn repo_root() -> PathBuf {
    if let Ok(v) = std::env::var("MAS_REPO_ROOT") {
        let p = PathBuf::from(v);
        if looks_like_repo(&p) {
            return p;
        }
    }
    // 从 exe / cwd 向上找仓库根（dist、target/release 下双击也能用）
    let mut seeds: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            seeds.push(parent.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        seeds.push(cwd);
    }
    seeds.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")),
    );
    for seed in seeds {
        let mut cur = Some(seed);
        for _ in 0..10 {
            let Some(dir) = cur else { break };
            if looks_like_repo(&dir) {
                return dir;
            }
            cur = dir.parent().map(|p| p.to_path_buf());
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn python_bin() -> String {
    std::env::var("MAS_PYTHON").unwrap_or_else(|_| {
        if cfg!(windows) {
            "python".into()
        } else {
            "python3".into()
        }
    })
}

fn spawn_host(root: &Path) -> Result<(Child, ReadyInfo, String), String> {
    let host = root.join("apps/desktop/host.py");
    if !host.is_file() {
        return Err(format!("找不到宿主脚本：{}", host.display()));
    }
    let mut child = Command::new(python_bin())
        .arg(&host)
        .current_dir(root)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("启动 sidecar 失败：{e}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "sidecar 没有 stdout".to_string())?;
    let reader = BufReader::new(stdout);
    let deadline = Instant::now() + Duration::from_secs(45);
    for line in reader.lines() {
        let line = line.map_err(|e| format!("读 sidecar 输出失败：{e}"))?;
        if let Some(rest) = line.strip_prefix("MAS_READY ") {
            // server.py 也会打一行不完整的 MAS_READY；缺 base 就跳过
            let Ok(info) = serde_json::from_str::<ReadyInfo>(rest) else {
                continue;
            };
            let token = info
                .url
                .split("desktop_token=")
                .nth(1)
                .unwrap_or("")
                .split('&')
                .next()
                .unwrap_or("")
                .to_string();
            if token.is_empty() || info.base.is_empty() {
                continue;
            }
            return Ok((child, info, token));
        }
        if Instant::now() > deadline {
            break;
        }
    }
    let _ = child.kill();
    Err("sidecar 在超时前没有输出 MAS_READY".into())
}

/// 直接向本机 sidecar 发 POST（令牌只在请求头里，不进任何子进程命令行）。
fn post_local(port: u16, token: &str, path: &str, body: &str) {
    if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
        let req = format!(
            "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\
X-MAS-Desktop-Token: {token}\r\nContent-Type: application/json\r\nContent-Length: {len}\r\n\
Connection: close\r\n\r\n{body}",
            len = body.len()
        );
        let _ = stream.write_all(req.as_bytes());
        let mut sink = Vec::new();
        let _ = stream.read_to_end(&mut sink);
    }
}

fn http_stop(base: &str, token: &str) {
    let Ok(url) = base.parse::<tauri::Url>() else { return };
    let Some(port) = url.port_or_known_default() else { return };
    post_local(port, token, "/api/desktop/stop", "{}");
}

fn force_kill_pid(pid: u32) {
    if pid == 0 {
        return;
    }
    if cfg!(windows) {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    } else {
        let _ = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status();
        std::thread::sleep(Duration::from_millis(200));
        let _ = Command::new("kill")
            .args(["-KILL", &pid.to_string()])
            .status();
    }
}

fn shutdown_host(state: &HostState) {
    let base = state.base.lock().ok().and_then(|mut g| g.take());
    let token = state.token.lock().ok().and_then(|mut g| g.take());
    let server_pid = state.server_pid.lock().ok().and_then(|mut g| g.take());
    if let (Some(base), Some(token)) = (base, token) {
        http_stop(&base, &token);
    }
    if let Some(pid) = server_pid {
        force_kill_pid(pid);
    }
    if let Ok(mut guard) = state.child.lock() {
        if let Some(mut child) = guard.take() {
            let pid = child.id();
            force_kill_pid(pid);
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// 只认 sidecar 同源：http + 127.0.0.1 + 同一端口。about:blank 放行，其余一律拒绝。
fn navigation_allowed(url: &tauri::Url, base: &tauri::Url) -> bool {
    if url.as_str() == "about:blank" {
        return true;
    }
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && base.host_str() == Some("127.0.0.1")
        && url.port_or_known_default() == base.port_or_known_default()
}

/// 安全审计交给 sidecar 写遥测（只发事件名 + 原因码 + scheme + 主机名，不发完整地址）。
fn audit_navigation_blocked(base: &tauri::Url, token: &str, url: &tauri::Url) {
    let host = url
        .host_str()
        .unwrap_or("")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '[' | ']'))
        .take(255)
        .collect::<String>();
    let scheme = url
        .scheme()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .take(16)
        .collect::<String>();
    let body = serde_json::json!({
        "event": "security.desktop_navigation_blocked",
        "reason": "not_sidecar_origin",
        "scheme": scheme,
        "host": host,
    })
    .to_string();
    let Some(port) = base.port_or_known_default() else { return };
    let token = token.to_string();
    std::thread::spawn(move || post_local(port, &token, "/api/desktop/security_event", &body));
}

fn open_main_window(app: &AppHandle, url: &str) -> Result<(), String> {
    let parsed = url
        .parse()
        .map_err(|e| format!("sidecar URL 不合法：{e}"))?;
    if let Some(window) = app.get_webview_window("main") {
        window
            .navigate(WebviewUrl::External(parsed.clone()))
            .map_err(|e| format!("导航失败：{e}"))?;
        let _ = window.show();
        return Ok(());
    }
    let state = app.state::<HostState>();
    let base: tauri::Url = state
        .base
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_default()
        .parse()
        .map_err(|e| format!("sidecar 地址不合法：{e}"))?;
    let token = state.token.lock().ok().and_then(|g| g.clone()).unwrap_or_default();
    let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
        .title("Multimodal Agent Studio")
        .inner_size(1280.0, 860.0)
        // release 构建关 devtools（debug 构建留给开发）
        .devtools(cfg!(debug_assertions))
        .on_navigation(move |url| {
            if navigation_allowed(url, &base) {
                return true;
            }
            audit_navigation_blocked(&base, &token, url);
            false
        })
        .build()
        .map_err(|e| format!("创建窗口失败：{e}"))?;
    let _ = win.show();
    Ok(())
}

#[tauri::command]
fn desktop_info() -> String {
    "mas.desktop/1".into()
}

pub fn run() {
    let root = repo_root();
    tauri::Builder::default()
        .manage(HostState {
            child: Mutex::new(None),
            base: Mutex::new(None),
            token: Mutex::new(None),
            server_pid: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![desktop_info])
        .setup(move |app| {
            let (child, info, token) = spawn_host(&root).map_err(|e| {
                eprintln!("MAS desktop: {e}");
                e
            })?;
            {
                let state = app.state::<HostState>();
                *state.child.lock().unwrap() = Some(child);
                *state.base.lock().unwrap() = Some(info.base.clone());
                *state.token.lock().unwrap() = Some(token);
                *state.server_pid.lock().unwrap() = Some(info.server_pid);
            }
            open_main_window(&app.handle(), &info.url)?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                let state = app_handle.state::<HostState>();
                shutdown_host(state);
            }
        });
}
