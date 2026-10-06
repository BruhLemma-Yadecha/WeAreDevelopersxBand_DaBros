@foreman Stage 1: build "pocketful", a wallet and payments service in the style of Venmo. Money must never be created, destroyed or spent twice, under concurrent transfers, retries and rounding. Put everything for this stage in `stage-1/` (relative to your working directory) and the factory log in `FACTORY.md` at your working directory root.

## Service
A JSON-over-HTTP service on port 8080. All money is an integer number of cents in one currency. Anything that is not a non-negative integer where a cent amount is expected is rejected. Use only the language's standard library or dependencies committed into `stage-1/`.

- `GET /health` returns `200 {"status":"ok"}`.
- `POST /users` with `{"name": "..."}` creates a user with a wallet at balance 0. Returns `201` with the user id.
- `GET /users/{id}` returns the user and current balance in cents.
- `POST /users/{id}/deposits` with `{"amount_cents": N}` adds money from outside the system. N must be at least 1.
- `POST /payments` with `{"from_user": id, "to_user": id, "amount_cents": N, "note": "..."}` moves money between two different users. N must be at least 1. A payment that would make the sender's balance negative is rejected and changes nothing.
- `POST /splits` with `{"payer": id, "participants": [id, ...], "total_cents": N, "note": "..."}` shares a bill. Each participant other than the payer owes the payer a share of N, split as evenly as whole cents allow, with leftover cents given to participants in listed order, one cent each, so the shares add up to N exactly. The payer counts as a participant and pays their own share implicitly. Either every share transfers or none do.
- `GET /users/{id}/feed` returns that user's payments and splits, newest first, each with who, how much, the note, and a timestamp.

Every `POST` that changes money (deposits, payments, splits) requires an `Idempotency-Key` header. Repeating the same key with the same body returns the original response and changes nothing. Repeating the same key with a different body returns `409`.

## Invariants
- The sum of all balances always equals the sum of all deposits. Payments and splits never change it.
- No balance is ever negative.
- A write applies at most once per key, including under 100 or more concurrent callers using the same key, and different keys racing for the same wallet.
- A request that fails or is cut off leaves no partial change.
- Data survives a restart of the service.

## Deliverables in `stage-1/`
- Source, tests, and a README with one command to build and one to run.
- A container image build file so the service builds and serves from a clean container with no outbound network.
- Adversarial verification scripts in `stage-1/verify/`.

Run the whole factory loop on your own: plan, build, independent verification, final check with the network off, and close the stage with a `FACTORY.md` entry that includes the measured token usage and cost, or says plainly that it could not be measured.
