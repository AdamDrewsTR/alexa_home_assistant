import importlib.util
import json
import os
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import boto3
from moto import mock_aws


SPEC = importlib.util.spec_from_file_location(
    "relay", Path(__file__).resolve().parents[1] / "lambda" / "relay.py"
)
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)
CLIENT = "amzn1.ask.skill.00000000-0000-0000-0000-000000000001"
REDIRECT = "https://pitangui.amazon.com/api/skill/link/VENDOR"


class RelayTests(unittest.TestCase):
    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        env = patch.dict(os.environ, {
            "AWS_DEFAULT_REGION": "us-east-1", "TABLE": "relay",
            "CLIENT_IDS": json.dumps([CLIENT, "other-client"]), "VENDOR_ID": "VENDOR",
            "CONNECTOR_PARAMETER": "/test/connector", "OAUTH_PARAMETER": "/test/oauth",
            "SOCKET_ENDPOINT": "https://example.execute-api.us-east-1.amazonaws.com/connector",
        })
        env.start()
        self.addCleanup(env.stop)
        relay.PARAMETERS.clear()
        relay.table.cache_clear()
        relay.aws_client.cache_clear()
        boto3.resource("dynamodb").create_table(
            TableName="relay", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "kind", "AttributeType": "S"},
                                  {"AttributeName": "expires", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "pending",
                "KeySchema": [{"AttributeName": "kind", "KeyType": "HASH"},
                              {"AttributeName": "expires", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ssm = boto3.client("ssm")
        ssm.put_parameter(Name="/test/connector", Type="SecureString", Value="connector-key")
        ssm.put_parameter(Name="/test/oauth", Type="SecureString", Value="oauth-secret")

    def socket(self, body=None, connection="home", route="$default", headers=None):
        return relay.handler({"requestContext": {"connectionId": connection, "routeKey": route},
                              "headers": headers or {}, "body": json.dumps(body or {})}, None)

    def connect(self):
        return self.socket(route="$connect", headers={"Authorization": "Bearer connector-key"})

    def respond(self, request_id, result):
        raw = json.dumps(result)
        frames = [
            {"action": "response", "id": request_id, "index": index,
             "total": (len(raw) + relay.RESPONSE_CHUNK - 1) // relay.RESPONSE_CHUNK,
             "data": raw[offset:offset + relay.RESPONSE_CHUNK]}
            for index, offset in enumerate(range(0, len(raw), relay.RESPONSE_CHUNK))
        ]
        for frame in reversed(frames):
            self.assertLessEqual(len(json.dumps(frame).encode()), relay.MAX_MESSAGE)
            self.assertEqual(self.socket(frame)["statusCode"], 200)
        return frames

    def authorize(self, **override):
        params = {"client_id": CLIENT, "redirect_uri": REDIRECT,
                  "response_type": "code", "state": "csrf-state"}
        params.update(override)
        return relay.handler({"routeKey": "GET /authorize", "queryStringParameters": params}, None)

    def approved_code(self):
        response = self.authorize()
        self.assertEqual(response["statusCode"], 200)
        link = relay.scan("link")[0]
        self.connect()
        self.assertEqual(self.socket({"action": "approve", "id": link["pk"]})["statusCode"], 200)
        code = relay.get(link["pk"])["code"]
        nonce = re.search(r"fetch\('/status\?id=' \+ \"([^\"]+)\"", response["body"]).group(1)
        status = relay.handler({"routeKey": "GET /status",
                                "queryStringParameters": {"id": nonce}}, None)
        self.assertIn("csrf-state", status["body"])
        self.assertIn(code, status["body"])
        return code

    def token(self, **fields):
        body = {"grant_type": "authorization_code", "client_id": CLIENT,
                "client_secret": "oauth-secret", "redirect_uri": REDIRECT, **fields}
        response = relay.handler({"routeKey": "POST /token", "body": urlencode(body)}, None)
        return response["statusCode"], json.loads(response["body"])

    def test_account_link_refresh_and_revoke(self):
        code = self.approved_code()
        status, tokens = self.token(code=code)
        self.assertEqual(status, 200)
        self.assertEqual(tokens["expires_in"], 3600)
        self.assertEqual(self.token(code=code)[1]["error"], "invalid_grant")
        status, refreshed = self.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        self.assertEqual(status, 200)
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        grant = relay.scan("grant")[0]
        self.socket({"action": "revoke", "id": grant["pk"]})
        self.assertEqual(self.token(grant_type="refresh_token",
                                    refresh_token=tokens["refresh_token"])[1]["error"], "invalid_grant")
        self.assertEqual(relay.invoke({"token": refreshed["access_token"], "client_id": CLIENT})["status"], 401)
        stored = str(relay.table().scan()["Items"])
        self.assertNotIn(tokens["refresh_token"], stored)
        self.assertNotIn(tokens["access_token"], stored)

    def test_wrong_redirect_or_client_and_expired_code(self):
        self.assertEqual(self.authorize(redirect_uri="https://attacker.example")["statusCode"], 400)
        self.assertEqual(self.authorize(client_id="unknown")["statusCode"], 400)
        code = self.approved_code()
        self.assertEqual(self.token(code=code, client_secret="wrong")[0], 401)
        self.assertEqual(self.token(code=code, client_id="other-client")[1]["error"], "invalid_grant")
        self.assertEqual(self.token(code=code, redirect_uri="https://attacker.example")[1]["error"], "invalid_grant")
        relay.table().update_item(Key={"pk": "code#" + relay.digest(code)},
                                  UpdateExpression="SET expires = :expired",
                                  ExpressionAttributeValues={":expired": int(time.time()) - 1})
        self.assertEqual(self.token(code=code)[1]["error"], "invalid_grant")

    def test_nonce_is_not_the_display_code(self):
        self.authorize()
        link = relay.scan("link")[0]
        response = relay.handler({"routeKey": "GET /status",
                                  "queryStringParameters": {"id": link["display"]}}, None)
        self.assertEqual(response["statusCode"], 400)
        self.assertNotIn("code", json.loads(response["body"]))

    def test_only_current_authenticated_connector_can_approve(self):
        self.assertEqual(self.socket(route="$connect")["statusCode"], 403)
        self.assertEqual(self.connect()["statusCode"], 200)
        self.authorize()
        key = relay.scan("link")[0]["pk"]
        self.assertEqual(self.socket({"action": "approve", "id": key}, connection="attacker")["statusCode"], 403)
        self.assertFalse(relay.get(key)["approved"])
        self.socket(route="$connect", connection="new-home",
                    headers={"Authorization": "Bearer connector-key"})
        self.assertEqual(self.socket({"action": "poll"})["statusCode"], 403)

    def test_roundtrip_correlates_response_and_deduplicates_alexa_retries(self):
        _, tokens = self.token(code=self.approved_code())
        expected = {"status": 200, "body": {"event": {"payload": {"data": "x" * 80_000}}}}

        def send_and_respond(connection, body):
            self.assertEqual(body["path"], "/api/alexa/smart_home")
            self.respond(body["id"], expected)

        request = {"operation": "invoke", "token": tokens["access_token"],
                   "client_id": CLIENT, "path": "/api/alexa/smart_home",
                   "event": {"directive": {"header": {"messageId": "same-alexa-message"}}}}
        with patch.object(relay, "send", side_effect=send_and_respond) as send:
            result = relay.handler(request, None)
            self.assertEqual(relay.handler(request, None), expected)
            send.assert_called_once()
        self.assertEqual(result, expected)
        json.dumps(result)  # DynamoDB numbers must not escape as Decimal.
        cached = [item for item in relay.table().scan()["Items"] if item["pk"].startswith("request#")]
        self.assertEqual(len(cached), 1)
        self.assertLessEqual(cached[0]["expires"], int(time.time()) + 60)

    def test_response_injection_and_expired_tokens_fail(self):
        self.connect()
        self.assertEqual(self.socket({"action": "response", "id": "invented",
                                      "body": {"status": 200}})["statusCode"], 400)
        self.assertEqual(relay.invoke({"token": "invented", "client_id": CLIENT})["status"], 401)

    def test_refresh_renews_active_grant_without_resurrecting_expired_grants(self):
        _, tokens = self.token(code=self.approved_code())
        grant = relay.scan("grant")[0]
        key = "refresh#" + relay.digest(tokens["refresh_token"])
        for pk in (grant["pk"], key):
            relay.table().update_item(Key={"pk": pk}, UpdateExpression="SET expires = :expires",
                                      ExpressionAttributeValues={":expires": int(time.time()) + 10})
        self.assertEqual(self.token(grant_type="refresh_token",
                                    refresh_token=tokens["refresh_token"])[0], 200)
        self.assertGreater(relay.get(grant["pk"])["expires"], int(time.time()) + 89 * 86400)
        self.assertGreater(relay.get(key)["expires"], int(time.time()) + 89 * 86400)
        relay.table().update_item(Key={"pk": grant["pk"]}, UpdateExpression="SET expires = :expires",
                                  ExpressionAttributeValues={":expires": int(time.time()) - 1})
        self.assertEqual(self.token(grant_type="refresh_token",
                                    refresh_token=tokens["refresh_token"])[1]["error"], "invalid_grant")

    def test_accepted_response_size_matches_connector_limit(self):
        self.connect()
        relay.put("request#" + "a" * 32, 60, connection="home", parts={})
        empty = {"status": 200, "body": {"data": ""}}
        result = {"status": 200, "body": {"data": "x" * (relay.MAX_RESPONSE - len(json.dumps(empty)))}}
        self.assertEqual(len(json.dumps(result)), relay.MAX_RESPONSE)
        frames = self.respond("a" * 32, result)
        stored = relay.get("request#" + "a" * 32)
        self.assertEqual(json.loads("".join(stored["parts"][str(index)]
                                           for index in range(stored["total"]))), result)
        self.assertGreater(len(frames), 1)
        self.assertNotEqual(self.socket(frames[0])["statusCode"], 200)

    def test_chunk_validation_rejects_inconsistent_counts_and_unbounded_responses(self):
        self.connect()
        relay.put("request#" + "a" * 32, 60, connection="home", parts={})
        for overrides in (
            {"total": 1000}, {"index": -1}, {"index": True}, {"data": "\u2600"},
            {"total": 2, "index": 0, "data": "too short"}, {"data": "x" * 12_001},
            {"total": 22, "index": 21, "data": "x" * 4001},
        ):
            frame = {"action": "response", "id": "a" * 32, "index": 0, "total": 1,
                     "data": '{"status":200}', **overrides}
            self.assertNotEqual(self.socket(frame)["statusCode"], 200)
        self.assertEqual(relay.get("request#" + "a" * 32)["parts"], {})
        frame = {"action": "response", "id": "a" * 32, "index": 0, "total": 2,
                 "data": "x" * relay.RESPONSE_CHUNK}
        self.assertEqual(self.socket(frame)["statusCode"], 200)
        self.assertNotEqual(self.socket({**frame, "index": 1, "total": 3})["statusCode"], 200)

    def test_poll_lists_pending_requests_without_browser_secrets(self):
        self.authorize()
        self.connect()
        with patch.object(relay, "send") as send:
            self.socket({"action": "poll"})
        response = send.call_args.args[1]
        self.assertEqual(len(response["links"]), 1)
        self.assertEqual(set(response["links"][0]), {"id", "code", "client"})

    def test_repeated_polls_reuse_aws_resources_and_connections(self):
        relay.table.cache_clear()
        relay.aws_client.cache_clear()
        with patch.object(relay.boto3, "resource", wraps=boto3.resource) as resource, \
             patch.object(relay.boto3, "client", wraps=boto3.client) as client:
            self.connect()
            for _ in range(3):
                with patch.object(relay.aws_client("apigatewaymanagementapi"), "post_to_connection"):
                    self.assertEqual(self.socket({"action": "poll"})["statusCode"], 200)
            self.assertEqual(resource.call_count, 1)
            self.assertEqual([call.args[0] for call in client.call_args_list],
                             ["ssm", "apigatewaymanagementapi"])


if __name__ == "__main__":
    unittest.main()
