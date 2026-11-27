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
