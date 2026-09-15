//! The agent control channel.
//!
//! An agent working in the herdr pane drives the front-end the same way the
//! front-end drives herdr: one JSON request per connection over a unix socket.
//! `podarcis tui open …` is the client; this is the server.
//!
//! The socket lives in the runtime directory rather than the checkout, because
//! it is per-process state with no business in a git tree, and it is exported
//! as `PODARCIS_TUI_SOCK` so anything spawned from the app — the herdr pane
//! above all — finds it without being told where to look.
//!
//! Requests carry their own reply channel, so the connection thread blocks
//! until the app answers. That is what lets `ask` be a real question: the
//! agent's CLI call does not return until a human has pressed a key.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Sender};
use std::time::Duration;

use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};

use crate::event::AppEvent;

/// What an agent asked the front-end to do.
#[derive(Clone, Debug)]
pub enum Command {
    /// Show a page, optionally scrolled to a source line.
    Open { path: String, line: Option<usize> },
    /// Mark source-line ranges of a page, or clear what is already marked.
    Highlight {
        /// `None` only ever means "clear every page".
        path: Option<String>,
        ranges: Vec<(usize, usize)>,
        label: Option<String>,
        clear: bool,
    },
    /// Put a question to the user and wait for the answer.
    Ask { question: String, options: Vec<String> },
}

impl Command {
    /// Whether the caller is waiting on a human rather than on the app.
    ///
    /// A blocking command is never acknowledged early: its whole point is the
    /// answer. Everything else replies as soon as the app has accepted it,
    /// even if the app cannot act on it until the editor is closed.
    pub fn blocking(&self) -> bool {
        matches!(self, Command::Ask { .. })
    }

    fn parse(value: &Value) -> Result<Self> {
        let cmd = value.get("cmd").and_then(Value::as_str).unwrap_or_default();
        match cmd {
            "open" => Ok(Command::Open {
                path: string_field(value, "path")?,
                line: value.get("line").and_then(Value::as_u64).map(|n| n as usize),
            }),
            "highlight" => {
                let clear = value.get("clear").and_then(Value::as_bool).unwrap_or(false);
                let path = value.get("path").and_then(Value::as_str).map(str::to_string);
                let mut ranges = Vec::new();
                if let Some(list) = value.get("ranges").and_then(Value::as_array) {
                    for entry in list {
                        let pair = entry
                            .as_array()
                            .filter(|p| p.len() == 2)
                            .ok_or_else(|| anyhow!("each range must be a [start, end] pair"))?;
                        let start = pair[0].as_u64().ok_or_else(|| anyhow!("range start must be a line number"))?;
                        let end = pair[1].as_u64().ok_or_else(|| anyhow!("range end must be a line number"))?;
                        if start == 0 || end < start {
                            bail!("ranges are 1-based and start must not exceed end");
                        }
                        ranges.push((start as usize, end as usize));
                    }
                }
                if !clear && ranges.is_empty() {
                    bail!("highlight needs at least one range, or --clear");
                }
                if !clear && path.is_none() {
                    bail!("highlight needs a path");
                }
                Ok(Command::Highlight {
                    path,
                    ranges,
                    label: value.get("label").and_then(Value::as_str).map(str::to_string),
                    clear,
                })
            }
            "ask" => {
                let options: Vec<String> = value
                    .get("options")
                    .and_then(Value::as_array)
                    .map(|list| list.iter().filter_map(Value::as_str).map(str::to_string).collect())
                    .unwrap_or_default();
                if options.is_empty() {
                    bail!("ask needs at least one option");
                }
                Ok(Command::Ask { question: string_field(value, "question")?, options })
            }
            "" => bail!("request has no `cmd`"),
            other => bail!("unknown command `{other}`"),
        }
    }
}

fn string_field(value: &Value, key: &str) -> Result<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(str::to_string)
        .ok_or_else(|| anyhow!("request needs a `{key}`"))
}

/// A command plus the channel its answer goes back down.
#[derive(Debug)]
pub struct Request {
    pub cmd: Command,
    reply: Sender<Value>,
}

impl Request {
    /// Answer the caller. A caller that has already given up (a client
    /// `--timeout`, or a killed agent) is not an error worth reporting: the
    /// app acted, and nobody is listening.
    pub fn reply(&self, payload: Value) {
        let _ = self.reply.send(payload);
    }

    pub fn ok(&self, payload: Value) {
        let mut merged = json!({"ok": true});
        if let (Some(target), Some(extra)) = (merged.as_object_mut(), payload.as_object()) {
            for (key, value) in extra {
                target.insert(key.clone(), value.clone());
            }
        }
        self.reply(merged);
    }

    pub fn err(&self, message: impl std::fmt::Display) {
        self.reply(json!({"ok": false, "error": message.to_string()}));
    }

    /// Acknowledge a non-blocking command the app has accepted but cannot act
    /// on yet, so the agent is not left hanging on the user's editor.
    pub fn deferred(&self) {
        if !self.cmd.blocking() {
            self.ok(json!({"deferred": true}));
        }
    }

    #[cfg(test)]
    pub fn for_test(cmd: Command) -> (Self, std::sync::mpsc::Receiver<Value>) {
        let (tx, rx) = channel();
        (Self { cmd, reply: tx }, rx)
    }
}

/// The listening socket. Dropping it unlinks the socket file, so a crashed or
/// closed front-end does not leave a dead address behind for the next one.
pub struct Server {
    path: PathBuf,
}

impl Server {
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Where the socket for `root` lives.
pub fn socket_path(root: &Path) -> PathBuf {
    let dir = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        // SAFETY-adjacent: a uid-suffixed fallback keeps two users on one box
        // from colliding on a world-writable /tmp path.
        .unwrap_or_else(|| std::env::temp_dir().join(format!("podarcis-{}", unsafe { libc::getuid() })));
    dir.join("podarcis").join(format!("{}.sock", slug(root)))
}

/// A filesystem-safe, collision-resistant name for a checkout.
///
/// The directory name alone would collide between two checkouts called
/// `podarcis`; the full path is not a legal filename. Name plus a hash of the
/// path keeps the socket recognisable and still unique.
fn slug(root: &Path) -> String {
    let name: String = root
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("project")
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '-' })
        .collect();
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in root.as_os_str().as_encoded_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{name}-{hash:016x}")
}

/// Bind the control socket and serve it until the returned handle is dropped.
///
/// A socket file left behind by a dead front-end is removed and rebound; one
/// that still answers belongs to a live instance, and this instance simply
/// goes without a control channel rather than stealing it.
pub fn listen(root: &Path, tx: Sender<AppEvent>) -> Result<Server> {
    let path = socket_path(root);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    if path.exists() {
        if UnixStream::connect(&path).is_ok() {
            bail!("another front-end already owns {}", path.display());
        }
        std::fs::remove_file(&path)?;
    }
    let listener = UnixListener::bind(&path)?;
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let tx = tx.clone();
            // One thread per connection: a blocking `ask` must not stop the
            // next agent from reporting what it just wrote.
            std::thread::spawn(move || serve(stream, &tx));
        }
    });
    Ok(Server { path })
}

fn serve(stream: UnixStream, tx: &Sender<AppEvent>) {
    let mut writer = match stream.try_clone() {
        Ok(w) => w,
        Err(_) => return,
    };
    let mut line = String::new();
    if BufReader::new(stream).read_line(&mut line).is_err() {
        return;
    }
    let payload = match serde_json::from_str::<Value>(&line).map_err(anyhow::Error::from).and_then(|v| Command::parse(&v)) {
        Ok(cmd) => {
            let (reply_tx, reply_rx) = channel();
            if tx.send(AppEvent::Control(Request { cmd, reply: reply_tx })).is_err() {
                json!({"ok": false, "error": "front-end is shutting down"})
            } else {
                // The app answers on its next pass through the event loop; an
                // `ask` waits for a keypress, which is the point.
                reply_rx.recv().unwrap_or_else(|_| json!({"ok": false, "error": "no answer"}))
            }
        }
        Err(err) => json!({"ok": false, "error": err.to_string()}),
    };
    let _ = writeln!(writer, "{payload}");
    let _ = writer.flush();
}

/// Send one request to a running front-end and return its reply.
///
/// `timeout` bounds only the wait for an answer, not the connect: a question
/// nobody is at the keyboard to answer must not hang an agent forever.
pub fn send(path: &Path, request: &Value, timeout: Option<Duration>) -> Result<Value> {
    let stream = UnixStream::connect(path)?;
    stream.set_read_timeout(timeout)?;
    let mut writer = stream.try_clone()?;
    writeln!(writer, "{request}")?;
    writer.flush()?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line)?;
    Ok(serde_json::from_str(&line)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_needs_a_path() {
        assert!(Command::parse(&json!({"cmd": "open"})).is_err());
        assert!(Command::parse(&json!({"cmd": "open", "path": "wiki/a.md"})).is_ok());
    }

    #[test]
    fn a_highlight_without_ranges_is_only_legal_as_a_clear() {
        assert!(Command::parse(&json!({"cmd": "highlight", "path": "wiki/a.md"})).is_err());
        assert!(Command::parse(&json!({"cmd": "highlight", "clear": true})).is_ok());
    }

    #[test]
    fn ranges_are_one_based_and_ordered() {
        let bad = json!({"cmd": "highlight", "path": "a.md", "ranges": [[0, 4]]});
        assert!(Command::parse(&bad).is_err());
        let backwards = json!({"cmd": "highlight", "path": "a.md", "ranges": [[9, 4]]});
        assert!(Command::parse(&backwards).is_err());
        let good = json!({"cmd": "highlight", "path": "a.md", "ranges": [[4, 9]]});
        assert!(Command::parse(&good).is_ok());
    }

    #[test]
    fn ask_without_options_has_no_answer_to_give() {
        assert!(Command::parse(&json!({"cmd": "ask", "question": "merge?"})).is_err());
        let good = json!({"cmd": "ask", "question": "merge?", "options": ["yes", "no"]});
        assert!(Command::parse(&good).is_ok());
    }

    #[test]
    fn only_a_blocking_command_withholds_its_early_acknowledgement() {
        let (req, rx) = Request::for_test(Command::Open { path: "a.md".into(), line: None });
        req.deferred();
        assert_eq!(rx.try_recv().unwrap()["deferred"], json!(true));

        let (req, rx) = Request::for_test(Command::Ask {
            question: "merge?".into(),
            options: vec!["yes".into()],
        });
        req.deferred();
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn two_checkouts_of_the_same_name_get_different_sockets() {
        let a = socket_path(Path::new("/home/one/podarcis"));
        let b = socket_path(Path::new("/home/two/podarcis"));
        assert_ne!(a, b);
        assert!(a.to_string_lossy().contains("podarcis-"));
    }

    #[test]
    fn a_request_round_trips_over_the_socket() {
        let root = std::env::temp_dir().join(format!("podarcis-control-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let (tx, rx) = channel();
        let server = listen(&root, tx).unwrap();
        let path = server.path().to_path_buf();

        let client = std::thread::spawn(move || {
            send(&path, &json!({"cmd": "open", "path": "wiki/a.md"}), Some(Duration::from_secs(5)))
        });

        let AppEvent::Control(request) = rx.recv().unwrap() else { panic!("not a control event") };
        assert!(matches!(request.cmd, Command::Open { .. }));
        request.ok(json!({"path": "wiki/a.md"}));

        let reply = client.join().unwrap().unwrap();
        assert_eq!(reply["ok"], json!(true));
        assert_eq!(reply["path"], json!("wiki/a.md"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn an_unparseable_request_is_answered_rather_than_dropped() {
        let root = std::env::temp_dir().join(format!("podarcis-control-bad-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let (tx, _rx) = channel();
        let server = listen(&root, tx).unwrap();
        let reply = send(server.path(), &json!({"cmd": "fly"}), Some(Duration::from_secs(5))).unwrap();
        assert_eq!(reply["ok"], json!(false));
        assert!(reply["error"].as_str().unwrap().contains("fly"));
        let _ = std::fs::remove_dir_all(&root);
    }
}
