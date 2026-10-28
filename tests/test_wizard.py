import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bootstrap
import wizard
from aiohttp import web
from aiohttp.test_utils import TestServer


class SupervisorTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_supervisor_websocket_and_explicit_failures(self):
        commands = []

        async def handler(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"type": "auth_required"})
            auth = await socket.receive_json()
            if auth.get("access_token") != "local-token":
                await socket.send_json({"type": "auth_invalid"})
                await socket.close()
                return socket
            await socket.send_json({"type": "auth_ok"})
            command = await socket.receive_json()
            commands.append(command)
            if command["endpoint"] == "/store":
                await socket.send_json({"id": command["id"], "type": "result", "success": True,
                                        "result": {"addons": [], "repositories": []}})
            else:
                await socket.send_json({"id": command["id"], "type": "result", "success": False,
                                        "error": {"code": "unauthorized", "message": "private detail"}})
            await socket.close()
            return socket

        app = web.Application()
        app.router.add_get("/api/websocket", handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        origin = str(server.make_url("/")).rstrip("/")
        result = await asyncio.to_thread(wizard.supervisor_request, origin, "GET", "/store", "local-token")
        self.assertEqual(result, {"addons": [], "repositories": []})
        self.assertEqual(commands[0], {"id": 1, "type": "supervisor/api", "endpoint": "/store",
                                       "method": "get", "timeout": 300})
        with self.assertRaisesRegex(RuntimeError, "authorize again"):
            await asyncio.to_thread(wizard.supervisor_request, origin, "GET", "/store", "invalid")
        with self.assertRaisesRegex(RuntimeError, "unauthorized") as stopped:
            await asyncio.to_thread(wizard.supervisor_request, origin, "POST", "/addons/test/start",
                                    "local-token", {"options": {}})
        self.assertNotIn("private detail", str(stopped.exception))
        self.assertEqual(commands[-1]["data"], {"options": {}})


class WizardTests(unittest.TestCase):
    def test_repository_uses_local_origin_or_explicit_override_without_credentials(self):
        with patch.object(wizard, "run", return_value="git@github.com:example/ha-alexa.git\n") as run:
            self.assertEqual(wizard.repository_url(), "https://github.com/example/ha-alexa")
            run.assert_called_once_with(["git", "remote", "get-url", "origin"], capture=True)
        with patch.object(wizard, "run") as run:
            self.assertEqual(wizard.repository_url("https://github.com/example/fork.git"),
                             "https://github.com/example/fork")
            run.assert_not_called()
        for url in ("https://token@github.com/example/fork", "https://github.com/example/fork?token=secret"):
            with self.assertRaises(ValueError):
                wizard.repository_url(url)

    def test_edge_on_macos_opens_urls_without_shell_interpolation(self):
        url = "http://ha.local/auth/authorize?state=example&client_id=local"
        with patch.object(wizard.sys, "platform", "darwin"), \
             patch.object(wizard.subprocess, "run", return_value=Mock(returncode=0)) as run:
            wizard.open_browser(url, "edge")
        run.assert_called_once_with(["open", "-a", "Microsoft Edge", url], check=False)
        with patch.object(wizard.sys, "platform", "darwin"), \
             patch.object(wizard.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "Microsoft Edge"):
                wizard.open_browser(url, "edge")

    def test_browser_selection_and_launch_failure(self):
        with patch.object(wizard.webbrowser, "get") as get:
            get.return_value.open.return_value = True
            wizard.open_browser("http://ha.local", "firefox")
            get.assert_called_once_with("firefox")
            get.return_value.open.assert_called_once_with("http://ha.local")
        with patch.object(wizard.webbrowser, "open", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Could not open browser"):
                wizard.open_browser("http://ha.local")

    def test_skill_manifests_use_native_apis_and_event_permission(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wizard, "ROOT", Path(directory)):
            setup = wizard.Setup(argparse.Namespace())
            setup.state = {"locale": "en-US", "name": "ha-alexa"}
            smart = setup.manifest("smart_home", "arn:aws:lambda:us-east-1:123456789012:function:test")
            self.assertEqual(smart["manifest"]["apis"]["smartHome"]["protocolVersion"], "3")
            self.assertEqual(smart["manifest"]["permissions"], [{"name": "alexa::async_event:write"}])
            self.assertIn("custom", setup.manifest("custom", "test")["manifest"]["apis"])

    def test_private_progress_is_atomic_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wizard, "ROOT", Path(directory)):
            setup = wizard.Setup(argparse.Namespace())
            setup.state = {"creating": "smart_home"}
            setup.save()
            self.assertEqual(wizard.Setup(argparse.Namespace()).state, setup.state)
            self.assertEqual(setup.path.stat().st_mode & 0o777, 0o600)

    def test_deploy_pins_saved_account_in_manual_terraform_variables(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wizard, "ROOT", Path(directory)), \
             patch.object(wizard, "confirm"), patch.object(wizard, "run") as run:
            setup = wizard.Setup(argparse.Namespace())
            setup.state = {"name": "ha-alexa", "aws_account": "123456789012",
                           "vendor_id": "vendor", "region": "us-east-1"}
            run.return_value = json.dumps({"relay": {"value": {"websocket_url": "wss://relay"}}})
            self.assertEqual(setup.deploy(), {"relay": {"websocket_url": "wss://relay"}})
            variables = json.loads((setup.directory / "relay.tfvars.json").read_text())
            self.assertEqual(variables["aws_account_id"], "123456789012")
            plan = next(call.args[0] for call in run.call_args_list
                        if call.args[0][:2] == ["terraform", "plan"])
            self.assertIn("-var-file=" + str(setup.directory / "relay.tfvars.json"), plan)

    def test_cloud_setup_uses_official_cli_not_a_terraform_shell_resource(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wizard, "ROOT", Path(directory)):
            setup = wizard.Setup(argparse.Namespace(ask_profile="personal"))
            with patch.object(wizard, "ask_executable", return_value="/tools/ask"), \
                 patch.object(wizard, "run", return_value=json.dumps({
                     "body": {"vendors": []}, "headers": {}, "statusCode": 200})) as run:
                self.assertEqual(setup.ask("get-vendor-list"), {"vendors": []})
            self.assertEqual(run.call_args.args[0],
                             ["/tools/ask", "smapi", "get-vendor-list", "--profile", "personal",
                              "--full-response"])
            with patch.object(wizard, "ask_executable", return_value="/tools/ask"), \
                 patch.object(wizard, "run", return_value=json.dumps({
                     "headers": {}, "statusCode": 204})):
                self.assertEqual(setup.ask("set-skill-enablement"), {})
            with patch.object(wizard, "ask_executable", return_value="/tools/ask"), \
                 patch.object(wizard, "run", return_value=json.dumps({"statusCode": 401})):
                with self.assertRaisesRegex(RuntimeError, "unsuccessful"):
                    setup.ask("get-vendor-list")

    def test_bootstrap_help_has_no_installation_or_login_side_effects(self):
        with patch("sys.argv", ["bootstrap.py", "--login", "--help"]), \
             patch.object(bootstrap.subprocess, "run") as run, \
             patch.object(bootstrap.venv, "create") as create:
            with self.assertRaises(SystemExit) as stopped:
                bootstrap.main()
            self.assertEqual(stopped.exception.code, 0)
            run.assert_not_called()
            create.assert_not_called()

    def test_failed_smapi_command_names_operation_without_leaking_output(self):
        result = Mock(returncode=1, stdout="private response",
                      stderr="private credentials: Request failed with status code 429")
        with patch.object(wizard.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as stopped:
                wizard.run(["ask", "smapi", "update-account-linking-info",
                            "--account-linking-request", "file:private.json"], capture=True)
        self.assertIn("ask smapi update-account-linking-info", str(stopped.exception))
        self.assertIn("HTTP 429", str(stopped.exception))
        self.assertNotIn("private", str(stopped.exception))

    def test_status_reads_retry_only_transient_http_errors_with_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wizard, "ROOT", Path(directory)), \
             patch.object(wizard, "ask_executable", return_value="/tools/ask"), \
             patch.object(wizard.time, "sleep") as sleep:
            setup = wizard.Setup(argparse.Namespace(ask_profile="default"))
            success = json.dumps({"statusCode": 200, "body": {"manifest": {}}})
            with patch.object(wizard, "run", side_effect=[
                wizard.CommandError("throttled", 429), success
            ]) as run:
                self.assertEqual(setup.ask("get-skill-status"), {"manifest": {}})
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once_with(5)
            sleep.reset_mock()
            with patch.object(wizard, "run", side_effect=wizard.CommandError("server error", 503)) as run:
                with self.assertRaises(wizard.CommandError):
                    setup.ask("get-skill-status")
                self.assertEqual(run.call_count, 4)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10, 20])
            for operation, status in (("get-skill-status", 403), ("get-skill-status", None),
                                      ("create-skill-for-vendor", 503)):
                with self.subTest(operation=operation, status=status), \
                     patch.object(wizard, "run", side_effect=wizard.CommandError("failed", status)) as run:
                    with self.assertRaises(wizard.CommandError):
                        setup.ask(operation)
                    run.assert_called_once()

    def test_installer_installs_or_restarts_and_keeps_ha_token_local(self):
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as directory, \
                 patch.object(wizard, "ROOT", Path(directory)), \
                 patch.object(wizard, "confirm"), patch.object(wizard.webbrowser, "open"), \
                 patch.object(wizard, "authorize_local_ha", return_value={"access_token": "local-auth"}):
                setup = wizard.Setup(argparse.Namespace(
                    ha_url="http://ha.local:8123", repository_url="https://github.com/example/ha-alexa"))
                config = {"websocket_url": "wss://relay.example/connector", "connector_key": "private",
                          "locale": "en-US", "custom_skill_id": "", "event_credentials": {
                              "endpoint": "https://api.amazonalexa.com/v3/events",
                              "client_id": "messaging-client", "client_secret": "messaging-secret"}}
                setup.document("connector.json", config)
                slug = "repository_alexa_connector"
                repository = "https://github.com/example/ha-alexa"

                def request(origin, method, path, **kwargs):
                    self.assertEqual(kwargs["token"], "local-auth")
                    if path == "/store":
                        return {
                            "repositories": [{"slug": "repository", "source": repository}],
                            "addons": [{"slug": slug, "repository": "repository"}]}
                    if path == "/addons":
                        return {"addons": [
                            {"slug": slug, "state": "started"}] if running else []}
                    return {}

                with patch.object(wizard, "supervisor_request", side_effect=request) as send, \
                     patch.object(wizard, "open_browser") as browser:
                    setup.install_ha()
                browser.assert_called_once_with(
                    f"http://ha.local:8123/config/app/{slug}/info", None)
                paths = [call.args[2] for call in send.call_args_list]
                self.assertEqual(f"/store/addons/{slug}/install" in paths, not running)
                self.assertEqual(paths[-1], f"/addons/{slug}/" + ("restart" if running else "start"))
                options = next(call.kwargs["body"]["options"] for call in send.call_args_list
                               if call.args[2].endswith("/options"))
                self.assertTrue(options["supervisor_ingress"])
                self.assertNotIn("local-auth", json.dumps(options))

    def test_provision_creates_once_and_binds_linking_to_actual_skill(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(wizard, "ROOT", Path(directory)), \
             patch.object(wizard, "confirm"), patch.object(wizard.webbrowser, "open"), \
             patch.object(wizard.shutil, "which", return_value="/tools/terraform"), \
             patch.object(wizard, "ask_executable", return_value="/tools/ask"), \
             patch.object(wizard.boto3, "Session") as session:
            session.return_value.client.return_value.get_caller_identity.return_value = {"Account": "123"}
            setup = wizard.Setup(argparse.Namespace(name="ha-alexa", locale="en-US", custom=False))
            setup.parameter = Mock(side_effect=lambda ssm, name: "connector-key" if "connector" in name else "oauth-secret")
            setup.deploy = Mock(return_value={
                "relay": {"function_arn": "arn:relay", "websocket_url": "wss://relay.example/connector"},
                "smart_home_endpoint_arn": "arn:smart",
                "account_linking": {"authorization_uri": "https://relay.example/authorize",
                                   "access_token_uri": "https://relay.example/token"}})
            skill = "amzn1.ask.skill.created"
            creations, links = [], []

            def ask(operation, *arguments):
                if operation == "get-vendor-list":
                    return {"vendors": [{"id": "vendor"}]}
                if operation == "create-skill-for-vendor":
                    creations.append(json.loads(Path(arguments[1].removeprefix("file:")).read_text()))
                    return {"skillId": skill}
                if operation == "get-skill-status":
                    return {"manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}}}
                if operation == "update-account-linking-info":
                    links.append(json.loads(Path(arguments[-1].removeprefix("file:")).read_text()))
                if operation == "get-skill-credentials":
                    return {"skillMessagingCredentials": {"clientId": "events", "clientSecret": "secret"}}
                return {}

            setup.ask = Mock(side_effect=ask)
            setup.provision()
            setup.provision()
            self.assertNotIn("set-skill-enablement",
                             [call.args[0] for call in setup.ask.call_args_list])
            self.assertEqual(len(creations), 1)
            self.assertEqual(creations[0]["vendorId"], "vendor")
            self.assertNotIn("endpoint", creations[0]["manifest"]["apis"]["smartHome"])
            self.assertEqual(links[0]["accountLinkingRequest"]["clientId"], skill)
            self.assertFalse((setup.directory / "smart_home-linking.json").exists())
            self.assertNotIn("oauth-secret", setup.path.read_text())
            setup.state.pop("smart_home")
            setup.state["creating"] = "smart_home"
            with self.assertRaisesRegex(RuntimeError, "uncertain outcome"):
                setup.provision()
            self.assertEqual(len(creations), 1)
