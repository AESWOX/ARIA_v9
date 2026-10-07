#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use std::env;
use std::fs;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
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
    /// Raised when the app is exiting so the supervisor stops respawning.
    shutdown: Arc<AtomicBool>,
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

/// Launch plan resolved once at startup. The sidecar is always respawned on the
/// same port and with the same runtime token so the webview URL and the
/// frontend's already-issued token stay valid across restarts.
struct LaunchPlan {
    port: u16,
    runtime_token: String,
    idle_lock_minutes: u32,
}

fn make_launch_plan(app: &AppHandle) -> LaunchPlan {
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

    LaunchPlan {
        port,
        runtime_token,
        idle_lock_minutes,
    }
}

fn bootstrap_from_plan(plan: &LaunchPlan) -> RuntimeBootstrap {
    RuntimeBootstrap {
        backend_base_url: format!("http://127.0.0.1:{}", plan.port),
        runtime_token: plan.runtime_token.clone(),
        pin_required: true,
        idle_lock_minutes: plan.idle_lock_minutes,
    }
}

/// Spawn the backend sidecar and wait until `/status` is healthy.
/// Cold start (PyInstaller onefile extraction + DB migrations + seed) measures
/// ~12s on fast hosts and can take minutes on slow/fresh machines (Windows
/// Sandbox measured >45s), so the health budget is 180s (360 x 500ms).
/// On any failure the spawned process is killed and an Err is returned.
fn launch_backend(app: &AppHandle, plan: &LaunchPlan) -> Result<Child, String> {
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
        .arg(plan.port.to_string())
        .env("HTTP_HOST", "127.0.0.1")
        .env("HTTP_PORT", plan.port.to_string())
        .env("LOCAL_AGENT_RUNTIME_TOKEN", &plan.runtime_token)
        .env("LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
    {
        Ok(child) => {
            eprintln!("[ARIA] Backend sidecar started on port {}", plan.port);
            child
        }
        Err(err) => {
            return Err(format!("Backend sidecar not started: {err}"));
        }
    };

    // 5. Write PID file so we can clean up on next start
    let _ = fs::write(pid_file_path(app), child.id().to_string());

    // 6. Health-check: poll /status until healthy. A short budget (e.g. 6s) is a
    //    real-world bug: the packaged backend needs ~12s cold (onefile extraction +
    //    DB init) and up to a minute+ in Windows Sandbox, so 360 x 500ms = 180s.
    let client = Client::builder()
        .timeout(Duration::from_millis(500))
        .build()
        .map_err(|e| format!("Failed to build HTTP client: {e}"))?;

    let mut last_err = String::from("timeout");
    let health_url = format!("http://127.0.0.1:{}/status", plan.port);
    let mut success = false;
    let attempts = 360;

    for i in 0..attempts {
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
        std::thread::sleep(Duration::from_millis(500));
    }

    if !success {
        eprintln!("[ARIA] Backend health-check FAILED after {attempts} attempts: {last_err}");
        let mut child = child;
        let _ = child.kill();
        let _ = child.wait();
        return Err(format!(
            "Backend did not become healthy within {} seconds (last error: {last_err}). \
             Check logs for details.",
            attempts * 500 / 1000
        ));
    }

    Ok(child)
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

enum ChildStatus {
    Running,
    Exited,
    Missing,
}

/// Exponential backoff: 1s, 2s, 4s, 8s, 16s, … capped at `max`.
fn backoff_delay(attempt: u32, max: Duration) -> Duration {
    let seconds = 1u64 << attempt.min(10);
    let delay = Duration::from_secs(seconds);
    if delay > max {
        max
    } else {
        delay
    }
}

/// Monitor the backend sidecar in a background thread.
///
/// - Polls `try_wait()` every `MONITOR_INTERVAL`; if the child exited or was
///   never spawned, relaunch it via `launch_backend` with the SAME port and
///   runtime token (the webview URL and the frontend token stay valid).
/// - Delays restarts with exponential backoff so a crash storm doesn't hammer
///   the machine. The counter resets only after `STABLE_RESET_AFTER` of stable
///   uptime — a single crash after hours of work still restarts immediately.
/// - Reloads the webview after a successful respawn so the frontend reconnects.
/// - Exits when `AppState.shutdown` is raised (app exiting), so it never races
///   with `graceful_shutdown`.
fn spawn_supervisor(app: AppHandle, plan: LaunchPlan) {
    std::thread::spawn(move || {
        const MONITOR_INTERVAL: Duration = Duration::from_secs(2);
        const STABLE_RESET_AFTER: Duration = Duration::from_secs(60);
        const MAX_BACKOFF: Duration = Duration::from_secs(30);

        let mut backoff_attempt: u32 = 0;
        let mut stable_since: Option<Instant> = None;

        loop {
            let state = app.state::<AppState>();
            if state.shutdown.load(Ordering::SeqCst) {
                return;
            }

            let status = {
                let mut guard = match state.sidecar.lock() {
                    Ok(guard) => guard,
                    Err(poisoned) => poisoned.into_inner(),
                };
                match guard.child.as_mut() {
                    Some(child) => match child.try_wait() {
                        Ok(Some(_)) => ChildStatus::Exited,
                        Ok(None) => ChildStatus::Running,
                        Err(_) => ChildStatus::Exited,
                    },
                    None => ChildStatus::Missing,
                }
            };

            match status {
                ChildStatus::Running => {
                    let now = Instant::now();
                    stable_since = Some(match stable_since {
                        Some(since) if now.duration_since(since) >= STABLE_RESET_AFTER => {
                            if backoff_attempt != 0 {
                                backoff_attempt = 0;
                                eprintln!("[ARIA] Supervisor: backoff reset after stable run");
                            }
                            now
                        }
                        Some(since) => since,
                        None => now,
                    });
                    std::thread::sleep(MONITOR_INTERVAL);
                }
                ChildStatus::Exited | ChildStatus::Missing => {
                    stable_since = None;
                    let delay = backoff_delay(backoff_attempt, MAX_BACKOFF);
                    eprintln!(
                        "[ARIA] Supervisor: backend down (attempt {}), restart in {:?}",
                        backoff_attempt + 1,
                        delay
                    );
                    std::thread::sleep(delay);

                    match launch_backend(&app, &plan) {
                        Ok(child) => {
                            if app.state::<AppState>().shutdown.load(Ordering::SeqCst) {
                                let mut child = child;
                                let _ = child.kill();
                                let _ = child.wait();
                                return;
                            }

                            let state = app.state::<AppState>();
                            {
                                let mut guard = match state.sidecar.lock() {
                                    Ok(guard) => guard,
                                    Err(poisoned) => poisoned.into_inner(),
                                };
                                // Re-check shutdown while holding the lock: if the app
                                // exited between the check above and the lock acquire,
                                // do not leave a freshly-spawned orphan process behind.
                                if state.shutdown.load(Ordering::SeqCst) {
                                    let mut child = child;
                                    let _ = child.kill();
                                    let _ = child.wait();
                                    return;
                                }
                                guard.child = Some(child);
                                guard.bootstrap = bootstrap_from_plan(&plan);
                            }
                            if let Ok(mut err) = state.init_error.lock() {
                                *err = None;
                            }

                            eprintln!(
                                "[ARIA] Supervisor: backend restarted on port {}",
                                plan.port
                            );
                            if let Some(window) = app.get_webview_window("main") {
                                let backend_url = format!("http://127.0.0.1:{}", plan.port);
                                if let Ok(url) = tauri::Url::parse(&backend_url) {
                                    let _ = window.navigate(url);
                                }
                            }
                            stable_since = Some(Instant::now());
                        }
                        Err(e) => {
                            eprintln!("[ARIA] Supervisor: restart failed: {e}");
                            backoff_attempt += 1;
                        }
                    }
                }
            }
        }
    });
}

#[tauri::command]
fn get_init_error(state: State<'_, AppState>) -> Option<String> {
    state.init_error.lock().ok().and_then(|g| g.clone())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Second instance launched — focus the existing window
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let plan = make_launch_plan(&app.handle());
            let bootstrap = bootstrap_from_plan(&plan);

            let (child, init_error) = match launch_backend(&app.handle(), &plan) {
                Ok(child) => (Some(child), None),
                Err(e) => {
                    eprintln!("[ARIA] Init error: {e}");
                    (None, Some(e))
                }
            };

            app.manage(AppState {
                sidecar: Mutex::new(SidecarRuntime {
                    child,
                    bootstrap: bootstrap.clone(),
                }),
                init_error: Mutex::new(init_error),
                shutdown: Arc::new(AtomicBool::new(false)),
            });

            // Same-origin SPA mode: navigate the webview to the backend itself,
            // which serves desktop/dist + embeds the runtime token into the HTML.
            // The API and the page then share one origin — no CORS, no invoke
            // bridge for auth.
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(url) = tauri::Url::parse(&bootstrap.backend_base_url) {
                    let _ = window.navigate(url);
                }
            }

            // Supervisor: respawn the backend if it crashes (see spawn_supervisor).
            spawn_supervisor(app.handle().clone(), plan);

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_init_error])
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::ExitRequested { .. } = event {
            let state = app_handle.state::<AppState>();

            // Stop the supervisor first so it doesn't respawn the backend
            // while we are tearing it down.
            state.shutdown.store(true, Ordering::SeqCst);

            let mut guard = match state.sidecar.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            graceful_shutdown(&mut guard);
        }
    });
}
