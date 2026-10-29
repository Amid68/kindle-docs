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
import os
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


# Ein einziger Selektor statt 58 einzelner: soupsieve ist reines Python, jeder Durchlauf kostet.
REMOVE_SELECTOR = ", ".join(REMOVE_SELECTORS)


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
        for sub in ("pages", "media"):
            (self.cache / sub).mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.local = threading.local()
        self.host_lock = {}
        self.host_next = {}
        self.host_delay = {}
        self.guard = threading.Lock()

    def session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = UA
            self.local.s = s
        return s

    def _wait(self, host, revalidate):
        with self.guard:
            lock = self.host_lock.setdefault(host, threading.Lock())
        with lock:
            now = time.monotonic()
            nxt = self.host_next.get(host, 0)
            if nxt > now:
                time.sleep(nxt - now)
            base = 0.1 if revalidate else self.delay
            self.host_next[host] = time.monotonic() + max(base, self.host_delay.get(host, 0))

    def get(self, url, binary=False, quiet=False):
        key = hashlib.sha1(url.encode()).hexdigest()
        folder = self.cache / ("media" if binary else "pages")
        body_f, meta_f = folder / (key + ".z"), folder / (key + ".json")
        meta = json.loads(meta_f.read_text()) if meta_f.exists() and body_f.exists() else None
        headers = {}
        if meta:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        host = urllib.parse.urlsplit(url).netloc
        r, err = None, None
        for attempt in range(6):
            self._wait(host, bool(headers))
            try:
                r = self.session().get(url, headers=headers, timeout=40)
            except requests.RequestException as e:
                r, err = None, e
            if r is not None and r.status_code not in (429, 500, 502, 503, 504):
                break
            pause = 5 * (attempt + 1)
            if r is not None and r.status_code in (429, 503):
                with self.guard:
                    self.host_delay[host] = min(3.0, max(0.5, self.host_delay.get(host, 0) * 2))
                retry = r.headers.get("Retry-After", "")
                if retry.isdigit():
                    pause = min(120, int(retry) + 1)
            time.sleep(pause)
        ok = r is not None and r.status_code in (200, 304)
        transient = r is None or r.status_code in (429, 500, 502, 503, 504)
        if not ok and meta and transient:
            log("using cached copy,", url, "answered", r.status_code if r is not None else err)
            r = None
        elif not ok:
            if not quiet:
                log("fetch failed", url, r.status_code if r is not None else err)
            return None
        if r is None or (r.status_code == 304 and meta):
            data = zlib.decompress(body_f.read_bytes())
        elif r.status_code == 200:
            data = r.content
            if not binary and (not meta or not body_f.exists() or zlib.decompress(body_f.read_bytes()) != data):
                self.local.changed = getattr(self.local, "changed", 0) + 1
            meta = {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"),
                    "final_url": r.url, "ctype": r.headers.get("Content-Type", "")}
            body_f.write_bytes(zlib.compress(data, 6))
            meta_f.write_text(json.dumps(meta))
        else:
            return None
        if binary:
            return meta["final_url"], meta.get("ctype", ""), data
        return meta["final_url"], data.decode("utf-8", "replace")


class GitWatch:
    """Letzter Commit im Doku-Ordner des Projekts, über die GitHub-API (ein Aufruf pro Buch)."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json", "User-Agent": UA})
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token and shutil.which("gh"):
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.releases = {}
        self.lock = threading.Lock()

    def _get(self, path):
        r = self.s.get("https://api.github.com/" + path, timeout=30)
        r.raise_for_status()
        return r.json()

    def branch(self, repo, pattern, release_repo=None):
        if "{minor}" not in pattern and "{version}" not in pattern:
            return pattern
        repo = release_repo or repo
        with self.lock:
            if repo not in self.releases:
                self.releases[repo] = self._get(f"repos/{repo}/releases/latest")["tag_name"]
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", self.releases[repo])
        return pattern.replace("{minor}", f"{m.group(1)}.{m.group(2)}").replace("{version}", m.group(0))

    def head(self, book):
        """(sha, Commit-Zeit) des letzten Commits im Doku-Ordner, None bei Fehlern."""
        cfg = book.cfg
        try:
            if cfg.get("branch_pattern") and not cfg.get("git_branch"):
                branch = latest_branch(f"https://github.com/{cfg['git']}.git", cfg["branch_pattern"])
            else:
                branch = self.branch(cfg["git"], cfg.get("git_branch", "main"), cfg.get("git_release_repo"))
            paths = cfg["git_path"] if isinstance(cfg["git_path"], list) else [cfg["git_path"]]
            newest = None
            for path in paths:
                c = self._get(f"repos/{cfg['git']}/commits?sha={branch}&path={path}&per_page=1")[0]
                when = dt.datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))
                if newest is None or when > newest[1]:
                    newest = (c["sha"], when)
            return newest
        except Exception as e:
            log(book.id, "git check failed:", repr(e)[:160])
            return None


# Ein neuer Commit erscheint erst nach dem Deploy auf der Website. Nur einem Crawl trauen, der
# mindestens so lange nach dem Commit lief; und jedes Buch spätestens nach einer Woche neu prüfen.
DEPLOY_GRACE = dt.timedelta(hours=6)
RECHECK_AFTER = dt.timedelta(days=7)


def can_skip(state, head, config_hash, now):
    if not state or not head or state.get("config") != config_hash or state.get("commit") != head[0]:
        return False
    crawled = dt.datetime.fromisoformat(state["crawled"])
    return crawled - head[1] >= DEPLOY_GRACE and now - crawled < RECHECK_AFTER


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
        got = fetcher.get(sm, quiet=True)
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
        elif clean_url(final) != clean_url(info[nxt]["url"]) and not book.in_scope(clean_url(final)):
            pages[nxt] = None
            continue
        soup = soup_of(text)
        content = find_content(soup, book.cfg.get("content"))
        pages[nxt] = (final, soup, content)
        merge_order(order, info, nav_links(book, soup, final, content, start=nxt == start_k))
    if book.cfg.get("sitemap", True) and len(pages) < book.max_pages:
        extra = [u for u in sitemap_urls(fetcher, book) if page_key(u) not in info]
        for u in extra:
            k = page_key(u)
            if k in info:
                continue
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
    result, seen = [], set()
    depths = [info[k]["depth"] for k in order if pages.get(k)]
    base = min(depths) if depths else 0
    for k in order:
        if pages.get(k) and page_key(pages[k][0]) not in seen:
            final, soup, content = pages[k]
            seen.add(page_key(final))
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


TOCTREE = re.compile(r"^\.\. toctree::[^\n]*\n((?:[ \t]+[^\n]*\n|[ \t]*\n)*)", re.M)
PLAIN_ROLES = {"ref", "doc", "term", "envvar", "abbr", "manpage", "rfc", "pep"}


def latest_branch(repo, pattern):
    out = subprocess.run(["git", "ls-remote", "--heads", repo], capture_output=True, text=True, check=True).stdout
    names = [line.split("refs/heads/")[1] for line in out.splitlines() if "refs/heads/" in line]
    found = [n for n in names if re.fullmatch(pattern, n)]
    return max(found, key=lambda n: [int(x) for x in re.findall(r"\d+", n)]) if found else None


def clone_docs(book, cache_dir):
    """Doku-Ordner eines Git-Repos flach und sparse klonen; liefert (Ordner, raw-URL-Basis für Bilder)."""
    cfg = book.cfg
    repo = cfg["repo"]
    branch = latest_branch(repo, cfg["branch_pattern"]) if cfg.get("branch_pattern") else cfg.get("branch", "main")
    root = cfg.get("root", "").strip("/")
    dest = Path(cache_dir) / "git" / book.id
    if dest.exists():
        shutil.rmtree(dest)
    if root:
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", branch,
                        repo, str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", root], check=True)
    else:
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", branch, repo, str(dest)], check=True)
    slug = re.sub(r"^https://github.com/|\.git$", "", repo)
    log(book.id, "from", slug, branch)
    raw = cfg.get("raw_base") or f"https://raw.githubusercontent.com/{slug}/{branch}/{root + '/' if root else ''}"
    return (dest / root) if root else dest, raw


def git_md_pages(book, cache_dir):
    """Markdown-Buch aus Git; die Kapitelreihenfolge steht in einer Listendatei (z. B. section_list.txt)."""
    cfg = book.cfg
    base, raw = clone_docs(book, cache_dir)
    names = cfg.get("files") or [line.strip() for line in (base / cfg["order_file"]).read_text().splitlines()]
    pages = []
    for name in names:
        if not name or name.startswith("#") or any(x.search(name) for x in book.exclude):
            continue
        f = base / name
        if not f.exists() or len(pages) >= book.max_pages:
            continue
        r = subprocess.run(["pandoc", "-f", cfg.get("markdown_format", "markdown"), "-t", "html", f.name],
                           cwd=f.parent, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        soup = soup_of(f"<html><body><div id='md'>{r.stdout}</div></body></html>")
        pages.append({"key": f"md:{name}", "url": raw + name, "soup": soup, "content": soup.select_one("#md"),
                      "level": 1})
    return pages


def listed_pages(fetcher, book):
    """Feste Liste einzelner Seiten aus verschiedenen offiziellen Quellen, ein Kapitel pro Seite."""
    pages = []
    for url in book.cfg["pages"]:
        got = fetch_page(fetcher, url)
        if not got:
            continue
        soup = soup_of(got[1])
        pages.append({"key": page_key(got[0]), "url": got[0], "soup": soup,
                      "content": find_content(soup, book.cfg.get("content")), "level": 1})
    return pages


def git_rst_pages(book, cache_dir):
    """Sphinx-Doku direkt aus dem Git-Repo: toctree-Reihenfolge abgehen, jede Datei mit pandoc wandeln.

    Für Seiten, die Anfragen aus Rechenzentren abweisen (docs.ansible.com antwortet GitHub mit 429)."""
    cfg = book.cfg
    base, raw = clone_docs(book, cache_dir)

    order, seen = [], set()

    def walk(doc, level):
        if doc in seen or len(order) >= book.max_pages or any(x.search(doc) for x in book.exclude):
            return
        f = base / f"{doc}.rst"
        if not f.exists():
            return
        seen.add(doc)
        order.append((doc, level))
        text = f.read_text(encoding="utf-8", errors="replace")
        for block in TOCTREE.findall(text):
            is_glob = ":glob:" in block
            for line in block.splitlines():
                entry = line.strip()
                if not entry or entry.startswith(":"):
                    continue
                m = re.search(r"<([^>]+)>$", entry)
                target = (m.group(1) if m else entry).strip()
                if target.startswith(("http://", "https://")):
                    continue
                path = target.lstrip("/") if target.startswith("/") else \
                    str(Path(doc).parent / target) if str(Path(doc).parent) != "." else target
                path = os.path.normpath(path)
                if is_glob and any(c in path for c in "*?["):
                    for g in sorted(base.glob(path + ".rst")):
                        walk(str(g.relative_to(base))[:-4], level + 1)
                else:
                    walk(path, level + 1)

    walk(cfg.get("index", "index"), 0)
    titles = {}
    heading = r"([^\n]+)\n[=\-~^\"'`#*+<>]{3,}\n"
    for doc, _ in order:
        text = (base / f"{doc}.rst").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^(?:[=\-~^\"'`#*+<>]{3,}\n)?" + heading, text, re.M)
        if m:
            titles["doc:" + doc] = re.sub(r"``(.*?)``", r"\1", m.group(1).strip())
        for lm in re.finditer(r"^\.\. _([^:\n]+):\s*\n(?:\s*\n|\.\. _[^\n]+\n)*(?:[=\-~^\"'`#*+<>]{3,}\n)?" + heading,
                              text, re.M):
            titles["ref:" + lm.group(1).strip().lower()] = re.sub(r"``(.*?)``", r"\1", lm.group(2).strip())
    pages = []
    for doc, level in order:
        f = base / f"{doc}.rst"
        r = subprocess.run(["pandoc", "-f", "rst", "-t", "html", str(f.name)], cwd=f.parent,
                           capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        soup = soup_of(f"<html><body><div id='rst'>{r.stdout}</div></body></html>")
        for code in soup.select("code[role]"):
            raw_text = code.get_text()
            label = re.sub(r"\s*<[^>]*>\s*$", "", raw_text).lstrip("~!")
            if "<" not in raw_text and code["role"] == "ref":
                label = titles.get("ref:" + label.lower(), label)
            elif "<" not in raw_text and code["role"] == "doc":
                target = os.path.normpath(str(Path(doc).parent / label)) if not label.startswith("/") else label.lstrip("/")
                label = titles.get("doc:" + target, label)
            if code["role"] in PLAIN_ROLES:
                code.replace_with(label)
            else:
                code.string = label
        url = cfg.get("web", "").rstrip("/") + f"/{doc}.html" if cfg.get("web") else raw + f"{doc}.rst"
        page = {"key": f"rst:{doc}", "url": raw + f"{doc}.rst", "soup": soup, "content": soup.select_one("#rst"),
                "level": max(1, min(3, level))}
        page["source"] = url
        pages.append(page)
    return pages


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
        for el in content.select(REMOVE_SELECTOR):
            if el.decomposed or (el.name == "header" and el.find("h1")):
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
        # Jede Doku-Seite wird eine eigene Kapiteldatei (Seitentitel h1-h3, pandoc teilt bei h3).
        # Überschriften in der Seite rutschen nach h4-h6, außer die Seite ist so groß, dass sie
        # selbst geteilt werden muss: große Kapiteldateien legen den Kindle lahm.
        inner = content.find_all(re.compile(r"^h[1-6]$"))
        size = len(str(content))
        shift = 3
        if size > 200_000 and inner:
            # Die flachste Überschriftenebene wählen, die die Seite in Teile von etwa 150 KB zerlegt.
            levels = sorted({int(hd.name[1]) for hd in inner})
            wanted = size // 150_000 + 1
            split = next((lv for lv in levels if sum(int(hd.name[1]) <= lv for hd in inner) >= wanted), levels[-1])
            shift = 3 - split
        for hd in inner:
            hd.name = f"h{max(2, min(6, int(hd.name[1]) + shift))}"
        if shift < 3:
            # pandoc teilt nur an Überschriften auf oberster Ebene: Hüllen um Abschnitte auflösen.
            for hd in inner:
                for parent in list(hd.parents):
                    if parent is content or parent.name is None:
                        break
                    if parent.name in ("div", "section", "article", "main"):
                        parent.unwrap()
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


# Hochzählen, wenn sich am erzeugten EPUB etwas ändert; dann werden alle Bücher neu gebaut.
OUTPUT_VERSION = "2"
BUILDER_VERSION = hashlib.sha256((OUTPUT_VERSION + (ROOT / "epub.css").read_text()).encode()).hexdigest()


def content_signature(body):
    """Nur sichtbarer Text und Bildanzahl: zufällige Attribute (Cloudflare-E-Mail-Schutz, IDs,
    Asset-Hashes) und Uhrzeiten aus Live-Beispielen (Caddy) sollen keinen Neubau auslösen."""
    soup = soup_of(body)
    text = " ".join(soup.get_text(" ").split())
    text = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", "", text)
    return f"{len(soup.find_all('img'))}\n{text}"


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


def build_book(fetcher, book, out_dir, work_dir, stamp, previous=None, force=False, config_hash=None):
    """Ein Buch crawlen; das EPUB nur neu bauen, wenn sich der bereinigte Text vom veröffentlichten unterscheidet."""
    log("start", book.id)
    fetcher.local.changed = 0
    if book.method == "git-rst":
        pages = git_rst_pages(book, fetcher.cache)
    elif book.method == "git-md":
        pages = git_md_pages(book, fetcher.cache)
    elif book.method == "pages":
        pages = listed_pages(fetcher, book)
    elif book.method == "print":
        pages = print_pages(fetcher, book)
    elif book.method == "single":
        pages = single_page(fetcher, book)
    else:
        pages = crawl(fetcher, book)
    if not pages:
        log("FAILED (no pages)", book.id)
        return None
    pages_sig = hashlib.sha256("\n".join(p["key"] for p in pages).encode()).hexdigest()
    out = Path(out_dir) / f"{book.id}.epub"
    if (not force and previous and out.exists() and book.method != "git-rst"
            and book.method not in ("git-md",)
            and fetcher.local.changed == 0 and previous.get("pages_sig") == pages_sig
            and previous.get("config") == config_hash):
        log("unchanged (no page changed on the server)", book.id)
        return {**previous, "status": "unchanged"}
    body = prepare(book, pages)
    fingerprint = hashlib.sha256("\0".join(
        [BUILDER_VERSION, book.title, book.cfg["category"], content_signature(body)]).encode()).hexdigest()
    legacy = hashlib.sha256("\0".join([BUILDER_VERSION, book.title, book.cfg["category"], body]).encode()).hexdigest()
    if not force and previous and previous.get("fingerprint") in (fingerprint, legacy) and out.exists():
        log("unchanged", book.id)
        return {**previous, "fingerprint": fingerprint, "pages_sig": pages_sig, "config": config_hash,
                "status": "unchanged"}
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
        "--toc", "--toc-depth=3", "--split-level=3", "--epub-title-page=false", f"--resource-path={wd}",
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
    with zipfile.ZipFile(tmp) as z:
        largest = max((i.file_size for i in z.infolist() if i.filename.endswith(".xhtml")), default=0)
    if largest > 1_000_000:
        log(f"WARNING {book.id}: largest chapter file is {largest // 1024} KB")
    tmp.replace(out)
    size = out.stat().st_size
    log(f"built {book.id}: {len(pages)} pages, {words} words, {size // 1024} KB")
    return {"id": book.id, "title": book.title, "category": book.cfg["category"], "source": book.start,
            "pages": len(pages), "words": words, "size": size, "built": stamp, "fingerprint": fingerprint,
            "pages_sig": pages_sig, "config": config_hash,
            "summary": book.cfg.get("summary", ""), "status": "built"}


def opds_entry(b, base):
    # Version nur in der Adresse: KOReaders OPDS-Sync merkt sich die Adresse des neuesten Buchs.
    # Die Eintrags-ID bleibt fest, sonst zeigt Bookshelf nach Updates Bücher doppelt (alte und neue ID).
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
    <link rel="http://opds-spec.org/acquisition" href="{base}books/{b['id']}.epub?v={b.get('fingerprint', '')[:10]}" type="application/epub+zip"/>
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
    started = dt.datetime.now(dt.timezone.utc)
    stamp = started.strftime("%Y-%m-%d")
    now = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    fetcher = Fetcher(args.cache)
    work = ROOT / ".work"
    watch = GitWatch()
    state_f = Path(args.cache) / "git-state.json"
    git_state = json.loads(state_f.read_text()) if state_f.exists() else {}
    skipped = []

    for book in books:
        if any(m in book.start or any(m in sc for sc in book.scopes) for m in ("{minor}", "{version}")):
            repo = book.cfg.get("git_release_repo") or book.cfg.get("git")
            book.start = watch.branch(repo, book.start, repo)
            book.scopes = [watch.branch(repo, sc, repo) for sc in book.scopes]
            log(book.id, "current version:", book.start)

    def run(book):
        try:
            previous = manifest.get(book.id)
            head = watch.head(book) if book.cfg.get("git") else None
            config_hash = hashlib.sha256((BUILDER_VERSION + json.dumps(book.cfg, sort_keys=True)).encode()).hexdigest()
            if (not args.force and previous and (site / "books" / f"{book.id}.epub").exists()
                    and can_skip(git_state.get(book.id), head, config_hash, started)):
                log("unchanged (no new commit)", book.id)
                skipped.append(book.id)
                return book, {**previous, "status": "unchanged"}
            res = build_book(fetcher, book, site / "books", work, stamp, previous, args.force, config_hash)
            if res and head:
                git_state[book.id] = {"commit": head[0], "crawled": started.isoformat(), "config": config_hash}
            elif not head:
                git_state.pop(book.id, None)
            return book, res
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
    state_f.write_text(json.dumps(git_state, indent=1, sort_keys=True) + "\n")
    log(f"crawled: {len(books) - len(skipped)}, skipped without new commit: {len(skipped)}")
    log(f"catalog: {len(entries)} books, rebuilt: {', '.join(built) or 'none'}"
        + (f", failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed and not entries else 0


if __name__ == "__main__":
    sys.exit(main())
