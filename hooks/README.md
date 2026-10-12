# hooks

The mod of the peer link plugin. Claude Code runs this code in its own process.

| File | Content |
|---|---|
| `hooks.json` | Names the hooks module. |
| `register.js` | The mod: the `/peer` command, the `SendMessage` hook for `peer:<name>`, the delivery of inbound messages, and the start of the helper. |

The mod has no network code and gives no consent. The helper `../peerlink.py` does these.

Up: [repository README](../README.md)
