"""Find the full recipe on the web when the post itself does not contain it.

Where it looks, in order:
  1. links in the caption or in the creator's own comments,
  2. the creator's bio links: link pages (Linktree and similar, or a "/links" page on the
     creator's site) and the creator's own website, searched for the dish through the
     WordPress search API or the sitemap.

How pages are read:
  - schema.org Recipe data (JSON-LD), which most recipe sites embed, is used word for word,
  - otherwise the readable page text is returned for Gemini to read (text only, no media).

Rules:
  - A page is only accepted when its recipe name or title matches the dish of the post
    (and, when known, its ingredients), so a different recipe of the same creator is never used.
  - Paid content (paywalled posts, recipes sold as products, courses) is never used.
  - Only public http(s) hosts are fetched; every redirect hop is checked again.
  - Pages are fetched directly first (free). When a site blocks plain requests (403, 429,
    503, Cloudflare, TLS problems) the same URL is fetched through Apify's cheerio-scraper.
"""
from __future__ import annotations

import html as htmllib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable
from urllib.parse import quote_plus, urljoin, urlparse

import requests

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
_TEXT_TYPES = ("text/html", "application/xhtml", "application/json", "application/ld+json",
               "application/xml", "text/xml", "text/plain", "application/rss")


class WebError(Exception):
    """The page cannot be used (bad URL, private host, 404, not a web page, ...)."""


class Blocked(WebError):
    """The site refused a plain request; worth retrying through Apify."""


def _log(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- hosts and URLs


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _host_in(host: str, names: list[str]) -> bool:
    """True if host equals one of names or is a subdomain of one. "amazon.*" matches any TLD."""
    for n in names:
        n = n.lower().strip()
        if not n:
            continue
        if n.endswith(".*"):
            base = n[:-2]
            if re.search(rf"(^|\.){re.escape(base)}\.[a-z.]+$", host):
                return True
        elif host == n or host.endswith("." + n):
            return True
    return False


def _check_public(url: str) -> None:
    """Raise WebError unless url is http(s) on a public host (no private, loopback or link-local IPs)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise WebError(f"not a public web link: {url[:120]}")
    if p.port not in (None, 80, 443):
        raise WebError(f"unusual port in {url[:120]}")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        raise WebError(f"host not found: {p.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise WebError(f"{p.hostname} is not a public host")


# Links in text: full URLs, "www." links and bare domains with a path ("site.com/recipe").
_URL_TEXT_RE = re.compile(
    r"(?i)(?<![@\w.])(?:https?://[^\s<>()\"']+|www\.[^\s<>()\"']+|"
    r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|co|io|blog|recipes|kitchen|food|me|uk|de|fr|ro|au|ca)"
    r"/[^\s<>()\"']*)"
)


def links_in_text(text: str) -> list[str]:
    out = []
    for m in _URL_TEXT_RE.findall(text or ""):
        u = m.rstrip(".,!?;:)]}'\"")
        if not u.lower().startswith("http"):
            u = "https://" + u
        out.append(u)
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- fetching


@dataclass
class Page:
    url: str  # final URL after redirects
    status: int
    ctype: str
    text: str
    via: str  # "direct" | "apify"


def fetch_direct(url: str, wcfg: dict, session: requests.Session | None = None) -> Page:
    """GET a public page, following up to 5 redirects and checking every hop."""
    s = session or requests.Session()
    limit = int(float(wcfg.get("max_page_mb", 3)) * 1024 * 1024)
    timeout = float(wcfg.get("timeout_s", 12))
    for _ in range(6):
        _check_public(url)
        try:
            r = s.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"},
                      timeout=timeout, allow_redirects=False, stream=True)
        except requests.exceptions.SSLError:
            raise Blocked("TLS error") from None
        except requests.RequestException as e:
            raise Blocked(type(e).__name__) from None
        with r:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                if not loc:
                    raise WebError("redirect without a target")
                url = urljoin(url, loc)
                continue
            if r.status_code in (401, 403, 429, 503) or r.headers.get("cf-mitigated"):
                raise Blocked(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise WebError(f"HTTP {r.status_code}")
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not any(t in ctype for t in _TEXT_TYPES):
                raise WebError(f"not a web page ({ctype.split(';')[0]})")
            body = b""
            for chunk in r.iter_content(64 * 1024):
                body += chunk
                if len(body) > limit:
                    break
            text = body.decode(r.encoding or "utf-8", errors="replace")
            if "<title>Just a moment...</title>" in text or "cf-chl-" in text:
                raise Blocked("Cloudflare challenge")
            return Page(r.url or url, r.status_code, ctype, text, "direct")
    raise WebError("too many redirects")


_APIFY_PAGE_FUNCTION = r"""
async function pageFunction(context) {
    const { request, response, body, contentType } = context;
    const text = typeof body === 'string' ? body : (body ? body.toString() : '');
    return {
        key: request.userData.key,
        loadedUrl: request.loadedUrl || request.url,
        status: response ? (response.statusCode || response.status || 200) : 200,
        contentType: (contentType && contentType.type) || '',
        body: text.slice(0, __MAX__),
    };
}
"""


def fetch_via_apify(urls: list[str], wcfg: dict) -> dict[str, Page]:
    """Fetch several URLs in one Apify cheerio-scraper run (runs on Apify's servers)."""
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise WebError("APIFY_TOKEN not set")
    from apify_client import ApifyClient

    limit = int(float(wcfg.get("max_page_mb", 3)) * 1024 * 1024)
    client = ApifyClient(token)
    try:
        run = client.actor(wcfg.get("apify_actor") or "apify/cheerio-scraper").call(
            run_input={
                "startUrls": [{"url": u, "userData": {"key": u}} for u in urls],
                "pageFunction": _APIFY_PAGE_FUNCTION.replace("__MAX__", str(limit)),
                "maxRequestsPerCrawl": len(urls),
                "maxRequestRetries": 1,
                "maxCrawlingDepth": 0,
                "linkSelector": "",
                "maxConcurrency": 5,
                "additionalMimeTypes": ["application/json", "application/xml", "text/xml", "text/plain"],
                "ignoreSslErrors": False,
                "proxyConfiguration": {"useApifyProxy": True},
            },
            run_timeout=timedelta(seconds=int(wcfg.get("apify_timeout_s", 120))),
            logger=None,
        )
    except Exception as e:  # noqa: BLE001 - any Apify problem just means "not fetched"
        raise WebError(f"Apify: {str(e)[:160]}") from None
    if run is None or not getattr(run, "default_dataset_id", None):
        raise WebError("Apify: run did not finish")
    items = client.dataset(run.default_dataset_id).list_items(clean=True).items or []
    out: dict[str, Page] = {}
    for it in items:
        it = dict(it)
        key = it.get("key")
        status = int(it.get("status") or 200)
        if key and status < 400:
            out[key] = Page(str(it.get("loadedUrl") or key), status, str(it.get("contentType") or "").lower(),
                            str(it.get("body") or ""), "apify")
    return out


class Web:
    """Fetches pages with a per-item budget, a cache and the Apify fallback."""

    def __init__(self, cfg: dict, deadline: float | None = None):
        self.wcfg = cfg["web"]
        self.deadline = deadline
        self.started = time.monotonic()
        self.cache: dict[str, Page | Exception] = {}
        self.fetches = 0
        self.apify_runs = 0
        self.session = requests.Session()

    def time_left(self) -> float:
        left = float(self.wcfg.get("max_seconds", 120)) - (time.monotonic() - self.started)
        if self.deadline is not None:
            left = min(left, self.deadline - time.monotonic() - 60)
        return left

    def fetch_many(self, urls: list[str]) -> dict[str, Page | Exception]:
        out: dict[str, Page | Exception] = {}
        blocked: list[str] = []
        for u in dict.fromkeys(urls):
            if u in self.cache:
                out[u] = self.cache[u]
                continue
            if self.fetches >= int(self.wcfg.get("max_fetches", 15)):
                out[u] = WebError("page limit reached")
                continue
            if self.time_left() < 15:
                out[u] = WebError("no time left")
                continue
            self.fetches += 1
            try:
                out[u] = fetch_direct(u, self.wcfg, self.session)
            except Blocked as e:
                blocked.append(u)
                out[u] = e
            except WebError as e:
                out[u] = e
        if (blocked and self.wcfg.get("apify_fallback", True) and os.environ.get("APIFY_TOKEN")
                and self.apify_runs < int(self.wcfg.get("max_apify_runs", 3)) and self.time_left() > 40):
            self.apify_runs += 1
            try:
                pages = fetch_via_apify(blocked, self.wcfg)
                for u in blocked:
                    out[u] = pages.get(u) or WebError("not fetched (blocked)")
            except WebError as e:
                for u in blocked:
                    out[u] = e
        self.cache.update(out)
        return out

    def fetch(self, url: str) -> Page | Exception:
        return self.fetch_many([url])[url]


# --------------------------------------------------------------------------- page parsing

_LD_RE = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_NEXT_RE = re.compile(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.S | re.I)
_A_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(v) -> str:
    """Plain text from a JSON-LD or HTML string: entities decoded (twice), tags removed."""
    s = htmllib.unescape(htmllib.unescape(str(v or "")))
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", s)).strip()


def _ld_objects(html: str):
    """Every JSON-LD object on the page, including @graph members and mainEntity."""
    stack = []
    for block in _LD_RE.findall(html or ""):
        try:
            stack.append(json.loads(block.strip()))
        except ValueError:
            continue
    while stack:
        o = stack.pop()
        if isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, dict):
            yield o
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
                if isinstance(o.get(key), (list, dict)):
                    stack.append(o[key])


def _has_type(o: dict, name: str) -> bool:
    t = o.get("@type")
    return name in (t if isinstance(t, list) else [t])


def _steps(v) -> list[str]:
    if isinstance(v, str):
        return [clean_text(x) for x in re.split(r"\n+|<br\s*/?>|</p>", v) if clean_text(x)]
    out: list[str] = []
    if isinstance(v, dict):
        v = [v]
    for x in v if isinstance(v, list) else []:
        if isinstance(x, str):
            out += _steps(x)
        elif isinstance(x, dict):
            if _has_type(x, "HowToSection") or "itemListElement" in x:
                out += _steps(x.get("itemListElement") or [])
            else:
                t = clean_text(x.get("text") or x.get("name") or "")
                if t:
                    out.append(t)
    return out


_ISO_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?)?$", re.I)


def iso_minutes(v) -> int | None:
    m = _ISO_RE.match(str(v or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def fmt_minutes(m: int | None) -> str:
    if not m:
        return ""
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} h {mm} min" if mm else f"{h} h"


@dataclass
class WebRecipe:
    name: str
    ingredients: list[str]
    steps: list[str]
    minutes: int | None = None
    servings: str = ""


def ld_recipe(html: str) -> WebRecipe | None:
    """The first schema.org Recipe on the page that has both ingredients and steps."""
    for o in _ld_objects(html):
        if not _has_type(o, "Recipe"):
            continue
        ings = [clean_text(x) for x in (o.get("recipeIngredient") or o.get("ingredients") or [])
                if isinstance(x, str) and clean_text(x)]
        steps = _steps(o.get("recipeInstructions") or [])
        if not ings or not steps:
            continue
        minutes = iso_minutes(o.get("totalTime"))
        if minutes is None:
            parts = [iso_minutes(o.get(k)) for k in ("prepTime", "cookTime")]
            minutes = sum(p for p in parts if p) or None
        y = o.get("recipeYield")
        servings = clean_text(y[0] if isinstance(y, list) and y else y) if y else ""
        return WebRecipe(clean_text(o.get("name")), ings, steps, minutes, servings)
    return None


_PAYWALL_RE = re.compile(
    r"This post is for paid subscribers|Keep reading with a \d+-day free trial|"
    r"Subscribe to keep reading|Upgrade to paid to|available to paid subscribers|"
    r"for paying members only|members[- ]only content",
    re.I,
)


def paid_reason(page: Page) -> str:
    """Non-empty when the page is paid content: paywalled, or a recipe sold as a product."""
    for o in _ld_objects(page.text):
        if str(o.get("isAccessibleForFree", "")).lower() == "false":
            return "paywalled"
        if _has_type(o, "Product"):
            offers = o.get("offers")
            offers = offers if isinstance(offers, list) else [offers]
            for off in offers:
                try:
                    if isinstance(off, dict) and float(off.get("price") or off.get("lowPrice") or 0) > 0:
                        return "sold as a product"
                except (TypeError, ValueError):
                    pass
    if re.search(r"<meta[^>]+property=[\"']og:type[\"'][^>]+content=[\"']product", page.text, re.I):
        return "sold as a product"
    if _PAYWALL_RE.search(page.text):
        return "paywalled"
    return ""


def page_title(page: Page) -> str:
    m = (re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)", page.text, re.I)
         or re.search(r"<title[^>]*>(.*?)</title>", page.text, re.S | re.I)
         or re.search(r"<h1[^>]*>(.*?)</h1>", page.text, re.S | re.I))
    title = clean_text(m.group(1)) if m else ""
    # "Dish Name | Site" / "Dish Name - Site": keep the dish part.
    return re.split(r"\s+[|\u2013\u2014-]\s+", title)[0].strip() if title else ""


def readable_text(html: str, limit: int = 12000) -> str:
    h = re.sub(r"(?is)<(script|style|noscript|svg|template|iframe|form|nav|header|footer|aside)\b.*?</\1>",
               " ", html or "")
    m = re.search(r"(?is)<article\b.*?</article>", h) or re.search(r"(?is)<main\b.*?</main>", h)
    if m:
        h = m.group(0)
    h = re.sub(r"(?i)<br\s*/?>|</(?:p|li|h[1-6]|div|tr|section|ul|ol)>", "\n", h)
    h = htmllib.unescape(_TAG_RE.sub(" ", h))
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in h.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:limit]


_RECIPE_SIGNAL_RE = re.compile(r"(?i)\bingredients?\b")
_UNIT_RE = re.compile(r"(?i)\d\s*(?:g|kg|ml|l|tbsp|tsp|cups?|oz|lbs?|cloves?|tablespoons?|teaspoons?)\b")


def looks_like_recipe_text(text: str) -> bool:
    return bool(_RECIPE_SIGNAL_RE.search(text)) and len(_UNIT_RE.findall(text)) >= 3


def page_links(page: Page) -> list[tuple[str, str]]:
    """(text, absolute url) for every link on the page, Linktree's own link list included."""
    out: list[tuple[str, str]] = []
    m = _NEXT_RE.search(page.text)
    if m:
        try:
            data = json.loads(m.group(1))
            for ln in (data.get("props", {}).get("pageProps", {}).get("links") or []):
                if isinstance(ln, dict) and ln.get("url"):
                    out.append((clean_text(ln.get("title")), str(ln["url"])))
        except ValueError:
            pass
    for href, text in _A_RE.findall(page.text):
        href = htmllib.unescape(href.strip())
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append((clean_text(text), urljoin(page.url, href)))
    seen: set[str] = set()
    uniq = []
    for t, u in out:
        if u.startswith("http") and u not in seen:
            seen.add(u)
            uniq.append((t, u))
    return uniq


# --------------------------------------------------------------------------- matching the dish

_STOP = {
    "the", "a", "an", "and", "with", "my", "easy", "best", "recipe", "recipes", "homemade", "how",
    "to", "make", "of", "in", "for", "on", "style", "simple", "quick", "perfect", "ultimate", "you",
    "your", "this", "that", "day", "is", "it", "at", "from", "by", "or", "made", "minute", "minutes",
    "www", "com", "html", "php", "p",
}


def tokens(text: str) -> set[str]:
    """Normalized significant words: accents and plurals removed, doubled letters collapsed."""
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    out = set()
    for w in re.split(r"[^a-z0-9]+", s):
        if len(w) < 2 or w in _STOP or w.isdigit():
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(re.sub(r"(.)\1+", r"\1", w))
    return out


def name_score(a: str | set[str], b: str | set[str]) -> float:
    ta = a if isinstance(a, set) else tokens(a)
    tb = b if isinstance(b, set) else tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def ingredient_overlap(known: list[str], found: list[str]) -> float:
    """Share of the known ingredient lines that have a word in common with the found ones."""
    pool = set().union(*(tokens(x) for x in found)) if found else set()
    lines = [tokens(x) for x in known if tokens(x)]
    if not lines or not pool:
        return 0.0
    return sum(1 for t in lines if t & pool) / len(lines)


def _slug_text(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")


# --------------------------------------------------------------------------- the search


@dataclass
class Found:
    url: str
    title: str
    via: str  # "caption link" | "comment link" | "bio link"
    recipe: WebRecipe | None = None  # schema.org data, used word for word
    text: str = ""  # readable page text, for Gemini, when there is no schema.org data


@dataclass
class Lookup:
    """Inputs of one search plus a trace of what was tried (for logs)."""
    dish: list[str]  # names to match: Gemini's name and the caption's first line
    known_ingredients: list[str]
    caption_links: list[str]
    comment_links: list[str]
    creator_ids: list[str]  # username, full name: used to recognise the creator's own site
    bio_links: Callable[[], list[tuple[str, str]]]  # loaded only when needed (costs an Apify run)
    tried: list[str] = field(default_factory=list)


_BAD_PATH_RE = re.compile(
    r"(?i)/(?:products?|collections?|shop|store|cart|checkout|tag|tags|category|categories|author|"
    r"account|login|wp-admin|feed)(?:/|$)|\.(?:jpe?g|png|gif|webp|pdf|mp4|zip)$"
)
_LINK_PAGE_RE = re.compile(r"(?i)/(?:links?|link-in-bio|linkinbio|bio)/?$")


class Finder:
    def __init__(self, web: Web, cfg: dict):
        self.web = web
        self.wcfg = cfg["web"]
        self.skip = list(self.wcfg.get("skip_hosts") or [])
        self.hubs = list(self.wcfg.get("link_pages") or [])
        self.min_score = float(self.wcfg.get("min_name_score", 0.7))

    # ---- helpers

    def usable(self, url: str) -> bool:
        h = host_of(url)
        return bool(h) and not _host_in(h, self.skip) and not _BAD_PATH_RE.search(urlparse(url).path)

    def is_hub(self, url: str) -> bool:
        return _host_in(host_of(url), self.hubs) or bool(_LINK_PAGE_RE.search(urlparse(url).path))

    def score(self, lk: Lookup, *texts: str) -> float:
        return max((name_score(d, t) for d in lk.dish for t in texts if t), default=0.0)

    def own_site(self, lk: Lookup, url: str) -> bool:
        """True if the host looks like the creator's own (contains their username or name)."""
        label = re.sub(r"[^a-z0-9]", "", host_of(url))
        ids = [re.sub(r"[^a-z0-9]", "", i.lower()) for i in lk.creator_ids]
        return any(len(i) >= 4 and i in label for i in ids)

    # ---- site search

    def _queries(self, lk: Lookup) -> list[str]:
        words = [w for w in re.split(r"[^\w]+", (lk.dish[0] if lk.dish else "").lower())
                 if len(w) > 2 and w not in _STOP]
        out = []
        if words:
            out.append(" ".join(words[:5]))
            longest = sorted(words, key=len, reverse=True)[:1]
            if longest and longest[0] != out[0]:
                out.append(longest[0])
        return out

    def search_site(self, lk: Lookup, root: str) -> list[tuple[float, str]]:
        """Candidate recipe URLs on a creator's site: WordPress search first, then the sitemap."""
        found: list[tuple[float, str]] = []
        wp_ok = False
        for q in self._queries(lk):
            api = f"{root}/wp-json/wp/v2/posts?search={quote_plus(q)}&per_page=8&_fields=link,title"
            lk.tried.append(api)
            page = self.web.fetch(api)
            if isinstance(page, Exception):
                break
            try:
                rows = json.loads(page.text)
            except ValueError:
                break
            if not isinstance(rows, list):
                break
            wp_ok = True
            for row in rows:
                if isinstance(row, dict) and row.get("link"):
                    title = clean_text((row.get("title") or {}).get("rendered", ""))
                    found.append((self.score(lk, title, _slug_text(row["link"])), str(row["link"])))
            if found:
                return found
        if wp_ok:
            return found
        return self._from_sitemap(lk, root)

    def _from_sitemap(self, lk: Lookup, root: str) -> list[tuple[float, str]]:
        for name in ("sitemap_index.xml", "sitemap.xml", "wp-sitemap.xml"):
            url = f"{root}/{name}"
            lk.tried.append(url)
            page = self.web.fetch(url)
            if isinstance(page, Exception) or "<loc>" not in page.text:
                continue
            locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", page.text, re.S)
            if "<sitemapindex" in page.text:
                children = [u for u in locs if re.search(r"(?i)post|recipe|blog|page", u)
                            and not re.search(r"(?i)product|collection|categor|tag|author|image|video", u)]
                locs = []
                for child in children[:2]:
                    lk.tried.append(child)
                    sub = self.web.fetch(child)
                    if not isinstance(sub, Exception):
                        locs += re.findall(r"<loc>\s*(.*?)\s*</loc>", sub.text, re.S)
            scored = [(self.score(lk, _slug_text(u)), htmllib.unescape(u)) for u in locs]
            return [x for x in scored if x[0] > 0]
        return []

    # ---- evaluation

    def evaluate(self, lk: Lookup, page: Page, via: str) -> Found | None:
        paid = paid_reason(page)
        if paid:
            _log(status="web-skip", url=page.url[:200], reason=paid)
            return None
        title = page_title(page)
        rec = ld_recipe(page.text)
        if rec:
            s = self.score(lk, rec.name, title)
            ok = s >= self.min_score or (
                s >= 0.4 and lk.known_ingredients
                and ingredient_overlap(lk.known_ingredients, rec.ingredients) >= 0.6)
            if ok:
                return Found(page.url, rec.name or title, via, recipe=rec)
            _log(status="web-skip", url=page.url[:200], reason=f"other dish ({rec.name[:60]!r}, score {s:.2f})")
            return None
        text = readable_text(page.text, int(self.wcfg.get("max_text_chars", 12000)))
        if self.score(lk, title) >= self.min_score and looks_like_recipe_text(text):
            return Found(page.url, title, via, text=text)
        return None

    # ---- main

    def find(self, lk: Lookup) -> Found | None:
        # (score, url, via): links the creator gave for this post come first.
        cands: list[tuple[float, str, str]] = []
        hubs: list[tuple[str, str]] = []
        for via, links in (("caption link", lk.caption_links), ("comment link", lk.comment_links)):
            for u in links:
                if not self.usable(u):
                    continue
                (hubs.append((u, via)) if self.is_hub(u) else cands.append((2.0, u, via)))
        found = self._try(lk, cands)
        if found:
            return found

        if not hubs and self.wcfg.get("follow_bio_links", True):
            for _, u in lk.bio_links():
                if self.usable(u) or self.is_hub(u):
                    hubs.append((u, "bio link"))
        roots: list[tuple[str, str]] = []
        cands = []
        for u, via in hubs[: int(self.wcfg.get("max_bio_links", 3))]:
            if not self.is_hub(u):
                # The creator's site itself: the page may be the recipe, and the site is searchable.
                cands.append((self.score(lk, _slug_text(u)) + 0.5, u, via))
                roots.append((f"https://{urlparse(u).hostname}", via))
                continue
            lk.tried.append(u)
            page = self.web.fetch(u)
            if isinstance(page, Exception):
                continue
            if not _host_in(host_of(page.url), self.hubs):
                roots.append((f"https://{urlparse(page.url).hostname}", via))
            for text, link in page_links(page):
                if not self.usable(link):
                    continue
                s = self.score(lk, text, _slug_text(link))
                if s > 0.3:
                    cands.append((s, link, via))
                if self.own_site(lk, link):
                    roots.append((f"https://{urlparse(link).hostname}", via))
        found = self._try(lk, cands)
        if found:
            return found

        cands = []
        for root, via in list(dict.fromkeys(roots))[:2]:
            if not self.usable(root + "/"):
                continue
            cands += [(s, u, via) for s, u in self.search_site(lk, root) if self.usable(u)]
        return self._try(lk, cands)

    def _try(self, lk: Lookup, cands: list[tuple[float, str, str]]) -> Found | None:
        best: dict[str, tuple[float, str]] = {}
        for s, u, via in cands:
            if u not in best or s > best[u][0]:
                best[u] = (s, via)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[: int(self.wcfg.get("max_pages", 6))]
        if not ranked:
            return None
        pages = self.web.fetch_many([u for u, _ in ranked])
        for u, (_, via) in ranked:
            lk.tried.append(u)
            page = pages.get(u)
            if isinstance(page, Page):
                found = self.evaluate(lk, page, via)
                if found:
                    return found
        return None
