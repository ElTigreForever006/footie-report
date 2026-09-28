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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
UA = "Mozilla/5.0 (compatible; FootieReportBot/1.0; +https://github.com/)"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
}
MIN_ITEMS = 15  # below this we fail the build so the last good deploy stays live
ESPN_SCORE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={date}"
ESPN_STAND = "https://site.api.espn.com/apis/v2/sports/soccer/{code}/standings"


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
        if feed.get("strip_source"):  # Google News appends " - Publisher" to every title
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
        items.append({
            "title": title,
            "url": link,
            "date": parse_date(date),
            "image": find_image(e),
            "source": feed["name"],
            "section": feed["section"],
            "priority": feed.get("priority", 1),
            "pure": feed.get("pure", False),  # football-only feed: skip the football-word check
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


# ---------------------------------------------------------------- fixtures & tables (ESPN public endpoints)
def fetch_json(url, timeout=12):
    return json.loads(fetch(url, timeout))


def tz_of(cfg):
    try:
        return ZoneInfo(cfg.get("timezone", "America/New_York"))
    except Exception:
        return timezone.utc


def channel_label(name, cfg):
    return (cfg.get("channel_names") or {}).get(name, name)


def get_fixtures(cfg):
    """Matches over the next few days, with US broadcasters and local kickoff times."""
    tz = tz_of(cfg)
    today = datetime.now(tz)
    days = [today + timedelta(days=i) for i in range(max(1, cfg.get("tv_days", 3)))]
    wanted = {d.date() for d in days}
    seen, out = set(), []
    for lg in cfg.get("leagues", []):
        if not lg.get("tv", True):
            continue
        for d in days:
            url = ESPN_SCORE.format(code=lg["code"], date=d.strftime("%Y%m%d"))
            try:
                data = fetch_json(url)
            except Exception as ex:
                print(f"  TV   FAIL {lg['name']} {d:%Y-%m-%d} ({type(ex).__name__})")
                continue
            for ev in data.get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                kick = parse_date(ev.get("date"))
                if not kick:
                    continue
                local = kick.astimezone(tz)
                if local.date() not in wanted:
                    continue
                sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
                home, away = sides.get("home") or {}, sides.get("away") or {}
                hname = (home.get("team") or {}).get("shortDisplayName", "")
                aname = (away.get("team") or {}).get("shortDisplayName", "")
                if not hname or not aname:
                    continue
                key = (local.isoformat(), hname, aname)
                if key in seen:
                    continue
                seen.add(key)
                chans, tvs = [], []
                for g in comp.get("geoBroadcasts", []):
                    n = (g.get("media") or {}).get("shortName")
                    if n:
                        chans.append(n)
                if not chans:
                    for b in comp.get("broadcasts", []):
                        chans.extend(b.get("names", []))
                for n in chans:
                    lab = channel_label(n, cfg)
                    if lab not in tvs:
                        tvs.append(lab)
                state = ((ev.get("status") or {}).get("type") or {})
                out.append({
                    "kick": local,
                    "league": lg["name"],
                    "home": hname,
                    "away": aname,
                    "tv": ", ".join(tvs),
                    "state": state.get("state", "pre"),
                    "detail": state.get("shortDetail", ""),
                    "score": f'{away.get("score", "")}-{home.get("score", "")}'
                             if state.get("state") in ("in", "post") else "",
                })
    out.sort(key=lambda x: x["kick"])
    print(f"Fixtures: {len(out)} across {cfg.get('tv_days', 3)} days.")
    return out


def get_tables(cfg):
    """Current standings per league."""
    tables = []
    for lg in cfg.get("leagues", []):
        if not lg.get("table"):
            continue
        try:
            data = fetch_json(ESPN_STAND.format(code=lg["code"]))
        except Exception as ex:
            print(f"  TBL  FAIL {lg['name']} ({type(ex).__name__})")
            continue
        entries = None
        for child in data.get("children") or []:
            entries = ((child.get("standings") or {}).get("entries")) or entries
            if entries:
                break
        if not entries:
            entries = (data.get("standings") or {}).get("entries")
        if not entries:
            continue
        rows = []
        for en in entries:
            stats = {s.get("name"): s for s in en.get("stats", [])}
            def val(name):
                s = stats.get(name) or {}
                return s.get("displayValue") or ("" if s.get("value") is None else str(s.get("value")))
            rows.append({
                "team": (en.get("team") or {}).get("shortDisplayName", ""),
                "gp": val("gamesPlayed"), "w": val("wins"), "d": val("ties"), "l": val("losses"),
                "gd": val("pointDifferential"), "pts": val("points"),
            })
        if rows:
            tables.append({"name": lg["name"], "rows": rows})
    print(f"Tables: {len(tables)} leagues.")
    return tables


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
    reject = [r.lower() for r in cfg.get("reject_terms", [])]
    reject_url = {r.lower() for r in cfg.get("reject_url_terms", [])}
    football = [f.lower() for f in cfg.get("football_terms", [])]
    dropped = 0

    def wrong_sport_url(url):
        # publishers file stories by sport: skysports.com/snooker/..., bbc.co.uk/sport/golf/...
        path = url.split("//", 1)[-1].split("?", 1)[0]
        return any(seg.lower() in reject_url for seg in path.split("/") if seg)

    def has(term, text):
        return re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text) is not None

    fresh = []
    for it in items:
        if it["date"] and it["date"] < cutoff:
            continue
        low = it["title"].lower()
        if any(b in low for b in blocked):
            continue
        # other sports never belong here, and anything off a mixed-sport feed must prove
        # it is about football before it gets in
        if wrong_sport_url(it["url"]) or any(has(r, low) for r in reject) or (
            not it.get("pure") and football and not any(has(f, low) for f in football)
        ):
            dropped += 1
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

    if dropped:
        print(f"Filtered out {dropped} non-football headlines.")
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


def render_tv(fixtures, cfg):
    if not fixtures:
        return ""
    tz = tz_of(cfg)
    today = datetime.now(tz).date()
    blocks = []
    for day in sorted({f["kick"].date() for f in fixtures}):
        label = "TODAY" if day == today else ("TOMORROW" if day == today + timedelta(days=1)
                                              else day.strftime("%A %b %-d").upper())
        rows = []
        for f in (x for x in fixtures if x["kick"].date() == day):
            when = f["detail"] if f["state"] == "in" else (
                "FT" if f["state"] == "post" else f["kick"].strftime("%-I:%M %p"))
            score = f' <b>{e(f["score"])}</b>' if f["score"] else ""
            rows.append(
                f'<tr><td class="t">{e(when)}</td>'
                f'<td class="m">{e(f["away"])} at {e(f["home"])}{score}</td>'
                f'<td class="c">{e(f["tv"]) or "&mdash;"}</td></tr>')
        blocks.append(f'<h3>{e(label)}</h3><table>{"".join(rows)}</table>')
    zone = "ET" if cfg.get("timezone", "America/New_York") == "America/New_York" else ""
    return (f'<section class="tv"><h2>ON TV {e(zone)}</h2>{"".join(blocks)}'
            f'<p class="note">US listings. Times {e(zone) or "local"}.</p></section>')


def render_tables(tables, cfg, limit=None):
    if not tables:
        return ""
    out = []
    for t in tables:
        rows = t["rows"] if limit is None else t["rows"][:limit]
        body = "".join(
            f'<tr><td class="p">{i}</td><td class="n">{e(r["team"])}</td>'
            f'<td>{e(r["gp"])}</td><td>{e(r["gd"])}</td><td class="pts">{e(r["pts"])}</td></tr>'
            for i, r in enumerate(rows, 1))
        out.append(f'<div class="table"><h3>{e(t["name"])}</h3>'
                   f'<table><tr class="hd"><td></td><td></td><td>P</td><td>GD</td><td>PTS</td></tr>{body}</table></div>')
    more = '<p class="note"><a href="tables.html">Full tables &rarr;</a></p>' if limit else ""
    return f'<section class="tables"><h2>LEAGUE TABLES</h2><div class="tablegrid">{"".join(out)}</div>{more}</section>'


def tables_page(cfg, tables):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>League tables &middot; {e(cfg["site_name"])}</title>
<meta name="description" content="Current standings for the top European leagues and MLS.">
<link rel="stylesheet" href="style.css">
</head>
<body>
<p><a href="/">&larr; Back to headlines</a></p>
{render_tables(tables, cfg)}
<footer>Standings via ESPN. {e(cfg["site_name"])}</footer>
</body>
</html>
"""


def render(cfg, lead, top, sections, fixtures=None, tables=None):
    now = datetime.now(timezone.utc)
    head_scripts = ""
    if cfg.get("adsense_client"):
        head_scripts += (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={e(cfg["adsense_client"])}" '
                         f'crossorigin="anonymous"></script>\n')
    if cfg.get("goatcounter_code"):
        head_scripts += (f'<script data-goatcounter="https://{e(cfg["goatcounter_code"])}.goatcounter.com/count" '
                         f'async src="//gc.zgo.at/count.js"></script>\n')
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
    counter_html = ""
    if cfg.get("goatcounter_code"):
        # fetch the number and render it in the site's own type; hide the line if the service is unreachable
        api = f'https://{e(cfg["goatcounter_code"])}.goatcounter.com/counter/TOTAL.json'
        counter_html = (
            '<div class="counter" id="counter" hidden>VISITS <span id="visits"></span></div>\n'
            '<script>fetch(' + repr(api).replace("'", '"') + ')'
            '.then(function(r){return r.json()})'
            '.then(function(d){var n=d.count||d.count_unique;if(!n)return;'
            'document.getElementById("visits").textContent=n;'
            'document.getElementById("counter").hidden=false;})'
            '.catch(function(){});</script>')

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
{render_tv(fixtures or [], cfg)}
<main class="cols">
  <div class="col">{column(cols["left"])}</div>
  <div class="col">{column(cols["center"])}</div>
  <div class="col">{column(cols["right"])}</div>
</main>
<hr>
{render_tables(tables or [], cfg, limit=cfg.get("table_rows_front", 6))}
{ad_slot(cfg, "bottom")}
<footer>
  {counter_html}
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
    # fixtures/tables are extras: never let them break the headline build
    games, tables = [], []
    if not args.fixtures:
        try:
            games = get_fixtures(cfg)
        except Exception as ex:
            print(f"Fixtures unavailable ({type(ex).__name__}: {ex})")
        try:
            tables = get_tables(cfg)
        except Exception as ex:
            print(f"Tables unavailable ({type(ex).__name__}: {ex})")
    total = sum(len(v) for v in sections.values()) + len(top) + (1 if lead else 0)
    print(f"Curated {total} headlines from {len(items)} raw items.")
    if total < MIN_ITEMS:
        print(f"Too few headlines (<{MIN_ITEMS}); refusing to overwrite the live site.")
        sys.exit(1)

    DIST.mkdir(exist_ok=True)
    (DIST / "index.html").write_text(render(cfg, lead, top, sections, games, tables))
    if tables:
        (DIST / "tables.html").write_text(tables_page(cfg, tables))
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
