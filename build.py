#!/usr/bin/env python3
"""Fetch every feed in feeds.opml and write a single text-only HTML page.

State lives in cache.json (items seen so far, plus ETag / Last-Modified per
feed so unchanged feeds cost one cheap request). Output goes to docs/, which
GitHub Pages serves.
"""
import calendar
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OPML = os.path.join(HERE, "feeds.opml")
CACHE = os.path.join(HERE, "cache.json")
OUT_DIR = os.path.join(HERE, "docs")
TEMPLATE = os.path.join(HERE, "template.html")

KEEP_DAYS = 45          # drop items older than this from the page and cache
MIN_PER_FEED = 5        # but always keep at least this many per feed (journals post rarely)
MAX_PER_FEED = 60       # cap chatty feeds
TIMEOUT = 20
WORKERS = 24
UA = "simple-rss-reader/1.0 (+https://github.com/; personal, low-volume)"
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid"}


# ---------- feeds.opml ----------

def load_feeds():
    raw = open(OPML, encoding="utf-8").read()
    # Exported OPML often has bare '&' in URLs; make it valid XML.
    raw = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", raw)
    root = ET.fromstring(raw)
    feeds, seen = [], set()
    for o in root.iter("outline"):
        url = (o.get("xmlUrl") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        tags = [t.strip() for t in (o.get("category") or "Uncategorized").split(",") if t.strip()]
        feeds.append({
            "id": hashlib.sha1(url.encode()).hexdigest()[:10],
            "title": (o.get("title") or o.get("text") or url).strip(),
            "url": url,
            "site": (o.get("htmlUrl") or "").strip(),
            "tags": tags,
        })
    return feeds


# ---------- fetching ----------

def clean_link(link):
    if not link:
        return ""
    p = urlsplit(link.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q), ""))


def entry_time(e):
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(k)
        if t:
            try:
                return calendar.timegm(t)
            except Exception:
                pass
    return None


def strip_html(s, limit=2000):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return s


def fetch_one(feed, state, now):
    """Return (feed_id, status, new_items, headers). status is 'ok', 'unchanged' or an error string."""
    headers = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("modified"):
        headers["If-Modified-Since"] = state["modified"]
    try:
        r = requests.get(feed["url"], headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as ex:
        return feed["id"], f"error: {type(ex).__name__}: {str(ex)[:120]}", [], {}
    if r.status_code == 304:
        return feed["id"], "unchanged", [], {}
    if r.status_code != 200:
        return feed["id"], f"http {r.status_code}", [], {}
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        return feed["id"], f"unparseable: {str(parsed.bozo_exception)[:120]}", [], {}
    items = []
    for e in parsed.entries:
        link = clean_link(e.get("link") or "")
        title = strip_html(e.get("title") or "", 300) or "(untitled)"
        guid = e.get("id") or link or title
        ts = entry_time(e)
        if ts is None or ts > now + 86400:  # missing or bogus future date: first-seen time
            ts = state.get("seen", {}).get(guid, now)
        summary = ""
        if e.get("summary"):
            summary = e["summary"]
        elif e.get("content"):
            summary = e["content"][0].get("value", "")
        author = e.get("author") or ""
        if not author and e.get("authors"):
            author = ", ".join(a.get("name", "") for a in e["authors"] if a.get("name"))
        items.append({
            "guid": guid,
            "feed": feed["id"],
            "title": title,
            "link": link,
            "ts": int(ts),
            "author": strip_html(author, 200),
            "summary": strip_html(summary),
        })
    new_state = {"etag": r.headers.get("ETag"), "modified": r.headers.get("Last-Modified")}
    return feed["id"], "ok", items, new_state


# ---------- main ----------

def main():
    now = int(time.time())
    feeds = load_feeds()
    cache = {"feeds": {}, "items": {}}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE, encoding="utf-8"))
    cache.setdefault("feeds", {})
    cache.setdefault("items", {})

    results = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(fetch_one, f, cache["feeds"].get(f["id"], {}), now): f for f in feeds}
        for fut in cf.as_completed(futs):
            fid, status, items, hdrs = fut.result()
            results[fid] = status
            st = cache["feeds"].setdefault(fid, {})
            st.setdefault("seen", {})
            if status in ("ok", "unchanged"):
                st["last_ok"] = now
                st["fail_count"] = 0
            else:
                st["fail_count"] = st.get("fail_count", 0) + 1
                st["last_error"] = status
            if status == "ok":
                st.update(hdrs)
                for it in items:
                    key = fid + ":" + hashlib.sha1(it["guid"].encode()).hexdigest()[:16]
                    if key not in cache["items"]:
                        st["seen"][it["guid"]] = it["ts"]
                        cache["items"][key] = it

    # prune: per-feed keep window, then dedupe by link across feeds
    by_feed = {}
    for key, it in cache["items"].items():
        by_feed.setdefault(it["feed"], []).append((key, it))
    cutoff = now - KEEP_DAYS * 86400
    keep = {}
    for fid, lst in by_feed.items():
        lst.sort(key=lambda kv: kv[1]["ts"], reverse=True)
        for i, (key, it) in enumerate(lst):
            if i < MIN_PER_FEED or (it["ts"] >= cutoff and i < MAX_PER_FEED):
                keep[key] = it
    cache["items"] = keep
    for fid, st in cache["feeds"].items():
        kept_guids = {it["guid"] for it in keep.values() if it["feed"] == fid}
        st["seen"] = {g: t for g, t in st.get("seen", {}).items() if g in kept_guids}

    feed_ids = {f["id"] for f in feeds}
    seen_links = set()
    page_items = []
    for it in sorted(keep.values(), key=lambda x: x["ts"], reverse=True):
        if it["feed"] not in feed_ids:
            continue
        if it["link"]:
            if it["link"] in seen_links:
                continue
            seen_links.add(it["link"])
        page_items.append(it)

    # write outputs
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    failures = []
    for f in feeds:
        st = cache["feeds"].get(f["id"], {})
        status = results.get(f["id"], "not fetched")
        if status not in ("ok", "unchanged"):
            failures.append((st.get("fail_count", 0), f["title"], f["url"], status))
    failures.sort(reverse=True)
    with open(os.path.join(OUT_DIR, "failures.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"Run: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n")
        fh.write(f"Feeds: {len(feeds)}  OK: {len(feeds) - len(failures)}  Failed: {len(failures)}\n\n")
        for n, title, url, status in failures:
            fh.write(f"[{n} consecutive] {title}\n  {url}\n  {status}\n\n")

    data = {
        "generated": now,
        "feeds": [{"id": f["id"], "title": f["title"], "tags": f["tags"], "site": f["site"]} for f in feeds],
        "items": [{"f": it["feed"], "t": it["title"], "l": it["link"], "d": it["ts"], "s": it["summary"], "a": it.get("author", ""),
                   "k": hashlib.sha1((it["feed"] + ":" + it["guid"]).encode()).hexdigest()[:12]}
                  for it in page_items],
    }
    tpl = open(TEMPLATE, encoding="utf-8").read()
    page = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8").write(page)
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()

    ok = sum(1 for s in results.values() if s in ("ok", "unchanged"))
    print(f"{ok}/{len(feeds)} feeds ok, {len(page_items)} items on page, {len(failures)} failures (see docs/failures.txt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
