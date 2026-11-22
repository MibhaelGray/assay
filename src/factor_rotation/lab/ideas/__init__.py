"""One file per idea. Each has an `IDEA` (registry.Idea: why it might work, the universe, a few variants) and a
`score(p, u, **params)` that returns a table of scores known at each day's close: higher = buy, a gap = not in play.
`p` is the panel, `u` the table of who is tradable. See docs/research-lab.md for how to add one."""
