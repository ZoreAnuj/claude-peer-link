#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["iroh==1.1.0"]
# ///
"""Peer link helper: transport, tickets, leases, limits and consent dialogs.

  peerlink.py serve [label]   started by the mod: events on standard output, requests on a control socket
  peerlink.py ctl             one request to a running helper: JSON on standard input, JSON on standard output
  peerlink.py --dialog        the consent dialog, a child of the helper: JSON on standard input

Design: docs/2026-10-12-peer-link-design.md
"""
import asyncio
import base64
import binascii
import hashlib
import json
import multiprocessing.connection
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

ALPN = b"peerlink/1"
LEASE_S = 3600.0         # an accepted link ends after this time
TICKET_S = 600.0         # an invitation is valid for this time and for one use
PENDING_S = 300.0        # a request or a dialog without an answer is a deny
PENDING_GRACE_S = 30.0   # the dialer waits this much longer than the dialog of the peer
DIAL_S = 20.0            # a dial through the relays gets this time
WARN_S = 300.0           # the person gets a notice this long before the lease end
GUARD_S = 1.0            # the accept button is off for this time
MAX_MSG_BYTES = 16384
MAX_FRAME = 65536
RATE_PER_MIN = 30        # inbound messages for each link
REQ_PER_HOUR = 3         # requests without a ticket, and renewal requests, for each peer
MAX_LINKS = 4
BINDS = (("127.0.0.1:0", "[::1]:0"), ("127.0.0.1:0",))   # the second is for a machine without IPv6
HEARTBEAT_S = 10.0       # the helper stops when it cannot write this line to the mod
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
    """Show one consent dialog. Exit code 0 and the word "accept" only if the person presses the accept button.

    Exit code 1 is a deny or no answer. Exit code 2 means that no dialog is possible."""
    spec = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))   # Windows PowerShell 5.1 pipes a byte-order mark first
    accepted = []
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        return 2   # no display, or Python without Tk
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
        if proc.returncode == 2:   # the mod shows lines with this mark to the person
            print("peerlink: no consent dialog is possible on this machine (no display, or Python without Tk). "
                  "Each consent is a deny.", file=sys.stderr, flush=True)
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

_HIDDEN = ("Cc", "Cf", "Cs", "Co")
_LINES = str.maketrans({chr(0x2028): "\n", chr(0x2029): "\n"})   # line and paragraph separator
_BRACKETS = str.maketrans({ch: chr(0x2039) for ch in ("<", chr(0xFF1C), chr(0xFE64))}
                          | {ch: chr(0x203A) for ch in (">", chr(0xFF1E), chr(0xFE65))})


def visible(text):
    """Text without characters that a person cannot see in a dialog: control, format and hidden characters."""
    return "".join(ch for ch in text.translate(_LINES)
                   if ch in "\n\t" or (unicodedata.category(ch) not in _HIDDEN and not 0xE0100 <= ord(ch) <= 0xE01EF))


def sanitize(text):
    """Peer text for the model: only visible characters, and no angle brackets."""
    return visible(text).translate(_BRACKETS)


def code_for(a, b):
    """Four digits from the two public keys, the same on the two sides."""
    low, high = sorted((bytes(a), bytes(b)))
    return "%04d" % (int.from_bytes(hashlib.sha256(b"peerlink code" + low + high).digest()[:4], "big") % 10000)


def make_ticket(endpoint_id, secret):
    """Ticket: 32 bytes of public key and 16 bytes of secret."""
    raw = bytes.fromhex(endpoint_id) + bytes.fromhex(secret)
    return "peer2." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_ticket(ticket):
    try:
        if not isinstance(ticket, str) or not ticket.startswith("peer2."):
            raise ValueError
        body = ticket[6:].strip()
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if len(raw) != 48:
            raise ValueError
        return raw[:32].hex(), raw[32:].hex()
    except (ValueError, binascii.Error):
        raise PeerError("this is not a valid ticket") from None


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,31}")


def check_name(name):
    name = name.strip() if isinstance(name, str) else ""
    if not _NAME.fullmatch(name):
        shown = name if len(name) <= 40 else name[:40] + "..."
        raise PeerError(f'"{shown}" is not a valid name: a name has 1 to 32 letters, digits, spaces, '
                        "'_' or '-', and starts with a letter or a digit")
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
    state: str = "pending"     # "pending" until the remote person accepts, then "open", then "closed"
    expires: float = 0.0       # monotonic clock
    expires_wall: float = 0.0  # wall clock, so that a suspended computer does not extend the lease
    warned: bool = False
    renew_until: float = 0.0   # we asked for a renewal: an answer counts until this time
    renew_busy: bool = False   # the renewal dialog of the peer's request is open
    dropped: int = 0           # inbound messages above a limit
    recv: deque = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list = field(default_factory=list)

    @property
    def key(self):
        return self.name.lower()


class PeerLink:
    """One endpoint with its tickets, contacts and links.

    `emit` gets one dict for each event. `ask` is the consent function.
    `home` is the directory of the identity key and the contacts; without it, the two stay in memory.
    """

    def __init__(self, emit, ask=ask, session="", secret_key=None, relay_urls=None, home=None):
        self.emit, self.ask, self.home = emit, ask, home
        self.session = re.sub(r"[^A-Za-z0-9 ._-]", "", session or "")[:40]
        self._secret_key, self._relay_urls = secret_key, relay_urls
        self.ep = None
        self.links = {}       # lower-case name -> Link
        self.contacts = {}    # lower-case name -> {"name", "key"}: peers that this identity accepted
        self.tickets = {}     # secret -> (name, expiry on the monotonic clock)
        self.requests = {}    # peer key -> times of its requests without a ticket
        self.pending = set()  # peer keys with a request that waits for the person
        self._tasks = set()
        self._lock_file = None
        self._start_lock = asyncio.Lock()
        self._dialog = asyncio.Lock()

    # ---- the requests of the person

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
        return make_ticket(self.me, secret)

    async def listen(self):
        """Start the endpoint, so that saved contacts can ask for a link. No ticket is made."""
        await self.start()
        names = self.contact_names()
        if not names:
            return "This session listens. No contact is saved: make the first link with /peer invite."
        return "This session listens for requests of the saved contacts: " + ", ".join(names) + "."

    async def connect(self, name, ticket=None):
        """With a ticket: the first link to a peer. Without: a renewal, or a new link to a saved contact."""
        name = check_name(name)
        key = name.lower()
        link = self.links.get(key)
        if ticket is None and link is not None and link.state == "open":
            return await self._renew(link)
        if link is not None:
            raise PeerError(f'a link or a request for "{link.name}" exists')
        if len(self.links) >= MAX_LINKS:
            raise PeerError(f"{MAX_LINKS} links is the maximum")
        peer, secret = parse_ticket(ticket) if ticket is not None else (None, None)
        await self.start()
        if secret is not None:
            code = code_for(bytes.fromhex(self.me), bytes.fromhex(peer))
            if not await self._consent(
                    f'Connect this session{self._where()} to "{name}" for {self._minutes()} minutes?\n\n'
                    f"Code {code}. The code must be the same on the two screens.{self._replaces(name, peer)}", yes="Connect"):
                raise PeerError("the local user refused in the dialog")
        elif key in self.contacts:
            # the person typed the command for a saved contact: that is the consent of this side
            name, peer = self.contacts[key]["name"], self.contacts[key]["key"]
        else:
            raise PeerError(f'no ticket and no saved contact for "{name}"; ask the peer for a ticket')
        if key in self.links:
            raise PeerError(f'a link or a request for "{name}" exists')
        conn = await self._dial(peer, name)
        try:
            bi = await asyncio.wait_for(conn.open_bi(), 10)
            link = Link(name, peer, conn, bi.send())
            await asyncio.wait_for(self._write(link, {"t": "hello", "secret": secret} if secret else {"t": "hello"}), 10)
            if key in self.links:   # a request of the peer was accepted during the dial: one link for each name
                raise PeerError(f'a link to "{name}" opened during the dial')
        except PeerError:
            conn.close(0, b"")
            raise
        except Exception:
            conn.close(0, b"")
            raise PeerError(f'"{name}" did not answer') from None
        link.expires = time.monotonic() + PENDING_S + PENDING_GRACE_S
        link.expires_wall = time.time() + PENDING_S + PENDING_GRACE_S
        self.links[key] = link   # no await after the look at the table
        link.tasks = [self._spawn(self._reader(link, Frames(bi.recv()))), self._spawn(self._lease(link))]
        return f'Request sent to "{name}". The result follows when the peer answers.'

    async def send(self, name, text):
        link = self._live(self.links.get(check_name(name).lower()), name)
        # The person approves the text in a dialog, so only characters that a dialog shows can leave.
        clean = visible(text) if isinstance(text, str) else ""
        if not clean.strip():
            raise PeerError("the message is empty or is not valid text")
        if len(clean.encode("utf-8")) > MAX_MSG_BYTES:
            raise PeerError(f"the message is larger than {MAX_MSG_BYTES} bytes")
        hidden = len(text) - len(clean)
        note = f"\n\n{hidden} hidden characters were removed." if hidden else ""
        if not await self._consent(f'Send this message to "{link.name}"?{note}', body=clean, yes="Send"):
            raise PeerError("the local user refused this message in the dialog")
        self._live(link, name)
        await self._write(link, {"t": "msg", "text": clean})
        return f'Sent to "{link.name}".'

    async def end(self, name):
        link = self.links.get(check_name(name).lower())
        if link is None:
            raise PeerError(f'no link to "{name}"')
        await self._end(link, "the local user ended the link", why="user")
        return f'The link to "{link.name}" is closed.'

    async def forget(self, name):
        """Remove a saved contact. Its key gets no dialog after this, and a new link needs a new ticket."""
        key = check_name(name).lower()
        self._load()
        contact, link = self.contacts.get(key), self.links.get(key)
        if contact is None and link is None:
            raise PeerError(f'no contact with the name "{name}"')
        peer = contact["key"] if contact else link.peer
        names = [c["name"] for c in self.contacts.values() if c["key"] == peer] or [link.name]
        self.contacts = {k: c for k, c in self.contacts.items() if c["key"] != peer}   # each name of this key
        self._save_contacts()
        for other in [x for x in self.links.values() if x.peer == peer]:
            await self._end(other, "the local user removed the contact", why="user")
        return " and ".join(f'"{n}"' for n in names) + " removed. A new link to this peer needs a new ticket."

    def status(self):
        now = time.monotonic()
        return [{"name": link.name, "state": link.state, "dropped": link.dropped,
                 "minutes_left": max(0, round((link.expires - now) / 60))} for link in self.links.values()]

    def contact_names(self):
        self._load()
        return sorted(contact["name"] for contact in self.contacts.values())

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
        if self._lock_file is not None:   # a different session can use the identity now
            self._lock_file.close()
            self._lock_file = None

    # ---- identity and contacts on disk

    def _load(self):
        """Take the identity of the home directory: one helper at a time. Read the key and the contacts."""
        if not self.home or self._lock_file is not None:
            return
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.home, 0o700)
        lock = open(os.path.join(self.home, "lock"), "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise PeerError("the peer link of this machine is in use by a different session. "
                            "Use that session, or end it") from None
        try:
            try:
                with open(os.path.join(self.home, "identity.key")) as f:
                    self._secret_key = bytes.fromhex(f.read().strip())
                if len(self._secret_key) != 32:
                    raise ValueError
            except FileNotFoundError:
                self._secret_key = secrets.token_bytes(32)
                self._write_private("identity.key", self._secret_key.hex())
            try:
                with open(os.path.join(self.home, "contacts.json")) as f:
                    saved = json.load(f)
                self.contacts = {k: {"name": v["name"], "key": v["key"]} for k, v in saved.items()}
            except FileNotFoundError:
                self._write_private("contacts.json", "{}")
        except (ValueError, KeyError, TypeError, AttributeError):
            lock.close()
            raise PeerError(f"a file in {self.home} is damaged. Remove identity.key and contacts.json "
                            "to make a new identity; each peer then needs a new ticket") from None
        self._lock_file = lock

    def _write_private(self, name, text):
        """Write a file that only the owner can read, in one step."""
        path = os.path.join(self.home, name)
        fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(path + ".new", path)

    def _save_contacts(self):
        if self.home:
            self._write_private("contacts.json", json.dumps(self.contacts, indent=1))

    # ---- endpoint

    async def start(self):
        """Bind the endpoint. The helper makes no network connection before this."""
        async with self._start_lock:
            if self.ep is not None:
                return
            self._load()
            import iroh
            mode = iroh.RelayMode.custom_from_urls(self._relay_urls) if self._relay_urls else iroh.RelayMode.default_mode()
            ep = None
            # Relay only: no socket on an address of this machine that a peer can reach, so no IP address goes to the peer.
            for binds in BINDS:
                builder = iroh.EndpointBuilder()
                builder.apply_minimal()   # no address lookup service
                builder.relay_mode(mode)
                builder.alpns([ALPN])
                for address in binds:
                    builder.bind_addr(address)
                if self._secret_key:
                    builder.secret_key(self._secret_key)
                try:
                    ep = await builder.bind()
                    break
                except Exception:
                    continue
            try:
                if ep is None:
                    raise PeerError("the endpoint did not start")
                if not all(str(address).startswith(("127.", "[::1]:")) for address in ep.bound_sockets()):
                    raise PeerError("the endpoint has a socket that is not on a loopback address. "
                                    "The peer link does not start, because a peer could see an IP address of this machine")
                await asyncio.wait_for(ep.online(), 30)
            except Exception as e:
                if ep is not None:
                    await ep.close()
                if self._lock_file is not None:
                    self._lock_file.close()
                    self._lock_file = None
                raise e if isinstance(e, PeerError) else PeerError("no connection to a relay; examine the network") from None
            self._iroh, self.ep = iroh, ep
            self.me = bytes(ep.id().to_bytes()).hex()
            self.relay = ep.addr().relay_url()
            self.relays = sorted(mode.relay_map().urls())
            self._audit_path = os.path.join(self.home, "audit.log") if self.home else None
            self._spawn(self._accept_loop())

    async def _dial(self, peer, name):
        """Dial the key through each relay of the list at the same time. The peer is on one of them."""
        iroh = self._iroh
        endpoint_id = iroh.EndpointId.from_bytes(bytes.fromhex(peer))
        dials = [asyncio.ensure_future(self.ep.connect(iroh.EndpointAddr(endpoint_id, relay, []), ALPN))
                 for relay in self.relays]
        winner = None
        try:
            for first in asyncio.as_completed(dials, timeout=DIAL_S):
                try:
                    winner = await first
                    return winner
                except asyncio.TimeoutError:
                    break
                except Exception:
                    continue
        finally:
            for dial in dials:
                if not dial.done():
                    dial.cancel()
                elif not dial.cancelled() and dial.exception() is None and dial.result() is not winner:
                    dial.result().close(0, b"")
        raise PeerError(f'"{name}" did not answer. The peer is not online, or its session does not listen')

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
        """One inbound connection. Without a valid secret or a saved contact: no dialog and no message."""
        conn = peer = None
        waits = False
        try:
            conn = await asyncio.wait_for((await incoming.accept()).connect(), 15)
            peer = bytes(conn.remote_id().to_bytes()).hex()
            bi = await asyncio.wait_for(conn.accept_bi(), 15)
            frames = Frames(bi.recv())
            hello = await asyncio.wait_for(frames.next(), 15)
            admitted = self._admit(peer, hello)
            if admitted is None:
                return
            name, saved = admitted
            self.pending.add(peer)
            waits = True
            code = code_for(bytes.fromhex(self.me), bytes.fromhex(peer))
            origin = "a saved contact" if saved else "new, with your invitation"
            ok = await self._consent(
                f'Accept connection from "{name}" ({origin}) for this session{self._where()} '
                f"for {self._minutes()} minutes?\n\nCode {code}. The code must be the same on the two screens."
                f"{self._replaces(name, peer)}", sound=True)
            link = Link(name, peer, conn, bi.send())
            if not ok:
                await asyncio.wait_for(self._write(link, {"t": "deny"}), 5)
                await asyncio.wait_for(conn.closed(), 1)   # let the peer read it
                return
            await asyncio.wait_for(self._write(link, {"t": "accept"}), 5)
            # From here to the end of the block there is no await: one link for each name, each under a lease.
            old = self.links.get(link.key)
            if old is not None:
                self._detach(old, "a new link replaced it")
            self.links[link.key] = link
            self._open(link)
            link.tasks = [self._spawn(self._reader(link, frames)), self._spawn(self._lease(link))]
            conn = None   # the link owns the connection now
            if old is not None:
                self._spawn(self._goodbye(old, "user"))
        except Exception:
            pass
        finally:
            if waits:   # only the task that put the peer in the set takes it out
                self.pending.discard(peer)
            if conn is not None:
                try:
                    conn.close(0, b"")
                except Exception:
                    pass

    def _admit(self, peer, hello):
        """(local name, saved contact or not) for this request, or None for a request that gets no dialog."""
        if hello.get("t") != "hello" or peer in self.pending:
            return None
        secret = hello.get("secret")
        if isinstance(secret, str):
            name, expiry = self.tickets.pop(secret, (None, 0.0))   # one use
            found = (name, False) if time.monotonic() < expiry else None
        else:
            name = next((c["name"] for c in self.contacts.values() if c["key"] == peer), None)
            found = (name, True) if name is not None and self._request_ok(peer) else None
        if found is None:
            return None
        mine = self.links.get(found[0].lower())
        if mine is None:
            return found if len(self.links) < MAX_LINKS else None
        if mine.state == "pending" and mine.peer == peer and self.me > peer:
            return None   # the two sides asked at the same time: the request of the side with the higher key is used
        return found

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
        self.contacts[link.key] = {"name": link.name, "key": link.peer}   # the two keys are now paired
        try:
            self._save_contacts()
        except OSError as e:   # the mod shows lines with this mark to the person
            print(f"peerlink: the contact could not be saved ({type(e).__name__}). "
                  "After a restart, this peer needs a new ticket.", file=sys.stderr, flush=True)
        self.emit({"ev": "open", "name": link.name, "minutes": self._minutes()})
        self._audit("accept", link)

    def _extend(self, link):
        link.expires, link.expires_wall, link.warned = time.monotonic() + LEASE_S, time.time() + LEASE_S, False

    async def _reader(self, link, frames):
        reason = None
        try:
            while link.state != "closed":
                frame = await frames.next()
                kind = frame.get("t")
                if link.state == "closed":
                    return
                if link.state == "pending":
                    if kind != "accept":
                        break
                    self._open(link)
                elif kind == "msg":
                    self._on_msg(link, frame)
                elif kind == "renew":
                    self._spawn(self._on_renew(link))
                elif kind == "renewed" and time.monotonic() < link.renew_until:   # only an answer to our request counts
                    link.renew_until = 0.0
                    self._extend(link)
                    self.emit({"ev": "renewed", "name": link.name, "minutes": self._minutes()})
                elif kind == "renew_denied" and link.renew_until:
                    link.renew_until = 0.0
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

    def _detach(self, link, reason):
        """Take one link out of use at once, with no await. False if it was closed before."""
        if link.state == "closed":
            return False
        was_open, link.state = link.state == "open", "closed"
        if self.links.get(link.key) is link:
            del self.links[link.key]
        for task in link.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self.emit({"ev": "closed" if was_open else "denied", "name": link.name, "reason": reason})
        if was_open:
            self._audit("end", link)
        return True

    async def _goodbye(self, link, why=None):
        """Close the connection of a detached link. `why` is set when this side ends it: the peer gets a last message."""
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

    async def _end(self, link, reason, why=None):
        if self._detach(link, reason):
            await self._goodbye(link, why)

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
        """The peer asks for a renewal. Only the dialog of the local person extends the lease of this side."""
        if link.renew_busy:
            return
        link.renew_busy = True
        try:
            ok = self._request_ok(link.peer) and await self._consent(
                f'Renew the connection with "{link.name}" for {self._minutes()} more minutes?', sound=True, yes="Renew")
            if link.state != "open":
                return
            if ok:
                self._extend(link)
                await self._write(link, {"t": "renewed"})
                self.emit({"ev": "renewed", "name": link.name, "minutes": self._minutes()})
            else:
                await self._write(link, {"t": "renew_denied"})   # the asker can ask again later
        except Exception:
            pass
        finally:
            link.renew_busy = False

    async def _renew(self, link):
        """The person typed the command for an open link: that is the consent of this side."""
        now = time.monotonic()
        if now < link.renew_until:
            raise PeerError(f'a renewal request waits for "{link.name}"')
        link.renew_until = now + PENDING_S + PENDING_GRACE_S   # an answer after this time does not count
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
        """One dialog at a time. The wait for a free dialog and the dialog together take PENDING_S at most."""
        end = time.monotonic() + PENDING_S
        try:
            await asyncio.wait_for(self._dialog.acquire(), PENDING_S)
        except asyncio.TimeoutError:
            return False
        try:
            left = end - time.monotonic()
            if left < min(5.0, PENDING_S / 2):   # too short to read a dialog
                return False
            return bool(await self.ask(TITLE, text, body=body, sound=sound, yes=yes, timeout=left))
        except Exception:
            return False
        finally:
            self._dialog.release()

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _minutes(self):
        return max(1, round(LEASE_S / 60))

    def _replaces(self, name, peer):
        """A line for a dialog, if the accept changes the key of a saved contact."""
        saved = self.contacts.get(name.lower())
        if saved is None or saved["key"] == peer:
            return ""
        return f'\n\nThis replaces your saved contact "{saved["name"]}", which has a different key.'

    def _where(self):
        return f' ("{self.session}")' if self.session else ""

    def _audit(self, event, link):
        """Metadata only: the name, the event and the time. No message text."""
        if not getattr(self, "_audit_path", None):
            return
        try:
            line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "name": link.name})
            fd = os.open(self._audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------- control socket and helper process

CTL_FAMILY = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def _ctl_address():
    """A new address for this user only: a pipe name on Windows, or a socket in a new directory with mode 0700."""
    if sys.platform == "win32":
        return r"\\.\pipe\peerlink-%d-%s" % (os.getpid(), secrets.token_hex(8))
    return os.path.join(tempfile.mkdtemp(prefix="peerlink-", dir=os.environ.get("XDG_RUNTIME_DIR")), "ctl.sock")


async def _dispatch(pl, req):
    try:
        op = req.get("op")
        if op == "invite":
            result = await pl.invite(req.get("name"))
        elif op == "connect":
            result = await pl.connect(req.get("name"), req.get("ticket"))
        elif op == "send":
            result = await pl.send(req.get("name"), req.get("text"))
        elif op == "end":
            result = await pl.end(req.get("name"))
        elif op == "listen":
            result = await pl.listen()
        elif op == "forget":
            result = await pl.forget(req.get("name"))
        elif op == "status":
            result = {"links": pl.status(), "contacts": pl.contact_names()}
        elif op == "stop":   # the control thread stops the helper after it sent this answer
            result = "The helper stops. This session does not listen now."
        else:
            raise PeerError("unknown request")
        return {"ok": True, "result": result}
    except PeerError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"helper error: {type(e).__name__}"}


def _ctl_serve(listener, loop, pl, key, stop):
    """Thread: one short connection for each request, each in its own thread.

    The key is proved in that thread before a request is read, so a silent client stops only itself."""
    slots = threading.BoundedSemaphore(16)

    def handle(conn):
        try:
            multiprocessing.connection.deliver_challenge(conn, key)
            multiprocessing.connection.answer_challenge(conn, key)
            req = json.loads(conn.recv_bytes(32 * MAX_FRAME))
            req = req if isinstance(req, dict) else {}
            conn.send_bytes(json.dumps(asyncio.run_coroutine_threadsafe(_dispatch(pl, req), loop).result()).encode())
            if req.get("op") == "stop":
                loop.call_soon_threadsafe(stop.set)
        except Exception:   # a wrong key, a client that left, a request that is not JSON
            pass
        finally:
            conn.close()
            slots.release()

    while not loop.is_closed():
        try:
            conn = listener.accept()
        except Exception:   # a closed listener
            time.sleep(0.05)
            continue
        if slots.acquire(blocking=False):
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        else:
            conn.close()


async def serve(label):
    """The helper process. It lives as long as the mod reads its output."""
    loop, stop, out = asyncio.get_running_loop(), asyncio.Event(), sys.stdout.buffer

    def emit(event):
        try:
            out.write(json.dumps(event).encode() + b"\n")
            out.flush()
        except (OSError, ValueError):   # the mod is gone
            stop.set()
            try:   # a second write error at interpreter exit would change the exit code
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except (OSError, ValueError):
                pass

    home = os.environ.get("PEERLINK_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "peer-link")
    pl = PeerLink(emit, session=label, home=home)
    key, address = secrets.token_bytes(32), _ctl_address()
    listener = multiprocessing.connection.Listener(address, family=CTL_FAMILY)
    threading.Thread(target=_ctl_serve, args=(listener, loop, pl, key, stop), daemon=True).start()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, stop.set)   # close the links with a last message
    emit({"ev": "ready", "ctl": address, "key": key.hex()})
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                emit({"ev": "hb"})
    finally:
        await pl.close()
        listener.close()
        if sys.platform != "win32":
            shutil.rmtree(os.path.dirname(address), ignore_errors=True)
    return 0


def ctl_main():
    """One request to a running helper."""
    msg = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    try:
        with multiprocessing.connection.Client(msg["ctl"], family=CTL_FAMILY, authkey=bytes.fromhex(msg["key"])) as conn:
            conn.send_bytes(json.dumps(msg["req"]).encode())
            answer, code = conn.recv_bytes(), 0
    except Exception as e:   # a wrong key, or no helper at this address
        answer, code = json.dumps({"ok": False, "error": f"no answer from the helper ({type(e).__name__})"}).encode(), 1
    sys.stdout.buffer.write(answer + b"\n")
    return code


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--dialog":
        sys.exit(dialog_main())
    if mode == "ctl":
        sys.exit(ctl_main())
    if mode == "serve":
        sys.exit(asyncio.run(serve(" ".join(sys.argv[2:]))))
    sys.exit(f"usage: {os.path.basename(__file__)} serve [label] | ctl | --dialog")
