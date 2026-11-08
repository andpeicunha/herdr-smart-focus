#!/usr/bin/env python3
"""smart-focus: grow the focused pane along its root->pane split path.

Subcommands: daemon | ensure | toggle | enable | disable | status
layout = "keep" (default) uses only layout.export + layout.set_split_ratio.
layout = "stack-bottom" | "stack-right" additionally uses pane.swap and
pane.move (never layout.apply), so panes and processes are never recreated.
"""
import fcntl
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smart_focus_core as core  # noqa: E402

DEBOUNCE_S = 0.08
SELF_SUPPRESS_S = 0.5
RECONNECT_GIVEUP_S = 60
SUBSCRIPTIONS = ["pane.focused", "pane.created", "pane.closed", "layout.updated", "tab.focused"]


# ---------- paths / config / state ----------

def state_dir():
    d = (os.environ.get("HERDR_SMART_FOCUS_STATE_DIR")
         or os.environ.get("HERDR_PLUGIN_STATE_DIR")
         or os.path.expanduser("~/.local/state/herdr/plugins/andpeicunha.smart-focus"))
    os.makedirs(d, exist_ok=True)
    return d


def config_dir():
    d = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if d:
        return d
    d = os.path.expanduser("~/.config/herdr/plugins/config/andpeicunha.smart-focus")
    if os.path.isdir(d):
        return d
    try:
        out = subprocess.run(["herdr", "plugin", "config-dir", "andpeicunha.smart-focus"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def socket_path():
    return (os.environ.get("HERDR_SOCKET_PATH")
            or os.path.expanduser("~/.config/herdr/herdr.sock"))


def read_config():
    cfg_dir = config_dir()
    if not cfg_dir:
        return {}
    try:
        with open(os.path.join(cfg_dir, "config.toml"), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        out = {}
        for line in text.splitlines():
            k, _, v = line.partition("=")
            v = v.split("#")[0].strip().strip('"').strip("'")
            if k.strip() and v:
                out[k.strip()] = v
        return out
    except ValueError:
        return {}


def load_layout():
    env = os.environ.get("HERDR_SMART_FOCUS_LAYOUT")
    return core.normalize_layout(env if env else read_config().get("layout"))


def load_stack_ratio():
    env = os.environ.get("HERDR_SMART_FOCUS_STACK_RATIO")
    return core.clamp_stack_ratio(env if env else read_config().get("stack_ratio", core.DEFAULT_STACK_RATIO))


def load_ratio():
    env = os.environ.get("HERDR_SMART_FOCUS_RATIO")
    if env:
        return core.clamp_ratio(env)
    cfg_dir = config_dir()
    if cfg_dir:
        try:
            with open(os.path.join(cfg_dir, "config.toml"), "r", encoding="utf-8") as fh:
                text = fh.read()
            try:
                import tomllib
                return core.clamp_ratio(tomllib.loads(text).get("focus_ratio", core.DEFAULT_RATIO))
            except ImportError:
                for line in text.splitlines():
                    k, _, v = line.partition("=")
                    if k.strip() == "focus_ratio":
                        return core.clamp_ratio(v.split("#")[0].strip())
        except (OSError, ValueError):
            pass
    return core.DEFAULT_RATIO


def load_balance():
    env = os.environ.get("HERDR_SMART_FOCUS_BALANCE")
    if env:
        return env.strip().lower() not in ("0", "false", "no", "off")
    cfg_dir = config_dir()
    if cfg_dir:
        try:
            with open(os.path.join(cfg_dir, "config.toml"), "r", encoding="utf-8") as fh:
                text = fh.read()
            try:
                import tomllib
                return bool(tomllib.loads(text).get("balance_others", True))
            except ImportError:
                for line in text.splitlines():
                    k, _, v = line.partition("=")
                    if k.strip() == "balance_others":
                        return v.split("#")[0].strip().lower() not in ("false", "0", "no", "off")
        except (OSError, ValueError):
            pass
    return True


def _state_file():
    return os.path.join(state_dir(), "tabs.json")


def load_enabled():
    try:
        with open(_state_file(), "r", encoding="utf-8") as fh:
            return set(json.load(fh).get("enabled", []))
    except (OSError, ValueError):
        return set()


def save_enabled(tabs):
    tmp = _state_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"enabled": sorted(tabs)}, fh)
    os.replace(tmp, _state_file())


# ---------- herdr socket ----------

class HerdrError(Exception):
    pass


def request(method, params=None, timeout=5):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(socket_path())
        s.sendall((json.dumps({"id": "sf", "method": method, "params": params or {}}) + "\n").encode())
        line = s.makefile("r", encoding="utf-8").readline()
    finally:
        s.close()
    if not line:
        raise HerdrError("empty response")
    resp = json.loads(line)
    if "error" in resp:
        raise HerdrError("%s: %s" % (resp["error"].get("code"), resp["error"].get("message")))
    return resp["result"]


def export_layout(**target):
    return request("layout.export", target)["layout"]


def notify(title, body=""):
    try:
        request("notification.show", {"title": title, "body": body})
    except Exception:
        pass


# ---------- applying a layout ----------

TMP_TAB_LABEL = "smart-focus-tmp"


def tab_is_focused(tab_id):
    try:
        for t in request("tab.list")["tabs"]:
            if t.get("tab_id") == tab_id:
                return bool(t.get("focused"))
    except (HerdrError, KeyError, OSError):
        pass
    return False


def _recover(tab_id, tmp, log):
    """Best effort: bring panes stranded in the scratch tab back."""
    try:
        stranded = core.leaves(export_layout(tab_id=tmp)["root"])
        anchor = core.leaves(export_layout(tab_id=tab_id)["root"])[0]
    except (HerdrError, KeyError, IndexError, OSError):
        return
    for p in stranded:
        try:
            request("pane.move", {"pane_id": p, "focus": False, "destination": {
                "type": "tab", "tab_id": tab_id, "split": "right", "target_pane_id": anchor}})
        except HerdrError as exc:
            log("recover failed %s: %s" % (p, exc))


def run_stack_ops(tab_id, ops, log):
    """Execute core.plan_stack ops. Returns the pane to expect a pane.focused for."""
    tmp = last_out = None
    expect = None
    try:
        for op in ops:
            kind = op[0]
            if kind == "swap":
                if not tab_is_focused(tab_id):
                    continue  # pane.swap focuses the tab; don't steal the view
                request("pane.swap", {"source_pane_id": op[1], "target_pane_id": op[2]})
                expect = op[1]
            elif kind == "move_out":
                dest = ({"type": "new_tab", "label": TMP_TAB_LABEL} if tmp is None else
                        {"type": "tab", "tab_id": tmp, "split": "right", "target_pane_id": last_out})
                res = request("pane.move", {"pane_id": op[1], "destination": dest, "focus": False})
                tmp = tmp or res["move_result"]["pane"]["tab_id"]
                last_out = op[1]
            elif kind == "move_in":
                request("pane.move", {"pane_id": op[1], "focus": False, "destination": {
                    "type": "tab", "tab_id": tab_id, "split": op[2], "target_pane_id": op[3]}})
            elif kind == "focus":
                request("pane.focus", {"pane_id": op[1]})
                expect = op[1]
            elif kind == "ratio":
                request("layout.set_split_ratio", {"tab_id": tab_id, "path": op[1], "ratio": op[2]})
    except (HerdrError, KeyError) as exc:
        log("stack op failed %s: %s" % (tab_id, exc))
        if tmp:
            _recover(tab_id, tmp, log)
    return expect


def apply_layout(layout, pane_id, log):
    """Apply the configured mode to an exported layout.
    Returns (did_something, pane_expected_to_emit_focus)."""
    mode = load_layout()
    tab_id = layout["tab_id"]
    if mode == "keep":
        ops = core.compute_ops(layout["root"], pane_id, load_ratio(), balance_others=load_balance(),
                                clamp_targets=True)
        for path, ratio in ops:
            try:
                request("layout.set_split_ratio", {"tab_id": tab_id, "path": path, "ratio": ratio})
            except HerdrError as exc:
                log("set_split_ratio failed %s %s: %s" % (tab_id, path, exc))
                break
        return bool(ops), None
    ops = core.plan_stack(layout["root"], pane_id, mode, load_stack_ratio())
    if not ops:
        return False, None
    log("stack %s tab=%s focus=%s ops=%s" % (mode, tab_id, pane_id, [o[0] for o in ops]))
    return True, run_stack_ops(tab_id, ops, log)


# ---------- daemon ----------

class Daemon:
    def __init__(self):
        self.q = queue.Queue()
        self.shapes = {}       # tab_id -> last seen shape signature
        self.suppress = {}     # tab_id -> monotonic deadline (our own layout.updated)
        self.expect_focus = {}  # pane_id -> deadline (pane.focused caused by our swap/focus)
        self.op_window = (0.0, 0.0)  # monotonic [start, end] of our last structural ops

    def log(self, msg):
        print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

    # -- applying --
    def apply_for(self, layout, force_pane=None):
        tab_id = layout["tab_id"]
        root = layout["root"]
        self.shapes[tab_id] = core.shape_signature(root)
        if tab_id not in load_enabled() or layout.get("zoomed"):
            return
        pane_id = force_pane or layout.get("focused_pane_id")
        self.suppress[tab_id] = time.monotonic() + SELF_SUPPRESS_S
        started = time.monotonic()
        did, expect = apply_layout(layout, pane_id, self.log)
        if did:
            # pane.focused emitted while we move/swap panes are ours, not the user's
            self.op_window = (started, time.monotonic() + DEBOUNCE_S * 2)
        if not did:
            self.suppress.pop(tab_id, None)
            return
        try:  # remember the post-op shape so our own changes are not "structural"
            self.shapes[tab_id] = core.shape_signature(export_layout(tab_id=tab_id)["root"])
        except (HerdrError, KeyError, OSError):
            self.shapes.pop(tab_id, None)
        if expect:
            self.expect_focus[expect] = time.monotonic() + SELF_SUPPRESS_S
        self.suppress[tab_id] = time.monotonic() + SELF_SUPPRESS_S

    def handle_batch(self, events):
        focus_panes, layout_tabs, any_tab_focus = [], [], False
        for name, data, ts in events:
            if name == "pane_focused" and data.get("pane_id"):
                if self.op_window[0] <= ts <= self.op_window[1]:
                    continue  # echo of our own move/swap/focus
                if time.monotonic() < self.expect_focus.get(data["pane_id"], 0):
                    continue
                focus_panes.append(data["pane_id"])
            elif name == "tab_focused":
                any_tab_focus = True
            elif name == "layout_updated":
                tab_id = (data.get("layout") or {}).get("tab_id") or data.get("tab_id")
                if tab_id:
                    layout_tabs.append(tab_id)
            elif name == "pane_created":
                pane = data.get("pane") or {}
                pid = pane.get("pane_id") or data.get("pane_id")
                tab_id = pane.get("tab_id") or data.get("tab_id")
                if tab_id:
                    layout_tabs.append(tab_id)
                elif pid:
                    focus_panes.append(pid)
        done_tabs = set()
        if focus_panes:
            pane_id = focus_panes[-1]
            try:
                layout = export_layout(pane_id=pane_id)
                done_tabs.add(layout["tab_id"])
                self.apply_for(layout, force_pane=pane_id)
            except HerdrError as exc:
                self.log("focus export failed %s: %s" % (pane_id, exc))
        if any_tab_focus:
            try:
                layout = export_layout()
                if layout["tab_id"] not in done_tabs:
                    done_tabs.add(layout["tab_id"])
                    self.apply_for(layout)
            except HerdrError as exc:
                self.log("tab focus export failed: %s" % exc)
        for tab_id in dict.fromkeys(layout_tabs):
            if tab_id in done_tabs:
                continue
            if time.monotonic() < self.suppress.get(tab_id, 0):
                continue  # our own set_split_ratio echo
            try:
                layout = export_layout(tab_id=tab_id)
            except HerdrError:
                self.shapes.pop(tab_id, None)
                continue
            # Only re-apply on structural change; a pure ratio change is a
            # manual resize and is respected until the next focus change.
            if self.shapes.get(tab_id) != core.shape_signature(layout["root"]):
                self.apply_for(layout)

    # -- event stream --
    def reader(self):
        """Connect + subscribe, push events on the queue. Returns on EOF/error."""
        s = socket.socket(socket.AF_UNIX)
        s.connect(socket_path())
        sub = {"id": "sub", "method": "events.subscribe",
               "params": {"subscriptions": [{"type": t} for t in SUBSCRIPTIONS]}}
        s.sendall((json.dumps(sub) + "\n").encode())
        for line in s.makefile("r", encoding="utf-8"):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "error" in msg:
                raise HerdrError(str(msg["error"]))
            ev = msg.get("event")
            if ev:
                self.q.put((ev.replace(".", "_"), msg.get("data") or {}, time.monotonic()))
        raise HerdrError("stream closed")

    def reader_loop(self):
        failing_since = None
        while True:
            try:
                self.reader()
            except Exception as exc:  # noqa: BLE001
                now = time.monotonic()
                failing_since = failing_since or now
                if now - failing_since > RECONNECT_GIVEUP_S:
                    self.log("giving up reconnect: %s" % exc)
                    self.q.put(("__exit__", {}, 0.0))
                    return
                self.log("reader: %s; retrying" % exc)
                time.sleep(2)
            else:
                failing_since = None

    def run(self):
        threading.Thread(target=self.reader_loop, daemon=True).start()
        self.log("daemon started pid=%d" % os.getpid())
        while True:
            batch = [self.q.get()]
            while True:  # debounce: wait for DEBOUNCE_S of quiet
                try:
                    batch.append(self.q.get(timeout=DEBOUNCE_S))
                except queue.Empty:
                    break
            if any(b[0] == "__exit__" for b in batch):
                return
            try:
                self.handle_batch(batch)
            except Exception as exc:  # noqa: BLE001
                self.log("batch error: %r" % exc)


def acquire_lock():
    fd = os.open(os.path.join(state_dir(), "daemon.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def daemon_pid():
    """PID of the running daemon, or None (probes the flock)."""
    path = os.path.join(state_dir(), "daemon.lock")
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return int(os.pread(fd, 32, 0).decode() or 0) or None
            except ValueError:
                return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def cmd_daemon():
    lock = acquire_lock()
    if lock is None:
        return 0  # another daemon owns the lock
    Daemon().run()
    return 0


def cmd_ensure():
    if daemon_pid():
        return 0
    log = open(os.path.join(state_dir(), "daemon.log"), "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True)
    return 0


# ---------- actions ----------

def resolve_tab():
    tab = os.environ.get("HERDR_TAB_ID")
    if tab:
        return tab
    pane = os.environ.get("HERDR_PANE_ID")
    try:
        return export_layout(pane_id=pane)["tab_id"] if pane else export_layout()["tab_id"]
    except HerdrError:
        return None


def cmd_toggle_like(action):
    tab = resolve_tab()
    if not tab:
        print("smart-focus: cannot resolve tab", file=sys.stderr)
        return 1
    enabled = load_enabled()
    want = (tab not in enabled) if action == "toggle" else (action == "enable")
    (enabled.add if want else enabled.discard)(tab)
    save_enabled(enabled)
    cmd_ensure()
    msg = "smart focus %s for %s (ratio %.2f)" % ("ON" if want else "OFF", tab, load_ratio())
    print(msg)
    notify("smart focus", msg)
    if want:  # apply right away for the current focus
        try:
            layout = export_layout(tab_id=tab)
            apply_layout(layout, layout.get("focused_pane_id"),
                         lambda m: print(m, file=sys.stderr))
        except HerdrError as exc:
            print("apply failed: %s" % exc, file=sys.stderr)
    return 0


def cmd_status():
    tab = resolve_tab()
    info = {"daemon_pid": daemon_pid(), "tab_id": tab,
            "tab_enabled": tab in load_enabled(), "focus_ratio": load_ratio(),
            "layout": load_layout(), "stack_ratio": load_stack_ratio(),
            "balance_others": load_balance(),
            "enabled_tabs": sorted(load_enabled()), "state_dir": state_dir()}
    out = json.dumps(info, indent=2)
    print(out)
    notify("smart focus status", "daemon=%s tab=%s ratio=%.2f" % (
        info["daemon_pid"] or "down", "on" if info["tab_enabled"] else "off", info["focus_ratio"]))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "daemon":
        return cmd_daemon()
    if cmd == "ensure":
        return cmd_ensure()
    if cmd in ("toggle", "enable", "disable"):
        return cmd_toggle_like(cmd)
    if cmd == "status":
        return cmd_status()
    print("usage: smart_focus.py daemon|ensure|toggle|enable|disable|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
