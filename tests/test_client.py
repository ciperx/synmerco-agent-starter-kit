"""Unit tests for the Synmerco client. No network: every call uses a fake transport."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synmerco import (  # noqa: E402
    AuthError,
    PreconditionError,
    RateLimited,
    SynmercoClient,
)


class FakeTransport:
    """Records calls and replays a queued response."""

    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = {} if payload is None else payload
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body,
                           "headers": dict(headers)})
        return self.status, self.payload


class TestProtocol(unittest.TestCase):
    def test_protocol_hits_the_documented_path(self):
        t = FakeTransport(200, {"protocol": "Synmerco Trust Standard v1"})
        c = SynmercoClient(api_key="k", transport=t)
        out = c.protocol()
        self.assertEqual(out["protocol"], "Synmerco Trust Standard v1")
        self.assertEqual(t.calls[0]["method"], "GET")
        self.assertTrue(t.calls[0]["url"].endswith("/v1/synmerco"))


class TestRegister(unittest.TestCase):
    def test_register_stores_key_and_did(self):
        t = FakeTransport(201, {"did": "did:pkh:eip155:8453:0xabc",
                                "apiKey": "fresh-key", "tier": "New"})
        c = SynmercoClient(api_key=None, transport=t)
        agent = c.register(["coding", "research"], display_name="kit demo")
        self.assertEqual(agent.did, "did:pkh:eip155:8453:0xabc")
        self.assertEqual(c.api_key, "fresh-key")
        self.assertEqual(t.calls[0]["body"]["capabilities"], ["coding", "research"])
        self.assertEqual(t.calls[0]["body"]["displayName"], "kit demo")

    def test_register_sends_no_auth_header_without_a_key(self):
        t = FakeTransport(201, {"did": "did:x", "apiKey": "k2"})
        c = SynmercoClient(api_key=None, transport=t)
        c.register(["coding"])
        self.assertNotIn("X-API-Key", t.calls[0]["headers"])


class TestHeartbeat(unittest.TestCase):
    def test_heartbeat_posts_availability(self):
        t = FakeTransport(200, {"ok": True})
        c = SynmercoClient(api_key="k", transport=t)
        c.heartbeat("busy")
        self.assertEqual(t.calls[0]["body"], {"availability": "busy"})
        self.assertEqual(t.calls[0]["headers"]["X-API-Key"], "k")

    def test_heartbeat_rejects_unknown_availability(self):
        c = SynmercoClient(api_key="k", transport=FakeTransport())
        with self.assertRaises(ValueError):
            c.heartbeat("sleeping")


class TestListJobs(unittest.TestCase):
    def test_list_jobs_parses_and_computes_usdc(self):
        t = FakeTransport(200, {"jobs": [
            {"jobId": "job_1", "title": "Write a case study", "capability": "marketing",
             "budgetCents": 500, "biddable": True, "fundsVerified": True,
             "intentId": "intent-1"},
        ], "total": 1})
        c = SynmercoClient(api_key="k", transport=t)
        jobs = c.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "job_1")
        self.assertAlmostEqual(jobs[0].budget_usdc, 5.00)
        self.assertTrue(jobs[0].biddable)
        self.assertIn("status=open", t.calls[0]["url"])

    def test_list_jobs_passes_capability_filter(self):
        t = FakeTransport(200, {"jobs": []})
        c = SynmercoClient(api_key="k", transport=t)
        c.list_jobs(capability="coding")
        self.assertIn("capability=coding", t.calls[0]["url"])


class TestBid(unittest.TestCase):
    def test_bid_posts_bidder_amount_and_message(self):
        t = FakeTransport(201, {"bidId": "217", "amountCents": 450, "status": "open"})
        c = SynmercoClient(api_key="k", transport=t)
        c.did = "did:pkh:eip155:8453:0xabc"
        bid = c.bid("intent-1", 450, message="cited sources")
        self.assertEqual(bid.bid_id, "217")
        self.assertEqual(bid.status, "open")
        self.assertTrue(t.calls[0]["url"].endswith("/v1/intents/intent-1/bids"))
        self.assertEqual(t.calls[0]["body"],
                         {"bidderDid": "did:pkh:eip155:8453:0xabc",
                          "amountCents": 450, "message": "cited sources"})

    def test_bid_without_did_is_a_precondition_error(self):
        c = SynmercoClient(api_key="k", transport=FakeTransport(201, {}))
        with self.assertRaises(PreconditionError):
            c.bid("intent-1", 450)

    def test_bid_rejects_non_positive_amount(self):
        c = SynmercoClient(api_key="k", transport=FakeTransport(201, {}))
        c.did = "did:x"
        with self.assertRaises(ValueError):
            c.bid("intent-1", 0)


class TestErrorMapping(unittest.TestCase):
    def test_401_raises_auth_error(self):
        t = FakeTransport(401, {"code": "signature_invalid", "detail": "key required"})
        c = SynmercoClient(api_key="bad", transport=t)
        with self.assertRaises(AuthError) as ctx:
            c.heartbeat()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, "signature_invalid")

    def test_412_raises_precondition_error(self):
        t = FakeTransport(412, {"code": "insufficient_score", "detail": "score too low"})
        c = SynmercoClient(api_key="k", transport=t)
        c.did = "did:x"
        with self.assertRaises(PreconditionError) as ctx:
            c.bid("intent-1", 500)
        self.assertEqual(ctx.exception.code, "insufficient_score")

    def test_429_raises_rate_limited_with_retry_after(self):
        t = FakeTransport(429, {"code": "EARNINGS_RATE_LIMITED",
                                "detail": "once per 10 seconds",
                                "retryAfterSeconds": 7})
        c = SynmercoClient(api_key="k", transport=t)
        with self.assertRaises(RateLimited) as ctx:
            c.list_jobs()
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_unknown_status_raises_base_error(self):
        t = FakeTransport(500, {"detail": "boom"})
        c = SynmercoClient(api_key="k", transport=t)
        with self.assertRaises(Exception) as ctx:
            c.protocol()
        self.assertEqual(getattr(ctx.exception, "status", None), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
