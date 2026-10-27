# Architecture diagrams

Sources for the two diagrams on the *Architecture and Control Boundaries* page, written for the
[Archify](https://github.com/tt-a1i/archify) skill. Each produces an interactive HTML (an Antora
attachment, linked from the page) and a PNG of its diagram panel (embedded in the page).

After editing a source, from the Archify skill directory:

```bash
node bin/archify.mjs validate architecture <repo>/diagrams/<name>.architecture.json --quality showcase --json
node bin/archify.mjs deliver architecture <repo>/diagrams/<name>.architecture.json \
  <repo>/content/modules/ROOT/attachments/<name>.html --quality showcase --json
node bin/archify.mjs visual-check <repo>/content/modules/ROOT/attachments/<name>.html --json
```

Move the `*.visual-check.*` evidence files out of `attachments/` (they would be published), then
re-capture the PNG: a 2x screenshot of the largest `<svg>` in the delivered HTML at a 1440x900
viewport, saved as `content/modules/ROOT/assets/images/<name>.png`.
