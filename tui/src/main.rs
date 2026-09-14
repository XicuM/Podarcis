//! `podarcis-tui` — the Podarcis wiki front-end.

use std::io::stdout;
use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use crossterm::event::{DisableMouseCapture, EnableMouseCapture};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use podarcis::app::App;
use podarcis::config::Config;
use podarcis::event::{AppEvent, Events};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

#[derive(Parser, Debug)]
#[command(name = "podarcis-tui", version, about = "Browse, search and edit the Podarcis wiki")]
struct Args {
    /// Page to open.
    path: Option<String>,
    /// Checkout root. Defaults to `$PODARCIS_ROOT`, else a walk up from the cwd.
    #[arg(long)]
    root: Option<PathBuf>,
    /// Print the resolved checkout and exit.
    #[arg(long)]
    check: bool,
    /// Print findings as `podarcis lint --json` would, and exit. Exists so the
    /// two linters can be diffed: if this ever disagrees with the engine, the
    /// in-app gutter is lying.
    #[arg(long)]
    lint: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let cwd = std::env::current_dir()?;
    let env_root = std::env::var("PODARCIS_ROOT").ok();
    let root = podarcis::config::find_root(args.root.as_deref(), &cwd, env_root.as_deref())?;
    let cfg = Config::load(&root);

    if args.check {
        println!("root: {}", root.display());
        println!("theme: {}", cfg.flavor.as_str());
        println!("qmd: {}", cfg.qmd_off_reason.unwrap_or("enabled"));
        println!("herdr: {}", if podarcis::herdr::pty::available() { "found" } else { "missing" });
        for (label, path) in cfg.collections() {
            println!("{label}: {}", path.display());
        }
        return Ok(());
    }

    if args.lint {
        let index = podarcis::vault::index::Index::build(
            &root,
            &cfg.collections().into_iter().map(|(_, p)| p).collect::<Vec<_>>(),
        );
        let mut files = serde_json::Map::new();
        for entry in &index.entries {
            if entry.findings.is_empty() {
                continue;
            }
            files.insert(entry.rel.clone(), findings_json(&entry.findings));
        }
        for (rel, finding) in &index.dir_findings {
            files.insert(rel.clone(), findings_json(std::slice::from_ref(finding)));
        }
        let payload = serde_json::json!({
            "ok": files.is_empty(),
            "root": root.display().to_string(),
            "files": files,
        });
        println!("{}", serde_json::to_string_pretty(&payload)?);
        return Ok(());
    }

    let open = args
        .path
        .as_deref()
        .map(|p| podarcis::config::resolve_open_path(p, &root, &cwd))
        .transpose()?;

    let mut events = Events::new();
    let mut app = App::new(cfg, events.tx.clone());
    app.start_indexing();
    let _ = events.watch(&app.collection_dirs());
    if let Some(path) = open {
        app.open_path(&path, false);
        app.tree.reveal(&path);
    }

    let mut terminal = enter()?;
    let result = run(&mut terminal, &mut app, &events);
    leave(&mut terminal)?;
    // A clean restart replaces the process image entirely — `leave()` has
    // already restored the terminal, so the fresh `main()` entry into
    // `enter()` sees a clean slate.
    if app.restart {
        exec_replace();
    }
    result
}

/// Replace the current process with a fresh copy of itself so a restart keeps
/// ownership of the terminal (foreground process group).
fn exec_replace() -> ! {
    use std::os::unix::ffi::OsStringExt;
    let exe = std::env::current_exe().expect("cannot resolve current binary");
    let c_exe = std::ffi::CString::new(exe.into_os_string().into_vec())
        .expect("binary path contains NUL");
    let c_args: Vec<std::ffi::CString> = std::env::args_os()
        .map(|a| std::ffi::CString::new(a.into_vec()).expect("argument contains NUL"))
        .collect();
    // execv requires argv to be NULL-terminated; it also takes ownership of
    // the pointers but not the CStrings, so they must outlive the call.
    let mut c_ptrs: Vec<*const std::ffi::c_char> = c_args.iter().map(|a| a.as_ptr()).collect();
    c_ptrs.push(std::ptr::null());
    // SAFETY: execv replaces the process image; it never returns on success.
    unsafe {
        libc::execv(c_exe.as_ptr(), c_ptrs.as_ptr());
    }
    // execv failed — the only remaining option is to abort, but a visible
    // panic is friendlier than silence.
    panic!("restart failed: {}", std::io::Error::last_os_error());
}

fn findings_json(findings: &[podarcis::vault::lint::Finding]) -> serde_json::Value {
    serde_json::Value::Array(
        findings
            .iter()
            // A code the engine does not emit would make the two linters
            // undiffable, which is the one thing this output exists for.
            .filter(|f| podarcis::vault::lint::is_known(f.code))
            .map(|f| serde_json::json!({"code": f.code, "detail": f.detail}))
            .collect(),
    )
}

struct Guard;

impl Drop for Guard {
    /// Restore the terminal even on a panic. Without this a crash leaves the
    /// user in raw mode with no echo, which is a genuinely hostile way to fail.
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = stdout().execute(DisableMouseCapture);
        let _ = stdout().execute(LeaveAlternateScreen);
    }
}

fn enter() -> Result<Terminal<CrosstermBackend<std::io::Stdout>>> {
    enable_raw_mode()?;
    stdout().execute(EnterAlternateScreen)?;
    stdout().execute(EnableMouseCapture)?;
    let hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = disable_raw_mode();
        let _ = stdout().execute(DisableMouseCapture);
        let _ = stdout().execute(LeaveAlternateScreen);
        hook(info);
    }));
    Ok(Terminal::new(CrosstermBackend::new(stdout()))?)
}

fn leave(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> Result<()> {
    disable_raw_mode()?;
    stdout().execute(DisableMouseCapture)?;
    stdout().execute(LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}

fn run(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    app: &mut App,
    events: &Events,
) -> Result<()> {
    let _guard = Guard;
    terminal.draw(|frame| podarcis::ui::draw(frame, app))?;

    while let Ok(first) = events.rx.recv() {
        // A burst of pty output or filesystem events collapses into one frame.
        let mut fs_changed: Vec<PathBuf> = Vec::new();
        let mut redraw = false;

        for event in events.drain(first) {
            match event {
                AppEvent::Key(key) => {
                    app.on_key(key);
                    redraw = true;
                }
                AppEvent::Paste(text) => {
                    for ch in text.chars() {
                        app.on_key(crossterm::event::KeyEvent::new(
                            crossterm::event::KeyCode::Char(ch),
                            crossterm::event::KeyModifiers::NONE,
                        ));
                    }
                    redraw = true;
                }
                AppEvent::Mouse(mouse) => {
                    app.on_mouse(mouse);
                    redraw = true;
                }
                AppEvent::Resize => {
                    app.reflow();
                    redraw = true;
                }
                AppEvent::FsChanged(paths) => {
                    fs_changed.extend(paths);
                    redraw = true;
                }
                AppEvent::IndexReady(index) => {
                    app.on_index_ready(*index);
                    redraw = true;
                }
                AppEvent::JobDone(result) => {
                    app.on_job_done(result);
                    redraw = true;
                }
                AppEvent::PtyOutput => redraw = true,
                AppEvent::PtyExited => redraw = true,
            }
        }

        if !fs_changed.is_empty() {
            fs_changed.sort();
            fs_changed.dedup();
            app.on_fs_changed(fs_changed);
        }
        app.expire_toasts();

        if app.quit {
            return Ok(());
        }
if redraw {
            app.reflow();
            terminal.draw(|frame| podarcis::ui::draw(frame, app))?;
        }
    }
    Ok(())
}
