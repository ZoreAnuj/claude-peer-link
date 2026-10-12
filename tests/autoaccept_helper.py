#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["iroh==1.1.0"]
# ///
"""TEST ONLY: the real helper, but each consent dialog is an accept without a person.

This file is not part of the plugin. For the end-to-end test, make a copy of the plugin in a
temporary directory with this file as `peerlink.py` and the real helper as `peerlink_real.py`:

    cp -r .claude-plugin hooks "$COPY"/
    cp peerlink.py "$COPY"/peerlink_real.py
    cp tests/autoaccept_helper.py "$COPY"/peerlink.py
    PEERLINK_HOME="$COPY"/home-alice claude --plugin-dir "$COPY"

Give each test session its own PEERLINK_HOME, so that the test does not use the identity of the person.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import peerlink_real as real  # noqa: E402


async def accept(*args, **kwargs):
    return True


class AutoAccept(real.PeerLink):
    def __init__(self, emit, **kwargs):
        super().__init__(emit, ask=accept, **kwargs)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "ctl":
        sys.exit(real.ctl_main())
    if mode == "serve":
        real.PeerLink = AutoAccept
        sys.exit(asyncio.run(real.serve(" ".join(sys.argv[2:]))))
    sys.exit("test helper: serve | ctl")
