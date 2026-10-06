# Pocketful — stage 1

This is a standard-library Python JSON-over-HTTP service. It makes no outbound
network calls and stores all state in the local file given by `--data`.

Build (syntax-check):

```sh
python3 -m py_compile app.py
```

Run on port 8080 with durable storage in the current directory:

```sh
python3 app.py --data ./pocketful.json
```

Run the builder tests:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```

The independently runnable, network-isolated adversarial suites are:

```sh
./verify/run-isolated.sh
./verify/run-money-isolated.sh
./verify/run-splits-isolated.sh
```

The Dockerfile has no package-install step and copies only `app.py`; build it
from this directory and mount `/data` for durable service state. The runtime
uses only Python's standard library and makes no outbound network calls.

Implemented endpoints are `GET /health`, `POST /users`, `GET /users/{id}`,
`POST /users/{id}/deposits`, `POST /payments`, `POST /splits`, and
`GET /users/{id}/feed`.
`POST /users` requires exactly `{"name":"..."}` with an `application/json`
content type. A successful user starts with `balance_cents: 0`. Writes are held
under a process-local lock and committed by fsynced temporary-file replacement,
so a successful response has a complete durable record; on a commit error, the
in-memory insertion is rolled back.

Deposits require exactly `{"amount_cents": N}` and payments require exactly
`{"from_user":"...","to_user":"...","amount_cents":N,"note":"..."}`.
Amounts are JSON integers (not floats or booleans) and must be at least one
cent. Both mutation endpoints require one nonempty `Idempotency-Key` header.
The key is stored in the same durable commit as the balance update and response:
the same canonical method/path/body returns the original status and JSON
response, while a reused key for any other request returns `409`. Different-key
requests serialize at the transaction boundary, so competing payments cannot
overdraw a wallet and transfers conserve total balances.

An insufficient payment returns `409` and moves no cents. Existing API behavior
and all idempotency records survive service restart.

`POST /splits` requires exactly:

```json
{"payer":"user-id","participants":["user-id","other-id"],"total_cents":101,"note":"dinner"}
```

It also requires `Idempotency-Key`. `participants` is ordered, nonempty, and
unique, and must contain the payer exactly once. The service divides
`total_cents` across all participants using whole cents, allocating any
remainder from the beginning of that ordered array. Non-payers transfer their
shares to the payer; the payer's own share transfers no cents. All participant
existence and available balances are checked before one durable transaction is
committed. A rejected split leaves balances, events, and key records untouched.

Successful payments and splits create durable UTC-timestamped feed events.
`GET /users/{id}/feed` returns `{"events":[...]}` newest first. Each event has
`type`, `who`, `amount_cents`, `note`, `timestamp`, and `direction`; a payer's
split summary additionally lists its non-payer counterparties and own share.
