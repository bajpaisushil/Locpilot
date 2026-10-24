//! Lifecycle of the bundled Python backend ("sidecar").
//!
//! The shell starts `locpilot-backend` on an ephemeral 127.0.0.1 port with a random
//! per-launch bearer token, waits for its `LOCPILOT_READY {...}` stdout line, and hands
//! `{base_url, token}` to the webview through the `backend_info` command. The webview
//! itself has no permission to spawn processes.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::Duration;
#[cfg(unix)]
use std::time::Instant;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const SIDECAR: &str = "locpilot-backend";
const READY_PREFIX: &str = "LOCPILOT_READY ";
const STATUS_EVENT: &str = "backend-status";
const MAX_LOG_LINES: usize = 400;
const LOG_TAIL_LINES: usize = 80;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(120);
#[cfg(unix)]
const GRACEFUL_STOP: Duration = Duration::from_secs(4);

#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum Status {
    #[default]
    Starting,
    Ready,
    Failed,
}

impl Status {
    fn as_str(self) -> &'static str {
        match self {
            Status::Starting => "starting",
            Status::Ready => "ready",
            Status::Failed => "failed",
        }
    }
}

#[derive(Default)]
struct Inner {
    status: Status,
    port: Option<u16>,
    token: Option<String>,
    error: Option<String>,
    logs: VecDeque<String>,
    child: Option<CommandChild>,
    /// Incremented on every (re)start so events from an old child are ignored.
    generation: u64,
    stopping: bool,
}

#[derive(Default)]
pub struct BackendState(Mutex<Inner>);

/// Payload returned to (and emitted for) the webview.
#[derive(Clone, Serialize)]
pub struct BackendInfo {
    status: &'static str,
    base_url: Option<String>,
    token: Option<String>,
    error: Option<String>,
    logs_tail: Option<String>,
}

impl Inner {
    fn snapshot(&self) -> BackendInfo {
        let ready = self.status == Status::Ready;
        let tail: Vec<&str> = self
            .logs
            .iter()
            .skip(self.logs.len().saturating_sub(LOG_TAIL_LINES))
            .map(String::as_str)
            .collect();
        BackendInfo {
            status: self.status.as_str(),
            base_url: if ready { self.port.map(|p| format!("http://127.0.0.1:{p}")) } else { None },
            token: if ready { self.token.clone() } else { None },
            error: self.error.clone(),
            logs_tail: if tail.is_empty() { None } else { Some(tail.join("\n")) },
        }
    }

    fn push_log(&mut self, line: &str) {
        let line = line.trim_end();
        if line.is_empty() {
            return;
        }
        if self.logs.len() >= MAX_LOG_LINES {
            self.logs.pop_front();
        }
        self.logs.push_back(line.to_string());
    }
}

fn random_token() -> Result<String, String> {
    let mut buf = [0u8; 32];
    getrandom::fill(&mut buf).map_err(|e| format!("secure random generator unavailable: {e}"))?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

fn emit_status(app: &AppHandle) {
    let info = app.state::<BackendState>().0.lock().unwrap().snapshot();
    let _ = app.emit(STATUS_EVENT, info);
}

/// Applies `f` only if `generation` is still the current backend generation.
fn with_current<F: FnOnce(&mut Inner)>(app: &AppHandle, generation: u64, f: F) -> bool {
    let state = app.state::<BackendState>();
    let mut inner = state.0.lock().unwrap();
    if inner.generation != generation {
        return false;
    }
    f(&mut inner);
    true
}

fn fail(app: &AppHandle, generation: u64, message: String) {
    if with_current(app, generation, |inner| {
        inner.status = Status::Failed;
        inner.error = Some(message.clone());
        inner.push_log(&format!("[locpilot] {message}"));
    }) {
        emit_status(app);
    }
}

#[derive(serde::Deserialize)]
struct ReadyLine {
    port: u16,
}

fn handle_stdout_line(app: &AppHandle, generation: u64, line: &str) {
    let line = line.trim();
    if let Some(json) = line.strip_prefix(READY_PREFIX) {
        match serde_json::from_str::<ReadyLine>(json) {
            Ok(ready) => {
                if with_current(app, generation, |inner| {
                    inner.status = Status::Ready;
                    inner.port = Some(ready.port);
                    inner.error = None;
                    inner.push_log(&format!("[locpilot] backend ready on 127.0.0.1:{}", ready.port));
                }) {
                    emit_status(app);
                }
            }
            Err(e) => fail(app, generation, format!("Malformed ready line from backend: {e}")),
        }
    } else {
        with_current(app, generation, |inner| inner.push_log(line));
    }
}

/// Debug builds only: `LOCPILOT_EXTERNAL_BACKEND=http://127.0.0.1:8420` (+ optional
/// `LOCPILOT_DEV_TOKEN`) makes `tauri dev` use a separately started, live-reloading backend
/// instead of the bundled sidecar. Release builds always use the sidecar.
fn external_backend() -> Option<(u16, Option<String>)> {
    if !cfg!(debug_assertions) {
        return None;
    }
    let url = std::env::var("LOCPILOT_EXTERNAL_BACKEND").ok()?;
    let port = url.strip_prefix("http://127.0.0.1:")?.trim_end_matches('/').parse().ok()?;
    Some((port, std::env::var("LOCPILOT_DEV_TOKEN").ok().filter(|t| !t.is_empty())))
}

/// Spawns the sidecar. Errors are recorded in the state (status = failed), never panics.
pub fn start(app: &AppHandle) {
    let state = app.state::<BackendState>();
    if let Some((port, token)) = external_backend() {
        {
            let mut inner = state.0.lock().unwrap();
            inner.generation += 1;
            inner.status = Status::Ready;
            inner.port = Some(port);
            inner.token = token;
            inner.error = None;
            inner.push_log(&format!("[locpilot] using external dev backend on 127.0.0.1:{port}"));
        }
        emit_status(app);
        return;
    }
    let token = match random_token() {
        Ok(t) => t,
        Err(e) => {
            let mut inner = state.0.lock().unwrap();
            inner.status = Status::Failed;
            inner.error = Some(e);
            return;
        }
    };
    let generation = {
        let mut inner = state.0.lock().unwrap();
        inner.generation += 1;
        inner.status = Status::Starting;
        inner.port = None;
        inner.error = None;
        inner.token = Some(token.clone());
        inner.stopping = false;
        inner.logs.clear();
        inner.generation
    };
    emit_status(app);

    let data_dir = match app.path().app_data_dir() {
        Ok(dir) => dir,
        Err(e) => return fail(app, generation, format!("Cannot resolve the app data directory: {e}")),
    };
    if let Err(e) = std::fs::create_dir_all(&data_dir) {
        return fail(app, generation, format!("Cannot create {}: {e}", data_dir.display()));
    }

    let command = match app.shell().sidecar(SIDECAR) {
        Ok(cmd) => cmd,
        Err(e) => return fail(app, generation, format!("The bundled backend binary was not found: {e}")),
    };
    let parent_pid = std::process::id().to_string();
    let command = command
        .args(["--host", "127.0.0.1", "--port", "0", "--parent-pid", parent_pid.as_str(), "--data-dir"])
        .arg(&data_dir)
        .env("LOCPILOT_AUTH_TOKEN", &token)
        .env("PYTHONUNBUFFERED", "1");

    let (mut rx, child) = match command.spawn() {
        Ok(pair) => pair,
        Err(e) => return fail(app, generation, format!("Failed to start the backend: {e}")),
    };
    state.0.lock().unwrap().child = Some(child);

    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut pending = String::new();
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    pending.push_str(&String::from_utf8_lossy(&bytes));
                    while let Some(pos) = pending.find('\n') {
                        let line: String = pending.drain(..=pos).collect();
                        handle_stdout_line(&handle, generation, &line);
                    }
                    // The shell plugin is line-oriented and may strip the newline.
                    if !pending.is_empty() {
                        let line = std::mem::take(&mut pending);
                        handle_stdout_line(&handle, generation, &line);
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    let text = String::from_utf8_lossy(&bytes).to_string();
                    with_current(&handle, generation, |inner| {
                        for line in text.lines() {
                            inner.push_log(line);
                        }
                    });
                }
                CommandEvent::Error(err) => {
                    with_current(&handle, generation, |inner| inner.push_log(&format!("[shell] {err}")));
                }
                CommandEvent::Terminated(payload) => {
                    let message = match (payload.code, payload.signal) {
                        (Some(code), _) => format!("The backend exited unexpectedly (exit code {code})."),
                        (None, Some(sig)) => format!("The backend was terminated by signal {sig}."),
                        _ => "The backend exited unexpectedly.".to_string(),
                    };
                    let stopping = with_current(&handle, generation, |inner| inner.child = None)
                        && handle.state::<BackendState>().0.lock().unwrap().stopping;
                    if !stopping {
                        fail(&handle, generation, message);
                    }
                    break;
                }
                _ => {}
            }
        }
    });

    // Startup watchdog: a backend that never becomes ready is reported as failed.
    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(STARTUP_TIMEOUT);
        let still_starting = {
            let state = handle.state::<BackendState>();
            let inner = state.0.lock().unwrap();
            inner.generation == generation && inner.status == Status::Starting
        };
        if still_starting {
            stop(&handle);
            fail(
                &handle,
                generation,
                format!("Timed out after {}s waiting for the backend to start.", STARTUP_TIMEOUT.as_secs()),
            );
        }
    });
}

/// Stops the sidecar (if running). Safe to call multiple times.
///
/// On Unix the backend first gets SIGTERM so it can cancel running tasks and kill their
/// subprocesses (the PyInstaller bootloader forwards the signal to Python); it is
/// force-killed if it has not exited after a short grace period. The backend also exits
/// on its own when this process disappears (`--parent-pid`), covering crashes.
pub fn stop(app: &AppHandle) {
    let child = {
        let state = app.state::<BackendState>();
        let mut inner = state.0.lock().unwrap();
        inner.stopping = true;
        inner.child.take()
    };
    let Some(child) = child else { return };
    #[cfg(unix)]
    {
        let pid = child.pid() as libc::pid_t;
        // SAFETY: plain kill(2) calls on the pid of a child process we spawned.
        unsafe { libc::kill(pid, libc::SIGTERM) };
        let deadline = Instant::now() + GRACEFUL_STOP;
        while Instant::now() < deadline {
            if unsafe { libc::kill(pid, 0) } != 0 {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    let _ = child.kill();
}

#[tauri::command]
pub fn backend_info(state: State<'_, BackendState>) -> BackendInfo {
    state.0.lock().unwrap().snapshot()
}

#[tauri::command]
pub fn restart_backend(app: AppHandle) -> BackendInfo {
    stop(&app);
    start(&app);
    app.state::<BackendState>().0.lock().unwrap().snapshot()
}

/// Shows a native save dialog and writes `contents` to the chosen file.
/// The webview never chooses an arbitrary path itself.
#[tauri::command]
pub async fn save_text_file(app: AppHandle, default_name: String, contents: String) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let picked = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().set_file_name(&default_name).blocking_save_file()
    })
    .await
    .map_err(|e| e.to_string())?;
    let Some(file) = picked else { return Ok(None) };
    let path = file.into_path().map_err(|e| e.to_string())?;
    std::fs::write(&path, contents).map_err(|e| format!("Could not write {}: {e}", path.display()))?;
    Ok(Some(path.display().to_string()))
}
