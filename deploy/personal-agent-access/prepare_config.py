"""Prepare a private candidate; never edits or reloads the running gateway."""
import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import secrets
from urllib.parse import urlparse

SAFE_TOOLS = {"enva_corpus_read", "enva_scm_sql_read"}


def prepare(base, origin, password):
    target = urlparse(origin)
    if target.scheme != "https" or not target.netloc or target.username or target.password or target.path not in ("", "/") or target.query or target.fragment:
        raise ValueError("A single HTTPS bridge origin is required")
    if len(password.encode()) < 32:
        raise ValueError("A separate maintenance password of at least 32 bytes is required")
    if set(base.get("tools", {}).get("allow", [])) != SAFE_TOOLS or base.get("tools", {}).get("elevated", {}).get("enabled") is not False:
        raise ValueError("The approved read-only tool policy must already exist")
    if "scm-personal" not in base.get("agents", {}).get("entries", {}):
        raise ValueError("The existing approved SCM agent template is required")
    template = base["agents"]["entries"]["scm-personal"]
    for field in ("workspace", "agentDir"):
        value = template.get(field)
        if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value:
            raise ValueError("The approved template requires canonical absolute workspace and agentDir paths")
    default_model = base.get("agents", {}).get("defaults", {}).get("model")
    if template.get("model") is not None and default_model is not None and template["model"] != default_model:
        raise ValueError("The approved template and default models diverge")
    config = copy.deepcopy(base)
    gateway = config.setdefault("gateway", {})
    gateway.update(bind="loopback", publicOrigin=origin.rstrip("/"), trustedProxies=["127.0.0.1", "::1"])
    gateway["reload"] = {**gateway.get("reload", {}), "mode": "hybrid"}
    # Source config remains intact. A shared browser token is never retained.
    gateway["auth"] = {
        "mode": "trusted-proxy", "password": password,
        "identityScopes": {},
        "trustedProxy": {
            "userHeader": "x-scm-openclaw-user",
            "requiredHeaders": ["x-forwarded-proto", "x-forwarded-host"],
            "allowLoopback": True,
            "deviceAutoApprove": {"enabled": True, "scopes": ["operator.sessions.read", "operator.sessions.write"]},
        },
    }
    gateway["roles"] = {"default": "deny_all", "definitions": {
        "deny_all": {"sessions": {"others": "none"}, "agents": [], "scopes": []},
    }}
    gateway["controlUi"] = {**gateway.get("controlUi", {}), "allowedOrigins": [origin.rstrip("/")]}
    config["agents"]["ownership"] = "explicit"
    return config


def write_private(path, content):
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if not args.out_dir.is_absolute():
        parser.error("--out-dir must be a protected absolute directory outside Git")
    config = json.loads(args.base.read_text(encoding="utf-8"))
    password = secrets.token_urlsafe(48)
    candidate = prepare(config, args.origin, password)
    args.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation: no accidental replacement of an existing release.
    write_private(args.out_dir / "openclaw.json", json.dumps(candidate, indent=2) + "\n")
    write_private(args.out_dir / "maintenance-password", password + "\n")
    print("Private candidate prepared. Running services and source config were not changed.")


if __name__ == "__main__":
    main()
