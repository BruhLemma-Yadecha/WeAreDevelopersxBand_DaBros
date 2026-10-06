#!/usr/bin/env python3
"""Black-box adversarial assay for the Stage 1 money endpoints.

This is run only by run-money-isolated.sh, inside a new OS network namespace.
It starts the service itself and communicates solely over its loopback port.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SOURCE = Path(sys.argv[1]).resolve()
DATA = Path(sys.argv[2]).resolve()
PORT = 8080


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def call(method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def post_json(path, value, key="key"):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return call("POST", path, json.dumps(value, separators=(",", ":")).encode(), headers)


def user(name):
    status, body, _ = post_json("/users", {"name": name}, key=None)
    require(status == 201, "could not create %s: %s %r" % (name, status, body))
    return json.loads(body)["id"]


def balance(user_id):
    status, body, _ = call("GET", "/users/" + user_id)
    require(status == 200, "GET user %s returned %s" % (user_id, status))
    return json.loads(body)["balance_cents"]


def start():
    process = subprocess.Popen(
        [sys.executable, "app.py", "--data", str(DATA / "pocketful.json")],
        cwd=SOURCE,
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError("server exited: " + process.stderr.read().decode())
        try:
            if call("GET", "/health")[:2] == (200, b'{"status":"ok"}\n'):
                return process
        except OSError:
            time.sleep(0.02)
    process.kill()
    raise RuntimeError("server did not become healthy")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
    require(process.returncode in (0, -15), "service stopped unexpectedly: %r" % process.returncode)


def raw_status(request_bytes):
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as connection:
        connection.sendall(request_bytes)
        response = connection.recv(4096)
    first = response.split(b"\r\n", 1)[0].split()
    require(len(first) >= 2 and first[0].startswith(b"HTTP/"), "invalid raw HTTP response: %r" % response[:80])
    return int(first[1])


def abort_valid_deposit(route, key, amount):
    body = json.dumps({"amount_cents": amount}, separators=(",", ":")).encode()
    request = (
        b"POST " + route.encode() + b" HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Type: application/json\r\nIdempotency-Key: " + key.encode() + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    connection = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    connection.sendall(request)
    # Deliberately abandon the response after the complete request body has
    # crossed the socket.  The following retry must still apply at most once.
    connection.close()


def main():
    process = start()
    try:
        alice, bob, carol, big = user("Alice"), user("Bob"), user("Carol"), user("Big")
        alice_deposit = "/users/%s/deposits" % alice

        # Header and wire schema strictness.
        for absent_or_empty in (None, "", "   "):
            status, _body, _ = post_json(alice_deposit, {"amount_cents": 1}, absent_or_empty)
            require(status == 400, "missing/empty idempotency key returned %s" % status)
        duplicate_header = (
            b"POST " + alice_deposit.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Idempotency-Key: first\r\nIdempotency-Key: second\r\nContent-Length: 18\r\n\r\n{\"amount_cents\":1}"
        )
        require(raw_status(duplicate_header) == 400, "duplicate idempotency header accepted")

        malformed_deposit_bodies = [
            b"", b"[]", b'{"amount_cents":1} trailing', b'{"amount_cents":1,"amount_cents":2}',
            b'{"amount_cents":1,"extra":true}', b'{"amount_cents":0}', b'{"amount_cents":-1}',
            b'{"amount_cents":true}', b'{"amount_cents":1.0}', b'{"amount_cents":"1"}',
        ]
        for number, body in enumerate(malformed_deposit_bodies):
            status, _body, _ = call("POST", alice_deposit, body, {"Content-Type": "application/json", "Idempotency-Key": "bad-deposit-%d" % number})
            require(status == 400, "invalid deposit body %r returned %s" % (body, status))

        payment_template = {"from_user": alice, "to_user": bob, "amount_cents": 1, "note": "n"}
        for value in (0, -1, True, 1.0, "1"):
            bad = dict(payment_template, amount_cents=value)
            status, _body, _ = post_json("/payments", bad, "bad-payment-%r" % value)
            require(status == 400, "invalid payment amount %r returned %s" % (value, status))
        largest_signed_64_bit_cent_value = 2**63 - 1
        status, _body, _ = post_json(
            "/users/%s/deposits" % big,
            {"amount_cents": largest_signed_64_bit_cent_value},
            "large-integer-boundary",
        )
        require(status == 201 and balance(big) == largest_signed_64_bit_cent_value, "large integer cents lost precision")
        for bad in (
            {"from_user": alice, "to_user": bob, "amount_cents": 1},
            dict(payment_template, unexpected=True),
            dict(payment_template, from_user=""),
            dict(payment_template, note=3),
        ):
            status, _body, _ = post_json("/payments", bad, "bad-payment-schema-%r" % sorted(bad))
            require(status == 400, "invalid payment schema returned %s" % status)

        unknown = "/users/not-a-user/deposits"
        status, body, _ = post_json(unknown, {"amount_cents": 1}, "unknown-deposit")
        require((status, json.loads(body)) == (404, {"error": "user not found"}), "unknown deposit user unsafe")
        missing_payment = dict(payment_template, from_user="not-a-user")
        status, body, _ = post_json("/payments", missing_payment, "unknown-payment")
        require((status, json.loads(body)) == (404, {"error": "user not found"}), "unknown payment user unsafe")
        status, _body, _ = post_json("/payments", dict(payment_template, from_user=alice, to_user=alice), "self-payment")
        require(status == 400, "self-payment accepted")
        status, _body, _ = post_json("/not-money", {"amount_cents": 1}, "wrong-path")
        require(status == 404, "unknown mutation path returned %s" % status)

        # Exact replay after process restart, and conflict across both body and path.
        status, first_deposit, _ = post_json(alice_deposit, {"amount_cents": 200}, "restart-key")
        require(status == 201, "initial deposit failed")
        stop(process)
        process = start()
        status, replay, _ = post_json(alice_deposit, {"amount_cents": 200}, "restart-key")
        require((status, replay) == (201, first_deposit), "same key/body did not replay exactly after restart")
        require(balance(alice) == 200, "replay changed balance")
        require(post_json(alice_deposit, {"amount_cents": 201}, "restart-key")[0] == 409, "different body reused key")
        bob_deposit = "/users/%s/deposits" % bob
        require(post_json(bob_deposit, {"amount_cents": 200}, "restart-key")[0] == 409, "different path reused key")
        require(post_json("/payments", dict(payment_template, amount_cents=1), "restart-key")[0] == 409, "different endpoint reused key")

        # 120 identical deposits: one write, identical response to every caller.
        def deposit_once(_):
            return post_json(bob_deposit, {"amount_cents": 7}, "same-deposit")[:2]

        with ThreadPoolExecutor(max_workers=48) as pool:
            deposit_replies = list(pool.map(deposit_once, range(120)))
        require(all(status == 201 for status, _ in deposit_replies), "same-key deposit did not return 201 to all callers")
        require(len({body for _, body in deposit_replies}) == 1 and balance(bob) == 7, "same-key deposits applied more than once")

        # An abandoned complete request and retry also must apply just once.
        abort_valid_deposit(alice_deposit, "abandoned-deposit", 11)
        time.sleep(0.05)
        status, _body, _ = post_json(alice_deposit, {"amount_cents": 11}, "abandoned-deposit")
        require(status == 201 and balance(alice) == 211, "abandoned request retry was not exactly once")

        # HTTP-visible persistence failure rolls back both balance and key.
        # The isolated copy's sitecustomize fault hook fails exactly the next
        # atomic replacement; production source remains unmodified.
        Path(os.environ["FAIL_COMMIT_MARKER"]).touch()
        status, _body, _ = post_json(alice_deposit, {"amount_cents": 13}, "commit-failure")
        require(status == 500, "injected commit failure did not surface as 500 (got %s)" % status)
        status, _body, _ = post_json(alice_deposit, {"amount_cents": 13}, "commit-failure")
        require(status == 201 and balance(alice) == 224, "commit failure leaked a balance or idempotency record")

        # 120 identical payments must transfer once; then distinct keys compete
        # to debit the remaining funds, with no overdraft possible.
        payment = {"from_user": alice, "to_user": bob, "amount_cents": 24, "note": "same"}
        def pay_once(_):
            return post_json("/payments", payment, "same-payment")[:2]

        with ThreadPoolExecutor(max_workers=48) as pool:
            payment_replies = list(pool.map(pay_once, range(120)))
        require(all(status == 201 for status, _ in payment_replies), "same-key payment failed")
        require(len({body for _, body in payment_replies}) == 1 and balance(alice) == 200 and balance(bob) == 31, "same-key payment applied more than once")

        race_payment = {"from_user": alice, "to_user": carol, "amount_cents": 1, "note": "race"}
        def distinct_debit(number):
            return post_json("/payments", race_payment, "debit-race-%03d" % number)[0]

        with ThreadPoolExecutor(max_workers=64) as pool:
            statuses = list(pool.map(distinct_debit, range(240)))
        require(statuses.count(201) == 200 and all(status in (201, 409) for status in statuses), "competing debits over- or under-applied: %r" % {s: statuses.count(s) for s in set(statuses)})
        require((balance(alice), balance(bob), balance(carol), balance(big)) == (0, 31, 200, largest_signed_64_bit_cent_value), "competing debits damaged balances")

        # The durable ledger's recorded deposit total must equal all balances;
        # payments are zero-sum.  Reload independently after a clean stop.
        stop(process)
        process = start()
        state = json.loads((DATA / "pocketful.json").read_text(encoding="utf-8"))
        total_balance = sum(entry["balance_cents"] for entry in state["users"].values())
        expected_total = largest_signed_64_bit_cent_value + 231
        require(total_balance == state["total_deposits_cents"] == expected_total, "conservation failed: balances=%s deposits=%s" % (total_balance, state["total_deposits_cents"]))
        print("PASS strict_money=24 large_integer=9223372036854775807 idempotency_restart=exact same_key_deposits=120 same_key_payments=120 distinct_debits=240 commit_rollback=ok aborted_retry=once conservation=9223372036854776038")
    finally:
        stop(process)


if __name__ == "__main__":
    main()
