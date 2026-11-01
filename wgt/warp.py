"""Cloudflare WARP: the wgcf tool, account registration and the WARP tunnel
configs (warp.conf, and each exit city's wx-<code>.conf)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from typing import TYPE_CHECKING, Callable, List, Optional

from . import host, render, store
from .errors import WgtError

if TYPE_CHECKING:
    from .ctx import Ctx


Fetch = Callable[[str, int], bytes]   # (url, timeout seconds) -> body


def install_wgcf(ctx: Ctx, reinstall: bool = False, fetch: Optional[Fetch] = None) -> bool:
    """Download wgcf for this architecture from GitHub, check it, install it
    (unless a working one is there and not <reinstall>). False if it can't be
    fetched (setup can carry on without WARP); a checksum mismatch is fatal.
    <fetch> replaces the download (tests)."""
    fetch = fetch or _fetch
    p = ctx.paths
    if os.access(p.wgcf_bin, os.X_OK) and ctx.runner.ok([p.wgcf_bin, "--help"]) and not reinstall:
        ctx.out.info(f"wgcf present: {p.wgcf_bin}")
        return True
    version = os.environ.get("WGCF_VERSION", "latest")
    arch = ctx.runner.query(["dpkg", "--print-architecture"]).strip() or "amd64"
    ctx.runner.step(f"Install wgcf ({version}, linux_{arch}) to {p.wgcf_bin}", f"rm {p.wgcf_bin}")
    if ctx.runner.dry_run:
        ctx.out.info("[dry-run] would download from github.com/ViRb3/wgcf releases")
        return True
    api = ("https://api.github.com/repos/ViRb3/wgcf/releases/latest" if version == "latest"
           else f"https://api.github.com/repos/ViRb3/wgcf/releases/tags/{version}")
    try:
        rel = json.loads(fetch(api, 30))
    except (OSError, ValueError):
        ctx.out.warn(f"cannot reach GitHub releases API ({api})")
        return False
    urls = [a.get("browser_download_url", "") for a in rel.get("assets", [])]
    url = next((u for u in urls if u.endswith(f"_linux_{arch}")), "")
    if not url:
        ctx.out.warn(f"no linux_{arch} asset in wgcf release {version}")
        return False
    sums_url = next((u for u in urls if re.search(r"checksums|sha256", u, re.I)), "")
    try:
        data = fetch(url, 120)
    except OSError:
        ctx.out.warn(f"download failed: {url}")
        return False
    got = hashlib.sha256(data).hexdigest()
    want = os.environ.get("WGCF_SHA256", "")
    if not want and sums_url:
        try:
            name = os.path.basename(url)
            for line in fetch(sums_url, 30).decode().splitlines():
                f = line.split()
                if len(f) == 2 and f[1].lstrip("*") == name:
                    want = f[0]
        except OSError:
            want = ""
    if want:
        if got != want:
            raise WgtError(f"wgcf checksum mismatch (got {got}, want {want})")
        ctx.out.info(f"sha256 verified: {got}")
    else:
        ctx.out.warn(f"release publishes no checksums; set WGCF_SHA256={got} to pin this binary")
    fd, tmp = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.chmod(tmp, 0o755)
    if not ctx.runner.ok([tmp, "--help"]):
        os.remove(tmp)
        ctx.out.warn("downloaded wgcf does not run")
        return False
    ctx.runner.run(["install", "-m", "755", tmp, p.wgcf_bin])
    os.remove(tmp)
    ctx.out.info(f"installed {os.path.basename(url)}")
    return True


def _fetch(url: str, timeout: int) -> bytes:
    """GET <url> (GitHub's API wants a User-Agent)."""
    req = urllib.request.Request(url, headers={"User-Agent": "wireguard-tools"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def register(ctx: Ctx, directory: Optional[str] = None, regenerate: bool = False) -> bool:
    """Register a WARP account and generate its WireGuard profile (once) in
    <directory> (default the main one; each exit city has its own; with
    <regenerate> the profile is made again from the same account). False if
    WARP refuses (setup can carry on without WARP)."""
    d = directory or ctx.paths.wgcf_dir
    log = os.path.join(d, "wgcf.log")
    if not ctx.runner.dry_run:
        os.makedirs(d, mode=0o700, exist_ok=True)
    if os.path.isfile(ctx.paths.wgcf_account(d)):
        ctx.out.info(f"WARP account exists ({ctx.paths.wgcf_account(d)}); not re-registering")
    else:
        ctx.runner.step(f"Register a free WARP account in {d} (accepts Cloudflare's WARP terms)", f"rm -rf {d}")
        if not ctx.runner.dry_run and not _wgcf(ctx, d, ["register", "--accept-tos"], log, append=False):
            ctx.out.warn(f"wgcf register failed; see {log}. WARP may refuse registrations from some server IPs:\n"
                         "       retry later ('wgt warp on'), or register on another machine and copy\n"
                         f"       wgcf-account.toml and wgcf-profile.conf into {d}/ (see README)")
            return False
    if os.path.isfile(ctx.paths.wgcf_profile(d)) and not regenerate:
        ctx.out.info("WARP profile exists")
        return True
    ctx.runner.step("Generate WARP WireGuard profile", f"rm {ctx.paths.wgcf_profile(d)}")
    if not ctx.runner.dry_run:
        if not _wgcf(ctx, d, ["generate"], log, append=True):
            ctx.out.warn(f"wgcf generate failed; see {log}")
            return False
        for f in os.listdir(d):
            os.chmod(os.path.join(d, f), 0o600)
    return True


def _wgcf(ctx: Ctx, d: str, args: List[str], log: str, append: bool) -> bool:
    """Run wgcf in <d>, output to <log>."""
    return ctx.runner.run_logged([ctx.paths.wgcf_bin, *args], log, cwd=d, append=append)


def render_profile(ctx: Ctx, profile_path: str, table: Optional[int] = None, mark: Optional[int] = None) -> str:
    """warp.conf (or an exit city's wx-<code>.conf) from a wgcf profile."""
    profile = store.read_text(profile_path)
    if profile is None:
        raise WgtError(f"missing {profile_path}")
    return render.render_warp_conf(ctx.cfg, profile, ctx.facts.has_ipv6, host.resolve4,
                                   table=table, mark=mark, warn=ctx.out.warn)
