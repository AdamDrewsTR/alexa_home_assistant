import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "bridge", Path(__file__).resolve().parents[1] / "lambda" / "bridge.py"
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

SKILL = "amzn1.ask.skill.00000000-0000-0000-0000-000000000002"


def smart_event(location="scope"):
    directive = {
        "header": {"payloadVersion": "3", "correlationToken": "correlation"},
        "payload": {},
        "endpoint": {"endpointId": "light#kitchen"},
    }
    scope = {"type": "BearerToken", "token": "test-account-token"}
    if location == "endpoint":
        directive["endpoint"]["scope"] = scope
    else:
        directive["payload"][location] = scope
    return {"directive": directive}


def custom_event():
    return {
        "session": {
            "application": {"applicationId": SKILL},
            "user": {"accessToken": "test-account-token"},
        },
        "request": {
            "type": "IntentRequest",
            "intent": {"name": "HomeStatusIntent", "slots": {}},
        },
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"BASE_URL": "https://ha.example.com", "SKILL_ID": SKILL})
        env.start()
        self.addCleanup(env.stop)
        transport = patch.object(bridge.http.client, "HTTPSConnection")
        self.connection_class = transport.start()
        self.addCleanup(transport.stop)
        self.connection = self.connection_class.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.expected = {"version": "1.0", "response": {"shouldEndSession": True}}
        self.response.read.return_value = json.dumps(self.expected).encode()

    def test_all_smart_token_locations_preserve_full_event(self):
        for location in ("scope", "grantee", "endpoint"):
            with self.subTest(location=location):
                event = smart_event(location)
                self.assertEqual(bridge.smart_home(event, None), self.expected)
                args, kwargs = self.connection.request.call_args
                self.assertEqual(args, ("POST", "/api/alexa/smart_home"))
                self.assertEqual(json.loads(kwargs["body"]), event)
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-account-token")
        self.assertEqual(self.connection.close.call_count, 3)

    def test_missing_smart_token_preserves_error_context(self):
        event = smart_event()
        event["directive"]["payload"]["scope"].pop("token")
        result = bridge.smart_home(event, None)["event"]
        self.assertEqual(result["payload"]["type"], "INVALID_AUTHORIZATION_CREDENTIAL")
        self.assertEqual(result["header"]["correlationToken"], "correlation")
        self.assertEqual(result["endpoint"]["endpointId"], "light#kitchen")
        self.connection.request.assert_not_called()

    def test_malformed_smart_directive(self):
        for event in ({}, {"directive": []}, {"directive": {"header": "invalid"}}):
            with self.subTest(event=event):
                result = bridge.smart_home(event, None)
                self.assertEqual(result["event"]["payload"]["type"], "INVALID_DIRECTIVE")
        self.connection.request.assert_not_called()

    def test_custom_session_and_context_user_tokens(self):
        event = custom_event()
        self.assertEqual(bridge.custom(event, None), self.expected)
        self.assertEqual(self.connection.request.call_args.args[1], "/api/alexa")
        event["context"] = {"System": event.pop("session")}
        self.assertEqual(bridge.custom(event, None), self.expected)
        self.assertEqual(json.loads(self.connection.request.call_args.kwargs["body"]), event)

    def test_custom_application_must_match(self):
        event = custom_event()
        event["session"]["application"]["applicationId"] = "different-skill"
        self.assertIn("not configured", bridge.custom(event, None)["response"]["outputSpeech"]["text"])
        self.connection.request.assert_not_called()

    def test_custom_link_card_for_missing_or_expired_token(self):
        event = custom_event()
        event["session"]["user"].pop("accessToken")
        self.assertEqual(bridge.custom(event, None)["response"]["card"], {"type": "LinkAccount"})
        self.connection.request.assert_not_called()
        self.response.status = 401
        self.assertEqual(
            bridge.custom(custom_event(), None)["response"]["card"], {"type": "LinkAccount"}
        )

    def test_proxy_denial_does_not_request_account_relinking(self):
        self.response.status = 403
        result = bridge.custom(custom_event(), None)["response"]
        self.assertNotIn("card", result)
        self.assertIn("unavailable", result["outputSpeech"]["text"])

    def test_session_ended_request_is_forwarded_but_errors_do_not_speak(self):
        event = custom_event()
        event["request"] = {"type": "SessionEndedRequest", "reason": "USER_INITIATED"}
        self.assertEqual(bridge.custom(event, None), self.expected)
        self.response.status = 503
        self.assertEqual(bridge.custom(event, None), {"version": "1.0", "response": {}})

    def test_upstream_failures_are_not_leaked_or_followed(self):
        for status in (301, 302, 403, 429, 500):
            with self.subTest(status=status):
                self.response.status = status
                self.response.read.return_value = b"private upstream detail"
                with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
                    result = bridge.smart_home(smart_event(), None)
                self.assertEqual(result["event"]["payload"]["type"], "BRIDGE_UNREACHABLE")
                self.assertNotIn("private upstream detail", json.dumps(result) + str(logs.output))
                self.response.read.assert_not_called()

    def test_timeout_and_invalid_json_surface_failure(self):
        for body in (b"not json", b"[]", b"\xff"):
            with self.subTest(body=body):
                self.response.read.return_value = body
                result = bridge.smart_home(smart_event(), None)
                self.assertEqual(result["event"]["payload"]["type"], "BRIDGE_UNREACHABLE")
        self.connection.request.side_effect = TimeoutError()
        result = bridge.smart_home(smart_event(), None)
        self.assertEqual(result["event"]["payload"]["type"], "BRIDGE_UNREACHABLE")

    def test_insecure_or_non_origin_urls_are_rejected(self):
        for url in ("http://ha.example.com", "https://user:pass@ha.example.com",
                    "https://ha.example.com/path", "https://ha.example.com:8123"):
            with self.subTest(url=url), patch.dict(os.environ, {"BASE_URL": url}):
                result = bridge.smart_home(smart_event(), None)
                self.assertEqual(result["event"]["payload"]["type"], "BRIDGE_UNREACHABLE")
        self.connection.request.assert_not_called()

    def test_credentials_are_not_logged_on_success_or_rejection(self):
        for status in (200, 401):
            self.response.status = status
            with patch.object(bridge.LOGGER, "handle") as handle:
                bridge.smart_home(smart_event(), None)
                bridge.custom(custom_event(), None)
                self.assertNotIn("test-account-token", str(handle.call_args_list))


if __name__ == "__main__":
    unittest.main()
