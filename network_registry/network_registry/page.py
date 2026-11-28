"""acnlist, the public page of the directory.

Cards come from the listed catalog. Gateway URLs are not written into this file.
"""

from datetime import UTC, datetime
from html import escape

from network_registry.catalog import Network

_PAGE = """
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      font-family: ui-sans-serif, system-ui, sans-serif;
      margin: 0;
      background: #f6f8fa;
      color: #161b22;
    }
    .page { max-width: 64rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
    header { display: flex; align-items: flex-end; justify-content: space-between; gap: 1rem; margin-bottom: 1.5rem; }
    h1 { font-size: 2rem; letter-spacing: -0.02em; margin: 0; }
    .intro { margin: 0.35rem 0 0; color: #57606a; font-size: 0.95rem; }
    .cta { display: inline-block; padding: 0.5rem 0.9rem; border-radius: 999px; background: #161b22; color: #fff; text-decoration: none; font-size: 0.9rem; font-weight: 600; white-space: nowrap; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr)); gap: 1rem; }
    .card {
      display: flex;
      flex-direction: column;
      background: #fff;
      border: 1px solid #d0d7de;
      border-radius: 14px;
      padding: 1.1rem 1.25rem 0.9rem;
      text-decoration: none;
      color: inherit;
      transition: border-color 0.15s, box-shadow 0.15s;
      min-height: 12rem;
    }
    .card:hover, .card:focus-visible { border-color: #0969da; box-shadow: 0 4px 14px rgba(9, 105, 218, 0.15); }
    .top { display: flex; align-items: center; gap: 0.65rem; }
    .badge { width: 2.25rem; height: 2.25rem; border-radius: 999px; background: #0969da; color: #fff; display: inline-flex; align-items: center; justify-content: center; font-weight: 700; font-size: 1rem; flex-shrink: 0; }
    .name { font-size: 1.15rem; font-weight: 700; margin: 0; }
    .id { color: #57606a; font-size: 0.8rem; margin: 0.15rem 0 0; font-family: ui-monospace, monospace; }
    .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin: 1rem 0 0.9rem; }
    .stat dt { font-size: 0.75rem; color: #57606a; margin: 0 0 0.15rem; }
    .stat dd { margin: 0; font-weight: 600; font-size: 0.9rem; overflow-wrap: anywhere; }
    .stat.gateway { grid-column: 1 / -1; }
    .reported { margin: -0.35rem 0 0.6rem; font-size: 0.75rem; color: #57606a; }
    .go { margin-top: auto; text-align: center; border: 1px solid #0969da; border-radius: 10px; padding: 0.45rem 0.9rem; color: #0969da; font-weight: 600; font-size: 0.9rem; }
    .card .go::after { content: " →"; }
    .empty { color: #57606a; }
    .back { display: inline-block; margin-bottom: 1rem; color: #0969da; text-decoration: none; }
    ol { padding-left: 1.4rem; }
    li { margin: 0.75rem 0; line-height: 1.55; }
    code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em; background: #eff1f3; border-radius: 6px; padding: 0.15rem 0.35rem; }
    pre { padding: 0.8rem 1rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
    .note { color: #57606a; border-top: 1px solid #d0d7de; padding-top: 1rem; margin-top: 1.75rem; font-size: 0.92rem; }
"""


def render_acnlist(records: tuple[Network, ...]) -> str:
    cards = "\n".join(_card(network) for network in records)
    if not cards:
        cards = '<p class="empty">No listed networks.</p>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>acnlist</title>
  <style>{_PAGE}</style>
</head>
<body>
  <div class="page">
    <header>
      <div>
        <h1>acnlist</h1>
        <p class="intro">Listed networks. This page introduces a gateway. It does not carry the call.</p>
      </div>
      <a class="cta" href="/start">Start another network</a>
    </header>
    <div class="grid">
      {cards}
    </div>
  </div>
</body>
</html>
"""


def render_start_network() -> str:
    """How a person starts another native ACN. The page does not register it."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Start another network</title>
  <style>{_PAGE}</style>
</head>
<body>
  <div class="page">
    <a class="back" href="/">← acnlist</a>
    <h1>Start another network</h1>
    <p class="intro">Global and CN are already running. These steps start a separate native ACN on a machine you already have. This page does not start it, and it does not add it to the list.</p>
    <ol>
      <li>Copy <code>env.network.example</code> to <code>.env.network</code>. Fill <code>INTERNAL_API_TOKEN</code> and <code>BLOB_SIGNING_SECRET</code>. Each must be at least 32 characters, and they must differ. Set <code>AUTH0_DOMAIN</code> and <code>AUTH0_AUDIENCE</code>, or <code>HUMAN_OIDC_PROVIDERS_JSON</code>. Set <code>GATEWAY_BASE_URL</code> to the public https URL callers will use.</li>
      <li>On that machine, from a checkout of the ACN repo, run:
        <pre>docker compose --env-file .env.network -p acn-one -f docker-compose.network.yml up -d --build</pre>
        A second network on the same machine needs its own <code>-p</code> name and its own <code>ACN_NETWORK_PORT</code>.</li>
      <li>Health on that machine is <code>http://127.0.0.1:9100/health</code> unless you changed the port.</li>
      <li>Give the directory operator the name, the network id, and the public https gateway. This page cannot register it. <code>http://127.0.0.1</code> cannot be registered.</li>
    </ol>
    <p class="note">An agent with a shell on that machine follows the same steps and gives the operator the same three facts. The directory key is not on this page.</p>
  </div>
</body>
</html>
"""


def _ago(reported_at: str) -> str:
    """How old a self-report is. An unparseable timestamp renders as-is."""
    try:
        moment = datetime.fromisoformat(reported_at)
    except ValueError:
        return reported_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _card(network: Network) -> str:
    gateway = escape(network.gateway_url, quote=True)
    initial = escape(network.name.strip()[:1].upper() or "A")
    seq = str(network.seq) if network.seq > 0 else "—"
    stats = (
        '<div class="stat"><dt>Network ID</dt>'
        + f"<dd>{seq}</dd></div>"
        + '<div class="stat"><dt>Framework</dt>'
        + f"<dd>{escape(network.framework)}</dd></div>"
    )
    reported = ""
    if network.reported_at is not None:
        agents = str(network.agents) if network.agents is not None else "—"
        subnets = str(network.subnets) if network.subnets is not None else "—"
        stats += (
            '<div class="stat"><dt>Agents</dt>'
            + f"<dd>{agents}</dd></div>"
            + '<div class="stat"><dt>Subnets</dt>'
            + f"<dd>{subnets}</dd></div>"
        )
        reported = f'<p class="reported">self-reported {escape(_ago(network.reported_at))}</p>'
    body = (
        '<div class="top">'
        + f'<span class="badge">{initial}</span>'
        + '<div>'
        + f'<p class="name">{escape(network.name)}</p>'
        + f'<p class="id">{escape(network.network_id)}</p>'
        + "</div></div>"
        + '<dl class="stats">'
        + stats
        + '<div class="stat gateway"><dt>Gateway</dt>'
        + f"<dd>{gateway}</dd></div>"
        + "</dl>"
        + reported
    )
    if gateway.startswith("https://"):
        return (
            f'<a class="card" href="{gateway}">'
            + body
            + '<span class="go">Open gateway</span>'
            + "</a>"
        )
    return '<div class="card">' + body + "</div>"
