# herdr-smart-focus

Herdr plugin (**beta**, `0.1.0-beta`). When focus moves to a pane, that pane
grows to ~70% (`focus_ratio`) along every split on the path root -> pane. It
only calls `layout.export` and `layout.set_split_ratio`, so panes and their
processes are never recreated, swapped or moved (no `layout.apply`,
`pane.swap`, `pane.move`).

## How it works

- `[[startup]]` starts one background daemon (`bin/smart_focus.py daemon`),
  guarded by an `flock` on `daemon.lock` in the plugin state dir.
- The daemon subscribes to `pane.focused`, `pane.created`, `pane.closed`,
  `layout.updated` and `tab.focused`, debounces 80 ms, and applies ratios only
  on tabs where smart focus is enabled (zoomed tabs are skipped).
- `layout.updated` echoes from its own changes are ignored. A `layout.updated`
  only triggers a re-apply when the tab structure changed (pane added/removed);
  a manual resize is kept until the next focus change.
- Layout math is a pure function in `bin/smart_focus_core.py`
  (`compute_ops(tree, pane_id, ratio)` -> `[(path, ratio)]`).

`layout.set_split_ratio` semantics (verified on herdr 0.9.5): `path` is a list
of bools from the root (`false` = first child, `true` = second child) and
`ratio` is the share of the **first** child.

## Actions

All act on the current tab (context tab, pane or global):

- `smart focus: toggle for this tab`
- `smart focus: enable for this tab`
- `smart focus: disable for this tab`
- `smart focus: status`

Bind one in your Herdr config, e.g.:

```toml
[[keys.command]]
key = "prefix+z"
type = "plugin_action"
command = "andpeicunha.smart-focus.toggle"
description = "smart focus toggle"
```

## Config

- `focus_ratio` in `$HERDR_PLUGIN_CONFIG_DIR/config.toml` (see
  `herdr plugin config-dir andpeicunha.smart-focus`), default `0.70`.
- `HERDR_SMART_FOCUS_RATIO` env var overrides it (read by the daemon at
  start of the daemon's environment).
- Values are clamped to 0.5..0.9.

State (`tabs.json`, `daemon.lock`, `daemon.log`) lives in
`$HERDR_PLUGIN_STATE_DIR` (fallback `~/.local/state/herdr-smart-focus`).

## Develop

```bash
herdr plugin link .
python3 -m unittest discover -s tests
```

Python 3 stdlib only. Platforms: linux, macos.

## Limitations

- The 70% applies **per split axis** along the path, not per screen area.
  In deep or mixed-axis trees the focused pane's area share is the product of
  the ratios of the splits it sits in.
- Other splits on the path are changed too, so siblings shrink; splits off the
  path are untouched.
- Beta: tested on herdr 0.9.5 only.
