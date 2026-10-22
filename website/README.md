# CommsPlan documentation site

This directory is the curated source for the public CommsPlan documentation
site. VitePress builds only this directory and explicitly selected image assets.
Do not point GitHub Pages at the repository root or copy internal project
documentation into this tree.

## Local preview

Use Node.js 20.x at version 20.19 or newer, or Node.js 22.12 or newer (22.x and later):

```bash
cd website
npm ci
npm run dev
```

The configured GitHub Pages base path is `/CommsPlan/` for the
`N1MAG/CommsPlan` repository destination.

## Release verification

```bash
npm ci
npm run verify
```

`npm run verify` audits the locked dependency tree and performs a production
build. It then verifies the exact generated HTML page set, internal links and
assets, rejects symbolic links, and applies publication-content tripwires to
the generated artifact. The generated site is written to
`website/.vitepress/dist/` and is not tracked by Git. The artifact-only Pages
upload remains the public-content boundary.

## Content boundary

- Publish task-focused operator documentation only.
- Do not publish internal project documentation, private testing material,
  databases, logs, or configuration samples containing station secrets.
- Review screenshots for callsigns, coordinates, messages, access codes, and
  local paths.
- Label upcoming releases clearly and direct downloads to the latest published
  release page. Do not list unpublished package assets.
- Prefer one authoritative page over duplicated instructions.
