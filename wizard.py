"""Resumable Terraform + official ASK CLI provisioning. Run from this checkout."""

import argparse
import asyncio
import difflib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import boto3
import aiohttp
from botocore.exceptions import BotoCoreError, ClientError

from addon.ha_config import apply, atomic_write, generate


ROOT = Path(__file__).resolve().parent
LOCALES = {
    "en-US": "us-east-1", "en-CA": "us-east-1", "pt-BR": "us-east-1",
    "en-GB": "eu-west-1", "en-IN": "eu-west-1", "de-DE": "eu-west-1",
    "es-ES": "eu-west-1", "fr-FR": "eu-west-1", "it-IT": "eu-west-1",
    "ja-JP": "us-west-2", "en-AU": "us-west-2",
}
GATEWAYS = {
    "us-east-1": "https://api.amazonalexa.com/v3/events",
    "eu-west-1": "https://api.eu.amazonalexa.com/v3/events",
    "us-west-2": "https://api.fe.amazonalexa.com/v3/events",
}


def ask_executable():
    local = ROOT / "node_modules/.bin/ask"
    return str(local) if local.exists() else shutil.which("ask")


def local_ha_request(origin, method, path, token=None, form=None, body=None, timeout=30):
    parsed = urlsplit(origin)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("HA URL must be an HTTP(S) origin without credentials or a path")
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=timeout)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        payload = urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    else:
        payload = None
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Local HA request {path} returned HTTP {response.status}")
        return json.loads(response.read())
    finally:
        connection.close()


def supervisor_request(origin, method, path, token, body=None, timeout=300):
    parsed = urlsplit(origin)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("HA URL must be an HTTP(S) origin without credentials or a path")
    url = ("wss" if parsed.scheme == "https" else "ws") + "://" + parsed.netloc + "/api/websocket"

    async def request():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_receive=timeout)) as socket:
                if (await socket.receive_json()).get("type") != "auth_required":
                    raise RuntimeError("Unexpected HA WebSocket authentication response")
                await socket.send_json({"type": "auth", "access_token": token})
                if (await socket.receive_json()).get("type") != "auth_ok":
                    raise RuntimeError("HA rejected local authorization; authorize again")
                command = {"id": 1, "type": "supervisor/api", "endpoint": path,
                           "method": method.lower(), "timeout": timeout}
                if body is not None:
                    command["data"] = body
                await socket.send_json(command)
                while True:
                    result = await socket.receive_json()
                    if result.get("id") != 1:
                        continue
                    if result.get("type") != "result" or not result.get("success"):
                        code = result.get("error", {}).get("code", "unknown_error")
                        raise RuntimeError(f"Supervisor {method} {path} failed ({code}); "
                                           "use an HA administrator account and inspect Supervisor logs")
                    return result.get("result", {})

    try:
        return asyncio.run(request())
    except (aiohttp.ClientError, TimeoutError) as error:
        raise RuntimeError(f"Local Supervisor connection failed for {method} {path} "
                           f"({type(error).__name__}); check HA availability") from error


def open_browser(url, browser=None):
    if browser == "edge" and sys.platform == "darwin":
        result = subprocess.run(["open", "-a", "Microsoft Edge", url], check=False)
        if result.returncode:
            raise RuntimeError("Could not open Microsoft Edge; check that it is installed")
        return
    try:
        controller = webbrowser.get(browser) if browser else webbrowser
        opened = controller.open(url)
    except webbrowser.Error as error:
        raise RuntimeError(f"Could not find browser {browser!r}") from error
    if not opened:
        raise RuntimeError("Could not open browser; check your --browser setting")


def authorize_local_ha(origin, browser=None):
    """Use native HA browser consent rather than asking for a long-lived token."""
    state = secrets.token_urlsafe(32)
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            valid = (parsed.path == "/callback" and
                     hmac.compare_digest(params.get("state", [""])[0], state))
            if valid and params.get("code"):
                received["code"] = params["code"][0]
            self.send_response(200 if "code" in received else 400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to the setup program." if valid else b"Invalid callback.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        client_id = f"http://127.0.0.1:{server.server_port}/"
        server.timeout = 1
        query = urlencode({"client_id": client_id, "redirect_uri": client_id + "callback",
                           "state": state, "response_type": "code"})
        print("Authorize setup in the Home Assistant browser window (local access only).")
        open_browser(origin.rstrip("/") + "/auth/authorize?" + query, browser)
        deadline = time.monotonic() + 180
        while "code" not in received and time.monotonic() < deadline:
            server.handle_request()
        if "code" not in received:
            raise RuntimeError("HA browser authorization timed out")
    result = local_ha_request(origin, "POST", "/auth/token", form={
        "grant_type": "authorization_code", "code": received["code"], "client_id": client_id,
    })
    result["client_id"] = client_id
    return result


def confirm(message):
    if input(message + " [y/N] ").strip().lower() != "y":
        raise RuntimeError("Cancelled; saved setup progress is retained")


class CommandError(RuntimeError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def run(command, capture=False):
    result = subprocess.run(command, cwd=ROOT, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, check=False)
    if result.returncode:
        # ASK output and Terraform JSON can contain credentials; don't echo captured output.
        operation = " ".join(command[:3] if len(command) > 2 and command[1] == "smapi" else command[:2])
        match = re.search(r"Request failed with status code ([45][0-9]{2})\b",
                          (result.stderr or "") + "\n" + (result.stdout or ""))
        status = int(match[1]) if match else None
        detail = f", HTTP {status}" if status else ""
        raise CommandError(f"{operation} failed (exit {result.returncode}{detail}); "
                           "check authentication and configuration, then rerun setup", status)
    return result.stdout if capture else ""


def repository_url(value=None):
    value = value or run(["git", "remote", "get-url", "origin"], capture=True).strip()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    value = value.rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Provide --repository-url as a credential-free https://github.com/OWNER/REPO URL")
    return value


class Setup:
    def __init__(self, args):
        self.args = args
        self.directory = ROOT / ".setup"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / "progress.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        atomic_write(self.path, json.dumps(self.state, indent=2) + "\n")

    def ask(self, operation, *arguments):
        for attempt in range(4):
            try:
                text = run([ask_executable(), "smapi", operation, "--profile", self.args.ask_profile,
                            "--full-response", *arguments], capture=True)
                response = json.loads(text)
                status = response.get("statusCode")
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise CommandError(
                        f"Alexa CLI {operation} returned an unsuccessful or invalid response"
                        + (f" (HTTP {status})" if isinstance(status, int) else ""),
                        status if isinstance(status, int) else None,
                    )
                body = response.get("body")
                if body is not None and not isinstance(body, dict):
                    raise RuntimeError("Alexa CLI returned an unexpected response body")
                return body if body is not None else {}
            except CommandError as error:
                if (operation != "get-skill-status" or attempt == 3
                        or error.http_status not in (429, 500, 502, 503, 504)):
                    raise
                delay = 5 * 2 ** attempt
                print(f"Alexa status HTTP {error.http_status}; retrying in {delay} seconds.",
                      flush=True)
                time.sleep(delay)

    def document(self, name, value):
        path = self.directory / name
        atomic_write(path, json.dumps(value, indent=2) + "\n")
        return "file:" + str(path)

    def deploy(self):
        state = self.state
        variables = {
            "name": state["name"], "relay_enabled": True,
            "aws_account_id": state["aws_account"],
            "alexa_vendor_id": state["vendor_id"], "aws_region": state["region"],
            "smart_home_skill_id": state.get("smart_home"),
            "custom_skill_id": state.get("custom"),
        }
        path = self.directory / "relay.tfvars.json"
        atomic_write(path, json.dumps(variables, indent=2) + "\n")
        plan = self.directory / "deployment.tfplan"
        run(["terraform", "init", "-input=false"])
        run(["terraform", "plan", "-input=false", "-var-file=" + str(path), "-out=" + str(plan)])
        confirm("Apply this Terraform plan? AWS services are metered")
        run(["terraform", "apply", "-input=false", str(plan)])
        outputs = json.loads(run(["terraform", "output", "-json"], capture=True))
        return {key: value["value"] for key, value in outputs.items()}

    def parameter(self, ssm, name):
        try:
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except ClientError as error:
            if error.response["Error"]["Code"] != "ParameterNotFound":
                raise
        value = secrets.token_urlsafe(48)
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=False,
                          Description="ha-alexa setup credential; rotate with connector reprovisioning")
        return value

    def manifest(self, kind, endpoint=None):
        api = {"endpoint": {"uri": endpoint}} if endpoint else {}
        if kind == "smart_home":
            api["protocolVersion"] = "3"
        manifest = {"manifest": {
            "manifestVersion": "1.0",
            "publishingInformation": {
                "locales": {self.state["locale"]: {
                    "name": self.state["name"] + (" Control" if kind == "smart_home" else " Questions"),
                    "summary": "Private Home Assistant integration",
                    "description": "Personal self-managed Home Assistant integration.",
                    "examplePhrases": (
                        ["Alexa, turn on the kitchen light"] if kind == "smart_home" else
                        ["Alexa, ask home assistant for the home status"]
                    ),
                }},
                "isAvailableWorldwide": True,
                "category": "SMART_HOME", "testingInstructions": "Personal development skill.",
            },
            "privacyAndCompliance": {
                "allowsPurchases": False, "usesPersonalInfo": False,
                "isChildDirected": False, "isExportCompliant": True,
                "containsAds": False,
            },
            "apis": {"smartHome" if kind == "smart_home" else "custom": api},
        }}
        if kind == "smart_home":
            manifest["manifest"]["permissions"] = [{"name": "alexa::async_event:write"}]
        return manifest

    def wait(self, skill_id):
        print(f"Waiting for Alexa manifest: {skill_id}", flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = self.ask("get-skill-status", "--skill-id", skill_id)
            manifest = status.get("manifest", {}).get("lastUpdateRequest", {})
            if manifest.get("status") == "FAILED":
                raise RuntimeError("Amazon rejected the skill manifest; inspect the Alexa console")
            if manifest.get("status") == "SUCCEEDED":
                return
            time.sleep(10)
        raise RuntimeError("Amazon skill update still pending; rerun setup later")

    def provision(self):
        import re

        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", self.args.name):
            raise ValueError("Name must use 1-40 lowercase letters, digits or hyphens")
        if not shutil.which("terraform") or not ask_executable():
            raise RuntimeError("Install Terraform and run npm ci for the official Alexa CLI first")
        if not self.state:
            if (ROOT / "terraform.tfvars").exists():
                raise RuntimeError("Existing terraform.tfvars found. Use a separate checkout/state "
                                   "for relay setup; do not overwrite a direct deployment.")
            vendor_response = self.ask("get-vendor-list")
            vendors = vendor_response.get("vendors", [])
            if len(vendors) != 1:
                raise RuntimeError("Expected one Amazon vendor. Select a dedicated ASK profile "
                                   "with one vendor before continuing.")
            self.state = {"name": self.args.name, "locale": self.args.locale,
                          "region": LOCALES[self.args.locale], "vendor_id": vendors[0]["id"],
                          "custom_enabled": self.args.custom}
            self.save()
        elif (self.args.name != self.state["name"] or self.args.locale != self.state["locale"]
              or self.args.custom != self.state["custom_enabled"]):
            raise RuntimeError("Resume using the same --name, --locale and --custom options")
        session = boto3.Session(region_name=self.state["region"])
        account = session.client("sts").get_caller_identity()["Account"]
        if self.state.get("aws_account") not in (None, account):
            raise RuntimeError("AWS account differs from saved setup; refusing to change deployment")
        self.state["aws_account"] = account
        self.save()
        confirm(f"Provision private development skills and AWS relay in account {account}?")
        ssm = session.client("ssm")
        connector_key = self.parameter(ssm, f"/{self.state['name']}/connector-key")
        oauth_secret = self.parameter(ssm, f"/{self.state['name']}/oauth-secret")
        for kind in ("smart_home", "custom") if self.state["custom_enabled"] else ("smart_home",):
            if kind not in self.state:
                if self.state.get("creating"):
                    raise RuntimeError("Previous skill creation had an uncertain outcome. Check the "
                                       "Alexa console and use --adopt-skill-id to resume without duplicates.")
                self.state["creating"] = kind
                self.save()
                response = self.ask(
                    "create-skill-for-vendor",
                    "--manifest", self.document(kind + "-manifest.json",
                                                {"vendorId": self.state["vendor_id"],
                                                 **self.manifest(kind)}),
                )
                self.state[kind] = response["skillId"]
                self.state.pop("creating", None)
                self.save()
                self.wait(self.state[kind])
        outputs = self.deploy()
        for kind in ("smart_home", "custom") if self.state["custom_enabled"] else ("smart_home",):
            skill_id = self.state[kind]
            endpoint = outputs["smart_home_endpoint_arn" if kind == "smart_home" else "custom_endpoint_arn"]
            self.ask("update-skill-manifest", "--skill-id", skill_id, "--stage", "development",
                     "--manifest", self.document(kind + "-manifest.json", self.manifest(kind, endpoint)))
            self.wait(skill_id)
            linking = {"accountLinkingRequest": {
                "type": "AUTH_CODE", "skipOnEnablement": False,
                "authorizationUrl": outputs["account_linking"]["authorization_uri"],
                "accessTokenUrl": outputs["account_linking"]["access_token_uri"],
                "accessTokenScheme": "REQUEST_BODY_CREDENTIALS",
                "clientId": skill_id, "clientSecret": oauth_secret,
                "scopes": ["smart_home" if kind == "smart_home" else "intent"],
                "domains": [],
            }}
            path = self.document(kind + "-linking.json", linking)
            try:
                self.ask("update-account-linking-info", "--skill-id", skill_id,
                         "--stage", "development", "--account-linking-request", path)
            finally:
                Path(path.removeprefix("file:")).unlink(missing_ok=True)
            if kind == "custom":
                model = json.loads((ROOT / "examples/interaction-model.json").read_text())
                self.ask("set-interaction-model", "--skill-id", skill_id, "--stage", "development",
                         "--locale", self.state["locale"], "--interaction-model",
                         self.document("interaction-model.json", model))
                self.wait_model(skill_id)
        # Provisioning credentials are never stored in Terraform variables/state.
        connector_config = {
            "websocket_url": outputs["relay"]["websocket_url"],
            "connector_key": connector_key, "supervisor_ingress": False,
            "locale": self.state["locale"], "custom_skill_id": self.state.get("custom", ""),
        }
        credentials = self.ask("get-skill-credentials", "--skill-id", self.state["smart_home"])
        messaging = credentials["skillMessagingCredentials"]
        connector_config["event_credentials"] = {
            "endpoint": GATEWAYS[self.state["region"]],
            "client_id": messaging["clientId"], "client_secret": messaging["clientSecret"],
        }
        atomic_write(self.directory / "connector.json", json.dumps(connector_config, indent=2) + "\n")
        print("\nCloud deployment configured. No HA service has been exposed publicly.")
        print("HA OS: python wizard.py install-ha --ha-url http://homeassistant.local:8123")
        print("Container: python wizard.py configure-ha --config-dir /path/to/ha")
        print("Then: python wizard.py connector-config   (for Docker/standalone)")
        print("In Alexa, link the development skills; approve matching codes in the local connector UI.")
        open_browser("https://alexa.amazon.com", getattr(self.args, "browser", None))

    def wait_model(self, skill_id):
        print(f"Waiting for Alexa interaction model: {skill_id} (up to ten minutes).", flush=True)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = self.ask("get-skill-status", "--skill-id", skill_id)
            status = response.get("interactionModel", {}).get(self.state["locale"], {}).get(
                "lastUpdateRequest", {}).get("status")
            if status == "SUCCEEDED":
                return
            if status == "FAILED":
                raise RuntimeError("Alexa interaction model failed; inspect its console diagnostics")
            time.sleep(15)
        raise RuntimeError("Interaction model build pending; rerun setup later")

    def configure_ha(self):
        if not self.state.get("smart_home"):
            raise RuntimeError("Run provision first")
        directory = Path(self.args.config_dir).expanduser().resolve()
        entities = self.args.entities
        if not entities:
            entities = input("Entity IDs to expose, separated by commas: ").split(",")
        entities = [entity.strip() for entity in entities if entity.strip()]
        connector = json.loads((self.directory / "connector.json").read_text())
        credentials = connector.get("event_credentials")
        files = generate(directory, entities, self.state["locale"], self.state.get("custom"), credentials)
        for name, content in files.items():
            before = (directory / name).read_text() if (directory / name).exists() else ""
            diff = "".join(difflib.unified_diff(before.splitlines(True), content.splitlines(True),
                                              fromfile=name, tofile=name))
            if credentials:
                diff = diff.replace(credentials["client_secret"], "[redacted]")
            print(diff, end="")
        confirm("Back up existing files and write ONLY these changes?")
        stamp = apply(directory, files)
        print(f"Configuration written; backup suffix: .alexa-backup-{stamp}")
        print("Check configuration in HA Developer Tools > YAML before restarting.")
        print("The setup program does not restart HA without your approval.")

    def connector_config(self):
        path = self.directory / "connector.json"
        config = json.loads(path.read_text())
        config["ha_url"] = input("Local HA URL [http://homeassistant.local:8123]: ").strip() or "http://homeassistant.local:8123"
        parsed = urlsplit(config["ha_url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("Provide a local HTTP(S) origin")
        auth = authorize_local_ha(config["ha_url"], getattr(self.args, "browser", None))
        config["ha_token"] = auth["access_token"]
        config["ha_refresh_token"] = auth["refresh_token"]
        config["ha_client_id"] = auth["client_id"]
        atomic_write(path, json.dumps(config, indent=2) + "\n")
        print("Start: ha-alexa-connector --config .setup/connector.json --data-dir .setup/connector")
        print("Approval UI: http://127.0.0.1:8099 (never publish this port)")

    def install_ha(self):
        repository = repository_url(getattr(self.args, "repository_url", None))
        config = json.loads((self.directory / "connector.json").read_text())
        origin = self.args.ha_url
        auth = authorize_local_ha(origin, getattr(self.args, "browser", None))
        token = auth["access_token"]

        def request(method, path, body=None):
            return supervisor_request(origin, method, path, token=token, body=body)

        store = request("GET", "/store")
        if not any(item.get("source", "").rstrip("/") == repository for item in store.get("repositories", [])):
            confirm("Add this app repository to HA Supervisor?")
            request("POST", "/store/repositories", {"repository": repository})
            store = request("GET", "/store")
        candidates = [item for item in store.get("addons", [])
                      if item.get("slug", "").endswith("_alexa_connector")
                      and item.get("repository") in {
                          repo.get("slug") for repo in store.get("repositories", [])
                          if repo.get("source", "").rstrip("/") == repository}]
        if len(candidates) != 1:
            raise RuntimeError("Connector app not found in the store. This checkout must first be "
                               "published to the repository's default branch.")
        slug = candidates[0]["slug"]
        installed = request("GET", "/addons").get("addons", [])
        current = next((item for item in installed if item["slug"] == slug), None)
        if current is None:
            confirm("Install the connector app with local HA API and config-file access?")
            request("POST", f"/store/addons/{slug}/install", {})
        options = {key: config[key] for key in
                   ("websocket_url", "connector_key", "locale", "custom_skill_id", "event_credentials")}
        options["supervisor_ingress"] = True
        confirm("Configure and start the connector? Existing app options will be replaced")
        request("POST", f"/addons/{slug}/options", {"options": options})
        operation = "restart" if current and current.get("state") == "started" else "start"
        request("POST", f"/addons/{slug}/{operation}")
        open_browser(origin.rstrip("/") + "/config/app/" + slug + "/info",
                     getattr(self.args, "browser", None))
        print("Open the app Web UI to select entities, preview configuration, and approve linking.")

    def setup(self):
        self.provision()
        self.install_ha()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "provision", "configure-ha", "connector-config", "install-ha"])
    parser.add_argument("--name", default="ha-alexa")
    parser.add_argument("--locale", choices=sorted(LOCALES), default="en-US")
    parser.add_argument("--custom", action="store_true")
    parser.add_argument("--ask-profile", default="default")
    parser.add_argument("--browser", help="Browser for wizard pages; use edge for Microsoft Edge on macOS")
    parser.add_argument("--config-dir")
    parser.add_argument("--entities", nargs="+")
    parser.add_argument("--ha-url", default="http://homeassistant.local:8123")
    parser.add_argument("--repository-url", help="Public HA app repository; defaults to this checkout's Git origin")
    parser.add_argument("--adopt-skill-id", help="Recover a skill whose creation response was lost")
    args = parser.parse_args()
    if args.command == "configure-ha" and not args.config_dir:
        parser.error("configure-ha requires --config-dir")
    try:
        setup = Setup(args)
        if args.adopt_skill_id:
            import re

            if not setup.state.get("creating") or not re.fullmatch(
                r"amzn1\.ask\.skill\.[a-fA-F0-9-]+", args.adopt_skill_id
            ):
                raise ValueError("Adoption requires a pending creation and a valid skill ID")
            confirm(f"Adopt {args.adopt_skill_id} as the pending {setup.state['creating']} skill?")
            setup.state[setup.state.pop("creating")] = args.adopt_skill_id
            setup.save()
        getattr(setup, args.command.replace("-", "_"))()
    except (OSError, ValueError, KeyError, RuntimeError, BotoCoreError, ClientError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
