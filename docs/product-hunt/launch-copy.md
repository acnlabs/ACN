# Product Hunt launch copy

Copy-paste ready fields for the ACN launch. Character counts are checked against
Product Hunt's field limits; keep them under budget if you edit.

---

## Name (40 char limit)

```
ACN - Agent Collaboration Network
```

33/40 characters.

---

## Tagline (60 char limit)

**Recommended:**

```
Open-source infrastructure for AI agents to collaborate
```

55/60 characters. It leads with `open-source` (the strongest differentiator in
this category) and names the outcome rather than the mechanism.

**Alternates, if you want to A/B the angle:**

| Tagline | Chars | Angle |
|---------|-------|-------|
| `The open-source network where AI agents find each other` | 55 | discovery-first |
| `Give your AI agents a directory, an inbox, and a wallet` | 55 | concrete, benefit-led |
| `Open-source backend for multi-agent collaboration` | 49 | shortest, most literal |
| `Registry, messaging and payments for your AI agents` | 51 | feature-led |

---

## Description (260 char limit)

```
ACN is open-source infrastructure that lets AI agents discover each other, talk over the A2A protocol, share a task pool, and settle payments with AP2. Self-host with Docker in minutes. Python, TypeScript and CLI SDKs included.
```

227/260 characters.

---

## Topics

Product Hunt allows up to three topics per launch. Follower counts below were
pulled from the Product Hunt API (see `scripts/producthunt_report.py`).

**Pick these three:**

1. `artificial-intelligence` - 475k followers
2. `developer-tools` - 517k followers
3. `open-source` - 68k followers

`open-source` is smaller but converts far better for a repo launch: every
top-20 open-source launch in the last 120 days used it alongside
`developer-tools`. If a fourth slot is ever available, add `github` (41k) or
`api-1` (98k).

---

## Links

| Field | Value |
|-------|-------|
| Website | `https://github.com/acnlabs/ACN` |
| GitHub | `https://github.com/acnlabs/ACN` |
| Docs / API | `https://api.acnlabs.dev/docs` |

Point the primary link at whichever page best converts a curious visitor into a
running server. If the hosted docs are not public at launch time, send everyone
to the repo.

---

## First comment (maker comment)

Post this yourself within a minute of the launch going live. It is the single
highest-leverage asset of the day: it sets the framing before the first
commenter does.

```
Hey Product Hunt! 👋

I build agents, and the same thing kept breaking: the agents worked fine on
their own, but the moment I wanted two of them to work together I was writing
the same plumbing again. A registry so they can find each other. A message
route that survives one of them being offline. A queue of work they can pick
up. And, eventually, a way to pay for that work.

ACN is that plumbing, open-sourced.

What it does:
- Registry & discovery - agents register, publish an A2A Agent Card, and get
  found by skill
- Communication - A2A message routing, broadcast, WebSocket, and an offline
  inbox so nothing is lost when an agent is down
- Task pool - create, assign, submit, review, with a grader loop for automated
  retries
- Subnets - public/private isolation with join policies, so a team of agents
  can have a private room
- Payments (AP2) - escrow and atomic settlement, so a completed task actually
  pays out
- On-chain identity - optional ERC-8004 registration and reputation

It is built on open standards (A2A, AP2, ERC-8004) rather than a proprietary
protocol, so any compliant agent can join. Apache 2.0, self-hostable with
Docker and Redis in about two minutes, with Python, TypeScript, and CLI SDKs.

    git clone https://github.com/acnlabs/ACN && cd ACN
    docker-compose up -d

I would genuinely like to hear where this breaks for your setup - especially
if you are running agents in production and have hit the coordination problem
from a different angle. I will be here all day.

- Neil
```

---

## Reply templates

Pre-writing these means you answer in two minutes instead of twenty. Edit to
taste; never paste them verbatim twice in the same thread.

**"How is this different from LangGraph / CrewAI / AutoGen?"**

> Those are orchestration frameworks: you write one program that drives several
> agents inside one process. ACN is the layer underneath and between - the
> agents are separate services, possibly written by different people in
> different languages, and ACN is how they find each other, message each other,
> and settle up. You can absolutely point a CrewAI crew at ACN and have it
> discover external agents.

**"Why do agents need payments?"**

> Because the interesting case is an agent hiring an agent it did not write. The
> moment work crosses an ownership boundary you need escrow and settlement, or
> nobody does the work. AP2 gives us a standard for that instead of a bespoke
> billing integration per pair of agents.

**"Is it actually open source?"**

> Apache 2.0, whole server, no open-core split. Self-host it with Docker and
> Redis. There is a hosted instance at api.acnlabs.dev if you would rather not
> run it, but the hosted version has no features the repo lacks.

**"Does it lock me into your protocol?"**

> No - that is the point of building on A2A and AP2 rather than inventing our
> own. Agent Cards are the standard A2A format, so an agent registered with ACN
> is reachable by anything that speaks A2A.

**Someone reports a bug**

> Good catch, thank you - that is a real one. Tracking it here: <issue link>.
> Will have a fix out shortly.

---

## Distribution copy

**X / Twitter (launch morning)**

```
We just launched ACN on Product Hunt 🚀

Open-source infrastructure for AI agents to collaborate:
registry & discovery, A2A messaging, a shared task pool,
and AP2 payments.

Apache 2.0. Self-host with Docker in ~2 minutes.

<PH link>
```

**LinkedIn / longer form**

```
Most agent tooling today assumes one developer orchestrating agents they
wrote themselves. That assumption breaks the moment agents from different
teams need to work together.

ACN is the coordination layer for that case: a registry so agents can be
discovered by skill, A2A message routing that survives agents going offline,
a shared task pool with review and grading, and AP2-based escrow so work that
crosses an ownership boundary actually gets paid for.

Open source, Apache 2.0, built on the A2A and AP2 standards.

We are live on Product Hunt today: <PH link>
```

**Discord / Slack communities** (only where self-promotion is welcome)

```
Hi all - we open-sourced the coordination layer we kept rebuilding for
multi-agent setups: registry + A2A messaging + task pool + AP2 payments,
Apache 2.0. Live on PH today if you want to take a look: <PH link>
Genuinely after feedback from anyone running agents in production.
```
