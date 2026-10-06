#!/usr/bin/env python3
"""Black-box adversarial assay for Stage 1 splits and feeds."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
        return response.status, response.read()
    finally:
        connection.close()


def post(path, value, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return call("POST", path, json.dumps(value, separators=(",", ":")).encode(), headers)


def create(name):
    status, body = post("/users", {"name": name})
    require(status == 201, "could not create %s: %s %r" % (name, status, body))
    return json.loads(body)["id"]


def fund(user_id, cents, key):
    return post("/users/%s/deposits" % user_id, {"amount_cents": cents}, key)


def split(value, key):
    return post("/splits", value, key)


def balance(user_id):
    status, body = call("GET", "/users/" + user_id)
    require(status == 200, "GET user failed")
    return json.loads(body)["balance_cents"]


def feed(user_id):
    status, body = call("GET", "/users/%s/feed" % user_id)
    require(status == 200, "GET feed failed: %s" % status)
    return json.loads(body)["events"]


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
            if call("GET", "/health") == (200, b'{"status":"ok"}\n'):
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
    require(process.returncode in (0, -15), "server exit %r" % process.returncode)


def raw_status(payload):
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as connection:
        connection.sendall(payload)
        response = connection.recv(4096)
    first = response.split(b"\r\n", 1)[0].split()
    require(len(first) >= 2 and first[0].startswith(b"HTTP/"), "invalid raw response")
    return int(first[1])


def abandon_complete_split(value, key):
    body = json.dumps(value, separators=(",", ":")).encode()
    request = (
        b"POST /splits HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        b"Idempotency-Key: " + key.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    connection = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    connection.sendall(request)
    connection.close()


def event_is_complete(event):
    required = {"type", "who", "amount_cents", "note", "timestamp", "direction"}
    if not required.issubset(event):
        return False
    try:
        parsed = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and event["type"] in {"payment", "split"} and type(event["amount_cents"]) is int


def main():
    process = start()
    try:
        payer, first, second, third, outsider = [create(name) for name in ("Payer", "First", "Second", "Third", "Outsider")]

        # Strict endpoint and schema validation, including payer membership.
        baseline = {"payer": payer, "participants": [payer, first], "total_cents": 2, "note": "x"}
        invalid = [
            b"", b"[]", b'{"payer":"x"} trailing',
            json.dumps({"payer": payer, "participants": [payer, first], "total_cents": 2}).encode(),
            json.dumps(dict(baseline, extra=True)).encode(),
            json.dumps(dict(baseline, participants=[])).encode(),
            json.dumps(dict(baseline, participants="not-list")).encode(),
            json.dumps(dict(baseline, participants=[payer, first, first])).encode(),
            json.dumps(dict(baseline, participants=[first])).encode(),
            json.dumps(dict(baseline, participants=[payer, payer])).encode(),
            json.dumps(dict(baseline, total_cents=0)).encode(),
            json.dumps(dict(baseline, total_cents=-1)).encode(),
            json.dumps(dict(baseline, total_cents=True)).encode(),
            json.dumps(dict(baseline, total_cents=1.5)).encode(),
            json.dumps(dict(baseline, total_cents="2")).encode(),
            json.dumps(dict(baseline, note=3)).encode(),
        ]
        for number, body in enumerate(invalid):
            status, _ = call("POST", "/splits", body, {"Content-Type": "application/json", "Idempotency-Key": "invalid-%d" % number})
            require(status == 400, "invalid split accepted (%s): %r" % (status, body))
        require(post("/splits", baseline, None)[0] == 400, "split without key accepted")
        require(post("/splits", baseline, " ")[0] == 400, "empty split key accepted")
        raw = b"POST /splits HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nIdempotency-Key: a\r\nIdempotency-Key: b\r\nContent-Length: 2\r\n\r\n{}"
        require(raw_status(raw) == 400, "duplicate idempotency key accepted")
        unknown = dict(baseline, participants=[payer, "missing-user"])
        status, body = split(unknown, "unknown-user")
        require((status, json.loads(body)) == (404, {"error": "user not found"}), "unknown participant unsafe")

        # Remainder allocation observes exact supplied ordering (payer in the
        # middle), and the payer's own share moves no money.
        for user_id, amount, key in ((first, 2, "fund-first"), (second, 1, "fund-second")):
            require(fund(user_id, amount, key)[0] == 201, "funding failed")
        ordered = {"payer": payer, "participants": [first, payer, second], "total_cents": 5, "note": "ordered"}
        status, body = split(ordered, "ordered-split")
        ordered_result = json.loads(body)
        require(status == 201 and ordered_result["shares"] == [
            {"user_id": first, "amount_cents": 2},
            {"user_id": payer, "amount_cents": 2},
            {"user_id": second, "amount_cents": 1},
        ], "ordered remainder allocation wrong: %r" % ordered_result)
        require(sum(entry["amount_cents"] for entry in ordered_result["shares"]) == 5, "shares do not sum")
        require((balance(payer), balance(first), balance(second)) == (3, 0, 0), "split transfers/implicit payer share wrong")

        # Smallest total with payer first has a zero-cent nonpayer obligation.
        smallest = {"payer": payer, "participants": [payer, third], "total_cents": 1, "note": "tiny"}
        status, body = split(smallest, "tiny-split")
        require(status == 201 and json.loads(body)["shares"] == [
            {"user_id": payer, "amount_cents": 1}, {"user_id": third, "amount_cents": 0}], "small split allocation wrong")
        require((balance(payer), balance(third)) == (3, 0), "zero-cent obligation moved money")

        # Very large exact integer split, also with payer not first.
        giant_payer, giant_a, giant_b = [create(name) for name in ("GiantPayer", "GiantA", "GiantB")]
        giant_total = 2**63 - 1
        quotient, remainder = divmod(giant_total, 3)
        giant_shares = [quotient + 1, quotient, quotient]
        require(fund(giant_a, giant_shares[0], "fund-giant-a")[0] == 201, "large funding A failed")
        require(fund(giant_b, giant_shares[1], "fund-giant-b")[0] == 201, "large funding B failed")
        giant = {"payer": giant_payer, "participants": [giant_a, giant_payer, giant_b], "total_cents": giant_total, "note": "huge"}
        status, body = split(giant, "giant-split")
        giant_response = json.loads(body)
        require(status == 201 and [e["amount_cents"] for e in giant_response["shares"]] == giant_shares, "large split lost precision")
        require(sum(e["amount_cents"] for e in giant_response["shares"]) == giant_total, "large shares fail sum")
        require((balance(giant_payer), balance(giant_a), balance(giant_b)) == (giant_shares[0] + giant_shares[2], 0, 0), "large split balances wrong")

        # A multiple-party insufficient split leaves every balance/feed/key
        # unchanged. Reusing its key after funding must be allowed because the
        # failed request did not commit.
        failure_payer, failure_a, failure_b = [create(name) for name in ("FailurePayer", "FailureA", "FailureB")]
        require(fund(failure_a, 3, "fund-failure-a")[0] == 201, "failure setup")
        failing = {"payer": failure_payer, "participants": [failure_payer, failure_a, failure_b], "total_cents": 8, "note": "atomic"}
        require(split(failing, "insufficient-split")[0] == 409, "insufficient split accepted")
        require((balance(failure_payer), balance(failure_a), balance(failure_b)) == (0, 3, 0), "insufficient split partially moved funds")
        require(feed(failure_payer) == [] and feed(failure_a) == [] and feed(failure_b) == [], "failed split leaked events")
        require(fund(failure_b, 2, "fund-failure-b")[0] == 201, "failure retry setup")
        require(split(failing, "insufficient-split")[0] == 201, "failed split key was incorrectly retained")

        # Exactly-once behavior for a detached response and a forced durable
        # failure. The one-shot fault is supplied only in this copied assay app.
        recovery_payer, recovery_participant = create("RecoveryPayer"), create("RecoveryParticipant")
        require(fund(recovery_participant, 3, "fund-recovery")[0] == 201, "recovery setup")
        abandoned = {"payer": recovery_payer, "participants": [recovery_payer, recovery_participant], "total_cents": 4, "note": "abandoned"}
        abandon_complete_split(abandoned, "abandoned-split")
        time.sleep(0.05)
        require(split(abandoned, "abandoned-split")[0] == 201, "abandoned split retry failed")
        require((balance(recovery_payer), balance(recovery_participant)) == (2, 1), "abandoned split applied more than once")
        faulted = {"payer": recovery_payer, "participants": [recovery_payer, recovery_participant], "total_cents": 2, "note": "fault"}
        Path(os.environ["FAIL_COMMIT_MARKER"]).touch()
        require(split(faulted, "faulted-split")[0] == 500, "faulted split did not return 500")
        require((balance(recovery_payer), balance(recovery_participant)) == (2, 1), "faulted split partially committed")
        require(split(faulted, "faulted-split")[0] == 201, "faulted split key was retained")
        require((balance(recovery_payer), balance(recovery_participant)) == (3, 0), "fault retry incorrect")

        # Same successful request has byte-identical replay after restart;
        # changing total or participant ordering conflicts with that key.
        restart_payer, restart_participant = create("RestartPayer"), create("RestartParticipant")
        require(fund(restart_participant, 2, "fund-restart")[0] == 201, "restart setup")
        restart_split = {"payer": restart_payer, "participants": [restart_payer, restart_participant], "total_cents": 3, "note": "restart"}
        status, original = split(restart_split, "restart-split")
        require(status == 201, "restart split failed")
        stop(process)
        process = start()
        status, replay = split(restart_split, "restart-split")
        require((status, replay) == (201, original), "split replay changed across restart")
        require(split(dict(restart_split, total_cents=2), "restart-split")[0] == 409, "changed total did not conflict")
        require(split(dict(restart_split, participants=[restart_participant, restart_payer]), "restart-split")[0] == 409, "changed participant order did not conflict")

        # 120 same-key callers apply one split once.
        same_payer, same_participant = create("SamePayer"), create("SameParticipant")
        require(fund(same_participant, 1, "fund-same")[0] == 201, "same setup")
        same = {"payer": same_payer, "participants": [same_payer, same_participant], "total_cents": 2, "note": "same"}
        def same_call(_):
            return split(same, "same-split")
        with ThreadPoolExecutor(max_workers=48) as pool:
            replies = list(pool.map(same_call, range(120)))
        require(all(status == 201 for status, _ in replies) and len({body for _, body in replies}) == 1, "same-key race was not a stable replay")
        require((balance(same_payer), balance(same_participant)) == (1, 0), "same-key split raced twice")

        # 160 different keys compete for 100 available cents; exactly 100 may
        # settle, and no loser may make a partial transfer.
        race_payer, race_participant = create("RacePayer"), create("RaceParticipant")
        require(fund(race_participant, 100, "fund-race")[0] == 201, "race setup")
        racing = {"payer": race_payer, "participants": [race_payer, race_participant], "total_cents": 2, "note": "race"}
        def competing(number):
            return split(racing, "competing-split-%03d" % number)[0]
        with ThreadPoolExecutor(max_workers=64) as pool:
            statuses = list(pool.map(competing, range(160)))
        require(statuses.count(201) == 100 and all(status in (201, 409) for status in statuses), "competing split outcome wrong")
        require((balance(race_payer), balance(race_participant)) == (100, 0), "competing split overdrew or lost funds")

        # Mixed feeds: membership, field completeness, newest-first ordering,
        # UTC timestamps, no uninvolved leakage, and restart durability.
        feed_sender, feed_recipient, feed_outsider = create("FeedSender"), create("FeedRecipient"), create("FeedOutsider")
        require(fund(feed_sender, 8, "fund-feed-sender")[0] == 201, "feed setup")
        payment_one = {"from_user": feed_sender, "to_user": feed_recipient, "amount_cents": 1, "note": "payment-one"}
        payment_two = {"from_user": feed_sender, "to_user": feed_recipient, "amount_cents": 1, "note": "payment-two"}
        require(post("/payments", payment_one, "feed-payment-one")[0] == 201, "first feed payment")
        feed_split = {"payer": feed_recipient, "participants": [feed_sender, feed_recipient], "total_cents": 2, "note": "feed-split"}
        require(split(feed_split, "feed-split")[0] == 201, "feed split")
        require(post("/payments", payment_two, "feed-payment-two")[0] == 201, "second feed payment")
        sender_feed, recipient_feed, outsider_feed = feed(feed_sender), feed(feed_recipient), feed(feed_outsider)
        require([event["note"] for event in sender_feed] == ["payment-two", "feed-split", "payment-one"], "sender feed not newest first")
        require([event["note"] for event in recipient_feed] == ["payment-two", "feed-split", "payment-one"], "recipient feed not newest first")
        require(outsider_feed == [], "uninvolved user received private events")
        require(all(event_is_complete(event) and "user_id" not in event for event in sender_feed + recipient_feed), "feed fields/timestamps incomplete or leaky")
        split_event = next(event for event in recipient_feed if event["note"] == "feed-split")
        require(split_event["who"] == [feed_sender] and split_event["own_share_cents"] == 1 and split_event["amount_cents"] == 1, "payer split feed summary wrong")
        stop(process)
        process = start()
        require([event["note"] for event in feed(feed_sender)] == ["payment-two", "feed-split", "payment-one"], "feed not durable across restart")

        state = json.loads((DATA / "pocketful.json").read_text(encoding="utf-8"))
        total = sum(record["balance_cents"] for record in state["users"].values())
        require(total == state["total_deposits_cents"], "conservation failure: balances=%s deposits=%s" % (total, state["total_deposits_cents"]))
        print("PASS split_strict=20 ordered_remainder=ok small_large_totals=ok atomic_recovery=ok restart_replay=exact same_key_splits=120 competing_splits=160 feeds=membership_order_timestamp_durable conservation=ok")
    finally:
        stop(process)


if __name__ == "__main__":
    main()
