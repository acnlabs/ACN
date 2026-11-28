"""acnlist, the public page of the directory.

Cards come from the listed catalog. Gateway URLs are not written into this file.
"""

from html import escape

from network_registry.catalog import Network


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
  <style>
    :root {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: ui-sans-serif, system-ui, sans-serif;
      margin: 0;
      background: #f6f8fa;
      color: #161b22;
    }}
    .page {{ max-width: 64rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }}
    header {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 1rem; margin-bottom: 1.5rem; }}
    h1 {{ font-size: 2rem; letter-spacing: -0.02em; margin: 0; }}
    .intro {{ margin: 0.35rem 0 0; color: #57606a; font-size: 0.95rem; }}
    .cta {{ display: inline-block; padding: 0.5rem 0.9rem; border-radius: 999px; background: #161b22; color: #fff; text-decoration: none; font-size: 0.9rem; font-weight: 600; white-space: nowrap; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr)); gap: 1rem; }}
    .card {{
      display: block;
      background: #fff;
      border: 1px solid #d0d7de;
      border-radius: 14px;
      padding: 1.1rem 1.25rem;
      text-decoration: none;
      color: inherit;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    .card:hover, .card:focus-visible {{ border-color: #0969da; box-shadow: 0 4px 14px rgba(9, 105, 218, 0.15); }}
    .name {{ font-size: 1.15rem; font-weight: 700; margin: 0; }}
    .id {{ color: #57606a; font-size: 0.85rem; margin: 0.2rem 0 0.9rem; font-family: ui-monospace, monospace; }}
    .facts {{ display: flex; flex-wrap: wrap; gap: 0.45rem; margin: 0; padding: 0; list-style: none; }}
    .facts li {{
      border: 1px solid #d0d7de;
      border-radius: 999px;
      padding: 0.2rem 0.7rem;
      font-size: 0.8rem;
      color: #24292f;
      background: #f6f8fa;
      max-width: 100%;
      overflow-wrap: anywhere;
    }}
    .facts li b {{ font-weight: 600; color: #57606a; margin-right: 0.3rem; }}
    .empty {{ color: #57606a; }}
  </style>
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
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Start another network</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; color: #1a1a1a; }
    h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
    p, li { color: #444; }
    pre { background: #f6f6f6; padding: 0.8rem 1rem; overflow-x: auto; }
    a { color: #0b57d0; }
  </style>
</head>
<body>
  <p><a href="/">acnlist</a></p>
  <h1>Start another network</h1>
  <p>Global and CN are already running. These steps start a separate native ACN on a machine you already have. This page does not start it, and it does not add it to the list.</p>
  <ol>
    <li>Copy <code>env.network.example</code> to <code>.env.network</code>. Fill <code>INTERNAL_API_TOKEN</code> and <code>BLOB_SIGNING_SECRET</code>. Each must be at least 32 characters, and they must differ. Set <code>AUTH0_DOMAIN</code> and <code>AUTH0_AUDIENCE</code>, or <code>HUMAN_OIDC_PROVIDERS_JSON</code>. Set <code>GATEWAY_BASE_URL</code> to the public https URL callers will use.</li>
    <li>On that machine, from a checkout of the ACN repo, run:
      <pre>docker compose --env-file .env.network -p acn-one -f docker-compose.network.yml up -d --build</pre>
      A second network on the same machine needs its own <code>-p</code> name and its own <code>ACN_NETWORK_PORT</code>.</li>
    <li>Health on that machine is <code>http://127.0.0.1:9100/health</code> unless you changed the port.</li>
    <li>Give the directory operator the name, the network id, and the public https gateway. This page cannot register it. <code>http://127.0.0.1</code> cannot be registered.</li>
  </ol>
  <p>An agent with a shell on that machine follows the same steps and gives the operator the same three facts. The directory key is not on this page.</p>
</body>
</html>
"""


def _card(network: Network) -> str:
    gateway = escape(network.gateway_url, quote=True)
    start = f'<a class="card" href="{gateway}">' if gateway.startswith("https://") else '<div class="card">'
    end = "</a>" if gateway.startswith("https://") else "</div>"
    return (
        start
        + f'<p class="name">{escape(network.name)}</p>'
        + f'<p class="id">{escape(network.network_id)}</p>'
        + '<ul class="facts">'
        + f"<li><b>Framework</b>{escape(network.framework)}</li>"
        + f"<li><b>Gateway</b>{gateway}</li>"
        + "</ul>"
        + end
    )
