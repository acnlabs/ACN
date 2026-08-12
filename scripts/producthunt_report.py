#!/usr/bin/env python3
"""
Read-only Product Hunt API client for launch research and launch-day tracking.

The Product Hunt developer token is read-only (``public`` scope), so this
script cannot create or edit a launch -- submitting the product stays a manual
step at https://www.producthunt.com/posts/new. See docs/product-hunt/README.md.

Usage:
    export PRODUCTHUNT_TOKEN=<developer token>

    # Verify the token and show whose account it belongs to
    python3 scripts/producthunt_report.py whoami

    # Follower counts for the topics the launch copy proposes
    python3 scripts/producthunt_report.py topics

    # What is ranking in a category right now, and with what taglines
    python3 scripts/producthunt_report.py research --topic developer-tools --days 120

    # Launch-day tracking
    python3 scripts/producthunt_report.py track --slug acn-agent-collaboration-network

Only the standard library is used so the script runs with a bare ``python3``,
outside the project virtualenv.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://api.producthunt.com/v2/api/graphql"
USER_AGENT = "acn-producthunt-report/1.0 (+https://github.com/acnlabs/ACN)"

# Topics proposed in docs/product-hunt/launch-copy.md, plus the runners-up worth
# checking before every launch since follower counts drift.
DEFAULT_TOPICS = (
    "artificial-intelligence",
    "developer-tools",
    "open-source",
    "api-1",
    "github",
    "bots",
    "saas",
)


class ProductHuntError(RuntimeError):
    """Raised when the Product Hunt API rejects a request or returns errors."""


def get_token() -> str:
    """Return the developer token, exiting with guidance when it is missing."""
    token = os.environ.get("PRODUCTHUNT_TOKEN", "").strip()
    if not token:
        print(
            "PRODUCTHUNT_TOKEN is not set.\n"
            "Create a developer token at "
            "https://api.producthunt.com/v2/oauth/applications and export it:\n"
            "    export PRODUCTHUNT_TOKEN=<token>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against the Product Hunt API and return ``data``."""
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 401:
            raise ProductHuntError(
                "Product Hunt rejected the token (401). It may have been revoked or rotated."
            ) from exc
        if exc.code == 429:
            raise ProductHuntError(
                "Rate limited by Product Hunt (429). Limits reset every 15 minutes."
            ) from exc
        raise ProductHuntError(f"HTTP {exc.code} from Product Hunt: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProductHuntError(f"Could not reach Product Hunt: {exc.reason}") from exc

    if body.get("errors"):
        messages = "; ".join(e.get("message", "unknown") for e in body["errors"])
        raise ProductHuntError(f"Product Hunt returned errors: {messages}")

    return body.get("data") or {}


def cmd_whoami(token: str, _args: argparse.Namespace) -> int:
    """Print the account the token belongs to."""
    query = "query { viewer { user { id name username headline } } }"
    user = ((graphql(token, query).get("viewer") or {}).get("user")) or {}
    if not user:
        print("Token is valid but returned no viewer (client-credentials token?).")
        return 0
    print(f"{user.get('name')} (@{user.get('username')})  id={user.get('id')}")
    if user.get("headline"):
        print(f"  {user['headline']}")
    return 0


def cmd_topics(token: str, args: argparse.Namespace) -> int:
    """Print follower and post counts for candidate launch topics."""
    slugs = args.slug or list(DEFAULT_TOPICS)
    query = """
    query($slug: String!) {
      topic(slug: $slug) { name slug followersCount postsCount }
    }
    """
    print(f"{'topic':<28} {'followers':>10} {'posts':>9}")
    print("-" * 49)
    missing: list[str] = []
    for slug in slugs:
        topic = graphql(token, query, {"slug": slug}).get("topic")
        if not topic:
            missing.append(slug)
            continue
        print(f"{topic['slug']:<28} {topic['followersCount']:>10,} {topic['postsCount']:>9,}")
    if missing:
        print(f"\nNo such topic: {', '.join(missing)}")
    return 0


def cmd_research(token: str, args: argparse.Namespace) -> int:
    """Print the top-voted recent launches in a topic, to calibrate copy."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)
    query = """
    query($topic: String, $after: DateTime, $first: Int!) {
      posts(topic: $topic, order: VOTES, postedAfter: $after, first: $first) {
        edges {
          node {
            name tagline votesCount commentsCount slug
            topics(first: 5) { edges { node { slug } } }
          }
        }
      }
    }
    """
    data = graphql(
        token,
        query,
        {"topic": args.topic, "after": since.isoformat(), "first": args.limit},
    )
    edges = ((data.get("posts") or {}).get("edges")) or []
    if not edges:
        print(f"No launches found in '{args.topic}' over the last {args.days} days.")
        return 0

    print(f"Top {len(edges)} launches in '{args.topic}' over the last {args.days} days\n")
    print(f"{'votes':>6} {'cmts':>6}  {'name':<26} tagline")
    print("-" * 100)
    for edge in edges:
        node = edge["node"]
        print(
            f"{node['votesCount']:>6} {node['commentsCount']:>6}  "
            f"{node['name'][:25]:<26} {node['tagline'][:58]}"
        )

    taglines = [e["node"]["tagline"] for e in edges]
    average = sum(len(t) for t in taglines) / len(taglines)
    print(f"\nMedian tagline length among these: {average:.0f} characters (limit is 60).")

    counts: dict[str, int] = {}
    for edge in edges:
        for topic_edge in edge["node"]["topics"]["edges"]:
            slug = topic_edge["node"]["slug"]
            counts[slug] = counts.get(slug, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    print("Topics these launches paired with: " + ", ".join(f"{s} ({n})" for s, n in ranked))
    return 0


def cmd_track(token: str, args: argparse.Namespace) -> int:
    """Print live votes, comments, and same-day rank for a launch."""
    query = """
    query($slug: String!) {
      post(slug: $slug) {
        name tagline slug votesCount commentsCount createdAt url
      }
    }
    """
    post = graphql(token, query, {"slug": args.slug}).get("post")
    if not post:
        print(
            f"No launch found with slug '{args.slug}'.\n"
            "The slug is the last path segment of the Product Hunt URL.",
            file=sys.stderr,
        )
        return 1

    created = dt.datetime.fromisoformat(post["createdAt"].replace("Z", "+00:00"))
    age = dt.datetime.now(dt.UTC) - created

    print(f"{post['name']} - {post['tagline']}")
    print(f"{post['url']}\n")
    print(f"  votes:    {post['votesCount']:,}")
    print(f"  comments: {post['commentsCount']:,}")
    print(f"  age:      {age.total_seconds() / 3600:.1f}h since launch")

    rank = _same_day_rank(token, post["slug"], created)
    if rank is None:
        print("  rank:     not in the top 100 for its launch day")
    else:
        print(f"  rank:     #{rank} for its launch day")
    return 0


def _same_day_rank(token: str, slug: str, created: dt.datetime) -> int | None:
    """Return the launch's position among same-day posts ordered by votes."""
    day_start = created.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + dt.timedelta(days=1)
    query = """
    query($after: DateTime, $before: DateTime, $cursor: String) {
      posts(order: VOTES, postedAfter: $after, postedBefore: $before, first: 50, after: $cursor) {
        edges { node { slug } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    position = 0
    cursor: str | None = None
    for _ in range(2):  # 100 posts is deeper than any rank worth reporting
        data = graphql(
            token,
            query,
            {"after": day_start.isoformat(), "before": day_end.isoformat(), "cursor": cursor},
        )
        posts = data.get("posts") or {}
        for edge in posts.get("edges") or []:
            position += 1
            if edge["node"]["slug"] == slug:
                return position
        page = posts.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Read-only Product Hunt research and launch-day tracking for ACN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="verify the token and show the owning account")

    topics = sub.add_parser("topics", help="follower counts for candidate launch topics")
    topics.add_argument("slug", nargs="*", help="topic slugs (defaults to the launch shortlist)")

    research = sub.add_parser("research", help="top recent launches in a topic")
    research.add_argument("--topic", default="developer-tools", help="topic slug to research")
    research.add_argument("--days", type=int, default=120, help="how far back to look")
    research.add_argument("--limit", type=int, default=20, help="how many launches to show")

    track = sub.add_parser("track", help="live votes, comments, and rank for a launch")
    track.add_argument("--slug", required=True, help="launch slug from its Product Hunt URL")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    handlers = {
        "whoami": cmd_whoami,
        "topics": cmd_topics,
        "research": cmd_research,
        "track": cmd_track,
    }
    try:
        return handlers[args.command](get_token(), args)
    except ProductHuntError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
