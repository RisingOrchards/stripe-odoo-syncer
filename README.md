# Stripe Odoo Syncer

**Don't pay that syncer fee for Stripe into Odoo... just use this!**

A single Python file that books your Stripe activity into an Odoo bank journal, ready to reconcile:
every charge, fee, refund, dispute and payout. Run it from cron. It needs no dependencies, has no
database of its own, and never asks you to pay per transaction.

```
2026-03-02    500.00  S00001 - Jane Doe            [stripe:txn_…]      partner linked
2026-03-02    -14.80  Stripe fee (S00001)          [stripe:txn_…:fee]
2026-03-03   -100.00  Refund S00001 - Jane Doe     [stripe:txn_…]
2026-03-05    -81.30  Stripe payout po_…           [stripe:po_…]
statement 'Stripe 2026-03-06 06:15': opening 0.00, closing 303.90, Stripe balance 303.90 ✔
```

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration reference](#configuration-reference)
- [Command reference](#command-reference)
- [Bringing in history (first-time cutover)](#bringing-in-history-first-time-cutover)
- [Reconciling in Odoo](#reconciling-in-odoo)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Limitations](#limitations)
- [Development](#development)
- [License](#license)

---

## How it works

In Odoo, Stripe is treated as a **bank account**: a bank journal whose balance should always equal
your Stripe balance. The syncer keeps it that way.

- **Source of truth: Stripe balance transactions.** Every change to your Stripe balance (a charge,
  a fee, a Stripe Billing fee, a refund, a dispute, an adjustment, a payout to your bank, a
  withdrawal to cover a negative balance) is a balance transaction. The syncer books each one
  exactly as Stripe records it: its own amount, its own date, nothing combined or calculated.
  It skips nothing and invents nothing.
- **Gross and fee are separate lines.** A $500 charge with a $14.80 fee becomes `+500.00` and
  `-14.80`. The gross line matches the customer payment in Odoo, and the fee line goes to your bank
  fees account.
- **Partners are linked automatically.** When Odoo's own Stripe payment provider takes a payment,
  it stores `Odoo Partner: <name> (id: <n>)` on the Stripe customer. The syncer reads that and sets
  the Odoo partner on the line. For other payments it fills in the customer's name instead.
- **Every line has a stable ID.** `stripe:<balance transaction id>`, plus `:fee` for fee lines, or
  `stripe:<payout id>` for payouts, stored in the line's *Reference* and *Import ID*. Before
  creating anything the syncer checks which IDs are already in the journal, so running it twice
  never books anything twice. That includes lines you imported by hand from its CSV export.
- **It catches up on its own.** Each run starts a few days before the newest line it has booked.
  If the server was off for a week, the next run fills the gap.
- **Each run ends with a balance check.** New lines are grouped into an Odoo bank statement whose
  closing balance is set to **Stripe's actual balance**, as Stripe reports it. Odoo then computes
  the opening balance and running total itself. If Odoo's total does not land on Stripe's balance,
  the run fails, emails you, and Odoo marks the statement as invalid. You find out the day a gap
  appears, not at year end.
- **Failures send an email.** Any error (bad key, Odoo down, balance mismatch) emails the full
  error and exits with status 1. A successful run only writes to its log.

| Stripe balance transaction            | Odoo statement line(s)                                   |
|---------------------------------------|----------------------------------------------------------|
| `charge` / `payment`                  | `+gross` (partner linked) and `-fee`                     |
| `refund` / `payment_refund`           | `-amount` (partner of the original charge)               |
| dispute / dispute adjustment          | `-amount` (+ `-fee` for the dispute fee, if any)         |
| `payout`                              | `-amount` to your bank (or `+` for a negative-balance withdrawal) |
| `stripe_fee` (Billing, Terminal, …)   | `-amount`, labelled with Stripe's description            |
| anything else (contributions, adjustments) | the amount, labelled with Stripe's description      |

Payouts are dated when they leave the Stripe balance, which matches Stripe's balance report. Your
bank usually shows them a day or two later, and Odoo has no problem reconciling across that gap.

## Requirements

- **Python 3.9+** on Linux or macOS. It uses only the standard library, so there is nothing to
  `pip install`.
- **Odoo 19** (Online, Odoo.sh or self-hosted) with Accounting. It uses Odoo 19's JSON-2 API by
  default. A legacy JSON-RPC mode is included for other setups, but only Odoo 19 has been tested.
- A **Stripe** account in a single currency. See [Limitations](#limitations).
- An SMTP account that can send the failure emails.

## Setup

### 1. Create the Stripe journal in Odoo

If you don't have one yet: **Accounting → Configuration → Journals → New**. Set **Type: Bank** and
**Name: Stripe**, and set the currency to your Stripe balance currency. This journal is your Stripe
balance.

If you use Odoo's Stripe *payment provider*, point its payment journal at this same journal, or
at least make sure the customer payments it creates can be matched against lines in this journal.

### 2. Create an Odoo API key

Log in as the user the syncer should act as (it needs Accounting rights, e.g. *Accountant*), then
go to **avatar → My Preferences → Account Security → New API Key**. Copy the key; Odoo shows it only
once.

### 3. Create a read-only Stripe key

In Stripe: **Developers → API keys → Create restricted key**. Give it **Read** access to:

> Balance · Balance transaction sources · Charges · Customers · Disputes · Payouts · Refunds

Leave everything else at *None*. The syncer never writes to Stripe.

### 4. Install and configure

```bash
sudo mkdir -p /opt/stripe-odoo-syncer && cd /opt/stripe-odoo-syncer
# copy stripe_odoo_sync.py and .env.example here (or git clone the repo)
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Every setting is documented in [`.env.example`](.env.example) and in the
[configuration reference](#configuration-reference) below.

### 5. Choose your start date

`SYNC_START` is the first day the syncer is allowed to book. Set it to **the day after your Odoo
Stripe journal is complete up to**:

- **New journal, want everything:** set it to the day before your first Stripe transaction.
- **Journal already up to date by hand until some date:** use the day after that date.

Not sure? Read [Bringing in history](#bringing-in-history-first-time-cutover) first.

### 6. Test it

```bash
set -a; . ./.env; set +a              # load the configuration into this shell

python3 stripe_odoo_sync.py --dry-run # connects to both systems, prints what it would book, writes nothing
python3 stripe_odoo_sync.py           # books it and creates the statement
python3 stripe_odoo_sync.py           # run again: must report "0 to create"
```

Check in Odoo that the statement's closing balance equals your Stripe balance, which the log also
prints.

Then make sure alerts reach you by forcing a failure:

```bash
ODOO_JOURNAL=does-not-exist python3 stripe_odoo_sync.py   # you should receive an email
```

Use a test or staging Odoo database for your first runs if you can.

### 7. Schedule it

Cron does not read `.env` by itself, so the cron line loads it. Run `crontab -e` and add:

```cron
# Stripe Odoo Syncer: daily at 06:15 (server time)
15 6 * * * cd /opt/stripe-odoo-syncer && set -a && . ./.env && set +a && /usr/bin/python3 stripe_odoo_sync.py >> /var/log/stripe-odoo-syncer.log 2>&1
```

**Once a day is recommended.** You get at most one statement per day, like a normal bank
statement, and missed days catch up automatically. Hourly also works: runs with nothing new
create nothing. Two runs can never overlap, because a lock file makes the second one exit.

Make sure the cron user can write the log file. You can also run the syncer by hand at any time.

## Configuration reference

All settings are environment variables. Usually they live in `.env`.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `STRIPE_API_KEY` | yes | | Restricted read-only key (`rk_live_…`). |
| `ODOO_URL` | yes | | Base URL of your Odoo, e.g. `https://your-company.odoo.com`. |
| `ODOO_DB` | usually | | Database name. Required when the server hosts several databases. |
| `ODOO_API_KEY` | yes | | Odoo API key of the syncing user. |
| `ODOO_JOURNAL` | | `Stripe` | The Stripe bank journal: name, short code or numeric id. |
| `ODOO_API` | | `json2` | `json2` (Odoo 19) or `jsonrpc` (legacy; also needs `ODOO_USER`). |
| `ODOO_USER` | jsonrpc only | | Login of the syncing user. |
| `SYNC_START` | yes | | First date that may be booked (`YYYY-MM-DD`). |
| `SYNC_TZ` | | `UTC` | Timezone for line dates. Use your Stripe account's reporting timezone, e.g. `America/New_York`. |
| `SYNC_OVERLAP_DAYS` | | `3` | Days re-checked before the newest booked line. |
| `SYNC_LOCK_FILE` | | `/tmp/stripe_odoo_sync.lock` | Prevents overlapping runs. |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING`, … |
| `ALERT_EMAIL` | yes* | | Where failure emails go. |
| `SMTP_HOST` | yes* | | SMTP server. |
| `SMTP_PORT` | | `587` | `465` = implicit TLS; any other port uses STARTTLS. |
| `SMTP_USER` / `SMTP_PASSWORD` | | | SMTP login, if your server needs one. |
| `SMTP_FROM` | | `SMTP_USER` | Sender address. |

\* Without `ALERT_EMAIL` and `SMTP_HOST` the syncer still runs, but failures are only logged. Don't
run it unattended like that.

## Command reference

```bash
stripe_odoo_sync.py                        # sync into Odoo (what cron runs)
stripe_odoo_sync.py --dry-run              # show what would be booked; change nothing
stripe_odoo_sync.py --since 2026-03-01     # re-scan from a date; already-booked lines are skipped

# Odoo-free CSV export, for review or manual import (needs only STRIPE_API_KEY):
stripe_odoo_sync.py --csv out.csv --since 2026-01-01 [--until 2026-04-01]

# Offline: build the CSV from Stripe dashboard reports instead of the API (no keys at all):
stripe_odoo_sync.py --csv out.csv --reports activity_itemized.csv payouts_itemized.csv \
                    [--summary balance_summary.csv]
```

- `--until` is exclusive: `--until 2026-04-01` stops at the end of March 31.
- Without `--until`, the API export also prints Stripe's current balance and the opening balance
  your journal must have for the file to land on it.
- `--reports` takes Stripe's **itemized** *Balance change from activity* and *Payouts* reports
  (Dashboard → Reports → Balance → Download), in either order. Download the activity report with
  **All columns**, or partners can't be linked. `--summary` takes the *Balance summary* report from
  the same page and checks the total. If it doesn't match, the command prints `DOES NOT MATCH` and
  exits with status 2. It never adjusts anything to make the numbers agree.

Exit codes: `0` success · `1` failure (alert sent) · `2` CSV total doesn't match the summary.

## Bringing in history (first-time cutover)

Choose **one** of these:

**A. Let the syncer book the history (recommended).** Set `SYNC_START` to the first day the journal
is missing, run `--dry-run`, then run it for real. The history goes in with the same IDs and the same
balance check as daily runs.

**B. Review a file first, then import by hand.** Export a CSV for the missing period:

```bash
SYNC_TZ=America/New_York python3 stripe_odoo_sync.py --csv history.csv --since 2026-01-01 --until 2026-04-01
```

Check it, then import it in Odoo (**Accounting → Stripe journal → Import file**). In the
column-mapping screen make sure:

- `ref` → **Reference**. **Required**: this is how the syncer recognises these lines later and
  won't book them again.
- `partner_id/.id` → **Partner / Database ID**
- `transaction_type` can stay unmapped if Odoo doesn't offer it.

Then set `SYNC_START` to the `--until` date.

The **Stripe payments export** (Payments → Export) is not enough on its own: it has no payouts,
no Stripe Billing fees, and no negative-balance withdrawals, so your journal would drift away from
Stripe. Use the API export or the balance reports above.

## Reconciling in Odoo

The syncer books the bank side; reconciliation stays in Odoo, where it belongs. Some reconciliation
models make it mostly automatic:

| Lines | Suggested rule |
|---|---|
| Label starts with `Stripe fee` or `Billing -` | Counterpart: your bank fees / merchant fees expense account |
| Label starts with `Stripe payout` | Counterpart: your liquidity transfer account; the matching bank deposit reconciles against the same account |
| Customer payments (`S00001 - Jane Doe`) | Odoo's built-in invoice/payment matching does this: the partner and order reference are on the line |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `HTTP 401 … api.stripe.com` | Wrong or revoked Stripe key, or it's missing one of the read permissions above. |
| `HTTP 404 … /json/2/…` | Your Odoo has no JSON-2 API (not Odoo 19). Set `ODOO_API=jsonrpc` and `ODOO_USER`. |
| `HTTP 401/403 … /json/2/…` | Wrong Odoo API key, or the wrong database: set `ODOO_DB`. |
| `expected exactly one Odoo bank journal matching 'Stripe', found 0` | Set `ODOO_JOURNAL` to the journal's exact name, code or id. It must be of type *Bank*. |
| `… is in EUR, expected USD` | Stripe balance currency differs from the journal currency. See [Limitations](#limitations). |
| `Odoo Stripe journal does not match Stripe … difference X` | Something in the journal isn't in Stripe, or the other way round. Usually a manual entry, a deleted line, or history booked before `SYNC_START`. Compare the journal with Stripe's balance report for the period. The syncer never "fixes" this for you. |
| `Stripe balance changed during this run` | Not an error: a payment arrived mid-run. The statement is created on the next run. |
| Lines imported by hand get booked again | The CSV's `ref` column wasn't mapped to **Reference** on import. Delete the duplicates and map it next time. |
| No partner on a customer payment | The Stripe customer wasn't created by Odoo, so there is no `Odoo Partner: … (id: n)` in its description. The name is still put in *Partner Name*. |

Run with `LOG_LEVEL=DEBUG` for more detail.

## Security

- Use a **restricted, read-only** Stripe key. The syncer only ever reads from Stripe.
- Give the Odoo user only the accounting rights it needs, and keep its API key private.
- Keep `.env` at mode `600` and out of version control. `.gitignore` already excludes `.env` and
  `*.csv`, because Stripe exports contain customer data.
- The syncer talks only to Stripe, your Odoo and your SMTP server. There is no telemetry and no
  third-party service.

## Limitations

- **One Stripe account, one currency.** Balance transactions in a currency other than the
  journal's stop the run with an error. They are never converted silently.
- **Stripe Connect** platform flows (transfers to connected accounts, application fees) are booked
  as generic lines with Stripe's description, without special handling.
- **Odoo 19** is the tested version.
- Lines are booked as Stripe reports them. The syncer does not post journal entries, reconcile, or
  create payments; that is Odoo's job.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests run against fake Stripe and Odoo APIs with made-up data, and need no credentials or
network. Pull requests are welcome. Please keep it to a single file using only the standard
library, and add a test for any new behaviour.

## License

[MIT](LICENSE). Not affiliated with Stripe or Odoo. This touches your books, so test against a
staging database first and review what it books.
