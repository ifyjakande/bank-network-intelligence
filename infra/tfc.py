"""Terraform Cloud workspaces and variables for this repo, through the TFC API.

    python3 infra/tfc.py workspaces                  # create/update bni-bootstrap and bni-demo
    python3 infra/tfc.py connect-vcs                 # link bni-demo to the repo (main)
    python3 infra/tfc.py bootstrap-keys              # one-off AWS keys for the bootstrap run
    python3 infra/tfc.py drop-bootstrap-keys         # remove them once bootstrap is applied
    python3 infra/tfc.py live-vars ROLE_ARN          # OIDC role + inputs for bni-demo

Reads the TFC token from ~/.terraform.d/credentials.tfrc.json and everything else from
the environment (`set -a; . ./.env; set +a`): AWS keys for bootstrap-keys;
CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN and BUDGET_EMAIL for live-vars.
Idempotent; never prints a secret value.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ORG = "ifyjakande-sandbox"
API = "https://app.terraform.io/api/v2"
REPO = "ifyjakande/bank-network-intelligence"
TOKEN = json.loads((Path.home() / ".terraform.d/credentials.tfrc.json").read_text())["credentials"][
    "app.terraform.io"
]["token"]


def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/vnd.api+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        raise SystemExit(f"TFC {method} {path}: {e.code} {e.read()[:300]!r}") from None


def oauth_token_id() -> str:
    clients = call("GET", f"/organizations/{ORG}/oauth-clients")["data"]
    github = [c for c in clients if c["attributes"]["service-provider"].startswith("github")]
    if not github:
        raise SystemExit("no GitHub VCS connection in the organization")
    tokens = github[0]["relationships"]["oauth-tokens"]["data"]
    return str(tokens[0]["id"])


def upsert_workspace(name: str, attributes: dict[str, Any]) -> str:
    existing = call("GET", f"/organizations/{ORG}/workspaces/{name}")
    body = {"data": {"type": "workspaces", "attributes": {"name": name, **attributes}}}
    if existing:
        ws = call("PATCH", f"/workspaces/{existing['data']['id']}", body)
    else:
        ws = call("POST", f"/organizations/{ORG}/workspaces", body)
    print(f"workspace {name}: {ws['data']['id']}")
    return str(ws["data"]["id"])


def workspace_id(name: str) -> str:
    ws = call("GET", f"/organizations/{ORG}/workspaces/{name}")
    if not ws:
        raise SystemExit(f"workspace {name} does not exist; run `workspaces` first")
    return str(ws["data"]["id"])


def upsert_var(ws: str, key: str, value: str, category: str, sensitive: bool) -> None:
    existing = call("GET", f"/workspaces/{ws}/vars")["data"]
    match = next((v for v in existing if v["attributes"]["key"] == key), None)
    attrs = {"key": key, "value": value, "category": category, "sensitive": sensitive}
    if match:
        call(
            "PATCH",
            f"/workspaces/{ws}/vars/{match['id']}",
            {"data": {"type": "vars", "id": match["id"], "attributes": attrs}},
        )
    else:
        call("POST", f"/workspaces/{ws}/vars", {"data": {"type": "vars", "attributes": attrs}})
    print(f"  {category:9} {key} {'(sensitive)' if sensitive else '= ' + value}")


def delete_var(ws: str, key: str) -> None:
    for v in call("GET", f"/workspaces/{ws}/vars")["data"]:
        if v["attributes"]["key"] == key:
            call("DELETE", f"/workspaces/{ws}/vars/{v['id']}")
            print(f"  removed {key}")


def workspaces() -> None:
    upsert_workspace(
        "bni-bootstrap",
        {
            "description": "One-off: OIDC trust for bni-demo and GitHub Actions (infra/bootstrap)",
            "execution-mode": "remote",
            "auto-apply": False,
        },
    )
    upsert_workspace(
        "bni-demo",
        {
            "description": "Live demo (infra/live): plans on PRs, applies from main on approval",
            "execution-mode": "remote",
            "auto-apply": False,
            "speculative-enabled": True,
        },
    )


def connect_vcs() -> None:
    """Link bni-demo to the repo once main contains infra/live."""
    upsert_workspace(
        "bni-demo",
        {
            "working-directory": "infra/live",
            "file-triggers-enabled": True,
            "trigger-patterns": ["infra/live/**"],
            "vcs-repo": {"identifier": REPO, "branch": "main", "oauth-token-id": oauth_token_id()},
        },
    )


def bootstrap_keys() -> None:
    ws = workspace_id("bni-bootstrap")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(key):
            raise SystemExit(f"{key} is not set in the environment")
        upsert_var(ws, key, os.environ[key], "env", sensitive=True)
    upsert_var(ws, "AWS_DEFAULT_REGION", "us-east-1", "env", sensitive=False)


def drop_bootstrap_keys() -> None:
    ws = workspace_id("bni-bootstrap")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        delete_var(ws, key)


def live_vars(role_arn: str) -> None:
    ws = workspace_id("bni-demo")
    upsert_var(ws, "TFC_AWS_PROVIDER_AUTH", "true", "env", sensitive=False)
    upsert_var(ws, "TFC_AWS_RUN_ROLE_ARN", role_arn, "env", sensitive=False)
    upsert_var(
        ws,
        "cloudflare_account_id",
        os.environ["CLOUDFLARE_ACCOUNT_ID"],
        "terraform",
        sensitive=False,
    )
    upsert_var(ws, "budget_email", os.environ["BUDGET_EMAIL"], "terraform", sensitive=False)
    if os.environ.get("CLOUDFLARE_API_TOKEN"):
        upsert_var(
            ws, "CLOUDFLARE_API_TOKEN", os.environ["CLOUDFLARE_API_TOKEN"], "env", sensitive=True
        )
    else:
        print("  CLOUDFLARE_API_TOKEN not in the environment: add it as a sensitive env var")


if __name__ == "__main__":
    commands = {
        "workspaces": workspaces,
        "bootstrap-keys": bootstrap_keys,
        "drop-bootstrap-keys": drop_bootstrap_keys,
    }
    if len(sys.argv) == 3 and sys.argv[1] == "live-vars":
        live_vars(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] in commands:
        commands[sys.argv[1]]()
    else:
        raise SystemExit(__doc__)
