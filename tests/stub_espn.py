"""Offline check of the TV block and league tables: feeds build.py fake ESPN JSON
(same shape as the real endpoints) so the layout can be verified without network.
Run: python tests/make_fixtures.py && python tests/stub_espn.py"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("b", ROOT / "build.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

cfg = json.loads((ROOT / "config.json").read_text())
pinned = json.loads((ROOT / "pinned.json").read_text())
now = datetime.now(timezone.utc)

TEAMS = [("Arsenal", "Chelsea"), ("Real Madrid", "Sevilla"), ("Inter", "Napoli"), ("Bayern", "Dortmund")]


def fake_event(i, day_offset, state):
    kick = (now + timedelta(days=day_offset)).replace(hour=14 + i, minute=30, second=0, microsecond=0)
    away, home = TEAMS[i % len(TEAMS)]
    return {
        "date": kick.strftime("%Y-%m-%dT%H:%MZ"),
        "status": {"type": {"state": state, "shortDetail": "63'" if state == "in" else "FT",
                            "completed": state == "post"}},
        "competitions": [{
            "competitors": [
                {"homeAway": "home", "team": {"shortDisplayName": home}, "score": "1"},
                {"homeAway": "away", "team": {"shortDisplayName": away}, "score": "2"},
            ],
            "geoBroadcasts": [{"media": {"shortName": "USA Net"}}, {"media": {"shortName": "Tele"}}],
        }],
    }


def fake_json(url, timeout=12):
    if "scoreboard" in url:
        day = url.split("dates=")[1]
        offset = (datetime.strptime(day, "%Y%m%d").date() - now.date()).days
        state = "post" if offset == 0 else "pre"
        return {"events": [fake_event(i, offset, state) for i in range(2)]}
    entries = []
    for i in range(20):
        entries.append({"team": {"shortDisplayName": f"Club {i+1}"}, "stats": [
            {"name": "gamesPlayed", "displayValue": "5"}, {"name": "wins", "displayValue": str(5 - i % 6)},
            {"name": "ties", "displayValue": "1"}, {"name": "losses", "displayValue": str(i % 5)},
            {"name": "pointDifferential", "displayValue": f"+{20 - i}"},
            {"name": "points", "displayValue": str(45 - i * 2)}]})
    return {"children": [{"name": "Test league", "standings": {"entries": entries}}]}


b.fetch_json = fake_json
items = b.load_all(cfg, str(ROOT / "tests" / "fixtures"))
lead, top, sections = b.curate(items, cfg, pinned)
games, tables = b.get_fixtures(cfg), b.get_tables(cfg)
out = ROOT / "dist"
out.mkdir(exist_ok=True)
(out / "index.html").write_text(b.render(cfg, lead, top, sections, games, tables))
(out / "tables.html").write_text(b.tables_page(cfg, tables))
for f in (ROOT / "static").iterdir():
    (out / f.name).write_bytes(f.read_bytes())
print(f"wrote {out}/index.html with {len(games)} fixtures and {len(tables)} tables")
