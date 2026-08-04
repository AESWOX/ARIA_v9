#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use std::env;
use std::fs;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, State};
use uuid::Uuid;

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeBootstrap {
    backend_base_url: String,
    runtime_token: String,
    pin_required: bool,
    idle_lock_minutes: u32,
}

struct SidecarRuntime {
    child: Option<Child>,
    bootstrap: RuntimeBootstrap,
}

struct AppState {
    sidecar: Mutex<SidecarRuntime>,
    init_error: Mutex<Option<String>>,
}

struct SpawnedSidecar {
    child: Option<Child>,
    bootstrap: RuntimeBootstrap,
}

/// Bind to an ephemeral port, learn it, then release — caller must use it before
/// the OS recycles it (typically 60s on Windows; plenty for our spawn window).
fn find_free_port() -> Result<u16, String> {
    TcpListener::bind("127.0.0.1:0")
        .map_err(|err| format!("Failed to bind ephemeral port: {err}"))?
        .local_addr()
        .map(|addr| addr.port())
        .map_err(|err| format!("Failed to resolve ephemeral port: {err}"))
}

/// Path to the PID file that records the sidecar process id.
fn pid_file_path(app: &AppHandle) -> PathBuf {
    let mut base = app
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from("."));
    fs::create_dir_all(&base).ok();
    base.push("sidecar.pid");
    base
}

/// Kill any process whose PID is recorded in `pid_path` and still alive.
fn cleanup_zombie(pid_path: &PathBuf) {
    let pid_str = match fs::read_to_string(pid_path) {
        Ok(s) => s.trim().to_string(),
        Err(_) => return,
    };
    let pid: u32 = match pid_str.parse() {
        Ok(n) => n,
        Err(_) => {
            let _ = fs::remove_file(pid_path);
            return;
        }
    };

    // On Windows, try to kill by PID via taskkill
    let _ = Command::new("taskkill")
        .args(["/f", "/pid", &pid.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();

    std::thread::sleep(Duration::from_millis(200));
    let _ = fs::remove_file(pid_path);
}

/// Try to kill any process listening on `port` by parsing `netstat` output.
fn kill_process_on_port(port: u16) {
    if cfg!(windows) {
        let output = Command::new("netstat")
            .args(["-ano"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
            .ok();
        if let Some(out) = output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            let needle = format!(":{}", port);
            for line in stdout.lines() {
                if line.contains("LISTENING") && line.contains(&needle) {
                    // Last column is PID
                    let pid = line.split_whitespace().last().unwrap_or("");
                    let _ = Command::new("taskkill")
                        .args(["/f", "/pid", pid])
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .status();
                }
            }
        }
    }
}

fn spawn_sidecar(app: &AppHandle) -> Result<SpawnedSidecar, String> {
    // 1. Kill any zombie sidecar from previous run
    let pid_path = pid_file_path(app);
    cleanup_zombie(&pid_path);

    // 2. Find a free port
    let port = find_free_port().unwrap_or(8765_u16);

    // 3. If fallback port is in use, kill the offender
    if port == 8765_u16 {
        // Only do this for the fallback — dynamic ports are always free
        std::thread::sleep(Duration::from_millis(50));
        kill_process_on_port(port);
        std::thread::sleep(Duration::from_millis(200));
    }

    let runtime_token = Uuid::new_v4().to_string();
    let idle_lock_minutes = env::var("LOCAL_AGENT_IDLE_LOCK_MINUTES")
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or(15);

    let backend_base_url = format!("http://127.0.0.1:{port}");
    let bootstrap = RuntimeBootstrap {
        backend_base_url: backend_base_url.clone(),
        runtime_token: runtime_token.clone(),
        pin_required: true,
        idle_lock_minutes,
    };

    // 4. Resolve backend entry
    let entry = resolve_backend_entry(app)?;
    let mut command = if entry
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| value.eq_ignore_ascii_case("py"))
        .unwrap_or(false)
    {
        let python = env::var("LOCAL_AGENT_BACKEND_PYTHON")
            .unwrap_or_else(|_| "python".to_string());
        let mut command = Command::new(python);
        command.arg(&entry);
        if let Some(parent) = entry.parent() {
            command.current_dir(parent);
        }
        command
    } else {
        let mut command = Command::new(&entry);
        if let Some(parent) = entry.parent() {
            command.current_dir(parent);
        }
        command
    };

    let child = match command
        .arg("--port")
        .arg(port.to_string())
        .env("HTTP_HOST", "127.0.0.1")
        .env("HTTP_PORT", port.to_string())
        .env("LOCAL_AGENT_RUNTIME_TOKEN", &runtime_token)
        .env("LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
    {
        Ok(child) => {
            eprintln!("[ARIA] Backend sidecar started on port {port}");
            Some(child)
        }
        Err(err) => {
            eprintln!("[ARIA] Backend sidecar not started (already running?): {err}");
            None
        }
    };

    // 5. Write PID file so we can clean up on next start
    if let Some(ref c) = child {
        let _ = fs::write(&pid_path, c.id().to_string());
    }

    // 6. Health-check: poll /status up to 30 times (200ms intervals = 6s total)
    if child.is_some() {
        let client = Client::builder()
            .timeout(Duration::from_millis(500))
            .build()
            .map_err(|e| format!("Failed to build HTTP client: {e}"))?;

        let mut last_err = String::from("timeout");
        let health_url = format!("{backend_base_url}/status");
        let mut success = false;

        for i in 0..30 {
            match client.get(&health_url).send() {
                Ok(resp) if resp.status().is_success() => {
                    eprintln!("[ARIA] Backend health-check OK (attempt {})", i + 1);
                    success = true;
                    break;
                }
                Ok(resp) => {
                    last_err = format!("HTTP {}", resp.status());
                }
                Err(e) => {
                    last_err = e.to_string();
                }
            }
            std::thread::sleep(Duration::from_millis(200));
        }

        if !success {
            eprintln!("[ARIA] Backend health-check FAILED after 30 attempts: {last_err}");
            // Kill the sidecar since it's not usable
            if let Some(mut c) = child {
                let _ = c.kill();
                let _ = c.wait();
            }
            return Err(format!(
                "Backend did not become healthy within 6 seconds (last error: {last_err}). \
                 Check logs for details. Click 'Retry' to try again."
            ));
        }
    }

    Ok(SpawnedSidecar { child, bootstrap })
}

fn resource_candidates(app: &AppHandle) -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(explicit) = env::var("LOCAL_AGENT_BACKEND_EXE") {
        candidates.push(PathBuf::from(explicit));
    }

    if let Ok(resource_dir) = app.path().resource_dir() {
        // Tauri 2 copies externalBin into resources. Depending on the build
        // stage it keeps the target-triple suffix (…-x86_64-pc-windows-msvc.exe)
        // or strips it (backend.exe) — accept both names.
        candidates.push(resource_dir.join("backend.exe"));
        candidates.push(resource_dir.join("backend-x86_64-pc-windows-msvc.exe"));
        candidates.push(resource_dir.join("backend").join("backend.exe"));
        candidates.push(resource_dir.join("backend").join("backend-x86_64-pc-windows-msvc.exe"));
        candidates.push(resource_dir.join("backend").join("run_backend.py"));
    }

    if let Ok(current_dir) = env::current_dir() {
        candidates.push(current_dir.join("backend.exe"));
        candidates.push(current_dir.join("backend-x86_64-pc-windows-msvc.exe"));
        candidates.push(current_dir.join("backend").join("backend.exe"));
        candidates.push(current_dir.join("backend").join("backend-x86_64-pc-windows-msvc.exe"));
        candidates.push(current_dir.join("backend").join("run_backend.py"));
        candidates.push(current_dir.join(r"..\backend\backend.exe"));
        candidates.push(current_dir.join(r"..\backend\backend-x86_64-pc-windows-msvc.exe"));
        candidates.push(current_dir.join(r"..\backend\run_backend.py"));
    }

    candidates
}

fn resolve_backend_entry(app: &AppHandle) -> Result<PathBuf, String> {
    resource_candidates(app)
        .into_iter()
        .find(|candidate| candidate.exists())
        .ok_or_else(|| "Unable to resolve backend sidecar executable or dev script".to_string())
}

fn graceful_shutdown(runtime: &mut SidecarRuntime) {
    let Some(child) = runtime.child.as_mut() else {
        return;
    };

    let shutdown_url = format!("{}/system/shutdown", runtime.bootstrap.backend_base_url);
    let _ = Client::builder()
        .timeout(Duration::from_millis(700))
        .build()
        .and_then(|client| {
            client
                .post(&shutdown_url)
                .header("X-Local-Agent-Token", &runtime.bootstrap.runtime_token)
                .send()
        });

    std::thread::sleep(Duration::from_millis(650));

    match child.try_wait() {
        Ok(Some(_)) => {}
        _ => {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    runtime.child = None;
}

#[tauri::command]
fn get_init_error(state: State<'_, AppState>) -> Option<String> {
    state.init_error.lock().ok().and_then(|g| g.clone())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Second instance launched — focus the existing window
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let init_error: Option<String>;
            let spawned = match spawn_sidecar(&app.handle()) {
                Ok(s) => {
                    init_error = None;
                    s
                }
                Err(e) => {
                    eprintln!("[ARIA] Init error: {e}");
                    init_error = Some(e);
                    // Return dummy bootstrap — frontend will check get_init_error
                    SpawnedSidecar {
                        child: None,
                        bootstrap: RuntimeBootstrap {
                            backend_base_url: "http://127.0.0.1:8765".into(),
                            runtime_token: String::new(),
                            pin_required: false,
                            idle_lock_minutes: 15,
                        },
                    }
                }
            };

            app.manage(AppState {
                sidecar: Mutex::new(SidecarRuntime {
                    child: spawned.child,
                    bootstrap: spawned.bootstrap.clone(),
                }),
                init_error: Mutex::new(init_error),
            });

            // Same-origin SPA mode: navigate the webview to the backend itself,
            // which serves desktop/dist + embeds the runtime token into the HTML.
            // The API and the page then share one origin — no CORS, no invoke
            // bridge for auth.
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(url) = tauri::Url::parse(&spawned.bootstrap.backend_base_url) {
                    let _ = window.navigate(url);
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_init_error])
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::ExitRequested { .. } = event {
            let state = app_handle.state::<AppState>();
            let mut guard = match state.sidecar.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            graceful_shutdown(&mut guard);
        }
    });
}
