#!/usr/bin/env python3
"""Offline sanity checks for the stack's config files.

Checks:
  * every *.yml / *.yaml file parses (requires PyYAML: pip install pyyaml)
  * every dashboard *.json parses
  * every dashboard datasource UID exists in the provisioned datasources
  * every alert rule has expr, a severity label and a summary annotation
  * every bind-mounted path in docker-compose.yml exists in the repo

Exit code 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator, List

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
errors: List[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def walk(obj: Any) -> Iterator[Any]:
    """Yield every nested dict in a JSON/YAML structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def main() -> int:
    docs = {}
    for path in sorted(list(ROOT.rglob("*.yml")) + list(ROOT.rglob("*.yaml"))):
        rel = path.relative_to(ROOT)
        try:
            docs[str(rel)] = yaml.safe_load(path.read_text(encoding="utf-8"))
            print(f"ok   yaml  {rel}")
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: YAML error: {exc}")

    ds_file = docs.get("grafana/provisioning/datasources/datasources.yml") or {}
    ds_uids = {d.get("uid") for d in ds_file.get("datasources", [])}
    check(bool(ds_uids), "no provisioned datasources found")

    for path in sorted(ROOT.glob("grafana/dashboards/*.json")):
        rel = path.relative_to(ROOT)
        try:
            dash = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: JSON error: {exc}")
            continue
        print(f"ok   json  {rel}")
        check(bool(dash.get("uid")), f"{rel}: dashboard has no uid")
        for node in walk(dash):
            ds = node.get("datasource")
            if isinstance(ds, dict) and ds.get("type") == "loki":
                check(ds.get("uid") in ds_uids, f"{rel}: unknown datasource uid {ds.get('uid')!r}")

    for rel, doc in docs.items():
        if not rel.startswith("loki/rules/"):
            continue
        for group in (doc or {}).get("groups", []):
            for rule in group.get("rules", []):
                name = rule.get("alert") or rule.get("record")
                check(bool(rule.get("expr")), f"{rel}: rule {name} has no expr")
                if "alert" in rule:
                    check("severity" in rule.get("labels", {}), f"{rel}: alert {name} has no severity label")
                    check("summary" in rule.get("annotations", {}), f"{rel}: alert {name} has no summary")
        print(f"ok   rules {rel}")

    compose = docs.get("docker-compose.yml") or {}
    for svc_name, svc in compose.get("services", {}).items():
        for vol in svc.get("volumes", []):
            src = vol.split(":", 1)[0]
            if src.startswith("./"):
                check((ROOT / src).exists(), f"docker-compose.yml: {svc_name} mounts missing path {src}")
    print("ok   compose bind mounts")

    if errors:
        print("\nFAILED:", *errors, sep="\n  - ")
        return 1
    print("\nAll config checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
