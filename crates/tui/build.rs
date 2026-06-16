use std::{
    path::{Path, PathBuf},
    process::Command,
};

fn main() {
    println!("cargo:rerun-if-env-changed=DEEPSEEK_BUILD_SHA");
    println!("cargo:rerun-if-env-changed=GITHUB_SHA");
    declare_git_head_rerun();
    configure_windows_stack();

    let package_version = env!("CARGO_PKG_VERSION");
    let build_version = build_sha()
        .map(|sha| format!("{package_version} ({sha})"))
        .unwrap_or_else(|| package_version.to_string());

    println!("cargo:rustc-env=DEEPSEEK_BUILD_VERSION={build_version}");

    // Strix-Build-Counter: Anzahl Commits = monoton wachsende Build-Nr.
    // Fällt zurück auf 0, wenn git nicht verfügbar (z.B. crates.io-Build).
    let build_number = git_commit_count().unwrap_or(0);
    println!("cargo:rustc-env=STRIX_BUILD_NUMBER={build_number}");

    // Build-Zeitstempel — wird in der Web-UI angezeigt damit man im Browser
    // sieht, welche Version gerade läuft (Cache-Verifikation).
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = secs / 86400;
    let h = (secs % 86400) / 3600;
    let m = (secs % 3600) / 60;
    let (yr, mo, dy) = days_to_ymd(days);
    let stamp_s = format!("{:04}-{:02}-{:02} {:02}:{:02}", yr, mo, dy, h, m);
    println!("cargo:rustc-env=STRIX_BUILD_STAMP={stamp_s}");
    // Bei jedem index.html-Edit neu bauen, damit Stempel + HTML zusammen passen.
    println!("cargo:rerun-if-changed=webui/index.html");
}

fn days_to_ymd(mut days: i64) -> (i32, u32, u32) {
    let mut year: i32 = 1970;
    loop {
        let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
        let ydays: i64 = if leap { 366 } else { 365 };
        if days < ydays { break; }
        days -= ydays;
        year += 1;
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let mlens: [i64; 12] = [31, if leap {29} else {28}, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut month: u32 = 1;
    for &ml in &mlens {
        if days < ml { break; }
        days -= ml;
        month += 1;
    }
    (year, month, (days + 1) as u32)
}

fn git_commit_count() -> Option<u64> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let output = Command::new("git")
        .args(["-C"])
        .arg(&manifest_dir)
        .args(["rev-list", "--count", "HEAD"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout).trim().parse().ok()
}

/// Tell Cargo to invalidate the cached build script output when `HEAD`
/// moves, so the embedded short-SHA stays in sync with the checkout.
///
/// `.git/HEAD` only changes on branch switches and detached-HEAD moves —
/// `git commit` on the current branch updates the underlying ref file
/// (loose `refs/heads/<name>`, or `packed-refs` after `git pack-refs`)
/// without touching `HEAD` itself. So when `HEAD` is a symbolic ref we
/// also watch the resolved target and `packed-refs`. A non-existent
/// `rerun-if-changed` path is treated as "always changed" by Cargo, which
/// covers the loose→packed transition.
fn declare_git_head_rerun() {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let workspace_root = manifest_dir.join("..").join("..");
    let git_meta = workspace_root.join(".git");

    let gitdir = if git_meta.is_dir() {
        git_meta
    } else if git_meta.is_file() {
        // Worktree pointer file: watch it directly, then follow `gitdir:`.
        println!("cargo:rerun-if-changed={}", git_meta.display());
        let Ok(contents) = std::fs::read_to_string(&git_meta) else {
            return;
        };
        let Some(rest) = contents.lines().find_map(|l| l.strip_prefix("gitdir:")) else {
            return;
        };
        let trimmed = rest.trim();
        if Path::new(trimmed).is_absolute() {
            PathBuf::from(trimmed)
        } else {
            workspace_root.join(trimmed)
        }
    } else {
        return;
    };

    let head = gitdir.join("HEAD");
    println!("cargo:rerun-if-changed={}", head.display());

    if let Ok(contents) = std::fs::read_to_string(&head)
        && let Some(target) = parse_symbolic_ref(&contents)
    {
        println!("cargo:rerun-if-changed={}", gitdir.join(target).display());
        println!(
            "cargo:rerun-if-changed={}",
            gitdir.join("packed-refs").display()
        );
    }
}

/// If `.git/HEAD` is a symbolic ref (`ref: refs/heads/...`) return the
/// target ref path. Returns `None` for a detached HEAD (raw SHA).
fn parse_symbolic_ref(head_contents: &str) -> Option<&str> {
    head_contents
        .lines()
        .next()
        .and_then(|line| line.strip_prefix("ref:"))
        .map(str::trim)
        .filter(|s| !s.is_empty())
}

#[cfg(test)]
mod tests {
    use super::parse_symbolic_ref;

    #[test]
    fn symbolic_ref_strips_prefix_and_whitespace() {
        assert_eq!(
            parse_symbolic_ref("ref: refs/heads/main\n"),
            Some("refs/heads/main")
        );
    }

    #[test]
    fn symbolic_ref_handles_no_trailing_newline() {
        assert_eq!(
            parse_symbolic_ref("ref: refs/heads/work/v0.8.26-security"),
            Some("refs/heads/work/v0.8.26-security")
        );
    }

    #[test]
    fn detached_head_is_not_a_symbolic_ref() {
        assert_eq!(
            parse_symbolic_ref("506343f44e48b9c2c8d6b2d3e8e8e8e8e8e8e8e8\n"),
            None
        );
    }

    #[test]
    fn empty_input_returns_none() {
        assert_eq!(parse_symbolic_ref(""), None);
        assert_eq!(parse_symbolic_ref("ref: \n"), None);
    }
}

fn configure_windows_stack() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    match std::env::var("CARGO_CFG_TARGET_ENV").as_deref() {
        Ok("msvc") => println!("cargo:rustc-link-arg-bin=deepseek-tui=/STACK:8388608"),
        Ok("gnu") => println!("cargo:rustc-link-arg-bin=deepseek-tui=-Wl,--stack,8388608"),
        _ => {}
    }
}

fn build_sha() -> Option<String> {
    env_sha("DEEPSEEK_BUILD_SHA")
        .or_else(|| env_sha("GITHUB_SHA"))
        .or_else(git_sha)
}

fn env_sha(name: &str) -> Option<String> {
    std::env::var(name).ok().and_then(short_sha)
}

fn git_sha() -> Option<String> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let top_level_output = Command::new("git")
        .args(["-C"])
        .arg(&manifest_dir)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .ok()?;
    if !top_level_output.status.success() {
        return None;
    }
    let top_level = PathBuf::from(String::from_utf8_lossy(&top_level_output.stdout).trim());
    if !top_level.join("Cargo.toml").is_file() || !top_level.join("crates/tui").is_dir() {
        return None;
    }

    let output = Command::new("git")
        .args(["-C"])
        .arg(top_level)
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }

    short_sha(String::from_utf8_lossy(&output.stdout).to_string())
}

fn short_sha(value: String) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(trimmed.chars().take(12).collect())
}
