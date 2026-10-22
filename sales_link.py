"""Sales link (v4.0, Route A — user-approved 22 Oct 2026).

The ZK FAN Sales system lives in a Google Apps Script + private Google
Sheet. The sheet is NEVER made public (user's data rule): instead the
script exposes one key-protected, read-only JSON action
(?action=salesExport&key=...) and this module fetches it server-side,
storing a full snapshot in local tables (sales_lines / sales_passes).
Every sync REPLACES the snapshot inside one transaction, so sheet
edits and deletions are mirrored exactly and re-syncs never duplicate.

Config (export URL + key) lives in the settings table — set by the
owner on the Links page, never in code or repos.
"""
import json
import urllib.parse
import urllib.request

import db


class SalesLinkError(Exception):
    pass


# ---------- config (settings table) ----------

def _setting(key, default=""):
    con = db._connect()
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?",
                          (key,)).fetchone()
        return (row["value"] if row else default) or default
    finally:
        con.close()


def _set_setting(key, value):
    con = db._connect()
    try:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        con.commit()
    finally:
        con.close()


def get_config():
    url = _setting("sales_export_url")
    key = _setting("sales_sync_key")
    return {"url": url, "key": key, "configured": bool(url and key)}


def set_config(url, key):
    _set_setting("sales_export_url", (url or "").strip())
    if key and key.strip():
        _set_setting("sales_sync_key", key.strip())


# ---------- fetch + sync ----------

def fetch_export(url, key, timeout=25):
    """GET the Apps Script export JSON. Raises SalesLinkError."""
    sep = "&" if "?" in url else "?"
    full = (url + sep + "action=salesExport&key="
            + urllib.parse.quote(key, safe=""))
    req = urllib.request.Request(
        full, headers={"User-Agent": "zkfan-supplier-stock/4.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — surface as one clean error
        raise SalesLinkError(f"Sales app se rabta nahi ho saka: {e}")
    try:
        data = json.loads(raw)
    except Exception:
        raise SalesLinkError("Sales app ne ghalat jawab bheja (JSON nahi)")
    if not isinstance(data, dict) or not data.get("ok"):
        err = (data or {}).get("error", "unknown") if isinstance(
            data, dict) else "unknown"
        raise SalesLinkError(f"Sales export fail: {err} — key check karein")
    return data


def sync_sales():
    """Fetch the export and replace the local snapshot atomically.
    Returns a summary dict. Raises SalesLinkError on any problem —
    the old snapshot is left untouched in that case."""
    cfg = get_config()
    if not cfg["configured"]:
        raise SalesLinkError("Sales link abhi setup nahi hai — Links page"
                             " par URL aur key dalein")
    data = fetch_export(cfg["url"], cfg["key"])
    sales = data.get("sales") or []
    passes = data.get("passes") or []
    con = db._connect()
    try:
        con.execute("DELETE FROM sales_lines")
        con.execute("DELETE FROM sales_passes")
        for r in sales:
            con.execute(
                "INSERT INTO sales_lines (qr_id, gatepass, date, party,"
                " order_type, model, qty, price, gross, net, received,"
                " balance, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("qr", ""), r.get("gp", ""), r.get("date", ""),
                 r.get("party", ""), r.get("otype", ""),
                 r.get("model", ""), r.get("qty", 0) or 0,
                 r.get("price", 0) or 0, r.get("gross", 0) or 0,
                 r.get("net", 0) or 0, r.get("received", 0) or 0,
                 r.get("balance", 0) or 0, r.get("status", "")))
        for r in passes:
            con.execute(
                "INSERT INTO sales_passes (qr_id, gatepass, date, status,"
                " fans, party, cod_amount, cod_received)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (r.get("qr", ""), r.get("gp", ""), r.get("date", ""),
                 r.get("status", ""), r.get("fans", 0) or 0,
                 r.get("party", ""), r.get("cod_amount", 0) or 0,
                 r.get("cod_received", 0) or 0))
        t = totals(con)
        con.execute(
            "INSERT INTO sales_sync_log (at, lines, passes, fans, net,"
            " note) VALUES (?,?,?,?,?,?)",
            (db.now_iso(), len(sales), len(passes), t["fans"], t["net"],
             "ok"))
        con.commit()
    finally:
        con.close()
    return {"lines": len(sales), "passes": len(passes),
            "fans": t["fans"], "net": t["net"], "at": db.now_iso()}


def last_sync():
    con = db._connect()
    try:
        row = con.execute(
            "SELECT * FROM sales_sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def is_stale(max_age_seconds=3600):
    ls = last_sync()
    if not ls:
        return True
    try:
        from datetime import datetime, timezone
        at = datetime.fromisoformat(ls["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - at).total_seconds() \
            > max_age_seconds
    except Exception:
        return True


def maybe_autosync():
    """Best-effort refresh used by the sales/report pages: only when
    configured and the snapshot is stale. Never raises."""
    try:
        if get_config()["configured"] and is_stale():
            sync_sales()
            return True
    except Exception:
        pass
    return False


# ---------- reads (local snapshot) ----------

def _live(status):
    s = (status or "").lower()
    return not any(w in s for w in ("void", "cancel", "delete"))


def _lines(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM sales_lines").fetchall() if _live(r["status"])]


def totals(con=None):
    own = con is None
    con = con or db._connect()
    try:
        lines = _lines(con)
        npass = con.execute(
            "SELECT COUNT(*) AS c FROM sales_passes").fetchone()["c"]
    finally:
        if own:
            con.close()
    fans = sum(r["qty"] or 0 for r in lines)
    gross = sum(r["gross"] or 0 for r in lines)
    net = sum(r["net"] or 0 for r in lines)
    received = sum(r["received"] or 0 for r in lines)
    return {"fans": fans, "gross": gross, "net": net,
            "received": received, "bakaya": net - received,
            "passes": npass, "lines": len(lines)}


def by_model():
    con = db._connect()
    try:
        lines = _lines(con)
    finally:
        con.close()
    out = {}
    for r in lines:
        d = out.setdefault(r["model"] or "(no model)",
                           {"model": r["model"] or "(no model)",
                            "fans": 0.0, "net": 0.0, "received": 0.0})
        d["fans"] += r["qty"] or 0
        d["net"] += r["net"] or 0
        d["received"] += r["received"] or 0
    return sorted(out.values(), key=lambda d: -d["fans"])


def by_month():
    con = db._connect()
    try:
        lines = _lines(con)
    finally:
        con.close()
    out = {}
    for r in lines:
        m = (r["date"] or "")[:7] or "(no date)"
        d = out.setdefault(m, {"month": m, "fans": 0.0, "net": 0.0})
        d["fans"] += r["qty"] or 0
        d["net"] += r["net"] or 0
    return [out[k] for k in sorted(out)]


def recent_passes(limit=15):
    con = db._connect()
    try:
        ps = [dict(r) for r in con.execute(
            "SELECT * FROM sales_passes ORDER BY date DESC, id DESC"
            " LIMIT ?", (limit,)).fetchall()]
        lines = _lines(con)
    finally:
        con.close()
    by_qr = {}
    for r in lines:
        by_qr.setdefault(r["qr_id"], []).append(r)
    for p in ps:
        ls = by_qr.get(p["qr_id"], [])
        p["models"] = ", ".join(
            f"{(r['model'] or '')} ×{('%g' % (r['qty'] or 0))}" for r in ls)
        p["net"] = sum(r["net"] or 0 for r in ls)
        p["received"] = sum(r["received"] or 0 for r in ls)
        p["fans_calc"] = sum(r["qty"] or 0 for r in ls)
    return ps


def sold_by_canonical():
    """Fans sold per canonical model key — the report's sales side,
    using the same canonicalisation (+ owner aliases) as stock."""
    con = db._connect()
    try:
        aliases = db._alias_map(con)
        lines = _lines(con)
    finally:
        con.close()
    out = {}
    for r in lines:
        ck = db.canonical_of(r["model"] or "", aliases)
        if ck:
            out[ck] = out.get(ck, 0.0) + (r["qty"] or 0)
    return out
