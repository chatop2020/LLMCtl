#!/usr/bin/env python3
"""Manage LLMCtl workflow routes and independently hosted resource pools.

The data plane never discovers Docker by itself.  This helper can import the
legacy local Worker layout, but every resulting target is an ordinary URL and
can be replaced with a remote vLLM, image, audio, video, or adapter endpoint.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pathlib
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def load(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"configuration does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON at line {error.lineno}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


def atomic_write(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def normalized_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an http(s) origin/path without credentials")
    return value


def normalized_env_name(value: str, *, optional: bool = False) -> str:
    value = value.strip()
    if optional and not value:
        return ""
    if not ENV_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "environment name must contain uppercase letters, digits and underscores"
        )
    return value


def normalized_listen(value: str) -> str:
    value = value.strip()
    try:
        parsed = urllib.parse.urlsplit(f"tcp://{value}")
        port = parsed.port
    except ValueError as error:
        raise ValueError("listen must use host:port syntax with a valid port") from error
    if not parsed.hostname or port is None or not 1 <= port <= 65535:
        raise ValueError("listen must use host:port syntax with a valid port")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}"


def initial_config(args: argparse.Namespace) -> dict[str, Any]:
    listen = normalized_listen(args.listen)
    api_key_env = normalized_env_name(args.api_key_env)
    shared_secret_env = normalized_env_name(args.shared_secret_env)
    targets: list[dict[str, str]] = []
    if args.local_worker_ids:
        for raw_id in args.local_worker_ids.split(","):
            worker_id = int(raw_id.strip())
            targets.append(
                {
                    "id": f"local-worker-{worker_id}",
                    "base_url": f"http://127.0.0.1:{args.worker_base_port + worker_id}/v1",
                    "api_key_env": api_key_env,
                }
            )
    for index, raw_url in enumerate(args.target):
        targets.append(
            {
                "id": f"remote-worker-{index}",
                "base_url": normalized_base_url(raw_url),
                "api_key_env": api_key_env,
            }
        )
    if not targets:
        raise ValueError("init requires --local-worker-ids or at least one --target")
    return {
        "version": 1,
        "listen": listen,
        "gateway_base_url": normalized_base_url(
            args.gateway_base_url
            or f"http://127.0.0.1:{listen.rsplit(':', 1)[-1]}/v1"
        ),
        "shared_secret_env": shared_secret_env,
        "request_body_limit_bytes": args.request_body_limit_bytes,
        "upstream_timeout_ms": args.upstream_timeout_ms,
        "models": {
            args.route_model: {
                "enabled": False,
                "mode": "transparent",
                "base_model": args.base_model,
                "pool": args.pool,
                "tools": [],
                "max_tool_rounds": 4,
                "system_prompt": "",
            }
        },
        "pools": {
            args.pool: {
                "strategy": "p2c-least-inflight",
                "targets": targets,
            }
        },
        "adapters": {},
    }


def command_init(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.config)
    if path.exists() and not args.force:
        raise ValueError(f"configuration already exists: {path}; use --force to replace")
    value = initial_config(args)
    atomic_write(path, value)
    print(json.dumps({"config": str(path), "targets": len(value["pools"][args.pool]["targets"])}))


def command_show(args: argparse.Namespace) -> None:
    print(json.dumps(load(pathlib.Path(args.config)), ensure_ascii=False, indent=2, sort_keys=True))


def command_target_add(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.config)
    value = load(path)
    pools = value.setdefault("pools", {})
    pool = pools.setdefault(args.pool, {"strategy": "p2c-least-inflight", "targets": []})
    targets = pool.setdefault("targets", [])
    replacement = {
        "id": args.id,
        "base_url": normalized_base_url(args.base_url),
        "api_key_env": normalized_env_name(args.api_key_env),
    }
    targets[:] = [target for target in targets if str(target.get("id", "")) != args.id]
    targets.append(replacement)
    atomic_write(path, value)
    print(json.dumps(replacement))


def command_target_remove(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.config)
    value = load(path)
    try:
        targets = value["pools"][args.pool]["targets"]
    except (KeyError, TypeError) as error:
        raise ValueError(f"pool does not exist: {args.pool}") from error
    kept = [target for target in targets if str(target.get("id", "")) != args.id]
    if len(kept) == len(targets):
        raise ValueError(f"target does not exist: {args.id}")
    if not kept:
        raise ValueError("refusing to leave a pool without targets")
    value["pools"][args.pool]["targets"] = kept
    atomic_write(path, value)
    print(json.dumps({"removed": args.id}))


def request_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4 << 20)
    except urllib.error.HTTPError as error:
        detail = error.read(1000).decode("utf-8", errors="replace")
        raise ValueError(f"endpoint returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ValueError(f"endpoint connection failed: {error.reason}") from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("endpoint returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("endpoint returned a non-object response")
    return value


def command_discover(args: argparse.Namespace) -> None:
    base_url = normalized_base_url(args.base_url)
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise ValueError(f"environment is empty: {args.api_key_env}")
    result = request_json(f"{base_url}/models", api_key, args.timeout)
    models = result.get("data", [])
    if not isinstance(models, list):
        raise ValueError("/models response does not contain a data array")
    ids = sorted(
        {
            str(item.get("id", "")).strip()
            for item in models
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
    )
    if args.expected_model and args.expected_model not in ids:
        raise ValueError(f"expected model not advertised: {args.expected_model}")
    print(json.dumps({"base_url": base_url, "models": ids}, ensure_ascii=False))


def command_check_targets(args: argparse.Namespace) -> None:
    value = load(pathlib.Path(args.config))
    expected_by_pool: dict[str, set[str]] = {}
    for route in value.get("models", {}).values():
        if not isinstance(route, dict) or not route.get("enabled"):
            continue
        expected_by_pool.setdefault(str(route.get("pool", "")), set()).add(
            str(route.get("base_model", ""))
        )
    results: list[dict[str, Any]] = []
    for pool_id, expected in expected_by_pool.items():
        try:
            targets = value["pools"][pool_id]["targets"]
        except (KeyError, TypeError) as error:
            raise ValueError(f"pool does not exist: {pool_id}") from error
        for target in targets:
            base_url = normalized_base_url(str(target.get("base_url", "")))
            env_name = str(target.get("api_key_env", ""))
            api_key = os.environ.get(env_name, "")
            if not api_key:
                raise ValueError(f"environment is empty: {env_name}")
            response = request_json(f"{base_url}/models", api_key, args.timeout)
            models = response.get("data", [])
            advertised = {
                str(item.get("id", "")).strip()
                for item in models
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            }
            missing = sorted(model for model in expected if model not in advertised)
            if missing:
                raise ValueError(
                    f"target {target.get('id', '')} does not advertise: {','.join(missing)}"
                )
            results.append(
                {
                    "pool": pool_id,
                    "target": str(target.get("id", "")),
                    "base_url": base_url,
                    "models": sorted(expected),
                    "status": "ok",
                }
            )
    if not results:
        raise ValueError("no enabled model routes to check")
    print(json.dumps({"targets": results}, ensure_ascii=False))


def command_model_set(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.config)
    value = load(path)
    if args.pool not in value.get("pools", {}):
        raise ValueError(f"pool does not exist: {args.pool}")
    tools = [item.strip() for item in args.tools.split(",") if item.strip()]
    missing = sorted(set(tools) - set(value.get("adapters", {})))
    if missing:
        raise ValueError(f"missing adapters: {','.join(missing)}")
    value.setdefault("models", {})[args.route_model] = {
        "enabled": args.enabled,
        "mode": args.mode,
        "base_model": args.base_model,
        "pool": args.pool,
        "tools": tools,
        "max_tool_rounds": args.max_tool_rounds,
        "system_prompt": args.system_prompt,
    }
    atomic_write(path, value)
    print(json.dumps({"model": args.route_model, "enabled": args.enabled, "mode": args.mode}))


def command_model_enable(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.config)
    value = load(path)
    try:
        route = value["models"][args.route_model]
    except (KeyError, TypeError) as error:
        raise ValueError(f"model route does not exist: {args.route_model}") from error
    route["enabled"] = args.enabled
    atomic_write(path, value)
    print(json.dumps({"model": args.route_model, "enabled": args.enabled}))


def command_adapter_set(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.config)
    value = load(path)
    try:
        definition = json.loads(pathlib.Path(args.tool_definition).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read tool definition: {error}") from error
    value.setdefault("adapters", {})[args.id] = {
        "kind": "http-json",
        "endpoint": normalized_base_url(args.endpoint),
        "secret_env": normalized_env_name(args.secret_env, optional=True),
        "timeout_ms": args.timeout_ms,
        "result_max_bytes": args.result_max_bytes,
        "tool_definition": definition,
        "allowed_purposes": [item.strip() for item in args.allowed_purposes.split(",") if item.strip()],
    }
    atomic_write(path, value)
    print(json.dumps({"adapter": args.id}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/var/lib/llm-cluster/workflow/workflow.json")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--listen", required=True)
    init.add_argument(
        "--gateway-base-url",
        default="",
        help="URL that OmniRoute can use to reach this data plane, ending in /v1",
    )
    init.add_argument("--route-model", default="llmctl-workflow-gdn-inside")
    init.add_argument("--base-model", required=True)
    init.add_argument("--pool", default="text-generation")
    init.add_argument("--local-worker-ids", default="")
    init.add_argument("--worker-base-port", type=int, default=8100)
    init.add_argument("--target", action="append", default=[])
    init.add_argument("--api-key-env", default="BACKEND_API_KEY")
    init.add_argument("--shared-secret-env", default="LLM_WORKFLOW_SHARED_SECRET")
    init.add_argument("--request-body-limit-bytes", type=int, default=32 << 20)
    init.add_argument("--upstream-timeout-ms", type=int, default=7_200_000)
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)

    show = commands.add_parser("show")
    show.set_defaults(handler=command_show)

    target_add = commands.add_parser("target-add")
    target_add.add_argument("--pool", required=True)
    target_add.add_argument("--id", required=True)
    target_add.add_argument("--base-url", required=True)
    target_add.add_argument("--api-key-env", default="BACKEND_API_KEY")
    target_add.set_defaults(handler=command_target_add)

    target_remove = commands.add_parser("target-remove")
    target_remove.add_argument("--pool", required=True)
    target_remove.add_argument("--id", required=True)
    target_remove.set_defaults(handler=command_target_remove)

    discover = commands.add_parser("discover")
    discover.add_argument("--base-url", required=True)
    discover.add_argument("--api-key-env", default="BACKEND_API_KEY")
    discover.add_argument("--expected-model", default="")
    discover.add_argument("--timeout", type=float, default=10)
    discover.set_defaults(handler=command_discover)

    check_targets = commands.add_parser("check-targets")
    check_targets.add_argument("--timeout", type=float, default=10)
    check_targets.set_defaults(handler=command_check_targets)

    model = commands.add_parser("model-set")
    model.add_argument("--route-model", required=True)
    model.add_argument("--base-model", required=True)
    model.add_argument("--pool", required=True)
    model.add_argument("--mode", choices=("transparent", "agent"), default="transparent")
    model.add_argument("--tools", default="")
    model.add_argument("--max-tool-rounds", type=int, default=4)
    model.add_argument("--system-prompt", default="")
    model.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    model.set_defaults(handler=command_model_set)

    model_enable = commands.add_parser("model-enable")
    model_enable.add_argument("--route-model", required=True)
    model_enable.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, default=True
    )
    model_enable.set_defaults(handler=command_model_enable)

    adapter = commands.add_parser("adapter-set")
    adapter.add_argument("--id", required=True)
    adapter.add_argument("--endpoint", required=True)
    adapter.add_argument("--secret-env", default="")
    adapter.add_argument("--timeout-ms", type=int, default=60000)
    adapter.add_argument("--result-max-bytes", type=int, default=4 << 20)
    adapter.add_argument("--tool-definition", required=True)
    adapter.add_argument("--allowed-purposes", default="")
    adapter.set_defaults(handler=command_adapter_set)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except (ValueError, TypeError, KeyError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
