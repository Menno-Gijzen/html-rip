# html-rip

A single-page website ripper that downloads HTML, CSS, JavaScript, and images for offline viewing. All asset references are rewritten to point to local files.

## What it does

- Prompts for a URL and a destination folder
- Downloads the main HTML page
- Downloads and localizes:
  - **CSS** — `<link rel="stylesheet">` and `@import` / `url()` references inside stylesheets
  - **JavaScript** — `<script src="...">`
  - **Images** — `<img src>`, `<source srcset>`, `<picture>`, favicons, apple-touch-icons, OpenGraph/Twitter meta images, and images referenced via CSS `url()`
- Extracts inline `<style>` blocks into `css/inline_styles.css`
- Rewrites all paths in HTML and CSS so the page works offline

## Output structure

```
destination/
├── index.html
├── css/
│   ├── inline_styles.css
│   └── *.css
├── js/
│   └── *.js
└── img/
    └── *.png / *.jpg / *.svg / ...
```

## Requirements

- Python 3.10+
- [requests](https://pypi.org/project/requests/)
- [beautifulsoup4](https://pypi.org/project/beautifulsoup4/)

## Installation

```bash
pip install requests beautifulsoup4
```

## Usage

```bash
python html-rip.py
```

You will be prompted for:

1. **Website URL** — e.g. `https://example.com` (the `https://` prefix is added automatically if omitted)
2. **Destination folder** — where the downloaded site will be saved

Then open `destination/index.html` in your browser.

## Limitations

- **Single page only** — this is not a full-site crawler; it downloads the given URL and its directly referenced assets.
- **No JS rendering** — pages that rely heavily on client-side JavaScript to generate content may not display correctly offline.
- **50 MB per-file limit** — individual assets larger than 50 MB are skipped.

## Disclaimer

Be mindful of website terms of service, `robots.txt`, and copyright when using this tool. This project is intended for personal/archival use.

## License

[MIT](LICENSE)
