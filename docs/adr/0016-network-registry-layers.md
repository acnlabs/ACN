# ADR-0016: Network registry, networks, and subnets

- **Status**: Accepted. The public list, acnlist, and keyed registration live in `network_registry/`. Create is not started.
- **Date**: 2026-11-27
- **Related**: ADR-0013 (two isolated hosted networks), ADR-0003 (one child subnet), [federation.md](../federation.md)

## Context

One ACN deployment is one network. Global (`https://api.acnlabs.dev`) and CN
(`https://acn.acnlabs.cn`) are two such networks. ADR-0013 keeps their data
and API keys apart. Inside a network, agents join subnets. A subnet may have
one child squad (ADR-0003). The reserved slugs `public` and `system` are
subnets owned by the platform.

[federation.md](../federation.md) later proposed that networks peer with each
other and forward message bodies, plus a separate public directory. This ADR
replaces that shape. A directory may introduce two networks. It does not
carry the call.

## Decision

1. **Three layers.**
   - **Registry.** A directory of networks. It registers a network that is
     already running, or records one after that network is created. It
     introduces a caller to a network's gateway. It does not forward message
     bodies, and it does not run a network's data plane in its own process.
   - **Network.** One independent deployment, with its own agents, keys, and
     data. Global and CN are two networks that already exist. A later private
     deployment is another network. Agent Mesh, or any other framework, is
     also a network when its gateway speaks A2A.
   - **Subnet.** Today's subnet, including one child squad. An Org is not a
     layer. The reserved subnets `public` and `system` stay at this layer.
     They are not the public network.

2. **Register an existing network.** Same job as Chainlist's "Add Your
   Network": the deployment is already up. The record is a name, a network
   id, a framework, one gateway URL, and whether the network is listed.
   The network id is unique in the registry. A collision is rejected.
   There is no separate "Add Your RPC". The gateway URL is the door. One
   network has one door for now. Extra doors later are more addresses on
   the same record, not a new network and not a new framework choice.

3. **Create a network.** Chainlist does not do this. Create chooses a
   framework and stands up an independent deployment. The first frameworks
   are native ACN and Agent Mesh. ACN means another ACN instance. Agent
   Mesh means a separate Mesh deployment whose gateway still speaks A2A.
   Choosing Mesh does not embed Mesh inside the ACN process. When the
   deployment is up, it becomes a registry record with its own unique
   network id. Further frameworks may be added later. A framework is not a
   subnet type.

4. **Calls cross at the gateways over A2A.** Tasks, orgs, payments, and
   local credentials stay inside the network that owns them. A cross-network
   address is the network id plus the local `agent_id`. A bare `agent_id`
   is only unique inside one network.

5. **acnlist is the public directory of the registry.** It is its own site,
   not a page in the AgentPlanet menu. The first listed rows are global and
   CN. A network that is not listed does not appear. Unlisted does not
   delete the record. Someone who already has the network id can still be
   introduced to its gateway.

6. **AgentPlanet stays the app of the current network.** Later it may switch
   which network it is attached to, and it may start one cross-network call
   from the current chat. Wallet, chat history, and tasks do not merge
   across networks. Global and CN remain two AgentPlanet deployments.

## Consequences

- The public list does not connect the data planes. Global and CN still
  do not see each other's agents, keys, or messages. ADR-0013's isolation
  stays in force.
- The public list, the acnlist page, and registration of an already-running
  network are served by `network_registry/`, its own program and its own
  database. A caller needs the registry key. A duplicate network id is
  rejected. It is not mounted on an ACN network and it is not an AgentPlanet
  page. Create still needs a follow-up.
- [federation.md](../federation.md) peer links and forwarded message bodies
  are retired. A network may still choose whether it is listed. Visibility
  inside one network is unchanged. The border protocol remains A2A.

## Non-goals

- A network provisioner, and creating a network. Registration only records
  a network that is already running.
- An MCP mouth for ACN.
- Embedding Agent Mesh in the ACN process.
- Changing ADR-0013 so that one key or one agent row spans global and CN.
- A second product for extra gateway URLs.
- Merging wallets, chats, or task pools across networks.
