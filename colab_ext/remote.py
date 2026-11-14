"""Run python inside a Colab runtime through colab_cli's own API.

Primary path: `colab_cli.runtime.ColabRuntime.execute_code()` (no subprocess, no temp file).
Fallback path: the `colab exec` CLI, for the case where the extension is used from a plain
script instead of inside the CLI process.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text or "")


def resolve_session(name: str | None = None) -> str:
    try:
        from colab_cli.common import state

        return state.resolve_session(name)
    except Exception:
        return name or "default"


def _outputs_to_text(outputs: list) -> str:
    chunks: list[str] = []
    for o in outputs or []:
        kind = o.get("output_type")
        if kind == "stream":
            chunks.append(o.get("text", ""))
        elif kind in ("execute_result", "display_data"):
            chunks.append(str(o.get("data", {}).get("text/plain", "")))
        elif kind == "error":
            chunks.append("\n".join(o.get("traceback", [])))
    return strip_ansi("\n".join(chunks))


# Render a remote snippet for a given mountpoint. Placeholder-based (not %-formatting)
# because the snippets themselves contain '%' shell/Python formatting.
def render(snippet: str, mountpoint: str) -> str:
    return snippet.replace("__MP__", repr(mountpoint))


def _wait_ws_ready(runtime, ws_timeout: float = 45.0) -> None:
    """Ensure the kernel WebSocket is actually open before sending execute_request.

    `jupyter_kernel_client.wsclient.KernelWebSocketClient.start_channels()` only waits
    `REQUEST_TIMEOUT` (10s) on `connection_ready` and silently continues when that wait
    times out, causing `execute_interactive()` to immediately raise
    `RuntimeError("Connection was lost.")` on a cold-starting Colab VM where the
    WebSocket handshake takes 10-30s.
    """
    kc = runtime.kernel_client
    wsclient = getattr(getattr(kc, "_manager", None), "client", None)
    ready = getattr(wsclient, "connection_ready", None)
    if ready is None or ready.is_set():
        return
    deadline = time.monotonic() + max(5.0, ws_timeout)
    while time.monotonic() < deadline:
        if ready.wait(timeout=min(1.0, max(0.1, deadline - time.monotonic()))):
            return
        thread = getattr(wsclient, "connection_thread", None)
        if thread is not None and not thread.is_alive():
            break
    raise RuntimeError("Connection was lost (websocket handshake timed out).")


def _is_transient_conn_error(exc: BaseException, elapsed: float, budget: float) -> bool:
    """True for cold-start / WebSocket drop errors that are safe to retry."""
    msg = str(exc)
    if isinstance(exc, RuntimeError) and any(
        s in msg for s in ("Connection was lost", "IOPub channel", "Kernel didn't respond")
    ):
        return True
    if isinstance(exc, TimeoutError) and elapsed < budget * 0.8:
        return True
    # requests.exceptions.RequestException (ConnectTimeout, ReadTimeout, 502/503/504, etc.)
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status in (401, 403, 404):
        return False
    if exc.__class__.__module__.startswith("requests"):
        return True
    return False


def exec_code(name: str | None, code: str, timeout: int = 180, retries: int = 3) -> str:
    """Execute `code` in the session's kernel and return its stdout/stderr text."""
    try:
        from colab_cli.common import state
        from colab_cli.runtime import ColabRuntime
    except ImportError:
        return _exec_cli(name, code, timeout)
    session = state.resolve_session(name)
    for attempt in range(max(1, retries)):
        st = state.get_session(session)
        runtime = ColabRuntime(st.url, st.token, session_name=session, history=state.history)
        t0 = time.monotonic()
        try:
            _wait_ws_ready(runtime, ws_timeout=min(45.0, float(timeout)))
            return _outputs_to_text(runtime.execute_code(code, timeout=timeout))
        except Exception as exc:
            elapsed = time.monotonic() - t0
            if attempt >= retries - 1 or not _is_transient_conn_error(exc, elapsed, float(timeout)):
                raise
            delay = 2 ** (attempt + 1)
            print(f"[drive] kernel connection not ready ({exc}); retrying in {delay}s "
                  f"({attempt + 1}/{retries}) ...", file=sys.stderr, flush=True)
            time.sleep(delay)
        finally:
            # Official commands do this in a finally too; without it the kernel client's
            # (non-daemon) threads keep the colab process alive after the command finished.
            try:
                runtime.stop()
            except Exception:
                pass
    return ""


def _exec_cli(name: str | None, code: str, timeout: int) -> str:
    """Fallback: `colab exec -s <name> -f <tmpfile>`."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        cmd = [os.environ.get("COLAB_BIN", "colab"), "exec", "--timeout", str(timeout)]
        if name:
            cmd += ["-s", name]
        cmd += ["-f", path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return strip_ansi((proc.stdout or "") + (proc.stderr or ""))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- small remote snippets used by `colab drive status` / `colab drive umount` -------------
STATUS_REMOTE = r'''
import os, subprocess, json
MP = __MP__
def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    return ((r.stdout or "") + (r.stderr or "")).strip()
print("DFS_RESULT " + json.dumps({
  "mountpoint": MP,
  "mounted": os.path.ismount(MP),
  "mount_line": [l for l in open("/proc/mounts") if "fuse" in l],
  "drive_procs": sh("pgrep -c -x drive || echo 0"),
  "shim_procs": sh("pgrep -c -f [d]fs_creds_shim.py || echo 0"),
  "shim_log": sh("tail -3 /tmp/dfs_creds_shim.log 2>/dev/null | cut -c1-90"),
  "entries": (len(os.listdir(os.path.join(MP, "My Drive")))
              if os.path.isdir(os.path.join(MP, "My Drive")) else None),
  "ephem_creds_addr": os.environ.get("TBE_EPHEM_CREDS_ADDR"),
  "official_env": {k: v for k, v in os.environ.items() if k in
                   ("TBE_CREDS_ADDR", "TBE_EPHEM_CREDS_ADDR", "USE_AUTH_EPHEM")},
}))
'''

UMOUNT_REMOTE = r'''
import os, subprocess, time, json
MP = __MP__
def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    return ((r.stdout or "") + (r.stderr or "")).strip()
for cmd in ("fusermount -u", "fusermount3 -u", "umount -i -f"):
    sh("%s %s" % (cmd, MP))
    if not os.path.ismount(MP):
        break
try:
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        cl = " ".join(p.info["cmdline"] or [])
        if p.info["name"] == "drive" or "dfs_creds_shim" in cl:
            try:
                p.kill()
            except Exception:
                pass
except ImportError:
    sh("pkill -9 -x drive; pkill -9 -f '[d]fs_creds_shim.py'")
time.sleep(1)
sh("rm -rf %s" % MP)
print("DFS_RESULT " + json.dumps({"mountpoint": MP, "mounted": os.path.ismount(MP),
                                  "gone": not os.path.exists(MP)}))
'''
