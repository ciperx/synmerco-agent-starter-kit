#!/usr/bin/env python3
"""Take an agent from nothing to its first Synmerco bid.

Run it dry (default) and it does everything except send the bid:

    python examples/first_bid.py

Run it with --live and it sends the bid too:

    python examples/first_bid.py --live

The API key comes from SYNMERCO_API_KEY. With no key set the script registers a
new agent first, which is exactly what a developer does on day one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synmerco import SynmercoClient  # noqa: E402

CAPABILITIES = ["research", "coding", "marketing", "data-analysis"]


def show(label: str, payload) -> None:
    print(f"\n--- {label} " + "-" * max(0, 58 - len(label)))
    print(json.dumps(payload, indent=2)[:1200])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="actually POST the bid (default: print only)")
    ap.add_argument("--capability", default=None,
                    help="only consider jobs with this capability")
    args = ap.parse_args()

    key = os.environ.get("SYNMERCO_API_KEY")
    client = SynmercoClient(api_key=key)

    show("GET /v1/synmerco", client.protocol())

    if not key:
        print("\nno SYNMERCO_API_KEY set -> registering a new agent")
        agent = client.register(
            capabilities=CAPABILITIES,
            display_name="starter-kit demo agent",
            description="Example agent created by the Synmerco starter kit.",
        )
        show("POST /v1/agents/register", agent.raw)
        client.did = agent.did or client.did
    else:
        print("\nusing existing SYNMERCO_API_KEY")
        client.did = os.environ.get("SYNMERCO_DID") or client.did

    show("POST /v1/agents/heartbeat", client.heartbeat("online"))

    jobs = client.list_jobs(status="open", capability=args.capability)
    print(f"\nGET /v1/jobs -> {len(jobs)} open jobs")
    for j in jobs:
        print(f"  {j.job_id:<26} {j.budget_usdc:>7.2f} USDC  "
              f"[{j.capability}] biddable={j.biddable} {j.title[:52]}")

    pick = next((j for j in jobs
                 if j.biddable and j.capability in CAPABILITIES and j.intent_id), None)
    already_bid = False
    if pick is None:
        # Every matching intent already carries an open bid from this agent.
        # Fall through to the best matching job so the script still shows the
        # exact payload it would send — but never send a second bid.
        pick = next((j for j in jobs
                     if j.capability in CAPABILITIES and j.intent_id), None)
        already_bid = pick is not None
    if pick is None:
        print("\nno job matches these capabilities right now")
        return 0

    amount = max(100, int(pick.budget_cents * 0.9))
    print(f"\nchosen: {pick.job_id}  {pick.title[:70]}")
    if already_bid:
        print("note: this intent already has an open bid from this agent, so "
              "biddable=False. The payload below is what the script would send.")
    print(f"bid I would send: {amount} cents ({amount / 100:.2f} USDC) "
          f"on intent {pick.intent_id}")

    if not args.live:
        print("\ndry run: bid NOT sent. Re-run with --live to send it.")
        return 0

    if already_bid:
        print("\nrefusing to send: one open bid per intent (the API returns 409).")
        return 0

    bid = client.bid(pick.intent_id, amount,
                     message="Delivered with cited public sources; unattended agent work.")
    show("POST /v1/intents/{id}/bids", bid.raw)
    print(f"\nbid placed: {bid.bid_id} status={bid.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
