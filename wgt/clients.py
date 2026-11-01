"""`wgt client ...`: the clients of this server. Each has a keypair, a
preshared key and an address; a client named <code>.<name> exits through
exit city <code>."""

from __future__ import annotations

from typing import List

from . import checks
from .errors import WgtError


def cmd_client(ctx, words: List[str]) -> None:
    sub = words[0] if words else "list"
    if sub == "list":
        cmd_list(ctx)
        return
    raise WgtError(f"'wgt client {sub}' isn't ported yet; use wg-client.sh {sub}")


def cmd_list(ctx) -> None:
    """The clients table, or with --tsv tab-separated rows (for wgt-ctl)."""
    if ctx.args.tsv:
        for row in checks.client_rows(ctx):
            print("\t".join(str(x) for x in row))
        return
    checks.print_clients(ctx, numbered=True)
