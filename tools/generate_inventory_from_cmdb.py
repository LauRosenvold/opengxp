#!/usr/bin/env python3
"""Regenerate an inventories/<env>/hosts.yml from ServiceNow CMDB.

This does NOT set up a live/dynamic inventory plugin. It queries the CMDB
once, renders a normal static hosts.yml from tools/templates/hosts.yml.j2,
and writes it to disk — the output is exactly the same kind of
git-versioned, PR-reviewed file this repo already uses (see
docs/CHANGE_CONTROL.md), just generated instead of hand-maintained. Run it
whenever the CMDB changes, review `git diff`, and send the result through
the normal PR flow like any other inventory change. It never touches git
itself.

Field names and value mappings are entirely site-specific and live in
tools/cmdb_mapping.yml (see that file's comments) — this script refuses to
run while any CHANGEME placeholder remains in the mapping.

Credentials come from environment variables, never from a file:
    SN_INSTANCE   e.g. "mycompany" for https://mycompany.service-now.com
    SN_USERNAME
    SN_PASSWORD

Usage:
    python tools/generate_inventory_from_cmdb.py --env staging
    python tools/generate_inventory_from_cmdb.py --env production --dry-run
    python tools/generate_inventory_from_cmdb.py --env validation \\
        --input-json fixtures/cmdb_export.json   # offline/testing, skips the API call

Requires: requests, PyYAML, Jinja2 (pip install -r tools/requirements.txt)
on the control node running this script — not a target-host dependency,
same category as pyvmomi/pywinrm/proxmoxer in requirements.yml.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fixed group vocabulary this repo's playbooks/group_vars actually key
# on (see inventories/production/hosts.yml's header/footer comments). A
# CMDB value that doesn't resolve to one of these is a hard error, not a
# silently-dropped value — an unrecognized group name is far more likely
# to be a typo or a stale CMDB tag than a group nobody's told this script
# about yet.
KNOWN_GROUPS = {
    "rhel9", "rhel10", "almalinux9", "almalinux10",
    "gxp_critical", "gxp_standard",
    "webservers", "appservers", "dbservers",
    "mssql_hosts", "etcd_hosts", "file_share_hosts", "docker_hosts",
    "k8s_control_plane", "k8s_workers", "k8s_etcd",
    "netbox_hosts", "registry_hosts", "nginx_hosts",
    "hyperv_hosts", "dns_servers", "ca_servers",
}

# Hosts tagged into any of these are Windows/WinRM-only and exempt from
# the "exactly one OS group + exactly one GxP tier" requirement — mirrors
# the documented exception in inventories/production/hosts.yml.
WINDOWS_EXEMPT_GROUPS = {"hyperv_hosts", "dns_servers", "ca_servers"}

# Groups where host ORDER is load-bearing (first host = kubeadm init /
# etcd CA generation target) — see the footer note in
# inventories/production/hosts.yml.
ORDERED_GROUPS = {"k8s_control_plane", "k8s_etcd", "etcd_hosts"}

# Groups that (per hosts.yml's own comments) should always also carry
# docker_hosts — checked as a warning, not a hard error, since it's a
# documented convention rather than something anything else in the repo
# enforces mechanically.
DOCKER_LAYERED_GROUPS = {"netbox_hosts", "registry_hosts", "nginx_hosts"}

PREAMBLE = {
    "production": (
        "# Production estate inventory, regenerated from ServiceNow CMDB."
    ),
    "staging": (
        "# Pre-production qualification environment, regenerated from\n"
        "# ServiceNow CMDB. Must mirror production's group structure\n"
        "# exactly (rhel9/rhel10/almalinux9/almalinux10 x\n"
        "# gxp_critical/gxp_standard x webservers/appservers/dbservers)\n"
        "# even if populated with far fewer hosts — playbooks and\n"
        "# group_vars keys are shared verbatim with production."
    ),
    "validation": (
        "# IQ/OQ/PQ execution environment, regenerated from ServiceNow\n"
        "# CMDB. See docs/VALIDATION.md. Typically a small, fixed set of\n"
        "# hosts (one per OS/tier combination is often enough) kept\n"
        "# intentionally stable so validation evidence stays comparable\n"
        "# run over run — re-run this generator deliberately, not as a\n"
        "# matter of routine."
    ),
}

FOOTER = (
    "# This file is regenerated wholesale on every run of\n"
    "# tools/generate_inventory_from_cmdb.py — it does not merge with\n"
    "# whatever was here before. If a host is missing, fix it in the\n"
    "# CMDB (or tools/cmdb_mapping.yml if it's a mapping problem), not\n"
    "# here."
)


class MappingError(Exception):
    pass


def load_mapping(path: Path) -> dict:
    mapping = yaml.safe_load(path.read_text(encoding="utf-8"))

    def _walk(value, where):
        if isinstance(value, str) and value.startswith("CHANGEME"):
            raise MappingError(
                f"{path}: {where} is still '{value}' — fill in the actual "
                f"ServiceNow field/value name before running this script. "
                f"See the comments in {path.name}."
            )
        if isinstance(value, dict):
            for k, v in value.items():
                _walk(v, f"{where}.{k}")

    _walk(mapping, "mapping")

    if mapping.get("table", "").strip() == "":
        raise MappingError(f"{path}: 'table' must not be empty")

    return mapping


def fetch_cmdb_records(mapping: dict, env: str) -> list[dict]:
    instance = os.environ.get("SN_INSTANCE")
    username = os.environ.get("SN_USERNAME")
    password = os.environ.get("SN_PASSWORD")
    if not all([instance, username, password]):
        raise SystemExit(
            "SN_INSTANCE, SN_USERNAME, and SN_PASSWORD must all be set in "
            "the environment (never pass credentials on the command line "
            "or store them in tools/cmdb_mapping.yml)."
        )

    query = mapping["environments"].get(env)
    if not query:
        raise MappingError(
            f"tools/cmdb_mapping.yml has no 'environments.{env}' query filter"
        )

    fields = mapping["fields"]
    wanted_fields = [
        fields["hostname"], fields["os_group"], fields["gxp_tier"],
        fields["ansible_groups"], fields["order"],
    ]

    url = f"https://{instance}.service-now.com/api/now/table/{mapping['table']}"
    records: list[dict] = []
    offset = 0
    limit = 200
    while True:
        resp = requests.get(
            url,
            auth=(username, password),
            headers={"Accept": "application/json"},
            params={
                "sysparm_query": query,
                "sysparm_fields": ",".join(wanted_fields),
                "sysparm_limit": limit,
                "sysparm_offset": offset,
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json().get("result", [])
        records.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    return records


class Host:
    __slots__ = ("name", "os_group", "gxp_tier", "groups", "order")

    def __init__(self, name, os_group, gxp_tier, groups, order):
        self.name = name
        self.os_group = os_group
        self.gxp_tier = gxp_tier
        self.groups = groups
        self.order = order


def normalize_records(records: list[dict], mapping: dict) -> list[Host]:
    f = mapping["fields"]
    delim = mapping["delimiter"]
    os_values = mapping["os_group_values"]
    tier_values = mapping["gxp_tier_values"]

    hosts: list[Host] = []
    seen: set[str] = set()
    errors: list[str] = []

    for rec in records:
        name = (rec.get(f["hostname"]) or "").strip()
        if not name:
            errors.append(f"record with sys_id={rec.get('sys_id', '?')} has no hostname")
            continue
        if name in seen:
            errors.append(f"duplicate hostname '{name}' returned by CMDB query")
            continue
        seen.add(name)

        raw_groups = (rec.get(f["ansible_groups"]) or "").strip()
        groups = {g.strip() for g in raw_groups.split(delim) if g.strip()}
        unknown = groups - KNOWN_GROUPS
        if unknown:
            errors.append(
                f"{name}: unrecognized group(s) {sorted(unknown)} in "
                f"'{f['ansible_groups']}' — add to KNOWN_GROUPS in this "
                f"script (and the matching group_vars file) if this is a "
                f"real new group, otherwise fix the CMDB record"
            )
            continue

        windows_exempt = bool(groups & WINDOWS_EXEMPT_GROUPS)

        os_group = None
        raw_os = (rec.get(f["os_group"]) or "").strip()
        if raw_os:
            os_group = os_values.get(raw_os)
            if os_group is None:
                errors.append(
                    f"{name}: os_group value '{raw_os}' has no entry in "
                    f"os_group_values in tools/cmdb_mapping.yml"
                )
                continue
        elif not windows_exempt:
            errors.append(
                f"{name}: no os_group and not tagged into a Windows-exempt "
                f"group ({sorted(WINDOWS_EXEMPT_GROUPS)})"
            )
            continue

        gxp_tier = None
        raw_tier = (rec.get(f["gxp_tier"]) or "").strip()
        if raw_tier:
            gxp_tier = tier_values.get(raw_tier)
            if gxp_tier is None:
                errors.append(
                    f"{name}: gxp_tier value '{raw_tier}' has no entry in "
                    f"gxp_tier_values in tools/cmdb_mapping.yml"
                )
                continue
        elif not windows_exempt:
            errors.append(
                f"{name}: no gxp_tier and not tagged into a Windows-exempt "
                f"group ({sorted(WINDOWS_EXEMPT_GROUPS)})"
            )
            continue

        raw_order = rec.get(f["order"])
        order = None
        if raw_order not in (None, ""):
            try:
                order = int(raw_order)
            except (TypeError, ValueError):
                errors.append(f"{name}: order value '{raw_order}' is not an integer")
                continue

        if os_group:
            groups.add(os_group)
        if gxp_tier:
            groups.add(gxp_tier)

        for layered in groups & DOCKER_LAYERED_GROUPS:
            if "docker_hosts" not in groups:
                print(
                    f"warning: {name} is in {layered} but not docker_hosts "
                    f"— hosts.yml's own convention says it should be (see "
                    f"the {layered} comment block)",
                    file=sys.stderr,
                )

        if (groups & ORDERED_GROUPS) and order is None:
            print(
                f"warning: {name} is in an order-sensitive group "
                f"({sorted(groups & ORDERED_GROUPS)}) but has no order "
                f"value — falling back to alphabetical, which may put the "
                f"wrong host first for kubeadm init / etcd CA generation. "
                f"Fix the order field in the CMDB or place this host by "
                f"hand instead.",
                file=sys.stderr,
            )

        hosts.append(Host(name, os_group, gxp_tier, groups, order))

    if errors:
        raise SystemExit(
            "Refusing to generate an inventory from CMDB data with "
            f"{len(errors)} problem(s):\n  - " + "\n  - ".join(errors)
        )

    return hosts


def build_group_membership(hosts: list[Host]) -> dict[str, list[str]]:
    membership: dict[str, list[Host]] = {g: [] for g in KNOWN_GROUPS}
    for host in hosts:
        for group in host.groups:
            membership[group].append(host)

    result: dict[str, list[str]] = {}
    for group, members in membership.items():
        if group in ORDERED_GROUPS:
            members.sort(key=lambda h: (h.order is None, h.order, h.name))
        else:
            members.sort(key=lambda h: h.name)
        result[group] = [h.name for h in members]
    return result


def render_hosts_factory(default_indent: int):
    def render_hosts(hostnames: list[str], ordered: bool = False, indent: int | None = None) -> str:
        pad = " " * (indent if indent is not None else default_indent)
        if not hostnames:
            return f"{pad}{{}}"
        return "\n".join(f"{pad}{name}:" for name in hostnames)

    return render_hosts


def render(env_name: str, groups: dict[str, list[str]], source_query: str) -> str:
    jinja_env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "tools" / "templates")),
        trim_blocks=False,
        keep_trailing_newline=True,
    )
    jinja_env.globals["render_hosts"] = render_hosts_factory(8)
    template = jinja_env.get_template("hosts.yml.j2")
    return template.render(
        env=env_name,
        groups=groups,
        preamble=PREAMBLE[env_name],
        footer=FOOTER,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_query=source_query,
    )


def write_atomic(path: Path, content: str) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".hosts.yml.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=["production", "staging", "validation"])
    parser.add_argument("--mapping", default=str(REPO_ROOT / "tools" / "cmdb_mapping.yml"))
    parser.add_argument("--output", default=None, help="default: inventories/<env>/hosts.yml")
    parser.add_argument("--input-json", default=None, help="load CMDB records from a local JSON file instead of calling the API (offline/testing)")
    parser.add_argument("--dry-run", action="store_true", help="print the diff against the current file, write nothing")
    args = parser.parse_args()

    try:
        mapping = load_mapping(Path(args.mapping))

        if args.input_json:
            records = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
            source_query = f"(offline: {args.input_json})"
        else:
            records = fetch_cmdb_records(mapping, args.env)
            source_query = mapping["environments"][args.env]

        hosts = normalize_records(records, mapping)
    except MappingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    groups = build_group_membership(hosts)
    output_text = render(args.env, groups, source_query)

    output_path = Path(args.output) if args.output else REPO_ROOT / "inventories" / args.env / "hosts.yml"

    if args.dry_run:
        existing = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        diff = difflib.unified_diff(
            existing.splitlines(keepends=True),
            output_text.splitlines(keepends=True),
            fromfile=str(output_path),
            tofile=f"{output_path} (generated)",
        )
        sys.stdout.writelines(diff)
        return 0

    write_atomic(output_path, output_text)
    print(f"wrote {output_path} ({len(hosts)} hosts across {sum(1 for v in groups.values() if v)} non-empty groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
