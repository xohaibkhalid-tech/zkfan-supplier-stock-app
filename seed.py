"""Seed for ZK FAN Supplier & Stock System v2.

v2 seeds ONLY the payment methods (Cash, UBL, Meezan, Allied Bank).
Suppliers, purchases and payments come from the user's own forms data via
tools/import_forms.py — the v1 seed data (15 draft lots / Sep-2026 payments
against differently-named suppliers) is retired and must never be inserted
again, or it would corrupt the forms-based totals.
"""
from db import init_db, seed_payment_methods


def seed():
    init_db()
    seed_payment_methods()
    print("seed ok: payment methods (Cash, UBL, Meezan, Allied Bank)")


if __name__ == "__main__":
    seed()
