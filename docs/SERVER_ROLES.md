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

Turns a hardened RHEL9/RHEL10/AlmaLinux9/AlmaLinux10 host into a generic Docker
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
  best-effort `rhel9cis_rule_3_1_1: false` for RHEL9/AlmaLinux9 that you
  should verify against your installed `ansible-lockdown.RHEL9-CIS`
  version). Without that override, a later `hardening.yml` re-run (it's
  designed to be safe to schedule — see `playbooks/hardening.yml`) could
  flip RHEL9/AlmaLinux9's forwarding back off until `docker_host` next
  runs and re-asserts it.

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
picks which minor-version repo a node group uses. `k8s_supported_versions`
in `defaults/main.yml` enumerates exactly which minors this role has
actually been reasoned about — **1.28 through 1.34** — and
`tasks/main.yml` fails loudly up front if `k8s_version` isn't one of
them, rather than silently attempting a repo/kubeadm-config combination
nobody has verified.

**The kubeadm config API version is derived, not fixed.** kubeadm moved
its own config format from `v1beta3` to `v1beta4` in Kubernetes 1.31
(`v1beta4` became the default that release; `v1beta3` was **removed**
entirely in 1.32 — kubeadm 1.32+ cannot parse a v1beta3 document at all,
this isn't just a deprecation warning). `k8s_kubeadm_api_version`
(`defaults/main.yml`, `v1beta4` for `k8s_version >= 1.31`, `v1beta3`
below that) picks which of `templates/kubeadm-config.yaml.j2`'s two
structurally different documents gets rendered — v1beta4 also changed
`apiServer`/`controllerManager`/`scheduler.extraArgs` and
`nodeRegistration.kubeletExtraArgs` from a map to a list of `{name,
value}` pairs, so this isn't a one-line apiVersion bump. Written from
documented kubeadm behavior, not tested against every patch release in
`k8s_supported_versions` — verify with `kubeadm config migrate` (or the
kubeadm API reference for whichever kubeadm version you actually
installed) before relying on either branch in production.

**CNI/MetalLB manifest pins are not automatically kept in step with
`k8s_version`.** `k8s_cni_manifest_url_by_provider` and
`k8s_metallb_manifest_url` (`defaults/main.yml`) were validated against
the middle of the newly-supported range, not its full 1.28-1.34 span —
check Calico's/Flannel's/MetalLB's own published Kubernetes-compatibility
matrix for whichever `k8s_version` you're actually deploying, especially
at the 1.32-1.34 end, and bump the pinned tag if needed. This role can't
reliably cross-reference that compatibility matrix for you.

Two more consequences worth understanding before you touch any of this:

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
  `playbooks/kubernetes_upgrade.yml`, deliberately, **one minor version at
  a time**, with `k8s_upgrade_confirmed=true` required (same gate pattern
  as `roles/fips_mode`) — kubeadm doesn't support skipping a minor in a
  single `kubeadm upgrade apply` either, and `tasks/upgrade.yml` now
  asserts `k8s_target_version_full`'s minor matches `k8s_version` before
  doing anything, so forgetting to bump `k8s_version` first — as this
  role's own upgrade playbook header already instructs — fails loudly
  instead of silently upgrading the wrong thing. Spanning the full
  1.28-1.34 range end to end means six separate, sequential, ticketed
  upgrade runs, not
  one.

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

### External (non-stacked) etcd

Off by default (`k8s_external_etcd_enabled: false`) — kubeadm's default
"stacked" topology, one etcd member co-located on each
`k8s_control_plane` node as a static pod, needs nothing described here
at all. Turning it on switches to kubeadm's own documented external-etcd
mechanism instead: a dedicated etcd cluster on its own hosts (the
`k8s_etcd` inventory group, a third child of `k8s_nodes` alongside
`k8s_control_plane`/`k8s_workers` — populate it with an odd number of
hosts, 3 or 5, for real quorum tolerance) that the control plane's API
servers connect to as clients, fully decoupled from control-plane node
lifecycle. Worth it once you're running more than a handful of
control-plane nodes, or want to replace/upgrade a control-plane node
without ever touching etcd cluster membership.

- **`k8s_etcd` hosts never run `kubeadm init`/`kubeadm join`, and never
  become a Kubernetes API object** (no `Node` registers for them) —
  they run a **standalone kubelet** (a systemd drop-in,
  `tasks/etcd_local.yml`/`templates/etcd-kubelet-dropin.conf.j2`,
  overrides kubeadm's packaged unit entirely, since the packaged one
  expects a `/var/lib/kubelet/kubeconfig` a real join would have
  created) whose only job is watching `/etc/kubernetes/manifests/` for
  the etcd static pod kubeadm generates
  (`kubeadm init phase etcd local`). They still need the same
  repo/container-runtime/node-prep prep every other node in this role
  gets — etcd itself runs as a container, same as everything else
  kubeadm manages.
- **Certs are generated once and distributed via the control node,
  never directly between managed hosts.** The etcd CA is generated on
  the first `k8s_etcd` host, fetched to
  `artifacts/k8s_etcd_ca/` (control-node-local, `*.key` already
  `.gitignore`'d), then copied out to every other `k8s_etcd` host so
  they can all sign server/peer certs from the same CA. The single
  `apiserver-etcd-client` cert/key (the credential every API server
  uses to talk to etcd) is generated the same way and distributed to
  every `k8s_control_plane` host, first or joining
  (`tasks/external_etcd_certs.yml`) — this repo never assumes managed
  hosts trust each other's SSH keys, only the control node's, so
  fetch-then-copy through the control node is the only cross-host
  distribution mechanism used anywhere in this repo (see
  `provisioning/certificate_enrollment` for the same posture applied to
  a completely different kind of certificate).
- **Task order matters even more here than usual.** `tasks/main.yml`
  runs `etcd_local.yml` (bootstrap the etcd cluster) before
  `external_etcd_certs.yml` (copy its output onto every control-plane
  host) before `control_plane_init.yml` (which needs those certs
  already in place for `templates/kubeadm-config.yaml.j2`'s
  `etcd.external` block to mean anything) — all three rely on the same
  "one flat play, no `serial:`, linear-strategy task-boundary sync"
  mechanism this role already used for control-plane-then-worker
  ordering. `playbooks/kubernetes_cluster.yml`'s `hosts:` line now
  includes `k8s_etcd` for exactly this reason.
- **Bootstrap only — this does not manage etcd cluster membership
  changes.** `tasks/etcd_local.yml` assumes it's initializing a FRESH
  cluster (`initial-cluster-state: new`, every member started
  together). Adding a member to an already-running external etcd
  cluster, or replacing a failed one, is a manual `etcdctl member
  add`/`remove` procedure — a documented gap, same posture as this
  role's HA-control-plane-load-balancer note and
  `postgresql_server`/`mssql_server`'s "no Patroni/Always On"
  scoping. There is also no etcd health-check wiring
  (`etcdctl` isn't installed by this role at all) —
  `tasks/etcd_local.yml`'s final report tells you to check
  `crictl ps`/`journalctl -u kubelet` on each `k8s_etcd` host directly.

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
- **Multi-cluster federation and non-kubeadm installation methods**
  (kOps, Cluster API, managed cloud Kubernetes). All valid approaches,
  none of them "vanilla kubeadm," which is specifically what was asked for.
- **External etcd cluster membership changes** (adding/replacing a
  member after the initial bootstrap) and **etcd health-check tooling**
  (no `etcdctl` install/wiring) — see the "External (non-stacked) etcd"
  section above. Initial bootstrap of a fresh external etcd cluster
  itself is in scope (`k8s_external_etcd_enabled`); ongoing membership
  operations are not.

## `postgresql_server`

PostgreSQL on PGDG packages (not the OS's built-in AppStream module —
that's disabled explicitly since it conflicts). Run via
`playbooks/postgresql_server.yml` against the `dbservers` inventory
group, which already existed as a functional group for
`roles/firewalld_baseline` purposes before this role did; it's now
populated with real PostgreSQL vars instead of a placeholder port.

**Supports major version 15 and every version forward**,
open-ended — unlike `kubernetes_node`'s enumerated
`k8s_supported_versions` list, there's no fixed ceiling here
(`postgresql_min_supported_version` in `defaults/main.yml` is a floor
only, and `tasks/main.yml` fails loudly if `postgresql_version` is set
below it). That asymmetry is deliberate, not an oversight: PostgreSQL
major versions don't break this role's config the way Kubernetes minors
break kubeadm's own config API — every GUC this role sets
(`listen_addresses`, the `pgaudit.*` settings, `wal_level`,
`scram-sha-256` auth, `standby.signal`-based replication) has been
stable well before PG15, and PGDG's package/path naming convention
(`postgresql<N>-server`, `/usr/pgsql-<N>`, `pgaudit_<N>`) has held for
every major to date. The one real forward-compatibility risk is
`postgresql_pgaudit_package` — pgaudit's own release only catches up to
a new PostgreSQL major some time after that major ships, so setting
`postgresql_version` to something newer than what's been validated here
may hit a package that doesn't exist yet in PGDG; `tasks/repo.yml`'s
install task fails loudly rather than silently skipping pgAudit if so.

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

## `mssql_server`

Microsoft SQL Server on Microsoft's own `mssql-server` RPM (not a
container). Run via `playbooks/mssql_server.yml` against the
`mssql_hosts` inventory group — deliberately **not** `dbservers`
(`postgresql_server`'s group). Each database engine gets its own
inventory group in this repo, same reasoning as `docker_hosts`/
`k8s_nodes`/`netbox_hosts` each being separate rather than one
overloaded "workload" group — see the note in
`group_vars/dbservers.yml`.

### Platform support is narrower than this repo's other server_roles

Every other `server_roles/` entry targets RHEL 9 **and** 10. This one
doesn't: Microsoft only officially certifies specific RHEL major
versions for `mssql-server`, and that support has historically lagged a
new RHEL release by a long time — `mssql_supported_el_major_versions`
(`defaults/main.yml`) is `["9"]` only, and `tasks/main.yml` fails loudly
on anything else. AlmaLinux 9 works in practice via binary compatibility
but is not itself a Microsoft-certified platform — flag that distinction
for your validation package if it matters for your regulatory posture,
same caveat this repo already carries for AlmaLinux against Red Hat's
own Satellite entitlement model. `mssql_version` (`"2019"` or `"2022"`)
is likewise an explicit, enumerated list (`mssql_supported_versions`),
not an open-ended floor — unlike `postgresql_server`'s `postgresql_min_
supported_version`, Microsoft's repo URL and package set genuinely
differ per release and SQL Server ships far less often, so there's no
stable naming formula to generalize.

### Things that are NOT obvious and will bite you if skipped

- **This role will not accept Microsoft's EULA on your behalf.**
  `mssql_accept_eula` defaults to `false` and `tasks/setup.yml` fails
  loudly until it's set `true` deliberately — read the actual EULA
  first. Separately, `mssql_pid` defaults to `"Developer"` (full-featured,
  but licensed for non-production use only under Microsoft's own terms)
  so lab/staging just works out of the box; the role warns loudly every
  run against `Developer`/`Express` rather than silently letting a
  non-production-licensed edition end up in production.
- **Two separate Microsoft repos, not one.** `mssql-server` gets its own
  per-version repo file (`mssql-server-<version>.repo`); `mssql-tools18`
  (bundling `sqlcmd` and the `msodbcsql18` ODBC driver, both required for
  this role's own SQL Server Audit and backup tasks to work at all) comes
  from Microsoft's general "prod" repo instead, and needs its own
  separate `ACCEPT_EULA=Y` at install time for the ODBC driver
  specifically.
- **Initial setup is non-interactive and one-time only.** `tasks/setup.yml`
  runs `mssql-conf -n setup` with `MSSQL_SA_PASSWORD`/`MSSQL_PID`/
  `MSSQL_COLLATION`/`ACCEPT_EULA` as environment variables, gated on
  `{{ mssql_data_dir }}/master.mdf` not existing yet — same
  "initial-setup-only, no automated in-place major-version handling"
  posture as `postgresql_server`'s `initdb`. Changing `mssql_collation`
  or `mssql_pid` after this point needs Microsoft's own documented
  rebuild procedure, not something this role automates.
- **`mssql-conf` has no clean scriptable idempotency signal**, unlike
  `firewall-cmd --get-log-denied`'s bare, parseable output —
  `tasks/config.yml` reads each setting back with `mssql-conf get` first
  and only calls `mssql-conf set` when the current value doesn't already
  contain the desired one (a best-effort substring check, not an exact
  match, since `mssql-conf get`'s output format isn't a documented
  stable contract). Verify against your installed `mssql-conf` version
  if a setting seems to re-apply every run when it shouldn't.
- **SQL Server Audit is the GxP-relevant equivalent of `postgresql_server`'s
  pgAudit**, not a native config-file GUC the way PostgreSQL's is — it's
  a server audit object plus a server audit specification, created via a
  templated, idempotent T-SQL script (`templates/audit_setup.sql.j2`,
  `IF NOT EXISTS`/`IF ... is_state_enabled = 0` guards) that
  `tasks/audit.yml` only actually runs when a pre-check finds the audit
  missing or disabled. Re-review `mssql_audit_action_groups` against your
  own risk assessment before relying on it verbatim, same caveat as every
  other hardening default list in this repo.
- **Backup is a mechanism, not a solution, same posture as
  `postgresql_server`'s WAL archiving** — off by default;
  `mssql_backup_enabled: true` schedules a cron-driven native
  `BACKUP DATABASE` for every user database (system databases
  deliberately excluded) to a local directory only. No offsite copy, no
  retention policy, no restore drill, no differential/log backups. It
  also runs as `sa` rather than a dedicated minimally-privileged backup
  login — a documented gap, not a dedicated backup role, same "bring
  your own real backup infrastructure" posture as PostgreSQL's WAL
  archiving note. The `sa` password the cron job reads lives in a
  root-only `0600` file on disk — plaintext, same trade-off `apps/netbox`
  already accepts for its TLS key (this repo's host-level controls,
  `roles/user_access_gxp` and `roles/auditd_gxp`, are what actually gate
  that access, not file mode alone).
- **Always On Availability Groups / Failover Cluster Instances are out
  of scope**, same posture as `postgresql_server`'s "no Patroni/repmgr"
  scoping — SQL Server HA on Linux needs Pacemaker/corosync clustering,
  a different and much larger undertaking than this role's ambition
  level. Single-instance-per-host only.
- **TLS defaults to a self-signed certificate**, same posture and fix as
  every other TLS-terminating role in this repo — replace
  `mssql_ssl_cert_src`/`_key_src` with a real CA-issued cert/key before
  this holds anything real. `mssql_force_encryption: true` by default
  requires every client connection to be encrypted, not just
  opportunistic.
- **Firewalld access is zone-wide, not source-restricted**, same
  documented gap as every other `server_roles/` group.

### What's intentionally out of scope

- **Always On Availability Groups, Failover Cluster Instances, any HA
  orchestration** — see above.
- **An actual backup solution** — native `BACKUP DATABASE` to local disk
  is the mechanism, not the solution.
- **A dedicated, minimally-privileged backup login** — the scheduled
  backup job runs as `sa`; a purpose-built login with only `BACKUP
  DATABASE` rights is a documented gap.
- **Linked servers, SQL Server Agent jobs, Integration/Analysis/Reporting
  Services** — this role stands up a bare database engine instance, not
  the broader SQL Server platform.
- **Multiple SQL Server instances on one host** — one instance per host
  is the assumed topology, same posture as `postgresql_server`.

## `etcd_cluster`

A standalone etcd cluster — plain upstream etcd, downloaded as a pinned,
checksum-verified GitHub release tarball (`etcd_version`/
`etcd_release_sha256` in `defaults/main.yml`), with its own role-owned
systemd unit. **No Kubernetes tooling is involved anywhere in this
role** — no kubeadm, no kubelet, no container runtime. Run via
`playbooks/etcd_cluster.yml` against the `etcd_hosts` inventory group.

**This is a deliberately separate role from
`server_roles/kubernetes_node`'s own `k8s_external_etcd_enabled`
option**, not an alternative implementation of the same thing:

| | `kubernetes_node`'s external etcd | `etcd_cluster` |
|---|---|---|
| Mechanism | `kubeadm init phase certs`/`etcd local` | Plain `etcd`/`etcdctl` binaries |
| Runs on | `k8s_etcd` inventory group | `etcd_hosts` inventory group |
| Needs kubelet/a container runtime | Yes (etcd runs as a kubelet-managed static pod) | No |
| Purpose-built for | A Kubernetes control plane to consume | Anything else — a generic distributed KV store/coordination service, evaluating etcd itself |
| Client cert shape | One `apiserver-etcd-client` cert, meant for `kube-apiserver` | Whatever you generate against the cluster CA — no Kubernetes-specific cert exists here at all |

They share no code and their certs/data are not interchangeable — don't
point a Kubernetes cluster at an `etcd_cluster`-created cluster (or vice
versa) expecting it to just work. If your only reason for wanting
external etcd is Kubernetes, use `kubernetes_node`'s own option instead
— this role is for every other reason.

### Things that are NOT obvious and will bite you if skipped

- **No package manager involved at all.** etcd has no consistent
  official RPM across RHEL/AlmaLinux versions, so `tasks/install.yml`
  downloads the upstream release tarball directly and verifies it
  against `etcd_release_sha256` (from etcd's own published
  `SHA256SUMS`) before extracting anything — this role refuses to
  install an unverified binary, and `tasks/main.yml` fails loudly up
  front if that checksum is empty.
- **Mutual TLS needs a real (if small) CA, not a bare self-signed leaf
  cert** — unlike `postgresql_server`/`mssql_server`/`apps/nginx`'s TLS
  pattern (one self-signed cert per host, used by external clients
  only), etcd needs every member to verify every *other* member's cert
  too (peer TLS), which requires a shared CA all of them trust.
  `tasks/tls.yml` generates a small self-signed CA once (the first
  `etcd_hosts` host) and distributes it via the control node — same
  "fetch to the control node, then copy out, never directly between
  managed hosts" model as `provisioning/certificate_enrollment` and
  `kubernetes_node`'s own external-etcd CA distribution — then each
  host signs its own server/peer cert from that CA. A real CA
  (`etcd_ssl_ca_src`) is pluggable, but then you're on the hook for
  each host's own cert too (`etcd_ssl_cert_src`/`_key_src`, set in that
  host's own `host_vars`, since the SAN has to carry that host's
  address) — `provisioning/certificate_enrollment` against an internal
  Windows CA is one way to get one.
- **Bootstrap only, same posture as `kubernetes_node`'s external-etcd
  option.** `templates/etcd.yaml.j2` always renders
  `initial-cluster-state: new` — this role initializes a FRESH cluster
  with every member started together. Adding a member to an
  already-running cluster, or replacing a failed one, is a manual
  `etcdctl member add`/`remove` procedure this role does not automate.
- **Auth is a second, optional layer, not a replacement for TLS.**
  `etcd_auth_enabled` (off by default) adds etcd's own username/password
  auth on top of the mutual-TLS requirement that's always on — useful
  once multiple distinct applications share one cluster and need
  different permission scopes, not needed just to keep the cluster
  secure at the network level. `tasks/auth.yml`'s `etcdctl user add
  root:<password>` non-interactive syntax is documented etcdctl
  behavior but wasn't tested against every patch release you might pin
  `etcd_version` to — verify against your installed `etcdctl` if it
  fails outright.
- **Firewalld access is zone-wide, not source-restricted**, same
  documented gap as every other `server_roles/` group.

### What's intentionally out of scope

- **Cluster membership changes after initial bootstrap** (adding a
  member, replacing a failed one) — see above.
- **Snapshotting/backup automation** — `etcdutl snapshot save` exists
  and is installed by this role, but nothing schedules or manages it;
  bring your own, same "mechanism vs. solution" posture as
  `postgresql_server`'s WAL archiving.
- **Multi-datacenter/WAN-aware topologies, learner members, dynamic
  reconfiguration tooling** — this role stands up one flat, same-DC
  cluster from a static inventory group, nothing more.
- **A Kubernetes control plane pointed at this cluster** — see the
  comparison table above; use `kubernetes_node`'s own
  `k8s_external_etcd_enabled` option for that instead.

## `file_share_server`

A **temporary** SMB/NFS file share server — Samba and/or NFSv4, both
from OS-packaged `samba`/`nfs-utils` (no separate content source, this
one rides whatever `roles/patch_management` already keeps current). Run
via `playbooks/file_share_server.yml` against the `file_share_hosts`
inventory group. "Temporary" is not a figure of speech: age-based
automatic cleanup (`file_share_cleanup_enabled`) is **on by default**,
deleting files older than a per-share retention window permanently,
with no recovery mechanism. This role assumes transient working
storage — if regulated data ever transits a host in this group,
retention/deletion is a control decision your validation package needs
to own explicitly, not something to inherit silently from the default
30-day window.

### Shares, concretely

Everything is driven by `file_share_shares`, a flat list — one entry
per share, each independently exposed over SMB, NFS, or both:

```yaml
file_share_shares:
  - name: dropbox
    path: /srv/shares/dropbox
    protocols: [smb, nfs]
    retention_days: 14                      # optional; 0 = never auto-purge; omit to use file_share_cleanup_default_retention_days
    smb_valid_users: ["alice", "bob"]        # required if "smb" in protocols
    nfs_allowed_hosts: ["10.20.10.0/24"]     # required if "nfs" in protocols
```

`tasks/main.yml` fails loudly up front on an empty `file_share_shares`,
an invalid/missing `protocols` list, an SMB share with no
`smb_valid_users`, or an NFS share with no `nfs_allowed_hosts` — there
is no "export to everyone" or "share with no auth" default anywhere in
this role.

### Things that are NOT obvious and will bite you if skipped

- **Both protocols require authentication, always — no guest/anonymous
  access.** SMB uses its own local password database
  (`file_share_smb_users`, independent of any OS login — accounts get
  `/sbin/nologin` and exist purely for `smbpasswd` auth); NFS uses
  `sec=sys` (see below) gated by `nfs_allowed_hosts` per share. An empty
  `file_share_smb_users` with any SMB share configured fails the run
  loudly, same "mandatory auth" posture as `apps/registry`.
- **NFS is v4-only, deliberately, and each share is its own independent
  pseudo-root (`fsid=N`), not nested under one shared `fsid=0` tree.**
  No NFSv3, so no rpcbind/mountd dynamic ports to open through
  firewalld — just `2049/tcp`. `file_share_nfs_domain` must match every
  NFSv4 client's own `/etc/idmapd.conf` `Domain` setting, or UID/GID
  mapping silently falls back to `nobody` instead of failing loudly (an
  NFS client-side limitation this role can't fix from the server side).
- **NFS traffic is not encrypted, and `sec=sys` is host/IP-based trust,
  not real client authentication.** `nfs_allowed_hosts` restricts by
  source address, which is what actually gates access — not a strong
  guarantee against a spoofed source on a network you don't otherwise
  trust. Kerberos (`sec=krb5p`, real encryption + authentication) is a
  documented gap, not wired up here.
- **SMB, by contrast, has mandatory SMB3 transport encryption
  (`smb encrypt = required`, not opportunistic) and rejects SMB1
  outright** (`min protocol version = SMB2`) — the two protocols this
  role wires up land in noticeably different places on the
  security spectrum; don't assume NFS shares get the same protections
  SMB shares do.
- **SELinux stays enforcing** (same posture as every other
  `server_roles/` entry) **via `public_content_t`/`public_content_rw_t`
  labeling** (`tasks/shares.yml`, `community.general.sefcontext` +
  `restorecon`), not the blunt `nfs_export_all_rw`/`ro` "allow
  everything" booleans. This is also what lets one directory be served
  over *either* protocol (or both) with a single, consistent label
  rather than needing Samba-specific and NFS-specific contexts to
  coexist. If a share still hits an AVC denial, `ausearch -m avc -ts
  recent` on that host is the first thing to check — this wasn't
  exhaustively tested against every SELinux policy version across
  `k8s_supported_versions`-style ranges the way some of this repo's
  other content is.
- **Cleanup does not distinguish GxP-relevant data from anything else
  in a share.** `retention_days` is per-share and blunt — everything
  older than the window goes, permanently, on the schedule in
  `file_share_cleanup_cron`. Set `retention_days: 0` on any share that
  needs to be exempt, deliberately, rather than assuming the default
  30-day window is conservative enough for what ends up there.
- **Firewalld access is zone-wide, not source-restricted**, same
  documented gap as every other `server_roles/` group — `nfs_allowed_hosts`/
  `smb_valid_users` are the actual access controls; firewalld here is a
  coarser, host-level backstop, not a per-share network boundary.

### What's intentionally out of scope

- **Active Directory-integrated SMB auth (`security = ads`), Kerberos
  for either protocol** — local Samba accounts and `sec=sys` NFS only.
  If you already have `roles/identity_sssd` joined to a domain on this
  host, integrating that with Samba/NFS auth is a deliberate, separate
  exercise this role doesn't attempt.
- **NFSv3, for legacy clients that can't do v4** — a documented,
  deliberate gap (see above), not an oversight.
- **Quota enforcement, per-user disk usage limits** — `retention_days`
  bounds how long files live, not how much any one user can store while
  they're there.
- **Replication, high availability, or any backup of share content
  beyond the retention window itself** — this is explicitly temporary,
  working storage; if something in a share needs to survive, that's a
  separate, deliberate copy to somewhere else, not this role's job.

## `kurrentdb_cluster`

A KurrentDB cluster. **KurrentDB is the current name of what used to
ship as "EventStoreDB"** — Event Store Ltd rebranded the product in
2024, and the last release actually published under the EventStoreDB
name (23.10 LTS) reached end-of-support in October 2025. Same
event-sourcing/event-store database engine underneath, new package/repo
names — if you came looking for "eventstoredb", this is it, targeting
the current 24.10 LTS line (`kurrentdb_version` in `defaults/main.yml`).
Run via `playbooks/kurrentdb_cluster.yml` against the `kurrentdb_hosts`
inventory group.

Structurally this role is a hybrid of two patterns already in this repo:
package installation follows `postgresql_server`/`mssql_server`'s
Satellite-content-view-with-public-fallback approach (`tasks/repo.yml`),
because — unlike `etcd_cluster` — KurrentDB has a real, consistent
official RPM (Cloudsmith-hosted `kurrentdb` package); but the cluster
bootstrap and mutual-TLS shape (`tasks/tls.yml`) closely follows
`etcd_cluster`'s, because KurrentDB is, like etcd, a gossip-clustered
store where every node has to verify every *other* node's identity, not
a single-instance engine with an optional replica the way
`postgresql_server`/`mssql_server` are.

### Things that are NOT obvious and will bite you if skipped

- **Every node's certificate must share the exact same Common Name.**
  Unlike `etcd_cluster`'s TLS (each peer's CN is its own hostname),
  KurrentDB authenticates a connecting peer by checking that its client
  certificate's CN exactly matches its own certificate's CN
  (`CertificateReservedNodeCommonName`, auto-read from each node's own
  cert as of 23.10+). `tasks/tls.yml` signs every node's cert with the
  same fixed `kurrentdb_cert_common_name` value for exactly this reason
  — changing it per-host (the instinct anyone familiar with
  `etcd_cluster` will have) makes every node reject every other node.
  SAN still carries each node's own hostname/IP for standard client-side
  hostname verification.
- **`TrustedRootCertificatesPath` is a directory, not a single file.**
  `tasks/tls.yml` keeps the cluster CA in its own `tls/ca/` subdirectory
  for exactly this reason, separate from each node's own `node.crt`/
  `node.key` one level up.
- **"Secure by default" is not configurable off here.** KurrentDB v20+
  refuses to run without valid TLS config unless you explicitly set
  `Insecure: true` — this role never sets it, same "TLS is not optional
  for this role" posture as `etcd_cluster`, stricter than
  `postgresql_server`/`mssql_server` where TLS is opt-in.
- **`ClusterSize` and `GossipSeed` are computed fresh from
  `kurrentdb_hosts` group membership on every run**, not hand-set.
  `templates/kurrentdb.conf.j2` renders both from `groups['kurrentdb_hosts']`
  directly — add or remove a host from the inventory group and every
  member's rendered config (and restart) reflects it on the next run.
  This is config regeneration, not a live cluster membership operation —
  KurrentDB has its own admin API/CLI for actually adding/removing a
  member from an already-running cluster, which this role does not
  automate, same "bootstrap-shaped, not membership-change-shaped"
  posture as `etcd_cluster`.
- **The public Cloudsmith repo path fetches a vendor-hosted `.repo`
  file directly** (`kurrentdb_public_repo_config_url`), the same
  `get_url`-not-`curl|bash` posture as `mssql_server`'s public-repo
  fallback — not Cloudsmith's own `setup.rpm.sh` convenience script,
  which pipes an unreviewed remote script through `sudo bash`.
- **The `kurrentdb` RPM creates its own system account** (assumed to be
  named `kurrentdb`) — this role doesn't create it the way
  `etcd_cluster` creates etcd's, because that role has no package to do
  it for it. Verify with `getent passwd kurrentdb` after a first install
  in your environment before trusting `kurrentdb_user`/`kurrentdb_group`
  blindly.
- **Firewalld access is zone-wide, not source-restricted**, same
  documented gap as every other `server_roles/` group.

### What's intentionally out of scope

- **Cluster membership changes after initial bootstrap** (adding a
  member, replacing a failed one) — see above.
- **Projections, persistent subscriptions tuning, or any
  application-level stream/schema design** — this role stands up a bare
  KurrentDB cluster, not an event-sourcing architecture on top of it.
- **Backup/snapshot automation** — same "mechanism vs. solution" posture
  as `postgresql_server`'s WAL archiving and `etcd_cluster`'s
  `etcdutl snapshot save`.
- **Multi-datacenter/WAN-aware topologies, read-only replicas** — this
  role stands up one flat, same-DC cluster from a static inventory
  group, nothing more.
