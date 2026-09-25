#!/usr/bin/env python3
"""Build a Drudge-style football headline page from RSS/Atom feeds.

Standard library only. Usage:
    python build.py                      # fetch live feeds, write dist/
    python build.py --fixtures tests/    # use local XML files instead (offline test)
"""
import argparse
import concurrent.futures as cf
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
UA = "Mozilla/5.0 (compatible; FootieReportBot/1.0; +https://github.com/)"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
}
MIN_ITEMS = 15  # below this we fail the build so the last good deploy stays live


# ---------------------------------------------------------------- fetching
def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def find_image(el):
    for tag in ("media:content", "media:thumbnail"):
        for m in el.findall(tag, NS):
            url = m.get("url")
            if url and (m.get("medium") in (None, "image") or tag == "media:thumbnail"):
                return url
    enc = el.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image"):
        return enc.get("url")
    return None


def parse_feed(raw, feed):
    root = ET.fromstring(raw)
    items = []
    entries = root.findall(".//item")
    atom = False
    if not entries:
        entries = root.findall(".//atom:entry", NS)
        atom = True
    for e in entries:
        if atom:
            title = e.findtext("atom:title", "", NS)
            link_el = e.find("atom:link[@rel='alternate']", NS)
            if link_el is None:
                link_el = e.find("atom:link", NS)
            link = link_el.get("href") if link_el is not None else ""
            date = e.findtext("atom:updated", None, NS) or e.findtext("atom:published", None, NS)
        else:
            title = e.findtext("title", "")
            link = e.findtext("link", "") or e.findtext("guid", "")
            date = e.findtext("pubDate") or e.findtext("{http://purl.org/dc/elements/1.1/}date")
        title, link = clean(title), (link or "").strip()
        if not title or not link.startswith("http"):
            continue
        items.append({
            "title": title,
            "url": link,
            "date": parse_date(date),
            "image": find_image(e),
            "source": feed["name"],
            "section": feed["section"],
            "priority": feed.get("priority", 1),
        })
    return items


def load_all(cfg, fixtures=None):
    def one(i_feed):
        i, feed = i_feed
        try:
            raw = (Path(fixtures) / f"{i}.xml").read_bytes() if fixtures else fetch(feed["url"])
            got = parse_feed(raw, feed)
            print(f"  ok   {len(got):3d}  {feed['name']:<16} {feed['url']}")
            return got
        except Exception as ex:  # one bad feed must never break the site
            print(f"  FAIL      {feed['name']:<16} {feed['url']}  ({type(ex).__name__}: {ex})")
            return []

    out = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for got in pool.map(one, enumerate(cfg["feeds"])):
            out.extend(got)
    return out


# ---------------------------------------------------------------- editorial logic
def words(title):
    return set(w for w in re.findall(r"[a-z0-9']+", title.lower()) if len(w) > 2)


def is_dupe(a, b):
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.6


def curate(items, cfg, pinned):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("max_age_hours", 48))
    blocked = [b.lower() for b in pinned.get("block", []) if b]
    hot_words = [h.lower() for h in cfg.get("hot_words", [])]

    fresh = []
    for it in items:
        if it["date"] and it["date"] < cutoff:
            continue
        low = it["title"].lower()
        if any(b in low for b in blocked):
            continue
        # keyword routing: a Real Madrid story from a general feed goes to Spain, etc.
        for section, kws in cfg.get("keyword_sections", {}).items():
            if any(re.search(r"\b" + re.escape(k) + r"\b", low) for k in kws):
                it["section"] = section
                break
        it["hot"] = any(re.search(r"\b" + re.escape(h) + r"\b", low) for h in hot_words)
        age_h = ((now - it["date"]).total_seconds() / 3600) if it["date"] else 12
        it["score"] = it["priority"] * 2 + (4 if it["hot"] else 0) + max(0, 12 - age_h) / 2
        fresh.append(it)

    # dedupe: best-scoring version of each story wins
    fresh.sort(key=lambda x: -x["score"])
    kept, seen_urls = [], set()
    for it in fresh:
        if it["url"] in seen_urls or any(is_dupe(it["title"], k["title"]) for k in kept):
            continue
        seen_urls.add(it["url"])
        kept.append(it)

    # lead story
    lead_pin = pinned.get("lead") or {}
    if lead_pin.get("title") and lead_pin.get("url"):
        lead = {"title": lead_pin["title"], "url": lead_pin["url"], "image": lead_pin.get("image") or None, "source": "", "hot": True}
    else:
        with_img = [k for k in kept[:10] if k["image"]]
        lead = with_img[0] if with_img else (kept[0] if kept else None)
    if lead in kept:
        kept.remove(lead)

    n_top = cfg.get("top_stories_count", 4)
    top = [{"title": t["title"], "url": t["url"], "hot": t.get("hot", False), "source": ""}
           for t in pinned.get("top", []) if t.get("title") and t.get("url")]
    for k in kept:
        if len(top) >= n_top:
            break
        if k["hot"] or k["priority"] >= 2:
            top.append(k)
    kept = [k for k in kept if k not in top]

    # sections: newest first inside each
    per = cfg.get("items_per_column", 22)
    sections = {}
    for k in sorted(kept, key=lambda x: x["date"] or cutoff, reverse=True):
        sections.setdefault(k["section"], []).append(k)
    for s in sections:
        sections[s] = sections[s][:per]
    return lead, top, sections


# ---------------------------------------------------------------- rendering
def e(s):
    return html.escape(s or "", quote=True)


def link(it, big=False):
    cls = "hot" if it.get("hot") else ""
    src = f' <span class="src">{e(it["source"])}</span>' if it.get("source") and not big else ""
    return f'<a class="{cls}" href="{e(it["url"])}" target="_blank" rel="noopener">{e(it["title"])}</a>{src}'


def ad_slot(cfg, name):
    client, slot = cfg.get("adsense_client"), (cfg.get("adsense_slots") or {}).get(name)
    if not (client and slot):
        return f'<div class="ad ad-placeholder" aria-hidden="true">ADVERTISEMENT</div>'
    return (f'<div class="ad"><ins class="adsbygoogle" style="display:block" data-ad-client="{e(client)}" '
            f'data-ad-slot="{e(slot)}" data-ad-format="auto" data-full-width-responsive="true"></ins>'
            f'<script>(adsbygoogle=window.adsbygoogle||[]).push({{}});</script></div>')


def render(cfg, lead, top, sections):
    now = datetime.now(timezone.utc)
    head_scripts = ""
    if cfg.get("adsense_client"):
        head_scripts += (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={e(cfg["adsense_client"])}" '
                         f'crossorigin="anonymous"></script>\n')
    if cfg.get("google_analytics_id"):
        ga = e(cfg["google_analytics_id"])
        head_scripts += (f'<script async src="https://www.googletagmanager.com/gtag/js?id={ga}"></script>'
                         f"<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments)}}gtag('js',new Date());gtag('config','{ga}');</script>\n")

    def column(names):
        parts = []
        for i, name in enumerate(names):
            its = sections.get(name, [])
            if not its:
                continue
            lis = "\n".join(f"<li>{link(it)}</li>" for it in its)
            parts.append(f'<section><h2>{e(name)}</h2><ul>{lis}</ul></section>')
            if i == 0:
                parts.append(ad_slot(cfg, "middle"))
        return "\n".join(parts)

    cols = cfg["columns"]
    lead_html = ""
    if lead:
        img = f'<a href="{e(lead["url"])}" target="_blank" rel="noopener"><img src="{e(lead["image"])}" alt="" loading="eager"></a>' if lead.get("image") else ""
        lead_html = f'<div class="lead">{img}<h1>{link(lead, big=True)}</h1></div>'
    top_html = "\n".join(f"<li>{link(t)}</li>" for t in top)
    updated = now.strftime("%a %b %d %Y %H:%M UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>{e(cfg["site_name"])} – {e(cfg["tagline"])}</title>
<meta name="description" content="{e(cfg["tagline"])}">
<link rel="canonical" href="https://{e(cfg["domain"])}/">
<meta property="og:title" content="{e(cfg["site_name"])}">
<meta property="og:description" content="{e(lead["title"] if lead else cfg["tagline"])}">
{f'<meta property="og:image" content="{e(lead["image"])}">' if lead and lead.get("image") else ""}
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚽</text></svg>">
<link rel="stylesheet" href="style.css">
{head_scripts}</head>
<body>
<header>
  {ad_slot(cfg, "top")}
  <ul class="top">{top_html}</ul>
  {lead_html}
  <div class="masthead">{e(cfg["site_name"])}</div>
  <div class="tagline">{e(cfg["tagline"])} &middot; <span id="upd">Updated {updated}</span></div>
</header>
<hr>
<main class="cols">
  <div class="col">{column(cols["left"])}</div>
  <div class="col">{column(cols["center"])}</div>
  <div class="col">{column(cols["right"])}</div>
</main>
<hr>
{ad_slot(cfg, "bottom")}
<footer>
  Headlines link to their original publishers; all stories &copy; their respective owners.
  <br>{e(cfg["site_name"])} &middot; <a href="about.html">About / Contact / Privacy</a>
</footer>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", help="directory of N.xml files matching feed order (offline test)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text())
    pinned = json.loads((ROOT / "pinned.json").read_text())

    print("Fetching feeds…")
    items = load_all(cfg, args.fixtures)
    lead, top, sections = curate(items, cfg, pinned)
    total = sum(len(v) for v in sections.values()) + len(top) + (1 if lead else 0)
    print(f"Curated {total} headlines from {len(items)} raw items.")
    if total < MIN_ITEMS:
        print(f"Too few headlines (<{MIN_ITEMS}); refusing to overwrite the live site.")
        sys.exit(1)

    DIST.mkdir(exist_ok=True)
    (DIST / "index.html").write_text(render(cfg, lead, top, sections))
    for f in (ROOT / "static").iterdir():
        (DIST / f.name).write_bytes(f.read_bytes())
    if cfg.get("domain") and "example.com" not in cfg["domain"]:
        (DIST / "CNAME").write_text(cfg["domain"] + "\n")
    if cfg.get("adsense_client"):
        pub = cfg["adsense_client"].replace("ca-", "")
        (DIST / "ads.txt").write_text(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\n")
    print("Wrote dist/index.html")


if __name__ == "__main__":
    main()
