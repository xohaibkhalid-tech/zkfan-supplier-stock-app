# ZK FAN Supplier, Purchase & Factory Stock System — v2 (forms edition)

Flask + SQLite, mobile-first, deliberately simple UI modelled on the user's
own chat data-entry forms. Mounted at `/supplier-stock/` inside the CashBook
Pro PythonAnywhere WSGI (DispatcherMiddleware, try/except — a mount failure
can never take CashBook down). Version flag: `v2-forms-a` (`GET /health`).

Built 21 Oct 2026 from the user's hand-entered forms data (previous balances,
payments, new purchases) — that data is SACRED and imported verbatim.

## Modules
- **Parties** — CRUD + opening (previous) balance; per-party **ledger** page
  (professional, print-clean for PDF via browser print) + ledger Excel export.
- **Purchases v2** — header (party, date, mode piece|kg, rate_per_kg, cash
  paid + cash method, note) + dynamic lines:
  `piece` (qty × rate), `dhalai` (body qty × per-body wt + cup qty × per-cup
  wt), `set` (blade sets × per-set wt; 1 set = 3 blades), `plain` (direct
  kg), `other` (extra item/service, amount). Live totals in the form.
  Full Edit / Void (audit) / Delete. Cash paid mirrors to a
  `purchase_payment` row so it appears in payments + ledger.
- **Payments v2** — amount + **discount** + **method** + kind
  `payment` | `advance` (advance can push outstanding negative = "Advance
  Rs X") + optional purchase link. Outstanding(party) =
  opening − Σ payments(amount+discount) + Σ purchases(amount − cash).
- **Payment methods** — Settings CRUD (seed: Cash, UBL, Meezan, Allied Bank);
  methods are snapshotted onto each payment.
- **Stock** — item-wise from purchase lines (piece → qty pcs; kg lines →
  weight) minus usage entries. Storekeeper: ONLY stock + usage, big simple
  screens, no money anywhere (403 on money routes).
- **Excel exports** — parties, purchases, payments (method/discount/kind),
  stock, per-party ledger.
- **Keyboard** — Enter saves entry forms, Esc = cancel/back, Delete on a
  focused list row = delete (with confirm).
- **Kept from v1** — auth + roles, `SUPPLIER_STOCK_NO_AUTH` testing bypass,
  daily auto-backup (keep 30), audit log, owner bootstrap.

## ⛔ CashBook integration — HARD OFF (user order, 21 Oct 2026)
The user's CashBook Pro already holds his own manual entries; auto-posting
would duplicate them. `cashbook_link.py` is inert: it contains **no posting
code path at all**, only queue bookkeeping; new records are saved with
`cashbook_status='na'` and no queue rows are created. `/integrations` states
this plainly. Integration is planned LATER, once, carefully, when the system
is complete.

## Data import (one-time, idempotent)
```
python3 tools/import_forms.py [--db PATH]   # backup FIRST, then import
```
Reads `import_data/forms_payments.json` + `forms_purchases.json` (the user's
own form data), imports STORED amounts/weights verbatim (never recomputed),
matches parties by exact name (union = 23), keys every row
(`source='forms'` + `external_key`) so re-runs change nothing.
Verified totals: opening Σ 6,715,000 · balance payments 18 / Σ 3,492,000 ·
purchases 29 / Σ 2,862,001 · purchase cash Σ 1,713,618 · total outstanding
Σ 4,371,383.

## Tests
```
python3 tests/test_v2.py     # 52 checks, throwaway temp DBs (needs flask+openpyxl)
```

## Schema notes (v2)
- New tables: `payment_methods`, `purchase_lines` (line math inputs + stored
  computed totals; the stored amount wins).
- Added columns via idempotent migration: purchases (mode, rate_per_kg,
  cash_paid, cash_method_id/name, source/external_key), payments (discount,
  method_id/name, kind, purchase_id, source/external_key), suppliers
  (source/external_key). Migrations are additive — a live v1 DB upgrades
  safely in place.
- `seed.py` now seeds ONLY the payment methods (import replaced v1 seeding).

## Files
| File | Purpose |
|---|---|
| `app.py` | Flask app (routes, auth, exports, backup) |
| `db.py` | SQLite schema + migrations + balance/stock helpers |
| `seed.py` | Payment-method seed only |
| `cashbook_link.py` | INERT — queue bookkeeping only, no posting |
| `tools/import_forms.py` | Idempotent forms-data import (backs up first) |
| `import_data/` | The user's forms JSON (source of truth for opening data) |
| `tests/test_v2.py` | Full local test suite |
| `templates/` | Simple mobile-first Jinja templates (CSS inlined) |
| `wsgi_mount_snippet.py` | Live-server mount snippet (v2 keeps testing bypass env) |
| `DEPLOY.md` | v1 deploy record (v2 deploy happens only on the user's word) |
