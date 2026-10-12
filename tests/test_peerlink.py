#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["iroh==1.1.0"]
# ///
"""Helper tests.

Run all:  uv run --managed-python --script tests/test_peerlink.py
Run some: uv run --managed-python --script tests/test_peerlink.py lease renewal

Two helpers run in one process and connect through the public relay.
The consent dialog is replaced here, in the test process only.
PEERLINK_TEST_DIALOG=1 also shows the real dialog for two seconds.
"""
import asyncio
import os
import secrets
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import peerlink  # noqa: E402
from peerlink import PeerError, PeerLink  # noqa: E402

SIDES = []
KNOBS = ("LEASE_S", "TICKET_S", "PENDING_S", "PENDING_GRACE_S", "WARN_S", "RATE_PER_MIN", "REQ_PER_HOUR")


class Side:
    """One helper with a recorded, scripted consent function and an event list."""

    def __init__(self, label, deny=()):
        self.label, self.deny = label, deny
        self.events, self.asks, self.seen = [], [], 0
        self.slow = {}  # keyword -> seconds to wait before the answer
        self.pl = PeerLink(self.events.append, ask=self.ask, session=label)
        SIDES.append(self)

    async def ask(self, title, text, body="", sound=False, yes="Accept", timeout=None):
        self.asks.append({"text": text, "body": body, "sound": sound})
        for word, seconds in self.slow.items():
            if word in text:
                await asyncio.sleep(seconds)
        return not any(word in text for word in self.deny)

    def asked(self, word):
        return [a for a in self.asks if word in a["text"]]

    async def wait(self, ev, timeout=30.0):
        end = time.monotonic() + timeout
        while True:
            for i in range(self.seen, len(self.events)):
                if self.events[i]["ev"] == ev:
                    self.seen = i + 1
                    return self.events[i]
            if time.monotonic() > end:
                raise AssertionError(f"{self.label}: no '{ev}' event in {timeout} s; got {self.events[self.seen:]}")
            await asyncio.sleep(0.05)

    async def absent(self, ev, seconds):
        await asyncio.sleep(seconds)
        late = [e for e in self.events[self.seen:] if e["ev"] == ev]
        assert not late, f"{self.label}: unwanted '{ev}' event: {late}"


async def up(*sides):
    """Bind the endpoints at the same time, to keep the tests short."""
    await asyncio.gather(*(s.pl.start() for s in sides))


async def linked(a_deny=(), b_deny=()):
    """Alice invites Bob, Bob connects, the two persons accept."""
    a, b = Side("alice", a_deny), Side("bob", b_deny)
    await up(a, b)
    ticket = await a.pl.invite("Bob")
    await b.pl.connect("Alice", ticket)
    await a.wait("open")
    await b.wait("open")
    return a, b


def raises(fn, *args):
    async def run():
        try:
            await fn(*args)
        except PeerError as e:
            return str(e)
        raise AssertionError(f"{fn.__name__}{args[:1]} did not raise PeerError")
    return run()


async def test_pure():
    assert peerlink.sanitize("a<b>\x00‮ c\n\td") == "a‹b› c\n\td"
    k1, k2 = secrets.token_bytes(32), secrets.token_bytes(32)
    code = peerlink.code_for(k1, k2)
    assert code == peerlink.code_for(k2, k1) and len(code) == 4 and code.isdigit()
    eid, relay, sec = k1.hex(), "https://relay.example./", secrets.token_hex(16)
    ticket = peerlink.make_ticket(eid, relay, sec)
    assert ticket.startswith("peer1.") and peerlink.parse_ticket(ticket) == (eid, relay, sec)
    for bad in ("", "garbage", "peer1.", "peer1.!!!", ticket[:30], "peer2." + ticket[6:]):
        try:
            peerlink.parse_ticket(bad)
        except PeerError:
            continue
        raise AssertionError(f"parse_ticket accepted {bad!r}")
    assert peerlink.check_name(" Bob Smith ") == "Bob Smith"
    for bad in ("", "x" * 33, 'a"b', "peer:bob", "<b>", "-bob", None):
        try:
            peerlink.check_name(bad)
        except PeerError:
            continue
        raise AssertionError(f"check_name accepted {bad!r}")


async def test_accept():
    a, b = await linked()
    accept, connect = a.asked("Accept connection"), b.asked("Connect this session")
    assert len(accept) == 1 and accept[0]["sound"] and '"Bob"' in accept[0]["text"], a.asks
    assert len(connect) == 1 and '"Alice"' in connect[0]["text"], b.asks
    code = accept[0]["text"].split("Code ")[1][:4]
    assert code.isdigit() and f"Code {code}" in connect[0]["text"], (accept, connect)
    assert a.pl.status()[0]["state"] == "open" and 58 <= a.pl.status()[0]["minutes_left"] <= 60

    await b.pl.send("alice", "hi <b>\x07 there")
    m = await a.wait("msg")
    assert m["name"] == "Bob" and m["text"] == "hi ‹b› there", m
    assert len(m["mark"]) == 8 and int(m["mark"], 16) >= 0
    assert b.asked("Send this message")[0]["body"] == "hi <b>\x07 there"

    await a.pl.send("Bob", "hello Bob")
    m2 = await b.wait("msg")
    assert m2["name"] == "Alice" and m2["text"] == "hello Bob" and m2["mark"] != m["mark"]


async def test_deny():
    a, b = Side("alice", deny=("Accept connection",)), Side("bob")
    await up(a, b)
    await b.pl.connect("Alice", await a.pl.invite("Bob"))
    d = await b.wait("denied")
    assert d["name"] == "Alice", d
    assert a.pl.status() == [] and b.pl.status() == []
    assert await raises(b.pl.send, "Alice", "x")


async def test_timeout():
    peerlink.PENDING_S, peerlink.PENDING_GRACE_S = 1.0, 0.5
    a, b = Side("alice"), Side("bob")
    await up(a, b)
    a.slow["Accept connection"] = 4.0
    await b.pl.connect("Alice", await a.pl.invite("Bob"))
    d = await b.wait("denied", 6)
    assert "no answer" in d["reason"], d
    await asyncio.sleep(3.5)  # the late accept of Alice must not make a link
    assert a.pl.status() == [] and b.pl.status() == [], (a.pl.status(), b.pl.status())


async def test_local_refusal():
    a, b = Side("alice"), Side("bob", deny=("Connect this session",))
    await up(a, b)
    ticket = await a.pl.invite("Bob")
    assert "refused" in await raises(b.pl.connect, "Alice", ticket)
    await asyncio.sleep(1.5)
    assert not a.asked("Accept connection"), "a refused connect made a dial"

    a, b = await linked(b_deny=("Send this message",))
    assert "refused" in await raises(b.pl.send, "Alice", "secret text")
    await a.absent("msg", 1.5)


async def test_lease_end():
    peerlink.LEASE_S, peerlink.WARN_S = 3.0, 2.0
    a, b = await linked()
    t0 = time.monotonic()
    for s in (a, b):
        assert (await s.wait("warn", 5))["minutes"] >= 1
        assert "lease" in (await s.wait("closed", 6))["reason"]
    assert 2.0 < time.monotonic() - t0 < 7.0
    assert a.pl.status() == [] and b.pl.status() == []
    assert await raises(b.pl.send, "Alice", "too late")


async def test_renewal():
    peerlink.LEASE_S, peerlink.WARN_S = 4.0, 0.5
    a, b = await linked()
    t0 = time.monotonic()
    await asyncio.sleep(1.5)
    await b.pl.connect("Alice")  # no ticket and a live link: a renewal request
    assert (await a.wait("renewed", 5))["name"] == "Bob"
    assert (await b.wait("renewed", 5))["name"] == "Alice"
    assert b.asked('Ask "Alice"') and a.asked("Renew the connection")[0]["sound"]
    await asyncio.sleep(max(0.0, 4.8 - (time.monotonic() - t0)))  # past the first lease end
    assert [x["state"] for x in a.pl.status() + b.pl.status()] == ["open", "open"]
    await a.wait("closed", 6)

    a, b = await linked(a_deny=("Renew the connection",))
    await b.pl.connect("Alice")
    assert (await b.wait("renew_denied", 5))["name"] == "Alice"
    await a.absent("renewed", 0.5)


async def test_one_sided_renewal():
    peerlink.LEASE_S, peerlink.WARN_S = 3.0, 0.5
    a, b = await linked()
    t0 = time.monotonic()
    await asyncio.sleep(1.0)
    await b.pl._write(b.pl.links["alice"], {"t": "renewed"})  # Bob tries to extend the lease of Alice alone
    await a.wait("closed", 5)
    assert time.monotonic() - t0 < 4.5, "the lease of Alice was extended"
    assert not [e for e in a.events if e["ev"] == "renewed"]


async def test_ask_again():
    peerlink.LEASE_S, peerlink.WARN_S = 2.0, 0.5
    a, b = await linked()
    await a.wait("closed", 6)
    await b.wait("closed", 6)
    await b.pl.connect("Alice")  # no ticket: Bob asks again
    await a.wait("open", 20)
    await b.wait("open", 20)
    assert len(a.asked("Accept connection")) == 2 and a.asked("Accept connection")[1]["sound"]
    await a.wait("closed", 6)
    await b.wait("closed", 6)
    await a.pl.connect("Bob")  # the other direction
    await b.wait("open", 20)
    await a.wait("open", 20)
    assert len(b.asked("Accept connection")) == 1 and b.asked("Accept connection")[0]["sound"]
    assert await raises(Side("carol").pl.connect, "Alice")  # no ticket and no earlier link


async def test_ticket_reuse():
    a, b, c = Side("alice"), Side("bob"), Side("carol")
    await up(a, b, c)
    ticket = await a.pl.invite("Bob")
    await b.pl.connect("Alice", ticket)
    await a.wait("open")
    await c.pl.connect("Alice", ticket)
    await c.wait("denied", 15)
    assert len(a.asked("Accept connection")) == 1, "a used ticket made a popup"


async def test_ticket_expired():
    peerlink.TICKET_S = 0.5
    a, b = Side("alice"), Side("bob")
    await up(a, b)
    ticket = await a.pl.invite("Bob")
    await asyncio.sleep(1.0)
    await b.pl.connect("Alice", ticket)
    await b.wait("denied", 15)
    assert not a.asked("Accept connection"), "an expired ticket made a popup"


async def test_no_secret():
    a, b = Side("alice"), Side("bob")
    await up(a, b)
    eid, relay, _ = peerlink.parse_ticket(await a.pl.invite("Bob"))
    await b.pl.connect("Alice", peerlink.make_ticket(eid, relay, secrets.token_hex(16)))
    await b.wait("denied", 15)
    assert not a.asked("Accept connection"), "a wrong secret made a popup"
    other = peerlink.make_ticket(eid, "https://relay.example./", secrets.token_hex(16))
    assert "relay" in await raises(Side("carol").pl.connect, "Alice", other)  # no dial to a relay that we do not know


async def test_limits():
    peerlink.RATE_PER_MIN = 3
    a, b = await linked()
    link = b.pl.links["alice"]
    assert "larger" in await raises(b.pl.send, "Alice", "x" * (peerlink.MAX_MSG_BYTES + 1))
    await b.pl._write(link, {"t": "msg", "text": "y" * (peerlink.MAX_MSG_BYTES + 1)})  # a peer that ignores the limit
    for i in range(5):
        await b.pl.send("Alice", f"m{i}")
    got = [(await a.wait("msg", 10))["text"] for _ in range(3)]
    assert got == ["m0", "m1", "m2"], got
    await a.absent("msg", 1.0)
    assert a.pl.status()[0]["dropped"] == 3, a.pl.status()

    async with link.lock:  # one line that is larger than a frame ends the link
        await link.tx.write_all(b"z" * (peerlink.MAX_FRAME + 10) + b"\n")
    await a.wait("closed", 10)


async def test_request_rate():
    peerlink.LEASE_S, peerlink.WARN_S, peerlink.REQ_PER_HOUR = 1.5, 0.5, 1
    a, b = await linked()
    await a.wait("closed", 6)
    await b.wait("closed", 6)
    await b.pl.connect("Alice")
    await a.wait("open", 20)
    await a.wait("closed", 6)
    await b.wait("closed", 6)
    await b.pl.connect("Alice")  # the second request in one hour
    await b.wait("denied", 15)
    assert len(a.asked("Accept connection")) == 2, "a request above the rate made a popup"


async def test_end():
    a, b = await linked()
    assert "closed" in await a.pl.end("bob")
    assert "ended" in (await b.wait("closed", 10))["reason"]
    assert a.pl.status() == []
    assert await raises(a.pl.end, "Bob")


async def test_dialog():
    if os.environ.get("PEERLINK_TEST_DIALOG") != "1":
        return "skipped (set PEERLINK_TEST_DIALOG=1)"
    t0 = time.monotonic()
    ok = await peerlink.ask("Peer link test", "Test dialog. It closes in 2 seconds.\nNo action is necessary.",
                            body="Text that the person approves is shown here.", sound=True, timeout=2.0)
    dt = time.monotonic() - t0
    assert ok is False and 1.5 <= dt < 20, f"dialog result {ok} after {dt:.1f} s (a time below 1.5 s means that no window showed)"


async def test_no_display():
    if not sys.platform.startswith("linux"):
        return "skipped (Linux only)"
    saved = {k: os.environ.pop(k) for k in ("DISPLAY", "WAYLAND_DISPLAY") if k in os.environ}
    try:
        t0 = time.monotonic()
        assert await peerlink.ask("Peer link test", "No display.", timeout=5.0) is False
        assert time.monotonic() - t0 < 4.5, "a consent without a display did not fail fast"
    finally:
        os.environ.update(saved)


async def main(names):
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and (not names or any(x in n for x in names))]
    failed = 0
    for name, fn in tests:
        saved = {k: getattr(peerlink, k) for k in KNOBS if hasattr(peerlink, k)}
        t0 = time.monotonic()
        try:
            note = await asyncio.wait_for(fn(), 120)
            print(f"ok    {name} ({time.monotonic() - t0:.1f} s){' ' + note if note else ''}", flush=True)
        except Exception:
            failed += 1
            print(f"FAIL  {name} ({time.monotonic() - t0:.1f} s)\n{traceback.format_exc()}", flush=True)
        finally:
            for k, v in saved.items():
                setattr(peerlink, k, v)
            for s in SIDES:
                try:
                    await asyncio.wait_for(s.pl.close(), 10)
                except Exception:
                    pass
            SIDES.clear()
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
