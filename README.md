# claude-peer-link

A Claude Code plugin that links one session to a Claude Code session of a different person, through the internet.
A person accepts each connection in a desktop popup. An accepted link ends after 60 minutes.

Status: version 0.2.0. The tests pass. A trial with a second person is not done yet.

## How it works

- You type `/peer invite Bob`. You send the ticket to Bob through a private channel.
- Bob types `/peer connect Alice <ticket>` in his session.
- You get a popup with a sound: "Accept connection from Bob for this session for 60 minutes?"
- After the accept, Claude on each side sends text with its normal `SendMessage` tool, to the address `peer:Bob` or `peer:Alice`.
- You approve each outgoing message in a popup that shows the full text.
- A message from the peer starts a turn in your session. A note tells Claude that the text is untrusted and comes from a different person.
- After 60 minutes the link closes.
- The first link saves the contact on each side. From then on, `/peer connect Bob` asks again with no ticket: one command for you, one popup for Bob.

The traffic is encrypted from end to end (QUIC with TLS 1.3 through iroh). It goes through a relay that sees only ciphertext.

## Install

Each person needs:

- Claude Code 2.1.287 or later, on Linux x86-64 or ARM64, macOS on Apple silicon, or Windows x86-64.
- [uv](https://docs.astral.sh/uv/). At the first use, uv gets a Python with Tk and the `iroh` package.
- A desktop session. Without a display, each consent is a deny.

With access to this repository:

```
/plugin install peer-link --marketplace ZoreAnuj/claude-peer-link
/reload-plugins
```

Or from a clone, for one session:

```
claude --plugin-dir /path/to/claude-peer-link
```

The plugin makes no process and no network connection before the first `/peer invite` or `/peer connect`.

## Commands

| Command | Function |
|---|---|
| `/peer invite <name>` | Makes a ticket for one person. The ticket is valid for 10 minutes and for one use. It goes to the clipboard. |
| `/peer connect <name> <ticket>` | The first link: connects to the person who made the ticket. |
| `/peer connect <name>` | Asks a saved contact for a link, or for 60 more minutes. No ticket is necessary. |
| `/peer listen` | Makes this session ready for requests of your saved contacts. |
| `/peer end <name>` | Closes the link immediately. The contact stays. |
| `/peer forget <name>` | Removes the contact. That person then needs a new ticket. |
| `/peer status` | Shows the links, the minutes that remain and the saved contacts. |

You give the name. It is your label for the peer. The other person gives a label to you.
The popups show a four-digit code, the same on the two screens. You can compare it by voice.
The code finds a wrong peer only if that peer could not see the two keys before it made its own key.

Only one session for each machine user can use the peer link at one time.
A request reaches you only while that session has a helper that runs: type `/peer listen` there.

## Security rules

1. A person accepts each invitation, each first connection, each inbound request and each outgoing message in a desktop popup. Claude cannot do this.
2. `/peer` obeys only what you type at the terminal. A request that you type to a saved contact needs no popup on your side.
3. Claude can only send text, with `SendMessage` to `peer:<name>`. The plugin adds no tool, no MCP server and no channel.
4. Text from a peer is untrusted data. A peer cannot approve a tool, run a command or change a setting.
5. A dial without a valid ticket, from a key that is not a saved contact, gets no popup and no answer.
6. The helper keeps your identity key and your contacts in `~/.claude/peer-link/`, for you only. It keeps no message. `audit.log` has only names and times.
7. The peer does not get your IP address. The relay operator can see it.

Limits that stay:

- Messages go into the Claude transcript on each side, and thus to the Anthropic API.
- A person who gets your `identity.key` file can ask your contacts for a link in your name. Remove the file to make a new identity.
- Prompt injection cannot be fully removed. Prefer a link to a session that is not in bypass-permissions mode and has no secrets in its directory.
- A mod runs with your permissions. Install this plugin only from this repository.
- The mods API of Claude Code can change between releases.

The full design and the reasons are in [docs](docs/README.md).

## Files

| Path | Content |
|---|---|
| `peerlink.py` | The helper: transport, tickets, leases, limits and the consent dialogs. |
| `hooks/` | The mod. See [hooks/README.md](hooks/README.md). |
| `.claude-plugin/` | The plugin manifest and the marketplace file. |
| `tests/` | Helper tests, mod tests and the cross-network test. See [tests/README.md](tests/README.md). |
| `docs/` | Design and plan. See [docs/README.md](docs/README.md). |
| `.github/workflows/ci.yml` | The helper tests on Ubuntu, macOS and Windows. |

## Tests

```
uv run --managed-python --script tests/test_peerlink.py
claude plugin test
```
