"""CashBook Pro link — DISABLED in v2 (hard OFF, user order 21 Oct 2026).

The user's CashBook Pro already carries his own manual, up-to-date entries.
Auto-posting from this app created duplicates/mismatch risk, so NO code path
in v2 posts anything to CashBook. Integration is planned for later, once,
carefully, including previous transactions.

This module is kept only so the `cashbook_sync` table and the /integrations
page have something truthful to show: every function here is inert. Queue
rows, if any are ever created, stay at status 'pending' forever — they are a
record of what WOULD be posted after a future, deliberate integration.
"""
from db import _connect, now_iso

_CB_DIRECT = False  # integration OFF — never True in v2

CATEGORY = "Raw Material"


def queue_cash_movement(kind, ref_id, date_, paid_from, party, amount, note):
    """Record-only: stores a 'pending' row and NEVER posts. Returns
    (False, 'integration_off'). v2 routes do not call this; it exists so any
    legacy caller fails safe instead of posting."""
    con = _connect()
    try:
        con.execute(
            """INSERT INTO cashbook_sync
               (kind, ref_id, date, account, party, category, amount, note,
                status, attempts, last_error, created_at)
               VALUES (?,?,?,?,?,?,?,?, 'pending', 0, 'integration_off', ?)""",
            (kind, ref_id, date_, (paid_from or ""), party, CATEGORY,
             float(amount or 0), note or "", now_iso()),
        )
        con.commit()
    finally:
        con.close()
    return False, "integration_off"


def try_post(queue_id):
    """Integration OFF: never posts. Always (False, 'integration_off')."""
    return False, "integration_off"


def retry_pending():
    """Integration OFF: nothing is retried. Returns (0, pending_count)."""
    pend, _fail = queue_counts()
    return 0, pend


def queue_counts():
    con = _connect()
    try:
        row = con.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0),"
            " COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0)"
            " FROM cashbook_sync"
        ).fetchone()
        return row[0] or 0, row[1] or 0
    finally:
        con.close()
