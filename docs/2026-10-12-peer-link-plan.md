# Peer Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code plugin that links one session to a session of a different person, with a popup consent and a 60-minute lease.

**Architecture:** A mod (`hooks/register.js`) gives the person the `/peer` command, catches `SendMessage` to `peer:<name>` and delivers inbound text as a prompt. A helper (`peerlink.py`) holds the iroh endpoint, the tickets, the leases, the limits and the Tk consent dialogs. The mod reads helper events from the helper output and sends requests through `peerlink.py ctl` and a per-user control socket.

**Tech Stack:** JavaScript for the mod (Claude Code mods API, version 2.1.287 or later). Python 3.10 or later for the helper, run by `uv run --managed-python --script`, with `iroh==1.1.0` and the standard library.

**Spec:** `docs/2026-10-12-peer-link-design.md`

This plan gives interfaces, tests and commands. It does not repeat the code, because one session writes the plan and the code.

## Global Constraints

- Lease 3600 s. Ticket 600 s. Pending request and dialog 300 s. Lease notice 300 s before the end. Accept button off for 1 s.
- Message limit 16384 bytes. Frame limit 65536 bytes. 30 inbound messages for each minute. 3 requests for each hour for each peer. 4 links maximum.
- Names: `^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$`.
- The helper binds to `127.0.0.1` and uses the iroh minimal preset with the default relay map.
- No product switch turns off a dialog. Tests replace the `ask` function in the test process.
- No peer text in a dialog. No ticket in the model context.
- Each new directory has a `README.md` with links one level up and one level down.
- Commits use `ZoreAnuj <95142805+ZoreAnuj@users.noreply.github.com>` and have no attribution line.
- All text is in Simplified Technical English.

## Review Focus

1. A peer sends `renewed` without a request. The lease must not change. Test `one_sided_renewal`.
2. A peer puts end marks or instructions in its text. The marks have a random number from the helper. Test `inbound` in the mod tests and `unsafe_text` in the helper tests.
3. The person refuses the dialog after Claude called `SendMessage`. Claude must read a refusal, not "delivered". Test `send` in the mod tests and `local_refusal` in the helper tests.
4. The helper stops while a link is open. The mod must forget the links, so that `SendMessage` gets a refusal. Test `helper_stop` in the mod tests.
5. The computer has no display or no Tk. Each consent must be a deny in a short time. Test `no_display`.

---

### Task 1: Helper core and its tests

**Files:** Create `peerlink.py`, `tests/test_peerlink.py`, `tests/README.md`.

**Interfaces (produces):**

```python
def sanitize(text: str) -> str            # no control or format characters, "<" and ">" replaced
def code_for(a: bytes, b: bytes) -> str   # four digits, the same for (a, b) and (b, a)
def make_ticket(endpoint_id: str, relay: str, secret: str) -> str   # "peer1." + base64url
def parse_ticket(ticket: str) -> tuple[str, str, str]               # raises PeerError
def check_name(name: str) -> str          # raises PeerError
async def ask(title, text, body="", sound=False, yes="Accept", timeout=None) -> bool   # Tk child process
class PeerError(Exception)
class PeerLink:
    def __init__(self, emit, ask=ask, session="", secret_key=None, relay_urls=None)
    async def invite(self, name) -> str               # ticket
    async def connect(self, name, ticket=None) -> str
    async def send(self, name, text) -> str
    async def end(self, name) -> str
    def status(self) -> list[dict]
    async def close(self) -> None
```

`emit` receives one dict for each event: `open`, `closed`, `denied`, `msg` (with `text` and `mark`), `warn`, `renewed`, `renew_denied`.

- [ ] Write the tests of the table "Helper tests" in the spec, without "Control socket".
- [ ] Run `uv run --managed-python --script tests/test_peerlink.py`. The tests fail because `peerlink.py` does not exist.
- [ ] Write `peerlink.py`.
- [ ] Run the tests again. All tests pass.
- [ ] Run `PEERLINK_TEST_DIALOG=1 uv run --managed-python --script tests/test_peerlink.py dialog` one time. One window shows for 2 s.
- [ ] Commit.

### Task 2: Control socket

**Files:** Modify `peerlink.py`, `tests/test_peerlink.py`.

**Interfaces (produces):**

```
peerlink.py serve [label]    events on standard output; first event {"ev":"ready","ctl":address,"key":hex}; {"ev":"hb"} each 10 s
peerlink.py ctl              standard input {"ctl":address,"key":hex,"req":{"op":...}}; standard output {"ok":true,"result":...} or {"ok":false,"error":"..."}
peerlink.py --dialog         standard input: the dialog as JSON; exit code 0 and "accept" only for an accept
```

Requests: `{"op":"invite","name"}`, `{"op":"connect","name","ticket"?}`, `{"op":"send","name","text"}`, `{"op":"end","name"}`, `{"op":"status"}`.

- [ ] Write the test "Control socket": start `serve` as a process, read `ready`, send `status` with the key, then with a wrong key.
- [ ] Run it. It fails.
- [ ] Add `serve` and `ctl` with `multiprocessing.connection` (`AF_UNIX` in a `mkdtemp` directory, `AF_PIPE` on Windows) and `send_bytes` / `recv_bytes` only.
- [ ] Run all tests. All tests pass.
- [ ] Commit.

### Task 3: Mod and its tests

**Files:** Create `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `hooks/hooks.json`, `hooks/register.js`, `hooks/README.md`, `tests/peer-link.test.ts`.

**Interfaces (consumes):** the `serve` and `ctl` interfaces of Task 2.

Hooks: `session.start` (registers `/peer`), `command.run` for `peer`, `session.send`, `prompt.submit`.

- [ ] Write the tests of the table "Mod tests" in the spec, and the test `helper_stop`.
- [ ] Run `claude plugin test`. The tests fail.
- [ ] Write the mod. Run `claude plugin validate .`.
- [ ] Run `claude plugin test`. All tests pass.
- [ ] Commit.

### Task 4: End-to-end test on Linux

**Files:** Create `tests/autoaccept_helper.py`.

- [ ] Make a copy of the plugin in a temporary directory. In the copy, `peerlink.py` starts the real helper with `ask` replaced by an accept.
- [ ] Start two sessions with `--plugin-dir <copy>`. Type `/peer invite Bob` in the first and `/peer connect Alice <ticket>` in the second.
- [ ] Ask the second session to send a question to `peer:Alice`. Pass: the first session shows the framed message and its answer arrives in the second session.
- [ ] Record the result in `tests/README.md`. Commit.

### Task 5: Three operating systems and two networks

**Files:** Create `.github/workflows/ci.yml`, `tests/cross_accept.py`, `tests/cross_dial.py`.

- [ ] The workflow runs the helper tests on `ubuntu-latest`, `macos-latest` and `windows-latest`, with the real dialog (`xvfb-run` on Linux). Actions are pinned to commit hashes.
- [ ] Push. Pass: three green jobs.
- [ ] A commit message that starts with `[cross]` also starts `tests/cross_accept.py` on each runner. The laptop runs `tests/cross_dial.py <run id>`. Pass: a link opens to each runner, one message goes each way, and the path is the relay.
- [ ] Record the result in `tests/README.md`. Commit.

### Task 6: Documents and pull request

**Files:** Modify `README.md`, `docs/README.md`.

- [ ] `README.md`: what it is, install, use, security rules, limits, test commands.
- [ ] Push to `main`. The owner said on 2026-10-12: no branch and no pull request are necessary.

### Task 7: Review fixes and saved contacts (version 0.2.0)

Added on 2026-10-12, after the code review and the owner's request for a later request with no ticket.

**Files:** Modify `peerlink.py`, `hooks/register.js`, the two test files, the spec, the README files.

**Interfaces (changes):**

```python
def make_ticket(endpoint_id: str, secret: str) -> str        # "peer2." + base64url, no relay
def parse_ticket(ticket: str) -> tuple[str, str]
class PeerLink:
    def __init__(self, emit, ask=ask, session="", secret_key=None, relay_urls=None, home=None)
    async def listen(self) -> str
    async def forget(self, name) -> str
    def contact_names(self) -> list[str]
```

Requests: `{"op":"listen"}`, `{"op":"forget","name"}`. `status` answers `{"links": [...], "contacts": [...]}`.

- [x] A failing test for each review finding: one link for each name, renewal time, one pending request, loopback only, silent control client, dialog time, no text after the end.
- [x] Failing tests for saved contacts: no ticket after a restart, one helper for each identity, forget.
- [x] The fixes and the feature in `peerlink.py`. 28 helper tests pass.
- [x] Mod: `listen`, `forget`, the new `status`, no helper start for `end`, `forget` and `status`, text only for open links, empty `PYTHONPATH`. 19 mod tests pass.
- [x] End-to-end with two real sessions: first link with a ticket, second request with no ticket, the same after a restart.
- [ ] Push to `main`. CI on three systems and the cross-network test.
