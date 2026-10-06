#!/usr/bin/env python3
"""Pocketful stage-1 HTTP service.  It deliberately uses only Python's standard library."""

import argparse
import copy
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4


MAX_BODY_BYTES = 1_048_576


class RequestError(ValueError):
    pass


def _without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestError("duplicate JSON member")
        result[key] = value
    return result


def strict_json_object(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RequestError) as exc:
        raise RequestError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise RequestError("JSON body must be an object")
    return value


def positive_cents(value):
    if type(value) is not int or value < 1:
        raise RequestError("amount_cents must be an integer of at least 1")
    return value


def required_text(value, field, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value):
        raise RequestError(field + " must be a string" + ("" if allow_empty else " and not empty"))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RequestError(field + " must be valid Unicode") from exc
    return value


class Store:
    """A process-local, lock-protected store with replace-on-commit durability."""

    def __init__(self, location):
        self.path = Path(location)
        self._lock = RLock()
        self._state = self._load()

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "users": {}, "idempotency": {}, "total_deposits_cents": 0, "events": []}
        try:
            with self.path.open("r", encoding="utf-8") as source:
                state = json.load(source, object_pairs_hook=_without_duplicate_keys)
            if (
                not isinstance(state, dict)
                or state.get("version") != 1
                or not isinstance(state.get("users"), dict)
            ):
                raise ValueError("invalid state")
            for user_id, user in state["users"].items():
                if not isinstance(user_id, str) or not isinstance(user, dict):
                    raise ValueError("invalid user")
                if set(user) != {"id", "name", "balance_cents"} or user["id"] != user_id:
                    raise ValueError("invalid user")
                if not isinstance(user["name"], str) or not isinstance(user["balance_cents"], int) or isinstance(user["balance_cents"], bool):
                    raise ValueError("invalid user")
                user["name"].encode("utf-8")
                if user["balance_cents"] < 0:
                    raise ValueError("invalid user")
            # The foundation format contained only users.  Its zero balances
            # make this an unambiguous, backwards-compatible migration.
            state.setdefault("idempotency", {})
            state.setdefault("total_deposits_cents", 0)
            state.setdefault("events", [])
            if not isinstance(state["idempotency"], dict):
                raise ValueError("invalid idempotency records")
            if type(state["total_deposits_cents"]) is not int or state["total_deposits_cents"] < 0:
                raise ValueError("invalid deposit total")
            if not isinstance(state["events"], list):
                raise ValueError("invalid events")
            for event in state["events"]:
                if not isinstance(event, dict) or not isinstance(event.get("user_id"), str):
                    raise ValueError("invalid event")
            for key, record in state["idempotency"].items():
                if not isinstance(key, str) or not isinstance(record, dict):
                    raise ValueError("invalid idempotency record")
                if set(record) != {"request", "status", "response"}:
                    raise ValueError("invalid idempotency record")
                if not isinstance(record["request"], str) or type(record["status"]) is not int or not isinstance(record["response"], dict):
                    raise ValueError("invalid idempotency record")
        except (OSError, ValueError, json.JSONDecodeError, RequestError) as exc:
            raise RuntimeError("cannot load durable store") from exc
        return state

    def _commit_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self._state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary_name = None
        try:
            with NamedTemporaryFile("wb", dir=self.path.parent, prefix=self.path.name + ".", delete=False) as temporary:
                temporary_name = temporary.name
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
            raise RuntimeError("durable commit failed") from exc

    def create_user(self, name):
        if not isinstance(name, str) or not name.strip():
            raise RequestError("name must be a non-empty string")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RequestError("name must be valid Unicode") from exc
        with self._lock:
            user_id = str(uuid4())
            user = {"id": user_id, "name": name, "balance_cents": 0}
            self._state["users"][user_id] = user
            try:
                self._commit_locked()
            except Exception:
                del self._state["users"][user_id]
                raise
            return dict(user)

    def get_user(self, user_id):
        with self._lock:
            user = self._state["users"].get(user_id)
            return None if user is None else dict(user)

    @staticmethod
    def _timestamp():
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _append_event(working, user_id, event_type, who, amount, note, timestamp, direction, **extra):
        event = {
            "user_id": user_id,
            "type": event_type,
            "who": who,
            "amount_cents": amount,
            "note": note,
            "timestamp": timestamp,
            "direction": direction,
        }
        event.update(extra)
        working["events"].append(event)

    def get_feed(self, user_id):
        with self._lock:
            if user_id not in self._state["users"]:
                return None
            result = []
            for stored in reversed(self._state["events"]):
                if stored["user_id"] == user_id:
                    event = dict(stored)
                    del event["user_id"]
                    result.append(event)
            return result

    @staticmethod
    def _request_marker(method, path, body):
        canonical_body = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return method + "\n" + path + "\n" + canonical_body

    def _idempotent_mutation(self, key, method, path, body, apply):
        marker = self._request_marker(method, path, body)
        with self._lock:
            existing = self._state["idempotency"].get(key)
            if existing is not None:
                if existing["request"] != marker:
                    return 409, {"error": "idempotency key conflicts with a different request"}
                return existing["status"], copy.deepcopy(existing["response"])

            before = self._state
            working = copy.deepcopy(before)
            status, response = apply(working)
            # A rejected business operation is not a committed mutation: it
            # leaves balances, feeds, and key records exactly as they were.
            if status >= 400:
                return status, copy.deepcopy(response)
            working["idempotency"][key] = {
                "request": marker,
                "status": status,
                "response": copy.deepcopy(response),
            }
            self._state = working
            try:
                self._commit_locked()
            except Exception:
                self._state = before
                raise
            return status, copy.deepcopy(response)

    def deposit(self, key, path, user_id, amount):
        body = {"amount_cents": amount}

        def apply(working):
            user = working["users"].get(user_id)
            if user is None:
                return 404, {"error": "user not found"}
            user["balance_cents"] += amount
            working["total_deposits_cents"] += amount
            return 201, {"user_id": user_id, "amount_cents": amount, "balance_cents": user["balance_cents"]}

        return self._idempotent_mutation(key, "POST", path, body, apply)

    def payment(self, key, path, from_user, to_user, amount, note):
        body = {"from_user": from_user, "to_user": to_user, "amount_cents": amount, "note": note}

        def apply(working):
            sender = working["users"].get(from_user)
            recipient = working["users"].get(to_user)
            if sender is None or recipient is None:
                return 404, {"error": "user not found"}
            if from_user == to_user:
                return 400, {"error": "from_user and to_user must differ"}
            if sender["balance_cents"] < amount:
                return 409, {"error": "insufficient funds"}
            sender["balance_cents"] -= amount
            recipient["balance_cents"] += amount
            timestamp = self._timestamp()
            self._append_event(working, from_user, "payment", to_user, amount, note, timestamp, "sent")
            self._append_event(working, to_user, "payment", from_user, amount, note, timestamp, "received")
            return 201, {
                "from_user": from_user,
                "to_user": to_user,
                "amount_cents": amount,
                "note": note,
                "from_balance_cents": sender["balance_cents"],
                "to_balance_cents": recipient["balance_cents"],
            }

        return self._idempotent_mutation(key, "POST", path, body, apply)

    def split(self, key, path, payer, participants, total, note):
        body = {"payer": payer, "participants": participants, "total_cents": total, "note": note}

        def apply(working):
            if any(user_id not in working["users"] for user_id in participants):
                return 404, {"error": "user not found"}
            quotient, remainder = divmod(total, len(participants))
            shares = [quotient + (1 if index < remainder else 0) for index in range(len(participants))]
            for user_id, share in zip(participants, shares):
                if user_id != payer and working["users"][user_id]["balance_cents"] < share:
                    return 409, {"error": "insufficient funds"}

            for user_id, share in zip(participants, shares):
                if user_id != payer:
                    working["users"][user_id]["balance_cents"] -= share
                    working["users"][payer]["balance_cents"] += share

            timestamp = self._timestamp()
            payer_share = shares[participants.index(payer)]
            non_payers = [user_id for user_id in participants if user_id != payer]
            self._append_event(
                working, payer, "split", non_payers, total - payer_share, note, timestamp, "received",
                own_share_cents=payer_share,
            )
            for user_id, share in zip(participants, shares):
                if user_id != payer:
                    self._append_event(working, user_id, "split", payer, share, note, timestamp, "paid")

            return 201, {
                "payer": payer,
                "total_cents": total,
                "note": note,
                "shares": [{"user_id": user_id, "amount_cents": share} for user_id, share in zip(participants, shares)],
            }

        return self._idempotent_mutation(key, "POST", path, body, apply)


class PocketfulServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Pocketful/1"

        def log_message(self, _format, *_args):
            return

        def send_json(self, status, payload):
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def send_error_json(self, status, message):
            self.send_json(status, {"error": message})

        def read_json_body(self):
            content_types = self.headers.get_all("Content-Type") or []
            if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise RequestError("Content-Type must be application/json")
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                raise RequestError("Content-Length is required")
            try:
                length = int(lengths[0], 10)
            except ValueError as exc:
                raise RequestError("invalid Content-Length") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise RequestError("request body is too large")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RequestError("incomplete request body")
            return strict_json_object(raw)

        def idempotency_key(self):
            keys = self.headers.get_all("Idempotency-Key") or []
            if len(keys) != 1 or not keys[0].strip():
                raise RequestError("Idempotency-Key is required")
            if len(keys[0]) > 512:
                raise RequestError("Idempotency-Key is too long")
            return keys[0]

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, {"status": "ok"})
                return
            route_parts = path.split("/")
            if len(route_parts) == 4 and route_parts[1] == "users" and route_parts[2] and route_parts[3] == "feed":
                events = store.get_feed(route_parts[2])
                if events is None:
                    self.send_error_json(404, "user not found")
                else:
                    self.send_json(200, {"events": events})
                return
            prefix = "/users/"
            if path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]:
                user = store.get_user(path[len(prefix):])
                if user is None:
                    self.send_error_json(404, "user not found")
                else:
                    self.send_json(200, user)
                return
            self.send_error_json(404, "not found")

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                body = self.read_json_body()
                if path == "/users":
                    if set(body) != {"name"}:
                        raise RequestError("body must contain only name")
                    user = store.create_user(body["name"])
                    self.send_json(201, {"id": user["id"], "balance_cents": 0})
                    return

                key = self.idempotency_key()
                route_parts = path.split("/")
                if len(route_parts) == 4 and route_parts[1] == "users" and route_parts[2] and route_parts[3] == "deposits":
                    if set(body) != {"amount_cents"}:
                        raise RequestError("body must contain only amount_cents")
                    amount = positive_cents(body["amount_cents"])
                    status, response = store.deposit(key, path, route_parts[2], amount)
                    self.send_json(status, response)
                    return
                if path == "/payments":
                    if set(body) != {"from_user", "to_user", "amount_cents", "note"}:
                        raise RequestError("body must contain from_user, to_user, amount_cents, and note")
                    from_user = required_text(body["from_user"], "from_user")
                    to_user = required_text(body["to_user"], "to_user")
                    amount = positive_cents(body["amount_cents"])
                    note = required_text(body["note"], "note", allow_empty=True)
                    status, response = store.payment(key, path, from_user, to_user, amount, note)
                    self.send_json(status, response)
                    return
                if path == "/splits":
                    if set(body) != {"payer", "participants", "total_cents", "note"}:
                        raise RequestError("body must contain payer, participants, total_cents, and note")
                    payer = required_text(body["payer"], "payer")
                    if type(body["participants"]) is not list or not body["participants"]:
                        raise RequestError("participants must be a non-empty array")
                    participants = [required_text(value, "participant") for value in body["participants"]]
                    if len(set(participants)) != len(participants):
                        raise RequestError("participants must be unique")
                    if participants.count(payer) != 1:
                        raise RequestError("participants must contain payer exactly once")
                    total = positive_cents(body["total_cents"])
                    note = required_text(body["note"], "note", allow_empty=True)
                    status, response = store.split(key, path, payer, participants, total, note)
                    self.send_json(status, response)
                    return
                self.send_error_json(404, "not found")
                return
            except RequestError as exc:
                self.send_error_json(400, str(exc))
                return
            except RuntimeError:
                self.send_error_json(500, "storage failure")
                return

        def do_PUT(self):
            self.send_error_json(405, "method not allowed")

        def do_DELETE(self):
            self.send_error_json(405, "method not allowed")

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Run the Pocketful stage-1 service")
    parser.add_argument("--data", default="pocketful.json", help="path for durable local JSON storage")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = PocketfulServer((args.host, args.port), make_handler(Store(args.data)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
