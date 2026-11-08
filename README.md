# herdr-smart-focus

Herdr plugin (**beta**, `0.3.1-beta`). When focus moves to a pane, that pane
grows to ~70% (`focus_ratio`) along every split on the path root -> pane.
With the default `layout = "keep"` it only calls `layout.export` and
`layout.set_split_ratio`, so panes are never swapped or moved. The optional
stack layouts (below) also use `pane.swap` / `pane.move`; processes and
scrollback are preserved (never `layout.apply`, which recreates panes).

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
  `herdr plugin config-dir andpeicunha.smart-focus`), default `0.70`. If
  `HERDR_PLUGIN_CONFIG_DIR` is unset, the daemon falls back to
  `~/.config/herdr/plugins/config/andpeicunha.smart-focus` (state:
  `~/.local/state/herdr/plugins/andpeicunha.smart-focus`).
- `balance_others` (bool, default `true`): splits off the focus path are set
  to `leaves(first)/leaves(total)`, so all unfocused panes of a subtree get
  equal size. Consecutive same-direction splits on the path count as one
  row/column: the focused pane gets `focus_ratio` of the whole column and the
  rest is split equally among the others (`A | (B/C/D)`: focus A -> B,C,D 1/3
  each; focus C with 0.85 -> B 7.5%, C 85%, D 7.5% of the column). `false` leaves off-path splits untouched (0.1
  behavior). Env override: `HERDR_SMART_FOCUS_BALANCE=0|1`.
- `HERDR_SMART_FOCUS_RATIO` env var overrides it (read by the daemon at
  start of the daemon's environment).
- Values are clamped to 0.5..0.9.
- All ratio targets are clamped to herdr's 0.1..0.9 before comparing with the
  current ratio, so unreachable targets are not re-sent on every focus change.
- `layout = "keep" | "stack-bottom" | "stack-right"` (default `"keep"` =
  behavior above, unchanged). Env override: `HERDR_SMART_FOCUS_LAYOUT`.
- `stack_ratio` (default `0.72`, clamped 0.1..0.9): share of the focused pane
  in the stack layouts. Env override: `HERDR_SMART_FOCUS_STACK_RATIO`.
  (`focus_ratio`/`balance_others` only apply to `keep`.)
- Env overrides are read from the daemon's environment at start.

## Stack layouts

- `stack-bottom`: the unfocused panes sit side by side in equal parts on top;
  the focused pane is at the bottom with `stack_ratio` of the height.
- `stack-right`: the unfocused panes are stacked in equal parts on the left;
  the focused pane is on the right with `stack_ratio` of the width.
- Focus change: the pane that was in the big slot takes the strip slot the new
  focused pane came from (`pane.swap`; focus ends on the pane you chose).
- First activation on an arbitrary tab (or after a pane is added/removed or the
  tree no longer matches): the layout is rebuilt in reading order with the
  focused pane in the big slot. This uses `pane.move` through a short-lived
  scratch tab `smart-focus-tmp`; the anchor pane never leaves the tab. Pids and
  scrollback are preserved (verified on herdr 0.9.5). If a move fails, stranded
  panes are moved back.
- Disabling smart focus (or switching back to `keep`) leaves the layout as is.
- `pane.swap` focuses the tab, so a swap is skipped when the tab is not the
  focused one (ratios are still fixed). `pane.focused` events emitted during
  our own operations are ignored (a focus change you make within ~0.2 s of a
  rebuild may be dropped).
- Planning is a pure function:
  `smart_focus_core.plan_stack(tree, focused, mode, stack_ratio)` ->
  `[("swap"|"move_out"|"move_in"|"focus"|"ratio", ...)]`.

## Safety and recovery (stack layouts)

- Write-ahead journal (`journal.json` in the state dir): before any pane is
  moved to the scratch tab it records home tab, anchor pane and the original
  reading order. It is deleted once every pane is back.
- Every rebuild runs under `try/finally`: any error (timeout, bad JSON,
  socket error) triggers recovery. If the scratch tab id was never parsed,
  the tab is found by its `smart-focus-tmp` label inside the same workspace.
  Only panes listed in the journal are ever moved back.
- Recovery also runs at daemon startup (covers `kill -9`/crash), before each
  rebuild, and on SIGTERM/SIGINT (the running rebuild stops between ops and
  restores the panes before exit).
- `toggle`/`enable` only write state and ask the daemon (`apply.request` +
  SIGUSR1); the CLI never moves panes, so there is no concurrent rebuild.
- The scratch tab alternates split directions (avoids minimum-size failures).
- A failed stack apply raises a notification and counts toward a per-tab
  backoff: 3 consecutive failures disable smart focus for that tab.
- A swap skipped because the tab is not focused aborts the rest of that plan.
  The final focus is not forced when the tab is hidden (it is applied when the
  tab is shown) or when you focused another pane during the rebuild.
- Test hooks: `HERDR_SMART_FOCUS_WS_PREFIX=<workspace id>` refuses any
  mutating call outside that workspace; `HERDR_SMART_FOCUS_FAULT=<point>:<n>:<raise|kill|term>`
  injects failures (`move_out_parse`, `after_move_out`, `after_move_in`).

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
- Other splits on the path are changed too, so siblings shrink; off-path
  splits are rebalanced by leaf count unless `balance_others = false`.
- Beta: tested on herdr 0.9.5 only.
