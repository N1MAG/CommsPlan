# CommsPlan documentation site

This directory is the curated source for the public CommsPlan documentation
prototype. VitePress builds only this directory and explicitly selected image
assets. Do not point GitHub Pages at the repository root or copy internal
project documentation into this tree.

## Local preview

Use Node.js 20 or newer:

```bash
cd website
npm ci
npm run dev
```

The configured GitHub Pages base path is `/CommsPlan/` for the intended
`N1MAG/CommsPlan` repository destination. FreqInOut remains a compatibility
name inside established stations and source/runtime paths.

## Release verification

```bash
npm ci
npm run verify
```

`npm run verify` audits the locked dependency tree and performs a production
build. It then verifies the exact generated HTML page set, internal links and
assets, rejects symbolic links, and applies a bounded internal-marker tripwire
to the publication artifact. The generated site is written to
`website/.vitepress/dist/` and is not tracked by Git. The artifact-only Pages
upload remains the primary public/private content boundary.

## Content boundary

- Publish task-focused operator documentation only.
- Do not publish `docs/internal`, private testing material, databases, logs, or
  configuration samples containing station secrets.
- Review screenshots for callsigns, coordinates, messages, access codes, and
  local paths.
- Keep installation claims aligned with CommsPlan 2.0.5 packaging inputs and
  label historical FreqInOut 2.0.4 compatibility details clearly.
- Prefer one authoritative page over duplicated instructions.
