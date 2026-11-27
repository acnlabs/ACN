"""The networks this directory knows about.

The first records are the two hosted ACN networks. Their network ids are
assigned here and must stay unique. This module does not register a new
network and does not start one.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Network:
    name: str
    network_id: str
    framework: str
    gateway_url: str
    listed: bool


# Stable ids for the two hosted networks. A later register call rejects a
# collision with either of these. `public` and `system` are subnet slugs
# inside a network, not network ids.
NETWORKS: tuple[Network, ...] = (
    Network(
        name="ACN Global",
        network_id="acn-global",
        framework="acn",
        gateway_url="https://api.acnlabs.dev",
        listed=True,
    ),
    Network(
        name="ACN CN",
        network_id="acn-cn",
        framework="acn",
        gateway_url="https://acn.acnlabs.cn",
        listed=True,
    ),
)


def _require_unique_ids(records: tuple[Network, ...]) -> None:
    ids = [record.network_id for record in records]
    if len(ids) != len(set(ids)):
        raise RuntimeError("network id collision in the registry catalog")


_require_unique_ids(NETWORKS)


def listed_networks(records: tuple[Network, ...] = NETWORKS) -> tuple[Network, ...]:
    """Networks a public directory shows. Unlisted records stay out."""
    return tuple(record for record in records if record.listed)


def get_network(network_id: str, records: tuple[Network, ...] = NETWORKS) -> Network | None:
    """One record by id, listed or not. Unknown ids are absent."""
    for record in records:
        if record.network_id == network_id:
            return record
    return None
