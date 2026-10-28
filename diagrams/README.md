# Architecture diagrams

Sources written for the [Archify](https://github.com/tt-a1i/archify) skill. Each produces an
interactive HTML and a PNG of its diagram panel:

* `guardrails-deterministic` and `guardrails-llm-judge` (architecture): the *Architecture and Control
  Boundaries* page, as Antora attachments (`content/modules/ROOT/attachments/`) and images.
* `live-monitor-flow` (sequence): the repository README, in `docs/` (`docs/images/` for the PNG).

After editing a source, from the Archify skill directory (use `sequence` and the `docs/` paths for
`live-monitor-flow`):

```bash
node bin/archify.mjs validate architecture <repo>/diagrams/<name>.architecture.json --quality showcase --json
node bin/archify.mjs deliver architecture <repo>/diagrams/<name>.architecture.json \
  <repo>/content/modules/ROOT/attachments/<name>.html --quality showcase --json
node bin/archify.mjs visual-check <repo>/content/modules/ROOT/attachments/<name>.html --json
```

Move the `*.visual-check.*` evidence files out of `attachments/` (they would be published), then
re-capture the PNG: a 2x screenshot of the largest `<svg>` in the delivered HTML at a 1440x900
viewport, saved as `content/modules/ROOT/assets/images/<name>.png`.
