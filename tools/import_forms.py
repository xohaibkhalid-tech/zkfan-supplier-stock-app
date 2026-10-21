#!/usr/bin/env python3
"""Import the user's forms data into the Supplier & Stock DB (v2).

Sources (his own hand-entered data — values are SACRED, imported verbatim,
never recomputed):
  import_data/forms_payments.json   — 19 parties, opening balances, 18 payments
  import_data/forms_purchases.json  — 23 parties, 29 purchases with lines

Behaviour:
  * Takes a timestamped backup of the DB BEFORE writing anything.
  * Idempotent: parties matched by exact name; purchases/payments carry a
    content-hash external_key — running twice changes nothing.
  * Purchase cash_paid > 0 also creates a payment row kind='purchase_payment'
    linked to the purchase; its method is parsed from the purchase note
    ('meezan' -> Meezan, 'allied' -> Allied Bank, else Cash). The purchase
    note itself is preserved verbatim on the purchase.
  * Prints a totals report at the end.

Usage: python3 tools/import_forms.py [--db PATH] [--data DIR]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import db  # noqa: E402


def _key(*parts):
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8"))
    return h.hexdigest()[:24]


def _next(con, prefix, table):
    """next_id on the SAME connection (sees uncommitted rows)."""
    row = con.execute(
        f"SELECT id FROM {table} WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (prefix + "-%",)).fetchone()
    n = 1
    if row:
        try:
            n = int(row[0].split("-")[-1]) + 1
        except (ValueError, IndexError):
            n = 1
    width = 4 if prefix in ("PUR", "PAY") else 3
    return f"{prefix}-{n:0{width}d}"


def parse_method(note):
    n = (note or "").lower()
    if "meezan" in n:
        return "Meezan"
    if "allied" in n:
        return "Allied Bank"
    return "Cash"


def method_id_for(con, name):
    row = con.execute(
        "SELECT id FROM payment_methods WHERE lower(name)=lower(?)",
        (name,)).fetchone()
    return row["id"] if row else 0


def backup_db(stamp):
    bdir = os.path.join(REPO, "backups")
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, f"supplier_stock_preimport_{stamp}.db")
    src = sqlite3.connect(db.DB_PATH)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--data", default=os.path.join(REPO, "import_data"))
    args = ap.parse_args()
    db.DB_PATH = os.path.abspath(args.db)

    payments_data = json.load(
        open(os.path.join(args.data, "forms_payments.json"), encoding="utf-8"))
    purchases_data = json.load(
        open(os.path.join(args.data, "forms_purchases.json"), encoding="utf-8"))

    db.init_db()
    db.seed_payment_methods()

    stamp = db.now_iso().replace(":", "").replace("-", "").replace("T", "")[:14]
    bpath = backup_db(stamp)
    print(f"backup (before import): {bpath}")

    ts = db.now_iso()
    con = db._connect()
    stats = {"parties_new": 0, "purchases_new": 0, "payments_new": 0,
             "purpay_new": 0, "skipped": 0}
    try:
        # ---------- parties (union of both files, exact-name match) ------
        parties = {}
        for p in payments_data["parties"]:
            parties[p["name"]] = {
                "detail": p.get("detail") or "",
                "opening_balance": float(p.get("balance") or 0),
            }
        for p in purchases_data["parties"]:
            if p["name"] not in parties:
                parties[p["name"]] = {
                    "detail": p.get("detail") or "",
                    "opening_balance": 0.0,
                }
            elif not parties[p["name"]]["detail"]:
                parties[p["name"]]["detail"] = p.get("detail") or ""

        supplier_ids = {}
        for name, info in parties.items():
            row = con.execute("SELECT id FROM suppliers WHERE name=?",
                              (name,)).fetchone()
            if row:
                supplier_ids[name] = row["id"]
                # Forms are the source of truth: a pre-existing party row
                # (e.g. from the old v1 seed, all carrying balance 0) must
                # still receive its forms opening balance.
                con.execute(
                    "UPDATE suppliers SET opening_balance=? WHERE id=?",
                    (info["opening_balance"], row["id"]))
                continue
            sid = _next(con, "SUP", "suppliers")
            con.execute(
                "INSERT INTO suppliers (id, name, category, phone,"
                " opening_balance, opening_date, notes, active, created_at,"
                " source, external_key) VALUES (?,?,?,?,?, '', '', 1, ?,"
                " 'forms', ?)",
                (sid, name, info["detail"] or "Other", "",
                 info["opening_balance"], ts, "forms:party"),
            )
            supplier_ids[name] = sid
            stats["parties_new"] += 1
        con.commit()

        # ---------- purchases (+ lines + purchase_payment rows) ----------
        for party in purchases_data["parties"]:
            sid = supplier_ids[party["name"]]
            for pur in party.get("purchases", []):
                canon = json.dumps(pur, sort_keys=True, ensure_ascii=False)
                ekey = "forms:pur:" + _key(party["name"], canon)
                exists = con.execute(
                    "SELECT id FROM purchases WHERE external_key=?",
                    (ekey,)).fetchone()
                if exists:
                    stats["skipped"] += 1
                    continue
                cash = float(pur.get("cashPaid") or 0)
                note = pur.get("note") or ""
                mname = parse_method(note) if cash > 0 else ""
                pid = _next(con, "PUR", "purchases")
                total_kg = sum(float(ln.get("weightKg") or 0)
                               for ln in pur.get("lines", []))
                con.execute(
                    """INSERT INTO purchases
                       (id, date, supplier_id, model, lot_no, total_kg, amount,
                        status, cashbook_status, notes, created_by, created_at,
                        mode, rate_per_kg, cash_paid, cash_method_id,
                        cash_method_name, source, external_key)
                       VALUES (?,?,?, '', '', ?, ?, 'active', 'na', ?, 'import',
                               ?, ?, ?, ?, ?, ?, 'forms', ?)""",
                    (pid, pur.get("date") or "", sid, total_kg,
                     float(pur.get("amount") or 0), note, ts,
                     pur.get("mode") or "piece",
                     float(pur.get("ratePerKg") or 0), cash,
                     method_id_for(con, mname) if mname else 0, mname, ekey),
                )
                for i, ln in enumerate(pur.get("lines", [])):
                    kind = ln.get("kind") or (
                        "piece" if pur.get("mode") == "piece" else "plain")
                    bq = float(ln.get("bodyQty") or 0)
                    bw = float(ln.get("bodyWeightKg") or 0)
                    cq = float(ln.get("cupQty") or 0)
                    cw = float(ln.get("cupWeightKg") or 0)
                    con.execute(
                        """INSERT INTO purchase_lines
                           (purchase_id, kind, name, qty, rate, body_qty,
                            body_wt, cup_qty, cup_wt, body_total_kg,
                            cup_total_kg, weight_kg, amount, sort)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pid, kind, ln.get("name") or "",
                         float(ln.get("qty") or 0),
                         float(ln.get("rate") or 0), bq, bw, cq, cw,
                         round(bq * bw, 3), round(cq * cw, 3),
                         float(ln.get("weightKg") or 0),
                         float(ln.get("amount") or 0), i),
                    )
                stats["purchases_new"] += 1
                if cash > 0:
                    pekey = "forms:purpay:" + _key(ekey)
                    pexists = con.execute(
                        "SELECT id FROM payments WHERE external_key=?",
                        (pekey,)).fetchone()
                    if not pexists:
                        payid = _next(con, "PAY", "payments")
                        con.execute(
                            """INSERT INTO payments
                               (id, date, supplier_id, amount, status,
                                cashbook_status, notes, created_by, created_at,
                                discount, method_id, method_name, kind,
                                purchase_id, source, external_key, applies_to)
                               VALUES (?,?,?, ?, 'active', 'na', ?, 'import',
                                       ?, 0, ?, ?, 'purchase_payment', ?,
                                       'forms', ?, 'current')""",
                            (payid, pur.get("date") or "", sid, cash, note,
                             ts, method_id_for(con, mname), mname, pid, pekey),
                        )
                        stats["purpay_new"] += 1
                con.commit()
                db.sync_purchase_stock(
                    {"id": pid, "date": pur.get("date") or ""}, "import")

        # ---------- balance payments (against previous balances) ---------
        for party in payments_data["parties"]:
            sid = supplier_ids[party["name"]]
            for pm in party.get("payments", []):
                canon = json.dumps(pm, sort_keys=True, ensure_ascii=False)
                ekey = "forms:pay:" + _key(party["name"], canon)
                exists = con.execute(
                    "SELECT id FROM payments WHERE external_key=?",
                    (ekey,)).fetchone()
                if exists:
                    stats["skipped"] += 1
                    continue
                note = pm.get("note") or ""
                mname = parse_method(note)
                payid = _next(con, "PAY", "payments")
                con.execute(
                    """INSERT INTO payments
                       (id, date, supplier_id, amount, status,
                        cashbook_status, notes, created_by, created_at,
                        discount, method_id, method_name, kind, purchase_id,
                        source, external_key, applies_to)
                       VALUES (?,?,?, ?, 'active', 'na', ?, 'import', ?, 0,
                               ?, ?, 'payment', '', 'forms', ?, 'previous')""",
                    (payid, pm.get("date") or None, sid,
                     float(pm.get("amount") or 0), note, ts,
                     method_id_for(con, mname), mname, ekey),
                )
                stats["payments_new"] += 1
        con.commit()
    finally:
        con.close()

    # ---------- report ----------
    con = db._connect()
    try:
        n_parties = con.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
        sum_open = con.execute(
            "SELECT COALESCE(SUM(opening_balance),0) FROM suppliers"
        ).fetchone()[0]
        bp = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments"
            " WHERE kind='payment' AND source='forms' AND status='active'"
        ).fetchone()
        pp = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments"
            " WHERE kind='purchase_payment' AND status='active'"
        ).fetchone()
        pur = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0),"
            " COALESCE(SUM(cash_paid),0) FROM purchases"
            " WHERE status='active'"
        ).fetchone()
    finally:
        con.close()
    total_out = db.total_outstanding()
    print("=== import report ===")
    print(f"new this run: parties={stats['parties_new']}"
          f" purchases={stats['purchases_new']}"
          f" balance_payments={stats['payments_new']}"
          f" purchase_payments={stats['purpay_new']}"
          f" skipped_existing={stats['skipped']}")
    print(f"parties total:            {n_parties}")
    print(f"opening balances sum:     Rs {sum_open:,.0f}   (expected 6,715,000)")
    print(f"balance payments:         {bp[0]} rows, Rs {bp[1]:,.0f}   (expected 18 / 3,492,000)")
    print(f"purchases:                {pur[0]} rows, Rs {pur[1]:,.0f}   (expected 29 / 2,862,001)")
    print(f"purchase cash (headers):  Rs {pur[2]:,.0f}   (expected 1,713,618)")
    print(f"purchase_payment rows:    {pp[0]} rows, Rs {pp[1]:,.0f}")
    print(f"total party outstanding:  Rs {total_out:,.0f}   (expected 4,371,383)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
