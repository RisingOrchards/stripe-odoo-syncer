#!/usr/bin/env python3
"""Sync Stripe balance activity into the Odoo 19 Stripe bank journal. Built to run from cron.

Source of truth is Stripe's balance transactions: every movement of the Stripe balance
(charges, fees incl. Billing fees, refunds, disputes, payouts, adjustments) in one stream,
so the Odoo journal balance tracks the real Stripe balance. Each balance transaction
becomes one statement line for the gross amount plus one line for the Stripe fee (if any).
Each run groups its new lines into an Odoo bank statement whose closing balance is Stripe's
actual balance; if Odoo's running total doesn't match it, the run fails and alerts.

Every line carries a stable key "stripe:<txn id>[:fee]" (payouts: "stripe:<po id>") in
both `ref` and `unique_import_id`. Lines whose key is already in the journal are skipped,
so runs are idempotent, and lines imported by hand from --csv output are recognised too.
Each run re-reads from a few days before the newest synced line, so a missed run catches up.

Stdlib only (Python 3.9+). Configuration comes from environment variables, see .env.example.
On any failure an email goes to ALERT_EMAIL and the exit code is 1.

  stripe_odoo_sync.py                      # sync into Odoo (cron mode)
  stripe_odoo_sync.py --dry-run            # show what would be created, write nothing
  stripe_odoo_sync.py --since 2026-10-01   # re-scan from a date (still deduplicated)
  stripe_odoo_sync.py --csv out.csv --since 2026-06-19 [--until 2026-10-16]
                                           # Stripe only: write an Odoo import CSV, no Odoo needed
"""
import argparse
import csv
import fcntl
import json
import logging
import os
import re
import smtplib
import socket
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from zoneinfo import ZoneInfo

log = logging.getLogger("stripe_odoo_sync")

PARTNER_RE = re.compile(r"Odoo Partner:\s*(.*?)\s*\(id:\s*(\d+)\)")
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
                "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
KEY_PREFIX = "stripe:"
HTTP_TIMEOUT = 60
# Odoo import column headers (technical names so the import wizard maps them automatically).
CSV_FIELDS = ["date", "payment_ref", "ref", "partner_id/.id", "partner_name", "amount", "transaction_type"]


class SyncError(Exception):
    pass


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise SyncError(f"missing required environment variable {name}")
    return val


def http_json(url, *, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:2000]
        raise SyncError(f"HTTP {e.code} from {url.split('?')[0]}: {body}") from None


# --------------------------------------------------------------------------- Stripe

class Stripe:
    BASE = "https://api.stripe.com/v1"

    def __init__(self, api_key):
        self.headers = {"Authorization": f"Bearer {api_key}", "Stripe-Version": "2024-06-20"}
        self._customers, self._charges = {}, {}

    def get(self, path, params=None):
        qs = urllib.parse.urlencode(params or [], doseq=True)
        return http_json(f"{self.BASE}{path}?{qs}", headers=self.headers)

    def balance_transactions(self, since_ts, until_ts=None):
        params = {"limit": 100, "created[gte]": since_ts, "expand[]": "data.source"}
        if until_ts:
            params["created[lt]"] = until_ts
        while True:
            page = self.get("/balance_transactions", params)
            yield from page["data"]
            if not page.get("has_more"):
                return
            params["starting_after"] = page["data"][-1]["id"]

    def balance(self, currency):
        """Current total (available + pending) balance in `currency`, in major units."""
        bal = self.get("/balance")
        return sum(to_amount(b["amount"], currency)
                   for b in bal.get("available", []) + bal.get("pending", [])
                   if b["currency"].lower() == currency)

    def customer(self, cus_id):
        if cus_id not in self._customers:
            try:
                self._customers[cus_id] = self.get(f"/customers/{cus_id}")
            except SyncError as e:
                log.warning("could not load customer %s: %s", cus_id, e)
                self._customers[cus_id] = {}
        return self._customers[cus_id]

    def charge(self, charge):
        """Accept a charge id or object, return the charge object."""
        if isinstance(charge, dict):
            return charge
        if charge not in self._charges:
            self._charges[charge] = self.get(f"/charges/{charge}")
        return self._charges[charge]


# --------------------------------------------------------------------------- Odoo

class Odoo:
    """Odoo external API. ODOO_API=json2 (Odoo 19 default) or jsonrpc (legacy, needs ODOO_USER)."""

    def __init__(self, url, db, api_key, mode="json2", user=None):
        self.url, self.db, self.key, self.mode = url.rstrip("/"), db, api_key, mode
        if mode == "jsonrpc":
            if not user:
                raise SyncError("ODOO_USER is required when ODOO_API=jsonrpc")
            self.uid = self._rpc("common", "authenticate", [db, user, api_key, {}])
            if not self.uid:
                raise SyncError("Odoo authentication failed (check ODOO_USER / ODOO_API_KEY / ODOO_DB)")
        elif mode != "json2":
            raise SyncError(f"unknown ODOO_API {mode!r}")

    def _rpc(self, service, method, args):
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1,
                   "params": {"service": service, "method": method, "args": args}}
        res = http_json(f"{self.url}/jsonrpc", data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"})
        if res.get("error"):
            err = res["error"]
            raise SyncError(f"Odoo error: {err.get('data', {}).get('message') or err.get('message')}")
        return res["result"]

    def call(self, model, method, ids=None, **kwargs):
        if self.mode == "jsonrpc":
            return self._rpc("object", "execute_kw",
                             [self.db, self.uid, self.key, model, method, [ids] if ids else [], kwargs])
        body = dict(kwargs)
        if ids:
            body["ids"] = ids
        headers = {"Content-Type": "application/json", "Authorization": f"bearer {self.key}"}
        if self.db:
            headers["X-Odoo-Database"] = self.db
        return http_json(f"{self.url}/json/2/{model}/{method}", data=json.dumps(body).encode(), headers=headers)


# --------------------------------------------------------------------------- conversion

def to_amount(minor, currency):
    return Decimal(minor) if currency.lower() in ZERO_DECIMAL else Decimal(minor) / 100


def partner_from_charge(stripe, charge):
    """Return (odoo_partner_id or None, display name) for a charge."""
    name = (charge.get("billing_details") or {}).get("name") or ""
    cus = charge.get("customer")
    if cus:
        cus_obj = cus if isinstance(cus, dict) else stripe.customer(cus)
        m = PARTNER_RE.search(cus_obj.get("description") or "")
        if m:
            return int(m.group(2)), m.group(1)
        name = name or cus_obj.get("name") or cus_obj.get("email") or ""
    return None, name


def bt_to_lines(bt, stripe, tz):
    """Convert one Stripe balance transaction into statement line dicts (amounts as Decimal)."""
    cur = bt["currency"]
    # Payouts leave the balance when they arrive (available_on), as in Stripe's balance report.
    when = bt["available_on"] if bt["type"] == "payout" and bt.get("available_on") else bt["created"]
    day = datetime.fromtimestamp(when, timezone.utc).astimezone(tz).date().isoformat()
    src = bt.get("source")
    src_obj = src if isinstance(src, dict) else {"id": src}
    src_id = src_obj.get("id") or bt["id"]
    obj_type = src_obj.get("object")
    key = KEY_PREFIX + (src_id if bt["type"] == "payout" else bt["id"])

    partner_id, name, order_ref = None, "", ""
    charge = None
    if obj_type == "charge":
        charge = src_obj
    elif obj_type in ("refund", "dispute") and src_obj.get("charge"):
        charge = stripe.charge(src_obj["charge"])
    if charge:
        partner_id, name = partner_from_charge(stripe, charge)
        order_ref = charge.get("description") or ""

    base = " - ".join(p for p in (order_ref, name) if p)
    if bt["type"] in ("charge", "payment"):
        label = base or bt.get("description") or src_id
    elif bt["type"] in ("refund", "payment_refund"):
        label = f"Refund {base}".strip()
    elif obj_type == "dispute" or (bt["type"].startswith("adjustment") and charge):
        label = f"Dispute {base}".strip()
    elif bt["type"] == "payout":
        label = f"Stripe payout {src_id}"
    else:
        label = bt.get("description") or bt["type"].replace("_", " ").capitalize()

    lines = [{
        "date": day,
        "payment_ref": label,
        "ref": key,
        "amount": to_amount(bt["amount"], cur),
        "partner_id": partner_id,
        "partner_name": name,
        "transaction_type": bt["type"],
    }]
    if bt.get("fee"):
        lines.append({
            "date": day,
            "payment_ref": f"Stripe fee ({order_ref or src_id})",
            "ref": key + ":fee",
            "amount": -to_amount(bt["fee"], cur),
            "partner_id": None,
            "partner_name": "",
            "transaction_type": "stripe_fee",
        })
    return lines


def fetch_lines(stripe, start, end, tz, currency=None):
    """All statement lines for Stripe activity on [start, end) dates in tz. Returns (lines, currency)."""
    since_ts = int(datetime.combine(start, datetime.min.time(), tz).timestamp())
    until_ts = int(datetime.combine(end, datetime.min.time(), tz).timestamp()) if end else None
    lines = []
    # Stripe lists newest first; build lines oldest first so the statement reads chronologically.
    for bt in sorted(stripe.balance_transactions(since_ts, until_ts), key=lambda b: (b["created"], b["id"])):
        cur = bt["currency"].lower()
        currency = currency or cur
        if cur != currency:
            raise SyncError(f"balance transaction {bt['id']} is in {cur.upper()}, expected {currency.upper()}")
        lines.extend(bt_to_lines(bt, stripe, tz))
    return lines, currency


# --------------------------------------------------------------------------- Stripe report files

def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def report_date(row, local_col, utc_col, tz):
    if row.get(local_col):
        return row[local_col][:10]  # already in the report's timezone
    if row.get(utc_col):
        dt = datetime.fromisoformat(row[utc_col].strip().replace("Z", "")).replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date().isoformat()
    raise SyncError(f"row {row.get('balance_transaction_id')} has neither {local_col} nor {utc_col}")


def report_lines(paths, tz):
    """Statement lines from Stripe dashboard itemized reports: 'Balance change from activity' and
    'Payouts' (Reports > Balance > Download > Itemized). Produces the same keys as the API path."""
    lines, has_payouts, warned = [], False, False
    for path in paths:
        rows = read_csv(path)
        if not rows:
            continue
        cols = set(rows[0])
        is_payouts = "payout_id" in cols
        has_payouts |= is_payouts
        missing = {"balance_transaction_id", "gross", "fee", "reporting_category", "currency"} - cols
        if missing:
            raise SyncError(f"{path}: not an itemized Stripe balance report, missing columns {sorted(missing)}; "
                            f"columns found: {sorted(cols)}")
        if not is_payouts and "customer_description" not in cols and not warned:
            log.warning("%s has no customer_description column: Odoo partners can't be linked. "
                        "Re-download with 'All columns' to fix.", path)
            warned = True
        for row in rows:
            cat = row["reporting_category"]
            gross, fee = Decimal(row["gross"].replace(",", "")), Decimal(row["fee"].replace(",", "") or 0)
            if is_payouts:
                day = report_date(row, "effective_at", "effective_at_utc", tz)
                src_id = row["payout_id"]
            else:
                day = report_date(row, "created", "created_utc", tz)
                src_id = row.get("source_id") or row["balance_transaction_id"]
            key = KEY_PREFIX + (src_id if cat == "payout" else row["balance_transaction_id"])

            m = PARTNER_RE.search(row.get("customer_description") or "")
            partner_id, name = (int(m.group(2)), m.group(1)) if m else (None, row.get("customer_name") or "")
            desc = (row.get("description") or "").strip()
            base = " - ".join(p for p in (desc, name) if p)
            if cat == "payout":
                label = f"Stripe payout {src_id}"
            elif cat == "refund":
                label = base if desc.lower().startswith("refund") else f"Refund {base}".strip()
            elif cat == "dispute":
                label = f"Dispute {base}".strip()
            else:
                label = base or cat.replace("_", " ").capitalize()
            ttype = {"fee": "stripe_fee"}.get(cat, cat)

            lines.append({"date": day, "payment_ref": label, "ref": key, "amount": gross,
                          "partner_id": partner_id, "partner_name": name, "transaction_type": ttype})
            if fee:
                lines.append({"date": day, "payment_ref": f"Stripe fee ({desc or src_id})", "ref": key + ":fee",
                              "amount": -fee, "partner_id": None, "partner_name": "",
                              "transaction_type": "stripe_fee"})
    if not has_payouts:
        log.warning("no itemized payouts report given: payouts will be missing from the output")
    lines.sort(key=lambda l: l["date"])  # stable: keeps each fee right after its transaction
    return lines


def summary_net(path):
    """(starting, ending) balance from a Stripe 'Balance summary' report."""
    vals = {r.get("category"): Decimal(r["net_amount"]) for r in read_csv(path) if r.get("net_amount")}
    if "ending_balance" in vals and "starting_balance" in vals:
        return vals["starting_balance"], vals["ending_balance"]
    raise SyncError(f"{path}: not a Stripe balance summary report (no starting/ending_balance rows)")


# --------------------------------------------------------------------------- CSV export

def write_import_csv(path, lines, source):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for l in lines:
            w.writerow([l["date"], l["payment_ref"], l["ref"], l["partner_id"] or "", l["partner_name"],
                        f"{l['amount']:.2f}", l["transaction_type"]])

    total = sum((l["amount"] for l in lines), Decimal(0))
    by_type = {}
    for l in lines:
        by_type[l["transaction_type"]] = by_type.get(l["transaction_type"], Decimal(0)) + l["amount"]
    print(f"Wrote {len(lines)} lines to {path} ({source})")
    for t, amt in sorted(by_type.items()):
        print(f"  {t:<20} {amt:>12.2f}")
    print(f"  {'NET':<20} {total:>12.2f}")
    return total


def export_csv(path, start, end):
    tz = ZoneInfo(env("SYNC_TZ", "UTC"))
    stripe = Stripe(env("STRIPE_API_KEY", required=True))
    lines, currency = fetch_lines(stripe, start, end, tz)
    total = write_import_csv(path, lines, f"Stripe API, {start} .. {end or 'now'}, dates in {tz.key}")
    if currency and not end:
        now = stripe.balance(currency)
        print(f"Stripe balance now: {now:.2f} {currency.upper()} -> implied balance at start of {start}: "
              f"{now - total:.2f} (should equal the Odoo Stripe journal balance before importing)")


# --------------------------------------------------------------------------- Odoo sync

def resolve_journal(odoo, spec):
    domain = [("id", "=", int(spec))] if spec.isdigit() else \
        ["&", ("type", "=", "bank"), "|", ("name", "=ilike", spec), ("code", "=ilike", spec)]
    journals = odoo.call("account.journal", "search_read", domain=domain,
                         fields=["id", "name", "currency_id", "company_id"])
    if len(journals) != 1:
        raise SyncError(f"expected exactly one Odoo bank journal matching {spec!r}, found {len(journals)}")
    j = journals[0]
    if j["currency_id"]:
        currency = j["currency_id"][1]
    else:
        company = odoo.call("res.company", "read", ids=[j["company_id"][0]], fields=["currency_id"])
        currency = company[0]["currency_id"][1]
    return j["id"], j["name"], currency.lower()


def key_domain(journal_id, op, value):
    return ["&", ("journal_id", "=", journal_id), "|", ("unique_import_id", op, value), ("ref", op, value)]


def sync_start(odoo, journal_id, configured_start, overlap_days, today):
    last = odoo.call("account.bank.statement.line", "search_read",
                     domain=key_domain(journal_id, "=like", KEY_PREFIX + "%"),
                     fields=["date"], order="date desc", limit=1)
    start = configured_start
    if last:
        # A payout is dated on arrival, possibly days after it was created; never let such a
        # future-dated line push the window past transactions that are not booked yet.
        newest = min(date.fromisoformat(last[0]["date"]), today)
        start = max(start, newest - timedelta(days=overlap_days))
    return start


def run(dry_run=False, since=None):
    tz = ZoneInfo(env("SYNC_TZ", "UTC"))
    stripe = Stripe(env("STRIPE_API_KEY", required=True))
    odoo = Odoo(env("ODOO_URL", required=True), env("ODOO_DB"), env("ODOO_API_KEY", required=True),
                env("ODOO_API", "json2"), env("ODOO_USER"))
    journal_id, journal_name, journal_cur = resolve_journal(odoo, env("ODOO_JOURNAL", "Stripe"))

    if since:
        start = date.fromisoformat(since)
    else:
        configured = date.fromisoformat(env("SYNC_START", required=True))
        start = sync_start(odoo, journal_id, configured, int(env("SYNC_OVERLAP_DAYS", "3")), datetime.now(tz).date())
    log.info("journal %r (id %s, %s); fetching Stripe activity since %s", journal_name, journal_id,
             journal_cur.upper(), start)

    # Read the balance before the transactions: if it is unchanged after the lines are written,
    # nothing happened in Stripe mid-run and it is a valid closing balance for the statement.
    stripe_balance = stripe.balance(journal_cur)
    lines, _ = fetch_lines(stripe, start, None, tz, currency=journal_cur)

    keys = [l["ref"] for l in lines]
    existing = set()
    if keys:
        for r in odoo.call("account.bank.statement.line", "search_read",
                           domain=key_domain(journal_id, "in", keys), fields=["unique_import_id", "ref"]):
            existing.update(k for k in (r["unique_import_id"], r["ref"]) if k)
    new = [l for l in lines if l["ref"] not in existing]

    # Only link partners that actually exist in this Odoo database.
    pids = sorted({l["partner_id"] for l in new if l["partner_id"]})
    known = set(odoo.call("res.partner", "search", domain=[("id", "in", pids)],
                          context={"active_test": False})) if pids else set()
    vals_list = []
    for l in new:
        pid = l["partner_id"]
        if pid and pid not in known:
            log.warning("Odoo partner id %s from Stripe not found; leaving partner blank", pid)
        vals_list.append({
            "journal_id": journal_id,
            "date": l["date"],
            "payment_ref": l["payment_ref"],
            "ref": l["ref"],
            "unique_import_id": l["ref"],
            "amount": float(l["amount"]),
            "partner_id": pid if pid in known else False,
            "partner_name": l["partner_name"] or False,
            "transaction_type": l["transaction_type"],
        })

    log.info("%d lines in window, %d already in Odoo, %d to create", len(lines), len(lines) - len(new), len(new))
    if dry_run:
        for v in vals_list:
            print(f"{v['date']}  {v['amount']:>12.2f}  {v['payment_ref']}  [{v['ref']}]")
        print(f"Stripe balance now: {stripe_balance:.2f} {journal_cur.upper()}")
        return len(new)
    if vals_list:
        ids = odoo.call("account.bank.statement.line", "create", vals_list=vals_list)
        log.info("created %d statement lines (ids %s..%s)", len(ids), ids[0], ids[-1])
    make_statement(odoo, stripe, journal_id, journal_cur, stripe_balance, tz)
    return len(new)


def make_statement(odoo, stripe, journal_id, currency, stripe_balance, tz):
    """Group all synced lines not yet in a statement into one statement whose closing balance is
    Stripe's real balance. Odoo computes the opening balance and the running total itself; if that
    total differs from Stripe's balance, the run fails (and alerts) so the gap gets investigated."""
    loose = odoo.call("account.bank.statement.line", "search_read",
                      domain=key_domain(journal_id, "=like", KEY_PREFIX + "%") + [("statement_id", "=", False)],
                      fields=["id"], order="date, id")
    if not loose:
        return
    if stripe.balance(currency) != stripe_balance:
        log.warning("Stripe balance changed during this run; %d lines will be put in a statement next run",
                    len(loose))
        return
    name = "Stripe " + datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    st_id = odoo.call("account.bank.statement", "create", vals_list=[{
        "name": name,
        "journal_id": journal_id,
        "balance_end_real": float(stripe_balance),
        "line_ids": [[6, 0, [l["id"] for l in loose]]],
    }])[0]
    st = odoo.call("account.bank.statement", "read", ids=[st_id],
                   fields=["balance_start", "balance_end", "balance_end_real"])[0]
    log.info("statement %r (id %s): %d lines, opening %.2f, closing %.2f, Stripe balance %.2f",
             name, st_id, len(loose), st["balance_start"], st["balance_end"], st["balance_end_real"])
    gap = round(st["balance_end"] - st["balance_end_real"], 2)
    if gap:
        raise SyncError(f"Odoo Stripe journal does not match Stripe: statement {name!r} (id {st_id}) computes a "
                        f"closing balance of {st['balance_end']:.2f}, Stripe's actual balance is "
                        f"{st['balance_end_real']:.2f} (difference {gap:.2f}). The lines and statement were "
                        f"created; Odoo shows the statement as invalid until the difference is resolved.")


# --------------------------------------------------------------------------- main

def send_alert(subject, body):
    to, host = env("ALERT_EMAIL"), env("SMTP_HOST")
    if not (to and host):
        log.error("ALERT_EMAIL and SMTP_HOST must be set to email failure alerts; no alert sent")
        return
    port = int(env("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, env("SMTP_FROM") or env("SMTP_USER") or to, to
    msg.set_content(body)
    smtp_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with smtp_cls(host, port, timeout=30) as s:
        if port != 465:
            s.starttls()
        if env("SMTP_USER"):
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD", ""))
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print lines that would be created; write nothing")
    ap.add_argument("--since", help="YYYY-MM-DD: scan from this date instead of the automatic start")
    ap.add_argument("--csv", metavar="FILE", help="write an Odoo import CSV instead of syncing (no Odoo needed)")
    ap.add_argument("--until", help="YYYY-MM-DD (exclusive) end date, --csv mode only")
    ap.add_argument("--reports", nargs="+", metavar="REPORT",
                    help="with --csv: build from Stripe dashboard itemized reports (activity + payouts) "
                         "instead of the API")
    ap.add_argument("--summary", help="with --reports: Stripe balance summary CSV to check the total against")
    args = ap.parse_args()
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

    if args.reports:  # offline: dashboard report files -> import CSV, no credentials needed
        if not args.csv:
            ap.error("--reports needs --csv OUTPUT")
        try:
            lines = report_lines(args.reports, ZoneInfo(env("SYNC_TZ", "UTC")))
            total = write_import_csv(args.csv, lines, "Stripe report files")
            if args.summary:
                start_bal, end_bal = summary_net(args.summary)
                ok = end_bal - start_bal == total
                print(f"Balance summary: {start_bal:.2f} -> {end_bal:.2f} (change {end_bal - start_bal:.2f}); "
                      f"import NET {total:.2f}: {'MATCHES' if ok else 'DOES NOT MATCH'}")
                if not ok:
                    return 2
        except SyncError as e:
            sys.exit(f"ERROR: {e}")
        return 0

    if args.csv:  # interactive export: errors go to the terminal, no lock, no email
        since = args.since or env("SYNC_START")
        if not since:
            ap.error("--csv needs --since (or SYNC_START)")
        export_csv(args.csv, date.fromisoformat(since), date.fromisoformat(args.until) if args.until else None)
        return 0

    with open(env("SYNC_LOCK_FILE", "/tmp/stripe_odoo_sync.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another sync is still running; exiting")
            return 0

        try:
            run(dry_run=args.dry_run, since=args.since)
            return 0
        except Exception as e:
            log.exception("sync failed")
            try:
                send_alert(f"[stripe-odoo-sync] FAILED on {socket.gethostname()}: {str(e)[:120]}",
                           f"The Stripe -> Odoo sync failed at {datetime.now().isoformat(timespec='seconds')}.\n\n"
                           f"{traceback.format_exc()}")
            except Exception:
                log.exception("could not send failure email")
            return 1


if __name__ == "__main__":
    sys.exit(main())
