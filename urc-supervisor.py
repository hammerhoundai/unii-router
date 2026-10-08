#!/usr/bin/env python3
"""Run urc's client, auto-starting its server only while the client runs."""

import fcntl
import http.client
import os
import re
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path


START_TIMEOUT = 15.0
SIGNIN_RE = re.compile(
    r"sign-in link for\s+([^:]+):\s*(https?://\S+)", re.IGNORECASE
)


def log(message):
    print(f"urc: {message}", file=sys.stderr, flush=True)


def server_state(port):
    """Return 'ready', 'free', or 'foreign' for the local Unii server port."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read(64 * 1024)
        conn.close()
        if response.status == 200 and b"uniichat" in body.lower():
            return "ready"
        return "foreign"
    except OSError:
        return "free"


def read_log(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def watch_signin_links(log_path, stop):
    seen = set()
    announced_log = False
    while not stop.is_set():
        links = SIGNIN_RE.findall(read_log(log_path))
        if links:
            for who, link in links:
                key = (who.strip(), link)
                if key not in seen:
                    seen.add(key)
                    log(f"sign-in link for {who.strip()}: {link}")
                if not announced_log:
                    announced_log = True
                    log(f"server log: {log_path}")
        stop.wait(0.2)


def stop_server(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(30):
        if process.poll() is not None:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def tail_lines(path, count=12):
    lines = read_log(path).splitlines()
    return "\n".join(lines[-count:])


def start_server(launcher, port, state_dir, client_lock_path):
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    log_path = state_dir / f"auto-server-{port}.log"
    env = os.environ.copy()
    env["URC_AUTO_STOP_LOCK"] = str(client_lock_path)

    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        process = subprocess.Popen(
            [launcher, str(port)],
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    finally:
        os.close(fd)

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"auto-started server exited with status {process.returncode}\n"
                + tail_lines(log_path)
            )
        if server_state(port) == "ready":
            return process, log_path
        time.sleep(0.1)

    stop_server(process)
    raise RuntimeError(
        f"auto-started server did not become ready within {START_TIMEOUT:g}s\n"
        + tail_lines(log_path)
    )


def release_and_reap(process, client_lock, state_dir):
    """Drop this client's lease, then stop the server only if it was the last."""
    if client_lock is not None:
        client_lock.close()
    if process is None or process.poll() is not None:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        lock = open(state_dir / "auto-clients.lock", "a+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop_server(process)
        finally:
            lock.close()
    except BlockingIOError:
        # Another live client holds the server's lifetime lease.
        pass


def main():
    if len(sys.argv) < 2:
        log("internal error: missing server port")
        return 2

    try:
        port = int(sys.argv[1])
    except ValueError:
        log(f"invalid server port: {sys.argv[1]!r}")
        return 2

    launcher = os.environ.get("URC_SERVER_LAUNCHER")
    state_dir = Path(os.environ.get("URC_STATE_DIR", ""))
    if not launcher or not state_dir:
        log("internal error: launcher state was not initialized")
        return 2

    client_args = sys.argv[2:]
    process = None
    log_path = None
    client = None
    terminal_signal = None
    signin_stop = None
    client_lock = None

    def forward_signal(number, frame):
        nonlocal terminal_signal
        terminal_signal = number
        if client is not None and number != signal.SIGINT and client.poll() is None:
            client.send_signal(number)

    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, forward_signal)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        client_lock_path = state_dir / "auto-clients.lock"
        client_lock = open(client_lock_path, "a+b")
        try:
            os.chmod(client_lock_path, 0o600)
            fcntl.flock(client_lock.fileno(), fcntl.LOCK_SH)
            lock_path = state_dir / "auto-start.lock"
            with open(lock_path, "a+b") as lock:
                os.chmod(lock_path, 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                state = server_state(port)
                if state == "foreign":
                    raise RuntimeError(
                        f"127.0.0.1:{port} is occupied by a service that is not UniiChat"
                    )
                if state == "free":
                    process, log_path = start_server(
                        launcher, port, state_dir, client_lock_path
                    )
                    signin_stop = threading.Event()
                    threading.Thread(
                        target=watch_signin_links,
                        args=(log_path, signin_stop),
                        daemon=True,
                    ).start()
        except BaseException:
            client_lock.close()
            client_lock = None
            raise
    except RuntimeError as error:
        log(str(error))
        if signin_stop is not None:
            signin_stop.set()
        release_and_reap(process, client_lock, state_dir)
        return 2
    except Exception as error:
        log(f"unexpected startup error: {error!r}")
        if signin_stop is not None:
            signin_stop.set()
        release_and_reap(process, client_lock, state_dir)
        return 1

    if terminal_signal is not None:
        if signin_stop is not None:
            signin_stop.set()
        release_and_reap(process, client_lock, state_dir)
        return 128 + terminal_signal

    status = 1
    try:
        client = subprocess.Popen(["unii", *client_args])
        deadline = None
        while True:
            try:
                status = client.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if terminal_signal is None:
                    continue
                if deadline is None:
                    deadline = time.monotonic() + 3
                elif time.monotonic() >= deadline and client.poll() is None:
                    client.kill()
    except FileNotFoundError:
        log("unii is not in PATH")
        status = 2
    finally:
        if client is not None and client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
        if signin_stop is not None:
            signin_stop.set()
        release_and_reap(process, client_lock, state_dir)

    if status < 0:
        return 128 - status
    return status


if __name__ == "__main__":
    raise SystemExit(main())
