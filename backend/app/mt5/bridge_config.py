"""D-078 — the runtime MT5 bridge endpoint + staged diagnostics.

THE 502 ANATOMY (user report: "MetaTrader 5 terminal bridge unavailable:
MT5 MCP HTTP 502" — data offline, Exness connect failing):

    Railway backend ──HTTPS──> tunnel gateway ──tunnel──> user's PC
                                                          └─ MT5 terminal
                                                             MCP :22346

  * HTTP 502/504  = the GATEWAY is alive but the tunnel client on the
                    user's PC is offline (PC off / tunnel app stopped /
                    MT5 terminal closed) — the most common case.
  * DNS failure   = the tunnel URL no longer exists — free tunnels ROTATE
                    their subdomain on every restart, so the env var
                    MT5_MCP_URL points at a dead name forever.
  * 401/403       = gateway + tunnel + terminal alive, but the bearer key
                    doesn't match.

The env var can only be fixed by a Railway redeploy — outside the app,
exactly the pain the user hit. D-078 makes the endpoint RUNTIME state:

  * `set_bridge_url()` hot-swaps the endpoint for every MT5TerminalClient
    (the singleton AND worker clones — they resolve the URL per RPC via
    `url_stamp()` invalidation, mcp.py D-078);
  * the override persists in `app_config` (key/value) so restarts keep it;
  * `diagnose_bridge()` walks the chain stage by stage (parse → dns → tcp
    → tls → http → mcp) and returns exactly WHERE it breaks with a human
    remediation hint — the opaque "HTTP 502" becomes "the tunnel client
    on your PC is offline — start it and retry";
  * the probe journal (`record_probe`/`last_probe`) feeds the honest
    feed_status note and the Exness connect errors.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("xauusd.bridge")

DEFAULT_URL = "http://127.0.0.1:22346/mcp"
_MAX_URL_LEN = 500

_lock = threading.Lock()
_runtime_url: str | None = None
_stamp: int = 0
_probe: dict[str, Any] | None = None


# ------------------------------------------------------------------ url
def env_url() -> str:
    """The env/default endpoint (MT5_MCP_URL — the deploy-time fallback)."""
    import os

    return os.environ.get("MT5_MCP_URL", DEFAULT_URL).rstrip("/")


def get_bridge_url() -> str:
    """The EFFECTIVE endpoint: runtime override > env var > default.

    Called on every MCP RPC (dynamic client resolution, mcp.py D-078) —
    keep it allocation-free and fast.
    """
    with _lock:
        url = _runtime_url
    return url if url else env_url()


def bridge_source() -> str:
    """"runtime" when an in-app override is active, else "env"."""
    with _lock:
        return "runtime" if _runtime_url else "env"


def url_stamp() -> int:
    """Monotonic change counter — clients compare to detect hot-swaps."""
    with _lock:
        return _stamp


def validate_bridge_url(url: str) -> str:
    """Normalize + validate; raises ValueError with a human reason."""
    u = (url or "").strip()
    if not u:
        raise ValueError("bridge URL is empty")
    if len(u) > _MAX_URL_LEN:
        raise ValueError(f"bridge URL too long (max {_MAX_URL_LEN} chars)")
    parts = urllib.parse.urlsplit(u)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            "bridge URL must start with http:// or https:// "
            "(the terminal MCP endpoint, e.g. https://<tunnel>/mcp)"
        )
    if not parts.hostname:
        raise ValueError("bridge URL has no host")
    return u.rstrip("/")


def set_bridge_url(url: str) -> str:
    """Set the runtime override (validated). Bumps the change stamp so
    every live MT5TerminalClient drops its pooled connections + MCP
    session and re-resolves on the very next call."""
    global _runtime_url, _stamp
    u = validate_bridge_url(url)
    with _lock:
        if _runtime_url == u:
            return u
        _runtime_url = u
        _stamp += 1
    logger.info("MT5 bridge URL hot-swapped (D-078) -> %s", mask_url(u))
    return u


def clear_runtime_url() -> None:
    """Drop the override — back to the env var (admin "reset")."""
    global _runtime_url, _stamp
    with _lock:
        if _runtime_url is None:
            return
        _runtime_url = None
        _stamp += 1
    logger.info("MT5 bridge runtime override cleared — env endpoint back in effect")


def mask_url(url: str) -> str:
    """Log/diagnosis-safe form: scheme+host(:port), truncated path/query."""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        rest = (p.path or "") + (f"?{p.query}" if p.query else "")
        if len(rest) > 24:
            rest = rest[:21] + "..."
        return f"{p.scheme}://{host}{port}{rest}"
    except Exception:  # noqa: BLE001 — masking must never raise
        return "<unparsable>"


# ----------------------------------------------------------- probe journal
def record_probe(ok: bool, stage: str, detail: str) -> None:
    """Remember the last bridge probe outcome (feeds honest status).

    NOTE: `bridge_source()` takes _lock too — it MUST be called BEFORE the
    `with _lock:` block (threading.Lock is not reentrant; calling it inside
    self-deadlocks, which froze the first failed probe forever).
    """
    global _probe
    src = bridge_source()
    with _lock:
        _probe = {
            "ok": bool(ok),
            "stage": stage,
            "detail": detail[:300],
            "url_source": src,
            "at": datetime.now(UTC).isoformat(),
        }


def last_probe() -> dict[str, Any] | None:
    with _lock:
        return dict(_probe) if _probe else None


def hint_for_error(text: str) -> str:
    """Human remediation for a raw bridge error string (502/401/...)."""
    t = text or ""
    if "HTTP 502" in t or "HTTP 504" in t:
        return (
            "the tunnel gateway is reachable but the tunnel client / MT5 "
            "terminal on your PC is offline — start the tunnel app and the "
            "MetaTrader 5 terminal, then retry (the app reconnects "
            "automatically)"
        )
    if "HTTP 401" in t or "HTTP 403" in t:
        return (
            "bridge key mismatch — the server's MT5_MCP_KEY doesn't match "
            "the terminal's MCP key"
        )
    if "HTTP 404" in t:
        return (
            "wrong bridge path — the URL must point at the terminal's MCP "
            "endpoint (keep the /mcp path and any ?query the tunnel gave you)"
        )
    if "unreachable" in t or "timed out" in t or "timeout" in t.lower():
        return (
            "the bridge URL didn't answer — check the tunnel is running and "
            "the URL is current (an admin can update it in Settings → "
            "Terminal Bridge)"
        )
    return (
        "check the MT5 terminal + tunnel on your PC; an admin can update "
        "the bridge URL in Settings → Terminal Bridge"
    )


# -------------------------------------------------------------- diagnose
_STAGE_HINTS = {
    "parse": "fix the URL format — it must be http(s)://host/mcp",
    "dns": (
        "the tunnel URL no longer resolves — free tunnels rotate their "
        "address on restart; copy the NEW URL from your tunnel app and "
        "save it here"
    ),
    "tcp": "the tunnel gateway refused the connection — network/firewall issue",
    "tls": "TLS handshake failed — the gateway certificate/protocol changed",
}


def _stage(
    name: str, ok: bool, detail: str, ms: float, hint: str | None = None
) -> dict[str, Any]:
    return {
        "stage": name,
        "ok": bool(ok),
        "detail": detail[:300],
        "ms": round(ms, 1),
        "hint": hint,
    }


def _post_json(
    conn: http.client.HTTPConnection, path: str, payload: dict[str, Any],
    key: str, timeout: float,
) -> tuple[int, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Connection": "close",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    conn.sock and conn.sock.settimeout(timeout)
    conn.request("POST", path, body=json.dumps(payload).encode(), headers=headers)
    r = conn.getresponse()
    return r.status, r.read().decode(errors="replace")[:500]


def diagnose_bridge(
    url: str | None = None, key: str | None = None,
) -> dict[str, Any]:
    """Walk the bridge chain stage by stage; return WHERE it breaks.

    Blocking (network) — call via asyncio.to_thread from async context.
    Never raises: every failure is a stage result with a remediation hint.
    """
    from app.mt5.mcp import _cfg_key

    url = (url or get_bridge_url()).strip()
    eff_key = key if key is not None else _cfg_key()
    stages: list[dict[str, Any]] = []

    # 1 — parse
    t0 = time.monotonic()
    try:
        u = validate_bridge_url(url)
        parts = urllib.parse.urlsplit(u)
        stages.append(_stage("parse", True, f"scheme={parts.scheme} host={parts.hostname}",
                             (time.monotonic() - t0) * 1000))
    except ValueError as exc:
        stages.append(_stage("parse", False, str(exc), (time.monotonic() - t0) * 1000,
                             _STAGE_HINTS["parse"]))
        return _finish_diag(url, stages)

    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    # 2 — dns
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addr = infos[0][4][0]
        stages.append(_stage("dns", True, f"resolved {host} -> {addr}",
                             (time.monotonic() - t0) * 1000))
    except OSError as exc:
        stages.append(_stage("dns", False, f"DNS resolve failed: {exc}",
                             (time.monotonic() - t0) * 1000, _STAGE_HINTS["dns"]))
        return _finish_diag(url, stages)

    # 3 — tcp
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        stages.append(_stage("tcp", True, f"gateway accepting connections on :{port}",
                             (time.monotonic() - t0) * 1000))
    except OSError as exc:
        stages.append(_stage("tcp", False, f"connect failed: {exc}",
                             (time.monotonic() - t0) * 1000, _STAGE_HINTS["tcp"]))
        return _finish_diag(url, stages)

    # 4 — tls (https only)
    if parts.scheme == "https":
        t0 = time.monotonic()
        try:
            raw = socket.create_connection((host, port), timeout=5)
            ctx = ssl.create_default_context()
            tls = ctx.wrap_socket(raw, server_hostname=host)
            tls.close()
            stages.append(_stage("tls", True, "TLS handshake ok",
                                 (time.monotonic() - t0) * 1000))
        except (OSError, ssl.SSLError) as exc:
            stages.append(_stage("tls", False, f"TLS failed: {exc}",
                                 (time.monotonic() - t0) * 1000, _STAGE_HINTS["tls"]))
            return _finish_diag(url, stages)

    # 5 — http (POST the MCP initialize — status code classifies the break)
    t0 = time.monotonic()
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port, timeout=10, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=10)
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "xauusd-diag", "version": "0.1.0"},
        },
    }
    try:
        status, body = _post_json(conn, path, init, eff_key, 10)
    except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
        stages.append(_stage("http", False, f"request failed: {exc}",
                             (time.monotonic() - t0) * 1000,
                             "the gateway accepted TCP but never answered "
                             "HTTP — tunnel gateway malfunction"))
        return _finish_diag(url, stages)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if status >= 400:
        hint = hint_for_error(f"HTTP {status}")
        stages.append(_stage(
            "http", False,
            f"HTTP {status} from the tunnel gateway"
            + (f" — {body[:120]}" if body else ""),
            (time.monotonic() - t0) * 1000, hint,
        ))
        record_probe(False, "http", f"HTTP {status}")
        return _finish_diag(url, stages)
    stages.append(_stage("http", True, f"HTTP {status} — endpoint answered",
                         (time.monotonic() - t0) * 1000))

    # 6 — mcp (initialize answered; prove the JSON-RPC/MCP layer works and
    # read the terminal session truth)
    t0 = time.monotonic()
    account: dict[str, Any] = {}
    mcp_detail = "MCP initialize accepted"
    ok = True
    try:
        conn2: http.client.HTTPConnection
        if parts.scheme == "https":
            conn2 = http.client.HTTPSConnection(
                host, port, timeout=12, context=ssl.create_default_context()
            )
        else:
            conn2 = http.client.HTTPConnection(host, port, timeout=12)
        try:
            sid = None
            # re-initialize on a fresh connection (session per connection)
            st2, body2 = _post_json(conn2, path, init, eff_key, 12)
            if st2 >= 400:
                raise RuntimeError(f"initialize answered HTTP {st2}")
            try:
                resp = json.loads(body2)
                sid = (resp.get("result") or {}).get("session_id") or None
            except (ValueError, AttributeError):
                pass
            if sid:
                conn2.close()
                if parts.scheme == "https":
                    conn2 = http.client.HTTPSConnection(
                        host, port, timeout=12, context=ssl.create_default_context()
                    )
                else:
                    conn2 = http.client.HTTPConnection(host, port, timeout=12)
            acct_payload = {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get_trading_account_info", "arguments": {}},
            }
            if sid:
                conn2.sock and conn2.sock.settimeout(12)
                conn2.request(
                    "POST", path, body=json.dumps(acct_payload).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {eff_key}",
                        **({"Mcp-Session-Id": sid} if sid else {}),
                    },
                )
                r2 = conn2.getresponse()
                st3, body3 = r2.status, r2.read().decode(errors="replace")[:800]
            else:
                st3, body3 = _post_json(conn2, path, acct_payload, eff_key, 12)
            if st3 >= 400:
                raise RuntimeError(f"account tool answered HTTP {st3}")
            txt = body3
            try:
                outer = json.loads(txt)
                content = (outer.get("result") or {}).get("content") or []
                if content and isinstance(content, list):
                    txt = content[0].get("text", txt)
                account = json.loads(txt) if isinstance(txt, str) else txt
            except (ValueError, AttributeError, IndexError):
                account = {}
        finally:
            try:
                conn2.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — diagnosis never raises
        ok = False
        mcp_detail = f"MCP layer failed: {exc}"
    if ok:
        acct = account.get("account") or {}
        term = account.get("terminal") or {}
        if term.get("server_connected") is True:
            mcp_detail = (
                f"MCP ok — terminal session live: login {acct.get('login', '?')} "
                f"@ {acct.get('server', '?')} (build {term.get('build', '?')})"
            )
        else:
            mcp_detail = (
                "MCP ok — bridge reachable, but the terminal has NO broker "
                "session (log in to your Exness account on the MT5 terminal)"
            )
    stages.append(_stage("mcp", ok, mcp_detail, (time.monotonic() - t0) * 1000))
    record_probe(ok, "mcp" if ok else "mcp", mcp_detail)
    return _finish_diag(url, stages)


def _finish_diag(url: str, stages: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(stages) and all(s["ok"] for s in stages)
    failed = next((s for s in stages if not s["ok"]), None)
    verdict: str
    if ok:
        verdict = "Bridge chain healthy — terminal reachable end to end."
    else:
        verdict = f"Chain breaks at '{failed['stage']}': {failed['detail']}"
    return {
        "url": mask_url(url),
        "ok": ok,
        "stages": stages,
        "verdict": verdict,
        "hint": (failed or {}).get("hint"),
        "at": datetime.now(UTC).isoformat(),
    }


# ------------------------------------------------------------ persistence
async def _run_db(db_engine: Any, fn) -> Any:
    """One DB op `fn(conn) -> T` in a transaction — async AND sync engines
    (the D-076 dual-mode pattern: production passes the async engine from
    app.state, tests pass a sync sqlite engine)."""
    import asyncio

    try:
        from sqlalchemy.ext.asyncio import AsyncEngine

        is_async = isinstance(db_engine, AsyncEngine)
    except ImportError:  # pragma: no cover — greenlet guaranteed in prod
        is_async = False
    if is_async:
        async with db_engine.begin() as acx:
            return await acx.run_sync(fn)

    def _sync():
        with db_engine.begin() as cx:  # type: ignore[union-attr]
            return fn(cx)

    return await asyncio.to_thread(_sync)


def _load_op(conn) -> str | None:
    from sqlalchemy import text

    row = conn.execute(
        text("select value from app_config where key = 'mt5_bridge_url'")
    ).first()
    return str(row[0]).strip() if row and row[0] else None


def _persist_op(url: str, ts: str):
    from sqlalchemy import text

    def op(conn) -> None:
        conn.execute(
            text(
                "insert into app_config (key, value, updated_at) "
                "values (:k, :v, :ts) "
                "on conflict (key) do update set value = :v, updated_at = :ts"
            ),
            {"k": "mt5_bridge_url", "v": url, "ts": ts},
        )

    return op


async def load_persisted_url(db_engine: Any) -> str | None:
    """The app_config override (None = env in effect). PG + sqlite."""
    if db_engine is None:
        return None
    try:
        return await _run_db(db_engine, _load_op)
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        logger.debug("bridge url load skipped: %s", exc)
        return None


async def persist_url(db_engine: Any, url: str) -> bool:
    """Best-effort persist of the runtime override (app_config kv)."""
    if db_engine is None:
        return False
    try:
        await _run_db(db_engine, _persist_op(url, datetime.now(UTC).isoformat()))
        return True
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning("bridge url persist failed (still hot-swapped): %s", exc)
        return False
