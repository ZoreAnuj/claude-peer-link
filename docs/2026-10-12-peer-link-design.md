# Peer link design

Date: 2026-10-12. Status: approved by the owner on 2026-10-12. Revision 2 added two later owner inputs:
all three operating systems, and communication in the manner of Claude Code mods and session messaging.
Revision 3 adds the third: after the first link, a request needs no ticket. It also has the changes from the code review.

## 1. Purpose

A Claude Code session on one machine exchanges text messages with a Claude Code session of a different person, through the internet.
A person accepts each connection in a desktop popup.
The link is safe, private and confidential.

## 2. Decisions

| Topic | Decision |
|---|---|
| Lease | 60 minutes from the accept. The lease can be renewed. Each renewal needs a new approval. |
| Consent | Desktop popup with sound. |
| Outgoing messages | A person approves each message. |
| Code location | Private repository `ZoreAnuj/claude-peer-link`. |
| Operating systems | Windows, Linux and macOS. |
| Integration | A Claude Code mod. Claude sends with the native `SendMessage` tool. Local processes talk through a per-user socket, as Claude Code sessions do. |
| Later requests | The first link is a secure handshake with a ticket. Each side then saves the contact. A later request needs one command and one popup, and no ticket. |

## 3. Feasibility results

Throwaway probes ran on 2026-10-12 on Ubuntu with Claude Code 2.1.296. The probe code is not in this repository.

Mods API, tested with a probe mod in a nested session:

| Question | Result |
|---|---|
| Does a mod load without a restart flag? | Yes. `--plugin-dir` loads it, and `/reload-plugins` loads it into a session that runs. |
| Can a mod tell a typed command from other callers? | Yes. A command that the person types has `origin.kind` `composer`. |
| Can a mod start a helper and read its output? | Yes. `$.process.spawn` streams the output. The child ends when the mod unloads. |
| Can a mod send data to a process? | Only at the start, through standard input. Thus later requests use a short control process. |
| Can a mod catch `SendMessage` to a name that no session has? | Yes. The `session.send` hook saw `to: "peer:probe"` and answered. The tool result was "Delivered by a plugin hook". |
| Can a mod give text to Claude? | Yes. `$.prompt.submit` starts a turn. Claude Code says that the text comes from the plugin and stands in the user's place. |
| Can a mod deliver to its own session through the session socket? | No. `$.session.send` to its own id gets "no live session on this machine has id". |
| Does `$.audio.play` make a sound everywhere? | No. The type notes say that a Linux or Windows terminal plays nothing. |

Transport and popup:

- Python package `iroh` 1.1.0 connects by public key through a relay in 0.2 s to 1.5 s. The remote key is authenticated.
- iroh works with its minimal preset and a relay. The n0 address lookup service is not necessary.
- With default settings, iroh gives our local, public and tailnet IP addresses to the peer. With a bind to `127.0.0.1`, it gives only the loopback address.
- A read on a closed connection raises an error. A dial with a wrong protocol name gets no answer.
- The `iroh` package has files for Linux x86-64 and ARM64, macOS ARM64 and Windows x86-64. It has no file for Intel Mac or Windows on ARM.
- The Ubuntu system Python has no `tkinter`. A Python that uv manages has Tk 9.0 and showed a window. Thus the helper runs with `uv run --managed-python`.
- Two of five test windows closed early with "No". The cause is not known. A key press that landed on the new window is the most probable cause.

The first design used a channel server. It worked, but a channel needs a development flag at session start. The mod design replaces it.

Not verified by the probes: two machines on different networks, macOS, Windows, and that the sound is audible. The plan has tests for the first three.

## 4. Design

The repository is one Claude Code plugin with the name `peer-link`. It has two parts.

| Part | File | Function |
|---|---|---|
| Mod | `hooks/register.js` | The `/peer` command, the `SendMessage` hook, the delivery of inbound messages. It has no network code. |
| Helper | `peerlink.py` | Transport, tickets, leases, limits and all consent dialogs. One Python file, one dependency: `iroh==1.1.0`. |

```
Their session + mod                                        Our session + mod
     | output lines, control socket                             | output lines, control socket
 peerlink.py  <==== QUIC, TLS 1.3, keys pinned (iroh) ====>  peerlink.py
     |              the relay sees ciphertext only              |
 desktop popup                                             desktop popup
```

The mod starts the helper at the first `/peer invite` or `/peer connect`. Before that, the plugin makes no process and no network connection.
The helper ends when the mod unloads, when the session ends, or when its output pipe closes.

### 4.1 Identity and contacts

Each machine user has one Ed25519 key. The helper makes it at the first use and keeps it in `~/.claude/peer-link/identity.key`.
When a link opens, each side saves the contact in `contacts.json`: the local name and the public key of the peer.
The two files are for the owner only (mode 0600 in a directory with mode 0700). `PEERLINK_HOME` names a different directory.
The first link is the secure handshake: the ticket pins our key, the secret proves the peer, and the two persons accept.
Each later connection is authenticated by the two saved keys in the TLS handshake.
A name is given locally when the invitation or the connection is made. A peer cannot give itself a name.
Only one helper at a time can use the identity. A second session gets a message that the peer link is in use.

### 4.2 Commands for the person

| Command | Popup | Function |
|---|---|---|
| `/peer invite <name>` | Yes | Makes a ticket: our key and a random 128-bit secret. Valid for 10 minutes and for one use. |
| `/peer connect <name> <ticket>` | Yes | The first link to a peer: dials the key and sends the secret. |
| `/peer connect <name>` | No | Asks a saved contact for a link, or for 60 more minutes. The typed command is the consent of this side. |
| `/peer listen` | No | Starts the helper, so that saved contacts can ask for a link. No ticket is made. |
| `/peer end <name>` | No | Closes the link immediately. The contact stays. |
| `/peer forget <name>` | No | Removes the contact and closes its link. That key gets no popup after this. |
| `/peer status` | No | Shows the links, the minutes that remain and the saved contacts. |

The mod obeys `/peer` only when the person types it. The output is a dim line that Claude does not read.
The ticket goes to that line and to the clipboard. It does not go into the model context.

### 4.3 Connection flow

1. Alice types `/peer invite Bob` and approves the popup. Alice sends the ticket to Bob through a private channel.
2. Bob types `/peer connect Alice <ticket>`. Bob approves "Connect this session to Alice for 60 minutes? Code 4821."
3. Bob's helper dials the key in the ticket and sends the secret.
4. Alice's helper checks the secret. For a wrong, used or expired secret, the helper closes the connection. It shows no popup and sends no message.
5. Alice gets the popup with sound: "Accept connection from Bob (new, with your invitation) for this session for 60 minutes? Code 4821."
6. Alice accepts. The two helpers start a 60-minute timer, save the contact, and the two sessions show that the link is open.

A later request has two steps:

1. Bob types `/peer connect Alice`. His helper dials the saved key through each relay of its list at the same time.
2. Alice's helper finds the key in its contacts. Alice gets the popup with sound: "Accept connection from Bob (a saved contact) ...". She accepts.

Alice's session must have a helper that runs: she typed `/peer listen`, or a different `/peer` command, in that session.
A key that is not in the contacts and has no valid ticket gets no popup.

The code is four digits from a hash of the two public keys. The two persons can compare it by voice.
The code finds a wrong peer only if that peer could not see the two keys before it made its own key.
"Deny" is the default button. A request that gets no answer in 5 minutes is denied.
The accept button is off for the first second, so a key press that lands on a new dialog cannot accept.
Without a desktop display or without Tk, each consent is a deny.

The popup is a Tk dialog from the Python standard library. The helper starts it as a child process of the same file, with `python -I`.
The sound comes from `winsound` on Windows, `afplay` on macOS and `pw-play`, `paplay` or `canberra-gtk-play` on Linux.
`PEERLINK_SOUND` can name a different sound file.

### 4.4 Lease and renewal

- Each side closes the connection when its timer ends. The grant is in memory only, so a helper restart also ends it.
- Five minutes before the end, each person gets a notice.
- A renewal: one person types `/peer connect <name>`, and the other person gets a popup. After the accept, the two timers start again at 60 minutes.
- A peer cannot extend a lease alone. A renewal counts only on the side that asked for it or approved it, and an answer counts for 5 minutes only.
- After the end, each side can ask again without a ticket, also after a restart. The popup of the other person shows again.
- One name has one link. A second link for the same name replaces the first, and each link stays under its lease.
- The timer uses the monotonic clock and the wall clock. Thus a suspended computer does not extend a lease.

### 4.5 Messages

Outbound: Claude calls the native `SendMessage` tool with the address `peer:<name>`. The mod catches the call in its `session.send` hook.
The helper shows the full text in a popup. After the approval, the helper sends the text. A refusal goes back to Claude as the tool result.

Inbound: the helper gives the text to the mod, and the mod starts a turn with `$.prompt.submit`.
The mod puts the text between two marks with a random number, below a note that says: the sender is a different person, the text is untrusted data, and it gives no permission.
The mod drops text for a name that has no open link.
While a link is open, the mod also adds one line of context to each prompt. The line names the open links and the address form.

One QUIC stream carries JSON lines: `hello`, `accept`, `deny`, `msg`, `renew`, `renewed`, `renew_denied`, `bye`.

### 4.6 Local communication

The helper writes its events as JSON lines to its output, and the mod reads them.
For a request to the helper, the mod runs `peerlink.py ctl`. This short process connects to the control socket of the helper.
The control socket is a Unix domain socket in a new directory with mode 0700, or a named pipe on Windows.
Each connection must prove a random key before the helper reads a request. The key is in memory only: the helper gives it to the mod, and the mod gives it to the control process.
Claude Code uses the same pattern for the inbox socket of each session.

## 5. Security rules

1. **A person accepts, not the model.** Each invitation, each first connection, each inbound request, each inbound renewal and each outgoing message needs a desktop popup that the helper shows.
2. **Commands come from the keyboard.** The mod refuses `/peer` from all other origins. A typed request to a saved contact needs no popup on the side that typed it. The other person still gets one.
3. **One model path.** Claude can only send text with `SendMessage` to `peer:<name>`. The plugin adds no tool, no MCP server and no channel.
4. **No peer text in a consent popup.** Names come from the local side. Only our own outgoing text is shown.
5. **Peer text is untrusted data.** The helper removes control, format and hidden characters, and replaces angle brackets. The mod adds the note and the random marks.
6. **Each outgoing message needs an approval.** This prevents data theft through the link.
7. **Limits.** 16 KB for each message. 30 messages for each minute. One pending request and three requests for each hour, for each peer. The wait for a free dialog and the dialog together take 5 minutes at most.
8. **Silence for strangers.** There is no directory. A dial without a valid secret gets no popup and no message.
9. **Little long-term data.** The helper keeps one identity key and the contacts, for the owner only. It keeps no message. The audit log has the name, the accept time and the end time only.
10. **Relay only.** The helper binds to the loopback addresses only, so it does not give our IP addresses to the peer. The relay carries all traffic.
11. **Local socket for the owner only.** A private directory and a key protect the control socket. Each connection proves the key in its own thread, so a silent client cannot stop the socket.
12. **No module from the session directory.** The dialog child runs with `python -I`. The helper and the control process run with an empty `PYTHONPATH`.
13. **Prefer a low-privilege session.** A link to a session in bypass-permissions mode gives a prompt injection more power.

Limits that stay:

- Messages enter the Claude transcript on each side and go to the Anthropic API. The encryption protects the path, not the two ends.
- The relay operator sees metadata: public keys, IP addresses, timing and sizes. The key of a machine is now the same each time.
- A person who gets `identity.key` can ask our contacts for a link in our name. Their popup still shows. Remove the key file to make a new identity.
- A saved contact can make three popups with sound each hour while our helper runs. `/peer forget` stops this.
- A ticket that the person types or sees stays in the transcript file and in the command history of Claude Code. It is valid for 10 minutes and for one use.
- Claude Code tells Claude that a plugin prompt stands in the user's place. Only our note says that the text is untrusted.
- A mod runs with the permissions of the user. Install this plugin only from this repository.
- A model that has a shell in the linked session can stop or change the helper.
- If the linked session passes peer text to other local sessions, that text stays untrusted. Code cannot enforce this.
- Prompt injection cannot be fully removed.
- The mods API can change between Claude Code releases.

## 6. Not in version 1

- More than one linked session for each machine user at the same time.
- Automatic send without an approval for each message.
- Files, groups, remote tool use.
- Intel Mac and Windows on ARM. The `iroh` package has no file for them.
- Direct connections without the relay.
- A relay of our own.

## 7. Tests

Helper tests: `tests/test_peerlink.py`. It starts two helpers in one process and replaces the popup function in the test only.
The product code has no switch that turns off the popup.

| Test | Pass condition |
|---|---|
| Accept | A message goes from one side to the other. |
| Deny and timeout | No link. The dialer gets a refusal. |
| Local refusal | A refused connect makes no dial. A refused send sends nothing. |
| Lease end | The connection closes at the end of the lease. |
| Renewal | The two timers start again only when the two sides accept. |
| One-sided renewal | A `renewed` message that we did not ask for does not change our timer. |
| Ask again | After the lease end, each side can ask again without a ticket. The popup shows. |
| Reused or expired ticket | No popup, no message. |
| Dial without a secret | No popup, no message. |
| Limits | An oversize message and a message above the rate are dropped. |
| Unsafe text | Control characters and angle brackets do not reach the session. |
| Control socket | A request with the key gets an answer. A request with a wrong key gets none. A silent client does not stop the socket. |
| One link for each name | A slow dial and an inbound request for one name leave one link, under its lease. |
| Renewal time | A renewal answer after 5 minutes does not count. A request above the rate gets a refusal. |
| One pending request | A known peer that dials again while its request waits gets no second popup. |
| Loopback only | The endpoint has no socket on an address that a peer can reach. |
| Dialog time | The wait for a free dialog and the dialog together have one limit. |
| No text after the end | After the end command starts, no peer text reaches the session. |
| Saved contact | After a restart of the two helpers, a request needs no ticket and no popup on the side that typed it. |
| One helper | A second helper for the same identity gets a refusal. |
| Forget | A forgotten contact gets no popup. |
| Dialog | The real dialog shows and gives a deny at its timeout. |
| No display | Each consent is a deny. Linux only. |

Mod tests: `tests/peer-link.test.ts`, run with `claude plugin test`.

| Test | Pass condition |
|---|---|
| Other addresses | `SendMessage` to a normal session passes through unchanged. |
| No link | `SendMessage` to `peer:<name>` without a link is refused with a reason. |
| Send | With an open link, the text goes to the helper, and the result is "delivered" only after the helper says so. |
| Inbound | A helper message becomes one prompt with the note and the marks. |
| Command origin | `/peer` from an origin that is not the keyboard does nothing. |
| Ticket privacy | `/peer invite` returns no text for Claude. |
| No link | A helper message for a name that is not open does not reach Claude. |
| New commands | `listen`, `forget` and `status` go to the helper. Without a helper, `end`, `forget` and `status` start none. |
| Environment | The helper and each control process run with an empty `PYTHONPATH`. |

GitHub Actions runs the helper tests on Ubuntu, macOS and Windows.
A cross-network test dials from the laptop to a runner of each system, through the relay.
An end-to-end test on Linux links two real sessions, with a test copy of the plugin that accepts each dialog.
The one-second delay of the accept button has no automatic test.
A trial with a second person follows the tests.

## 8. Open points

- None.
