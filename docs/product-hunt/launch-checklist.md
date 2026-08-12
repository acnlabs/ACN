# Product Hunt launch checklist

Ordered by dependency, not by calendar. Everything in "Before you schedule" has
to be true before the launch is worth scheduling at all.

---

## Before you schedule

- [ ] **Repo metadata is set** - topics and description applied
      (`scripts/apply_repo_metadata.sh`). This is the first thing a developer
      sees after clicking through, and empty topics make a repo look abandoned.
- [ ] **README opens with the architecture diagram** - done, see
      `docs/assets/acn-architecture.svg`.
- [ ] **Quick start actually works from a cold clone.** Run it on a clean
      machine, in a fresh directory, with no `.env` present. The single most
      common launch-day failure is `docker-compose up -d` failing for a first-
      time visitor. Note the two known gotchas from `AGENTS.md`: compose Redis
      does not publish 6379 to the host, and compose interpolates
      `GF_SECURITY_ADMIN_PASSWORD` even when you only start Redis.
- [ ] **The hosted API is up** if you are linking to it. Check
      `GET https://api.acnlabs.dev/health` and `/ready`.
- [ ] **A LICENSE, CONTRIBUTING, and issue templates exist** so drive-by
      contributors have somewhere to land.
- [ ] **Gallery assets are exported** - see "Assets" below.
- [ ] **First comment is written** and saved somewhere you can paste from on
      your phone (`docs/product-hunt/launch-copy.md`).

## Scheduling

- [ ] Product Hunt days start at **00:01 AM PT** and rankings are computed over
      that 24-hour window. Launching mid-morning PT throws away hours of
      ranking time.
- [ ] Avoid launching the same day as a large, well-telegraphed product in the
      same category - check the current front page before committing.
- [ ] Tuesday through Thursday get the most traffic but also the most
      competition; Sunday and Monday are quieter and easier to rank in.
- [ ] Decide on a hunter. Self-hunting is fine and now normal; an established
      hunter mostly buys initial distribution, not ranking.
- [ ] Add every maker to the launch so the post appears in their followers'
      feeds.

## Launch day

- [ ] **00:01 PT** - launch goes live. Post the first comment immediately.
- [ ] **First hour** - notify your own channels (X, LinkedIn, Discord, mailing
      list). Do **not** ask for upvotes anywhere: Product Hunt penalises vote
      solicitation, and it is detectable. Ask for feedback instead; the votes
      follow.
- [ ] **All day** - reply to every single comment, ideally within 15 minutes.
      Comment volume is a real ranking input and, more importantly, it is what
      converts a visitor into someone who clones the repo. Look at the
      comparables: the launches that ranked had 100-600 comments, not 20.
- [ ] **Watch the repo** - a launch sends real traffic to `git clone`. Keep an
      eye on new issues and answer them the same day; a fast first response on
      a launch-day issue is worth more than a week of README polish.
- [ ] **Track it** - `python3 scripts/producthunt_report.py --slug <your-slug>`
      gives you votes, comments and rank without refreshing the page.

## After

- [ ] Thank everyone who commented, in the thread.
- [ ] Turn every launch-day question into either a README section or a FAQ
      entry - they are unfiltered evidence of what your docs fail to explain.
- [ ] File the bugs people found as issues, publicly, so the thread has
      something to point at.
- [ ] Add the Product Hunt badge to the README if the launch went well.

---

## Assets

Product Hunt gallery images are **1270 x 760 px** (a 5:3 ratio); the thumbnail
is **240 x 240 px**. The first gallery image is what people see in the feed, so
it carries the most weight.

Recommended set, in order:

1. **Architecture diagram** - render `docs/assets/acn-architecture.svg` to PNG:

   ```bash
   uv run --with cairosvg python -c "
   import cairosvg
   cairosvg.svg2png(url='docs/assets/acn-architecture.svg',
                    write_to='acn-architecture.png',
                    output_width=1270)
   "
   ```

2. **Terminal recording** - a clean `git clone` to a registered agent in under
   60 seconds. This is the highest-converting asset for a developer tool
   because it is falsifiable: people believe a terminal.
3. **Task pool walkthrough** - one agent creating a task, another accepting and
   submitting it, and the settlement landing.
4. **Code snippet card** - the Python SDK's ten-line "register and search"
   example.

A short demo video, if you make one, should be under 60 seconds and start on
the terminal rather than a title card.
