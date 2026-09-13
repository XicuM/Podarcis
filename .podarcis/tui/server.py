'''Daemonize a named herdr server and talk to it over CLI / socket.'''

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from podarcis.tui import SESSION_NAME


def package_herdr_dir() -> Path:
    '''Engine package data: ``.podarcis/herdr`` next to this module's package.'''
    return Path(__file__).resolve().parent.parent / 'herdr'


def session_dir() -> Path:
    return Path.home() / '.config' / 'herdr' / 'sessions' / SESSION_NAME


def session_sock(root: Path | None = None) -> Path:
    return (root or session_dir()) / 'herdr.sock'


def session_config(root: Path | None = None) -> Path:
    return (root or session_dir()) / 'config.toml'


def session_template() -> Path:
    return package_herdr_dir() / 'session.toml'


def ensure_session_config(*, reset: bool = False, dest: Path | None = None) -> Path:
    '''Copy the tracked template into the user session dir if missing.

    Never writes into the checkout: herdr mutates the live config (onboarding).
    '''
    path = dest if dest is not None else session_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    src = session_template()
    if reset or not path.exists():
        shutil.copy2(src, path)
    return path


def _merge_missing_herdr(src: Path, dest: Path) -> None:
    '''Copy files that exist in package data but not in a stale checkout tree.'''
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in ('__pycache__',) or item.suffix == '.pyc':
            continue
        target = dest / item.name
        if item.is_dir():
            _merge_missing_herdr(item, target)
        elif not target.exists():
            shutil.copy2(item, target)


def herdr_dir_has_flavors(path: Path) -> bool:
    return (path / 'flavors' / 'yazi' / 'yazi.toml').is_file() or (
        path / 'flavors' / 'nvim' / 'wiki.lua'
    ).is_file()


def ensure_checkout_herdr(wiki_root: Path) -> Path:
    '''Copy package ``herdr/**`` into ``$WIKI_ROOT/.podarcis/herdr`` if missing files.'''
    dest = Path(wiki_root) / '.podarcis' / 'herdr'
    src = package_herdr_dir()
    if not src.is_dir():
        return dest if dest.is_dir() else src
    if src.resolve() == dest.resolve():
        return dest
    if not dest.is_dir():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        return dest
    _merge_missing_herdr(src, dest)
    return dest


def resolved_herdr_dir(wiki_root: Path | None = None) -> Path:
    '''Checkout flavors if present, else package data (PR1 trees lack flavors).'''
    pkg = package_herdr_dir()
    if wiki_root is not None:
        checkout = Path(wiki_root) / '.podarcis' / 'herdr'
        if herdr_dir_has_flavors(checkout):
            return checkout
    return pkg


def plugin_link_dir(root: Path | None = None) -> Path:
    return (root or session_dir()) / 'plugin'


def _toml_basic_string(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_linked_plugin(
    herdr_dir: Path,
    *,
    python: str | None = None,
    dest: Path | None = None,
) -> Path:
    '''Copy the manifest into the user session dir, rewriting ``python3`` to ``sys.executable``.'''
    python = python or sys.executable
    dest = dest if dest is not None else plugin_link_dir()
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(herdr_dir) / 'herdr-plugin.toml'
    if not src.is_file():
        src = package_herdr_dir() / 'herdr-plugin.toml'
    text = src.read_text(encoding='utf-8')
    text = text.replace('"python3"', _toml_basic_string(python))
    (dest / 'herdr-plugin.toml').write_text(text, encoding='utf-8')
    return dest


def ensure_herdr_server(
    herdr_bin: str,
    *,
    sock: Path | None = None,
    config: Path | None = None,
    timeout_s: float = 3.0,
    interval_s: float = 0.1,
    environ: dict[str, str] | None = None,
) -> subprocess.Popen | None:
    '''Start ``herdr --session podarcis server`` detached and poll the socket.

    Never ``subprocess.run`` the server — it is a long-lived foreground process.
    Returns the spawned process, or None if the socket already existed.
    '''
    sock_path = sock if sock is not None else session_sock()
    cfg_path = config if config is not None else session_config()
    if sock_path.exists():
        return None

    env = dict(os.environ if environ is None else environ)
    env['HERDR_CONFIG_PATH'] = str(cfg_path)
    env['HERDR_SESSION'] = SESSION_NAME
    proc = subprocess.Popen(
        [herdr_bin, '--session', SESSION_NAME, 'server'],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sock_path.exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError(
                f'herdr server exited before creating {sock_path} (status {proc.returncode}).'
            )
        time.sleep(interval_s)

    _kill_group(proc)
    raise RuntimeError(f'herdr server did not create sock {sock_path} within {timeout_s:.0f}s.')


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _unwrap(data: object) -> dict:
    if not isinstance(data, dict):
        raise RuntimeError(f'herdr returned non-object JSON: {data!r}')
    if data.get('error'):
        err = data['error']
        if isinstance(err, dict):
            raise RuntimeError(err.get('message') or str(err))
        raise RuntimeError(str(err))
    result = data.get('result', data)
    if not isinstance(result, dict):
        raise RuntimeError(f'herdr result is not an object: {result!r}')
    return result


def _loads_json(text: str) -> dict:
    text = (text or '').strip()
    if not text:
        return {}
    try:
        return _unwrap(json.loads(text))
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                return _unwrap(json.loads(line))
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f'herdr produced no JSON:\n{text}')


class HerdrSession:
    '''CLI + socket client pinned to session ``podarcis``.'''

    def __init__(self, herdr_bin: str, *, env: dict[str, str] | None = None, sock: Path | None = None):
        self.herdr_bin = herdr_bin
        self.env = env if env is not None else os.environ.copy()
        self.sock = sock if sock is not None else session_sock()

    def cli(self, *args: str) -> dict:
        cmd = [self.herdr_bin, '--session', SESSION_NAME, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=self.env, check=False)
        stdout = (proc.stdout or '').strip()
        stderr = (proc.stderr or '').strip()
        if proc.returncode != 0:
            for payload in (stdout, stderr):
                if not payload.startswith('{'):
                    continue
                try:
                    return _unwrap(json.loads(payload))
                except (json.JSONDecodeError, RuntimeError) as exc:
                    if isinstance(exc, RuntimeError):
                        raise
            raise RuntimeError(
                stderr or stdout or f'herdr {" ".join(args)} failed (status {proc.returncode})'
            )
        return _loads_json(proc.stdout)

    def rpc(self, method: str, params: dict | None = None, *, timeout_s: float = 10.0) -> dict:
        req = {
            'id': f'podarcis-{uuid.uuid4().hex[:8]}',
            'method': method,
            'params': params or {},
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect(str(self.sock))
            sock.sendall((json.dumps(req) + '\n').encode())
            buf = b''
            while b'\n' not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf.strip():
            raise RuntimeError(f'herdr rpc {method} returned empty response')
        return _unwrap(json.loads(buf.decode()))
