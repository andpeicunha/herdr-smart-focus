"""Pure layout math for smart-focus (no I/O).

Layout tree (from `layout.export`): nodes are either
  {"type": "pane", "pane_id": str, ...}
  {"type": "split", "direction": "right"|"down", "ratio": float, "first": node, "second": node}

`layout.set_split_ratio` path semantics (verified on herdr 0.9.5): a list of
bools from the root, False = first child, True = second child; `ratio` is the
share of the FIRST child.
"""

MIN_RATIO = 0.5
MAX_RATIO = 0.9
DEFAULT_RATIO = 0.70
# herdr clamps every split ratio to this range; targets outside it would be
# re-sent forever (current ratio never reaches the target).
SPLIT_MIN = 0.1
SPLIT_MAX = 0.9
DEFAULT_STACK_RATIO = 0.72
LAYOUTS = ("keep", "stack-bottom", "stack-right")


def clamp_split(value):
    return max(SPLIT_MIN, min(SPLIT_MAX, float(value)))


def clamp_stack_ratio(value, default=DEFAULT_STACK_RATIO):
    try:
        r = float(value)
    except (TypeError, ValueError):
        return default
    if r != r:
        return default
    return clamp_split(r)


def normalize_layout(value):
    v = str(value or "keep").strip().lower()
    return v if v in LAYOUTS else "keep"


def clamp_ratio(value, default=DEFAULT_RATIO):
    try:
        r = float(value)
    except (TypeError, ValueError):
        return default
    if r != r:  # NaN
        return default
    return max(MIN_RATIO, min(MAX_RATIO, r))


def contains_pane(node, pane_id):
    if node.get("type") == "pane":
        return node.get("pane_id") == pane_id
    return contains_pane(node["first"], pane_id) or contains_pane(node["second"], pane_id)


def count_leaves(node):
    if node.get("type") == "pane":
        return 1
    return count_leaves(node["first"]) + count_leaves(node["second"])


def _balance_ops(node, path, ops, eps, clamp=False):
    """Splits in an off-path subtree: ratio = leaves(first)/leaves(total)."""
    if node.get("type") != "split":
        return
    target = count_leaves(node["first"]) / float(count_leaves(node))
    if clamp:
        target = clamp_split(target)
    if abs(float(node["ratio"]) - target) > eps:
        ops.append((list(path), round(target, 4)))
    _balance_ops(node["first"], path + [False], ops, eps, clamp)
    _balance_ops(node["second"], path + [True], ops, eps, clamp)


def axis_count(node, direction):
    """Number of slots a subtree takes along `direction`."""
    if node.get("type") == "pane":
        return 1
    a, b = axis_count(node["first"], direction), axis_count(node["second"], direction)
    return a + b if node.get("direction") == direction else max(a, b)


def _path_nodes(root, pane_id):
    """[(split, in_first)] from root down to the pane."""
    out = []
    node = root
    while node.get("type") == "split":
        in_first = contains_pane(node["first"], pane_id)
        out.append((node, in_first))
        node = node["first"] if in_first else node["second"]
    return out


def compute_ops(root, pane_id, focus_ratio=DEFAULT_RATIO, eps=0.005, balance_others=True,
                clamp_targets=False):
    """Return [(path, ratio)] to apply, root first.

    Splits on root->pane: with `balance_others`, consecutive same-direction
    splits form one row/column; the focused member gets `focus_ratio` of the
    whole group and the rest is shared among the other members proportionally
    to their extent along that axis. Without it, each split's focused side
    gets `focus_ratio`. With `balance_others`, splits off the path are set so
    all leaves of a subtree get equal size. Splits already within `eps` are
    skipped. `clamp_targets` clamps every target to herdr's [0.1, 0.9] so an
    unreachable target is not re-sent forever (the daemon always sets it).
    Returns [] when the pane is not in the tree or is alone.
    """
    focus_ratio = clamp_ratio(focus_ratio)
    if not contains_pane(root, pane_id):
        return []
    nodes = _path_nodes(root, pane_id)
    targets = {}  # index in nodes -> ratio
    if balance_others:
        k = 0
        while k < len(nodes):
            d = nodes[k][0]["direction"]
            m = k
            while m + 1 < len(nodes) and nodes[m + 1][0]["direction"] == d:
                m += 1
            # shares: others first, focused member gets focus_ratio
            others = []
            for idx in range(k, m + 1):
                sp, in_first = nodes[idx]
                off = sp["second"] if in_first else sp["first"]
                others.append(float(axis_count(off, d)))
            total_w = sum(others)
            share_off = [(1.0 - focus_ratio) * w / total_w for w in others]
            # remaining share of the subtree under each split
            tail = focus_ratio
            rem = []
            for idx in range(m, k - 1, -1):
                tail += share_off[idx - k]
                rem.append(tail)
            rem.reverse()  # rem[i] = total share under split k+i
            for idx in range(k, m + 1):
                sp, in_first = nodes[idx]
                off_share = share_off[idx - k]
                on_share = rem[idx - k] - off_share
                first_share = on_share if in_first else off_share
                targets[idx] = first_share / rem[idx - k]
            k = m + 1
    else:
        for idx, (sp, in_first) in enumerate(nodes):
            targets[idx] = focus_ratio if in_first else 1.0 - focus_ratio
    if clamp_targets:
        for idx in targets:
            targets[idx] = clamp_split(targets[idx])
    ops = []
    path = []
    for idx, (sp, in_first) in enumerate(nodes):
        if abs(float(sp["ratio"]) - targets[idx]) > eps:
            ops.append((list(path), round(targets[idx], 4)))
        if balance_others:
            off = sp["second"] if in_first else sp["first"]
            _balance_ops(off, path + [in_first], ops, eps, clamp_targets)
        path.append(not in_first)
    return ops


def shape_signature(root):
    """Ratio-independent structure signature (pane ids + split directions)."""
    if root.get("type") == "pane":
        return str(root.get("pane_id"))
    return "(%s %s %s)" % (
        root.get("direction"),
        shape_signature(root["first"]),
        shape_signature(root["second"]),
    )


# ---------- stack layouts (stack-bottom / stack-right) ----------
#
# Target shape, stack-bottom:  split(down, first=STRIP, second=focused pane)
# stack-right:                 split(right, first=STRIP, second=focused pane)
# STRIP = the other panes, all splits in one direction (right for bottom,
# down for right), equal parts. `ratio` of the root = share of the strip
# = 1 - stack_ratio.
#
# plan_stack returns ops, executed in order by the daemon:
#   ("swap", source_pane, target_pane)   source ends up focused (herdr semantics)
#   ("move_out", pane)                   move to a scratch tab (pane.move)
#   ("move_in", pane, split, target)     move back next to `target`
#   ("focus", pane)
#   ("ratio", path, ratio)               layout.set_split_ratio
# Paths of "ratio" ops refer to the tree AFTER the structural ops.

STACK_DIRS = {"stack-bottom": ("down", "right"), "stack-right": ("right", "down")}


def leaves(node):
    """pane ids in reading order (first before second)."""
    if node.get("type") == "pane":
        return [node.get("pane_id")]
    return leaves(node["first"]) + leaves(node["second"])


def _only_direction(node, direction):
    if node.get("type") == "pane":
        return True
    return (node.get("direction") == direction
            and _only_direction(node["first"], direction)
            and _only_direction(node["second"], direction))


def is_stack_shape(root, mode):
    root_dir, strip_dir = STACK_DIRS[mode]
    return (root.get("type") == "split" and root.get("direction") == root_dir
            and root["second"].get("type") == "pane"
            and _only_direction(root["first"], strip_dir))


def _chain(panes, direction):
    node = {"type": "pane", "pane_id": panes[-1]}
    for p in reversed(panes[:-1]):
        node = {"type": "split", "direction": direction, "ratio": 0.5,
                "first": {"type": "pane", "pane_id": p}, "second": node}
    return node


def build_stack_tree(strip_panes, focused, mode):
    """Simulated tree for a restructure (strip panes in order, right-chain)."""
    root_dir, strip_dir = STACK_DIRS[mode]
    return {"type": "split", "direction": root_dir, "ratio": 0.5,
            "first": _chain(strip_panes, strip_dir),
            "second": {"type": "pane", "pane_id": focused}}


def stack_ratio_ops(root, stack_ratio, mode, eps=0.005, force=False):
    """[(path, ratio)] so the strip is equal parts and focused = stack_ratio.
    `force` emits every split (used right after a restructure, where the
    real ratios are unknown)."""
    stack_ratio = clamp_stack_ratio(stack_ratio)
    ops = []

    def want(node, path, target):
        target = round(clamp_split(target), 4)
        if force or abs(float(node["ratio"]) - target) > eps:
            ops.append((list(path), target))

    want(root, [], 1.0 - stack_ratio)

    def strip(node, path):
        if node.get("type") != "split":
            return
        want(node, path, count_leaves(node["first"]) / float(count_leaves(node)))
        strip(node["first"], path + [False])
        strip(node["second"], path + [True])

    strip(root["first"], [False])
    return ops


def plan_stack(root, focused, mode, stack_ratio=DEFAULT_STACK_RATIO, eps=0.005):
    """Pure planner: (tree, focused pane, mode) -> ordered op list (see above)."""
    if mode not in STACK_DIRS or not contains_pane(root, focused):
        return []
    order = leaves(root)
    if len(order) < 2:
        return []
    root_dir, strip_dir = STACK_DIRS[mode]
    if is_stack_shape(root, mode):
        ops = []
        bottom = root["second"]["pane_id"]
        if bottom != focused:
            ops.append(("swap", focused, bottom))
            # same shape afterwards; strip/ratio ops depend on shape only
        ops += [("ratio", p, r) for p, r in stack_ratio_ops(root, stack_ratio, mode, eps)]
        return ops
    strip = [p for p in order if p != focused]
    anchor = strip[0]
    ops = [("move_out", focused)] + [("move_out", p) for p in strip[1:]]
    ops.append(("move_in", focused, root_dir, anchor))
    for i in range(1, len(strip)):
        ops.append(("move_in", strip[i], strip_dir, strip[i - 1]))
    ops.append(("focus", focused))
    target = build_stack_tree(strip, focused, mode)
    ops += [("ratio", p, r) for p, r in stack_ratio_ops(target, stack_ratio, mode, force=True)]
    return ops


# ---------- safety: journal / recovery / backoff / scope (pure) ----------

def ops_panes(ops):
    """Every pane id a stack plan touches (swap / move / focus)."""
    out = set()
    for op in ops:
        if op[0] == "swap":
            out.update((op[1], op[2]))
        elif op[0] in ("move_out", "focus"):
            out.add(op[1])
        elif op[0] == "move_in":
            out.update((op[1], op[3]))
    return out


def journal_for(tab_id, root, ops):
    """Write-ahead record for a plan that moves panes out of the tab; None
    when the plan never leaves the tab (swap / ratio only)."""
    outs = [op[1] for op in ops if op[0] == "move_out"]
    if not outs:
        return None
    order = leaves(root)
    anchor = next((op[3] for op in ops if op[0] == "move_in"), None)
    if anchor is None:  # anchor = first pane that never leaves
        anchor = next((p for p in order if p not in outs), order[0])
    return {"tab": tab_id, "anchor": anchor, "order": order, "tmp_tab": None, "attempts": 0}


def tmp_split(index):
    """Direction for the index-th pane (>=1) parked in the scratch tab.
    Alternating avoids a long same-direction chain hitting herdr's minimum
    pane size."""
    return "right" if index % 2 == 1 else "down"


def recovery_moves(order, anchor, stranded, home):
    """[(pane, split, target)] bringing stranded panes back to the home tab.
    Only panes listed in the journal `order` are ever moved. Each pane goes
    next to the nearest preceding pane (original reading order) already
    present; direction alternates to keep sizes viable."""
    pos = {p: i for i, p in enumerate(order)}
    todo = sorted((p for p in stranded if p in pos), key=lambda p: pos[p])
    present = list(home)
    base = anchor if anchor in present else (present[0] if present else None)
    if base is None:
        return []
    moves = []
    for n, p in enumerate(todo):
        target = base
        for q in reversed(order[:pos[p]]):
            if q in present:
                target = q
                break
        moves.append((p, "right" if n % 2 == 0 else "down", target))
        present.append(p)
    return moves


def parse_requests(text):
    """Apply-request file -> ordered unique tab ids."""
    seen = []
    for line in (text or "").splitlines():
        t = line.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


class FailureTracker:
    """Consecutive failures per key; `fail` returns True at the limit."""

    def __init__(self, limit=3):
        self.limit = limit
        self.counts = {}

    def fail(self, key):
        n = self.counts.get(key, 0) + 1
        self.counts[key] = n
        return n >= self.limit

    def ok(self, key):
        self.counts.pop(key, None)

    def count(self, key):
        return self.counts.get(key, 0)
