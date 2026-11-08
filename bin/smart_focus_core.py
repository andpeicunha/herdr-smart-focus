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


def compute_ops(root, pane_id, focus_ratio=DEFAULT_RATIO, eps=0.005, balance_others=True):
    """Return [(path, ratio)] to apply, root first.

    Splits on root->pane get `focus_ratio` for the focused side. With
    `balance_others`, every other split (off-path subtrees) is set so all
    their leaves get equal size. Splits already within `eps` are skipped.
    Returns [] when the pane is not in the tree or the tree is a single pane.
    """
    focus_ratio = clamp_ratio(focus_ratio)
    if not contains_pane(root, pane_id):
        return []
    ops = []
    path = []
    node = root
    while node.get("type") == "split":
        in_first = contains_pane(node["first"], pane_id)
        target = focus_ratio if in_first else 1.0 - focus_ratio
        if abs(float(node["ratio"]) - target) > eps:
            ops.append((list(path), round(target, 4)))
        if balance_others:
            other = node["second"] if in_first else node["first"]
            _balance_ops(other, path + [in_first], ops, eps)
        path.append(not in_first)
        node = node["first"] if in_first else node["second"]
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
