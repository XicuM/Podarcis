//! `podarcis-tui` — the Podarcis wiki front-end.

mod actions;
mod app;
mod config;
mod editor;
mod event;
mod herdr;
mod keymap;
mod search;
mod theme;
mod ui;
mod vault;

use std::io::stdout;
use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use crossterm::event::{DisableMouseCapture, EnableMouseCapture};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use crate::app::App;
use crate::config::Config;
use crate::event::{AppEvent, Events};

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
    let root = config::find_root(args.root.as_deref(), &cwd, env_root.as_deref())?;
    let cfg = Config::load(&root);

    if args.check {
        println!("root: {}", root.display());
        println!("theme: catppuccin-{}", cfg.flavor.as_str());
        println!("qmd: {}", cfg.qmd_enabled);
        println!("herdr: {}", if herdr::pty::available() { "found" } else { "missing" });
        for (label, path) in cfg.collections() {
            println!("{label}: {}", path.display());
        }
        return Ok(());
    }

    if args.lint {
        let index = vault::index::Index::build(&root, &cfg.collections().into_iter().map(|(_, p)| p).collect::<Vec<_>>());
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
        .map(|p| config::resolve_open_path(p, &root, &cwd))
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
    result
}

fn findings_json(findings: &[vault::lint::Finding]) -> serde_json::Value {
    serde_json::Value::Array(
        findings
            .iter()
            // A code the engine does not emit would make the two linters
            // undiffable, which is the one thing this output exists for.
            .filter(|f| vault::lint::is_known(f.code))
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
    terminal.draw(|frame| ui::draw(frame, app))?;

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
                AppEvent::JobLine(id, line) => {
                    app.jobs.note(id, line);
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
            terminal.draw(|frame| ui::draw(frame, app))?;
        }
    }
    Ok(())
}
