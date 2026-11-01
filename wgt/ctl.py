"""`wgt-ctl`: drives the servers listed in local/servers from your own machine.
The server is the first word: `wgt-ctl boba client add phone --exit par-fr`.
(Being ported from scripts/servers.sh; see docs/python-plan.md, step 7.)"""

from __future__ import annotations

import sys
from typing import List, Optional

from .errors import WgtError
from .system import Out

RESERVED = ("init", "clients", "all")


def main(argv: Optional[List[str]] = None) -> int:
    out = Out()
    try:
        raise WgtError("wgt-ctl isn't ported yet; use scripts/servers.sh")
    except WgtError as e:
        out.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
