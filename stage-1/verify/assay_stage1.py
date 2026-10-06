#!/usr/bin/env python3
"""Adversarial black-box verifier for Pocketful Stage 1.

Run through ./run-isolated.sh.  It expects a service source directory and a
temporary data directory as its two arguments, and only speaks HTTP to the
isolated service's loopback interface.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SOURCE = Path(sys.argv[1]).resolve()
DATA = Path(sys.argv[2]).resolve()
PORT = 8080


def request(method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def expect(condition, detail):
    if not condition:
        raise AssertionError(detail)


def start():
    process = subprocess.Popen(
        [sys.executable, "app.py", "--data", str(DATA / "pocketful.json")],
        cwd=SOURCE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("server exited early: " + process.stderr.read().decode())
        try:
            status, body, _ = request("GET", "/health")
            if status == 200 and body == b'{"status":"ok"}\n':
                return process
        except OSError:
            time.sleep(0.02)
    process.terminate()
    raise RuntimeError("server did not become healthy")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    expect(process.returncode in (0, -15), "server did not stop cleanly: %r" % (process.returncode,))


def post_user(name):
    return request(
        "POST",
        "/users",
        json.dumps({"name": name}, separators=(",", ":")).encode(),
        {"Content-Type": "application/json"},
    )


def raw_status(payload):
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as connection:
        connection.sendall(payload)
        response = connection.recv(4096)
    first_line = response.split(b"\r\n", 1)[0]
    expect(first_line.startswith(b"HTTP/1.0 ") or first_line.startswith(b"HTTP/1.1 "), "malformed HTTP response")
    return int(first_line.split()[1])


def main():
    process = start()
    users = []
    try:
        # Exact health response, including canonical compact JSON body.
        status, body, headers = request("GET", "/health")
        expect((status, body) == (200, b'{"status":"ok"}\n'), "GET /health was not exactly 200 {status:ok}")
        expect(headers.get("Content-Type") == "application/json; charset=utf-8", "health response was not JSON")

        status, body, _ = post_user("Ada")
        created = json.loads(body)
        expect(status == 201, "POST /users expected 201, got %s" % status)
        expect(set(created) == {"id", "balance_cents"}, "create response leaked or omitted fields")
        expect(created["balance_cents"] == 0, "new wallet was not zero cents")
        expect(isinstance(created["id"], str) and uuid.UUID(created["id"]), "create response id was not a UUID")
        users.append((created["id"], "Ada"))

        status, body, _ = request("GET", "/users/" + created["id"])
        expect(status == 200, "GET created user did not return 200")
        expect(json.loads(body) == {"id": created["id"], "name": "Ada", "balance_cents": 0}, "GET user record differs")

        # Strict JSON/content-type handling, duplicate member attack, and an
        # advertised oversized content length that must be rejected before read.
        invalid = [
            b"",
            b"[]",
            b"{}",
            b'{"name":""}',
            b'{"name":"   "}',
            b'{"name":3}',
            b'{"name":"Ada","role":"admin"}',
            b'{"name":"Ada"} trailing',
            b'{"name":"Ada","name":"Mallory"}',
        ]
        for payload in invalid:
            status, _body, _ = request("POST", "/users", payload, {"Content-Type": "application/json"})
            expect(status == 400, "invalid JSON/body accepted: %r (status %s)" % (payload, status))
        status, _body, _ = request("POST", "/users", b'{"name":"Ada"}', {"Content-Type": "text/plain"})
        expect(status == 400, "wrong content type accepted")
        expect(raw_status(b"POST /users HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}") == 400, "duplicate Content-Length accepted")
        expect(raw_status(b"POST /users HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 1048577\r\n\r\n") == 400, "oversized body accepted")

        status, body, _ = request("GET", "/users/not-a-real-id")
        expect(status == 404 and json.loads(body) == {"error": "user not found"}, "unknown user was not safely rejected")

        # Abrupt partial request must not poison subsequent work.
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as connection:
            connection.sendall(b"POST /users HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 18\r\n\r\n{\"name\":\"part")
        time.sleep(0.05)
        status, body, _ = post_user("retry-after-partial")
        expect(status == 201, "retry after partial request failed")
        retried = json.loads(body)
        users.append((retried["id"], "retry-after-partial"))

        # Race many independent connections, then retain every issued id for
        # restart verification.
        def concurrent_create(number):
            status, body, _ = post_user("racer-%03d" % number)
            return status, json.loads(body) if status == 201 else body

        with ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(concurrent_create, range(160)))
        expect(all(status == 201 for status, _ in results), "one or more concurrent creates failed")
        raced = [(payload["id"], "racer-%03d" % number) for number, (_status, payload) in enumerate(results)]
        expect(len({user_id for user_id, _ in raced}) == 160, "concurrent creates duplicated ids")
        users.extend(raced)

        # Stop hard enough to require a real reload, then prove every prior
        # successful write remains present and unchanged.
        stop(process)
        process = start()
        for user_id, name in users:
            status, body, _ = request("GET", "/users/" + user_id)
            expect(status == 200, "user %s lost after restart" % user_id)
            expect(json.loads(body) == {"id": user_id, "name": name, "balance_cents": 0}, "user %s changed after restart" % user_id)
        print("PASS health=exact create/get=2 strict_rejections=12 partial_retry=ok concurrent=160 restart_records=162")
    finally:
        if process.poll() is None:
            stop(process)


if __name__ == "__main__":
    main()
