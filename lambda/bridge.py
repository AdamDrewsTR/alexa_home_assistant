"""Alexa bridges using Home Assistant's native OAuth and Alexa endpoints."""

import http.client
import json
import logging
import os
from functools import cache
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
HTTP_TIMEOUT = 5


class UpstreamError(Exception):
    """An unusable Home Assistant response."""


@cache
def relay_client():
    import boto3
    from botocore.config import Config

    return boto3.client("lambda", config=Config(
        connect_timeout=1, read_timeout=6, retries={"total_max_attempts": 1},
    ))


if os.environ.get("RELAY_FUNCTION"):
    # Initialize the SDK during Lambda initialization, not inside Alexa's response window.
    relay_client()


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _forward(event: dict[str, Any], token: str, path: str) -> dict[str, Any]:
    if os.environ.get("RELAY_FUNCTION"):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = relay_client().invoke(
                FunctionName=os.environ["RELAY_FUNCTION"],
                Payload=json.dumps({"operation": "invoke", "event": event, "token": token,
                                    "path": path, "client_id": os.environ["SKILL_ID"]}).encode(),
            )
        except (BotoCoreError, ClientError) as error:
            LOGGER.error("Relay invocation failed (%s)", type(error).__name__)
            raise UpstreamError("Relay invocation failed") from error
        result = json.loads(response["Payload"].read())
        if response.get("FunctionError"):
            raise UpstreamError("Relay execution failed")
        if result.get("status") == 401:
            raise PermissionError("Relay rejected account credentials")
        if result.get("status") != 200 or not isinstance(result.get("body"), dict):
            LOGGER.error("Relay returned HTTP %s", result.get("status"))
            raise UpstreamError("Relay unavailable")
        return result["body"]
    origin = urlsplit(os.environ["BASE_URL"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.port not in (None, 443)
        or origin.username is not None
        or origin.password is not None
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise ValueError("BASE_URL must be an HTTPS origin on port 443")

    connection = http.client.HTTPSConnection(origin.hostname, timeout=HTTP_TIMEOUT)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(event).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        if response.status == 401:
            raise PermissionError("Home Assistant rejected account credentials")
        if response.status != 200:
            raise UpstreamError(f"Home Assistant HTTP status {response.status}")
        result = json.loads(response.read())
        if not isinstance(result, dict):
            raise UpstreamError("Home Assistant response must be a JSON object")
        return result
    finally:
        connection.close()


def _smart_error(
    event: dict[str, Any], error_type: str, message: str
) -> dict[str, Any]:
    directive = _object(event.get("directive"))
    header = _object(directive.get("header"))
    endpoint = _object(directive.get("endpoint"))
    response: dict[str, Any] = {
        "header": {
            "namespace": "Alexa",
            "name": "ErrorResponse",
            "payloadVersion": "3",
            "messageId": str(uuid4()),
        },
        "payload": {"type": error_type, "message": message},
    }
    if isinstance(header.get("correlationToken"), str):
        response["header"]["correlationToken"] = header["correlationToken"]
    if isinstance(endpoint.get("endpointId"), str):
        response["endpoint"] = {"endpointId": endpoint["endpointId"]}
    return {"event": response}


def smart_home(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Forward Discovery, control, ReportState, and AcceptGrant directives."""
    event = _object(event)
    directive = _object(event.get("directive"))
    header = _object(directive.get("header"))
    if header.get("payloadVersion") != "3":
        LOGGER.warning("Rejected malformed Smart Home directive")
        return _smart_error(event, "INVALID_DIRECTIVE", "Expected payload version 3")
    payload = _object(directive.get("payload"))
    scope = (
        _object(_object(directive.get("endpoint")).get("scope"))
        or _object(payload.get("grantee"))
        or _object(payload.get("scope"))
    )
    token = scope.get("token")
    if scope.get("type") != "BearerToken" or not isinstance(token, str) or not token:
        LOGGER.warning("Smart Home directive missing account credentials")
        return _smart_error(
            event, "INVALID_AUTHORIZATION_CREDENTIAL", "Link your Home Assistant account"
        )
    try:
        return _forward(event, token, "/api/alexa/smart_home")
    except PermissionError:
        LOGGER.warning("Smart Home account credentials rejected")
        return _smart_error(
            event, "INVALID_AUTHORIZATION_CREDENTIAL", "Relink your Home Assistant account"
        )
    except (OSError, http.client.HTTPException, UpstreamError, ValueError, KeyError):
        LOGGER.error("Smart Home forwarding failed; check endpoint configuration and availability")
        return _smart_error(event, "BRIDGE_UNREACHABLE", "Home Assistant is unavailable")


def _custom_error(
    event: dict[str, Any], message: str, link_account: bool = False
) -> dict[str, Any]:
    response: dict[str, Any] = {}
    if _object(event.get("request")).get("type") != "SessionEndedRequest":
        response = {
            "shouldEndSession": True,
            "outputSpeech": {"type": "PlainText", "text": message},
        }
        if link_account:
            response["card"] = {"type": "LinkAccount"}
    return {"version": "1.0", "response": response}


def custom(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Forward Custom skill requests, retaining slots and session context."""
    event = _object(event)
    session = _object(event.get("session"))
    system = _object(_object(event.get("context")).get("System"))
    application = _object(system.get("application")) or _object(session.get("application"))
    if application.get("applicationId") != os.environ.get("SKILL_ID") or not application:
        LOGGER.warning("Rejected Custom request from unexpected application")
        return _custom_error(event, "This skill is not configured correctly.")
    user = _object(system.get("user")) or _object(session.get("user"))
    token = user.get("accessToken")
    if not isinstance(token, str) or not token:
        LOGGER.warning("Custom request missing account credentials")
        return _custom_error(event, "Please link your Home Assistant account.", True)
    try:
        return _forward(event, token, "/api/alexa")
    except PermissionError:
        LOGGER.warning("Custom account credentials rejected")
        return _custom_error(event, "Please relink your Home Assistant account.", True)
    except (OSError, http.client.HTTPException, UpstreamError, ValueError, KeyError):
        LOGGER.error("Custom forwarding failed; check endpoint configuration and availability")
        return _custom_error(event, "Home Assistant is unavailable. Please try again later.")
