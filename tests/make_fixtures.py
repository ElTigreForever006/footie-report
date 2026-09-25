"""Generate fake offline feeds (tests/fixtures/N.xml) so build.py can be tested without network.
All headlines here are dummy test data. Run: python tests/make_fixtures.py && python build.py --fixtures tests/fixtures"""
import json
import random
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)
cfg = json.loads((ROOT / "config.json").read_text())
random.seed(7)
clubs = ["Arsenal", "Real Madrid", "Juventus", "Bayern", "PSG", "Inter Miami", "Napoli", "Barcelona", "Liverpool", "Dortmund"]
verbs = ["TEST: {c} complete signing of midfielder", "TEST: {c} manager sacked after derby defeat", "TEST: {c} in Champions League thriller",
         "TEST: {c} confirm injury blow", "TEST: {c} bid rejected for striker", "TEST: {c} win on penalties", "TEST: World Cup qualifier preview for {c} players"]
now = datetime.now(timezone.utc)
for i, feed in enumerate(cfg["feeds"]):
    items = []
    for j in range(8):
        t = random.choice(verbs).format(c=random.choice(clubs)) + " " + " ".join(random.sample(["amid","late","drama","fans","stun","rivals","coach","deal","talks","season","window","keeper","captain","derby","title","race","exit","return","debut","crisis","hero","record","clash","away","home"], 5))
        d = format_datetime(now - timedelta(minutes=random.randint(1, 60 * 30)))
        img = f'<media:content url="https://picsum.photos/seed/{i}{j}/800/450" medium="image"/>' if j == 0 else ""
        items.append(f"<item><title>{t}</title><link>https://example.com/{i}/{j}</link><pubDate>{d}</pubDate>{img}</item>")
    (OUT / f"{i}.xml").write_text('<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
                                  + "".join(items) + "</channel></rss>")
print(f"wrote {len(cfg['feeds'])} fixture feeds to {OUT}")
