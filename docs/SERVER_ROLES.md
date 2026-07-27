# `server_roles/` — Workload Enablement, Separate From the Compliance Baseline

## Why a second top-level directory instead of `roles/`

`roles/` is the security/compliance baseline: CIS hardening, audit,
SSH/SELinux/firewall posture, the accounts that can touch a host at all.
Change control for it is deliberately heavy — see
[CHANGE_CONTROL.md](CHANGE_CONTROL.md) — because it's what your validation
package cites and a mistake there is fleet-wide and security-relevant.

`server_roles/` is workload enablement: turning an already-hardened host
into a *specific kind* of server (a Docker host, a Kubernetes node, a
PostgreSQL server, and whatever comes next — a web tier, a message
queue). This content changes on its own, faster cadence (a new Docker
version, a new Kubernetes minor version, a new PostgreSQL major version,
a daemon.json tweak for a new workload requirement) that would otherwise
dilute the hardening repo's review gate if mixed in. Keeping them apart
means:

- Reviewers for `roles/` changes don't have to also reason about
  workload-specific tradeoffs unrelated to the security baseline.
- A `server_roles/` change doesn't need the second security-owner
  approval `docs/CHANGE_CONTROL.md` requires for `roles/cis_hardening`,
  `roles/auditd_gxp`, etc. — unless it touches something that also lives
  in that list.
- Nothing in `server_roles/` runs as part of `playbooks/site.yml`. Each
  gets its own playbook (`playbooks/docker_host.yml`, ...), run
  deliberately against its own inventory group (`docker_hosts`, ...),
  after `site.yml` has already put the host into its baseline state.

`ansible.cfg`'s `roles_path = roles:server_roles:apps` means all three
directories are searched by name — `server_roles/docker_host` and
`roles/common` are both just `docker_host` / `common` in a playbook's
`roles:` list. The separation is about change-control and review
boundaries, not ergonomics.

If this grows to cover genuinely different products/teams (a database
team's roles, an app platform team's roles), reach for a second Ansible
repo per the earlier conversation about this — `server_roles/` is the
right amount of separation for "workload roles maintained by the same
team, on a faster cadence," not for "a different team owns this
entirely."

There's a third, further tier below this one: **[APPS.md](APPS.md)**
covers `apps/` — specific applications (NetBox, and whatever comes next)
deployed *on top of* a `server_roles/` platform, e.g. `apps/netbox`
running on a `server_roles/docker_host`. Same reasoning as this file,
one step further down.

## `docker_host`

Turns a hardened RHEL9/RHEL10/AlmaLinux10 host into a generic Docker
workload host. Run via `playbooks/docker_host.yml` against the
`docker_hosts` inventory group (present, empty, in all three
environments — populate with real hostnames the same way as the OS
groups).

### Things that are NOT obvious and will bite you if skipped

- **Published container ports bypass `firewalld_baseline`.** Docker
  inserts its own `ACCEPT` rules into the `DOCKER-USER` chain ahead of
  firewalld's zone rules, for anything a container publishes with `-p`.
  The default-deny zone `roles/firewalld_baseline` sets up does **not**
  protect those ports. `docker_host` warns about this on every run until
  you set `docker_restrict_published_ports: true` and
  `docker_allowed_source_cidrs` in `group_vars/docker_hosts.yml` — which
  deploys a systemd drop-in that re-locks down `DOCKER-USER` to your
  allow-list after every `docker.service` start (Docker recreates that
  chain on every start, so a one-time `iptables` rule doesn't survive a
  restart).

- **IP forwarding is fleet-wide disabled by the hardening baseline, and
  Docker cannot work without it.** `docker_host` forces
  `net.ipv4.ip_forward=1` itself regardless of what the baseline set, so
  it's never silently broken — but `group_vars/docker_hosts.yml` also
  carries the "proper" per-tier override
  (`el10_baseline_ip_forward_enabled: true` for RHEL10/AlmaLinux10, a
  best-effort `rhel9cis_rule_3_1_1: false` for RHEL9 that you should
  verify against your installed `ansible-lockdown.RHEL9-CIS` version).
  Without that override, a later `hardening.yml` re-run (it's designed to
  be safe to schedule — see `playbooks/hardening.yml`) could flip RHEL9's
  forwarding back off until `docker_host` next runs and re-asserts it.

- **`/var/lib/docker` is deliberately excluded from AIDE.** Image layers
  and container filesystems change constantly; watching that path would
  drown `roles/aide_fim`'s file-integrity checks in false positives.
  `/etc/docker` (daemon config, TLS material) is watched instead.

- **Docker group membership is root-equivalent.** Nobody is added to the
  `docker` group by this role, including the automation account — Ansible
  already runs Docker-management tasks via `become` (root), the same as
  every other privileged task in this repo. If a deploy pipeline needs
  non-root `docker` CLI access, adding an account to that group needs the
  same change-control rigor as a `sudo` grant in `roles/user_access_gxp`,
  because functionally it *is* one.

- **Docker CE isn't Satellite content by default.** `docker_repo_baseurl`
  defaults to `CHANGEME`, which falls back to Docker's public repo
  (`download.docker.com`) — fine for evaluation, but it's the one package
  family on the fleet not flowing through Satellite (see
  `roles/satellite_registration`) until you sync Docker CE into a
  Satellite custom product/content view and point `docker_repo_baseurl`/
  `docker_repo_gpgkey` at it.

### What's intentionally out of scope

- **Rootless Docker.** A real option for higher-isolation workloads, but
  enough additional complexity (per-user `newuidmap`/`newgidmap`,
  `systemd --user` services) that it didn't fit "do your best" for a
  generic default. Worth a dedicated role if a workload specifically
  needs it.
- **Kubernetes/container orchestration.** This is a single-host Docker
  Engine role, not a cluster — see `server_roles/kubernetes_node` below
  if that's what you actually need. The two roles are independent (a
  Kubernetes node does NOT run this role first); don't combine
  `docker_hosts` and `k8s_nodes` group membership on the same host.
- **Application-level roles** (the actual containers/compose files a
  workload runs). This role stops at "Docker is installed, hardened, and
  ready" — deploying specific workloads onto it is a different role (or
  a different repo, depending on how many workload teams are involved).

## `kubernetes_node`

Vanilla (kubeadm) Kubernetes — not a managed distribution, not OpenShift,
not k3s. Turns hardened hosts into a cluster's control-plane and worker
nodes. Run via `playbooks/kubernetes_cluster.yml` against the
`k8s_control_plane` and `k8s_workers` inventory groups (present, empty,
in all three environments). Upgrades are a separate, explicitly-gated
playbook: `playbooks/kubernetes_upgrade.yml`.

Container runtime and CNI are both pluggable, set in
`group_vars/k8s_nodes.yml`:

| Variable                 | Options                     | Default      |
|---------------------------|------------------------------|--------------|
| `k8s_container_runtime`   | `containerd`, `crio`          | `containerd` |
| `k8s_cni_provider`        | `calico`, `flannel`           | `calico`     |
| `k8s_metallb_enabled`     | `true`, `false`               | `false`      |

None of these are hardening recommendations one way or the other — pick
based on what your org already knows how to operate.

### "Multiple versions" support, concretely

Kubernetes' own package repos (`pkgs.k8s.io`) are versioned per **minor**
version — there is no floating "latest" repo, by design upstream. This
role's `k8s_version` var (set in `group_vars/k8s_nodes.yml`, the whole
cluster should agree on one version outside of a deliberate upgrade)
picks which minor-version repo a node group uses. Two consequences worth
understanding before you touch this:

- **Different node pools can genuinely run different minor versions** —
  set `k8s_version` in `host_vars` for a specific subset of workers if
  you're validating a new minor version on a canary pool before rolling
  it out further. Kubernetes' own version-skew policy (kubelet up to a
  couple of minors behind the API server, roughly — check the current
  policy for the versions you're actually running) still applies; this
  role doesn't enforce that policy for you.
- **Routine patching won't silently move you between minor versions.**
  `roles/patch_management` runs a fleet-wide `dnf update *`; this role
  applies `dnf versionlock` to `kubeadm`/`kubelet`/`kubectl` specifically
  so that never touches them — a version change only happens through
  `playbooks/kubernetes_upgrade.yml`, deliberately, one version at a
  time, with `k8s_upgrade_confirmed=true` required (same gate pattern as
  `roles/fips_mode`).

### Node setup: control-plane vs. worker

Behavior branches on inventory group membership
(`'k8s_control_plane' in group_names`), not a separate role variable —
one thing to keep in sync, not two:

- The **first host** in `k8s_control_plane` (list order in `hosts.yml`
  matters here in a way it doesn't for any other group in this repo — see
  the comment there) runs `kubeadm init`. Every other control-plane host
  joins with `--control-plane`, using a certificate key and join token
  regenerated fresh on every run (never a persisted, potentially-stale
  secret sitting in this repo) and shared via Ansible `hostvars` — see
  `tasks/control_plane_init.yml`.
- Worker nodes join with the same regenerated token.
- The whole sequence (prep every node -> init the first control-plane
  node -> join everyone else -> install the CNI) runs as **one
  Ansible play with no `serial:`**, relying entirely on the linear
  strategy's per-task synchronization across hosts for correct ordering.
  See `tasks/main.yml`'s header comment before changing that structure.

### Things that are NOT obvious and will bite you if skipped

- **SELinux stays enforcing regardless of which runtime you pick.**
  Unlike a lot of Kubernetes-on-RHEL tutorials that reach for
  `setenforce 0`, this role does not touch `roles/selinux_enforce`'s
  baseline. Both runtimes have `enable_selinux`/`selinux = true` set
  explicitly in their config (`templates/containerd_config.toml.j2`,
  `templates/crio.conf.j2`) — CRI-O in particular has especially mature
  SELinux integration, since it's the runtime OpenShift itself uses. If a
  workload hits a real SELinux denial, the fix is correct labeling or a
  targeted policy module — not disabling enforcement on a GxP-regulated
  host.
- **Switching container runtime after a cluster already exists isn't
  handled.** `k8s_container_runtime` picks the runtime a *new* node is
  provisioned with; changing it on an existing node means kubelet's
  `--container-runtime-endpoint` and every already-running container
  disagree about who owns what. If you need to migrate a live cluster
  from containerd to CRI-O (or back), that's a node-by-node
  drain/reprovision/rejoin exercise this role doesn't automate — treat it
  like the upgrade path's caution, but more so.
- **An HA control plane needs a load balancer this role does not
  provide.** `k8s_control_plane_endpoint` must point at a stable
  VIP/DNS name in front of every control-plane node before you put more
  than one host in that group — an HAProxy+keepalived pair, an F5, a
  cloud LB, whatever your org already runs for this. The role asserts
  loudly and refuses to join a second control-plane node if this is
  still empty, rather than silently building an inconsistent cluster.
- **kubeadm's own IP-forwarding/bridge-netfilter requirements clash with
  the CIS baseline the same way Docker's do** — see the equivalent note
  under `docker_host` above. `group_vars/k8s_nodes.yml` carries the same
  `el10_baseline_ip_forward_enabled` / best-effort `rhel9cis_rule_3_1_1`
  override pattern, and the role re-asserts the sysctls itself regardless.
- **CNI is not optional, and the pod CIDR patch applied to the fetched
  manifest is best-effort for Calico specifically.** Calico
  (`NetworkPolicy` support, broad enterprise familiarity) and Flannel
  (simpler VXLAN overlay, no `NetworkPolicy` enforcement) are both
  version-pinned in `k8s_cni_manifest_url_by_provider` — bump deliberately,
  never float on `master`/`latest`. `tasks/cni.yml` fetches whichever
  manifest is selected and rewrites its pod-CIDR value to match
  `k8s_pod_network_cidr` before applying: Flannel's is a clean string
  substitution (its ConfigMap ships the value uncommented), Calico's
  requires uncommenting a specific block in the manifest and is verified
  against the exact pinned tag at authoring time — if you bump that tag,
  the task logs whether the patch actually matched, check that output.
  Set `k8s_cni_enabled: false` and apply your own if your org
  standardizes on Cilium or something else — this role's job stops at
  "there is a working pod network," not "here is the policy engine you
  specifically need."
- **MetalLB gives you `LoadBalancer`-type Services on bare metal, L2 mode
  only.** Off by default (`k8s_metallb_enabled: false`) because it needs
  real IP ranges from your network (`k8s_metallb_address_pools`) this role
  can't guess — enabling it with an empty pool list fails loudly rather
  than silently doing nothing. Only L2 (ARP/NDP) advertisement is wired
  up, chosen because it needs no BGP router configuration and works on
  any flat L2 network — the common on-prem case. BGP mode is a documented
  gap (see "out of scope" below), not a limitation of MetalLB itself.
  MetalLB's speaker pods need memberlist gossip traffic between nodes
  (`7946/tcp` and `7946/udp` — already in `group_vars/k8s_control_plane.yml`
  / `k8s_workers.yml`, remove if you don't enable this).
- **API server audit logging and etcd Secrets encryption are both
  GxP-relevant and both need attention, not just acceptance of defaults.**
  `templates/audit-policy.yaml.j2` is a reasonable starting policy (full
  request/response logging for RBAC and workload mutations, metadata-only
  for Secrets so values never land in a plaintext log) — review it against
  your own risk assessment. `k8s_encryption_at_rest_enabled` is off by
  default because it needs a real key; turn it on (with a vaulted key,
  never plaintext) for any cluster storing GxP-relevant data in Secrets.
- **Kubernetes/container-runtime content isn't Satellite content by
  default**, same caveat and same fix as `docker_host`'s Docker CE repo —
  applies to `k8s_repo_baseurl`, `containerd_repo_baseurl`, and
  `crio_repo_baseurl` alike, whichever runtime you've selected.
- **The automation account gets a `~/.kube/config` symlink to
  `/etc/kubernetes/admin.conf`** on control-plane nodes, for `kubectl`
  convenience during troubleshooting via `become`. The underlying file
  stays root-only (`0600`) — the symlink doesn't loosen that, it just
  saves typing `--kubeconfig` every time as root.

### What's intentionally out of scope

- **The HA load balancer** in front of a multi-control-plane cluster (see
  above) — bring your own.
- **MetalLB BGP mode.** L2 advertisement only, as noted above — BGP mode
  (peering with real routers, useful for larger/multi-rack deployments
  where L2 adjacency doesn't reach) needs router-side configuration this
  role has no way to know, and isn't wired up.
- **Ingress controllers, service meshes, cluster autoscaling, storage
  classes/CSI drivers, monitoring stacks.** All application/platform
  layer, not "a Kubernetes cluster exists and is reasonably hardened,"
  which is where this role's responsibility ends. (MetalLB is the one
  exception let in, because "how do I expose a Service at all on bare
  metal" is close enough to core cluster function to matter for a
  "generic workloads" cluster, unlike an Ingress controller's routing
  policy which is genuinely workload-specific.)
- **Multi-cluster federation, etcd running external to the control-plane
  nodes (stacked etcd only), and non-kubeadm installation methods**
  (kOps, Cluster API, managed cloud Kubernetes). All valid approaches,
  none of them "vanilla kubeadm," which is specifically what was asked for.

## `postgresql_server`

PostgreSQL on PGDG packages (not the OS's built-in AppStream module —
that's disabled explicitly since it conflicts). Run via
`playbooks/postgresql_server.yml` against the `dbservers` inventory
group, which already existed as a functional group for
`roles/firewalld_baseline` purposes before this role did; it's now
populated with real PostgreSQL vars instead of a placeholder port.

### Node setup: standalone vs. primary vs. replica

Unlike `k8s_control_plane`/`k8s_workers`, `dbservers` is a flat group
that can hold several **unrelated** PostgreSQL instances — there's no
"first host" convention here. Set `postgresql_role` per host in
`host_vars`, not in `group_vars/dbservers.yml`:

- `standalone` (default): no replication, `initdb` on first run.
- `primary`: same `initdb` path, plus creates a `REPLICATION` role
  (`postgresql_replication_user`/`_password`, vaulted) once the server is
  running.
- `replica`: skips `initdb` entirely — its data directory comes from
  `pg_basebackup -R` against `postgresql_primary_host`, which also writes
  `standby.signal` and `primary_conninfo` for you. `pg_hba.conf` on the
  primary is generated with replication entries for every replica that
  names it via `postgresql_primary_host`, discovered automatically
  through Ansible `hostvars` — no manual IP bookkeeping, same pattern
  `kubernetes_node` uses for its join tokens.

### Things that are NOT obvious and will bite you if skipped

- **This is basic streaming replication, not high availability.** There
  is no automatic failover, no Patroni/repmgr/pgpool, and promoting a
  replica to primary is a manual `pg_ctl promote` (or equivalent) you run
  yourself — this role doesn't attempt to detect a failed primary or
  react to one. If your GxP system needs automated failover, that's a
  deliberate, separate design exercise on top of what's here, not
  something to assume exists.
- **WAL archiving is a mechanism, not a backup solution.**
  `postgresql_wal_archiving_enabled` (off by default) copies WAL segments
  to a local directory — no offsite copy, no retention policy, no restore
  tooling. `tasks/backup.yml` warns loudly either way (on: "this alone
  isn't a strategy"; off: "you have no PITR mechanism at all, flag it for
  your validation package"). Point `archive_command` at pgBackRest,
  Barman, or whatever your org actually uses before trusting this with
  real data.
- **pgAudit's `log_parameter` setting is a deliberate privacy/traceability
  trade-off, on by default.** It logs actual bound values, not just query
  shape — without it, your audit trail shows *that* a row changed but not
  *to what*, which is usually not good enough to reconstruct a GxP
  record's history. The cost: parameter values (which might include
  sensitive data) land in the PostgreSQL log file, which then flows
  through whatever `roles/logging_forward` is configured to do. Review
  this against your own data classification before accepting the default.
- **`pgaudit.log` classes deliberately exclude `read`** (bare `SELECT`
  logging) — enabling it is usually enough audit volume to bury the
  signal you actually want. Add it back in
  `postgresql_pgaudit_log_classes` if your risk assessment specifically
  needs read-access logging.
- **pgAudit is enabled in `template1` and `postgres`, not retroactively in
  every existing database.** New databases inherit it automatically
  (they're created from `template1`); a database that existed before this
  role first ran does not — `tasks/pgaudit.yml` reminds you of this every
  run, but enabling it in an existing application database is a manual,
  separate action.
- **PostgreSQL major version upgrades are not automated**, unlike
  `kubernetes_node`'s gated upgrade playbook. Changing `postgresql_version`
  on a host that already has an initialized data directory does not
  migrate anything — PostgreSQL major upgrades need `pg_upgrade` or a
  dump/restore, which touches data in ways this role deliberately doesn't
  automate. Standing up a *new* major version on a *new* host is what
  `postgresql_version` actually controls.
- **TLS defaults to a self-signed certificate**, same posture and same
  fix as `docker_host`'s Docker CE repo caveat pattern — fine for
  lab/staging, replace `postgresql_ssl_cert_src`/`_key_src` with a real
  CA-issued cert/key before this holds anything real.
- **PGDG content isn't Satellite content by default**, same caveat as
  every other `server_roles/` package source.
- **Firewalld access is zone-wide, not source-restricted to specific
  application-tier hosts**, same documented gap as `docker_host`'s
  `DOCKER-USER` note and `kubernetes_node`'s port list —
  `postgresql_allowed_hosts` controls which CIDRs get a `pg_hba.conf`
  entry at all (empty means nobody but localhost can even authenticate,
  a fail-closed default), but firewalld itself doesn't further restrict
  by source within the zone.

### What's intentionally out of scope

- **Automatic failover / HA orchestration** (Patroni, repmgr, pgpool-II,
  etcd-based consensus) — see above.
- **An actual backup solution** (pgBackRest, Barman, retention policies,
  restore drills) — WAL archiving is the mechanism, not the solution.
- **Connection pooling** (PgBouncer or similar) — a workload/architecture
  decision, not a database-server hardening concern.
- **Performance tuning beyond conservative starting defaults**
  (`postgresql_shared_buffers`, `_effective_cache_size`,
  `_max_connections`) — size these against your actual hardware and
  workload; this role gets you a safe, working starting point, not a
  tuned one.
- **Multiple PostgreSQL instances on one host** (different ports/data
  directories for logically separate databases sharing hardware) — one
  instance per host is the assumed topology; a host wanting more is a
  variables/paths exercise this role doesn't parameterize for.
