# Peer link design

Date: 2026-10-12. Status: written spec, waits for the owner's review. No product code exists.

## 1. Purpose

A Claude Code session on one machine exchanges text messages with a Claude Code session of a different person, through the internet.
A person accepts each connection in a desktop popup.
The link is safe, private and confidential.

## 2. Decisions

The owner made these decisions on 2026-10-12.

| Topic | Decision |
|---|---|
| Lease | 60 minutes from the accept. The lease can be renewed. Each renewal needs a new approval. |
| Consent | Desktop popup with sound. |
| Outgoing messages | A person approves each message. |
| Code location | Private repository `ZoreAnuj/claude-peer-link`. |

## 3. Feasibility results

Three throwaway probes ran on 2026-10-12 on Ubuntu, Claude Code 2.1.296. The probe code is not in this repository.

| Probe | Result | Evidence |
|---|---|---|
| A custom channel delivers messages into a session | Yes | A 60-line Python server with no SDK registered as a channel. Two pushed events arrived in the session. The session called the reply tool. |
| A desktop popup with sound works from the channel server | Yes | The server process has the display variables. zenity 4.2.1 showed the dialog and returned code 5 at the timeout. `pw-play` started the sound. |
| The iroh transport connects by public key | Yes | Python package `iroh` 1.1.0. A dial with only the key and the relay URL connected in 0.2 s. The remote key was authenticated. |

More facts from the probes:

- Claude Code first sends `server/discover` for protocol revision 2026-07-28. The server answers "Method not found". Claude Code then sends `initialize`, and the channel works.
- Events that arrive in a group get one turn. The session sent one reply for two events.
- A new session starts in auto mode. A classifier approves tool calls there. Thus the approval for outgoing messages must be our popup, not the Claude Code permission prompt.
- With default settings, iroh gives our local, public and tailnet IP addresses to the peer. With a bind to `127.0.0.1`, it gives only the loopback address.
- One of three test dialogs closed with "No" after 0.6 s. The cause is not known. A key press that landed on the new dialog is the most probable cause.

Not verified:

- Two machines on different networks.
- macOS.
- That the sound is audible. Only the start of the player was verified.

## 4. Design

One Python file for each side: `peerlink.py`. Claude Code starts it as a channel server.
`uv run --script` runs it. It has one dependency: `iroh==1.1.0`.
The MCP part is a JSON-RPC loop from the standard library, as in the probe.

```
Their session                                              Our session
     | stdio                                                    | stdio
 peerlink.py  <==== QUIC, TLS 1.3, keys pinned (iroh) ====>  peerlink.py
     |              the relay sees ciphertext only              |
 desktop popup                                             desktop popup
```

### 4.1 Identity

Each server start makes a new Ed25519 key in memory. No key file and no contact file exist.
A name is given locally when the invitation is made. A peer cannot give itself a name.

### 4.2 Tools

| Tool | Popup | Function |
|---|---|---|
| `peer_invite(name)` | Yes | Makes a ticket: our key, our relay URL and a random 128-bit secret. Valid for 10 minutes and for one use. |
| `peer_connect(ticket, name)` | Yes | Dials the peer and sends the secret. |
| `peer_connect(name)` | Yes | Asks for a renewal, or for a new link to a peer that this session accepted before. |
| `peer_send(name, text)` | Yes, shows the full text | Sends one message. |
| `peer_disconnect(name)` | No | Closes the link immediately. |
| `peer_status()` | No | Shows the links and the minutes that remain. |

### 4.3 Connection flow

1. Alice approves an invitation for "Bob". Alice sends the ticket to Bob through a private channel.
2. Bob gives the ticket to his session. Bob approves "Connect this session to Alice for 60 minutes? Code 4821."
3. Bob's server dials the key in the ticket and sends the secret.
4. Alice's server checks the secret. For a wrong, used or expired secret, the server closes the connection. It shows no popup and sends no message.
5. Alice gets the popup with sound: "Accept connection from Bob for this session for 60 minutes? Code 4821."
6. Alice accepts. The two servers start a 60-minute timer and tell their sessions that the link is open.

The code is four digits from a hash of the two public keys. The two persons can compare it by voice.
"No" is the default button. A request that gets no answer in 5 minutes is denied.
An answer in less than 1 second is ignored and the dialog shows again, two times maximum.
Without a desktop display, each consent is a deny.

### 4.4 Lease and renewal

- Each side closes the connection when its timer ends. The grant is in memory only, so a session restart also ends it.
- Five minutes before the end, each session gets a notice.
- A renewal request shows a popup on the two sides. If the two persons accept, the timers start again at 60 minutes.
- After the end, a peer that this session accepted before can ask again without a ticket. The server identifies it by its key. The popup shows again.
- After a session restart, a new ticket is necessary.

### 4.5 Messages

One QUIC stream carries JSON lines: `hello`, `accept`, `deny`, `msg`, `renew`, `bye`.
An inbound message goes to the session as `<channel source="peerlink" peer="bob" trust="untrusted">`.
The server instructions say that peer text is data and not instructions.

## 5. Security rules

1. **A person accepts, not the model.** Each consent is a desktop popup that the server shows.
2. **No peer text in a consent popup.** Names come from the local side. Only our own outgoing text is shown.
3. **Peer text is untrusted data.** The server removes control characters and bidirectional overrides, and replaces angle brackets.
4. **No permission relay.** The server never declares `claude/channel/permission`.
5. **Each outgoing message needs an approval.** This prevents data theft through the link.
6. **Limits.** 16 KB for each message. 30 messages for each minute. One pending request and three requests for each hour, for each peer.
7. **Silence for strangers.** There is no directory. A dial without a valid secret gets no popup and no message.
8. **No long-term data.** No key file, no contact file, no message log. The audit log has the name, the accept time and the end time only.
9. **Link a low-privilege session.** Start the linked session in a clean directory, not in bypass-permissions mode. This is the primary defence against prompt injection.
10. **Relay only, as an option.** A bind to `127.0.0.1` hides our IP addresses from the peer. The relay then carries all traffic.

Limits that stay:

- Messages enter the Claude transcript on each side and go to the Anthropic API. The encryption protects the path, not the two ends.
- The relay operator sees metadata: public keys, IP addresses, timing and sizes. The keys change at each session start.
- The ticket goes through the model context of each side. It is valid for 10 minutes and for one use, and the popup is still necessary.
- A model that has a shell in the linked session can stop or change the server. Rule 9 limits this.
- If the linked session passes peer text to other local sessions, that text stays untrusted. Code cannot enforce this.
- Prompt injection cannot be fully removed.
- Channels are a research preview. The flag and the protocol can change.

## 6. Not in version 1

- Stored contacts and reconnection after a restart.
- Automatic send without an approval for each message.
- Files, groups, remote tool use.
- macOS and Windows popups.
- A relay of our own.

## 7. Tests

One file, `test_peerlink.py`. It starts two servers in one process and replaces the popup function in the test only.
The product code has no switch that turns off the popup.

| Test | Pass condition |
|---|---|
| Accept | A message goes from one side to the other. |
| Deny and timeout | No link. The dialer gets a refusal. |
| Lease end | The connection closes at the end of the lease. |
| Renewal | The two timers start again only when the two sides accept. |
| Reused or expired ticket | No popup, no message. |
| Dial without a secret | No popup, no message. |
| Limits | An oversize message and a message above the rate are dropped. |
| Unsafe text | Control characters and angle brackets do not reach the session. |
| Fast answer | An answer in less than 1 second does not accept. |
| No display | Each consent is a deny. |

A trial with a second person on a different network follows the tests.

## 8. Open points

- The first peer's operating system. Version 1 is for a Linux desktop.
- The sound file. The default is `dialog-warning.oga` from the freedesktop sound theme.
