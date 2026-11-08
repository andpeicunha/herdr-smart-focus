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


def compute_ops(root, pane_id, focus_ratio=DEFAULT_RATIO, eps=0.005):
    """Return [(path, ratio)] to apply, root first, for splits on root->pane.

    Splits already within `eps` of the target are skipped. Returns [] when the
    pane is not in the tree or the tree is a single pane.
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
