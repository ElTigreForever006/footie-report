# The Footie Report — a Drudge-style world football site

A self-updating headline page. Every 15 minutes a free GitHub Action pulls ~19 football RSS feeds, dedupes and ranks the stories, and publishes a static page to GitHub Pages on your own domain. No server, no database, no monthly hosting bill — your only cost is the domain (~$10–15/yr).

## What's in here

| File | What it does |
|---|---|
| `config.json` | Site name, domain, AdSense IDs, feed list, column layout, "hot" (red) keywords |
| `pinned.json` | **Your editorial desk.** Override the giant red lead headline, pin top links, block stories |
| `build.py` | Fetches feeds and writes `dist/` (Python standard library only) |
| `static/` | Stylesheet and About/Contact/Privacy page |
| `.github/workflows/build.yml` | Rebuilds every 15 min and whenever you edit a file |
| `tests/make_fixtures.py` | Fake feeds for offline testing |

## Launch it (about 30 minutes)

1. **Pick a name & buy a domain.** Cloudflare Registrar or Namecheap. Then set `site_name`, `tagline`, and `domain` (e.g. `www.yourname.com`) in `config.json`.
2. **Create a GitHub account** (free) and a new **public** repository. Upload this whole folder (drag-and-drop works; make sure the hidden `.github` folder comes along).
3. **Turn on Pages:** repo → Settings → Pages → Source: **GitHub Actions**.
4. **Run it:** Actions tab → "Build & deploy headlines" → Run workflow. The site appears at `https://<you>.github.io/<repo>/`. Check the log to see which feeds succeeded.
5. **Connect your domain:** Settings → Pages → Custom domain → enter `www.yourname.com`. At your registrar add a `CNAME` record `www` → `<you>.github.io`, plus the four `A` records GitHub lists for the bare domain. Tick **Enforce HTTPS** once it verifies.

## Running it day to day

- **Be the editor.** Drudge works because a human picks the lead. Edit `pinned.json` on GitHub (pencil icon) — set `lead.title` + `lead.url` (+ optional `image`) and it's live within a couple of minutes. Clear it to go back to automatic.
- **Add/remove sources** in `config.json → feeds`. `section` picks the column; `priority` (1–3) affects what floats to the top.
- **Red headlines** are driven by `hot_words`.

## Making money

1. Launch, then publish some original content before applying — AdSense frequently rejects pure link aggregators as "low-value content." A short daily editor's take, a transfer-rumour roundup, or a weekly column goes a long way. Real traffic helps too.
2. Finish the privacy policy in `static/about.html` and add a Google-certified consent banner for EEA/UK visitors.
3. Once approved, put your `ca-pub-…` ID in `adsense_client` and your ad-unit IDs in `adsense_slots`. The build then injects the ad code and creates `ads.txt` automatically.
4. Alternatives if AdSense says no or you outgrow it: Ezoic, Media.net, Carbon/BuySellAds (direct sponsorships), or selling the top banner directly to betting/kit/streaming advertisers. Check local rules first if you take gambling ads.

## Legal notes (you'll know these better than I do)

- Headline-plus-link aggregation is the Drudge model and is widely practiced, but each feed comes with its own publisher terms — some RSS terms limit commercial use. Review the terms for the feeds you keep, and honour removal requests (the About page promises to).
- Don't republish article text or images from publishers. The only image used is the lead thumbnail the feed supplies itself; if you'd rather be conservative, leave `lead.image` blank and drop images from `find_image` usage.
- Avoid using club, league or competition marks in your name or logo.

## Test locally

```bash
python tests/make_fixtures.py
python build.py --fixtures tests/fixtures   # offline, fake headlines
python build.py                             # live feeds
open dist/index.html
```
