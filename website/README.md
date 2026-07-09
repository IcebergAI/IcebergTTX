# IcebergTTX documentation site

The public site at **https://icebergai.github.io/IcebergTTX/**, built with
[Zensical](https://zensical.org) (a Material-for-MkDocs–based static site generator)
and styled to the shared **Iceberg** design system.

## Layout

```
website/
├─ zensical.toml            # site config (nav, palette, theme, self-hosted fonts)
├─ docs/
│  ├─ index.md              # landing page (hero + feature grid + screenshots)
│  ├─ deployment.md
│  ├─ scenarios.md          # scenario JSON authoring
│  ├─ security.md
│  ├─ assets/               # brand SVGs, favicon, screenshots
│  ├─ fonts/                # self-hosted woff2 (Archivo / JetBrains Mono / Spectral)
│  └─ stylesheets/
│     ├─ fonts.css          # @font-face rules (path-rewritten from the app's fonts.css)
│     └─ iceberg.css        # Iceberg tokens mapped onto Material's --md-* variables
└─ overrides/               # theme overrides (currently unused)
```

## Local preview

```bash
pip install zensical           # into your virtualenv
cd website
zensical serve                 # http://localhost:8000
zensical build --clean         # outputs to website/site/
```

## Deployment

Pushes to `main` that touch `website/**` trigger `.github/workflows/docs.yml`, which
builds and deploys to GitHub Pages. This requires **Settings → Pages → Source =
"GitHub Actions"** (one-time, in the repository settings).

## Styling notes

- Fonts are **self-hosted** (no Google Fonts); `font = false` in `zensical.toml`
  disables the default CDN fonts and `stylesheets/fonts.css` provides the woff2.
- The palette uses `primary = "custom"` / `accent = "custom"`; the real colours come
  from the Iceberg oklch tokens in `stylesheets/iceberg.css`, which map onto
  Material's `--md-*` variables for both the light (`default`) and dark (`slate`)
  schemes. Keep it in sync with `../static/css/iceberg.css`.
- Brand assets are the IcebergAI marks; plain-named SVGs carry light ink (for dark
  backgrounds), `-light` variants carry dark ink (for light backgrounds).
