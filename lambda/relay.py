"""Single-home serverless command relay and locally approved OAuth linking."""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlencode

import boto3
from botocore.exceptions import ClientError


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
MAX_MESSAGE = 28_000
PARAMETERS = {}


def table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE"])


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def secret(name):
    cached = PARAMETERS.get(name)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    value = boto3.client("ssm").get_parameter(
        Name=os.environ[name], WithDecryption=True
    )["Parameter"]["Value"]
    PARAMETERS[name] = (time.monotonic(), value)
    return value


def get(key):
    item = table().get_item(Key={"pk": key}, ConsistentRead=True).get("Item")
    return plain(item) if item and item["expires"] > int(time.time()) else None


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def put(key, ttl, **fields):
    table().put_item(Item={"pk": key, "expires": int(time.time()) + ttl, **fields})


def scan(kind):
    result, options = [], {}
    while True:
        page = table().query(
            IndexName="pending",
            KeyConditionExpression="kind = :kind AND expires > :now",
            ExpressionAttributeValues={":kind": kind, ":now": int(time.time())}, **options
        )
        result.extend(
            item for item in page.get("Items", [])
            if item.get("kind") == kind and item["expires"] > int(time.time())
        )
        if "LastEvaluatedKey" not in page:
            return result
        options["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def send(connection, body):
    encoded = json.dumps(body).encode()
    if len(encoded) > MAX_MESSAGE:
        raise ValueError("Relay message exceeds 28 KB limit")
    boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["SOCKET_ENDPOINT"]).post_to_connection(
        ConnectionId=connection, Data=encoded
    )


def reply(status, body, content_type="application/json"):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        },
        "body": json.dumps(body) if content_type == "application/json" else body,
    }


def oauth_error(error, status=400):
    return reply(status, {"error": error})


def issue(client, grant):
    access = secrets.token_urlsafe(32)
    put("access#" + digest(access), 3600, client=client, grant=grant)
    return access


def http(event):
    route = event["routeKey"]
    params = event.get("queryStringParameters") or {}
    if len(json.dumps(params)) > 8192 or len(event.get("body") or "") > 8192:
        return oauth_error("invalid_request", 413)
    clients = json.loads(os.environ["CLIENT_IDS"])
    if route == "GET /authorize":
        client = params.get("client_id", "")
        redirect = params.get("redirect_uri", "")
        allowed = {
            f"https://{host}/api/skill/link/{os.environ['VENDOR_ID']}"
            for host in ("pitangui.amazon.com", "layla.amazon.com", "alexa.amazon.co.jp")
        }
        if (client not in clients or redirect not in allowed
                or params.get("response_type") != "code" or not params.get("state")):
            return oauth_error("invalid_request")
        nonce = secrets.token_urlsafe(32)
        display = secrets.token_hex(4).upper()
        put("link#" + digest(nonce), 300, kind="link", client=client, redirect=redirect,
            state=params["state"], display=display, approved=False)
        # The polling nonce is a browser secret, distinct from the displayed comparison code.
        page = """<!doctype html><meta charset="utf-8"><title>Link Home Assistant</title>
<h1>Approve in your local connector</h1>
<p>Open the connector's local approval page. Only approve if its code matches:</p>
<h2>DISPLAY</h2><p id="status">Waiting for approval (expires in five minutes).</p>
<script>
const timer = setInterval(async () => {
  try {
    const r = await fetch('/status?id=' + NONCE);
    const data = await r.json();
    if (data.redirect) { clearInterval(timer); location.replace(data.redirect); }
    if (!r.ok) { clearInterval(timer); document.getElementById('status').textContent = 'Expired. Start linking again.'; }
  } catch (_) { document.getElementById('status').textContent = 'Connection interrupted; retrying.'; }
}, 2000);
</script>"""
        return reply(200, page.replace("DISPLAY", display).replace("NONCE", json.dumps(nonce)),
                     "text/html; charset=utf-8")
    if route == "GET /status":
        link = get("link#" + digest(params.get("id", "")))
        if not link:
            return oauth_error("expired_token")
        if not link.get("approved"):
            return reply(200, {"pending": True})
        return reply(200, {"redirect": link["redirect"] + "?" + urlencode(
            {"state": link["state"], "code": link["code"]})})
    if route != "POST /token":
        return oauth_error("invalid_request", 404)
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw, validate=True).decode()
    form = parse_qs(raw, keep_blank_values=True, max_num_fields=12)
    if any(len(values) != 1 for values in form.values()):
        return oauth_error("invalid_request")
    form = {key: values[0] for key, values in form.items()}
    client = form.get("client_id", "")
    if client not in clients or not hmac.compare_digest(
        form.get("client_secret", ""), secret("OAUTH_PARAMETER")
    ):
        return oauth_error("invalid_client", 401)
    if form.get("grant_type") == "authorization_code":
        key = "code#" + digest(form.get("code", ""))
        code = get(key)
        if (not code or code.get("used") or code["client"] != client
                or form.get("redirect_uri") != code["redirect"]):
            return oauth_error("invalid_grant")
        try:
            table().update_item(
                Key={"pk": key}, UpdateExpression="SET used = :yes",
                ConditionExpression="attribute_not_exists(used) AND expires > :now",
                ExpressionAttributeValues={":yes": True, ":now": int(time.time())},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return oauth_error("invalid_grant")
        refresh = secrets.token_urlsafe(32)
        grant = secrets.token_hex(16)
        put("grant#" + grant, 90 * 86400, kind="grant", client=client)
        put("refresh#" + digest(refresh), 90 * 86400, client=client, grant=grant)
    elif form.get("grant_type") == "refresh_token":
        refresh = form.get("refresh_token", "")
        record = get("refresh#" + digest(refresh))
        if not record or record["client"] != client or not get("grant#" + record["grant"]):
            return oauth_error("invalid_grant")
        grant = record["grant"]
        for key in ("grant#" + grant, "refresh#" + digest(refresh)):
            try:
                table().update_item(
                    Key={"pk": key}, UpdateExpression="SET expires = :expires",
                    ConditionExpression="attribute_exists(pk) AND expires > :now",
                    ExpressionAttributeValues={":expires": int(time.time()) + 90 * 86400,
                                               ":now": int(time.time())},
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                return oauth_error("invalid_grant")
    else:
        return oauth_error("unsupported_grant_type")
    return reply(200, {"access_token": issue(client, grant), "refresh_token": refresh,
                       "token_type": "Bearer", "expires_in": 3600})


def socket(event):
    context = event["requestContext"]
    connection, route = context["connectionId"], context["routeKey"]
    if route == "$connect":
        headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
        if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + secret("CONNECTOR_PARAMETER")):
            return {"statusCode": 403}
        # A new connection supersedes the old one; old disconnect events cannot clear it.
        put("connection", 7200, connection=connection)
        return {"statusCode": 200}
    current = get("connection")
    if not current or current["connection"] != connection:
        return {"statusCode": 403}
    if route == "$disconnect":
        return {"statusCode": 200}
    if len((event.get("body") or "").encode()) > MAX_MESSAGE:
        return {"statusCode": 413}
    body = json.loads(event.get("body") or "{}")
    action = body.get("action")
    if action == "poll":
        links = [{"id": item["pk"], "code": item["display"], "client": item["client"]}
                 for item in scan("link") if not item.get("approved")]
        grants = [{"id": item["pk"], "client": item["client"]} for item in scan("grant")]
        send(connection, {"type": "links", "links": links, "grants": grants})
    elif action == "approve":
        key = body.get("id", "")
        link = get(key) if key.startswith("link#") else None
        if not link or link.get("approved"):
            return {"statusCode": 400}
        code = secrets.token_urlsafe(32)
        put("code#" + digest(code), 60, client=link["client"], redirect=link["redirect"])
        table().update_item(
            Key={"pk": key}, UpdateExpression="SET approved = :yes, code = :code",
            ConditionExpression="approved = :no AND expires > :now",
            ExpressionAttributeValues={":yes": True, ":no": False, ":code": code,
                                       ":now": int(time.time())},
        )
    elif action == "revoke":
        key = body.get("id", "")
        if not key.startswith("grant#"):
            return {"statusCode": 400}
        table().delete_item(Key={"pk": key})
    elif action == "response":
        request_id = body.get("id", "")
        request = get("request#" + request_id)
        if not request or request["connection"] != connection:
            return {"statusCode": 400}
        result = body.get("body")
        if not isinstance(result, dict):
            return {"statusCode": 413}
        table().update_item(
            Key={"pk": "request#" + request_id},
            UpdateExpression="SET #result = :result",
            ConditionExpression="attribute_not_exists(#result) AND expires > :now",
            ExpressionAttributeNames={"#result": "result"},
            ExpressionAttributeValues={":result": result, ":now": int(time.time())},
        )
    else:
        return {"statusCode": 400}
    return {"statusCode": 200}


def invoke(event):
    access = get("access#" + digest(event.get("token", "")))
    if (not access or access["client"] != event.get("client_id")
            or not get("grant#" + access["grant"])):
        return {"status": 401}
    path = event.get("path")
    if path not in ("/api/alexa", "/api/alexa/smart_home"):
        return {"status": 400}
    connection = get("connection")
    if not connection:
        return {"status": 503}
    payload = event["event"]
    directive = payload.get("directive", {})
    message_id = directive.get("header", {}).get("messageId") or payload.get("request", {}).get("requestId")
    request_id = (digest(event["client_id"] + ":" + message_id)[:32]
                  if isinstance(message_id, str) else secrets.token_hex(16))
    key = "request#" + request_id
    created = False
    try:
        table().put_item(
            Item={"pk": key, "expires": int(time.time()) + 60, "connection": connection["connection"]},
            ConditionExpression="attribute_not_exists(pk) OR expires < :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
        created = True
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    if created:
        send(connection["connection"], {"type": "request", "id": request_id,
                                        "path": path, "event": payload})
    deadline = time.monotonic() + 4.5
    while time.monotonic() < deadline:
        response = get(key)
        if response and "result" in response:
            return response["result"]
        time.sleep(0.15)
    return {"status": 504}


def handler(event, context):
    try:
        if event.get("operation") == "invoke" and "requestContext" not in event:
            return invoke(event)
        if "connectionId" in event.get("requestContext", {}):
            return socket(event)
        return http(event)
    except (ValueError, KeyError, TypeError):
        LOGGER.warning("Invalid relay request")
        return oauth_error("invalid_request")
    except ClientError as error:
        LOGGER.error("AWS relay operation failed (%s)", error.response["Error"]["Code"])
        if event.get("operation") == "invoke":
            return {"status": 503}
        return oauth_error("server_error", 503)
