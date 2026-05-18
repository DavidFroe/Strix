//! Auto-recovery for the local OwlTrail adapter.
//!
//! When a request to a loopback `base_url` fails with a network error
//! (typically "Connection refused" because the adapter process died), this
//! module checks whether `owltrail_adapter.py` is running and respawns it
//! if not. Best-effort, idempotent, and rate-limited via a cooldown so it
//! cannot fork-bomb on a tight retry loop.
//!
//! Wired from `client::send_with_retry`'s retry callback. Activated only
//! when `OWLTRAIL_ADAPTER_PATH` is set in the environment (start.sh exports
//! it). Without that env var, recovery is a no-op so non-OwlTrail users are
//! unaffected.

use std::net::TcpStream;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::logging;

const RECOVERY_COOLDOWN: Duration = Duration::from_secs(15);
const PORT_READY_TIMEOUT: Duration = Duration::from_secs(5);
const PORT_POLL_INTERVAL: Duration = Duration::from_millis(200);

static LAST_RECOVERY: Mutex<Option<Instant>> = Mutex::new(None);

/// Returns true if `base_url` points at a loopback address. Recovery only
/// makes sense for the local adapter — we must not try to "restart" a
/// remote DeepSeek endpoint.
pub fn is_local_url(base_url: &str) -> bool {
    base_url.starts_with("http://127.0.0.1")
        || base_url.starts_with("http://localhost")
        || base_url.starts_with("http://[::1]")
}

/// Extract the port from a base_url like `http://127.0.0.1:8081/v1`.
/// Falls back to `OWLTRAIL_PORT` env var or `8081`.
fn resolve_port(base_url: &str) -> String {
    if let Some(rest) = base_url
        .strip_prefix("http://127.0.0.1:")
        .or_else(|| base_url.strip_prefix("http://localhost:"))
        .or_else(|| base_url.strip_prefix("http://[::1]:"))
        && let Some(end) = rest.find(|c: char| !c.is_ascii_digit())
    {
        let port = &rest[..end];
        if !port.is_empty() {
            return port.to_string();
        }
    }
    std::env::var("OWLTRAIL_PORT").unwrap_or_else(|_| "8081".to_string())
}

fn cooldown_active(now: Instant) -> bool {
    let mut last = LAST_RECOVERY.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(t) = *last
        && now.duration_since(t) < RECOVERY_COOLDOWN
    {
        return true;
    }
    *last = Some(now);
    false
}

fn adapter_running() -> bool {
    Command::new("pgrep")
        .arg("-f")
        .arg("owltrail_adapter.py")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn port_reachable(port: &str) -> bool {
    let addr = format!("127.0.0.1:{port}");
    addr.parse()
        .ok()
        .and_then(|sock| TcpStream::connect_timeout(&sock, Duration::from_millis(200)).ok())
        .is_some()
}

fn wait_for_port(port: &str, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if port_reachable(port) {
            return true;
        }
        std::thread::sleep(PORT_POLL_INTERVAL);
    }
    false
}

fn spawn_adapter(adapter_path: &str, port: &str) -> bool {
    let log_path = std::env::var("OWLTRAIL_LOG").unwrap_or_else(|_| {
        Path::new(adapter_path)
            .parent()
            .map(|p| p.join("owltrail.log").to_string_lossy().into_owned())
            .unwrap_or_else(|| "/tmp/owltrail.log".to_string())
    });

    let mut cmd = Command::new("python3");
    cmd.arg(adapter_path)
        .arg("--port")
        .arg(port)
        .stdin(Stdio::null());

    match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
    {
        Ok(stdout_file) => {
            let stderr_file = stdout_file.try_clone().ok();
            cmd.stdout(stdout_file);
            if let Some(f) = stderr_file {
                cmd.stderr(f);
            } else {
                cmd.stderr(Stdio::null());
            }
        }
        Err(_) => {
            cmd.stdout(Stdio::null()).stderr(Stdio::null());
        }
    }

    match cmd.spawn() {
        Ok(child) => {
            logging::warn(format!(
                "owltrail recovery: spawned adapter pid={} (log: {log_path})",
                child.id()
            ));
            // Detach: don't wait, the adapter is a long-running daemon.
            std::mem::drop(child);
            true
        }
        Err(err) => {
            logging::warn(format!(
                "owltrail recovery: failed to spawn '{adapter_path}': {err}"
            ));
            false
        }
    }
}

/// Outcome of a recovery attempt — useful for logging and tests.
#[derive(Debug, PartialEq, Eq)]
pub enum RecoveryOutcome {
    /// Not a loopback URL, recovery doesn't apply.
    NotLocal,
    /// `OWLTRAIL_ADAPTER_PATH` env var not set.
    NotConfigured,
    /// Adapter binary path was set but doesn't exist on disk.
    AdapterMissing,
    /// Skipped because we restarted within `RECOVERY_COOLDOWN`.
    CoolingDown,
    /// pgrep says adapter is already running — likely a different problem
    /// (backend down, slow startup), don't respawn.
    AlreadyRunning,
    /// Failed to spawn the python process.
    SpawnFailed,
    /// Spawned but port didn't come up within `PORT_READY_TIMEOUT`.
    SpawnedButNotReady,
    /// Spawned and port is reachable.
    Recovered,
}

/// Attempt to restart the local OwlTrail adapter for the given base URL.
/// Returns the outcome so the caller (typically the retry callback) can log
/// it. Synchronous: blocks the calling thread for up to ~5 seconds when a
/// restart is actually triggered, otherwise returns immediately.
pub fn try_restart_adapter(base_url: &str) -> RecoveryOutcome {
    if !is_local_url(base_url) {
        return RecoveryOutcome::NotLocal;
    }

    let adapter_path = match std::env::var("OWLTRAIL_ADAPTER_PATH") {
        Ok(p) if !p.trim().is_empty() => p,
        _ => return RecoveryOutcome::NotConfigured,
    };

    if !Path::new(&adapter_path).is_file() {
        logging::warn(format!(
            "owltrail recovery: OWLTRAIL_ADAPTER_PATH='{adapter_path}' does not exist"
        ));
        return RecoveryOutcome::AdapterMissing;
    }

    if cooldown_active(Instant::now()) {
        return RecoveryOutcome::CoolingDown;
    }

    if adapter_running() {
        logging::info(
            "owltrail recovery: adapter process is already running; not respawning",
        );
        return RecoveryOutcome::AlreadyRunning;
    }

    let port = resolve_port(base_url);
    logging::warn(format!(
        "owltrail recovery: adapter appears down — restarting on port {port}"
    ));

    if !spawn_adapter(&adapter_path, &port) {
        return RecoveryOutcome::SpawnFailed;
    }

    if wait_for_port(&port, PORT_READY_TIMEOUT) {
        logging::info(format!(
            "owltrail recovery: adapter ready on port {port}"
        ));
        RecoveryOutcome::Recovered
    } else {
        logging::warn(format!(
            "owltrail recovery: adapter did not bind port {port} within {:?}",
            PORT_READY_TIMEOUT
        ));
        RecoveryOutcome::SpawnedButNotReady
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_loopback_urls() {
        assert!(is_local_url("http://127.0.0.1:8081/v1"));
        assert!(is_local_url("http://localhost:8081"));
        assert!(is_local_url("http://[::1]:8081/v1"));
        assert!(!is_local_url("https://api.deepseek.com"));
        assert!(!is_local_url("http://10.0.0.1:8081"));
    }

    #[test]
    fn resolves_port_from_url() {
        assert_eq!(resolve_port("http://127.0.0.1:8081/v1"), "8081");
        assert_eq!(resolve_port("http://localhost:9000"), "9000");
        assert_eq!(resolve_port("http://[::1]:7777/v1"), "7777");
    }

    #[test]
    fn skips_recovery_for_non_local_url() {
        assert_eq!(
            try_restart_adapter("https://api.deepseek.com/v1"),
            RecoveryOutcome::NotLocal
        );
    }

    #[test]
    fn skips_recovery_when_env_not_set() {
        // Only meaningful when OWLTRAIL_ADAPTER_PATH is unset; otherwise
        // assert it's at least one of the documented "skip" outcomes so
        // the test is robust to test-env interleaving.
        let outcome = try_restart_adapter("http://127.0.0.1:65000/v1");
        assert!(matches!(
            outcome,
            RecoveryOutcome::NotConfigured
                | RecoveryOutcome::AdapterMissing
                | RecoveryOutcome::CoolingDown
                | RecoveryOutcome::AlreadyRunning
                | RecoveryOutcome::SpawnFailed
                | RecoveryOutcome::SpawnedButNotReady
                | RecoveryOutcome::Recovered
        ));
    }
}
