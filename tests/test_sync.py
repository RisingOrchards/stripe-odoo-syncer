"""Tests for stripe_odoo_sync against fake Stripe and Odoo APIs. Run: python3 -m unittest -v
All data here is made up."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stripe_odoo_sync as s  # noqa: E402


def ts(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


CHARGE = {"id": "ch_1", "object": "charge", "description": "S00001", "customer": "cus_1",
          "billing_details": {"name": "Jane Doe"}}


def account():
    """A small Stripe history: paid order, refunded payment, Billing fee, payout. Ends at 300.00."""
    return [
        {"id": "txn_c1", "type": "charge", "amount": 50000, "fee": 1480, "currency": "usd",
         "created": ts("2026-03-02T15:00:00"), "source": CHARGE},
        {"id": "txn_p1", "type": "payment", "amount": 10000, "fee": 320, "currency": "usd",
         "created": ts("2026-03-03T15:00:00"),
         "source": {"id": "py_1", "object": "charge", "description": "Subscription", "customer": None,
                    "billing_details": {"name": "John Roe"}}},
        {"id": "txn_r1", "type": "refund", "amount": -10000, "fee": 0, "currency": "usd",
         "created": ts("2026-03-03T16:00:00"), "source": {"id": "re_1", "object": "refund", "charge": "ch_1"}},
        {"id": "txn_f1", "type": "stripe_fee", "amount": -70, "fee": 0, "currency": "usd",
         "created": ts("2026-03-03T23:00:00"), "description": "Billing - Usage Fee", "source": None},
        {"id": "txn_po", "type": "payout", "amount": -8130, "fee": 0, "currency": "usd",
         "created": ts("2026-03-05T05:00:00"), "available_on": ts("2026-03-06T00:00:00"),
         "source": {"id": "po_1", "object": "payout"}},
    ]  # net: 485.20 + 96.80 - 100 - 0.70 - 81.30 = 400.00


def evaluate(domain, rec):
    """Minimal Odoo domain evaluator (prefix &, |; implicit AND) for the fake."""
    stack = []
    for t in reversed(domain):
        if t == "&":
            stack.append(stack.pop() and stack.pop())
        elif t == "|":
            stack.append(stack.pop() or stack.pop())
        else:
            f, op, v = t
            x = rec.get(f, False)
            stack.append({"=": lambda: x == v, "in": lambda: x in v,
                          "=like": lambda: bool(x) and x.startswith(v.rstrip("%"))}[op]())
    return all(stack)


class Fakes:
    """Fake Stripe + Odoo 19 (JSON-2) behind stripe_odoo_sync.http_json."""

    def __init__(self, bts, balance_cents, opening=0.0):
        self.bts, self.balances, self.opening = bts, list(balance_cents), opening
        self.lines, self.statements = [], []

    def __call__(self, url, *, data=None, headers=None, method=None):
        body = json.loads(data) if data else {}
        if url.startswith(s.Stripe.BASE):
            path = urllib.parse.urlparse(url).path
            if path.endswith("/balance_transactions"):
                return {"data": list(reversed(self.bts)), "has_more": False}
            if path.endswith("/balance"):
                amt = self.balances.pop(0) if len(self.balances) > 1 else self.balances[0]
                return {"available": [{"amount": amt, "currency": "usd"}], "pending": []}
            if path.endswith("/customers/cus_1"):
                return {"id": "cus_1", "description": "Odoo Partner: Jane Doe (id: 42)"}
            if path.endswith("/charges/ch_1"):
                return CHARGE
            raise AssertionError(f"unexpected Stripe call {url}")

        assert headers["Authorization"] == "bearer test-key"
        model, meth = url.split("/json/2/")[1].split("/")
        if model == "account.journal":
            return [{"id": 13, "name": "Stripe", "currency_id": [1, "USD"], "company_id": [1, "Co"]}]
        if model == "res.partner":
            return [i for i in body["domain"][0][2] if i == 42]
        if model == "account.bank.statement.line":
            if meth == "create":
                ids = []
                for v in body["vals_list"]:
                    v.update(id=len(self.lines) + 1, statement_id=False)
                    self.lines.append(v)
                    ids.append(v["id"])
                return ids
            rows = sorted((l for l in self.lines if evaluate(body["domain"], l)),
                          key=lambda l: (l["date"], l["id"]), reverse=body.get("order") == "date desc")
            rows = rows[: body.get("limit") or None]
            return [{k: r.get(k, False) for k in ["id"] + body.get("fields", [])} for r in rows]
        if model == "account.bank.statement":
            if meth == "create":
                v = body["vals_list"][0]
                ids = v["line_ids"][0][2]
                prev = self.statements[-1]["balance_end_real"] if self.statements else self.opening
                between = sum(l["amount"] for l in self.lines if not l["statement_id"] and l["id"] not in ids)
                st = {"id": len(self.statements) + 1, "balance_end_real": v["balance_end_real"],
                      "balance_start": round(prev + between, 2)}
                for l in self.lines:
                    if l["id"] in ids:
                        l["statement_id"] = st["id"]
                st["balance_end"] = round(st["balance_start"] + sum(l["amount"] for l in self.lines
                                                                    if l["id"] in ids), 2)
                self.statements.append(st)
                return [st["id"]]
            return [self.statements[body["ids"][0] - 1]]
        raise AssertionError(f"unexpected Odoo call {url}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"STRIPE_API_KEY": "rk_test", "ODOO_URL": "https://odoo.example", "ODOO_DB": "db",
               "ODOO_API_KEY": "test-key", "SYNC_START": "2026-03-01", "SYNC_TZ": "America/New_York",
               "SYNC_LOCK_FILE": os.path.join(self.tmp.name, "lock"), "LOG_LEVEL": "CRITICAL",
               "ALERT_EMAIL": "alerts@example.com", "SMTP_HOST": "smtp.example"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def use(self, fakes):
        p = mock.patch.object(s, "http_json", fakes)
        p.start()
        self.addCleanup(p.stop)
        return fakes


class TestConversion(Base):
    def test_lines(self):
        f = self.use(Fakes(account(), [40000]))
        lines, cur = s.fetch_lines(s.Stripe("k"), datetime(2026, 3, 1).date(), None,
                                   s.ZoneInfo("America/New_York"))
        got = [(l["date"], l["payment_ref"], l["ref"], str(l["amount"]), l["partner_id"]) for l in lines]
        self.assertEqual(got, [
            ("2026-03-02", "S00001 - Jane Doe", "stripe:txn_c1", "500", 42),
            ("2026-03-02", "Stripe fee (S00001)", "stripe:txn_c1:fee", "-14.8", None),
            ("2026-03-03", "Subscription - John Roe", "stripe:txn_p1", "100", None),
            ("2026-03-03", "Stripe fee (Subscription)", "stripe:txn_p1:fee", "-3.2", None),
            ("2026-03-03", "Refund S00001 - Jane Doe", "stripe:txn_r1", "-100", 42),
            ("2026-03-03", "Billing - Usage Fee", "stripe:txn_f1", "-0.7", None),
            ("2026-03-05", "Stripe payout po_1", "stripe:po_1", "-81.3", None),  # dated when it left the balance
        ])
        self.assertEqual(sum(l["amount"] for l in lines), Decimal("400.00"))
        self.assertEqual(cur, "usd")


class TestSync(Base):
    def test_sync_statement_and_idempotency(self):
        f = self.use(Fakes(account(), [40000]))
        self.assertEqual(s.run(), 7)
        self.assertEqual(len(f.statements), 1)
        self.assertEqual(f.statements[0]["balance_end"], 400.0)
        self.assertTrue(all(l["statement_id"] == 1 for l in f.lines))
        self.assertEqual(f.lines[0]["unique_import_id"], "stripe:txn_c1")
        self.assertEqual(f.lines[0]["partner_id"], 42)
        self.assertEqual(s.run(), 0)  # nothing new, no new statement
        self.assertEqual(len(f.statements), 1)

    def test_recognises_manually_imported_lines(self):
        f = self.use(Fakes(account(), [40000]))
        # Lines imported by hand from --csv carry the key only in `ref`.
        lines, _ = s.fetch_lines(s.Stripe("k"), datetime(2026, 3, 1).date(), None, s.ZoneInfo("America/New_York"))
        for l in lines:
            f.lines.append({"id": len(f.lines) + 1, "journal_id": 13, "date": l["date"], "ref": l["ref"],
                            "amount": float(l["amount"]), "statement_id": 1})
        self.assertEqual(s.run(), 0)

    def test_balance_moving_mid_run_defers_statement(self):
        f = self.use(Fakes(account(), [40000, 41000, 40000]))
        self.assertEqual(s.run(), 7)
        self.assertEqual(f.statements, [])
        self.assertEqual(s.run(), 0)  # next run groups the loose lines
        self.assertEqual(len(f.statements), 1)
        self.assertEqual(f.statements[0]["balance_end"], 400.0)

    def test_mismatch_alerts(self):
        self.use(Fakes(account(), [99999]))
        sent = []
        smtp = mock.MagicMock()
        smtp.return_value.__enter__.return_value.send_message.side_effect = sent.append
        with mock.patch.object(s.smtplib, "SMTP", smtp), mock.patch.object(sys, "argv", ["x"]):
            self.assertEqual(s.main(), 1)
        self.assertEqual(sent[0]["To"], "alerts@example.com")
        self.assertIn("does not match Stripe", sent[0]["Subject"])

    def test_dry_run_writes_nothing(self):
        f = self.use(Fakes(account(), [40000]))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(s.run(dry_run=True), 7)
        self.assertEqual((f.lines, f.statements), ([], []))


class TestCsvModes(Base):
    def test_api_csv_export(self):
        self.use(Fakes(account(), [40000]))
        out = os.path.join(self.tmp.name, "out.csv")
        with redirect_stdout(io.StringIO()) as buf:
            s.export_csv(out, datetime(2026, 3, 1).date(), None)
        rows = s.read_csv(out)
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["partner_id/.id"], "42")
        self.assertIn("implied balance at start of 2026-03-01: 0.00", buf.getvalue())

    def test_reports_mode_with_summary(self):
        d = self.tmp.name
        act = os.path.join(d, "activity.csv")
        pay = os.path.join(d, "payouts.csv")
        summ = os.path.join(d, "summary.csv")
        with open(act, "w") as f:
            f.write("balance_transaction_id,created,currency,gross,fee,net,reporting_category,source_id,"
                    "description,customer_name,customer_description\n"
                    "txn_c1,2026-03-02 10:00:00,usd,500.00,14.80,485.20,charge,ch_1,S00001,Jane Doe,"
                    "Odoo Partner: Jane Doe (id: 42)\n"
                    "txn_f1,2026-03-03 18:00:00,usd,-0.70,0.00,-0.70,fee,,Billing - Usage Fee,,\n")
        with open(pay, "w") as f:
            f.write("balance_transaction_id,effective_at,currency,gross,fee,net,reporting_category,"
                    "description,payout_id\n"
                    "txn_po,2026-03-05 19:00:00,usd,-84.50,0.00,-84.50,payout,STRIPE PAYOUT,po_1\n")
        with open(summ, "w") as f:
            f.write('"category","net_amount"\n"starting_balance","0.00"\n"ending_balance","400.00"\n')
        out = os.path.join(d, "out.csv")
        argv = ["x", "--csv", out, "--reports", pay, act, "--summary", summ]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(s.main(), 0)
        self.assertIn("MATCHES", buf.getvalue())
        refs = [r["ref"] for r in s.read_csv(out)]
        self.assertEqual(refs, ["stripe:txn_c1", "stripe:txn_c1:fee", "stripe:txn_f1", "stripe:po_1"])

        with open(summ, "w") as f:
            f.write('"category","net_amount"\n"starting_balance","0.00"\n"ending_balance","999.00"\n')
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(s.main(), 2)
        self.assertIn("DOES NOT MATCH", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
