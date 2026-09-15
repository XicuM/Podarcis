//! One event queue for everything the app reacts to.
//!
//! Terminal input, filesystem changes, background job output and the herdr PTY
//! all arrive on the same channel, so the main loop blocks on a single receive
//! and never polls. There is no tick: a frame is drawn because something
//! happened, not because time passed.

use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Duration;

use anyhow::Result;
use crossterm::event::{Event as CtEvent, KeyEvent, KeyEventKind, MouseEvent};
use notify::{RecursiveMode, Watcher};

use crate::vault::index::Index;

#[derive(Debug)]
pub enum AppEvent {
    Key(KeyEvent),
    Mouse(MouseEvent),
    Paste(String),
    Resize,
    /// Content files changed on disk.
    FsChanged(Vec<PathBuf>),
    /// The background index finished its first full build.
    IndexReady(Box<Index>),
    /// A background job finished.
    JobDone(crate::actions::JobResult),
    /// An agent asked the front-end to do something over the control socket.
    Control(crate::control::Request),
    /// The herdr child produced output and the pane needs a repaint.
    PtyOutput,
    /// The herdr child exited.
    PtyExited,
}

pub struct Events {
    pub rx: Receiver<AppEvent>,
    pub tx: Sender<AppEvent>,
    _watcher: Option<notify::RecommendedWatcher>,
}

impl Events {
    pub fn new() -> Self {
        let (tx, rx) = mpsc::channel();
        spawn_input(tx.clone());
        Self { rx, tx, _watcher: None }
    }

    /// Watch the content collections. Failures are not fatal — the app simply
    /// stops auto-refreshing.
    pub fn watch(&mut self, dirs: &[PathBuf]) -> Result<()> {
        let tx = self.tx.clone();
        let mut watcher = notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
            let Ok(event) = res else { return };
            if !matches!(
                event.kind,
                notify::EventKind::Create(_) | notify::EventKind::Modify(_) | notify::EventKind::Remove(_)
            ) {
                return;
            }
            let paths: Vec<PathBuf> = event
                .paths
                .into_iter()
                .filter(|p| {
                    matches!(p.extension().and_then(|e| e.to_str()), Some("md") | Some("csv"))
                })
                .collect();
            if !paths.is_empty() {
                let _ = tx.send(AppEvent::FsChanged(paths));
            }
        })?;
        for dir in dirs {
            watcher.watch(dir, RecursiveMode::Recursive)?;
        }
        self._watcher = Some(watcher);
        Ok(())
    }

    /// Drain anything already queued. Used to coalesce a burst of filesystem
    /// events or PTY output into a single redraw.
    pub fn drain(&self, first: AppEvent) -> Vec<AppEvent> {
        let mut out = vec![first];
        while let Ok(event) = self.rx.try_recv() {
            out.push(event);
        }
        out
    }
}

impl Default for Events {
    fn default() -> Self {
        Self::new()
    }
}

fn spawn_input(tx: Sender<AppEvent>) {
    std::thread::spawn(move || loop {
        // A poll with a timeout rather than a blocking read, so the thread
        // notices a closed channel and exits instead of leaking.
        match crossterm::event::poll(Duration::from_millis(250)) {
            Ok(true) => {}
            Ok(false) => {
                if tx.send(AppEvent::PtyOutput).is_err() {
                    return;
                }
                continue;
            }
            Err(_) => return,
        }
        let Ok(event) = crossterm::event::read() else { return };
        let mapped = match event {
            // Windows sends key-release events too; acting on both would
            // double every keystroke.
            CtEvent::Key(key) if key.kind == KeyEventKind::Press => AppEvent::Key(key),
            CtEvent::Key(_) => continue,
            CtEvent::Mouse(m) => AppEvent::Mouse(m),
            CtEvent::Paste(text) => AppEvent::Paste(text),
            CtEvent::Resize(_, _) => AppEvent::Resize,
            CtEvent::FocusGained | CtEvent::FocusLost => continue,
        };
        if tx.send(mapped).is_err() {
            return;
        }
    });
}
