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
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smart_focus_core as core  # noqa: E402

FAIL_LIMIT = 3  # consecutive failed stack applies before a tab is disabled
DEBOUNCE_S = 0.08
SELF_SUPPRESS_S = 0.5
RECONNECT_GIVEUP_S = 60
RENAME_RETRY_S = 1.0
BADGE_DEBOUNCE_S = 0.3
STATUS_REFRESH_S = 0.3


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


def load_auto_rename():
    env = os.environ.get("HERDR_SMART_FOCUS_AUTO_RENAME")
    return core.parse_bool(env if env else read_config().get("auto_rename", True))


def load_status_badges():
    env = os.environ.get("HERDR_SMART_FOCUS_STATUS_BADGES")
    return core.parse_bool(env if env else read_config().get("status_badges", True))


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


def _names_file():
    return os.path.join(state_dir(), "names.json")


def load_names():
    """{pane_id: name} the plugin itself applied via pane.rename."""
    try:
        with open(_names_file(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_names(names):
    tmp = _names_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(names, fh)
    os.replace(tmp, _names_file())


def _badges_file():
    return os.path.join(state_dir(), "badges.json")


def load_badges():
    """{pane_id: {"base", "applied"}} for panes whose label carries a badge."""
    try:
        with open(_badges_file(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, dict)
                and isinstance(v.get("base"), str) and isinstance(v.get("applied"), str)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_badges(badges):
    tmp = _badges_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(badges, fh)
    os.replace(tmp, _badges_file())


# ---------- herdr socket ----------

class HerdrError(Exception):
    pass


READ_ONLY = ("layout.export", "tab.list", "tab.get", "pane.list", "pane.get", "workspace.list",
             "workspace.get", "notification.show", "events.subscribe")


def _ids(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith(("pane_id", "tab_id", "workspace_id")) and isinstance(v, str):
                out.append(v)
            else:
                _ids(v, out)
    return out


def _guard(method, params):
    """Optional scope guard (HERDR_SMART_FOCUS_WS_PREFIX=<workspace id>): every
    mutating call must target ids inside that workspace. Used by the test
    harness so a bug can never touch another workspace."""
    prefix = os.environ.get("HERDR_SMART_FOCUS_WS_PREFIX")
    if not prefix or method in READ_ONLY:
        return
    ids = _ids(params or {}, [])
    if not ids or any(i != prefix and not i.startswith(prefix + ":") for i in ids):
        raise HerdrError("scope guard: %s refused for %r" % (method, ids))


_fault_counts = {}


def _fault(point, count=None):
    """Test hook: HERDR_SMART_FOCUS_FAULT=<point>:<nth>:<raise|kill|term>."""
    spec = os.environ.get("HERDR_SMART_FOCUS_FAULT")
    if not spec:
        return
    name, _, rest = spec.partition(":")
    nth, _, mode = rest.partition(":")
    if name != point or count != int(nth or 1):
        return
    if mode == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "term":
        os.kill(os.getpid(), signal.SIGTERM)
        return
    raise OSError("injected fault at %s #%s" % (point, count))


def request(method, params=None, timeout=5):
    _guard(method, params)
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


# ---------- auto rename ----------

def git_facts(cwd):
    """(toplevel, common_dir) as absolute real paths; (None, None) outside a work tree."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel", "--git-common-dir"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None, None
    lines = r.stdout.splitlines()
    if r.returncode != 0 or len(lines) < 2:
        return None, None
    return (os.path.realpath(lines[0]),
            os.path.realpath(os.path.join(cwd, lines[1])))


def _pane_base(p):
    cwd = p.get("cwd") or ""
    top, common = git_facts(cwd) if cwd else (None, None)
    return core.repo_display_name(cwd, top, common)


def auto_rename_tab(tab_id, order, log):
    """Enable/toggle-on trigger: name every unnamed pane of `tab_id` after its
    repo. `order` = pane ids in reading order. Only pane.rename is used;
    manual names are never touched."""
    panes = {p["pane_id"]: p for p in request("pane.list")["panes"]}
    infos = []
    for pid in order:
        p = panes.get(pid)
        if p and p.get("tab_id") == tab_id:
            infos.append({"pane_id": pid, "label": p.get("label"), "base": _pane_base(p)})
    applied = core.prune_applied(load_names(), set(panes))
    renames, new_applied = core.plan_renames(infos, applied)
    for pid, name in renames.items():
        try:
            request("pane.rename", {"pane_id": pid, "label": name})
        except RECOVERABLE as exc:
            log("pane.rename %s failed: %s" % (pid, exc))
            new_applied.pop(pid, None)
    save_names(new_applied)
    if renames:
        log("auto_rename %s: %s" % (tab_id, renames))


def auto_rename_new_pane(pane_id, log):
    """pane.created trigger: name ONLY this pane, once; others are untouched.
    Returns "retry" when the cwd is not known yet, else None."""
    panes = {p["pane_id"]: p for p in request("pane.list")["panes"]}
    p = panes.get(pane_id)
    if not p or p.get("tab_id") not in load_enabled():
        return None
    applied = core.prune_applied(load_names(), set(panes))
    if not core.should_manage(p.get("label"), applied.get(pane_id)):
        return None
    if not p.get("cwd"):
        return "retry"
    taken = [q.get("label") for q in panes.values()
             if q.get("tab_id") == p["tab_id"] and q["pane_id"] != pane_id and q.get("label")]
    name = core.name_for_new(_pane_base(p), taken)
    try:
        request("pane.rename", {"pane_id": pane_id, "label": name})
    except RECOVERABLE as exc:
        log("pane.rename %s failed: %s" % (pane_id, exc))
        return None
    applied[pane_id] = name
    save_names(applied)
    log("auto_rename new pane %s: %s" % (pane_id, name))
    return None


# ---------- status badges ----------

def sync_badges(log, pane_ids=None, tab_id=None, restore_tab=None):
    """Make labels match status. Targets: `pane_ids`, or every pane of `tab_id`,
    or (restore_tab) every pane of that tab with badges removed. Panes of tabs
    that are not enabled are restored. Only pane.rename; no-op when the label
    is already correct (so our own rename never causes more work)."""
    panes = {p["pane_id"]: p for p in request("pane.list")["panes"]}
    enabled = load_enabled() if restore_tab is None else set()
    state = core.prune_applied(load_badges(), set(panes))
    if pane_ids is not None:
        targets = [panes[i] for i in pane_ids if i in panes]
    else:
        want = restore_tab or tab_id
        targets = [p for p in panes.values() if p.get("tab_id") == want]
    changed = False
    for p in targets:
        pid = p["pane_id"]
        label = p.get("label")
        entry = state.get(pid)
        if p.get("tab_id") not in enabled or not load_status_badges():
            # never touch labels we did not badge (entry None) in a disabled tab
            new, rename = None, (core.plan_restore(label, entry) if entry else None)
        else:
            status = p.get("agent_status") if p.get("agent") else None
            fallback = None
            if not label and status and load_auto_rename() and p.get("cwd"):
                taken = [core.strip_prefix(q.get("label")) for q in panes.values()
                         if q.get("tab_id") == p.get("tab_id") and q["pane_id"] != pid
                         and q.get("label")]
                fallback = core.name_for_new(_pane_base(p), taken)
            if not label and not status:
                new, rename = None, None
            else:
                new, rename = core.plan_badge(label, status, entry, fallback)
        if rename is not None:
            try:
                request("pane.rename", {"pane_id": pid, "label": rename})
            except RECOVERABLE as exc:
                log("pane.rename %s failed: %s" % (pid, exc))
                continue
            log("badge %s: %r -> %r" % (pid, label, rename))
        if new != entry:
            changed = True
            if new:
                state[pid] = new
            else:
                state.pop(pid, None)
    if changed or set(load_badges()) != set(state):
        save_badges(state)


# ---------- applying a layout ----------

TMP_TAB_LABEL = "smart-focus-tmp"
RECOVERABLE = (HerdrError, OSError, ValueError, KeyError, IndexError, TypeError)
MAX_RECOVERY_ATTEMPTS = 5


class _Aborted(Exception):
    pass


def _journal_path():
    return os.path.join(state_dir(), "journal.json")


def journal_exists():
    return os.path.exists(_journal_path())


def journal_read():
    try:
        with open(_journal_path(), "r", encoding="utf-8") as fh:
            j = json.load(fh)
        return j if isinstance(j, dict) and j.get("tab") and j.get("order") else None
    except (OSError, ValueError):
        return None


def journal_write(j):
    tmp = _journal_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(j, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _journal_path())


def journal_clear():
    try:
        os.unlink(_journal_path())
    except OSError:
        pass


def tab_is_focused(tab_id):
    try:
        for t in request("tab.list")["tabs"]:
            if t.get("tab_id") == tab_id:
                return bool(t.get("focused"))
    except RECOVERABLE:
        pass
    return False


def _move_back(pane, split, target, log):
    for sp in (split, "down" if split == "right" else "right"):
        try:
            request("pane.move", {"pane_id": pane, "focus": False, "destination": {
                "type": "tab", "tab_id": target[0], "split": sp, "target_pane_id": target[1]}})
            return True
        except RECOVERABLE as exc:
            log("recover move %s failed (%s): %s" % (pane, sp, exc))
    return False


def recover_journal(journal, log):
    """Bring panes of `journal` stranded in the scratch tab back to the home
    tab. Returns (clean, recovered): clean = nothing left stranded (journal
    can be dropped). Only panes listed in the journal are ever moved."""
    home = journal["tab"]
    ws = home.split(":")[0]
    try:
        tabs = request("tab.list")["tabs"]
    except RECOVERABLE as exc:
        log("recover: tab.list failed: %s" % exc)
        return False, 0
    if not any(t.get("tab_id") == home for t in tabs):
        log("recover: home tab %s is gone; leaving scratch tab as is" % home)
        return True, 0
    cands = [t["tab_id"] for t in tabs if t.get("workspace_id") == ws and t.get("tab_id") != home
             and (t.get("tab_id") == journal.get("tmp_tab") or t.get("label") == TMP_TAB_LABEL)]
    stranded = []
    for tab in cands:
        try:
            stranded += core.leaves(export_layout(tab_id=tab)["root"])
        except RECOVERABLE as exc:
            log("recover: scratch tab %s unreadable: %s" % (tab, exc))
    stranded = [p for p in stranded if p in journal["order"]]
    if not stranded:
        return True, 0
    try:
        home_panes = core.leaves(export_layout(tab_id=home)["root"])
    except RECOVERABLE as exc:
        log("recover: home export failed: %s" % exc)
        return False, 0
    clean, n = True, 0
    for pane, split, target in core.recovery_moves(journal["order"], journal.get("anchor"),
                                                   stranded, home_panes):
        if _move_back(pane, split, (home, target), log):
            n += 1
        else:
            clean = False
    log("recover: %s moved back %d/%d pane(s) to %s" % ("ok" if clean else "PARTIAL", n, len(stranded), home))
    return clean, n


def recover_pending(log):
    """Recover from the journal (startup / before each rebuild / shutdown)."""
    j = journal_read()
    if not j:
        if journal_exists():
            journal_clear()  # unreadable journal: nothing actionable
        return 0
    clean, n = recover_journal(j, log)
    if clean:
        journal_clear()
    else:
        j["attempts"] = j.get("attempts", 0) + 1
        if j["attempts"] >= MAX_RECOVERY_ATTEMPTS:
            log("recover: giving up after %d attempts; panes may remain in the %s tab" % (
                j["attempts"], TMP_TAB_LABEL))
            notify("smart focus", "could not restore panes; check the %s tab" % TMP_TAB_LABEL)
            journal_clear()
        else:
            journal_write(j)
    if n:
        notify("smart focus", "recovered %d pane(s) after an interrupted rebuild" % n)
    return n


def run_stack_ops(tab_id, ops, log, journal=None, abort=None):
    """Execute core.plan_stack ops. Panes moved to the scratch tab are always
    brought back (try/finally, journal-backed). Returns a dict:
    expect (pane to expect a pane.focused for), failed, skipped, refocus."""
    out = {"expect": None, "failed": False, "skipped": False, "refocus": None, "deferred": None}
    tmp = last_out = None
    n_out = 0
    anchor = journal["anchor"] if journal else None
    if journal:
        journal_write(journal)  # write-ahead: before any pane leaves the tab
    try:
        for op in ops:
            if abort and abort():
                raise _Aborted()
            kind = op[0]
            if kind == "swap":
                if not tab_is_focused(tab_id):
                    # pane.swap focuses the tab; don't steal the view. State is
                    # stale now, so drop the rest of this plan.
                    out["skipped"] = True
                    break
                request("pane.swap", {"source_pane_id": op[1], "target_pane_id": op[2]})
                out["expect"] = op[1]
            elif kind == "move_out":
                dest = ({"type": "new_tab", "label": TMP_TAB_LABEL} if n_out == 0 else
                        {"type": "tab", "tab_id": tmp, "split": core.tmp_split(n_out),
                         "target_pane_id": last_out})
                res = request("pane.move", {"pane_id": op[1], "destination": dest, "focus": False})
                n_out += 1
                _fault("move_out_parse", n_out)  # response lost after the move happened
                if tmp is None:
                    tmp = res["move_result"]["pane"]["tab_id"]
                    if journal:
                        journal["tmp_tab"] = tmp
                        journal_write(journal)
                last_out = op[1]
                _fault("after_move_out", n_out)
            elif kind == "move_in":
                request("pane.move", {"pane_id": op[1], "focus": False, "destination": {
                    "type": "tab", "tab_id": tab_id, "split": op[2], "target_pane_id": op[3]}})
                _fault("after_move_in", 1 + sum(1 for o in ops[:ops.index(op)] if o[0] == "move_in"))
            elif kind == "focus":
                cur = None
                try:
                    cur = export_layout(tab_id=tab_id).get("focused_pane_id")
                except RECOVERABLE:
                    pass
                if cur and cur not in (op[1], anchor) and cur in core.ops_panes(ops):
                    # the user focused another pane meanwhile: respect it
                    out["refocus"] = cur
                    log("focus changed by user to %s; skipping final focus" % cur)
                    continue
                if not tab_is_focused(tab_id):
                    out["deferred"] = op[1]  # pane.focus would steal the view; do it on tab focus
                    continue
                request("pane.focus", {"pane_id": op[1]})
                out["expect"] = op[1]
            elif kind == "ratio":
                request("layout.set_split_ratio", {"tab_id": tab_id, "path": op[1], "ratio": op[2]})
    except _Aborted:
        log("stack ops aborted (shutdown) %s" % tab_id)
        out["failed"] = True
    except Exception as exc:  # noqa: BLE001 - recovery below must always run
        log("stack op failed %s: %r" % (tab_id, exc))
        out["failed"] = True
    finally:
        if journal:
            if journal.get("tmp_tab") is None and tmp:
                journal["tmp_tab"] = tmp
            try:
                clean, n = recover_journal(journal, log)
            except Exception as exc:  # noqa: BLE001
                log("recover crashed: %r" % exc)
                clean, n = False, 0
            if clean:
                journal_clear()
            else:
                journal_write(journal)
            if n or not clean:
                out["failed"] = True
    return out


def apply_layout(layout, pane_id, log, abort=None):
    """Apply the configured mode to an exported layout. Returns a dict:
    did, expect, failed, skipped, refocus, panes (panes touched, for echo filtering)."""
    out = {"did": False, "expect": None, "failed": False, "skipped": False,
           "refocus": None, "deferred": None, "panes": set()}
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
        out["did"] = bool(ops)
        return out
    ops = core.plan_stack(layout["root"], pane_id, mode, load_stack_ratio())
    if not ops:
        return out
    if not core.ops_panes(ops) <= set(core.leaves(layout["root"])):
        log("refusing plan touching panes outside %s" % tab_id)
        out["failed"] = True
        return out
    log("stack %s tab=%s focus=%s ops=%s" % (mode, tab_id, pane_id, [o[0] for o in ops]))
    journal = core.journal_for(tab_id, layout["root"], ops)
    res = run_stack_ops(tab_id, ops, log, journal=journal, abort=abort)
    out.update(res)
    out["did"] = True
    out["panes"] = core.ops_panes(ops)
    return out


# ---------- apply requests (CLI -> daemon) ----------

def _request_file():
    return os.path.join(state_dir(), "apply.request")


def request_apply(tab_id):
    """CLI side: ask the daemon to apply the layout for `tab_id`."""
    fd = os.open(_request_file(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (tab_id + "\n").encode())
    finally:
        os.close(fd)


def take_apply_requests():
    taking = _request_file() + ".taking"
    text = ""
    for path in (taking, _request_file()):
        try:
            if path == _request_file():
                os.replace(path, taking)
                path = taking
            with open(path, "r", encoding="utf-8") as fh:
                text += fh.read()
            if path == taking:
                os.unlink(taking)
        except OSError:
            pass
    return core.parse_requests(text)


# ---------- per-pane status subscription (separate connection) ----------

class StatusWatcher:
    """pane.agent_status_changed needs one subscription per pane. Runs on its
    own socket/thread: a failure here never touches the main event stream."""

    def __init__(self, q, log):
        self.q, self.log = q, log
        self.wake = threading.Event()
        self.sock = None
        self.panes = None

    def refresh(self):
        self.wake.set()

    def _wanted(self):
        if not load_status_badges():
            return set()
        enabled = load_enabled()
        return {p["pane_id"] for p in request("pane.list")["panes"] if p.get("tab_id") in enabled}

    def _close(self):
        s, self.sock = self.sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _listen(self, panes):
        s = socket.socket(socket.AF_UNIX)
        s.connect(socket_path())
        self.sock = s
        sub = {"id": "status", "method": "events.subscribe",
               "params": {"subscriptions": core.status_subscriptions(panes)}}
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
        raise HerdrError("status stream closed")

    def _reader(self, panes):
        try:
            self._listen(panes)
        except Exception as exc:  # noqa: BLE001
            if self.panes == panes:  # not a deliberate close for a new pane set
                self.log("status reader: %s; retrying" % exc)
                self.panes = None
                time.sleep(2)
            self.wake.set()

    def run(self):
        while True:
            self.wake.wait()
            time.sleep(STATUS_REFRESH_S)
            self.wake.clear()
            try:
                want = self._wanted()
            except Exception as exc:  # noqa: BLE001
                self.log("status watcher: %s" % exc)
                time.sleep(2)
                self.wake.set()
                continue
            if want == self.panes:
                continue
            self._close()
            self.panes = want
            if want:  # startup / reconnect / pane set change: reconcile all labels once
                self.q.put(("badge_sweep", {"pane_ids": sorted(want)}, time.monotonic()))
                threading.Thread(target=self._reader, args=(want,), daemon=True).start()


# ---------- daemon ----------

class Daemon:
    def __init__(self):
        self.q = queue.SimpleQueue()  # reentrant put: safe from signal handlers
        self.shapes = {}       # tab_id -> last seen shape signature
        self.suppress = {}     # tab_id -> monotonic deadline (our own layout.updated)
        self.expect_focus = {}  # pane_id -> deadline (pane.focused caused by our swap/focus)
        self.echo = (0.0, 0.0, frozenset())  # (start, end, panes) of our last structural ops
        self.failures = core.FailureTracker(FAIL_LIMIT)
        self.deferred = {}     # tab_id -> pane to focus once the tab is shown
        self.pending = []      # [(tab_id, pane)] re-apply requests (user focused meanwhile)
        self.stop = False
        self.critical = False
        self.status_watch = StatusWatcher(self.q, self.log)
        self.badge_timers = {}  # pane_id -> debounce Timer for status badges

    # -- signals --
    def _on_term(self, signum, frame):
        self.stop = True
        if not self.critical:
            raise SystemExit(0)

    def _on_usr1(self, signum, frame):
        self.q.put(("apply_request", {}, time.monotonic()))

    def log(self, msg):
        print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

    # -- applying --
    def apply_for(self, layout, force_pane=None):
        tab_id = layout["tab_id"]
        self.shapes[tab_id] = core.shape_signature(layout["root"])
        if tab_id not in load_enabled() or layout.get("zoomed"):
            return
        self.critical = True
        try:
            self._apply_for(layout, force_pane)
        finally:
            self.critical = False
            if self.stop:
                raise SystemExit(0)

    def _apply_for(self, layout, force_pane):
        tab_id = layout["tab_id"]
        if load_layout() != "keep" and journal_exists():
            recover_pending(self.log)  # a previous rebuild never finished
            try:
                layout = export_layout(tab_id=tab_id)
            except (HerdrError, OSError, ValueError, KeyError):
                return
        pane_id = force_pane or layout.get("focused_pane_id")
        want = self.deferred.get(tab_id)
        if want and want in core.leaves(layout["root"]) and tab_is_focused(tab_id):
            self.deferred.pop(tab_id, None)  # finish a rebuild done while the tab was hidden
            if layout.get("focused_pane_id") != want:
                try:
                    request("pane.focus", {"pane_id": want})
                    self.expect_focus[want] = time.monotonic() + SELF_SUPPRESS_S
                except RECOVERABLE as exc:
                    self.log("deferred focus failed %s: %s" % (want, exc))
            pane_id = want
        self.suppress[tab_id] = time.monotonic() + SELF_SUPPRESS_S
        started = time.monotonic()
        out = apply_layout(layout, pane_id, self.log, abort=lambda: self.stop)
        if out["did"]:
            # pane.focused emitted for panes we move/swap/focus are ours, not the user's
            self.echo = (started, time.monotonic() + DEBOUNCE_S * 2, frozenset(out["panes"]))
        if out["failed"]:
            self.on_failure(tab_id)
        elif out["did"] and not out["skipped"]:
            self.failures.ok(tab_id)
        if out["deferred"]:
            self.deferred[tab_id] = out["deferred"]
        if out["refocus"]:
            self.pending.append((tab_id, out["refocus"]))
        if not out["did"]:
            self.suppress.pop(tab_id, None)
            return
        try:  # remember the post-op shape so our own changes are not "structural"
            self.shapes[tab_id] = core.shape_signature(export_layout(tab_id=tab_id)["root"])
        except (HerdrError, KeyError, OSError, ValueError):
            self.shapes.pop(tab_id, None)
        if out["expect"]:
            self.expect_focus[out["expect"]] = time.monotonic() + SELF_SUPPRESS_S
        self.suppress[tab_id] = time.monotonic() + SELF_SUPPRESS_S

    def on_failure(self, tab_id):
        if load_layout() == "keep":
            return
        hit = self.failures.fail(tab_id)
        notify("smart focus", "stack layout failed on %s (%d/%d)" % (
            tab_id, self.failures.count(tab_id) or FAIL_LIMIT, FAIL_LIMIT))
        if hit:
            enabled = load_enabled()
            enabled.discard(tab_id)
            save_enabled(enabled)
            if load_status_badges():
                self.badge_safe(restore_tab=tab_id)
            self.failures.ok(tab_id)
            self.log("tab %s disabled after %d consecutive failures" % (tab_id, FAIL_LIMIT))
            notify("smart focus", "disabled for %s after %d consecutive failures" % (tab_id, FAIL_LIMIT))

    def schedule_badge(self, pane_id, delay=BADGE_DEBOUNCE_S):
        """Per-pane debounce: a flapping status collapses into one rename that
        reads the live status when the timer fires."""
        old = self.badge_timers.pop(pane_id, None)
        if old:
            old.cancel()
        t = threading.Timer(delay, self.q.put,
                            (("badge_flush", {"pane_id": pane_id}, time.monotonic()),))
        t.daemon = True
        self.badge_timers[pane_id] = t
        t.start()

    def badge_safe(self, **kw):
        try:
            sync_badges(self.log, **kw)
        except RECOVERABLE as exc:
            self.log("status_badges failed: %s" % exc)

    def handle_batch(self, events):
        focus_panes, layout_tabs, any_tab_focus, requested = [], [], False, False
        new_panes = []
        status_panes, flush_panes = [], []
        watch_refresh = False
        for name, data, ts in events:
            if name == "pane_focused" and data.get("pane_id"):
                start, end, panes = self.echo
                if data["pane_id"] in panes and start <= ts <= end:
                    continue  # echo of our own move/swap/focus of this very pane
                if time.monotonic() < self.expect_focus.get(data["pane_id"], 0):
                    continue
                focus_panes.append(data["pane_id"])
            elif name == "tab_focused":
                any_tab_focus = True
            elif name == "apply_request":
                requested = True
            elif name == "layout_updated":
                tab_id = (data.get("layout") or {}).get("tab_id") or data.get("tab_id")
                if tab_id:
                    layout_tabs.append(tab_id)
            elif name == "pane_closed":
                watch_refresh = True
            elif name == "pane_agent_status_changed":
                if data.get("pane_id"):
                    status_panes.append(data["pane_id"])
            elif name == "badge_sweep":
                flush_panes.extend(data.get("pane_ids") or [])
            elif name == "badge_flush":
                flush_panes.append(data.get("pane_id"))
            elif name == "rename_retry":
                new_panes.append((data.get("pane_id"), True))
            elif name == "pane_created":
                watch_refresh = True
                pane = data.get("pane") or {}
                pid = pane.get("pane_id") or data.get("pane_id")
                tab_id = pane.get("tab_id") or data.get("tab_id")
                if pid:
                    new_panes.append((pid, False))
                if tab_id:
                    layout_tabs.append(tab_id)
                elif pid:
                    focus_panes.append(pid)
        done_tabs = set()
        if requested or watch_refresh:
            self.status_watch.refresh()  # enabled-tab pane set may have changed
        if requested:  # toggle/enable from the CLI: only the daemon moves panes
            for tab_id in take_apply_requests():
                if tab_id.startswith("off:"):  # disable: remove badges, back to base names
                    tab_id = tab_id[4:]
                    if load_status_badges():
                        self.badge_safe(restore_tab=tab_id)
                    continue
                try:
                    layout = export_layout(tab_id=tab_id)
                except (HerdrError, OSError, ValueError, KeyError) as exc:
                    self.log("apply request %s failed: %s" % (tab_id, exc))
                    continue
                done_tabs.add(tab_id)
                if tab_id in load_enabled() and load_auto_rename():
                    try:  # only on enable/toggle-on; no other trigger
                        auto_rename_tab(tab_id, core.leaves(layout["root"]), self.log)
                    except RECOVERABLE as exc:
                        self.log("auto_rename %s failed: %s" % (tab_id, exc))
                if load_status_badges():
                    self.badge_safe(tab_id=tab_id)
                self.apply_for(layout)
        if new_panes and load_auto_rename():
            for pid, is_retry in dict.fromkeys(new_panes):
                try:
                    res = auto_rename_new_pane(pid, self.log)
                except RECOVERABLE as exc:
                    self.log("auto_rename %s failed: %s" % (pid, exc))
                    continue
                if res == "retry" and not is_retry:  # cwd not set yet: one more try
                    t = threading.Timer(RENAME_RETRY_S, self.q.put,
                                        (("rename_retry", {"pane_id": pid}, time.monotonic()),))
                    t.daemon = True
                    t.start()
        if load_status_badges():
            for pid in dict.fromkeys(status_panes):
                self.schedule_badge(pid)
            for pid, _retry in dict.fromkeys(new_panes):
                if pid:
                    self.schedule_badge(pid, 1.5)  # after auto_rename (+ its retry)
            due = [pid for pid in dict.fromkeys(flush_panes) if pid]
            for pid in due:
                self.badge_timers.pop(pid, None)
            if due:
                self.badge_safe(pane_ids=due)
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
        for _ in range(2):  # user changed focus while we rebuilt: honor it
            if not self.pending:
                break
            tab_id, pane_id = self.pending.pop(0)
            try:
                self.apply_for(export_layout(tab_id=tab_id), force_pane=pane_id)
            except (HerdrError, OSError, ValueError, KeyError) as exc:
                self.log("re-apply %s failed: %s" % (tab_id, exc))
        self.pending = []

    # -- event stream --
    def reader(self):
        """Connect + subscribe, push events on the queue. Returns on EOF/error."""
        s = socket.socket(socket.AF_UNIX)
        s.connect(socket_path())
        sub = {"id": "sub", "method": "events.subscribe",
               "params": {"subscriptions": core.main_subscriptions()}}
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
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        signal.signal(signal.SIGUSR1, self._on_usr1)
        self.log("daemon started pid=%d" % os.getpid())
        self.critical = True
        try:
            recover_pending(self.log)  # panes stranded by a crash / kill -9
        finally:
            self.critical = False
        if self.stop:
            return
        threading.Thread(target=self.reader_loop, daemon=True).start()
        threading.Thread(target=self.status_watch.run, daemon=True).start()
        self.status_watch.refresh()
        if os.path.exists(_request_file()):
            self.q.put(("apply_request", {}, time.monotonic()))
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
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)  # before the lock: CLI may signal at once
    lock = acquire_lock()
    if lock is None:
        return 0  # another daemon owns the lock
    try:
        Daemon().run()
    except SystemExit:
        pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)  # don't interrupt the final recovery
        try:
            recover_pending(lambda m: print(m, flush=True))
        except Exception:  # noqa: BLE001
            pass
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
    if want or load_status_badges():  # the daemon applies/restores; CLI just asks
        request_apply(tab if want else "off:" + tab)
        for _ in range(20):
            pid = daemon_pid()
            if pid:
                try:
                    os.kill(pid, signal.SIGUSR1)
                except OSError:
                    pass
                break
            time.sleep(0.1)
    return 0


def cmd_status():
    tab = resolve_tab()
    info = {"daemon_pid": daemon_pid(), "tab_id": tab,
            "tab_enabled": tab in load_enabled(), "focus_ratio": load_ratio(),
            "layout": load_layout(), "stack_ratio": load_stack_ratio(),
            "balance_others": load_balance(), "auto_rename": load_auto_rename(),
            "status_badges": load_status_badges(),
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
