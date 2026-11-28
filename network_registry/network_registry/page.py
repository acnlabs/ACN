"""acnlist, the public page of the directory.

Rows come from the listed catalog. Gateway URLs are not written into this file.
"""

from html import escape

from network_registry.catalog import Network


def render_acnlist(records: tuple[Network, ...]) -> str:
    rows = "\n".join(_row(network) for network in records)
    if not rows:
        rows = '<tr><td colspan="4">No listed networks.</td></tr>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>acnlist</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; color: #1a1a1a; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
    p {{ color: #444; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 0.55rem 0.4rem; border-bottom: 1px solid #ddd; }}
    th {{ font-size: 0.8rem; letter-spacing: 0.02em; color: #555; }}
    a {{ color: #0b57d0; }}
  </style>
</head>
<body>
  <h1>acnlist</h1>
  <p>Listed networks. This page introduces a gateway. It does not carry the call.</p>
  <p><a href="/start">Start another network</a></p>
  <table>
    <thead>
      <tr><th>Name</th><th>Network id</th><th>Framework</th><th>Gateway</th></tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
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


def _row(network: Network) -> str:
    gateway = escape(network.gateway_url, quote=True)
    href = f'<a href="{gateway}">{gateway}</a>' if gateway.startswith("https://") else gateway
    return (
        "<tr>"
        f"<td>{escape(network.name)}</td>"
        f"<td>{escape(network.network_id)}</td>"
        f"<td>{escape(network.framework)}</td>"
        f"<td>{href}</td>"
        "</tr>"
    )
