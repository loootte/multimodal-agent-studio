//! MAS 桌面壳（13.A / #74）：启动 Python sidecar，窗口加载带令牌的本机页面，关窗停服务。
use serde::Deserialize;
use std::io::{BufRead, BufReader};
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

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
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
            let info: ReadyInfo = serde_json::from_str(rest)
                .map_err(|e| format!("解析 MAS_READY 失败：{e}; {rest}"))?;
            let token = info
                .url
                .split("desktop_token=")
                .nth(1)
                .unwrap_or("")
                .split('&')
                .next()
                .unwrap_or("")
                .to_string();
            if token.is_empty() {
                let _ = child.kill();
                return Err("MAS_READY 里没有 desktop_token".into());
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

fn http_stop(base: &str, token: &str) {
    let script = format!(
        "import urllib.request\n\
req=urllib.request.Request('{base}/api/desktop/stop', data=b'{{}}', method='POST',\n\
headers={{'X-MAS-Desktop-Token':'{token}','Content-Type':'application/json'}})\n\
urllib.request.urlopen(req, timeout=2)\n",
        base = base.trim_end_matches('/'),
        token = token.replace('\\', "\\\\").replace('\'', "\\'"),
    );
    let _ = Command::new(python_bin())
        .args(["-c", &script])
        .status();
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
    let win = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
        .title("Multimodal Agent Studio")
        .inner_size(1280.0, 860.0)
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
