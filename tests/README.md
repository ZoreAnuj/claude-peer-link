# tests

Tests for the peer link.

| File | Content |
|---|---|
| `test_peerlink.py` | Helper tests. Two helpers run in one process and connect through the public relay. The consent dialog is replaced in the test process only. |

| `peer-link.test.ts` | Mod tests. A fake helper stands for `peerlink.py`. |

Run the mod tests from the repository directory:

```
claude plugin test
```

Run the helper tests:

```
uv run --managed-python --script tests/test_peerlink.py
```

Add test names to run only some tests, for example `lease renewal`.
`PEERLINK_TEST_DIALOG=1` also shows the real dialog for two seconds.

Up: [repository README](../README.md)
