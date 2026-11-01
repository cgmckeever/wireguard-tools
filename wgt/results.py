"""PASS/FAIL results for `wgt test` and `wgt exit test`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .cli import Ctx


@dataclass
class Results:
    """Running PASS/FAIL/WARN counts; each result is printed as it comes."""

    passed: int = 0
    failed: int = 0
    warned: int = 0
    out: Callable[[str], None] = print

    def result(self, kind: str, desc: str) -> None:
        self.out(f"  {kind:<4} {desc}")
        if kind == "PASS":
            self.passed += 1
        elif kind == "FAIL":
            self.failed += 1
        elif kind == "WARN":
            self.warned += 1

    def check(self, desc: str, ok: bool) -> None:
        self.result("PASS" if ok else "FAIL", desc)


def report(ctx: Ctx, res: Results) -> int:
    """Print the totals; the exit status is 1 on any FAIL."""
    print("")
    ctx.out.info(f"{res.passed} passed, {res.failed} failed, {res.warned} warnings")
    return 1 if res.failed else 0
