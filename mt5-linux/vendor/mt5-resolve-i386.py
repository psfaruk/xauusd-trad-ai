#!/usr/bin/env python3
"""Fast in-memory dependency resolver over a Debian Packages file."""
import re, subprocess, os, sys

PKGS_FILE = "/tmp/Packages-i386"
OUT = os.environ.get("MT5_RESTORE_DIR", "/home/z/mt5-restore") + "/debs-i386"
os.makedirs(OUT, exist_ok=True)

# parse
pkgs = {}  # name -> {deps: [alt-lists], filename, version}
cur = {}
alt = None
def flush():
    global cur
    if cur.get("Package"):
        name = cur["Package"]
        if name not in pkgs:  # first wins (main > updates ordering)
            pkgs[name] = cur
    cur = {}

with open(PKGS_FILE, errors="replace") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            flush(); continue
        if line.startswith(" ") and alt is not None:
            alt.append(line.strip()); continue
        m = re.match(r"^(\S+):\s*(.*)$", line)
        if not m: continue
        k, v = m.groups()
        if k == "Package":
            flush(); cur = {"Package": v, "deps": []}
        elif k == "Depends":
            for expr in v.split(","):
                expr = expr.strip()
                if not expr: continue
                # strip version constraints
                parts = [re.sub(r"\s*\(.*\)$", "", p).strip() for p in expr.split("|")]
                cur["deps"].append(parts)
        elif k == "Filename":
            cur["filename"] = v
        elif k == "Version":
            cur["version"] = v
    flush()

def installed_on_host(name):
    r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", name], capture_output=True, text=True)
    return r.stdout.startswith("install ok installed")

# host-provided (amd64) packages whose i386 counterpart we still need are those
# that are LIBRARIES. Data/arch-indep packages we skip.
SKIP_ALWAYS = {"libc-bin", "manpages", "install-info", "dpkg", "awk", "perl", "perl-base"}

seen, order = set(), []
queue = [[p] for p in sys.argv[1:]]
skip_log = []
while queue:
    alts = queue.pop(0)
    # prefer already-resolved, then host-installed, then first
    name = None
    for a in alts:
        base = a.split(":")[0]
        if base in pkgs or a in pkgs:
            name = a if a in pkgs else base
            break
    if name is None:
        # try host-installed alternative
        if any(installed_on_host(a.split(":")[0]) for a in alts):
            skip_log.append("/".join(alts))
            continue
        skip_log.append("MISSING:" + "/".join(alts))
        continue
    if name in seen: continue
    seen.add(name)
    entry = pkgs.get(name)
    if not entry:
        continue
    if name not in SKIP_ALWAYS:
        order.append(name)
    for dep in entry["deps"]:
        queue.append(dep)

print(f"i386 resolution: {len(order)} packages")
fails = []
for name in order:
    e = pkgs[name]
    fn = e["filename"]
    url = f"https://deb.debian.org/debian/{fn}"
    dest = os.path.join(OUT, os.path.basename(fn))
    if os.path.exists(dest): continue
    r = subprocess.run(["curl", "-s", "--max-time", "60", "-o", dest, url])
    if r.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 500:
        fails.append((name, url))
        if os.path.exists(dest): os.remove(dest)

print(f"downloaded all except {len(fails)}:")
for n, u in fails: print("  FAIL", n, u)
print("\nSkipped (host-installed or missing):")
for s in skip_log[:40]: print("  ", s)
