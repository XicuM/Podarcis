//! The embedded herdr pane.
//!
//! A real herdr process on a real pty, parsed by vt100 and drawn by tui-term.
//! `[experimental] allow_nested = true` in the session config is what makes
//! this legal — herdr otherwise refuses to run inside another multiplexer.

use std::io::{Read, Write};
use std::path::Path;
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use portable_pty::{Child, CommandBuilder, MasterPty, NativePtySystem, PtySize, PtySystem};

use crate::event::AppEvent;
use crate::theme::Flavor;

/// Scrollback the pane keeps, in lines.
const SCROLLBACK: usize = 2000;

pub struct Pane {
    parser: Arc<Mutex<vt100::Parser>>,
    writer: Box<dyn Write + Send>,
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
    size: (u16, u16),
    /// Set once the child has exited, so the pane can say so instead of
    /// rendering a frozen screen.
    exited: Arc<Mutex<bool>>,
}

impl Pane {
    /// Spawn `herdr --session podarcis` in `cwd`.
    pub fn spawn(cwd: &Path, rows: u16, cols: u16, tx: Sender<AppEvent>) -> Result<Self> {
        let mut cmd = CommandBuilder::new(herdr_binary());
        cmd.args(["--session", super::config::SESSION]);
        cmd.cwd(cwd);
        // A child that believes it is inside us would try to talk to our
        // (non-existent) socket; it must connect to the real herdr server.
        cmd.env("TERM", "xterm-256color");
        cmd.env("COLORTERM", "truecolor");
        Self::spawn_command(cmd, rows, cols, tx)
    }

    pub fn spawn_command(
        cmd: CommandBuilder,
        rows: u16,
        cols: u16,
        tx: Sender<AppEvent>,
    ) -> Result<Self> {
        let rows = rows.max(1);
        let cols = cols.max(1);
        let pty = NativePtySystem::default()
            .openpty(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
            .context("opening a pty for the herdr pane")?;

        let child = pty.slave.spawn_command(cmd).context("spawning herdr")?;
        drop(pty.slave);

        let writer = pty.master.take_writer().context("taking the pty writer")?;
        let mut reader = pty.master.try_clone_reader().context("cloning the pty reader")?;
        let parser = Arc::new(Mutex::new(vt100::Parser::new(rows, cols, SCROLLBACK)));
        let exited = Arc::new(Mutex::new(false));

        {
            let parser = Arc::clone(&parser);
            let exited = Arc::clone(&exited);
            std::thread::spawn(move || {
                let mut buf = [0u8; 8192];
                loop {
                    match reader.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if let Ok(mut parser) = parser.lock() {
                                parser.process(&buf[..n]);
                            }
                            if tx.send(AppEvent::PtyOutput).is_err() {
                                return;
                            }
                        }
                    }
                }
                if let Ok(mut flag) = exited.lock() {
                    *flag = true;
                }
                let _ = tx.send(AppEvent::PtyExited);
            });
        }

        Ok(Self { parser, writer, master: pty.master, child, size: (rows, cols), exited })
    }

    pub fn resize(&mut self, rows: u16, cols: u16) {
        let (rows, cols) = (rows.max(1), cols.max(1));
        if self.size == (rows, cols) {
            return;
        }
        self.size = (rows, cols);
        let _ = self
            .master
            .resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
        if let Ok(mut parser) = self.parser.lock() {
            parser.screen_mut().set_size(rows, cols);
        }
    }

    pub fn send(&mut self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        let _ = self.writer.write_all(bytes);
        let _ = self.writer.flush();
    }

    pub fn is_alive(&self) -> bool {
        !*self.exited.lock().map(|g| *g).as_ref().unwrap_or(&true)
    }

    /// Run `f` against the current screen. The lock is held only for the call,
    /// so the reader thread is never blocked for longer than one paint.
    pub fn with_screen<T>(&self, f: impl FnOnce(&vt100::Screen) -> T) -> Option<T> {
        self.parser.lock().ok().map(|parser| f(parser.screen()))
    }

    /// True while the child has DECCKM set. Arrow keys must then be sent as
    /// `ESC O A` rather than `ESC [ A`, which is what full-screen programs
    /// (herdr included) expect.
    pub fn application_cursor(&self) -> bool {
        self.with_screen(|screen| screen.application_cursor()).unwrap_or(false)
    }
}

impl Drop for Pane {
    fn drop(&mut self) {
        // The pane is a child, not a daemon: closing the app closes it.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn herdr_binary() -> String {
    std::env::var("PODARCIS_HERDR").unwrap_or_else(|_| "herdr".to_string())
}

/// Is a herdr binary on `PATH` at all? Used to explain an empty sidebar rather
/// than showing a blank box.
pub fn available() -> bool {
    which(&herdr_binary()).is_some()
}

pub fn which(binary: &str) -> Option<std::path::PathBuf> {
    if binary.contains('/') {
        let path = std::path::PathBuf::from(binary);
        return path.is_file().then_some(path);
    }
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths).find_map(|dir| {
            let candidate = dir.join(binary);
            candidate.is_file().then_some(candidate)
        })
    })
}

/// Reload the running herdr server's config after a theme change. Best effort:
/// if there is no server the theme applies at next launch anyway.
pub fn reload_config(flavor: Flavor) {
    if super::config::provision(flavor).is_err() || !available() {
        return;
    }
    let binary = herdr_binary();
    std::thread::spawn(move || {
        let _ = std::process::Command::new(binary)
            .args(["--session", super::config::SESSION, "server", "reload-config"])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pane(script: &str) -> (Pane, std::sync::mpsc::Receiver<AppEvent>) {
        let (tx, rx) = std::sync::mpsc::channel();
        let mut cmd = CommandBuilder::new("/bin/sh");
        cmd.args(["-c", script]);
        cmd.env("TERM", "xterm-256color");
        (Pane::spawn_command(cmd, 10, 40, tx).unwrap(), rx)
    }

    fn wait_for(rx: &std::sync::mpsc::Receiver<AppEvent>, want_exit: bool) {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while std::time::Instant::now() < deadline {
            match rx.recv_timeout(std::time::Duration::from_millis(200)) {
                Ok(AppEvent::PtyExited) => return,
                Ok(_) if !want_exit => return,
                Ok(_) => {}
                Err(_) => {}
            }
        }
    }

    fn screen_text(pane: &Pane) -> String {
        pane.with_screen(|s| s.contents()).unwrap_or_default()
    }

    #[test]
    fn renders_child_output_into_the_screen() {
        let (pane, rx) = pane("printf 'hello pane'; sleep 0.2");
        wait_for(&rx, true);
        assert!(screen_text(&pane).contains("hello pane"), "{:?}", screen_text(&pane));
    }

    #[test]
    fn input_reaches_the_child() {
        let (mut pane, rx) = pane("read line; printf 'got:%s' \"$line\"");
        pane.send(b"ping\r");
        wait_for(&rx, true);
        assert!(screen_text(&pane).contains("got:ping"), "{:?}", screen_text(&pane));
    }

    #[test]
    fn reports_when_the_child_exits() {
        let (pane, rx) = pane("exit 0");
        wait_for(&rx, true);
        // The reader thread sets the flag just before sending PtyExited.
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert!(!pane.is_alive());
    }

    #[test]
    fn resize_updates_both_the_pty_and_the_parser() {
        let (mut pane, _rx) = pane("sleep 5");
        pane.resize(20, 60);
        assert_eq!(pane.with_screen(|s| s.size()), Some((20, 60)));
        // A no-op resize must not churn the parser.
        pane.resize(20, 60);
        assert_eq!(pane.with_screen(|s| s.size()), Some((20, 60)));
    }

    #[test]
    fn a_zero_size_is_clamped_rather_than_panicking() {
        let (mut pane, _rx) = pane("sleep 5");
        pane.resize(0, 0);
        assert_eq!(pane.with_screen(|s| s.size()), Some((1, 1)));
    }

    #[test]
    fn application_cursor_mode_is_visible_to_the_key_encoder() {
        let (pane, rx) = pane("printf '\\033[?1h'; sleep 0.2");
        wait_for(&rx, true);
        assert!(pane.application_cursor());
    }

    #[test]
    fn sending_nothing_is_a_no_op() {
        let (mut pane, _rx) = pane("sleep 5");
        pane.send(b"");
    }

    #[test]
    fn a_missing_binary_is_an_error_not_a_panic() {
        let (tx, _rx) = std::sync::mpsc::channel();
        let cmd = CommandBuilder::new("/nonexistent/herdr-binary");
        assert!(Pane::spawn_command(cmd, 10, 40, tx).is_err());
    }

    #[test]
    fn which_finds_a_real_binary_and_misses_a_fake_one() {
        assert!(which("sh").is_some());
        assert!(which("definitely-not-a-real-binary-xyz").is_none());
    }
}
