//! The embedded herdr pane.
//!
//! A real herdr process on a real pty, parsed by vt100 and drawn by tui-term.
//! `[experimental] allow_nested = true` in the session config is what makes
//! this legal — herdr otherwise refuses to run inside another multiplexer.

use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
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
    writer: Arc<Mutex<Box<dyn Write + Send>>>,
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
    size: Arc<Mutex<(u16, u16)>>,
    /// Set once the child has exited, so the pane can say so instead of
    /// rendering a frozen screen.
    exited: Arc<Mutex<bool>>,
    /// True while the child has DECSET 1016 (SGR pixels) enabled.
    pixel_mouse: Arc<AtomicBool>,
    /// Stops the tab-label sync thread when the pane is dropped.
    label_sync: Arc<AtomicBool>,
}

impl Pane {
    /// Spawn `herdr --session podarcis` in `cwd`, ensuring the project workspace exists and is focused.
    pub fn spawn(project_name: &str, cwd: &Path, rows: u16, cols: u16, tx: Sender<AppEvent>) -> Result<Self> {
        // Ensure the workspace exists and is focused if server is running
        let _ = super::space::ensure_project_space(project_name, cwd);

        let mut cmd = CommandBuilder::new(herdr_binary());
        cmd.args(["--session", super::config::SESSION]);
        cmd.cwd(cwd);
        // A child that believes it is inside us would try to talk to our
        // (non-existent) socket; it must connect to the real herdr server.
        cmd.env("TERM", "xterm-256color");
        cmd.env("COLORTERM", "truecolor");
        // Clear multiplexer nesting variables as done in herdr-companion
        cmd.env_remove("HERDR_ENV");
        cmd.env_remove("HERDR_PANE_ID");
        cmd.env_remove("HERDR_TAB_ID");
        if let Some(cfg_path) = super::config::session_dir().map(|d| d.join("config.toml")) {
            if cfg_path.exists() {
                cmd.env("HERDR_CONFIG_PATH", cfg_path);
            }
        }

        let pane = Self::spawn_command(cmd, rows, cols, tx, Some(cwd))?;

        // In case the Herdr server was just started by spawn_command, ensure the workspace is named & focused
        let p_name = project_name.to_string();
        let p_cwd = cwd.to_path_buf();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(500));
            let _ = super::space::ensure_project_space(&p_name, &p_cwd);
        });

        Ok(pane)
    }

    /// Switch Herdr workspace focus to `name`, and update tab label sync for `cwd`.
    pub fn switch_project(&mut self, name: &str, cwd: &Path) {
        let _ = super::space::ensure_project_space(name, cwd);
        self.label_sync.store(true, Ordering::Relaxed);
        self.label_sync = super::labels::spawn(cwd);
    }

    pub fn spawn_command(
        cmd: CommandBuilder,
        rows: u16,
        cols: u16,
        tx: Sender<AppEvent>,
        label_cwd: Option<&Path>,
    ) -> Result<Self> {
        let rows = rows.max(1);
        let cols = cols.max(1);
        let pty = NativePtySystem::default()
            .openpty(pty_size(rows, cols))
            .context("opening a pty for the herdr pane")?;

        let child = pty.slave.spawn_command(cmd).context("spawning herdr")?;
        drop(pty.slave);

        let writer = Arc::new(Mutex::new(
            pty.master.take_writer().context("taking the pty writer")?,
        ));
        let mut reader = pty.master.try_clone_reader().context("cloning the pty reader")?;
        let parser = Arc::new(Mutex::new(vt100::Parser::new(rows, cols, SCROLLBACK)));
        let exited = Arc::new(Mutex::new(false));
        let pixel_mouse = Arc::new(AtomicBool::new(false));
        let size = Arc::new(Mutex::new((rows, cols)));

        {
            let parser = Arc::clone(&parser);
            let exited = Arc::clone(&exited);
            let writer = Arc::clone(&writer);
            let pixel_mouse = Arc::clone(&pixel_mouse);
            let size = Arc::clone(&size);
            std::thread::spawn(move || {
                let mut buf = [0u8; 8192];
                let mut carry = Vec::new();
                loop {
                    match reader.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if let Ok(mut parser) = parser.lock() {
                                parser.process(&buf[..n]);
                            }
                            let dims = size.lock().map(|g| *g).unwrap_or((1, 1));
                            reply_child_queries(&buf[..n], &mut carry, &writer, dims, &pixel_mouse);
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

        let label_sync = match label_cwd {
            Some(cwd) => super::labels::spawn(cwd),
            None => Arc::new(AtomicBool::new(false)),
        };

        Ok(Self {
            parser,
            writer,
            master: pty.master,
            child,
            size,
            exited,
            pixel_mouse,
            label_sync,
        })
    }

    pub fn resize(&mut self, rows: u16, cols: u16) {
        let (rows, cols) = (rows.max(1), cols.max(1));
        let current = self.size.lock().map(|g| *g).unwrap_or((0, 0));
        if current == (rows, cols) {
            return;
        }
        if let Ok(mut size) = self.size.lock() {
            *size = (rows, cols);
        }
        let _ = self.master.resize(pty_size(rows, cols));
        if let Ok(mut parser) = self.parser.lock() {
            parser.screen_mut().set_size(rows, cols);
        }
    }

    pub fn send(&mut self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        if let Ok(mut writer) = self.writer.lock() {
            let _ = writer.write_all(bytes);
            let _ = writer.flush();
        }
    }

    pub fn pixel_mouse(&self) -> bool {
        self.pixel_mouse.load(Ordering::Relaxed)
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
        self.label_sync.store(true, Ordering::Relaxed);
        // The pane is a child, not a daemon: closing the app closes it.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn pty_size(rows: u16, cols: u16) -> PtySize {
    let (cw, ch) = super::keys::PTY_CELL_PX;
    PtySize {
        rows,
        cols,
        pixel_width: cols.saturating_mul(cw),
        pixel_height: rows.saturating_mul(ch),
    }
}

fn write_pty(writer: &Mutex<Box<dyn Write + Send>>, bytes: &[u8]) {
    if let Ok(mut writer) = writer.lock() {
        let _ = writer.write_all(bytes);
        let _ = writer.flush();
    }
}

/// Answer each host query once (and drop it from the buffer).
///
/// A leftover `\x1b[16t` in the carry would otherwise be answered on every
/// subsequent PTY read, flooding herdr's stdin and making the pane unusable.
fn reply_child_queries(
    chunk: &[u8],
    carry: &mut Vec<u8>,
    writer: &Mutex<Box<dyn Write + Send>>,
    size: (u16, u16),
    pixel_mouse: &AtomicBool,
) {
    note_mouse_mode(chunk, pixel_mouse);
    carry.extend_from_slice(chunk);

    let mut i = 0;
    while i < carry.len() {
        if carry[i] != 0x1b {
            i += 1;
            continue;
        }
        if let Some((consumed, reply)) = query_reply_at(&carry[i..], size) {
            write_pty(writer, &reply);
            carry.drain(i..i + consumed);
            continue;
        }
        i += 1;
    }
    if carry.len() > 64 {
        let keep = 64;
        carry.drain(..carry.len() - keep);
    }
}

fn query_reply_at(buf: &[u8], size: (u16, u16)) -> Option<(usize, Vec<u8>)> {
    let (cw, ch) = super::keys::PTY_CELL_PX;
    let (rows, cols) = size;
    let win_h = u32::from(rows).saturating_mul(u32::from(ch));
    let win_w = u32::from(cols).saturating_mul(u32::from(cw));
    if buf.starts_with(b"\x1b[16t") {
        return Some((5, format!("\x1b[6;{ch};{cw}t").into_bytes()));
    }
    if buf.starts_with(b"\x1b[14t") {
        return Some((5, format!("\x1b[4;{win_h};{win_w}t").into_bytes()));
    }
    if buf.starts_with(b"\x1b[18t") {
        return Some((5, format!("\x1b[8;{rows};{cols}t").into_bytes()));
    }
    // RESET: we forward cell-coordinate SGR. Claiming SET (1016) makes herdr
    // interpret those reports as pixels and every click lands in the first cell.
    if buf.starts_with(b"\x1b[?1016$p") {
        return Some((8, b"\x1b[?1016;2$y".to_vec()));
    }
    None
}

fn note_mouse_mode(buf: &[u8], pixel_mouse: &AtomicBool) {
    // Combined DECSET/DECRST: look for 1016 followed by h or l in a CSI ?.
    let text = String::from_utf8_lossy(buf);
    if text.contains("1016h") {
        pixel_mouse.store(true, Ordering::Relaxed);
    }
    if text.contains("1016l") {
        pixel_mouse.store(false, Ordering::Relaxed);
    }
}

pub(crate) fn herdr_binary() -> String {
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
    let config_path = super::config::session_dir().map(|d| d.join("config.toml"));
    std::thread::spawn(move || {
        let mut cmd = std::process::Command::new(binary);
        cmd.args(["--session", super::config::SESSION, "server", "reload-config"]);
        if let Some(path) = config_path {
            cmd.env("HERDR_CONFIG_PATH", path);
        }
        let _ = cmd
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
        (Pane::spawn_command(cmd, 10, 40, tx, None).unwrap(), rx)
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
    fn decset_1016_turns_pixel_mouse_on() {
        let flag = AtomicBool::new(false);
        note_mouse_mode(b"\x1b[?1000;1002;1006;1016h", &flag);
        assert!(flag.load(Ordering::Relaxed));
        note_mouse_mode(b"\x1b[?1016l", &flag);
        assert!(!flag.load(Ordering::Relaxed));
    }

    #[test]
    fn pixel_mouse_decrqm_is_answered_reset_so_clicks_stay_in_cells() {
        let (len, reply) = query_reply_at(b"\x1b[?1016$p extra", (24, 80)).unwrap();
        assert_eq!(len, 8);
        assert_eq!(reply, b"\x1b[?1016;2$y");
    }

    #[test]
    fn host_queries_are_consumed_so_they_are_not_answered_twice() {
        let (len, _) = query_reply_at(b"\x1b[16t\x1b[16t", (24, 80)).unwrap();
        assert_eq!(len, 5, "only the first query is consumed");
    }

    #[test]
    fn a_missing_binary_is_an_error_not_a_panic() {
        let (tx, _rx) = std::sync::mpsc::channel();
        let cmd = CommandBuilder::new("/nonexistent/herdr-binary");
        assert!(Pane::spawn_command(cmd, 10, 40, tx, None).is_err());
    }

    #[test]
    fn which_finds_a_real_binary_and_misses_a_fake_one() {
        assert!(which("sh").is_some());
        assert!(which("definitely-not-a-real-binary-xyz").is_none());
    }
}
