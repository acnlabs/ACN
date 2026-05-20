# ADR-0005: Public SDK Governance — Deprecation Policy, Release Discipline, and Consumer-Compat CI

**Status:** Accepted  
**Date:** 2026-05-20  
**Deciders:** ACN core team  
**Related:** [Issue #73](https://github.com/acnlabs/ACN/issues/73) — root cause analysis of the 2026-05-16 backend 502 incident

---

## Context

`acn-client` (Python) and `acn-client` (TypeScript / npm) are public SDKs published to PyPI and npm. Their stated purpose is to let any external developer or company integrate ACN by running `pip install acn-client` or `npm install acn-client`. First-party consumers (`Agentplanet-backend`, `AgentPlanet` frontend, etc.) use the same packages from the same registries.

### Incident (2026-05-16)

`acn-client` 0.7.0 removed `PaymentTaskStatus` (a server-state enum that should never have been in the SDK). `Agentplanet-backend` had no upper-bound pin on `acn-client`. A routine redeploy triggered a `pip install` that pulled 0.7.0, causing an `ImportError` at startup — containers crashed and the service returned 502 for several hours.

### Root causes

| Layer | Description |
|-------|-------------|
| Immediate | `PaymentTaskStatus` removed without a deprecation cycle |
| Mechanism | Downstream `requirements.txt` lacked an upper-bound pin; no consumer-compat CI blocked the SDK PR |
| Design | Server-protocol state machines were typed as SDK enums — coupling the SDK release cycle to server-side protocol evolution |
| Structural | Public SDK commitment (published to PyPI/npm) combined with internal-package discipline (no deprecation policy, no compat matrix, no release checklist) |

---

## Decision

### Rule 1 — No server-state enums in the SDK

Any field whose legal values are governed by server protocol **must** be typed as `str` (Python) or `string` (TypeScript) in the SDK. A companion constant provides the currently known values for consumer convenience without creating a hard coupling:

```python
# Python — correct pattern
KNOWN_PAYMENT_TASK_STATUSES: tuple[str, ...] = (
    "pending", "confirmed", "completed", "failed", "expired",
)

class PaymentTask(BaseModel):
    status: str   # open string — server may add states without an SDK release
```

```typescript
// TypeScript — correct pattern
export const KNOWN_PAYMENT_TASK_STATUSES = [
  "pending", "confirmed", "completed", "failed", "expired",
] as const;

export interface PaymentTask {
  status: string;   // open string
}
```

**Rationale:** Pinning an enum in the SDK forces a release every time the server adds a status value. If the SDK is not updated in lockstep and a consumer pins `acn-client<next`, they silently miss new statuses; if the server removes a status and the SDK hard-removes the enum member, all consumers that pattern-match on the old member get an `AttributeError` / `TypeError` at runtime.

### Rule 2 — Deprecation before removal (breaking-change protocol)

For `0.x` versions:

1. In minor version `N`: mark the symbol deprecated (`DeprecationWarning` in Python, `@deprecated` JSDoc in TypeScript). Do not remove it. Document the migration in `CHANGELOG.md`.
2. In minor version `N+1` (or later): physically remove the symbol.

For `1.0+`: breaking changes may only appear in a new **major** version. Minor and patch releases are fully backward-compatible.

Hard removal in the same release as the deprecation announcement is prohibited.

**Scope of "public symbol":** Any name exported from the package's top-level `__init__.py` / `index.ts`, any parameter of a public function, any field of a public model/interface.

### Rule 3 — Three-place version sync

Every release commit must update all three of the following in a single commit:

| Artifact | Python | TypeScript |
|----------|--------|------------|
| Package metadata | `pyproject.toml` → `version` | `package.json` → `version` |
| Runtime constant | `acn_client/__init__.py` → sourced from `importlib.metadata` (no manual edit needed) | `src/version.ts` → `VERSION` constant (if present) |
| Changelog | `CHANGELOG.md` → move `[Unreleased]` to dated section | same |

CI (`ci.yml`) runs a version-sync check on every PR that touches `clients/`. The check fails if `pyproject.toml` version and the latest `CHANGELOG.md` dated entry do not match.

Note: the Python `__version__` is already sourced via `importlib.metadata.version("acn-client")` — this eliminates the `pyproject.toml` ↔ `__version__` drift class of error. Keep it that way; do not reintroduce a hard-coded string.

### Rule 4 — Public compat matrix

The root-level `README.md` maintains a "Server ↔ SDK Compatibility" table covering:
- the current release of each client
- the immediately prior minor of each client
- the minimum server version required for each SDK minor

Update the table on every release PR. The release PR checklist (`.github/pull_request_template.md`) includes this as a mandatory checkbox.

### Rule 5 — Breaking-change PR checklist

Any PR that removes or renames a public symbol must include in its description:

1. Output from the `consumer-compat` CI job listing affected first-party consumers.
2. Confirmation that affected consumers have been updated, or that a separate PR / issue has been filed.

The `consumer-compat` job (`.github/workflows/ci.yml`, job `consumer-compat`) runs automatically on PRs touching `clients/` and posts a summary comment.

---

## Consequences

### Positive

- Hard removal without deprecation is structurally blocked by the `consumer-compat` CI gate combined with the written policy — any PR that tries to hard-remove a symbol still used by a first-party consumer will fail CI.
- Version drift between `pyproject.toml` and `CHANGELOG.md` is caught before merge.
- External developers have a documented, predictable upgrade path.
- The compat matrix gives downstream consumers a clear upper-bound pin target.

### Negative / Trade-offs

- Release discipline adds a small overhead per release (updating compat matrix, writing a proper CHANGELOG entry).
- The deprecation cycle means breaking changes take at minimum two minor versions to land fully, extending the time-to-clean for design mistakes.
- `consumer-compat` CI requires read access to first-party consumer repos; the job must be maintained as the consumer list evolves.

---

## Compliance

The following items track implementation:

- [x] D-1 — Three-place version sync completed for 0.11.0 / 0.13.0 release
- [x] D-2 — Python `__version__` uses `importlib.metadata`
- [ ] D-3 — AGENTS.md updated with the five rules (this PR)
- [ ] D-4 — This ADR merged (this PR)
- [ ] D-5 — CI version-sync checker added to `ci.yml`
- [ ] B — `consumer-compat` CI job added to `ci.yml`
- [ ] D-6 — First-party consumers pin SDK upper bound + commit lock files

---

## References

- [Issue #73 — SDK governance root cause and action plan](https://github.com/acnlabs/ACN/issues/73)
- [Agentplanet-backend#1 — incident fix](https://github.com/acnlabs/Agentplanet-backend/issues/1)
- Python packaging: [PEP 517](https://peps.python.org/pep-0517/), [`importlib.metadata`](https://docs.python.org/3/library/importlib.metadata.html)
- Keep a Changelog: [keepachangelog.com](https://keepachangelog.com/en/1.0.0/)
