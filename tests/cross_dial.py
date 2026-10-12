#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["iroh==1.1.0"]
# ///
"""Cross-network test, laptop side.

    uv run --managed-python --script tests/cross_dial.py <run id>

It dials the runner of each operating system through the relay, sends one message and waits for the answer.
"""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..")]
import peerlink  # noqa: E402
from cross_accept import accept, derive, route  # noqa: E402

WAIT_S = 540


async def one(run_id, system):
    _, secret, _, endpoint = derive(f"{run_id}-{system}")
    ticket = peerlink.make_ticket(endpoint, secret)
    events = []
    pl = peerlink.PeerLink(events.append, ask=accept, session="laptop")
    end = time.monotonic() + WAIT_S
    try:
        while time.monotonic() < end:   # the runner needs time to start
            try:
                await pl.connect(system, ticket)
            except peerlink.PeerError:
                await asyncio.sleep(10)
                continue
            while not any(e["ev"] in ("open", "denied") for e in events):
                await asyncio.sleep(0.2)
            if events[-1]["ev"] == "open":
                break
            return f"{system}: FAIL, the runner refused: {events[-1]['reason']}"
        else:
            return f"{system}: FAIL, no runner in {WAIT_S} s"
        t0 = time.monotonic()
        await pl.send(system, "ping from the laptop")
        while time.monotonic() < end and not any(e["ev"] == "msg" for e in events):
            await asyncio.sleep(0.1)
        got = [e["text"] for e in events if e["ev"] == "msg"]
        if not got:
            return f"{system}: FAIL, no answer"
        return f"{system}: ok, answer \"{got[0]}\" in {time.monotonic() - t0:.1f} s | route: {route(pl, system)}"
    finally:
        await pl.close()


async def main(run_id):
    results = await asyncio.gather(*(one(run_id, s) for s in ("Linux", "macOS", "Windows")))
    print("\n".join(results))
    return 0 if all(": ok" in r for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
