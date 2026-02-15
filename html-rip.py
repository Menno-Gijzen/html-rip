#!/usr/bin/env python3
"""
Website ripper (HTML + CSS + JS + images)

What it does:
- Prompts for a website URL and a destination folder
- Downloads the main HTML
- Downloads:
  - linked CSS: <link rel="stylesheet" href="...">
  - linked JS:  <script src="..."></script>
  - images:     <img src>, <source srcset>, <picture>, <link rel=icon>, apple-touch-icon,
                OpenGraph/Twitter images, and CSS url(...) images
- Extracts inline <style> blocks to css/inline_styles.css and rewrites HTML
- Rewrites HTML/CSS to point to local downloaded assets

Notes:
- Best-effort. Modern sites that generate content via JS may not render offline.
- Respects only what’s referenced by the given page + referenced assets (not a full crawler).
- Be mindful of site Terms/robots and copyright.
"""

from __future__ import annotations

import os
import re
import sys
import hashlib
from urllib.parse import urljoin, urlparse, urldefrag, unquote

import requests
from bs4 import BeautifulSoup


USER_AGENT = "Mozilla/5.0 (compatible; SiteRipper/1.0)"
TIMEOUT = 25
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB safety limit


SKIP_SCHEMES = {"data", "mailto", "tel", "javascript"}


def prompt_nonempty(prompt: str) -> str:
    while True:
        v = input(prompt).strip().strip('"').strip("'")
        if v:
            return v


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def is_http_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https")
    except Exception:
        return False


def safe_filename(name: str) -> str:
    name = unquote(name).strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = name.strip("._")
    return name or "file"


def filename_from_url(url: str, default: str) -> str:
    p = urlparse(url)
    base = os.path.basename(p.path)
    if not base:
        return default
    return safe_filename(base)


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:10]


def join_and_clean(base_url: str, ref: str) -> str | None:
    if not ref:
        return None
    ref = ref.strip()
    parsed = urlparse(ref)
    if parsed.scheme and parsed.scheme.lower() in SKIP_SCHEMES:
        return None

    absolute = urljoin(base_url, ref)
    absolute, _frag = urldefrag(absolute)  # drop #fragment
    if not is_http_url(absolute):
        return None
    return absolute


def fetch_bytes(url: str, session: requests.Session) -> tuple[bytes, str, str]:
    """
    Returns (content_bytes, final_url, content_type)
    """
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    r.raise_for_status()

    # size guard (best-effort)
    cl = r.headers.get("Content-Length")
    if cl and cl.isdigit() and int(cl) > MAX_FILE_SIZE:
        raise ValueError(f"File too large (Content-Length={cl})")

    content = r.content
    if len(content) > MAX_FILE_SIZE:
        raise ValueError("File too large (downloaded size exceeded limit)")

    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return content, r.url, ctype


def fetch_text(url: str, session: requests.Session) -> tuple[str, str]:
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    if not r.encoding:
        r.encoding = "utf-8"
    return r.text, r.url


def write_bytes(path: str, data: bytes) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(data)


def write_text(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def relpath_web(from_dir: str, to_path: str) -> str:
    rel = os.path.relpath(to_path, start=from_dir)
    return rel.replace(os.sep, "/")


def choose_ext_from_ctype(ctype: str) -> str:
    return {
        "text/css": ".css",
        "text/javascript": ".js",
        "application/javascript": ".js",
        "application/x-javascript": ".js",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/x-icon": ".ico",
        "image/vnd.microsoft.icon": ".ico",
        "image/avif": ".avif",
    }.get(ctype, "")


def extract_urls_from_css(css_text: str) -> list[str]:
    """
    Extract url(...) and @import '...'
    Returns raw refs (may be relative).
    """
    urls = []

    # url(...)
    for m in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", css_text, flags=re.IGNORECASE):
        ref = m.group(2).strip()
        if ref:
            urls.append(ref)

    # @import
    for m in re.finditer(r"@import\s+(?:url\()?['\"](.*?)['\"]\)?", css_text, flags=re.IGNORECASE):
        ref = m.group(1).strip()
        if ref:
            urls.append(ref)

    return urls


def parse_srcset(srcset: str) -> list[str]:
    """
    srcset="a.jpg 1x, b.jpg 2x" -> ["a.jpg","b.jpg"]
    """
    out = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        url_part = part.split()[0].strip()
        if url_part:
            out.append(url_part)
    return out


class AssetStore:
    def __init__(self, dest_root: str):
        self.dest_root = dest_root
        self.map_url_to_local: dict[str, str] = {}

        self.css_dir = os.path.join(dest_root, "css")
        self.js_dir = os.path.join(dest_root, "js")
        self.img_dir = os.path.join(dest_root, "img")
        ensure_dir(self.css_dir)
        ensure_dir(self.js_dir)
        ensure_dir(self.img_dir)

    def local_path_for(self, kind: str, final_url: str, content_type: str) -> str:
        """
        kind: css|js|img|other
        """
        base_name = filename_from_url(final_url, default=kind)
        ext = os.path.splitext(base_name)[1]
        if not ext:
            ext = choose_ext_from_ctype(content_type)

        # Make name stable and avoid collisions
        stem = os.path.splitext(base_name)[0]
        name = f"{safe_filename(stem)}_{short_hash(final_url)}{ext or ''}"

        if kind == "css":
            return os.path.join(self.css_dir, name)
        if kind == "js":
            return os.path.join(self.js_dir, name)
        if kind == "img":
            return os.path.join(self.img_dir, name)
        return os.path.join(self.dest_root, "assets", name)


def main() -> None:
    print("=== Website Ripper (HTML + CSS + JS + images) ===")
    start_url = normalize_url(prompt_nonempty("Enter website URL (e.g. https://example.com): "))
    dest = os.path.abspath(os.path.expanduser(prompt_nonempty("Enter destination folder: ")))
    ensure_dir(dest)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    store = AssetStore(dest)

    # ---- Fetch HTML ----
    try:
        html, final_url = fetch_text(start_url, session)
    except requests.RequestException as e:
        raise SystemExit(f"Failed to fetch HTML: {e}") from e

    soup = BeautifulSoup(html, "html.parser")

    # ---- Inline style blocks -> css/inline_styles.css ----
    inline_css_parts: list[str] = []
    for style_tag in soup.find_all("style"):
        css_text = style_tag.get_text("\n", strip=False)
        if css_text.strip():
            inline_css_parts.append(css_text)
        style_tag.decompose()

    if inline_css_parts:
        inline_css_rel = "css/inline_styles.css"
        inline_css_full = os.path.join(dest, inline_css_rel)
        write_text(inline_css_full, "\n\n/* ---- inline style separator ---- */\n\n".join(inline_css_parts))

        # Ensure head exists
        if soup.head is None:
            if soup.html is None:
                soup.append(soup.new_tag("html"))
            soup.html.insert(0, soup.new_tag("head"))
        soup.head.append(soup.new_tag("link", rel="stylesheet", href=inline_css_rel))

    downloaded_css = []
    downloaded_js = []
    downloaded_img = []

    # ---- Download external CSS and rewrite links ----
    for link_tag in soup.find_all("link"):
        rel = link_tag.get("rel") or []
        rel = [r.lower() for r in rel]

        href = link_tag.get("href")
        if not href:
            continue

        # Stylesheet
        if "stylesheet" in rel:
            css_url = join_and_clean(final_url, href)
            if not css_url:
                continue
            if css_url in store.map_url_to_local:
                link_tag["href"] = relpath_web(dest, store.map_url_to_local[css_url])
                continue

            try:
                css_text, css_final = fetch_text(css_url, session)
            except requests.RequestException:
                continue

            local = store.local_path_for("css", css_final, "text/css")
            write_text(local, css_text)
            store.map_url_to_local[css_url] = local
            downloaded_css.append((css_final, local))
            link_tag["href"] = relpath_web(dest, local)

        # Icons (treat as images)
        if any(r in rel for r in ("icon", "shortcut icon", "apple-touch-icon", "mask-icon")):
            icon_url = join_and_clean(final_url, href)
            if not icon_url:
                continue
            local_rel = download_image(icon_url, session, store, dest, downloaded_img)
            if local_rel:
                link_tag["href"] = local_rel

    # ---- Download external JS and rewrite script src ----
    for script_tag in soup.find_all("script"):
        src = script_tag.get("src")
        if not src:
            continue

        js_url = join_and_clean(final_url, src)
        if not js_url:
            continue

        if js_url in store.map_url_to_local:
            script_tag["src"] = relpath_web(dest, store.map_url_to_local[js_url])
            continue

        try:
            data, js_final, ctype = fetch_bytes(js_url, session)
        except Exception:
            continue

        # Force .js if content-type hints it or if URL looks like JS
        if not ctype:
            ctype = "application/javascript"

        local = store.local_path_for("js", js_final, ctype)
        write_bytes(local, data)
        store.map_url_to_local[js_url] = local
        downloaded_js.append((js_final, local))
        script_tag["src"] = relpath_web(dest, local)

    # ---- Download images referenced in HTML ----
    # <img src>
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src")
        if src:
            absu = join_and_clean(final_url, src)
            if absu:
                local_rel = download_image(absu, session, store, dest, downloaded_img)
                if local_rel:
                    img_tag["src"] = local_rel

        # srcset
        srcset = img_tag.get("srcset")
        if srcset:
            urls = parse_srcset(srcset)
            new_parts = []
            for u in urls:
                absu = join_and_clean(final_url, u)
                if not absu:
                    continue
                local_rel = download_image(absu, session, store, dest, downloaded_img)
                if local_rel:
                    new_parts.append(f"{local_rel} 1x")
            if new_parts:
                img_tag["srcset"] = ", ".join(new_parts)

    # <source srcset> (e.g. in <picture>)
    for source_tag in soup.find_all("source"):
        srcset = source_tag.get("srcset")
        if not srcset:
            continue
        urls = parse_srcset(srcset)
        new_parts = []
        for u in urls:
            absu = join_and_clean(final_url, u)
            if not absu:
                continue
            local_rel = download_image(absu, session, store, dest, downloaded_img)
            if local_rel:
                new_parts.append(f"{local_rel} 1x")
        if new_parts:
            source_tag["srcset"] = ", ".join(new_parts)

    # Meta images (OpenGraph/Twitter)
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or "").lower()
        name = (meta.get("name") or "").lower()
        if prop in ("og:image", "og:image:url") or name in ("twitter:image", "twitter:image:src"):
            content = meta.get("content")
            absu = join_and_clean(final_url, content or "")
            if absu:
                local_rel = download_image(absu, session, store, dest, downloaded_img)
                if local_rel:
                    meta["content"] = local_rel

    # ---- Parse downloaded CSS for url(...) images and @import CSS ----
    # We will:
    # - For each downloaded CSS: download url(...) assets that look like images
    # - For @import: try to download additional CSS and rewrite (best-effort)
    for _remote, css_local in downloaded_css:
        try:
            css_text = open(css_local, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue

        css_base_url = None
        # Find the original remote URL for this local css if possible
        # (reverse map)
        for k, v in store.map_url_to_local.items():
            if v == css_local:
                css_base_url = k
                break
        if not css_base_url:
            continue

        refs = extract_urls_from_css(css_text)
        updated = css_text

        for ref in refs:
            absu = join_and_clean(css_base_url, ref)
            if not absu:
                continue

            # If @import points to CSS, download and rewrite to local
            if ref.lower().endswith(".css") or "text/css" in ref.lower():
                if absu not in store.map_url_to_local:
                    try:
                        imported_text, imported_final = fetch_text(absu, session)
                        imported_local = store.local_path_for("css", imported_final, "text/css")
                        write_text(imported_local, imported_text)
                        store.map_url_to_local[absu] = imported_local
                        downloaded_css.append((imported_final, imported_local))
                    except Exception:
                        pass

                if absu in store.map_url_to_local:
                    local_rel = relpath_web(os.path.dirname(css_local), store.map_url_to_local[absu])
                    updated = updated.replace(ref, local_rel)
                continue

            # Otherwise treat as potential image/font; we’ll download if content-type is image/*
            local_rel = download_image(absu, session, store, os.path.dirname(css_local), downloaded_img)
            if local_rel:
                updated = updated.replace(ref, local_rel)

        if updated != css_text:
            write_text(css_local, updated)

    # ---- Save final HTML ----
    index_path = os.path.join(dest, "index.html")
    write_text(index_path, str(soup))

    print("\nDone.")
    print(f"Saved HTML: {index_path}")
    print(f"CSS files:  {len(downloaded_css)}")
    print(f"JS files:   {len(downloaded_js)}")
    print(f"Images:     {len(downloaded_img)}")

    if downloaded_css:
        print("\nSaved CSS (local paths):")
        for remote, local in downloaded_css[:15]:
            print(f"  - {local}  (from {remote})")
        if len(downloaded_css) > 15:
            print(f"  ... and {len(downloaded_css)-15} more")

    if downloaded_js:
        print("\nSaved JS (local paths):")
        for remote, local in downloaded_js[:15]:
            print(f"  - {local}  (from {remote})")
        if len(downloaded_js) > 15:
            print(f"  ... and {len(downloaded_js)-15} more")


def download_image(url: str, session: requests.Session, store: AssetStore, rel_base_dir: str, downloaded_list: list) -> str | None:
    """
    Downloads an image-like asset and returns a relative web path (from rel_base_dir)
    to the saved local file. rel_base_dir can be dest root (for HTML) or css file dir.
    """
    if url in store.map_url_to_local:
        return relpath_web(rel_base_dir, store.map_url_to_local[url])

    try:
        data, final_u, ctype = fetch_bytes(url, session)
    except Exception:
        return None

    if not ctype.startswith("image/"):
        # Not an image; skip (keeps it simple/safe)
        return None

    local = store.local_path_for("img", final_u, ctype)
    try:
        write_bytes(local, data)
    except Exception:
        return None

    store.map_url_to_local[url] = local
    downloaded_list.append((final_u, local))
    return relpath_web(rel_base_dir, local)


if __name__ == "__main__":
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        print("Missing dependency: beautifulsoup4")
        print("Install with: python3 -m pip install beautifulsoup4 requests")
        sys.exit(1)

    main()
