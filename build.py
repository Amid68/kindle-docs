#!/usr/bin/env python3
"""Baut EPUBs aus Live-Dokuseiten und einen OPDS-Katalog dafür."""

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import html
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

import requests
import warnings

from bs4 import BeautifulSoup, Comment, XMLParsedAsHTMLWarning
from PIL import Image, ImageDraw, ImageFont

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
ROOT = Path(__file__).resolve().parent
UA = "kindle-docs/1.0 (+https://github.com/Amid68/kindle-docs; nightly e-reader build)"
PAGE_EXCLUDE = re.compile(
    r"/(search|tags?|categories|blog|news|community|_print|404|genindex|py-modindex|"
    r"_modules|_sources|_static|_images|feed|rss|print)(/|\.html|$)|\.(pdf|png|jpe?g|gif|svg|webp|zip|gz|tgz|"
    r"xml|json|txt|ya?ml|md|csv|ico|css|js|epub|mp4|woff2?)$",
    re.I,
)
NAV_CANDIDATES = (
    "nav, aside, [role=navigation], [class*=sidebar], [class*=side-bar], [class*=menu], "
    "[class*=toctree], [id*=sidebar], [id*=menu], [class*=nav]"
)
CONTENT_SELECTORS = [
    ".td-content", "article.md-content__inner", ".md-content article", ".theme-doc-markdown",
    "[itemprop=articleBody]", ".rst-content [role=main]", "div.body[role=main]", "article[role=main]",
    ".document .body", "#main-content", "main article", "article", "[role=main]", "main", "#content",
    ".content", "body",
]
REMOVE_SELECTORS = [
    "script", "style", "noscript", "iframe", "form", "button", "input", "select", "template", "nav",
    "footer", "link", "meta", ".headerlink", "a.anchor", ".hash-link", ".anchor-link", ".md-source-file",
    ".md-content__button", ".td-page-meta", ".td-toc", "#TableOfContents", ".feedback", "[class*=feedback]",
    ".pagination-nav", ".md-footer", ".theme-doc-footer", ".theme-doc-toc-mobile", ".theme-edit-this-page",
    "[class*=breadcrumb]", ".prev-next", ".rst-footer-buttons", ".copybtn", "[class*=copy-button]",
    "[class*=copyButton]", "[class*=edit-page]", "[class*=edit-this-page]", ".contents.local", ".sr-only",
    ".visually-hidden", "[aria-hidden=true]", ".d-print-none", ".td-content > .td-page-meta", "aside.toc", "aside[class*=toc]",
    "aside[class*=sidebar]", "[class*=announcement]", "[class*=banner]", ".last-updated", ".page-meta",
]


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def page_key(url):
    p = urllib.parse.urlsplit(url)
    path = re.sub(r"/index\.html?$", "/", p.path)
    return f"{p.netloc.lower()}{path.rstrip('/')}"


def clean_url(url):
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


class Fetcher:
    """HTTP-GET mit Plattencache (bedingte Anfragen per ETag) und Pause pro Host."""

    def __init__(self, cache_dir, delay=0.25):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.local = threading.local()
        self.host_lock = {}
        self.host_next = {}
        self.guard = threading.Lock()

    def session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = UA
            self.local.s = s
        return s

    def _wait(self, host):
        with self.guard:
            lock = self.host_lock.setdefault(host, threading.Lock())
        with lock:
            now = time.monotonic()
            nxt = self.host_next.get(host, 0)
            if nxt > now:
                time.sleep(nxt - now)
            self.host_next[host] = time.monotonic() + self.delay

    def get(self, url, binary=False):
        key = hashlib.sha1(url.encode()).hexdigest()
        body_f, meta_f = self.cache / key, self.cache / (key + ".json")
        meta = json.loads(meta_f.read_text()) if meta_f.exists() and body_f.exists() else None
        headers = {}
        if meta:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        r = None
        for attempt in range(4):
            self._wait(urllib.parse.urlsplit(url).netloc)
            try:
                r = self.session().get(url, headers=headers, timeout=40)
            except requests.RequestException as e:
                r = None
                err = e
            if r is not None and r.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(2 * (attempt + 1))
        if r is None:
            log("fetch failed", url, err)
            return None
        if r.status_code == 304 and meta:
            data = body_f.read_bytes()
        elif r.status_code == 200:
            data = r.content
            meta = {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"),
                    "final_url": r.url, "ctype": r.headers.get("Content-Type", "")}
            body_f.write_bytes(data)
            meta_f.write_text(json.dumps(meta))
        else:
            return None
        if binary:
            return meta["final_url"], meta.get("ctype", ""), data
        return meta["final_url"], data.decode("utf-8", "replace")


class Book:
    def __init__(self, cfg, defaults):
        self.cfg = {**defaults, **cfg}
        self.id = self.cfg["id"]
        self.title = self.cfg["title"]
        self.start = self.cfg["start"]
        self.method = self.cfg.get("method", "crawl")
        scope = self.cfg.get("scope") or self.start.rsplit("/", 1)[0] + "/"
        self.scopes = [scope] if isinstance(scope, str) else scope
        self.exclude = [re.compile(x) for x in self.cfg.get("exclude", [])]
        self.max_pages = int(self.cfg.get("max_pages", 400))

    def in_scope(self, url):
        if not url.startswith(("http://", "https://")):
            return False
        if not any(url.startswith(s) or url.rstrip("/") == s.rstrip("/") for s in self.scopes):
            return False
        path = urllib.parse.urlsplit(url).path
        if PAGE_EXCLUDE.search(path):
            return False
        return not any(x.search(url) for x in self.exclude)


def soup_of(text):
    return BeautifulSoup(text, "lxml")


META_REFRESH = re.compile(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>]+)', re.I)


def fetch_page(fetcher, url):
    """Seite holen und bis zu drei <meta http-equiv="refresh">-Weiterleitungen folgen."""
    for _ in range(4):
        got = fetcher.get(url)
        if not got:
            return None
        m = META_REFRESH.search(got[1][:4000])
        if not m:
            return got
        url = urllib.parse.urljoin(got[0], html.unescape(m.group(1).strip()))
    return got


def rebase_scope(book, final):
    """Ist die Startseite umgezogen, zieht der Standard-Scope des Buchs mit."""
    if final != book.start and not book.cfg.get("scope") and not book.in_scope(final):
        book.scopes = [final.rsplit("/", 1)[0] + "/"]
        log(book.id, "moved to", final)


def find_content(soup, selector=None):
    if selector:
        el = soup.select_one(selector)
        if el:
            return el
    for sel in CONTENT_SELECTORS:
        for el in soup.select(sel):
            if len(el.get_text(" ", strip=True)) > 150:
                return el
    return soup.body or soup


def links_of(book, el, url):
    out, seen = [], set()
    for a in el.find_all("a", href=True):
        u = clean_url(urllib.parse.urljoin(url, a["href"]))
        k = page_key(u)
        if k in seen or not book.in_scope(u):
            continue
        seen.add(k)
        depth = len([p for p in a.parents if p.name in ("ul", "ol")])
        out.append((k, u, depth))
    return out


def nav_links(book, soup, url, content, start=False):
    """Links des Seitenmenüs im Scope, in Reihenfolge, mit Verschachtelungstiefe.

    Seiten ohne Seitenmenü (nginx, MediaWiki, Man-Page-Bücher) haben ihr
    Inhaltsverzeichnis im Text der Startseite, dann wird das genommen."""
    best = _nav_links(book, soup, url, content)
    if (start or book.cfg.get("follow_content")) and len(best) < 8 and content is not None:
        body = links_of(book, content, url)
        if len(body) > len(best):
            return body
    return best


def _nav_links(book, soup, url, content):
    if book.cfg.get("nav"):
        el = soup.select_one(book.cfg["nav"])
        return links_of(book, el, url) if el else []
    best = []
    for el in soup.select(NAV_CANDIDATES):
        if content is not None and (el is content or content in el.descendants):
            continue
        found = links_of(book, el, url)
        if len(found) > len(best):
            best = found
    return best


def merge_order(order, info, links):
    last = None
    for k, u, depth in links:
        if k in info:
            if k in order:
                last = order.index(k)
            continue
        info[k] = {"url": u, "depth": depth}
        if last is None:
            order.append(k)
            last = len(order) - 1
        else:
            order.insert(last + 1, k)
            last += 1


def sitemap_urls(fetcher, book):
    p = urllib.parse.urlsplit(book.start)
    todo = [f"{p.scheme}://{p.netloc}/sitemap.xml"]
    for s in book.scopes:
        todo.append(s.rstrip("/") + "/sitemap.xml")
    seen, urls = set(), []
    while todo and len(seen) < 30:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        got = fetcher.get(sm)
        if not got or "<loc>" not in got[1]:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", got[1]):
            loc = html.unescape(loc)
            if loc.endswith(".xml"):
                if any(loc.startswith(s) for s in book.scopes) or "sitemap" in loc:
                    todo.append(loc)
            elif book.in_scope(loc):
                urls.append(clean_url(loc))
    return urls


def crawl(fetcher, book):
    """Doku in Menü-Reihenfolge abgehen; eingeklappte Menüs öffnen sich beim Besuch der Seiten."""
    order, info, pages = [], {}, {}
    start_k = page_key(book.start)
    info[start_k] = {"url": book.start, "depth": 0}
    order.append(start_k)
    while len(pages) < book.max_pages:
        nxt = next((k for k in order if k not in pages), None)
        if nxt is None:
            break
        got = fetch_page(fetcher, info[nxt]["url"])
        if not got:
            pages[nxt] = None
            continue
        final, text = got
        if nxt == start_k:
            rebase_scope(book, final)
            info[page_key(final)] = info[start_k]
        soup = soup_of(text)
        content = find_content(soup, book.cfg.get("content"))
        pages[nxt] = (final, soup, content)
        merge_order(order, info, nav_links(book, soup, final, content, start=nxt == start_k))
    if book.cfg.get("sitemap", True) and len(pages) < book.max_pages:
        extra = [u for u in sitemap_urls(fetcher, book) if page_key(u) not in info]
        for u in extra:
            k = page_key(u)
            path = urllib.parse.urlsplit(u).path
            best, best_len = None, -1
            for i, ok in enumerate(order):
                op = urllib.parse.urlsplit(info[ok]["url"]).path
                common = _common(op, path)
                if common >= best_len:
                    best, best_len = i, common
            parent_depth = info[order[best]]["depth"] if best is not None else 0
            info[k] = {"url": u, "depth": parent_depth}
            order.insert((best + 1) if best is not None else len(order), k)
        for k in order:
            if len(pages) >= book.max_pages:
                break
            if k in pages:
                continue
            got = fetcher.get(info[k]["url"])
            if not got:
                pages[k] = None
                continue
            soup = soup_of(got[1])
            pages[k] = (got[0], soup, find_content(soup, book.cfg.get("content")))
    result = []
    depths = [info[k]["depth"] for k in order if pages.get(k)]
    base = min(depths) if depths else 0
    for k in order:
        if pages.get(k):
            final, soup, content = pages[k]
            result.append({"key": k, "url": final, "soup": soup, "content": content,
                           "level": max(1, min(3, info[k]["depth"] - base + 1)) if k != start_k else 1})
    return result


def _common(a, b):
    pa, pb = [x for x in a.split("/") if x], [x for x in b.split("/") if x]
    n = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        n += 1
    return n


def print_pages(fetcher, book):
    """Docsy-Druckansicht `_print`: ein Abruf enthält den ganzen Abschnitt, ein .td-content pro Seite."""
    got = fetch_page(fetcher, book.start)
    if not got:
        return []
    final, text = got
    soup = soup_of(text)
    result = []
    for i, el in enumerate(soup.select(".td-content")):
        h = el.find("h1")
        title = h.get_text(" ", strip=True) if h else ""
        m = re.match(r"^((\d+\.)*\d+)\s+-\s+(.*)$", title)
        level = 1
        if m:
            level = min(3, m.group(1).count(".") + 1)
            if h:
                h.string = m.group(3)
        for ul in el.find_all("ul", recursive=False):
            hrefs = [a.get("href", "") for a in ul.find_all("a")]
            if hrefs and all(h.startswith("#pg-") for h in hrefs):
                ul.decompose()
        anchor = el.find(id=re.compile(r"^pg-"))
        result.append({"key": f"print-{i}", "url": final, "soup": soup, "content": el, "level": level,
                       "anchor": anchor.get("id") if anchor else None})
    return result


def single_page(fetcher, book):
    got = fetch_page(fetcher, book.start)
    if not got:
        return []
    soup = soup_of(got[1])
    return [{"key": page_key(got[0]), "url": got[0], "soup": soup,
             "content": find_content(soup, book.cfg.get("content")), "level": 1}]


def page_title(page):
    h = page["content"].find("h1")
    if h and h.get_text(strip=True):
        return h.get_text(" ", strip=True), h
    t = page["soup"].title.get_text(" ", strip=True) if page["soup"].title else page["url"]
    return re.split(r"\s+[|—–·-]\s+", t)[0].strip() or t, None


class ImageStore:
    def __init__(self, fetcher, out_dir):
        self.fetcher = fetcher
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.done = {}

    def local(self, url):
        if url in self.done:
            return self.done[url]
        self.done[url] = None
        base = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1].lower()
        if re.search(r"copy|clipboard|icon|logo|badge|avatar|spinner", base):
            return None
        got = self.fetcher.get(url, binary=True)
        if not got:
            return None
        _, ctype, data = got
        name = hashlib.sha1(url.encode()).hexdigest()[:16]
        if "svg" in ctype or url.lower().split("?")[0].endswith(".svg"):
            if len(data) > 400_000:
                return None
            path = self.dir / f"{name}.svg"
            path.write_bytes(data)
        else:
            try:
                im = Image.open(io.BytesIO(data))
                im.seek(0)
                if im.width < 24 or im.height < 24:
                    return None
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, "white")
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.thumbnail((1000, 1400))
                path = self.dir / f"{name}.jpg"
                im.save(path, "JPEG", quality=72, optimize=True)
            except Exception:
                return None
        self.done[url] = path.name
        return path.name


def kroki_url(source):
    data = base64.urlsafe_b64encode(zlib.compress(source.encode("utf-8"), 9)).decode()
    return f"https://kroki.io/mermaid/png/{data}"


def prepare(book, pages):
    """Seiten zu einem HTML-Dokument zusammenführen, mit eindeutigen IDs und Links innerhalb des Buchs."""
    pid_of = {}
    anchor_pid = {}
    for i, p in enumerate(pages):
        p["pid"] = f"p{i}"
        pid_of.setdefault(p["key"], p["pid"])
        if p.get("anchor"):
            anchor_pid[p["anchor"]] = p["pid"]
    body = []
    for p in pages:
        content = p["content"]
        for c in content.find_all(string=lambda s: isinstance(s, Comment)):
            c.extract()
        for sel in REMOVE_SELECTORS:
            for el in content.select(sel):
                if el.name == "header" and el.find("h1"):
                    continue
                el.decompose()
        title, h = page_title(p)
        title = re.sub(r"[\s¶#\uf0c1]+$", "", title)
        if h is not None:
            h.decompose()
        for el in content.select("td.linenos, .linenodiv, .lineno, .linenos"):
            el.decompose()
        for el in content.select(".mermaid, pre > code.language-mermaid"):
            target = el.parent if el.name == "code" else el
            src = el.get_text().strip()
            if src:
                img = p["soup"].new_tag("img", src=kroki_url(src), alt="diagram")
                target.replace_with(img)
        for pre in content.find_all("pre"):
            for br in pre.find_all("br"):
                br.replace_with("\n")
            text = pre.get_text().strip("\n")
            pre.clear()
            code = p["soup"].new_tag("code")
            code.string = text
            pre.append(code)
        for svg in content.find_all("svg"):
            if svg.find_parent(["a", "span", "button", "summary", "h1", "h2", "h3", "h4", "h5", "h6"]):
                svg.decompose()
        for el in content.find_all(True):
            if el.get("id"):
                el["id"] = f"{p['pid']}--{el['id']}"
            if el.name in ("main", "body", "html"):
                el.name = "div"
            for attr in ("class", "style", "onclick", "data-src", "srcset", "loading", "data-lang", "role"):
                if attr in el.attrs and not (attr == "class" and el.name in ("pre", "code")):
                    if attr == "data-src" and el.name == "img" and not el.get("src"):
                        el["src"] = el["data-src"]
                    del el[attr]
        for a in content.find_all("a", href=True):
            href = a["href"]
            if href.startswith("#"):
                frag = href[1:]
                if frag in anchor_pid:
                    a["href"] = "#" + anchor_pid[frag]
                elif frag:
                    a["href"] = f"#{p['pid']}--{frag}"
                continue
            absu = urllib.parse.urljoin(p["url"], href)
            parts = urllib.parse.urlsplit(absu)
            k = page_key(clean_url(absu))
            if k in pid_of and parts.scheme in ("http", "https"):
                target = pid_of[k]
                a["href"] = f"#{target}--{parts.fragment}" if parts.fragment else f"#{target}"
            else:
                a["href"] = absu
        for img in content.find_all("img"):
            src = img.get("src")
            if src and not src.startswith("data:"):
                img.attrs = {"src": urllib.parse.urljoin(p["url"], src), "alt": img.get("alt", "")}
            else:
                img.decompose()
        for pic in content.find_all(["picture", "source"]):
            if pic.name == "source":
                pic.decompose()
            else:
                pic.unwrap()
        shift = p["level"]
        for hd in content.find_all(re.compile(r"^h[1-6]$")):
            hd.name = f"h{min(6, int(hd.name[1]) + shift)}"
        body.append(f'<h{p["level"]} id="{p["pid"]}">{html.escape(title)}</h{p["level"]}>')
        body.append(content.decode_contents())
    return "\n".join(body)


IMG_TAG = re.compile(r"<img\b[^>]*>")
IMG_SRC = re.compile(r'\ssrc="([^"]*)"')


def localize_images(body, images):
    """Jedes Bild einmal laden, für E-Ink verkleinern und das Tag auf die lokale Kopie zeigen lassen."""
    def sub(m):
        src = IMG_SRC.search(m.group(0))
        name = images.local(html.unescape(src.group(1))) if src else None
        return m.group(0).replace(src.group(1), f"img/{name}", 1) if name else ""
    return IMG_TAG.sub(sub, body)


def find_font(size, bold=False):
    names = (["DejaVuSans-Bold.ttf", "Helvetica.ttc", "Arial Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "Helvetica.ttc", "Arial.ttf"])
    dirs = ["/usr/share/fonts/truetype/dejavu", "/System/Library/Fonts", "/Library/Fonts",
            "/System/Library/Fonts/Supplemental"]
    for n in names:
        for d in dirs:
            f = Path(d) / n
            if f.exists():
                try:
                    return ImageFont.truetype(str(f), size, index=1 if bold and n.endswith(".ttc") else 0)
                except Exception:
                    continue
    return ImageFont.load_default(size=size)


def wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def make_cover(path, title, category, stamp):
    im = Image.new("L", (600, 800), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 600, 70], fill=0)
    d.text((30, 20), category.upper(), font=find_font(26, True), fill=255)
    y = 220
    big = find_font(56, True)
    for line in wrap(d, title, big, 540):
        d.text((30, y), line, font=big, fill=0)
        y += 68
    d.line([30, y + 20, 570, y + 20], fill=0, width=3)
    small = find_font(26)
    d.text((30, y + 45), "Official documentation", font=small, fill=0)
    d.text((30, 730), f"Updated {stamp}", font=small, fill=0)
    im.save(path, "JPEG", quality=85)


BUILDER_VERSION = hashlib.sha256((ROOT / "build.py").read_bytes() + (ROOT / "epub.css").read_bytes()).hexdigest()


def drop_nav_from_spine(epub):
    """Inhaltsverzeichnis nur im Menü des Readers zeigen, nicht als Seiten vor Kapitel 1."""
    tmp = epub.with_suffix(".rezip")
    with zipfile.ZipFile(epub) as src, zipfile.ZipFile(tmp, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.endswith(".opf"):
                data = re.sub(rb'\s*<itemref idref="nav"[^>]*/>', b"", data)
            compress = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            dst.writestr(info.filename, data, compress_type=compress)
    tmp.replace(epub)


def build_book(fetcher, book, out_dir, work_dir, stamp, previous=None, force=False):
    """Ein Buch crawlen; das EPUB nur neu bauen, wenn sich der bereinigte Text vom veröffentlichten unterscheidet."""
    log("start", book.id)
    if book.method == "print":
        pages = print_pages(fetcher, book)
    elif book.method == "single":
        pages = single_page(fetcher, book)
    else:
        pages = crawl(fetcher, book)
    if not pages:
        log("FAILED (no pages)", book.id)
        return None
    body = prepare(book, pages)
    fingerprint = hashlib.sha256("\0".join(
        [BUILDER_VERSION, book.title, book.cfg["category"], body]).encode()).hexdigest()
    out = Path(out_dir) / f"{book.id}.epub"
    if not force and previous and previous.get("fingerprint") == fingerprint and out.exists():
        log("unchanged", book.id)
        return {**previous, "status": "unchanged"}
    wd = Path(work_dir) / book.id
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    body = localize_images(body, ImageStore(fetcher, wd / "img"))
    words = len(re.sub(r"<[^>]+>", " ", body).split())
    about = (
        f'<h1 id="about">About this book</h1>'
        f"<p>This book is the official <b>{html.escape(book.title)}</b> documentation, converted for "
        f"e-readers. This version was made on <b>{stamp}</b> from the live site:</p>"
        f'<p><a href="{html.escape(book.start)}">{html.escape(book.start)}</a></p>'
        f"<p>{len(pages)} pages. The site is checked every night and a new version is published whenever "
        f"its text changes. The date on the cover tells you which version you have.</p>"
        f"<p>{html.escape(book.cfg.get('license', 'Content belongs to its authors under the project license.'))}</p>"
    )
    doc = (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(book.title)}</title></head>'
           f"<body>{about}{body}</body></html>")
    (wd / "book.html").write_text(doc, encoding="utf-8")
    cover = wd / "cover.jpg"
    make_cover(cover, book.title, book.cfg["category"], stamp)
    tmp = out.with_suffix(".epub.part")
    cmd = [
        "pandoc", str(wd / "book.html"), "-f", "html", "-t", "epub3", "-o", str(tmp),
        "--toc", "--toc-depth=3", "--split-level=1", "--epub-title-page=false", f"--resource-path={wd}",
        f"--epub-cover-image={cover}", f"--css={ROOT / 'epub.css'}",
        "--metadata", f"title={book.title}",
        "--metadata", f"author={book.cfg.get('author', book.title + ' authors')}",
        "--metadata", f"date={stamp}",
        "--metadata", "lang=en",
        "--metadata", f"rights={book.cfg.get('license', '')}",
        "--metadata", f"description=Official {book.title} documentation from {book.start}, version of {stamp}.",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        log("FAILED (pandoc)", book.id, r.stderr[-800:])
        return None
    drop_nav_from_spine(tmp)
    tmp.replace(out)
    size = out.stat().st_size
    log(f"built {book.id}: {len(pages)} pages, {words} words, {size // 1024} KB")
    return {"id": book.id, "title": book.title, "category": book.cfg["category"], "source": book.start,
            "pages": len(pages), "words": words, "size": size, "built": stamp, "fingerprint": fingerprint,
            "summary": book.cfg.get("summary", ""), "status": "built"}


def opds_entry(b, base):
    upd = b["updated"]
    summary = (f"{b['summary']} " if b.get("summary") else "") + \
        f"Built {b['built']} from {b['source']} ({b['pages']} pages, {b['size'] // 1024} KB)."
    return f"""  <entry>
    <title>{escape(b['title'])}</title>
    <id>urn:kindle-docs:{b['id']}</id>
    <updated>{upd}</updated>
    <author><name>{escape(b['title'])} project</name></author>
    <dc:language>en</dc:language>
    <summary>{escape(summary)}</summary>
    <link rel="http://opds-spec.org/acquisition" href="{base}books/{b['id']}.epub" type="application/epub+zip"/>
    <link rel="http://opds-spec.org/image" href="{base}covers/{b['id']}.jpg" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{base}covers/{b['id']}.jpg" type="image/jpeg"/>
  </entry>"""


def opds_feed(fid, title, entries, base, kind, now, extra_links=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:kindle-docs:{fid}</id>
  <title>{escape(title)}</title>
  <updated>{now}</updated>
  <author><name>kindle-docs</name></author>
  <link rel="self" href="{base}{fid}.xml" type="application/atom+xml;profile=opds-catalog;kind={kind}"/>
  <link rel="start" href="{base}catalog.xml" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{extra_links}{chr(10).join(entries)}
</feed>
"""


def write_catalog(site, books, categories, base):
    now = max((b["updated"] for b in books), default="1970-01-01T00:00:00Z")
    nav = []
    def nav_entry(fid, title, content):
        return f"""  <entry>
    <title>{escape(title)}</title>
    <id>urn:kindle-docs:{fid}</id>
    <updated>{now}</updated>
    <content type="text">{escape(content)}</content>
    <link rel="subsection" href="{base}{fid}.xml" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>"""
    nav.append(nav_entry("recent", "Recently updated", "Books whose content changed most recently"))
    nav.append(nav_entry("all", "All documentation", f"All {len(books)} books"))
    for cat in categories:
        items = [b for b in books if b["category"] == cat]
        if not items:
            continue
        fid = "cat-" + re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-")
        nav.append(nav_entry(fid, cat, ", ".join(b["title"] for b in items)))
        (site / f"{fid}.xml").write_text(opds_feed(fid, cat, [opds_entry(b, base) for b in items], base,
                                                    "acquisition", now))
    allb = sorted(books, key=lambda b: b["title"].lower())
    (site / "all.xml").write_text(opds_feed("all", "All documentation", [opds_entry(b, base) for b in allb],
                                            base, "acquisition", now))
    recent = sorted(books, key=lambda b: b["updated"], reverse=True)
    (site / "recent.xml").write_text(opds_feed("recent", "Recently updated", [opds_entry(b, base) for b in recent],
                                               base, "acquisition", now))
    (site / "catalog.xml").write_text(opds_feed("catalog", "DevOps & Security Docs", nav, base, "navigation", now))
    rows = "\n".join(
        f'<tr><td><a href="books/{b["id"]}.epub">{html.escape(b["title"])}</a></td><td>{html.escape(b["category"])}</td>'
        f'<td>{b["pages"]}</td><td>{b["size"] // 1024} KB</td><td>{b["updated"][:10]}</td></tr>' for b in allb)
    (site / "index.html").write_text(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>DevOps &amp; Security Docs</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ccc;padding:6px;text-align:left}}</style>
</head><body><h1>DevOps &amp; Security Docs</h1>
<p>Official documentation of {len(books)} projects, rebuilt every night from the live sites as EPUB.
OPDS catalog for e-readers: <code>{base}catalog.xml</code></p>
<table><tr><th>Book</th><th>Category</th><th>Pages</th><th>Size</th><th>Content changed</th></tr>
{rows}</table><p>Newest change: {now}. Every site is checked nightly; a book is rebuilt only when its text changes.</p></body></html>
""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="build just these book ids")
    ap.add_argument("--site", default=str(ROOT / "site"))
    ap.add_argument("--cache", default=str(ROOT / ".cache"))
    ap.add_argument("--base", default="https://amid68.github.io/kindle-docs/")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--force", action="store_true", help="rebuild even when nothing changed")
    args = ap.parse_args()

    cfg = tomllib.loads((ROOT / "books.toml").read_text())
    defaults = cfg.get("defaults", {})
    categories = cfg["categories"]
    books = []
    for cat in categories:
        for b in cfg.get("book", []):
            if b["category"] == cat:
                books.append(Book(b, defaults))
    if args.only:
        books = [b for b in books if b.id in args.only]
    if args.max_pages:
        for b in books:
            b.max_pages = min(b.max_pages, args.max_pages)

    site = Path(args.site)
    (site / "books").mkdir(parents=True, exist_ok=True)
    (site / "covers").mkdir(parents=True, exist_ok=True)
    manifest_f = site / "manifest.json"
    manifest = {b["id"]: b for b in json.loads(manifest_f.read_text())} if manifest_f.exists() else {}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetcher = Fetcher(args.cache)
    work = ROOT / ".work"

    def run(book):
        try:
            return book, build_book(fetcher, book, site / "books", work, stamp, manifest.get(book.id), args.force)
        except Exception as e:
            log("FAILED (error)", book.id, repr(e))
            return book, None

    failed, built = [], []
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        for book, res in ex.map(run, books):
            if not res:
                failed.append(book.id)
                continue
            status = res.pop("status")
            if status == "built":
                res["updated"] = now
                built.append(book.id)
                shutil.copy(work / book.id / "cover.jpg", site / "covers" / f"{book.id}.jpg")
            manifest[book.id] = res
    known = {b["id"] for b in cfg.get("book", [])}
    for gone in set(manifest) - known:
        log("removed", gone)
        for f in (site / "books" / f"{gone}.epub", site / "covers" / f"{gone}.jpg"):
            f.unlink(missing_ok=True)
    rank = {b["id"]: i for i, b in enumerate(cfg.get("book", []))}
    entries = sorted((b for b in manifest.values() if b["id"] in known and (site / "books" / f"{b['id']}.epub").exists()),
                     key=lambda b: rank[b["id"]])
    manifest_f.write_text(json.dumps(sorted(entries, key=lambda b: b["id"]), indent=1) + "\n")
    write_catalog(site, entries, categories, args.base)
    (ROOT / "changes.txt").write_text("".join(f"{b}\n" for b in built))
    log(f"catalog: {len(entries)} books, rebuilt: {', '.join(built) or 'none'}"
        + (f", failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed and not entries else 0


if __name__ == "__main__":
    sys.exit(main())
