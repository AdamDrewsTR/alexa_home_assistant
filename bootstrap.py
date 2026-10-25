"""Standard-library entry point: python3 bootstrap.py [wizard options]."""

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(
        description="Install local tooling and run the HA OS setup wizard.",
        epilog="Other options are forwarded to wizard.py setup: --name, --locale, "
               "--custom, --ha-url, --browser, --repository-url, --adopt-skill-id. See README.md for details.",
    )
    parser.add_argument("--login", action="store_true", help="Authorize the official Alexa CLI")
    parser.add_argument("--ask-profile", default="default")
    parser.add_argument("--browser", help="Browser for wizard pages, e.g. edge on macOS (not ASK login)")
    args, forwarded = parser.parse_known_args()
    if not shutil.which("terraform"):
        sys.exit("Install Terraform >= 1.7 first: https://developer.hashicorp.com/terraform/install")
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.create(ROOT / ".venv", with_pip=True)
    result = subprocess.run([str(python), "-c", "import boto3, aiohttp, ruamel.yaml"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        subprocess.run([str(python), "-m", "pip", "install", "-e", str(ROOT)], check=True, cwd=ROOT)
    ask = ROOT / "node_modules/.bin/ask"
    if not ask.exists() and not shutil.which("ask"):
        if not shutil.which("npm"):
            sys.exit("Install Node.js/npm so setup can install Amazon's official ASK CLI")
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], check=True, cwd=ROOT)
    if args.login:
        subprocess.run([str(ask) if ask.exists() else "ask", "configure",
                        "--profile", args.ask_profile], check=True, cwd=ROOT)
    return subprocess.run([str(python), str(ROOT / "wizard.py"), "setup",
                           "--ask-profile", args.ask_profile,
                           *(["--browser", args.browser] if args.browser else []),
                           *forwarded], cwd=ROOT).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(f"Setup dependency/login command failed (exit {error.returncode}); rerun to resume")
