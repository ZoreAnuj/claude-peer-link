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
import contextlib
import io
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import peerlink  # noqa: E402
from peerlink import PeerError, PeerLink  # noqa: E402

SIDES = []
HOMES = []
KNOBS = ("LEASE_S", "TICKET_S", "PENDING_S", "PENDING_GRACE_S", "WARN_S", "RATE_PER_MIN", "REQ_PER_HOUR")


class Side:
    """One helper with a recorded, scripted consent function and an event list."""

    def __init__(self, label, deny=(), home=None):
        self.label, self.deny = label, deny
        self.events, self.asks, self.seen = [], [], 0
        self.slow = {}  # keyword -> seconds to wait before the answer
        self.pl = PeerLink(self.events.append, ask=self.ask, session=label, **({"home": home} if home else {}))
        SIDES.append(self)

    async def ask(self, title, text, body="", sound=False, yes="Accept", timeout=None):
        self.asks.append({"text": text, "body": body, "sound": sound, "timeout": timeout})
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


def home():
    """A new directory for the identity and the contacts of one helper. The runner removes it."""
    HOMES.append(tempfile.mkdtemp(prefix="peerlink-test-"))
    return HOMES[-1]


class SlowDial:
    """An endpoint whose dial is slow. A remote peer controls the time of a handshake."""

    def __init__(self, ep, seconds):
        self._ep, self._seconds = ep, seconds

    def __getattr__(self, name):
        return getattr(self._ep, name)

    async def connect(self, addr, alpn):
        await asyncio.sleep(self._seconds)
        return await self._ep.connect(addr, alpn)


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
    # fullwidth brackets, line and paragraph separators, and the invisible selectors that can hide text
    hidden = "x" + chr(0xFF1C) + "y" + chr(0xFF1E) + chr(0x2028) + "z" + chr(0x2029) + "w" + chr(0xE0101) + "!"
    assert peerlink.sanitize(hidden) == "x" + chr(0x2039) + "y" + chr(0x203A) + "\nz\nw!", repr(peerlink.sanitize(hidden))
    emoji = "ok " + chr(0x1F600) + chr(0xFE0F)
    assert peerlink.sanitize(emoji) == emoji   # a normal emoji stays
    k1, k2 = secrets.token_bytes(32), secrets.token_bytes(32)
    code = peerlink.code_for(k1, k2)
    assert code == peerlink.code_for(k2, k1) and len(code) == 4 and code.isdigit()
    eid, sec = k1.hex(), secrets.token_hex(16)
    ticket = peerlink.make_ticket(eid, sec)
    assert ticket.startswith("peer2.") and peerlink.parse_ticket(ticket) == (eid, sec)
    for bad in ("", "garbage", "peer2.", "peer2.!!!", ticket[:30], ticket + "AAAA", "peer1." + ticket[6:]):
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
    try:   # the person must see which name was refused
        peerlink.check_name("<their name>")
    except PeerError as e:
        assert str(e).startswith('"<their name>" is not a valid name'), e
    try:
        peerlink.check_name("y" * 200)
    except PeerError as e:
        assert len(str(e)) < 260, len(str(e))


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
    assert not b.asked('Ask "Alice"'), "a typed renewal request showed a local dialog"
    assert a.asked("Renew the connection")[0]["sound"]
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
    eid, _ = peerlink.parse_ticket(await a.pl.invite("Bob"))
    await b.pl.connect("Alice", peerlink.make_ticket(eid, secrets.token_hex(16)))
    await b.wait("denied", 15)
    assert not a.asked("Accept connection"), "a wrong secret made a popup"


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


async def test_ctl():
    """A request with the key gets an answer. A request with a wrong key gets none. The helper stops with its reader."""
    helper = os.path.join(ROOT, "peerlink.py")
    env = dict(os.environ, PEERLINK_HOME=home())        # not the identity of the person who runs the tests
    serve = subprocess.Popen([sys.executable, helper, "serve", "ctl test"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=env)
    try:
        ready = json.loads(await asyncio.wait_for(asyncio.to_thread(serve.stdout.readline), 30))
        assert ready["ev"] == "ready" and len(ready["key"]) == 64, ready
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(os.path.dirname(ready["ctl"])).st_mode) == 0o700

        async def ctl(key, req):
            proc = await asyncio.create_subprocess_exec(sys.executable, helper, "ctl", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(json.dumps({"ctl": ready["ctl"], "key": key, "req": req}).encode()), 30)
            return proc.returncode, json.loads(out)

        assert await ctl(ready["key"], {"op": "status"}) == (0, {"ok": True, "result": {"links": [], "contacts": []}})
        code, answer = await ctl(ready["key"], {"op": "end", "name": "Nobody"})
        assert code == 0 and answer["ok"] is False and "no link" in answer["error"], answer
        assert (await ctl(ready["key"], {"op": "explode"}))[1]["ok"] is False
        assert (await ctl(ready["key"], {"op": "send", "name": None, "text": 5}))[1]["ok"] is False
        code, answer = await ctl("00" * 32, {"op": "status"})
        assert code == 1 and answer["ok"] is False, (code, answer)
        assert (await ctl(ready["key"], {"op": "status"}))[1]["ok"] is True, "the helper stopped after a wrong key"
        # Review I4: a client that connects and sends nothing must not stop the other requests
        if os.name == "posix":
            silent = socket.socket(socket.AF_UNIX)
            silent.connect(ready["ctl"])
        else:
            silent = open(ready["ctl"], "r+b", buffering=0)
        try:
            await asyncio.sleep(0.5)
            assert (await ctl(ready["key"], {"op": "status"}))[1]["ok"] is True
        finally:
            silent.close()
        serve.stdout.close()   # the mod is gone: the helper must stop at its next output line
        assert await asyncio.wait_for(asyncio.to_thread(serve.wait), 20) == 0
        if os.name == "posix":
            assert not os.path.exists(os.path.dirname(ready["ctl"])), "the socket directory stayed"
    finally:
        if serve.poll() is None:
            serve.kill()


async def test_no_link_outside_the_table():
    """Review C1: a slow dial and an inbound request for one name. Each link must stay under a lease."""
    peerlink.LEASE_S, peerlink.WARN_S = 4.0, 0.5
    a, b = await linked()
    await a.wait("closed", 8)
    await b.wait("closed", 8)
    a.pl.ep = SlowDial(a.pl.ep, 3.0)
    dial = asyncio.ensure_future(a.pl.connect("Bob"))   # Alice asks; her dial is slow
    await asyncio.sleep(0.3)
    await b.pl.connect("Alice")                         # Bob asks at the same time; Alice accepts
    await a.wait("open", 10)
    await b.wait("open", 10)
    t_open, inbound, b_link = time.monotonic(), a.pl.links["bob"], b.pl.links["alice"]
    b_link.expires = b_link.expires_wall = float("inf")   # a hostile peer does not obey its own timer
    b.deny = ("Accept connection",)
    try:
        await dial
    except PeerError:
        pass
    else:
        raise AssertionError("the late dial made a second link for the same name")
    assert a.pl.links.get("bob") is inbound and inbound.state == "open"
    await a.wait("closed", 8)                           # the lease of Alice still ends the link
    assert time.monotonic() - t_open < 6.0 and a.pl.status() == []
    try:
        await b.pl._write(b_link, {"t": "msg", "text": "text after the lease end"})
    except Exception:
        pass
    await a.absent("msg", 1.5)


async def test_late_renewed():
    """Review I1: a renewal answer counts only for the time of one request."""
    peerlink.LEASE_S, peerlink.WARN_S, peerlink.PENDING_S, peerlink.PENDING_GRACE_S = 6.0, 0.5, 1.0, 0.5
    a, b = await linked()
    t0, b_link = time.monotonic(), b.pl.links["alice"]
    b.slow["Renew the connection"] = 60.0               # Bob never answers the request
    b_link.expires = b_link.expires_wall = float("inf")
    await a.pl.connect("Bob")                           # Alice asks for a renewal
    await asyncio.sleep(5.0 - (time.monotonic() - t0))
    await b.pl._write(b_link, {"t": "renewed"})         # the answer comes 5 s later, 1 s before the lease end
    await a.wait("closed", 4)
    assert time.monotonic() - t0 < 8.0, "a late answer extended the lease"
    assert not [e for e in a.events if e["ev"] == "renewed"]


async def test_renewal_always_gets_an_answer():
    """Review I1: a renewal request above the rate gets a refusal, so that the asker can ask again later."""
    peerlink.REQ_PER_HOUR = 0
    a, b = await linked()
    await b.pl.connect("Alice")
    assert (await b.wait("renew_denied", 5))["name"] == "Alice"
    assert not a.asked("Renew the connection")
    await b.pl.connect("Alice")                         # not "a renewal request waits"
    await b.wait("renew_denied", 5)


async def test_one_pending_request():
    """Review I2: a known peer that dials again while its first request waits gets no second dialog."""
    peerlink.LEASE_S, peerlink.WARN_S = 2.0, 0.5
    a, b = await linked()
    await a.wait("closed", 8)
    await b.wait("closed", 8)
    a.slow["Accept connection"] = 4.0                   # Alice does not answer for 4 s
    iroh, before, keep = b.pl._iroh, len(a.asked("Accept connection")), []
    addr = iroh.EndpointAddr(iroh.EndpointId.from_bytes(bytes.fromhex(a.pl.me)), a.pl.relay, [])
    for _ in range(3):
        conn = await b.pl.ep.connect(addr, peerlink.ALPN)
        bi = await conn.open_bi()
        await bi.send().write_all(b'{"t":"hello"}\n')
        keep.append((conn, bi))
        await asyncio.sleep(0.8)
    await asyncio.sleep(3.0)
    assert len(a.asked("Accept connection")) - before == 1, a.asks


async def test_loopback_only():
    """Review I3: the endpoint has no socket on an address of this machine that a peer can reach."""
    a = Side("alice")
    await a.pl.start()
    bound = [str(x) for x in a.pl.ep.bound_sockets()]
    assert bound and all(x.startswith(("127.0.0.1:", "[::1]:")) for x in bound), bound


async def test_consent_wait_limit():
    """Review I5: the wait for a free dialog and the dialog together have one limit."""
    peerlink.PENDING_S = 1.5
    a = Side("alice")
    await a.pl.start()
    a.slow["Create"] = 4.0                              # the first dialog stays open
    first = asyncio.ensure_future(a.pl.invite("Bob"))
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    assert "refused" in await raises(a.pl.invite, "Carol")
    assert time.monotonic() - t0 < 2.5, "the second request waited longer than the limit"
    assert len(a.asked("Create")) == 1, "a dialog showed after its request had a refusal"
    assert a.asks[0]["timeout"] <= 1.5
    await first


async def test_no_text_after_end():
    """Review I6: after /peer end starts, no peer text reaches the session."""
    a, b = await linked()
    b_link = b.pl.links["alice"]
    for task in b_link.tasks:                           # a hostile peer does not obey "bye" and does not close
        task.cancel()

    async def flood():
        for i in range(60):
            try:
                await b.pl._write(b_link, {"t": "msg", "text": f"m{i}"})
            except Exception:
                return
            await asyncio.sleep(0.05)
    task = asyncio.ensure_future(flood())
    await a.wait("msg", 5)
    before = len([e for e in a.events if e["ev"] == "msg"])
    await a.pl.end("Bob")
    await asyncio.sleep(1.0)
    task.cancel()
    late = len([e for e in a.events if e["ev"] == "msg"]) - before
    assert late == 0, f"{late} peer messages reached the session after the end command"


async def test_saved_contact_needs_no_ticket():
    """After the first link, each side keeps its key and the contact. A later request needs no ticket."""
    da, db = home(), home()
    a, b = Side("alice", home=da), Side("bob", home=db)
    await up(a, b)
    await b.pl.connect("Alice", await a.pl.invite("Bob"))
    await a.wait("open")
    await b.wait("open")
    keys = (a.pl.me, b.pl.me)
    if os.name == "posix":
        for d in (da, db):
            for name in ("identity.key", "contacts.json"):
                assert stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode) == 0o600, (d, name)
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    await a.pl.close()                                  # the two helpers stop
    await b.pl.close()
    a2, b2 = Side("alice", home=da), Side("bob", home=db)
    assert "Bob" in await a2.pl.listen()                # Alice listens; no ticket is made
    await b2.pl.connect("alice")                        # Bob types /peer connect alice
    await a2.wait("open", 30)
    await b2.wait("open", 30)
    assert (a2.pl.me, b2.pl.me) == keys
    assert b2.asks == [], "a typed request to a saved contact showed a local dialog"
    accept = a2.asked("Accept connection")
    assert len(accept) == 1 and accept[0]["sound"] and "saved contact" in accept[0]["text"], a2.asks
    await b2.pl.send("Alice", "hello again")
    assert (await a2.wait("msg"))["text"] == "hello again"
    await a2.pl.connect("Bob")                          # a renewal, typed by Alice: no dialog on her side
    await b2.wait("renewed", 10)
    assert not a2.asked("Ask ")


async def test_one_helper_for_each_identity():
    d = home()
    a = Side("alice", home=d)
    await a.pl.start()
    assert "in use" in await raises(Side("alice-2", home=d).pl.start)
    await a.pl.close()
    c = Side("alice-3", home=d)
    await c.pl.start()
    assert c.pl.me == a.pl.me


async def test_forget():
    """A forgotten contact gets no dialog, and the local side cannot ask it without a new ticket."""
    da = home()
    a, b = Side("alice", home=da), Side("bob", home=home())
    await up(a, b)
    await b.pl.connect("Alice", await a.pl.invite("Bob"))
    await a.wait("open")
    await b.wait("open")
    assert "Bob" in await a.pl.forget("bob")            # this also ends the link
    await a.wait("closed", 5)
    assert "bob" not in open(os.path.join(da, "contacts.json")).read().lower()
    await b.wait("closed", 5)
    await b.pl.connect("Alice")                         # Bob still has the contact and asks
    await b.wait("denied", 15)
    assert len(a.asked("Accept connection")) == 1, "a forgotten contact made a popup"
    assert "no ticket" in await raises(a.pl.connect, "Bob")
    assert await raises(a.pl.forget, "Bob")


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
        t0, said = time.monotonic(), io.StringIO()
        with contextlib.redirect_stderr(said):
            assert await peerlink.ask("Peer link test", "No display.", timeout=5.0) is False
        assert time.monotonic() - t0 < 4.5, "a consent without a display did not fail fast"
        assert said.getvalue().startswith("peerlink: no consent dialog is possible"), said.getvalue()
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
            while HOMES:
                shutil.rmtree(HOMES.pop(), ignore_errors=True)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
