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
import unicodedata
import urllib.parse
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
        # Google News wraps every link in a news.google.com redirect and names itself as the
        # feed; the real publisher is in <source url="...">Name</source>. Credit them instead.
        source = feed["name"]
        if feed.get("strip_source"):
            src_el = e.find("source")
            if src_el is not None and (src_el.text or "").strip():
                source = clean(src_el.text)
        items.append({
            "title": title,
            "url": link,
            "date": parse_date(date),
            "image": find_image(e),
            "source": source,
            "section": feed["section"],
            "priority": feed.get("priority", 1),
            "pure": feed.get("pure", False),  # football-only feed: skip the football-word check
            "womens": feed.get("womens", False),  # women's-only feed: section is not negotiable
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
    jobs = [(lg, d) for lg in cfg.get("leagues", []) if lg.get("tv", True) for d in days]

    def grab(job):
        lg, d = job
        try:
            return lg, fetch_json(ESPN_SCORE.format(code=lg["code"], date=d.strftime("%Y%m%d")))
        except Exception as ex:
            print(f"  TV   FAIL {lg['name']} {d:%Y-%m-%d} ({type(ex).__name__})")
            return lg, {}

    seen, out = set(), []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for lg, data in pool.map(grab, jobs):
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
                if not hname or not aname or "TBD" in hname or "TBD" in aname:
                    continue
                key = (local.isoformat(), hname, aname)
                if key in seen:
                    continue
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
                if not tvs:          # no US broadcaster: nothing for a reader to watch
                    continue
                seen.add(key)
                state = ((ev.get("status") or {}).get("type") or {})
                out.append({
                    "kick": local,
                    "league": lg["name"],
                    "women": bool(lg.get("women")),
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
        # A league may be split into groups (MLS = Eastern/Western Conference). Emit one
        # table per group rather than silently showing only the first. With merge_groups
        # the groups are combined into one overall table (MLS Supporters' Shield).
        groups = [(c.get("abbreviation") or c.get("name") or "",
                   ((c.get("standings") or {}).get("entries")) or [])
                  for c in (data.get("children") or [])]
        groups = [g for g in groups if g[1]]
        if not groups:
            groups = [("", ((data.get("standings") or {}).get("entries")) or [])]
        if lg.get("merge_groups") and len(groups) > 1:
            groups = [("", [en for _, entries in groups for en in entries])]
        for label, entries in groups:
            if not entries:
                continue
            rows = []
            for en in entries:
                stats = {s.get("name"): s for s in en.get("stats", [])}
                def val(name):
                    s = stats.get(name) or {}
                    return s.get("displayValue") or ("" if s.get("value") is None else str(s.get("value")))
                def num(name):
                    s = stats.get(name) or {}
                    try:
                        return float(s.get("value"))
                    except (TypeError, ValueError):
                        return 0.0
                rows.append({
                    "team": (en.get("team") or {}).get("shortDisplayName", ""),
                    "gp": val("gamesPlayed"), "w": val("wins"), "d": val("ties"), "l": val("losses"),
                    "gd": val("pointDifferential"), "pts": val("points"),
                    "_pts": num("points"), "_gd": num("pointDifferential"), "_gp": num("gamesPlayed"),
                    "_w": num("wins"),
                })
            # ESPN returns some groups unsorted, so never trust its order. MLS breaks ties
            # on wins before goal difference; European leagues go straight to GD.
            if lg.get("merge_groups"):
                rows.sort(key=lambda r: (-r["_pts"], -r["_w"], -r["_gd"], r["_gp"]))
            else:
                rows.sort(key=lambda r: (-r["_pts"], -r["_gd"], r["_gp"]))
            if rows:
                name = f'{lg["name"]} {label}'.strip() if len(groups) > 1 else lg["name"]
                tables.append({"name": name, "rows": rows})
    print(f"Tables: {len(tables)} leagues.")
    return tables


# ---------------------------------------------------------------- editorial logic
STOP = set("""the and for with after from his her their its that this has have had was were are not
but out who how what when over into than then they them you your our can could would should will
says said say make made just more most some new old amid ahead back been being before best both
down even first last like long much next now off once only other same still such take there these
those too under until very which while win wins won all any has his her""".split())


def stem(w):
    """Crude suffix stripping: 'causing' and 'caused' describe the same event."""
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[:-len(suf)].rstrip("e") or w
    return w


def fold(s):
    """Strip accents, so Hallgrimsson and Hallgrímsson are the same man. Without this the
    tokenizer splits the accented spelling in two and the match is silently lost."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def words(title):
    toks = re.findall(r"[a-z0-9']+", fold(title).lower())
    return set(stem(t) for t in toks if len(t) > 2 and t not in STOP)


def doc_freq(sigs):
    """How many headlines each word appears in -- rare words are the distinctive ones."""
    df = {}
    for s in sigs:
        for w in s:
            df[w] = df.get(w, 0) + 1
    return df


def is_dupe(wa, wb, df, rare_max=3):
    """Two accounts of one story. Shared *rare* words (names, clubs) carry the signal;
    plain word overlap is unreliable when a wire brief meets a 30-word tabloid headline,
    so containment against the shorter headline matters more than symmetric overlap."""
    if not wa or not wb:
        return False
    inter = wa & wb
    if not inter:
        return False
    jaccard = len(inter) / len(wa | wb)
    if jaccard >= 0.50:
        return True
    contain = len(inter) / min(len(wa), len(wb))
    # the shorter headline almost entirely inside the longer one, and plenty of overlap
    # either way: the same story told at two lengths, whatever the words happen to be
    if jaccard >= 0.40 and contain >= 0.70:
        return True
    rare = sum(1 for w in inter if df.get(w, 99) <= rare_max)
    if rare >= 2 and jaccard >= 0.22:
        return True
    if rare >= 2 and contain >= 0.55:
        return True
    return rare >= 3 and contain >= 0.50


def curate(items, cfg, pinned):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("max_age_hours", 48))
    blocked = [b.lower() for b in pinned.get("block", []) if b]
    hot_words = [h.lower() for h in cfg.get("hot_words", [])]
    reject = [r.lower() for r in cfg.get("reject_terms", [])]
    reject_url = {r.lower() for r in cfg.get("reject_url_terms", [])}
    football = [f.lower() for f in cfg.get("football_terms", [])]
    womens_section = cfg.get("womens_section")
    womens_terms = [w.lower() for w in cfg.get("womens_terms", [])]
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
        # Women's football is decided first and overrules everything else: a WSL report
        # that mentions Arsenal belongs in the women's section, not the Premier League
        # one. Dedicated feeds are trusted outright, since plenty of women's headlines
        # ("Pina hits four as Barcelona score seven") carry no marker of their own.
        if womens_section and (it.get("womens") or any(has(t, low) for t in womens_terms)):
            it["section"] = womens_section
        else:
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
    # dedupe: cluster every retelling of a story, then keep the best-scoring one. The
    # clustering is transitive because two accounts of the same event often share almost
    # no wording with each other while both plainly match a third.
    fresh.sort(key=lambda x: -x["score"])
    sigs = [words(f["title"]) for f in fresh]
    df = doc_freq(sigs)
    # "rare" has to scale with the batch: on a busy night Ireland and Israel appear in a
    # dozen headlines and stop being distinctive at all.
    rare_max = max(3, len(fresh) // 50)
    parent = list(range(len(fresh)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[a] = b

    by_url = {}
    for i, it in enumerate(fresh):
        first = by_url.setdefault(it["url"], i)
        if first != i:
            union(i, first)
    for i in range(len(fresh)):
        for j in range(i + 1, len(fresh)):
            if find(i) != find(j) and is_dupe(sigs[i], sigs[j], df, rare_max):
                union(i, j)

    kept, seen_root = [], set()
    for i, it in enumerate(fresh):  # score-sorted, so the first of a cluster is the best
        root = find(i)
        if root in seen_root:
            continue
        seen_root.add(root)
        kept.append(it)
    if len(fresh) != len(kept):
        print(f"Merged {len(fresh) - len(kept)} duplicate retellings.")

    # lead story
    lead_pin = pinned.get("lead") or {}
    if lead_pin.get("title") and lead_pin.get("url"):
        lead = {"title": lead_pin["title"], "url": lead_pin["url"], "image": None, "source": "", "hot": True}
    else:
        # no image preference: publishers' photos are not ours to display, so the
        # lead is simply the strongest story (see render() -- images are not emitted)
        lead = kept[0] if kept else None
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

    # sections: newest first inside each. The lead and the stories above the masthead
    # were already pulled out of `kept`, so the cap applies to the columns alone.
    per = cfg.get("section_limit", cfg.get("items_per_column", 7))
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
        # Until a network fills these, the empty inventory sells itself.
        email = cfg.get("ad_email") or "info@footiereport.com"
        subject = urllib.parse.quote(f'Advertising on {cfg.get("site_name", "The Footie Report")}')
        return (f'<a class="ad ad-house" href="mailto:{e(email)}?subject={subject}">'
                f'<span class="ad-house-h">ADVERTISE HERE</span>'
                f'<span class="ad-house-s">Reach football fans all day, every day</span>'
                f'<span class="ad-house-c">{e(email)}</span></a>')
    return (f'<div class="ad"><ins class="adsbygoogle" style="display:block" data-ad-client="{e(client)}" '
            f'data-ad-slot="{e(slot)}" data-ad-format="auto" data-full-width-responsive="true"></ins>'
            f'<script>(adsbygoogle=window.adsbygoogle||[]).push({{}});</script></div>')


def wmark(f):
    """Women's fixtures are marked inline so the two games aren't confused."""
    return ' <span class="w">(w)</span>' if f.get("women") else ""


def render_tv(fixtures, cfg, limit=None):
    if not fixtures:
        return ""
    tz = tz_of(cfg)
    today = datetime.now(tz).date()
    shown = fixtures[:limit] if limit else fixtures
    blocks = []
    for day in sorted({f["kick"].date() for f in shown}):
        label = "TODAY" if day == today else ("TOMORROW" if day == today + timedelta(days=1)
                                              else day.strftime("%A %b %-d").upper())
        rows = []
        for f in (x for x in shown if x["kick"].date() == day):
            when = f["detail"] if f["state"] == "in" else (
                "FT" if f["state"] == "post" else f["kick"].strftime("%-I:%M %p"))
            # data-time marks a cell holding a clock time; live and finished rows carry a
            # status instead, so the browser regroups them by day but leaves the text be.
            live = f["state"] in ("in", "post")
            score = f' <b>{e(f["score"])}</b>' if f["score"] else ""
            rows.append(
                f'<tr data-utc="{utc_attr(f["kick"])}">'
                f'<td class="t"{"" if live else " data-time"}>{e(when)}</td>'
                f'<td class="m">{e(f["away"])} at {e(f["home"])}{wmark(f)}{score}</td>'
                f'<td class="c">{e(f["tv"]) or "&mdash;"}</td></tr>')
        blocks.append(f'<h3>{e(label)}</h3><table>{"".join(rows)}</table>')
    zone = "ET" if cfg.get("timezone", "America/New_York") == "America/New_York" else ""
    more = len(fixtures) - len(shown)
    tail = f"{more} more &middot; " if more > 0 else ""
    return (f'<section class="tv"><h2>ON TV <span class="tz">{e(zone)}</span></h2>{"".join(blocks)}'
            f'<p class="note">{tail}'
            f'<a href="/how-to-watch.html">Full week and channels &rarr;</a></p></section>')


def render_pods_panel(cfg):
    """Compact podcast list for the front page; the full page carries the detail."""
    shows = cfg.get("podcasts") or []
    if not shows:
        return ""
    rows = []
    for s in shows[:cfg.get("pod_rows_front", 12)]:
        links = []
        if s.get("apple"):
            links.append(f'<a href="{e(s["apple"])}" target="_blank" rel="noopener">Apple</a>')
        if s.get("spotify"):
            links.append(f'<a href="{e(s["spotify"])}" target="_blank" rel="noopener">Spotify</a>')
        rows.append(f'<tr><td class="m">{e(s["name"])}</td>'
                    f'<td class="c">{" &middot; ".join(links)}</td></tr>')
    return (f'<section class="pods-panel"><h2>PODCASTS</h2><table>{"".join(rows)}</table>'
            f'<p class="note"><a href="/podcasts.html">All shows &rarr;</a></p></section>')


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


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def zone_times(kick, cfg):
    """Kickoff in the four US time zones, e.g. '3:00 PM ET / 12:00 PM PT'."""
    out = []
    for name, tz in (("ET", "America/New_York"), ("CT", "America/Chicago"),
                     ("MT", "America/Denver"), ("PT", "America/Los_Angeles")):
        try:
            out.append(f'{kick.astimezone(ZoneInfo(tz)).strftime("%-I:%M %p")} {name}')
        except Exception:
            continue
    return " / ".join(out)


def utc_attr(kick):
    """Kickoff as an ISO instant, so the browser can re-render it in the viewer's zone."""
    return kick.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Served times are Eastern so that crawlers and no-JS readers get a correct, indexable
# page. This rewrites them in the visitor's own zone when that zone differs, regrouping
# the day headings too -- a 9pm ET Friday kickoff is Saturday morning in Europe. Any
# failure leaves the server-rendered Eastern markup exactly as it is.
TZ_SCRIPT = """<script>
(function(){
try{
  var tz=Intl.DateTimeFormat().resolvedOptions().timeZone; if(!tz) return;
  var HOME='America/New_York', now=new Date();
  function part(d,zone,opts){return new Intl.DateTimeFormat('en-US',Object.assign({timeZone:zone},opts)).format(d);}
  var stamp={hour:'numeric',minute:'2-digit',year:'numeric',month:'2-digit',day:'2-digit'};
  if(part(now,tz,stamp)===part(now,HOME,stamp)) return;   // same clock as the server: nothing to do
  function key(d){return new Intl.DateTimeFormat('en-CA',{timeZone:tz,year:'numeric',month:'2-digit',day:'2-digit'}).format(d);}
  function clock(d){return part(d,tz,{hour:'numeric',minute:'2-digit'});}
  function abbr(d){var p=new Intl.DateTimeFormat('en-US',{timeZone:tz,timeZoneName:'short'}).formatToParts(d);
    for(var i=0;i<p.length;i++){if(p[i].type==='timeZoneName') return p[i].value;} return '';}
  var todayK=key(now), tomorrowK=key(new Date(now.getTime()+864e5));
  function label(d){var k=key(d);
    if(k===todayK) return 'TODAY';
    if(k===tomorrowK) return 'TOMORROW';
    return part(d,tz,{weekday:'long',month:'short',day:'numeric'}).replace(/,/g,'').toUpperCase();}

  Array.prototype.forEach.call(document.querySelectorAll('section.tv'),function(sec){
    var rows=sec.querySelectorAll('tr[data-utc]'); if(!rows.length) return;
    var order=[],seen={};
    Array.prototype.forEach.call(rows,function(tr){
      var d=new Date(tr.getAttribute('data-utc')); if(isNaN(d)) return;
      var cell=tr.querySelector('td.t[data-time]'); if(cell) cell.textContent=clock(d);
      var k=key(d);
      if(!seen[k]){seen[k]={label:label(d),rows:[]};order.push(seen[k]);}
      seen[k].rows.push(tr);
    });
    if(!order.length) return;
    Array.prototype.forEach.call(sec.querySelectorAll('h3, table'),function(n){n.parentNode.removeChild(n);});
    var anchor=sec.querySelector('p.note');
    order.forEach(function(g){
      var h=document.createElement('h3'); h.textContent=g.label;
      var t=document.createElement('table'), b=document.createElement('tbody');
      g.rows.forEach(function(r){b.appendChild(r);}); t.appendChild(b);
      if(anchor){sec.insertBefore(h,anchor); sec.insertBefore(t,anchor);} else {sec.appendChild(h); sec.appendChild(t);}
    });
  });

  var a=abbr(now);
  Array.prototype.forEach.call(document.querySelectorAll('.tz'),function(n){n.textContent=a;});
  Array.prototype.forEach.call(document.querySelectorAll('.tz-name'),function(n){n.textContent='your local time';});

  var lt=document.querySelector('.localtime[data-utc]');
  if(lt){var d=new Date(lt.getAttribute('data-utc'));
    if(!isNaN(d)){
      lt.textContent=part(d,tz,{weekday:'long',month:'long',day:'numeric',hour:'numeric',minute:'2-digit'})+' '+abbr(d);
      var row=lt.parentNode; while(row&&row.tagName!=='TR'){row=row.parentNode;}
      if(row) row.removeAttribute('hidden');
    }}
}catch(e){}
})();
</script>"""


def page_shell(cfg, title, description, body, canonical=""):
    can = f'<link rel="canonical" href="https://{e(cfg["domain"])}/{e(canonical)}">' if cfg.get("domain") else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
{can}
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<link rel="stylesheet" href="/style.css">
</head>
<body>
<p class="crumb"><a href="/">&larr; {e(cfg["site_name"])}</a></p>
{body}
<footer>Listings are for the United States and can change; check your provider.
<br>{e(cfg["site_name"])} &middot; <a href="/about.html">About</a></footer>
{TZ_SCRIPT}
</body>
</html>
"""


def watch_pages(cfg, fixtures):
    """A weekly US TV schedule plus one page per fixture — the pages people search for."""
    if not fixtures:
        return {}
    tz = tz_of(cfg)
    today = datetime.now(tz).date()
    pages, rows_by_day = {}, {}
    for f in fixtures:
        rows_by_day.setdefault(f["kick"].date(), []).append(f)

    blocks = []
    for day in sorted(rows_by_day):
        label = "Today" if day == today else ("Tomorrow" if day == today + timedelta(days=1)
                                              else day.strftime("%A, %B %-d"))
        rows = []
        for f in rows_by_day[day]:
            match = f'{f["away"]} at {f["home"]}'
            slug = f'{slugify(f["away"])}-vs-{slugify(f["home"])}-{f["kick"]:%Y-%m-%d}'
            rows.append(
                f'<tr data-utc="{utc_attr(f["kick"])}">'
                f'<td class="t" data-time>{e(f["kick"].strftime("%-I:%M %p"))}</td>'
                f'<td class="m"><a href="/watch/{e(slug)}.html">{e(match)}</a>{wmark(f)} '
                f'<span class="src">{e(f["league"])}</span></td>'
                f'<td class="c">{e(f["tv"]) or "&mdash;"}</td></tr>')
            pages[f"watch/{slug}.html"] = match_page(cfg, f, slug)
        blocks.append(f'<h3>{e(label)}</h3><table>{"".join(rows)}</table>')

    body = (f'<section class="tv"><h1>Soccer on US TV this week</h1>'
            f'<p class="lede">Every match with a US broadcaster, with kickoff times in '
            f'<span class="tz-name">Eastern</span>. '
            f'Click a match for times in every US time zone.</p>{"".join(blocks)}</section>')
    pages["how-to-watch.html"] = page_shell(
        cfg, "Soccer on TV in the US this week: times and channels",
        "Full schedule of Premier League, La Liga, Serie A, Bundesliga, Ligue 1, Champions League and MLS "
        "matches on US television this week, with kickoff times and channels.",
        body, "how-to-watch.html")
    return pages


def match_page(cfg, f, slug):
    match = f'{f["away"]} vs {f["home"]}'
    title = f'How to watch {match} in the US: TV channel, live stream and kickoff time'
    tv = f["tv"] or "no US broadcaster listed yet"
    when = f["kick"].strftime("%A, %B %-d")
    desc = (f'{match} kicks off at {f["kick"].strftime("%-I:%M %p")} ET on {when}'
            f'{" and is on " + f["tv"] if f["tv"] else ""}. Kickoff times for every US time zone.')
    body = f"""<article class="match">
<h1>How to watch {e(match)} in the US</h1>
<table class="detail">
<tr><td>Match</td><td>{e(match)} &middot; {e(f["league"])}</td></tr>
<tr><td>Date</td><td>{e(when)}</td></tr>
<tr><td>Kickoff</td><td>{e(zone_times(f["kick"], cfg))}</td></tr>
<tr hidden><td>Your time</td><td class="localtime" data-utc="{utc_attr(f["kick"])}"></td></tr>
<tr><td>US TV / stream</td><td>{e(tv)}</td></tr>
</table>
<p class="note">Times and channels come from the published schedule and can change.
See the <a href="/how-to-watch.html">full week of fixtures</a> or
<a href="/">today's football headlines</a>.</p>
</article>"""
    return page_shell(cfg, title, desc, body, f"watch/{slug}.html")


def sitemap(cfg, extra_paths):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = ["", "tables.html", "podcasts.html", "about.html", "privacy.html"] + list(extra_paths)
    body = "".join(
        f'<url><loc>https://{e(cfg["domain"])}/{e(p)}</loc><lastmod>{now}</lastmod></url>' for p in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'


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


def podcasts_page(cfg):
    """Static directory of football podcasts. No feeds: episode links are unreliable
    across podcast hosts, and a show's listing page never goes stale."""
    shows = cfg.get("podcasts") or []
    if not shows:
        return ""
    rows = []
    for s in shows:
        links = []
        if s.get("apple"):
            links.append(f'<a href="{e(s["apple"])}" target="_blank" rel="noopener">Apple</a>')
        if s.get("spotify"):
            links.append(f'<a href="{e(s["spotify"])}" target="_blank" rel="noopener">Spotify</a>')
        rows.append(f'<tr><td class="n">{e(s["name"])}</td>'
                    f'<td class="by">{e(s.get("by", ""))}</td>'
                    f'<td class="l">{" &middot; ".join(links)}</td></tr>')
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Football podcasts &middot; {e(cfg["site_name"])}</title>
<meta name="description" content="A curated list of the best world football podcasts, with links to listen on Apple Podcasts and Spotify.">
<link rel="canonical" href="https://{e(cfg["domain"])}/podcasts.html">
<link rel="stylesheet" href="style.css">
</head>
<body>
<p class="crumb"><a href="/">&larr; Back to headlines</a></p>
<section class="pods">
<h1>FOOTBALL PODCASTS</h1>
<p class="lede">The shows we actually listen to. Links open on Apple Podcasts or Spotify.</p>
<table>{"".join(rows)}</table>
</section>
<footer>{e(cfg["site_name"])} &middot; <a href="/about.html">About</a></footer>
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
        lead_html = f'<div class="lead"><h1>{link(lead, big=True)}</h1></div>'
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
<div class="panels">
  <div class="panel">{render_tv(fixtures or [], cfg, limit=cfg.get("tv_rows_front", 8))}</div>
  <div class="panel">{render_pods_panel(cfg)}</div>
</div>
<hr>
{render_tables(tables or [], cfg, limit=cfg.get("table_rows_front", 6))}
{ad_slot(cfg, "bottom")}
<footer>
  {counter_html}
  Headlines link to their original publishers; all stories &copy; their respective owners.
  <br>{e(cfg["site_name"])} &middot; <a href="podcasts.html">Podcasts</a> &middot; <a href="about.html">About / Contact / Privacy</a>
</footer>
{TZ_SCRIPT}
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
    pods = podcasts_page(cfg)
    if pods:
        (DIST / "podcasts.html").write_text(pods)
        print(f"Podcasts: {len(cfg.get('podcasts') or [])} shows.")
    pages = watch_pages(cfg, games)
    for path, html in pages.items():
        out = DIST / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html)
    if pages:
        print(f"Wrote {len(pages)} how-to-watch pages.")
    # Per-match pages carry the kickoff date in the URL, so today's set 404s
    # tomorrow. Submitting them would hand Google a sitemap that is mostly dead
    # links within a day; they stay linked from how-to-watch.html, which is
    # stable, so crawlers can still reach them on their own.
    stable = [p for p in pages if not p.startswith("watch/")]
    (DIST / "sitemap.xml").write_text(sitemap(cfg, stable))
    for f in (ROOT / "static").iterdir():
        (DIST / f.name).write_bytes(f.read_bytes())
    if cfg.get("domain") and "example.com" not in cfg["domain"]:
        (DIST / "CNAME").write_text(cfg["domain"] + "\n")
    if cfg.get("adsense_client"):
        pub = cfg["adsense_client"].replace("ca-", "")
        (DIST / "ads.txt").write_text(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n")
    (DIST / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: https://{cfg.get('domain', '')}/sitemap.xml\n")
    print("Wrote dist/index.html")


if __name__ == "__main__":
    main()
