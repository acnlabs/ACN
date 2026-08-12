# Product Hunt launch kit

Everything needed to launch ACN on Product Hunt, kept in the repo so the copy
is reviewable and versioned like the rest of the project.

| File | Contents |
|------|----------|
| [`launch-copy.md`](launch-copy.md) | Name, tagline, description, topics, first comment, reply templates, distribution posts |
| [`launch-checklist.md`](launch-checklist.md) | Pre-launch gates, scheduling notes, launch-day runbook, gallery asset specs |

Supporting tooling:

| Script | Purpose |
|--------|---------|
| [`scripts/apply_repo_metadata.sh`](../../scripts/apply_repo_metadata.sh) | Applies the GitHub repo topics and description that the launch copy assumes |
| [`scripts/producthunt_report.py`](../../scripts/producthunt_report.py) | Read-only Product Hunt API client for competitive research and launch-day tracking |

---

## What the Product Hunt API can and cannot do

This matters for planning, because it is tempting to assume the launch can be
automated end to end. It cannot.

**A developer token is read-only.** Product Hunt's API v2 grants the `public`
scope by default. Write scope exists but requires manual approval from Product
Hunt, and even with it the API exposes no mutation for creating a launch.

**The launch itself is a manual, browser-only step.** Submit the product at
[producthunt.com/posts/new](https://www.producthunt.com/posts/new), fill in the
fields from `launch-copy.md`, upload the gallery assets, and schedule it.

**What the API is genuinely good for:**

- Competitive research before you write the copy - what taglines and topic
  combinations are ranking in your category right now
- Topic selection backed by real follower counts rather than a guess
- Launch-day tracking without refreshing the page

Rate limits are 6,250 complexity points and 450 requests per 15 minutes, which
is far more than this kit needs.

## Credentials

`scripts/producthunt_report.py` reads the token from `PRODUCTHUNT_TOKEN`:

```bash
export PRODUCTHUNT_TOKEN=<developer token>
python3 scripts/producthunt_report.py topics
```

Create the token at
[api.producthunt.com/v2/oauth/applications](https://api.producthunt.com/v2/oauth/applications)
(create an application, then "Create Token" at the bottom of its page). The
token does not expire and is tied to the account that created it, so treat it
as a credential: never commit it, and rotate it if it is ever pasted into a
chat, an issue, or a log.
