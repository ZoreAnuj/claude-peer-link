#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["iroh==1.1.0"]
# ///
"""Peer link helper: transport, tickets, leases, limits and consent dialogs.

  peerlink.py --dialog   the consent dialog, a child of the helper: JSON on standard input

Design: docs/2026-10-12-peer-link-design.md
"""
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

ALPN = b"peerlink/1"
LEASE_S = 3600.0         # an accepted link ends after this time
TICKET_S = 600.0         # an invitation is valid for this time and for one use
PENDING_S = 300.0        # a request or a dialog without an answer is a deny
PENDING_GRACE_S = 30.0   # the dialer waits this much longer than the dialog of the peer
WARN_S = 300.0           # the person gets a notice this long before the lease end
GUARD_S = 1.0            # the accept button is off for this time
MAX_MSG_BYTES = 16384
MAX_FRAME = 65536
RATE_PER_MIN = 30        # inbound messages for each link
REQ_PER_HOUR = 3         # requests without a ticket, and renewal requests, for each peer
MAX_LINKS = 4
TITLE = "Peer link"


class PeerError(Exception):
    """An error with a text for the person or for Claude."""


# ---------------------------------------------------------------- consent dialog

def _sound(root):
    """Start the attention sound. The player depends on the operating system."""
    path = os.environ.get("PEERLINK_SOUND")
    try:
        if sys.platform == "win32":
            import winsound
            if path:
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
            return
        if sys.platform == "darwin":
            players = [["afplay", path or "/System/Library/Sounds/Glass.aiff"]]
        else:
            path = path or "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"
            players = [["pw-play", path], ["paplay", path], ["canberra-gtk-play", "-f", path]]
        for cmd in players:
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    except Exception:
        pass
    root.bell()


def dialog_main():
    """Show one consent dialog. Exit code 0 and the word "accept" only if the person presses the accept button."""
    import tkinter as tk
    spec = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    accepted = []
    root = tk.Tk()
    root.title(spec["title"])
    root.resizable(False, False)
    root.attributes("-topmost", True)
    tk.Label(root, text=spec["text"], justify="left", wraplength=520, padx=22, pady=16).pack(anchor="w")
    if spec.get("body"):
        frame = tk.Frame(root, padx=22)
        frame.pack(fill="both")
        bar = tk.Scrollbar(frame)
        bar.pack(side="right", fill="y")
        box = tk.Text(frame, width=72, height=14, wrap="word", yscrollcommand=bar.set)
        box.insert("1.0", spec["body"])
        box.configure(state="disabled")
        box.pack(side="left")
        bar.configure(command=box.yview)
    row = tk.Frame(root, pady=14)
    row.pack()
    deny = tk.Button(row, text="Deny", width=14, command=root.destroy)
    yes = tk.Button(row, text=spec.get("yes") or "Accept", width=14, state="disabled",
                    command=lambda: (accepted.append(True), root.destroy()))
    deny.pack(side="left", padx=10)
    yes.pack(side="left", padx=10)
    root.bind("<Escape>", lambda event: root.destroy())
    # A key press that lands on a new dialog cannot accept: the button is off at first, and the focus is on Deny.
    root.after(int(GUARD_S * 1000), lambda: yes.configure(state="normal"))
    root.after(int(float(spec["timeout"]) * 1000), root.destroy)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    deny.focus_set()
    root.lift()
    if spec.get("sound"):
        _sound(root)
    root.mainloop()
    if accepted:
        sys.stdout.write("accept\n")
    return 0 if accepted else 1


async def ask(title, text, body="", sound=False, yes="Accept", timeout=None):
    """Show the consent dialog in a child process. Each failure is a deny."""
    timeout = PENDING_S if timeout is None else timeout
    spec = json.dumps({"title": title, "text": text, "body": body, "sound": sound, "yes": yes, "timeout": timeout})
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    proc = None
    try:
        # -I: the child loads no module from the session directory
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", os.path.abspath(__file__), "--dialog",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **flags)
        out, _ = await asyncio.wait_for(proc.communicate(spec.encode()), timeout + 15)
        return proc.returncode == 0 and out.strip() == b"accept"
    except Exception:
        return False
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------- text, codes, tickets

def sanitize(text):
    """Peer text for the model: no control or format characters, no angle brackets."""
    kept = (ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return "".join(kept).replace("<", "‹").replace(">", "›")


def code_for(a, b):
    """Four digits from the two public keys, the same on the two sides."""
    low, high = sorted((bytes(a), bytes(b)))
    return "%04d" % (int.from_bytes(hashlib.sha256(b"peerlink code" + low + high).digest()[:4], "big") % 10000)


def make_ticket(endpoint_id, relay, secret):
    """Ticket: 32 bytes of public key, 16 bytes of secret, the relay URL."""
    raw = bytes.fromhex(endpoint_id) + bytes.fromhex(secret) + relay.encode()
    return "peer1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_ticket(ticket):
    try:
        if not isinstance(ticket, str) or not ticket.startswith("peer1."):
            raise ValueError
        body = ticket[6:].strip()
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        relay = raw[48:].decode()
        if len(raw) < 56 or len(relay) > 200 or not relay.startswith("https://"):
            raise ValueError
        return raw[:32].hex(), relay, raw[32:48].hex()
    except (ValueError, binascii.Error):
        raise PeerError("this is not a valid ticket") from None


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,31}")


def check_name(name):
    name = name.strip() if isinstance(name, str) else ""
    if not _NAME.fullmatch(name):
        raise PeerError("a name has 1 to 32 letters, digits, spaces, '_' or '-', and starts with a letter or a digit")
    return name


# ---------------------------------------------------------------- links

class Frames:
    """JSON lines from one QUIC stream, with a size limit."""

    def __init__(self, rx):
        self.rx, self.buf = rx, b""

    async def next(self):
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_FRAME:
                raise PeerError("frame too large")
            chunk = await self.rx.read(65536)
            if not chunk:
                raise PeerError("stream closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        frame = json.loads(line) if len(line) <= MAX_FRAME else None
        if not isinstance(frame, dict):
            raise PeerError("bad frame")
        return frame


@dataclass(eq=False)
class Link:
    name: str                  # the name that the local person gave
    peer: str                  # the public key of the peer, hex
    conn: object
    tx: object
    state: str = "pending"     # "pending" until the remote person accepts, then "open"
    expires: float = 0.0       # monotonic clock
    expires_wall: float = 0.0  # wall clock, so that a suspended computer does not extend the lease
    warned: bool = False
    renew_sent: bool = False   # we asked for a renewal and wait for the answer
    renew_busy: bool = False   # the renewal dialog of the peer's request is open
    dropped: int = 0           # inbound messages above a limit
    recv: deque = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list = field(default_factory=list)

    @property
    def key(self):
        return self.name.lower()


class PeerLink:
    """One endpoint with its tickets and links. `emit` gets one dict for each event. `ask` is the consent function."""

    def __init__(self, emit, ask=ask, session="", secret_key=None, relay_urls=None, audit_path=None):
        self.emit, self.ask = emit, ask
        self.session = re.sub(r"[^A-Za-z0-9 ._-]", "", session or "")[:40]
        self._secret_key, self._relay_urls, self._audit_path = secret_key, relay_urls, audit_path
        self.ep = None
        self.links = {}       # lower-case name -> Link
        self.known = {}       # lower-case name -> (name, peer key, relay URL or None) of peers that this helper accepted
        self.tickets = {}     # secret -> (name, expiry on the monotonic clock)
        self.requests = {}    # peer key -> times of its requests without a ticket
        self.pending = set()  # peer keys with a request that waits for the person
        self._tasks = set()
        self._start_lock = asyncio.Lock()
        self._dialog = asyncio.Lock()

    # ---- the four requests of the person

    async def invite(self, name):
        name = check_name(name)
        await self.start()
        if not await self._consent(f'Create a one-time invitation for "{name}"?\n\n'
                                   f"It is valid for {max(1, round(TICKET_S / 60))} minutes.", yes="Create"):
            raise PeerError("the local user refused in the dialog")
        now = time.monotonic()
        self.tickets = {s: t for s, t in self.tickets.items() if t[1] > now}
        secret = secrets.token_hex(16)
        self.tickets[secret] = (name, now + TICKET_S)
        return make_ticket(self.me, self.relay, secret)

    async def connect(self, name, ticket=None):
        name = check_name(name)
        link = self.links.get(name.lower())
        if ticket is None and link and link.state == "open":
            return await self._renew(link)
        if link:
            raise PeerError(f'a link or a request for "{link.name}" exists')
        if len(self.links) >= MAX_LINKS:
            raise PeerError(f"{MAX_LINKS} links is the maximum")
        if ticket is not None:
            peer, relay, secret = parse_ticket(ticket)
        elif name.lower() in self.known:
            name, peer, relay = self.known[name.lower()]
            secret = None
            if relay is None:
                raise PeerError(f'no relay is known for "{name}"; make a new invitation')
        else:
            raise PeerError(f'no ticket and no earlier link for "{name}"; ask the peer for a ticket')
        await self.start()
        if relay not in self.relays:
            raise PeerError("the ticket names a relay that is not in the relay list of this helper")
        code = code_for(bytes.fromhex(self.me), bytes.fromhex(peer))
        if not await self._consent(f'Connect this session{self._where()} to "{name}" for {self._minutes()} minutes?\n\n'
                                   f"Code {code}. The code must be the same on the two screens.", yes="Connect"):
            raise PeerError("the local user refused in the dialog")
        if name.lower() in self.links:
            raise PeerError(f'a link or a request for "{name}" exists')
        iroh = self._iroh
        try:
            addr = iroh.EndpointAddr(iroh.EndpointId.from_bytes(bytes.fromhex(peer)), relay, [])
            conn = await asyncio.wait_for(self.ep.connect(addr, ALPN), 30)
            bi = await asyncio.wait_for(conn.open_bi(), 10)
            link = Link(name, peer, conn, bi.send())
            hello = {"t": "hello", "relay": self.relay}
            if secret:
                hello["secret"] = secret
            await asyncio.wait_for(self._write(link, hello), 10)
        except Exception:
            raise PeerError(f'"{name}" did not answer; the ticket can be old, or the peer is not online') from None
        link.relay = relay
        link.expires = time.monotonic() + PENDING_S + PENDING_GRACE_S
        link.expires_wall = time.time() + PENDING_S + PENDING_GRACE_S
        self.links[link.key] = link
        link.tasks = [self._spawn(self._reader(link, Frames(bi.recv()))), self._spawn(self._lease(link))]
        return f'Request sent to "{name}". The result follows when the peer answers.'

    async def send(self, name, text):
        link = self._live(self.links.get(check_name(name).lower()), name)
        try:
            size = len(text.encode("utf-8")) if isinstance(text, str) and text.strip() else 0
        except UnicodeEncodeError:
            size = 0
        if size == 0:
            raise PeerError("the message is empty or is not valid text")
        if size > MAX_MSG_BYTES:
            raise PeerError(f"the message is larger than {MAX_MSG_BYTES} bytes")
        if not await self._consent(f'Send this message to "{link.name}"?', body=text, yes="Send"):
            raise PeerError("the local user refused this message in the dialog")
        self._live(link, name)
        await self._write(link, {"t": "msg", "text": text})
        return f'Sent to "{link.name}".'

    async def end(self, name):
        link = self.links.get(check_name(name).lower())
        if link is None:
            raise PeerError(f'no link to "{name}"')
        await self._end(link, "the local user ended the link", why="user")
        return f'The link to "{link.name}" is closed.'

    def status(self):
        now = time.monotonic()
        return [{"name": link.name, "state": link.state, "dropped": link.dropped,
                 "minutes_left": max(0, round((link.expires - now) / 60))} for link in self.links.values()]

    async def close(self):
        for link in list(self.links.values()):
            await self._end(link, "the helper stopped", why="user")
        for task in list(self._tasks):
            task.cancel()
        if self.ep is not None:
            ep, self.ep = self.ep, None
            try:
                await asyncio.wait_for(ep.close(), 5)
            except Exception:
                pass

    # ---- endpoint

    async def start(self):
        """Bind the endpoint. The helper makes no network connection before the first invitation or connection."""
        async with self._start_lock:
            if self.ep is not None:
                return
            import iroh
            mode = iroh.RelayMode.custom_from_urls(self._relay_urls) if self._relay_urls else iroh.RelayMode.default_mode()
            builder = iroh.EndpointBuilder()
            builder.apply_minimal()            # no address lookup service
            builder.relay_mode(mode)
            builder.alpns([ALPN])
            builder.bind_addr("127.0.0.1:0")   # relay only: no IP address of this machine goes to the peer
            if self._secret_key:
                builder.secret_key(self._secret_key)
            ep = await builder.bind()
            try:
                await asyncio.wait_for(ep.online(), 30)
            except Exception:
                await ep.close()
                raise PeerError("no connection to a relay; examine the network") from None
            self._iroh, self.ep = iroh, ep
            self.me = bytes(ep.id().to_bytes()).hex()
            self.relay = ep.addr().relay_url()
            self.relays = set(mode.relay_map().urls())
            self._spawn(self._accept_loop())

    async def _accept_loop(self):
        while True:
            try:
                incoming = await self.ep.accept_next()
            except Exception:
                return
            if incoming is None:
                return
            self._spawn(self._inbound(incoming))

    async def _inbound(self, incoming):
        """One inbound connection. Without a valid secret or an earlier accept: no dialog and no message."""
        conn = peer = None
        try:
            conn = await asyncio.wait_for((await incoming.accept()).connect(), 15)
            peer = bytes(conn.remote_id().to_bytes()).hex()
            bi = await asyncio.wait_for(conn.accept_bi(), 15)
            frames = Frames(bi.recv())
            hello = await asyncio.wait_for(frames.next(), 15)
            name = self._admit(peer, hello)
            if name is None:
                return
            self.pending.add(peer)
            code = code_for(bytes.fromhex(self.me), bytes.fromhex(peer))
            ok = await self._consent(
                f'Accept connection from "{name}" for this session{self._where()} for {self._minutes()} minutes?\n\n'
                f"Code {code}. The code must be the same on the two screens.", sound=True)
            link = Link(name, peer, conn, bi.send())
            old = self.links.get(link.key)
            if not ok or (old is None and len(self.links) >= MAX_LINKS):
                await asyncio.wait_for(self._write(link, {"t": "deny"}), 5)
                await asyncio.wait_for(conn.closed(), 1)   # let the peer read it
                return
            await asyncio.wait_for(self._write(link, {"t": "accept"}), 5)
            if old is not None:
                await self._end(old, "a new link replaced it", why="user")
            relay = hello.get("relay")
            link.relay = relay if isinstance(relay, str) and relay in self.relays else None
            self.links[link.key] = link
            self._open(link)
            link.tasks = [self._spawn(self._reader(link, frames)), self._spawn(self._lease(link))]
            conn = None   # the link owns the connection now
        except Exception:
            pass
        finally:
            self.pending.discard(peer)
            if conn is not None:
                try:
                    conn.close(0, b"")
                except Exception:
                    pass

    def _admit(self, peer, hello):
        """The local name for this request, or None for a request that gets no dialog."""
        if hello.get("t") != "hello" or peer in self.pending:
            return None
        secret = hello.get("secret")
        if isinstance(secret, str):
            name, expiry = self.tickets.pop(secret, (None, 0.0))   # one use
            return name if time.monotonic() < expiry else None
        for name, known_peer, _ in self.known.values():
            if known_peer == peer:
                return name if self._request_ok(peer) else None
        return None

    def _request_ok(self, peer):
        times, now = self.requests.setdefault(peer, deque()), time.monotonic()
        while times and now - times[0] > 3600:
            times.popleft()
        if len(times) >= REQ_PER_HOUR:
            return False
        times.append(now)
        return True

    # ---- one link

    def _open(self, link):
        link.state = "open"
        self._extend(link)
        self.known[link.key] = (link.name, link.peer, link.relay)
        self.emit({"ev": "open", "name": link.name, "minutes": self._minutes()})
        self._audit("accept", link)

    def _extend(self, link):
        link.expires, link.expires_wall, link.warned = time.monotonic() + LEASE_S, time.time() + LEASE_S, False

    async def _reader(self, link, frames):
        reason = None
        try:
            while True:
                frame = await frames.next()
                kind = frame.get("t")
                if link.state == "pending":
                    if kind != "accept":
                        break
                    self._open(link)
                elif kind == "msg":
                    self._on_msg(link, frame)
                elif kind == "renew":
                    self._spawn(self._on_renew(link))
                elif kind == "renewed" and link.renew_sent:   # a renewal counts only if we asked for it
                    link.renew_sent = False
                    self._extend(link)
                    self.emit({"ev": "renewed", "name": link.name, "minutes": self._minutes()})
                elif kind == "renew_denied" and link.renew_sent:
                    link.renew_sent = False
                    self.emit({"ev": "renew_denied", "name": link.name})
                elif kind == "bye":
                    reason = "the lease ended" if frame.get("why") == "lease" else "the peer ended the link"
                    break
        except Exception:
            pass
        if reason is None:
            reason = ("the peer refused the request, or the ticket is not valid" if link.state == "pending"
                      else "the peer closed the connection")
        await self._end(link, reason)

    async def _lease(self, link):
        while True:
            left = min(link.expires - time.monotonic(), link.expires_wall - time.time())
            if left <= 0:
                break
            if link.state == "open" and not link.warned and left <= WARN_S < LEASE_S:
                link.warned = True
                self.emit({"ev": "warn", "name": link.name, "minutes": max(1, round(left / 60))})
            await asyncio.sleep(min(left, 1.0))
        if link.state == "open":
            await self._end(link, "the lease ended", why="lease")
        else:
            await self._end(link, "the request got no answer", why="user")

    async def _end(self, link, reason, why=None):
        """Close one link. `why` is set when this side ends it; the peer then gets a last message."""
        if self.links.get(link.key) is not link:
            return
        del self.links[link.key]
        was_open, link.state = link.state == "open", "closed"
        if why:
            try:
                await asyncio.wait_for(self._write(link, {"t": "bye", "why": why}), 2)
                await asyncio.wait_for(link.conn.closed(), 1)   # let the peer read it
            except Exception:
                pass
        try:
            link.conn.close(0, b"")
        except Exception:
            pass
        for task in link.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self.emit({"ev": "closed" if was_open else "denied", "name": link.name, "reason": reason})
        if was_open:
            self._audit("end", link)

    def _on_msg(self, link, frame):
        text, now = frame.get("text"), time.monotonic()
        while link.recv and now - link.recv[0] > 60:
            link.recv.popleft()
        if (not isinstance(text, str) or len(text.encode("utf-8", "replace")) > MAX_MSG_BYTES
                or len(link.recv) >= RATE_PER_MIN):
            link.dropped += 1
            return
        link.recv.append(now)
        self.emit({"ev": "msg", "name": link.name, "text": sanitize(text), "mark": secrets.token_hex(4)})

    async def _on_renew(self, link):
        if link.renew_busy or not self._request_ok(link.peer):
            return
        link.renew_busy = True
        try:
            ok = await self._consent(f'Renew the connection with "{link.name}" for {self._minutes()} more minutes?',
                                     sound=True, yes="Renew")
            if self.links.get(link.key) is not link:
                return
            if ok:
                self._extend(link)
                await self._write(link, {"t": "renewed"})
                self.emit({"ev": "renewed", "name": link.name, "minutes": self._minutes()})
            else:
                await self._write(link, {"t": "renew_denied"})
        except Exception:
            pass
        finally:
            link.renew_busy = False

    async def _renew(self, link):
        if link.renew_sent:
            raise PeerError(f'a renewal request waits for "{link.name}"')
        if not await self._consent(f'Ask "{link.name}" for {self._minutes()} more minutes?', yes="Ask"):
            raise PeerError("the local user refused in the dialog")
        self._live(link, link.name)
        link.renew_sent = True
        await self._write(link, {"t": "renew"})
        return f'Renewal request sent to "{link.name}". The result follows when the peer answers.'

    # ---- small parts

    def _live(self, link, name):
        if link is None or self.links.get(link.key) is not link or link.state != "open":
            raise PeerError(f'no open link to "{name}"')
        return link

    async def _write(self, link, frame):
        data = json.dumps(frame, ensure_ascii=False).encode() + b"\n"
        async with link.lock:
            await link.tx.write_all(data)

    async def _consent(self, text, body="", sound=False, yes="Accept"):
        async with self._dialog:   # one dialog at a time
            try:
                return bool(await self.ask(TITLE, text, body=body, sound=sound, yes=yes))
            except Exception:
                return False

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _minutes(self):
        return max(1, round(LEASE_S / 60))

    def _where(self):
        return f' ("{self.session}")' if self.session else ""

    def _audit(self, event, link):
        """Metadata only: the name, the event and the time. No message text."""
        if not self._audit_path:
            return
        try:
            os.makedirs(os.path.dirname(self._audit_path), mode=0o700, exist_ok=True)
            line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "name": link.name})
            fd = os.open(self._audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["--dialog"]:
        sys.exit(dialog_main())
    sys.exit(f"usage: {os.path.basename(__file__)} --dialog")
