#!/usr/bin/env python3
"""Resolve + download Debian packages user-space (no root needed).

Usage: python3 fetch_pkgs.py <pkg>... — downloads pkg + missing deps into ./debs/
"""
import subprocess, sys, os, re

OUT = os.environ.get("MT5_RESTORE_DIR", "/home/z/mt5-restore") + "/debs"
os.makedirs(OUT, exist_ok=True)

def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)

def installed(pkg):
    return sh(f"dpkg-query -W -f='${{Status}}' {pkg} 2>/dev/null").stdout.startswith("install ok installed")

def candidates(pkg):
    """apt-cache depends: list of real dep names (incl. alternatives resolved)."""
    out = sh(f"apt-cache depends {pkg}").stdout
    deps = []
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^Depends:\s+(.+)$", line)
        if not m:
            continue
        expr = m.group(1).strip()
        if "|" in expr:
            alts = [a.strip() for a in expr.split("|")]
            # prefer an installed alternative, else first
            pick = next((a for a in alts if installed(a)), alts[0])
            deps.append(pick)
        else:
            deps.append(expr)
    return deps

def exists_in_repo(pkg):
    return sh(f"apt-cache show {pkg}").returncode == 0 and bool(sh(f"apt-cache show {pkg}").stdout.strip())

def version_of(pkg):
    out = sh(f"apt-cache policy {pkg}").stdout
    m = re.search(r"Candidate:\s+(\S+)", out)
    return m.group(1) if m else None

def resolve(root_pkgs):
    seen, order = set(), []
    queue = list(root_pkgs)
    while queue:
        p = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        if installed(p):
            continue
        if not exists_in_repo(p):
            print(f"  [skip] {p} (virtual/absent)")
            continue
        order.append(p)
        for d in candidates(p):
            if d not in seen:
                queue.append(d)
    return order

if __name__ == "__main__":
    pkgs = sys.argv[1:]
    todo = resolve(pkgs)
    print(f"Need to download {len(todo)} packages:")
    for p in todo:
        print(" ", p, version_of(p))
    ok, fail = [], []
    for p in todo:
        r = sh(f"cd {OUT} && apt-get download {p}")
        if r.returncode == 0:
            ok.append(p)
        else:
            fail.append((p, r.stderr.strip()[:100]))
    print(f"\nDownloaded OK: {len(ok)}  |  Failed: {len(fail)}")
    for p, e in fail:
        print("  FAIL", p, e)
