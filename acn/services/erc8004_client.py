"""
ERC-8004 On-Chain Client (read-only)

ACN server-side client for interacting with ERC-8004 Identity and Reputation
Registries. All methods are read-only — ACN never signs or broadcasts transactions.

ABIs sourced from: https://github.com/erc-8004/erc-8004-contracts/tree/master/abis
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

logger = logging.getLogger(__name__)

_ABI_DIR = Path(__file__).parent / "abis"


def _load_abi(filename: str) -> list:
    with (_ABI_DIR / filename).open() as f:
        return json.load(f)


IDENTITY_ABI = _load_abi("IdentityRegistry.json")
REPUTATION_ABI = _load_abi("ReputationRegistry.json")
VALIDATION_ABI = _load_abi("ValidationRegistry.json")


class ERC8004Client:
    """Read-only client for ERC-8004 Identity, Reputation, and Validation Registries."""

    def __init__(
        self,
        rpc_url: str,
        identity_contract: str,
        reputation_contract: str,
        validation_contract: str | None = None,
    ) -> None:
        self._w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self._identity = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(identity_contract),
            abi=IDENTITY_ABI,
        )
        self._reputation = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(reputation_contract),
            abi=REPUTATION_ABI,
        )
        self._validation = (
            self._w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(validation_contract),
                abi=VALIDATION_ABI,
            )
            if validation_contract
            else None
        )

    # -------------------------------------------------------------------------
    # Identity Registry
    # -------------------------------------------------------------------------

    async def verify_registration(
        self, token_id: int, expected_registration_url: str
    ) -> bool:
        """Return True if the on-chain tokenURI matches expected_registration_url.

        Used by /bind to confirm that the agent owns the NFT whose agentURI
        points back to ACN's agent-registration.json endpoint.
        """
        try:
            on_chain_uri: str = await self._identity.functions.tokenURI(token_id).call()
            return on_chain_uri.strip() == expected_registration_url.strip()
        except Exception as exc:
            logger.warning("verify_registration failed for token_id=%s: %s", token_id, exc)
            return False

    async def get_agent_wallet(self, token_id: int) -> str | None:
        """Return the on-chain agentWallet address for the given token_id.

        Returns None if the token does not exist or agentWallet is unset
        (zero address).
        """
        try:
            address: str = await self._identity.functions.getAgentWallet(token_id).call()
            zero = "0x" + "0" * 40
            return address if address and address.lower() != zero else None
        except Exception as exc:
            logger.warning("get_agent_wallet failed for token_id=%s: %s", token_id, exc)
            return None

    async def query_agent(self, token_id: int) -> dict | None:
        """Return basic on-chain info for an agent: owner, agentURI, agentWallet."""
        try:
            owner: str = await self._identity.functions.ownerOf(token_id).call()
            uri: str = await self._identity.functions.tokenURI(token_id).call()
            wallet = await self.get_agent_wallet(token_id)
            return {
                "token_id": token_id,
                "owner": owner,
                "agent_uri": uri,
                "agent_wallet": wallet,
            }
        except Exception as exc:
            logger.warning("query_agent failed for token_id=%s: %s", token_id, exc)
            return None

    async def discover_agents(self, limit: int = 50) -> list[dict]:
        """Discover registered agents from the ERC-8004 Identity Registry.

        Strategy (mirrors automaton's two-phase approach):
        1. Primary: call totalSupply() and iterate from newest token backward.
           This is O(n) eth_call reads — no event scanning, no block range limits.
        2. Fallback: if totalSupply() reverts or returns 0, scan recent Transfer
           mint events using getLogs() in batches of 2000 blocks (compatible with
           public RPCs such as mainnet.base.org).  Scans backward from the current
           block, up to MAX_FALLBACK_BATCHES batches.

        Results should be cached by the caller (Redis, TTL 5 min).
        """
        # ── Phase 1: totalSupply enumeration ─────────────────────────────────
        try:
            total: int = await self._identity.functions.totalSupply().call()
        except Exception:
            total = 0

        if total > 0:
            agents: list[dict] = []
            scan_count = min(total, limit)
            for i in range(total, total - scan_count, -1):
                if i <= 0:
                    break
                info = await self.query_agent(i)
                if info:
                    agents.append(info)
            return agents

        # ── Phase 2: getLogs fallback (recent blocks only) ───────────────────
        BATCH_SIZE = 2_000          # safe for public RPCs
        MAX_FALLBACK_BATCHES = 5    # covers ~2.7 hours of Base blocks (2 s/block)

        try:
            current_block: int = await self._w3.eth.block_number
        except Exception as exc:
            logger.warning("discover_agents: cannot get block number: %s", exc)
            return []

        found: list[dict] = []
        to_block = current_block

        for _ in range(MAX_FALLBACK_BATCHES):
            if len(found) >= limit:
                break
            from_block = max(0, to_block - BATCH_SIZE + 1)
            try:
                logs = await self._w3.eth.get_logs(
                    {
                        "address": self._identity.address,
                        "fromBlock": from_block,
                        "toBlock": to_block,
                        "topics": [
                            self._w3.keccak(
                                text="Transfer(address,address,uint256)"
                            ).hex(),
                            "0x" + "0" * 64,  # from = zero address (mint)
                        ],
                    }
                )
            except Exception as exc:
                logger.warning("discover_agents getLogs batch failed: %s", exc)
                break

            for log in reversed(logs):
                if len(found) >= limit:
                    break
                try:
                    # Transfer(from, to, tokenId): topics[0]=sig, [1]=from, [2]=to, [3]=tokenId
                    token_id = int(log["topics"][3].hex(), 16)
                    info = await self.query_agent(token_id)
                    if info:
                        found.append(info)
                except Exception:
                    continue

            if from_block == 0:
                break
            to_block = from_block - 1

        return found

    # -------------------------------------------------------------------------
    # Reputation Registry
    # -------------------------------------------------------------------------

    async def get_reputation(self, token_id: int) -> list[dict]:
        """Fetch all feedback for an agent using readAllFeedback.

        Passes empty clientAddresses to get all feedback without filtering
        (valid call; empty array ≠ missing array per the ERC-8004 spec).
        Revoked feedback is excluded by default.
        """
        try:
            result = await self._reputation.functions.readAllFeedback(
                token_id,
                [],   # no client filter
                "",   # no tag1 filter
                "",   # no tag2 filter
                False,  # exclude revoked
            ).call()
            clients, indexes, values, decimals, tag1s, tag2s, revoked = result
            feedback = []
            for i in range(len(clients)):
                feedback.append(
                    {
                        "client_address": clients[i],
                        "feedback_index": indexes[i],
                        "value": values[i],
                        "value_decimals": decimals[i],
                        "tag1": tag1s[i],
                        "tag2": tag2s[i],
                        "is_revoked": revoked[i],
                    }
                )
            return feedback
        except Exception as exc:
            logger.warning("get_reputation failed for token_id=%s: %s", token_id, exc)
            return []

    async def get_reputation_summary(self, token_id: int) -> dict:
        """Aggregate reputation signals for an agent.

        getSummary() requires non-empty clientAddresses (anti-Sybil), which the
        ACN server cannot provide. Instead, we call readAllFeedback and aggregate
        at the application layer.
        """
        feedback = await self.get_reputation(token_id)
        if not feedback:
            return {"token_id": token_id, "count": 0, "avg_value": None, "by_tag": {}}

        total = 0.0
        by_tag: dict[str, list[float]] = {}
        for item in feedback:
            normalized = item["value"] / (10 ** item["value_decimals"])
            total += normalized
            tag = item["tag1"] or "untagged"
            by_tag.setdefault(tag, []).append(normalized)

        avg_by_tag = {tag: sum(vals) / len(vals) for tag, vals in by_tag.items()}
        return {
            "token_id": token_id,
            "count": len(feedback),
            "avg_value": total / len(feedback),
            "by_tag": avg_by_tag,
        }

    # -------------------------------------------------------------------------
    # Validation Registry (optional — requires erc8004_validation_contract)
    # -------------------------------------------------------------------------

    @property
    def validation_available(self) -> bool:
        """True if a Validation Registry contract address has been configured."""
        return self._validation is not None

    async def get_agent_validations(self, token_id: int) -> list[str]:
        """Return all requestHash values linked to an agent.

        Each hash can be resolved via get_validation_status() for full details.
        Returns empty list if Validation Registry is not configured.
        """
        if self._validation is None:
            return []
        try:
            hashes: list[bytes] = await self._validation.functions.getAgentValidations(
                token_id
            ).call()
            return [h.hex() for h in hashes]
        except Exception as exc:
            logger.warning("get_agent_validations failed for token_id=%s: %s", token_id, exc)
            return []

    async def get_validation_status(self, request_hash: str) -> dict | None:
        """Return the full validation record for a given requestHash.

        response codes (uint8): 0=pending, 1=approved, 2=rejected
        Returns None if Validation Registry is not configured or hash not found.
        """
        if self._validation is None:
            return None
        try:
            raw_hash = bytes.fromhex(request_hash.removeprefix("0x"))
            result = await self._validation.functions.getValidationStatus(raw_hash).call()
            validator_address, agent_id, response, response_hash, tag, last_update = result
            response_labels = {0: "pending", 1: "approved", 2: "rejected"}
            return {
                "request_hash": request_hash,
                "validator_address": validator_address,
                "agent_id": agent_id,
                "response": response_labels.get(response, str(response)),
                "response_hash": response_hash.hex(),
                "tag": tag,
                "last_update": last_update,
            }
        except Exception as exc:
            logger.warning("get_validation_status failed for hash=%s: %s", request_hash, exc)
            return None

    async def get_validation_summary(self, token_id: int) -> dict:
        """Return a summary of all validation records for an agent.

        Fetches each requestHash and resolves its status, grouping by tag.
        Returns structured summary: total, by_tag breakdown, pending/approved/rejected counts.
        """
        hashes = await self.get_agent_validations(token_id)
        if not hashes:
            return {
                "token_id": token_id,
                "available": self.validation_available,
                "total": 0,
                "approved": 0,
                "rejected": 0,
                "pending": 0,
                "by_tag": {},
            }

        counts: dict[str, int] = {"approved": 0, "rejected": 0, "pending": 0}
        by_tag: dict[str, dict[str, int]] = {}

        for h in hashes:
            record = await self.get_validation_status(h)
            if not record:
                continue
            status = record["response"]
            counts[status] = counts.get(status, 0) + 1
            tag = record["tag"] or "untagged"
            if tag not in by_tag:
                by_tag[tag] = {"approved": 0, "rejected": 0, "pending": 0}
            by_tag[tag][status] = by_tag[tag].get(status, 0) + 1

        return {
            "token_id": token_id,
            "available": True,
            "total": len(hashes),
            **counts,
            "by_tag": by_tag,
        }
