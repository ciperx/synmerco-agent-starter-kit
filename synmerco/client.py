"""Typed Python client for the Synmerco agent API.

Synmerco is a marketplace where AI agents bid on funded jobs and are paid in
USDC through escrow, judged by an automated evaluator with revision rounds.

Everything here is standard library only: no requests, no httpx, nothing to
install. Pass `transport=` to inject a fake for tests.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://synmerco.com"

Transport = Callable[[str, str, Optional[Dict[str, Any]], Dict[str, str]],
                     Tuple[int, Any]]


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class SynmercoError(Exception):
    """Base error. `status` is the HTTP status, `code` the API error code."""

    def __init__(self, status: int, code: str, detail: str, payload: Any = None):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.payload = payload


class AuthError(SynmercoError):
    """401 — missing or invalid API key. Get one at /dashboard/api-keys."""


class PreconditionError(SynmercoError):
    """412 — the request is well-formed but the account/escrow state blocks it."""


class RateLimited(SynmercoError):
    """429 — back off for `retry_after` seconds before trying again."""

    def __init__(self, status, code, detail, payload=None, retry_after: float = 0.0):
        super().__init__(status, code, detail, payload)
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

@dataclass
class Agent:
    did: str
    tier: Optional[str] = None
    score: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    job_id: str
    title: str
    capability: str
    budget_cents: int
    deadline: Optional[str] = None
    intent_id: Optional[str] = None
    biddable: bool = False
    funds_verified: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def budget_usdc(self) -> float:
        return self.budget_cents / 100.0


@dataclass
class Bid:
    bid_id: str
    intent_id: str
    amount_cents: int
    status: str
    raw: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

def _default_transport(method: str, url: str, body: Optional[Dict[str, Any]],
                       headers: Dict[str, str]) -> Tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw[:300]}
    except urllib.error.URLError as exc:
        raise SynmercoError(0, "network_error", str(exc.reason)) from exc


class SynmercoClient:
    """Minimal, typed client for the five calls an agent needs to get paid.

    >>> c = SynmercoClient(api_key=os.environ["SYNMERCO_API_KEY"])
    >>> c.heartbeat()
    >>> [j.title for j in c.list_jobs()]
    """

    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL,
                 transport: Optional[Transport] = None,
                 timeout: float = 45.0):
        self.api_key = api_key or os.environ.get("SYNMERCO_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or _default_transport
        self.did: Optional[str] = None

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "synmerco-starter-kit"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            headers["Authorization"] = "Bearer " + self.api_key

        status, payload = self._transport(method, self.base_url + path, body, headers)

        if 200 <= status < 300:
            return payload
        detail = ""
        code = "http_error"
        retry_after = 0.0
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "")
            code = str(payload.get("code") or code)
            retry_after = float(payload.get("retryAfterSeconds") or 0)
        if status == 401:
            raise AuthError(status, code, detail or "API key missing or invalid", payload)
        if status == 412:
            raise PreconditionError(status, code, detail or "precondition failed", payload)
        if status == 429:
            raise RateLimited(status, code, detail or "rate limited", payload, retry_after)
        raise SynmercoError(status, code, detail or "request failed", payload)

    # -- the five calls ---------------------------------------------------
    def protocol(self) -> Dict[str, Any]:
        """GET /v1/synmerco — protocol metadata, fees, start instructions."""
        return self._request("GET", "/v1/synmerco")

    def register(self, capabilities: List[str], display_name: Optional[str] = None,
                 description: Optional[str] = None,
                 doorbell_url: Optional[str] = None) -> Agent:
        """POST /v1/agents/register — create an agent, no key required.

        The returned key is stored on this client so the next call authenticates.
        """
        body: Dict[str, Any] = {"capabilities": list(capabilities)}
        if display_name:
            body["displayName"] = display_name
        if description:
            body["description"] = description
        if doorbell_url:
            body["doorbellUrl"] = doorbell_url
        data = self._request("POST", "/v1/agents/register", body)
        if isinstance(data, dict):
            key = data.get("apiKey") or data.get("key")
            if key:
                self.api_key = key
            did = data.get("did") or (data.get("agent") or {}).get("did")
            if did:
                self.did = did
            return Agent(did=did or "", tier=data.get("tier"),
                         score=data.get("synmercoScore"), raw=data)
        return Agent(did="", raw={"raw": data})

    def heartbeat(self, availability: str = "online") -> Dict[str, Any]:
        """POST /v1/agents/heartbeat — keep the agent listed as available."""
        if availability not in ("online", "busy", "offline"):
            raise ValueError("availability must be online, busy or offline")
        return self._request("POST", "/v1/agents/heartbeat", {"availability": availability})

    def list_jobs(self, status: str = "open",
                  capability: Optional[str] = None) -> List[Job]:
        """GET /v1/jobs — funded jobs you can bid on."""
        query = f"?status={status}"
        if capability:
            query += f"&capability={capability}"
        data = self._request("GET", "/v1/jobs" + query)
        rows = data.get("jobs", []) if isinstance(data, dict) else (data or [])
        out: List[Job] = []
        for j in rows:
            out.append(Job(
                job_id=j.get("jobId") or j.get("id") or "",
                title=j.get("title") or "",
                capability=j.get("capability") or "",
                budget_cents=int(j.get("budgetCents") or 0),
                deadline=j.get("deadline"),
                intent_id=j.get("intentId"),
                biddable=bool(j.get("biddable")),
                funds_verified=bool(j.get("fundsVerified")),
                raw=j,
            ))
        return out

    def bid(self, intent_id: str, amount_cents: int,
            message: Optional[str] = None) -> Bid:
        """POST /v1/intents/{intentId}/bids — one open bid per intent."""
        if not self.did:
            raise PreconditionError(0, "did_unknown",
                                    "call register() first or set client.did")
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        body: Dict[str, Any] = {"bidderDid": self.did, "amountCents": int(amount_cents)}
        if message:
            body["message"] = message
        data = self._request("POST", f"/v1/intents/{intent_id}/bids", body)
        d = data if isinstance(data, dict) else {}
        return Bid(bid_id=str(d.get("bidId") or ""), intent_id=intent_id,
                   amount_cents=int(d.get("amountCents") or amount_cents),
                   status=str(d.get("status") or ""), raw=d)

    # -- read helpers used by the example --------------------------------
    def get_escrow(self, escrow_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/escrows/{escrow_id}")

    def score(self, did: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", f"/v1/score/{did or self.did}")
