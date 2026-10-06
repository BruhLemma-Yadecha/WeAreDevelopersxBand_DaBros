import http.client
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app import PocketfulServer, Store, make_handler


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "pocketful.json")
        self.server = PocketfulServer(("127.0.0.1", 0), make_handler(Store(self.path)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tempdir.cleanup()

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def post_json(self, path, value, key=None):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.call("POST", path, json.dumps(value, separators=(",", ":")).encode(), headers)

    def create_user(self, name):
        status, body = self.post_json("/users", {"name": name})
        self.assertEqual(status, 201, body)
        return json.loads(body)["id"]

    def restart_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = PocketfulServer(("127.0.0.1", 0), make_handler(Store(self.path)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body), (200, b'{"status":"ok"}\n'))

    def test_create_user_has_zero_wallet_and_survives_restart(self):
        status, body = self.call("POST", "/users", b'{"name":"Ada"}', {"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        user_id = json.loads(body)["id"]

        restarted = Store(self.path)
        user = restarted.get_user(user_id)
        self.assertEqual(user, {"id": user_id, "name": "Ada", "balance_cents": 0})

    def test_create_user_rejects_malformed_and_noncanonical_requests(self):
        invalid = (b"", b"{}", b'{"name":""}', b'{"name":3}', b'{"name":"Ada","unknown":true}', b'{"name":"Ada"} extra', b'{"name":"\\ud800"}')
        for body in invalid:
            with self.subTest(body=body):
                status, _ = self.call("POST", "/users", body, {"Content-Type": "application/json"})
                self.assertEqual(status, 400)

    def test_concurrent_creates_commit_every_user(self):
        def create(number):
            return self.call("POST", "/users", ('{"name":"User %d"}' % number).encode(), {"Content-Type": "application/json"})

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(create, range(40)))
        self.assertTrue(all(status == 201 for status, _ in results))

        restarted = Store(self.path)
        self.assertEqual(len(restarted._state["users"]), 40)

    def test_deposit_replays_exact_response_and_rejects_key_conflict(self):
        user_id = self.create_user("Ada")
        route = "/users/" + user_id + "/deposits"
        status, first = self.post_json(route, {"amount_cents": 125}, "deposit-1")
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(first)["balance_cents"], 125)

        status, replay = self.post_json(route, {"amount_cents": 125}, "deposit-1")
        self.assertEqual((status, replay), (201, first))
        status, conflict = self.post_json(route, {"amount_cents": 126}, "deposit-1")
        self.assertEqual(status, 409, conflict)
        status, body = self.call("GET", "/users/" + user_id)
        self.assertEqual((status, json.loads(body)["balance_cents"]), (200, 125))

    def test_payment_moves_exact_cents_and_overdraw_is_unchanged(self):
        sender, recipient = self.create_user("Sender"), self.create_user("Recipient")
        self.post_json("/users/" + sender + "/deposits", {"amount_cents": 100}, "fund-sender")
        payment = {"from_user": sender, "to_user": recipient, "amount_cents": 37, "note": "lunch"}
        status, _ = self.post_json("/payments", payment, "payment-1")
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(self.call("GET", "/users/" + sender)[1])["balance_cents"], 63)
        self.assertEqual(json.loads(self.call("GET", "/users/" + recipient)[1])["balance_cents"], 37)

        payment["amount_cents"] = 64
        status, _ = self.post_json("/payments", payment, "overdraw")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(self.call("GET", "/users/" + sender)[1])["balance_cents"], 63)
        self.assertEqual(json.loads(self.call("GET", "/users/" + recipient)[1])["balance_cents"], 37)

    def test_money_requests_require_strict_schema_positive_integer_and_key(self):
        user_id = self.create_user("Ada")
        route = "/users/" + user_id + "/deposits"
        for amount in (0, -1, 1.5, True, "1"):
            with self.subTest(amount=amount):
                status, _ = self.post_json(route, {"amount_cents": amount}, "invalid-" + repr(amount))
                self.assertEqual(status, 400)
        status, _ = self.post_json(route, {"amount_cents": 1, "extra": True}, "extra")
        self.assertEqual(status, 400)
        status, _ = self.post_json(route, {"amount_cents": 1})
        self.assertEqual(status, 400)

    def test_one_hundred_identical_payment_calls_apply_once(self):
        sender, recipient = self.create_user("Sender"), self.create_user("Recipient")
        self.post_json("/users/" + sender + "/deposits", {"amount_cents": 9}, "fund-sender")
        payment = {"from_user": sender, "to_user": recipient, "amount_cents": 9, "note": "once"}

        def pay(_):
            return self.post_json("/payments", payment, "same-payment")

        with ThreadPoolExecutor(max_workers=30) as pool:
            replies = list(pool.map(pay, range(100)))
        self.assertTrue(all(status == 201 for status, _ in replies))
        self.assertEqual(len({body for _, body in replies}), 1)
        self.assertEqual(json.loads(self.call("GET", "/users/" + sender)[1])["balance_cents"], 0)
        self.assertEqual(json.loads(self.call("GET", "/users/" + recipient)[1])["balance_cents"], 9)

    def test_different_payment_keys_racing_for_one_wallet_never_overdraw(self):
        sender, recipient = self.create_user("Sender"), self.create_user("Recipient")
        self.post_json("/users/" + sender + "/deposits", {"amount_cents": 75}, "fund-sender")
        payment = {"from_user": sender, "to_user": recipient, "amount_cents": 1, "note": "race"}

        def pay(number):
            return self.post_json("/payments", payment, "race-" + str(number))[0]

        with ThreadPoolExecutor(max_workers=40) as pool:
            statuses = list(pool.map(pay, range(110)))
        self.assertEqual(statuses.count(201), 75)
        self.assertTrue(all(status in (201, 409) for status in statuses))
        sender_balance = json.loads(self.call("GET", "/users/" + sender)[1])["balance_cents"]
        recipient_balance = json.loads(self.call("GET", "/users/" + recipient)[1])["balance_cents"]
        self.assertEqual((sender_balance, recipient_balance), (0, 75))
        restarted = Store(self.path)
        self.assertEqual(sum(user["balance_cents"] for user in restarted._state["users"].values()), restarted._state["total_deposits_cents"])

    def test_idempotency_and_balances_survive_restart(self):
        user_id = self.create_user("Ada")
        route = "/users/" + user_id + "/deposits"
        status, first = self.post_json(route, {"amount_cents": 8}, "survive-restart")
        self.assertEqual(status, 201)
        self.restart_server()
        status, replay = self.post_json(route, {"amount_cents": 8}, "survive-restart")
        self.assertEqual((status, replay), (201, first))
        self.assertEqual(json.loads(self.call("GET", "/users/" + user_id)[1])["balance_cents"], 8)

    def test_failed_commit_rolls_back_balance_and_idempotency_record(self):
        store = Store(os.path.join(self.tempdir.name, "failure.json"))
        user_id = store.create_user("Ada")["id"]
        original_commit = store._commit_locked
        store._commit_locked = lambda: (_ for _ in ()).throw(RuntimeError("injected failure"))
        with self.assertRaises(RuntimeError):
            store.deposit("failure-key", "/users/" + user_id + "/deposits", user_id, 3)
        store._commit_locked = original_commit
        self.assertEqual(store.get_user(user_id)["balance_cents"], 0)
        self.assertNotIn("failure-key", store._state["idempotency"])

    def test_split_allocates_remainder_in_order_and_records_feeds(self):
        payer, first, second, third = (self.create_user(name) for name in ("Payer", "First", "Second", "Third"))
        for user_id, amount, key in ((first, 3, "fund-first"), (second, 2, "fund-second"), (third, 2, "fund-third")):
            self.assertEqual(self.post_json("/users/" + user_id + "/deposits", {"amount_cents": amount}, key)[0], 201)
        split = {"payer": payer, "participants": [payer, first, second, third], "total_cents": 10, "note": "dinner"}
        status, body = self.post_json("/splits", split, "dinner-split")
        self.assertEqual(status, 201, body)
        response = json.loads(body)
        self.assertEqual(response["shares"], [
            {"user_id": payer, "amount_cents": 3},
            {"user_id": first, "amount_cents": 3},
            {"user_id": second, "amount_cents": 2},
            {"user_id": third, "amount_cents": 2},
        ])
        self.assertEqual(sum(item["amount_cents"] for item in response["shares"]), 10)
        self.assertEqual(json.loads(self.call("GET", "/users/" + payer)[1])["balance_cents"], 7)
        self.assertEqual(json.loads(self.call("GET", "/users/" + first)[1])["balance_cents"], 0)
        status, payer_feed = self.call("GET", "/users/" + payer + "/feed")
        self.assertEqual(status, 200)
        payer_event = json.loads(payer_feed)["events"][0]
        self.assertEqual((payer_event["who"], payer_event["amount_cents"], payer_event["own_share_cents"]), ([first, second, third], 7, 3))
        status, feed = self.call("GET", "/users/" + first + "/feed")
        self.assertEqual(status, 200)
        event = json.loads(feed)["events"][0]
        self.assertEqual((event["type"], event["who"], event["amount_cents"], event["note"]), ("split", payer, 3, "dinner"))
        self.assertIn("timestamp", event)

    def test_rejected_or_invalid_split_leaves_balances_events_and_key_records_unchanged(self):
        payer, funded, unfunded = (self.create_user(name) for name in ("Payer", "Funded", "Unfunded"))
        self.post_json("/users/" + funded + "/deposits", {"amount_cents": 3}, "fund-funded")
        split = {"payer": payer, "participants": [payer, funded, unfunded], "total_cents": 6, "note": "fail"}
        status, _ = self.post_json("/splits", split, "insufficient-split")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(self.call("GET", "/users/" + payer)[1])["balance_cents"], 0)
        self.assertEqual(json.loads(self.call("GET", "/users/" + funded)[1])["balance_cents"], 3)
        self.assertEqual(json.loads(self.call("GET", "/users/" + unfunded)[1])["balance_cents"], 0)
        stored = Store(self.path)
        self.assertEqual(stored._state["events"], [])
        self.assertNotIn("insufficient-split", stored._state["idempotency"])
        self.assertEqual(self.post_json("/users/" + unfunded + "/deposits", {"amount_cents": 2}, "fund-unfunded")[0], 201)
        self.assertEqual(self.post_json("/splits", split, "insufficient-split")[0], 201)

        duplicate = {"payer": payer, "participants": [payer, funded, funded], "total_cents": 3, "note": "bad"}
        missing = {"payer": payer, "participants": [funded, unfunded], "total_cents": 3, "note": "bad"}
        self.assertEqual(self.post_json("/splits", duplicate, "duplicate")[0], 400)
        self.assertEqual(self.post_json("/splits", missing, "missing")[0], 400)

    def test_split_replay_conflict_and_events_survive_restart(self):
        payer, participant = self.create_user("Payer"), self.create_user("Participant")
        self.post_json("/users/" + participant + "/deposits", {"amount_cents": 2}, "fund-participant")
        split = {"payer": payer, "participants": [payer, participant], "total_cents": 3, "note": "taxi"}
        status, first = self.post_json("/splits", split, "taxi-split")
        self.assertEqual(status, 201)
        self.restart_server()
        status, replay = self.post_json("/splits", split, "taxi-split")
        self.assertEqual((status, replay), (201, first))
        changed = dict(split)
        changed["total_cents"] = 2
        self.assertEqual(self.post_json("/splits", changed, "taxi-split")[0], 409)
        status, feed = self.call("GET", "/users/" + participant + "/feed")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(feed)["events"]), 1)

    def test_one_hundred_identical_split_calls_apply_once(self):
        payer, participant = self.create_user("Payer"), self.create_user("Participant")
        self.post_json("/users/" + participant + "/deposits", {"amount_cents": 1}, "fund-participant")
        split = {"payer": payer, "participants": [payer, participant], "total_cents": 2, "note": "once"}

        def submit(_):
            return self.post_json("/splits", split, "same-split")

        with ThreadPoolExecutor(max_workers=30) as pool:
            replies = list(pool.map(submit, range(100)))
        self.assertTrue(all(status == 201 for status, _ in replies))
        self.assertEqual(len({body for _, body in replies}), 1)
        self.assertEqual(json.loads(self.call("GET", "/users/" + payer)[1])["balance_cents"], 1)
        self.assertEqual(json.loads(self.call("GET", "/users/" + participant)[1])["balance_cents"], 0)

    def test_different_split_keys_racing_cannot_overdraw_or_create_money(self):
        payer, participant = self.create_user("Payer"), self.create_user("Participant")
        self.post_json("/users/" + participant + "/deposits", {"amount_cents": 50}, "fund-participant")
        split = {"payer": payer, "participants": [payer, participant], "total_cents": 2, "note": "race"}

        def submit(number):
            return self.post_json("/splits", split, "split-race-" + str(number))[0]

        with ThreadPoolExecutor(max_workers=40) as pool:
            statuses = list(pool.map(submit, range(110)))
        self.assertEqual(statuses.count(201), 50)
        self.assertTrue(all(status in (201, 409) for status in statuses))
        stored = Store(self.path)
        balances = [user["balance_cents"] for user in stored._state["users"].values()]
        self.assertTrue(all(balance >= 0 for balance in balances))
        self.assertEqual(sum(balances), stored._state["total_deposits_cents"])

    def test_payment_feeds_are_newest_first_with_required_fields(self):
        sender, recipient = self.create_user("Sender"), self.create_user("Recipient")
        self.post_json("/users/" + sender + "/deposits", {"amount_cents": 3}, "fund-sender")
        for number, note in enumerate(("first", "second"), start=1):
            payment = {"from_user": sender, "to_user": recipient, "amount_cents": 1, "note": note}
            self.assertEqual(self.post_json("/payments", payment, "feed-" + str(number))[0], 201)
        status, body = self.call("GET", "/users/" + sender + "/feed")
        self.assertEqual(status, 200)
        events = json.loads(body)["events"]
        self.assertEqual([event["note"] for event in events], ["second", "first"])
        for event in events:
            self.assertEqual((event["type"], event["who"], event["amount_cents"]), ("payment", recipient, 1))
            self.assertIn("timestamp", event)


if __name__ == "__main__":
    unittest.main()
