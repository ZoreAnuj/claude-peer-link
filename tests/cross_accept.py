#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["iroh==1.1.0"]
# ///
"""Cross-network test, runner side. A commit message with [cross] starts it on a GitHub runner.

The key and the ticket secret come from PEERLINK_CROSS_ID, so that the laptop can dial without a
message from the runner. This is for the test only: the run id is not a secret.
The consent function is replaced here, in the test process only.
"""
import asyncio
import hashlib
import os
import platform
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import peerlink  # noqa: E402

WAIT_S = 480


def derive(cross_id):
    """The endpoint secret key, the ticket secret and the relay for one side of the test."""
    import iroh
    key = hashlib.sha256(b"peerlink cross key " + cross_id.encode()).digest()
    secret = hashlib.sha256(b"peerlink cross secret " + cross_id.encode()).hexdigest()[:32]
    urls = sorted(iroh.RelayMode.default_mode().relay_map().urls())
    relay = next((u for u in urls if "use1" in u), urls[0])
    return key, secret, relay, bytes(iroh.SecretKey.from_bytes(key).public().to_bytes()).hex()


async def accept(*args, **kwargs):
    return True


def route(pl, name):
    """How the traffic of one link goes: "relay" or "direct"."""
    paths = pl.links[name.lower()].conn.paths()
    return ", ".join(("relay" if p.is_relay else "direct") + (" (selected)" if p.is_selected else "") for p in paths)


async def main():
    key, secret, relay, _ = derive(os.environ["PEERLINK_CROSS_ID"])
    events = []
    pl = peerlink.PeerLink(events.append, ask=accept, session="runner", secret_key=key, relay_urls=[relay])
    await pl.start()
    pl.tickets[secret] = ("Laptop", time.monotonic() + WAIT_S)   # the invitation of this test
    print(f"{platform.system()}: endpoint {pl.me[:10]} waits on {relay}", flush=True)
    end = time.monotonic() + WAIT_S
    while time.monotonic() < end and not any(e["ev"] == "msg" for e in events):
        await asyncio.sleep(0.2)
    got = [e["text"] for e in events if e["ev"] == "msg"]
    if not got:
        print("no message from the laptop:", [e["ev"] for e in events])
        return 1
    print("message from the laptop:", got[0], "| route:", route(pl, "Laptop"), flush=True)
    await pl.send("Laptop", f"pong from {platform.system()} {platform.machine()}")
    await asyncio.sleep(3)
    await pl.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
