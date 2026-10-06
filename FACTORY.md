# Factory log

## Stage 1 — pocketful

### Plan and acceptance evidence

1. **Foundation and read API.** Build a standard-library JSON HTTP server on port 8080 with durable local storage, `GET /health`, user creation, and user lookup.  The assayer will build from a clean copy, create a user, check its zero balance and restart the process to check that user survives.
2. **Atomic money mutations.** Implement deposits and payments with strict JSON validation, required idempotency keys, atomic durable commits, no overdrafts, and stable replay/409 behavior.  The assayer will test all validation boundaries, replay same and conflicting bodies, kill/restart around writes, and race 100 identical and competing writes.
3. **Atomic splits and feed.** Implement ordered-cent remainder allocation, all-or-nothing settlement, event feeds newest-first, and durable timestamps/events.  The assayer will run split arithmetic/rollback cases, concurrent conflicts, inspect both feeds, and restart to verify events and balances.
4. **Delivery and adversarial suite.** Add unit/integration tests, `verify/` attack scripts, README build/run commands, and a no-network container build file.  The assayer will run the documented workflow in a clean, OS-network-disabled environment and retain each attack script as evidence.

### Invariants to preserve

- Amounts are exact, non-negative integer cents; mutation amounts must be at least one cent.
- Durably committed balances always sum to durably committed deposits; transfers and splits conserve value.
- Balances never become negative, including with concurrent, different-key requests.
- Every money mutation either commits completely once with its idempotency record and response, or leaves no persistent state change.  Same key plus same canonical request replays; a different request yields HTTP 409.
- Split shares sum exactly to `total_cents`; leftover cents are assigned in participant-list order; the payer's own share is implicit; every non-payer obligation commits or rolls back together.
- User/event data and idempotency outcomes survive a clean restart.

### Assumptions

- IDs, error-body fields, timestamp encoding, and storage format are implementation-defined, provided the specified statuses/data and invariants are observable.
- Duplicate participant IDs are rejected to prevent ambiguous split obligations.
- A valid split includes the payer exactly once in participants and has a positive total; this is recorded rather than inferred as an alternative settlement rule.

### Work-item rounds

- Work item 1 (foundation/read API): PASS, round 1. Assayer evidence: `stage-1/verify/run-isolated.sh` in a fresh user+network namespace; exact result `PASS health=exact create/get=2 strict_rejections=12 partial_retry=ok concurrent=160 restart_records=162`.
- Work item 2 (money mutations): PASS, round 1. Assayer evidence: `stage-1/verify/run-money-isolated.sh` in a fresh user+network namespace; exact result `PASS strict_money=24 large_integer=9223372036854775807 idempotency_restart=exact same_key_deposits=120 same_key_payments=120 distinct_debits=240 commit_rollback=ok aborted_retry=once conservation=9223372036854776038`.
- Work item 3 (splits/feed): PASS, round 1. Assayer evidence: `stage-1/verify/run-splits-isolated.sh` in a fresh user+network namespace; exact result `PASS split_strict=20 ordered_remainder=ok small_large_totals=ok atomic_recovery=ok restart_replay=exact same_key_splits=120 competing_splits=160 feeds=membership_order_timestamp_durable conservation=ok`.
- Work item 4 (delivery/adversarial verification): PASS, round 1. From a clean copy, the assayer passed all 17 unit tests and all three OS-network-isolated verifier suites. README, source/tests, Dockerfile, and executable verification artifacts are correctly contained in `stage-1/`. No Docker/Podman/nerdctl engine is installed, so the Dockerfile passed static inspection but could not be built or run in this runtime.

### Final foreman check

Copied `stage-1/` to `/tmp/tmp.s4EfyJicmj/stage-1`, compiled it, and started it in `unshare --user --map-root-user --net` with only loopback enabled. A direct in-namespace request returned `200 {"status":"ok"}`. Exact result: `FINAL_PASS build=ok network_namespace=isolated health=200`.

### Stage closure

Stage 1 closed after all four work items passed independent verification and the final OS-network-isolated check passed. Container execution is the only unavailable evidence item because this runtime has no supported container engine; the required container build file is present and independently statically reviewed.

### Usage and cost

Could not be measured for this room and all three factory seats. `band usage rooms --json` did not include room `6ec51bfc-2241-4da3-b57e-ce1eeee19864`; the per-session report attributed foreman and builder records to a different room and listed the active work as unattributed. No stage token or cost number is recorded.
