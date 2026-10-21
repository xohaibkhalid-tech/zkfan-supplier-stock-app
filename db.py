"""ZK FAN Supplier, Purchase & Factory Stock System — database layer (v2).

SQLite via stdlib. All money in Rs (REAL). Dates as ISO text (YYYY-MM-DD).
Write operations that touch stock run under a process lock + SQLite transaction.

v2 changes (forms-based model):
- payment_methods table (Settings CRUD; Cash/UBL/Meezan/Allied Bank seeded).
- purchase_lines table: one purchase (party+date) has many lines, each with a
  kind: piece | dhalai | set | plain | other.
- purchases header carries mode (piece|kg), rate_per_kg, cash_paid +
  cash_method (snapshot name), source + external_key (import idempotency).
- payments carry discount, method (id + snapshot name), kind
  (payment | advance | purchase_payment), optional purchase link,
  source + external_key.
- CashBook Pro integration is HARD OFF (user order, 21 Oct 2026): no code
  path posts to CashBook. cashbook_sync stays as a dormant log table.
"""
import os
import sqlite3
import threading
from datetime import datetime, date

VERSION = "v2.3-simplicity-a"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get(
    "SUPPLIER_STOCK_DB",
    os.path.join(BASE_DIR, "data", "supplier_stock.db"),
)

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT DEFAULT 'Other',
  phone TEXT DEFAULT '',
  opening_balance REAL DEFAULT 0,
  opening_date TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  active INTEGER DEFAULT 1,
  created_at TEXT,
  source TEXT DEFAULT '',
  external_key TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  unit TEXT DEFAULT 'kg',
  opening_stock REAL DEFAULT 0,
  min_level REAL DEFAULT 0,
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS payment_methods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER DEFAULT 1,
  sort INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS purchases (
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  supplier_id TEXT NOT NULL REFERENCES suppliers(id),
  model TEXT NOT NULL DEFAULT '',
  lot_no TEXT DEFAULT '',
  body_qty REAL DEFAULT 0,
  cup_qty REAL DEFAULT 0,
  body_wt REAL DEFAULT 0,
  cup_wt REAL DEFAULT 0,
  total_kg REAL DEFAULT 0,
  rate_kg REAL DEFAULT 0,
  amount REAL DEFAULT 0,
  pay_type TEXT DEFAULT '',
  paid_from TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  cashbook_status TEXT DEFAULT 'na',
  cashbook_ref TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  created_by TEXT DEFAULT '',
  created_at TEXT,
  void_reason TEXT DEFAULT '',
  mode TEXT DEFAULT 'piece',
  rate_per_kg REAL DEFAULT 0,
  cash_paid REAL DEFAULT 0,
  cash_method_id INTEGER DEFAULT 0,
  cash_method_name TEXT DEFAULT '',
  source TEXT DEFAULT '',
  external_key TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS purchase_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  purchase_id TEXT NOT NULL REFERENCES purchases(id),
  kind TEXT NOT NULL DEFAULT 'piece',
  name TEXT NOT NULL DEFAULT '',
  qty REAL DEFAULT 0,
  rate REAL DEFAULT 0,
  body_qty REAL DEFAULT 0,
  body_wt REAL DEFAULT 0,
  cup_qty REAL DEFAULT 0,
  cup_wt REAL DEFAULT 0,
  body_total_kg REAL DEFAULT 0,
  cup_total_kg REAL DEFAULT 0,
  weight_kg REAL DEFAULT 0,
  amount REAL DEFAULT 0,
  sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,
  date TEXT,
  supplier_id TEXT NOT NULL REFERENCES suppliers(id),
  amount REAL NOT NULL,
  paid_from TEXT DEFAULT '',
  apply_to TEXT DEFAULT 'FIFO',
  status TEXT DEFAULT 'active',
  cashbook_status TEXT DEFAULT 'na',
  cashbook_ref TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  created_by TEXT DEFAULT '',
  created_at TEXT,
  void_reason TEXT DEFAULT '',
  discount REAL DEFAULT 0,
  method_id INTEGER DEFAULT 0,
  method_name TEXT DEFAULT '',
  kind TEXT DEFAULT 'payment',
  purchase_id TEXT DEFAULT '',
  source TEXT DEFAULT '',
  external_key TEXT DEFAULT '',
  applies_to TEXT DEFAULT 'previous'
);
CREATE TABLE IF NOT EXISTS stock_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  material_id TEXT NOT NULL REFERENCES materials(id),
  type TEXT NOT NULL,
  qty REAL DEFAULT 0,
  ref_id TEXT DEFAULT '',
  balance_after REAL DEFAULT 0,
  entered_by TEXT DEFAULT '',
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cashbook_sync (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  date TEXT,
  account TEXT,
  party TEXT,
  category TEXT,
  amount REAL,
  note TEXT,
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  last_error TEXT DEFAULT '',
  created_at TEXT,
  posted_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  must_change_pw INTEGER DEFAULT 1,
  active INTEGER DEFAULT 1,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  actor TEXT,
  action TEXT,
  entity TEXT,
  entity_id TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS item_aliases (
  variant TEXT PRIMARY KEY,
  canonical TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_labels (
  canonical TEXT PRIMARY KEY,
  label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_groups (
  canonical TEXT PRIMARY KEY,
  group_name TEXT NOT NULL
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_payments_supplier_status"
    " ON payments(supplier_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_supplier_status"
    " ON purchases(supplier_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_purchase_lines_purchase"
    " ON purchase_lines(purchase_id)",
    "CREATE INDEX IF NOT EXISTS idx_stock_ledger_material_type"
    " ON stock_ledger(material_id, type)",
]

# Columns added in v2 (name -> ALTER fragment) so a live v1 DB migrates
# in place without losing a single row.
_MIGRATIONS = {
    "suppliers": [("source", "TEXT DEFAULT ''"),
                  ("external_key", "TEXT DEFAULT ''")],
    "purchases": [("mode", "TEXT DEFAULT 'piece'"),
                  ("rate_per_kg", "REAL DEFAULT 0"),
                  ("cash_paid", "REAL DEFAULT 0"),
                  ("cash_method_id", "INTEGER DEFAULT 0"),
                  ("cash_method_name", "TEXT DEFAULT ''"),
                  ("source", "TEXT DEFAULT ''"),
                  ("external_key", "TEXT DEFAULT ''")],
    "payments": [("discount", "REAL DEFAULT 0"),
                 ("method_id", "INTEGER DEFAULT 0"),
                 ("method_name", "TEXT DEFAULT ''"),
                 ("kind", "TEXT DEFAULT 'payment'"),
                 ("purchase_id", "TEXT DEFAULT ''"),
                 ("source", "TEXT DEFAULT ''"),
                 ("external_key", "TEXT DEFAULT ''"),
                 ("applies_to", "TEXT DEFAULT 'previous'")],
}


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def _migrate(con):
    for stmt in _INDEXES:
        try:
            con.execute(stmt)
        except Exception:
            pass
    for table, cols in _MIGRATIONS.items():
        try:
            existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        for col, frag in cols:
            if col not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {frag}")
    # v2.1 backfill (idempotent): a payment tied to a purchase always
    # belongs to Current — purchase-cash mirror rows and any payment row
    # carrying a purchase link. Plain balance payments stay 'previous'.
    try:
        pay_cols = {r[1] for r in con.execute("PRAGMA table_info(payments)")}
        if "applies_to" in pay_cols:
            con.execute(
                "UPDATE payments SET applies_to='current'"
                " WHERE kind='purchase_payment'"
                " OR (purchase_id IS NOT NULL AND purchase_id <> '')")
    except Exception:
        pass


def init_db():
    con = _connect()
    try:
        existed = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        con.executescript(SCHEMA)
        _migrate(con)
        if "item_aliases" not in existed:
            # One-time seed (v2.2, the user's own words): the copper wire
            # bought as 'hi star 21 number' IS '21 number' — one material,
            # display label '21 Number Copper Wire'. Seeded only when the
            # table is first created, so an owner's later Unmerge sticks.
            con.execute(
                "INSERT OR IGNORE INTO item_aliases (variant, canonical)"
                " VALUES ('hi star 21 number', '21 number')")
            con.execute(
                "INSERT OR IGNORE INTO item_labels (canonical, label)"
                " VALUES ('21 number', '21 Number Copper Wire')")
        _seed_item_groups(con)
        con.commit()
    finally:
        con.close()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_iso():
    return date.today().isoformat()


def next_id(prefix, table):
    """Next sequential id like SUP-001 / PUR-0001 / PAY-0001 / MAT-001."""
    con = _connect()
    try:
        row = con.execute(
            f"SELECT id FROM {table} WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
            (prefix + "-%",),
        ).fetchone()
        n = 1
        if row:
            try:
                n = int(row["id"].split("-")[-1]) + 1
            except (ValueError, IndexError):
                n = 1
        width = 4 if prefix in ("PUR", "PAY") else 3
        return f"{prefix}-{n:0{width}d}"
    finally:
        con.close()


def audit(actor, action, entity, entity_id, detail=""):
    con = _connect()
    try:
        con.execute(
            "INSERT INTO audit (ts, actor, action, entity, entity_id, detail)"
            " VALUES (?,?,?,?,?,?)",
            (now_iso(), actor or "", action, entity, entity_id, detail or ""),
        )
        con.commit()
    finally:
        con.close()


# ---------- payment methods ----------

DEFAULT_METHODS = ["Cash", "UBL", "Meezan", "Allied Bank"]


def seed_payment_methods():
    con = _connect()
    try:
        for i, name in enumerate(DEFAULT_METHODS):
            con.execute(
                "INSERT OR IGNORE INTO payment_methods (name, active, sort,"
                " created_at) VALUES (?,?,?,?)",
                (name, 1, i, now_iso()),
            )
        con.commit()
    finally:
        con.close()


def list_methods(active_only=True):
    con = _connect()
    try:
        q = "SELECT * FROM payment_methods"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY sort, name"
        return con.execute(q).fetchall()
    finally:
        con.close()


def method_by_id(mid):
    con = _connect()
    try:
        return con.execute("SELECT * FROM payment_methods WHERE id=?",
                           (mid,)).fetchone()
    finally:
        con.close()


def method_by_name(name):
    con = _connect()
    try:
        return con.execute(
            "SELECT * FROM payment_methods WHERE lower(name)=lower(?)",
            ((name or "").strip(),)).fetchone()
    finally:
        con.close()


# ---------- line math ----------

def _r3(x):
    return round(float(x or 0), 3)


def compute_line(kind, name="", qty=0, rate=0, body_qty=0, body_wt=0,
                 cup_qty=0, cup_wt=0, weight_kg=0, amount=0, rate_per_kg=0):
    """Normalize one purchase line and compute its totals.

    piece : amount = qty x rate
    dhalai: body_total = body_qty x body_wt; cup_total = cup_qty x cup_wt;
            weight = body_total + cup_total; amount = weight x rate_per_kg
    set   : sets are carried in body_qty, per-set weight in body_wt
            (1 set = 3 blades); weight = body_qty x body_wt;
            amount = weight x rate_per_kg
    plain : weight = weight_kg (direct kg); amount = weight x rate_per_kg
    other : amount = the entered amount (extra service/product); no weight
    """
    kind = (kind or "piece").strip()
    out = {
        "kind": kind, "name": (name or "").strip(),
        "qty": float(qty or 0), "rate": float(rate or 0),
        "body_qty": float(body_qty or 0), "body_wt": float(body_wt or 0),
        "cup_qty": float(cup_qty or 0), "cup_wt": float(cup_wt or 0),
        "body_total_kg": 0.0, "cup_total_kg": 0.0,
        "weight_kg": 0.0, "amount": 0.0,
    }
    if kind == "piece":
        out["amount"] = round(out["qty"] * out["rate"], 2)
    elif kind == "dhalai":
        out["body_total_kg"] = _r3(out["body_qty"] * out["body_wt"])
        out["cup_total_kg"] = _r3(out["cup_qty"] * out["cup_wt"])
        out["weight_kg"] = _r3(out["body_total_kg"] + out["cup_total_kg"])
        out["amount"] = round(out["weight_kg"] * float(rate_per_kg or 0), 2)
    elif kind == "set":
        out["weight_kg"] = _r3(out["body_qty"] * out["body_wt"])
        out["amount"] = round(out["weight_kg"] * float(rate_per_kg or 0), 2)
    elif kind == "plain":
        out["weight_kg"] = _r3(weight_kg)
        out["amount"] = round(out["weight_kg"] * float(rate_per_kg or 0), 2)
    elif kind == "other":
        out["amount"] = round(float(amount or 0), 2)
    else:
        raise ValueError(f"Unknown line kind: {kind}")
    return out


# ---------- balances (v2 formula) ----------

def purchase_bakaya(purchase_id, con=None):
    """What is still owed on one purchase: amount - header cash - linked
    later payments (amount+discount). Pass a shared connection on hot
    paths; otherwise a private one is opened (and closed)."""
    own = con is None
    con = con or _connect()
    try:
        p = con.execute("SELECT amount, cash_paid FROM purchases WHERE id=?",
                        (purchase_id,)).fetchone()
        if not p:
            return 0
        linked = con.execute(
            "SELECT COALESCE(SUM(amount + discount),0) FROM payments"
            " WHERE purchase_id=? AND status='active'"
            " AND kind IN ('payment','advance')",
            (purchase_id,),
        ).fetchone()[0] or 0
        return (p["amount"] or 0) - (p["cash_paid"] or 0) - linked
    finally:
        if own:
            con.close()


def _split_from(opening, prev_paid, current_paid, advance_paid, gross):
    """The v2.1 split math, shared by party_balances() (single party) and
    all_party_balances() (bulk) so both always agree to the paisa."""
    prev_remaining = max(0.0, opening - prev_paid)
    spill = max(0.0, prev_paid - opening)
    current_bakaya = gross - current_paid - spill - advance_paid
    return {
        "opening": opening,
        "prev_paid": prev_paid,
        "prev_remaining": prev_remaining,
        "spill": spill,
        "purchase_bakaya_gross": gross,
        "current_paid": current_paid,
        "advance_paid": advance_paid,
        "current_bakaya": current_bakaya,
        "total": prev_remaining + current_bakaya,
    }


def party_balances(supplier_id, con=None):
    """Previous / Current split of one party's outstanding — the single
    source of truth for every balance display (v2.1).

    Previous bucket: the opening (purana) balance minus payments the user
    marked applies_to='previous'. Paying MORE than the opening balance
    spills the excess into Current.
    Current bucket: bakaya of active purchases (amount - header cash)
    minus 'current' payments, minus the previous-spill, minus advances.
    'purchase_payment' mirror rows are never summed (the header cash_paid
    is already deducted per purchase).

    total = prev_remaining + current_bakaya always equals the old v2
    formula: opening - SUM(payment+advance rows) + SUM(purchase bakaya).

    Pass a shared connection on hot paths (dashboard, lists); otherwise
    a private one is opened (and closed).
    """
    own = con is None
    con = con or _connect()
    try:
        s = con.execute(
            "SELECT opening_balance FROM suppliers WHERE id=?", (supplier_id,)
        ).fetchone()
        opening = (s["opening_balance"] or 0) if s else 0

        def _paid(kind, applies=None):
            q = ("SELECT COALESCE(SUM(amount + discount),0) FROM payments"
                 " WHERE supplier_id=? AND status='active' AND kind=?")
            args = [supplier_id, kind]
            if applies is not None:
                q += " AND COALESCE(applies_to,'previous')=?"
                args.append(applies)
            return con.execute(q, args).fetchone()[0] or 0

        prev_paid = _paid("payment", "previous")
        current_paid = _paid("payment", "current")
        advance_paid = _paid("advance")
        gross = con.execute(
            "SELECT COALESCE(SUM(amount - cash_paid),0) FROM purchases"
            " WHERE supplier_id=? AND status='active'",
            (supplier_id,),
        ).fetchone()[0] or 0
    finally:
        if own:
            con.close()
    return _split_from(opening, prev_paid, current_paid, advance_paid, gross)


def all_party_balances(con=None, openings=None):
    """party_balances() for EVERY supplier in a constant number of
    grouped queries (v2.2 perf fix — the dashboard used to call
    party_balances() once per party: ~5 queries x N parties, each on its
    own connection, which cost ~6s on the live server's slow disk).

    Returns {supplier_id: balances-dict} identical in shape and values
    to party_balances(). `openings` may carry {id: opening_balance}
    fetched already by the caller (saves one query).
    """
    own = con is None
    con = con or _connect()
    try:
        if openings is None:
            openings = {r["id"]: (r["opening_balance"] or 0) for r in
                        con.execute("SELECT id, opening_balance"
                                    " FROM suppliers").fetchall()}
        paid = {}
        for r in con.execute(
                "SELECT supplier_id, kind,"
                " COALESCE(applies_to,'previous') AS ap,"
                " COALESCE(SUM(amount + discount),0) AS s"
                " FROM payments WHERE status='active'"
                " AND kind IN ('payment','advance')"
                " GROUP BY supplier_id, kind, ap").fetchall():
            d = paid.setdefault(r["supplier_id"],
                                {"previous": 0.0, "current": 0.0,
                                 "advance": 0.0})
            if r["kind"] == "advance":
                d["advance"] += r["s"] or 0
            else:
                d[r["ap"] if r["ap"] in ("previous", "current")
                  else "previous"] += r["s"] or 0
        gross = {r["supplier_id"]: (r["g"] or 0) for r in con.execute(
            "SELECT supplier_id,"
            " COALESCE(SUM(amount - cash_paid),0) AS g"
            " FROM purchases WHERE status='active'"
            " GROUP BY supplier_id").fetchall()}
    finally:
        if own:
            con.close()
    out = {}
    for sid, opening in openings.items():
        d = paid.get(sid, {"previous": 0.0, "current": 0.0, "advance": 0.0})
        out[sid] = _split_from(opening, d["previous"], d["current"],
                               d["advance"], gross.get(sid, 0.0))
    return out


def party_outstanding(supplier_id):
    """Total outstanding for a party (Previous remaining + Current bakaya).
    Kept as the v2 entry point; delegates to party_balances()."""
    return party_balances(supplier_id)["total"]


# v1 name kept as an alias so older templates/exports keep working.
def supplier_balance(supplier_id):
    return party_outstanding(supplier_id)


def total_outstanding():
    con = _connect()
    try:
        openings = {r["id"]: (r["opening_balance"] or 0) for r in con.execute(
            "SELECT id, opening_balance FROM suppliers"
            " WHERE active=1").fetchall()}
        bals = all_party_balances(con, openings)
    finally:
        con.close()
    return sum(b["total"] for b in bals.values())


def all_material_stocks(con=None, openings=None):
    """material_stock() for every material in 1-2 grouped queries
    (v2.2 perf fix — the dashboard used to loop material_stock() per
    material: 4 queries + a fresh connection each). `openings` may carry
    {material_id: opening_stock} fetched already by the caller."""
    own = con is None
    con = con or _connect()
    try:
        if openings is None:
            openings = {r["id"]: (r["opening_stock"] or 0) for r in
                        con.execute("SELECT id, opening_stock"
                                    " FROM materials").fetchall()}
        moves = {}
        for r in con.execute(
                "SELECT material_id, type, COALESCE(SUM(qty),0) AS q"
                " FROM stock_ledger GROUP BY material_id, type").fetchall():
            moves.setdefault(r["material_id"], {})[r["type"]] = r["q"] or 0
    finally:
        if own:
            con.close()
    out = {}
    for mid, opening in openings.items():
        mm = moves.get(mid, {})
        out[mid] = opening + mm.get("IN", 0) - mm.get("OUT", 0) \
            + mm.get("ADJUST", 0)
    return out


def total_payable():
    return total_outstanding()


# ---------- stock (item-wise, from purchase lines) ----------

def material_stock(material_id):
    con = _connect()
    try:
        m = con.execute(
            "SELECT opening_stock FROM materials WHERE id=?", (material_id,)
        ).fetchone()
        opening = (m["opening_stock"] or 0) if m else 0
        inn = con.execute(
            "SELECT COALESCE(SUM(qty),0) FROM stock_ledger"
            " WHERE material_id=? AND type='IN'", (material_id,)
        ).fetchone()[0] or 0
        out = con.execute(
            "SELECT COALESCE(SUM(qty),0) FROM stock_ledger"
            " WHERE material_id=? AND type='OUT'", (material_id,)
        ).fetchone()[0] or 0
        adj = con.execute(
            "SELECT COALESCE(SUM(qty),0) FROM stock_ledger"
            " WHERE material_id=? AND type='ADJUST'", (material_id,)
        ).fetchone()[0] or 0
        return opening + inn - out + adj
    finally:
        con.close()


def get_or_create_material(name, unit="kg"):
    name = (name or "").strip()
    if not name:
        return None
    con = _connect()
    try:
        row = con.execute(
            "SELECT id FROM materials WHERE lower(name)=lower(?)", (name,)
        ).fetchone()
        if row:
            return row["id"]
        mid = next_id("MAT", "materials")
        con.execute(
            "INSERT INTO materials (id, name, unit, opening_stock, min_level, active)"
            " VALUES (?,?,?,?,?,1)",
            (mid, name, unit, 0, 0),
        )
        con.commit()
        return mid
    finally:
        con.close()


def post_stock(date_, material_id, type_, qty, ref_id="", entered_by="", notes=""):
    """Append a stock movement; returns (movement_id, balance_after).
    IN/OUT take positive qty. ADJUST takes signed qty (correction +/-)."""
    qty = float(qty or 0)
    if type_ not in ("IN", "OUT", "ADJUST"):
        raise ValueError("Bad stock type")
    if type_ in ("IN", "OUT") and qty <= 0:
        raise ValueError("Qty must be positive")
    if type_ == "ADJUST" and qty == 0:
        raise ValueError("Adjustment qty cannot be zero")
    with _write_lock:
        bal = material_stock(material_id)
        if type_ == "IN":
            bal += qty
        elif type_ == "OUT":
            bal -= qty
        else:
            bal += qty  # ADJUST is signed
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO stock_ledger (date, material_id, type, qty, ref_id,"
                " balance_after, entered_by, notes) VALUES (?,?,?,?,?,?,?,?)",
                (date_, material_id, type_, qty, ref_id or "", bal,
                 entered_by or "", notes or ""),
            )
            con.commit()
            return cur.lastrowid, bal
        finally:
            con.close()


def clear_purchase_stock(purchase_id):
    """Remove a purchase's IN movements (used before re-posting on edit,
    and on void/delete)."""
    con = _connect()
    try:
        con.execute(
            "DELETE FROM stock_ledger WHERE ref_id=? AND type='IN'",
            (purchase_id,),
        )
        con.commit()
    finally:
        con.close()


def sync_purchase_stock(purchase, entered_by):
    """Post stock IN for every line of an active purchase:
    piece lines -> qty (pcs) under the line name;
    kg lines (dhalai/set/plain) -> weight_kg (kg) under the line name;
    'other' lines carry no stock."""
    clear_purchase_stock(purchase["id"])
    con = _connect()
    try:
        lines = con.execute(
            "SELECT * FROM purchase_lines WHERE purchase_id=? ORDER BY sort, id",
            (purchase["id"],),
        ).fetchall()
    finally:
        con.close()
    for ln in lines:
        if ln["kind"] == "piece" and (ln["qty"] or 0) > 0:
            mid = get_or_create_material(ln["name"], "pcs")
            if mid:
                post_stock(purchase["date"], mid, "IN", ln["qty"],
                           purchase["id"], entered_by,
                           f"Purchase {purchase['id']}")
        elif ln["kind"] in ("dhalai", "set", "plain") \
                and (ln["weight_kg"] or 0) > 0:
            mid = get_or_create_material(ln["name"], "kg")
            if mid:
                post_stock(purchase["date"], mid, "IN", ln["weight_kg"],
                           purchase["id"], entered_by,
                           f"Purchase {purchase['id']}")


# ---------- stock list (v2.1: purchased items ONLY; v2.2: canonical) ---

def _norm_name(name):
    """Canonical text form of an item name: lower, trimmed, internal
    whitespace collapsed — 'Copper Wire X ' and 'copper  wire x' are the
    same variant."""
    return " ".join((name or "").split()).lower()


def _alias_map(con):
    try:
        return {r["variant"]: r["canonical"] for r in con.execute(
            "SELECT variant, canonical FROM item_aliases").fetchall()}
    except Exception:
        return {}


def _label_map(con):
    try:
        return {r["canonical"]: r["label"] for r in con.execute(
            "SELECT canonical, label FROM item_labels").fetchall()}
    except Exception:
        return {}


def _group_map(con):
    try:
        return {r["canonical"]: r["group_name"] for r in con.execute(
            "SELECT canonical, group_name FROM item_groups").fetchall()}
    except Exception:
        return {}


def canonical_of(name, aliases=None):
    """Canonical key for a line/material name: its alias target when one
    is set, else its own normalized form."""
    n = _norm_name(name)
    if aliases and n in aliases:
        return aliases[n]
    return n


# Stock groups (v2.2): fixed display order + keyword default rules.
GROUP_ORDER = ["Boxes & Packaging", "Copper Wire", "Dhalai (Models)",
               "Blades", "Kits", "Bearings", "Canopy",
               "Electric Sheet & Parts", "Hardware", "Other"]

_GROUP_RULES = [
    (("dabb", "box", "carton"), "Boxes & Packaging"),
    (("number", "wire", "copper"), "Copper Wire"),
    (("bearing",), "Bearings"),
    (("kit", "acdc"), "Kits"),
    (("conopy", "canopy", "nutt"), "Canopy"),
    (("blade", "shenk"), "Blades"),
    (("flash", "magnum", "silk", "tycon", "wifi", "crown", "king"),
     "Dhalai (Models)"),
    (("excel", "armature", "sheet"), "Electric Sheet & Parts"),
    (("nakka", "jain", "insolation"), "Hardware"),
]


def default_group_for(canonical_key):
    """Keyword default group for a canonical item name (priority order
    as listed in _GROUP_RULES); unmatched items land in 'Other'."""
    k = canonical_key or ""
    for words, group in _GROUP_RULES:
        if any(w in k for w in words):
            return group
    return "Other"


def _seed_item_groups(con):
    """Assign keyword-default groups to canonical stock items that have
    no group yet (INSERT OR IGNORE — an owner's later move sticks)."""
    try:
        aliases = _alias_map(con)
        assigned = _group_map(con)
        names = con.execute(
            "SELECT DISTINCT pl.name AS name FROM purchase_lines pl"
            " JOIN purchases p ON p.id = pl.purchase_id"
            " WHERE p.status='active' AND TRIM(pl.name) <> ''").fetchall()
        for r in names:
            ck = canonical_of(r["name"], aliases)
            if ck and ck not in assigned:
                con.execute(
                    "INSERT OR IGNORE INTO item_groups (canonical,"
                    " group_name) VALUES (?,?)",
                    (ck, default_group_for(ck)))
                assigned[ck] = default_group_for(ck)
    except Exception:
        pass


def set_alias(variant_norm, canonical_key):
    con = _connect()
    try:
        con.execute(
            "INSERT INTO item_aliases (variant, canonical) VALUES (?,?)"
            " ON CONFLICT(variant) DO UPDATE SET canonical=excluded.canonical",
            (_norm_name(variant_norm), canonical_key))
        con.commit()
    finally:
        con.close()


def delete_alias(variant_norm):
    con = _connect()
    try:
        con.execute("DELETE FROM item_aliases WHERE variant=?",
                    (_norm_name(variant_norm),))
        con.commit()
    finally:
        con.close()


def list_aliases(con=None, items=None):
    """[{variant, canonical, target_name}] for the Stock page's merged
    items card; target_name is the target item's display label. Pass
    `items` (a purchased_items() result) to avoid recomputing it."""
    own = con is None
    con = con or _connect()
    try:
        rows = con.execute(
            "SELECT variant, canonical FROM item_aliases"
            " ORDER BY variant").fetchall()
        labels = _label_map(con)
    finally:
        if own:
            con.close()
    names = {}
    if rows:
        if items is None:
            items = purchased_items()
        names = {it["key"]: it["name"] for it in items}
    return [{"variant": r["variant"], "canonical": r["canonical"],
             "target_name": names.get(r["canonical"])
             or labels.get(r["canonical"]) or r["canonical"]}
            for r in rows]


def set_label(canonical_key, label):
    label = (label or "").strip()
    con = _connect()
    try:
        if label:
            con.execute(
                "INSERT INTO item_labels (canonical, label) VALUES (?,?)"
                " ON CONFLICT(canonical) DO UPDATE SET"
                " label=excluded.label",
                (canonical_key, label))
        else:
            con.execute("DELETE FROM item_labels WHERE canonical=?",
                        (canonical_key,))
        con.commit()
    finally:
        con.close()


def set_item_group(canonical_key, group_name):
    group_name = (group_name or "").strip()
    if not group_name:
        return
    con = _connect()
    try:
        con.execute(
            "INSERT INTO item_groups (canonical, group_name) VALUES (?,?)"
            " ON CONFLICT(canonical) DO UPDATE SET"
            " group_name=excluded.group_name",
            (canonical_key, group_name))
        con.commit()
    finally:
        con.close()


def merge_items(src_key, dst_key):
    """Merge canonical item src into dst (stock display only — purchase
    lines are never touched): every variant name of src gets an alias to
    dst, aliases that pointed at src are repointed, src's label row is
    dropped (dst keeps its own label)."""
    if not src_key or not dst_key or src_key == dst_key:
        return False
    src = purchased_item(src_key)
    if not src:
        return False
    con = _connect()
    try:
        for original in src["variants"]:
            con.execute(
                "INSERT INTO item_aliases (variant, canonical)"
                " VALUES (?,?) ON CONFLICT(variant) DO UPDATE SET"
                " canonical=excluded.canonical",
                (_norm_name(original), dst_key))
        con.execute(
            "UPDATE item_aliases SET canonical=? WHERE canonical=?",
            (dst_key, src_key))
        con.execute("DELETE FROM item_labels WHERE canonical=?",
                    (src_key,))
        con.commit()
    finally:
        con.close()
    return True


def purchased_items(con=None):
    """The stock list, recomputed from purchase_lines of ACTIVE purchases
    and grouped by CANONICAL item (v2.2): the same material bought under
    slightly different line names — or merged by the owner through
    item_aliases — counts as ONE item. Purchase history is never
    altered; this is stock-display logic only.

    An item appears ONLY if it was actually purchased (legacy
    materials-table rows with no purchase history never appear).
    Usage (OUT) and corrections (ADJUST) are summed across the
    stock_ledger rows of ALL materials whose names belong to the
    canonical group — a usage entry against the combined item reduces
    the combined remaining.

    Each item dict: key/canonical, name (display label: explicit label
    if set, else the most frequent original line name), label (explicit
    label or ''), variants (original line names), group, unit,
    material_id (primary material — the one sharing the canonical name,
    else the most frequent variant's), material_ids, in/out/adj/rem in
    qty (pcs) and kg, low.
    """
    own = con is None
    con = con or _connect()
    try:
        rows = con.execute(
            "SELECT TRIM(pl.name) AS name, pl.kind AS kind,"
            " COALESCE(SUM(pl.qty),0) AS q,"
            " COALESCE(SUM(pl.weight_kg),0) AS kg, COUNT(*) AS n"
            " FROM purchase_lines pl"
            " JOIN purchases p ON p.id = pl.purchase_id"
            " WHERE p.status='active' AND TRIM(pl.name) <> ''"
            " GROUP BY TRIM(pl.name), pl.kind").fetchall()
        mats = con.execute("SELECT * FROM materials").fetchall()
        moves = con.execute(
            "SELECT material_id, type, COALESCE(SUM(qty),0) AS q"
            " FROM stock_ledger GROUP BY material_id, type").fetchall()
        aliases = _alias_map(con)
        labels = _label_map(con)
        groups = _group_map(con)
    finally:
        if own:
            con.close()
    mat_by_norm = {}
    for m in mats:
        mat_by_norm.setdefault(_norm_name(m["name"]), m)
    mv = {}
    for r in moves:
        mv.setdefault(r["material_id"], {})[r["type"]] = r["q"] or 0

    # variant (normalized name) -> aggregates + original-name line counts
    variants = {}
    for r in rows:
        vn = _norm_name(r["name"])
        if not vn:
            continue
        v = variants.setdefault(vn, {"counts": {}, "in_qty": 0.0,
                                     "in_kg": 0.0})
        v["counts"][r["name"]] = v["counts"].get(r["name"], 0) + (r["n"] or 0)
        if r["kind"] == "piece":
            v["in_qty"] += r["q"] or 0
        elif r["kind"] in ("dhalai", "set", "plain"):
            v["in_kg"] += r["kg"] or 0

    # canonical key -> merged item
    items = {}
    for vn, v in variants.items():
        ck = aliases.get(vn, vn)
        it = items.setdefault(ck, {"key": ck, "canonical": ck,
                                   "in_qty": 0.0, "in_kg": 0.0,
                                   "_counts": {}, "_variants": set()})
        it["in_qty"] += v["in_qty"]
        it["in_kg"] += v["in_kg"]
        for orig, n in v["counts"].items():
            it["_counts"][orig] = it["_counts"].get(orig, 0) + n
            it["_variants"].add(orig)

    out = []
    for ck, it in items.items():
        if it["in_qty"] <= 0 and it["in_kg"] <= 0:
            continue  # no purchase history (e.g. only 'other' lines)
        # most frequent original line name (deterministic tie-break)
        best = sorted(it["_counts"].items(),
                      key=lambda kv: (-kv[1], kv[0].lower()))[0][0]
        it["variants"] = sorted(it["_variants"])
        it["label"] = labels.get(ck, "")
        it["name"] = it["label"] or best
        # materials of this group (by normalized material name)
        group_mats = []
        seen_mid = set()
        for vn, v in variants.items():
            if aliases.get(vn, vn) != ck:
                continue
            m = mat_by_norm.get(vn)
            if m and m["id"] not in seen_mid:
                group_mats.append(m)
                seen_mid.add(m["id"])
        primary = mat_by_norm.get(ck)
        if primary is None and group_mats:
            # most frequent variant's material
            for orig in sorted(it["_counts"].items(),
                               key=lambda kv: (-kv[1], kv[0].lower())):
                primary = mat_by_norm.get(_norm_name(orig[0]))
                if primary:
                    break
        it["material_id"] = primary["id"] if primary else ""
        it["material_ids"] = [m["id"] for m in group_mats]
        it["unit"] = (primary["unit"] if primary and primary["unit"]
                      else ("pcs" if it["in_qty"] else "kg"))
        used = sum((mv.get(m["id"], {}).get("OUT", 0) or 0)
                   for m in group_mats)
        adj = sum((mv.get(m["id"], {}).get("ADJUST", 0) or 0)
                  for m in group_mats)
        it["out_qty"] = used if it["unit"] == "pcs" else 0.0
        it["out_kg"] = 0.0 if it["unit"] == "pcs" else used
        it["adj_qty"] = adj if it["unit"] == "pcs" else 0.0
        it["adj_kg"] = 0.0 if it["unit"] == "pcs" else adj
        it["rem_qty"] = it["in_qty"] - it["out_qty"] + it["adj_qty"]
        it["rem_kg"] = it["in_kg"] - it["out_kg"] + it["adj_kg"]
        rem_primary = it["rem_qty"] if it["unit"] == "pcs" else it["rem_kg"]
        it["low"] = bool(primary and primary["min_level"]
                         and rem_primary < primary["min_level"])
        it["group"] = groups.get(ck) or default_group_for(ck)
        del it["_counts"], it["_variants"]
        out.append(it)
    out.sort(key=lambda x: x["name"].lower())
    return out


def purchased_item(name, con=None):
    """One canonical purchased item, resolved flexibly: by canonical
    key, by display label, by alias variant, or by any original line
    name (all case/space-insensitive). None when not purchased."""
    n = _norm_name(name)
    if not n:
        return None
    items = purchased_items(con=con)
    for it in items:
        if it["key"] == n:
            return it
    for it in items:
        if _norm_name(it["name"]) == n:
            return it
    own = con is None
    con = con or _connect()
    try:
        aliases = _alias_map(con)
    finally:
        if own:
            con.close()
    ck = aliases.get(n, n)
    for it in items:
        if it["key"] == ck:
            return it
    return None


def stock_groups(items=None, con=None):
    """purchased_items() organised into the fixed group order (unknown
    groups after, alphabetical), each with item count + remaining
    totals for the Stock page's accordion summaries."""
    if items is None:
        items = purchased_items(con=con)
    by = {}
    for it in items:
        by.setdefault(it["group"], []).append(it)
    names = [g for g in GROUP_ORDER if g in by] \
        + sorted(g for g in by if g not in GROUP_ORDER)
    return [{"name": n, "rows": by[n], "count": len(by[n]),
             "rem_qty": sum(i["rem_qty"] for i in by[n]),
             "rem_kg": sum(i["rem_kg"] for i in by[n])} for n in names]
