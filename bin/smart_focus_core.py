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


def _balance_ops(node, path, ops, eps):
    """Splits in an off-path subtree: ratio = leaves(first)/leaves(total)."""
    if node.get("type") != "split":
        return
    target = count_leaves(node["first"]) / float(count_leaves(node))
    if abs(float(node["ratio"]) - target) > eps:
        ops.append((list(path), round(target, 4)))
    _balance_ops(node["first"], path + [False], ops, eps)
    _balance_ops(node["second"], path + [True], ops, eps)


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


def compute_ops(root, pane_id, focus_ratio=DEFAULT_RATIO, eps=0.005, balance_others=True):
    """Return [(path, ratio)] to apply, root first.

    Splits on root->pane: with `balance_others`, consecutive same-direction
    splits form one row/column; the focused member gets `focus_ratio` of the
    whole group and the rest is shared among the other members proportionally
    to their extent along that axis. Without it, each split's focused side
    gets `focus_ratio`. With `balance_others`, splits off the path are set so
    all leaves of a subtree get equal size. Splits already within `eps` are
    skipped. Returns [] when the pane is not in the tree or is alone.
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
    ops = []
    path = []
    for idx, (sp, in_first) in enumerate(nodes):
        if abs(float(sp["ratio"]) - targets[idx]) > eps:
            ops.append((list(path), round(targets[idx], 4)))
        if balance_others:
            off = sp["second"] if in_first else sp["first"]
            _balance_ops(off, path + [in_first], ops, eps)
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
