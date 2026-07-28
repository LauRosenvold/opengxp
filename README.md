# opengxp

**opengxp** is Ansible automation for configuring and securing a large
fleet of RHEL 9, RHEL 10, AlmaLinux 9, and AlmaLinux 10 servers in a
GxP-regulated environment (GMP/GLP/GCP).

Standing up GxP-compliant infrastructure normally means re-deriving the
same CIS-to-GxP mapping by hand for every estate: which hardening
controls apply, which need to be superseded or extended for a specific
audit event taxonomy, how compliance evidence gets generated and
preserved, and how all of that stays consistent across dev/staging/
validation/production without drifting. opengxp packages that mapping
as a ready-to-run, change-controlled Ansible codebase — CIS Benchmark
hardening (via upstream `ansible-lockdown` roles where mature, a
hand-built fallback where it isn't yet), a GxP-specific audit event
taxonomy, segregation-of-duties access control, OpenSCAP compliance
evidence generation, and pluggable workload-enablement layers (Docker,
Kubernetes, PostgreSQL) and application deployments (NetBox, a private
registry) on top — so building and validating a new regulated host is a
matter of adding it to inventory and running a playbook, not
re-authoring the control baseline from scratch. Every role and playbook
here is written to be idempotent — re-running any of them against an
already-converged host changes nothing and reports no changes, which is
what makes `hardening.yml`'s scheduled re-runs and a periodic-review
`--check --diff` (see [docs/VALIDATION.md](docs/VALIDATION.md))
meaningful evidence of drift rather than noise. This is enforced by
convention, not hope: every `command`/`shell` task either reports
`changed_when: false` (a read-only check) or is guarded behind a
`when:` condition that only fires when real work remains, so a clean
second run touches nothing — and `molecule/ssh_hardening` (the pattern
documented there to replicate for every other role) runs an automated
`idempotence` step that fails CI outright if a second `converge` reports
any change at all.

This repo handles the **technical control** layer of computerized system
validation — it does not replace the procedural controls (change control
tickets, IQ/OQ/PQ sign-off, periodic review) your Quality organization
must run around it. See [docs/VALIDATION.md](docs/VALIDATION.md) for how
the two fit together.

## Supported Versions

Every version below is pinned somewhere in this repo — `requirements.yml`
for collections/roles, a role's own `defaults/main.yml` for
software/image versions, or a role's `meta/main.yml` for the target
platform. Bumping any of them is itself a change-controlled event (see
[docs/CHANGE_CONTROL.md](docs/CHANGE_CONTROL.md)), not a drive-by
dependency update.

**Managed operating systems** (`roles/`, `server_roles/`, `apps/`):

| OS | Versions | Notes |
|---|---|---|
| RHEL | 9, 10 | `rhel9`/`rhel10` inventory groups — see [docs/BASELINE_MAPPING.md](docs/BASELINE_MAPPING.md) |
| AlmaLinux | 9, 10 | `almalinux9`/`almalinux10` inventory groups, binary-compatible with the matching RHEL major version |

Python interpreter: `python3.9` on the EL9 family (RHEL 9 / AlmaLinux 9),
`python3.12` on the EL10 family (RHEL 10 / AlmaLinux 10) — set per OS
group in `inventories/<env>/group_vars/`.

**Control node**: `ansible-core >= 2.15` (every role's `min_ansible_version`).
No specific control-node OS is required, but several roles need Python
libraries or CLI tools installed there specifically, not on a managed
target — see [docs/PROVISIONING.md](docs/PROVISIONING.md) and
[docs/PKI_DNS.md](docs/PKI_DNS.md):

| Tool | Needed by | Install |
|---|---|---|
| `pyvmomi` | `provisioning/vm_provision` (vCenter) | `pip install pyvmomi` |
| `pywinrm` | `provisioning/vm_provision` (Hyper-V), `provisioning/dns_registration`, `provisioning/certificate_enrollment` | `pip install "pywinrm>=0.3.0"` |
| `proxmoxer`, `requests` | `provisioning/vm_provision` (Proxmox VE) | `pip install proxmoxer requests` |
| `openssl` (CLI) | `apps/netbox`, `apps/registry`, `apps/nginx`, `provisioning/certificate_enrollment` | usually already present |
| `ssh-keygen` (CLI) | `playbooks/rotate_automation_user_key.yml` | part of any OpenSSH client install |

**Hypervisors / VM provisioning** (`provisioning/vm_provision` — see
[docs/PROVISIONING.md](docs/PROVISIONING.md)):

| Hypervisor | Collection | Version pinned | Server version |
|---|---|---|---|
| VMware vCenter | `community.vmware` | 4.5.0 | Not independently pinned — whatever vSphere version that collection release supports; verify against your estate before relying on it |
| Microsoft Hyper-V | `ansible.windows` | 2.4.0 | Not independently pinned — same caveat |
| Proxmox VE | `community.proxmox` | 1.3.0 | Not independently pinned — same caveat, see that role's note on verifying the exact module parameter set too |

**Windows infrastructure** (`dns_servers`/`ca_servers` inventory groups —
see [docs/PKI_DNS.md](docs/PKI_DNS.md)):

| Component | Versions | Collection |
|---|---|---|
| Windows Server (DNS Server role) | 2019, 2022 | `community.windows` 2.3.0 (`win_dns_record`) |
| Windows Server (Active Directory Certificate Services) | 2019, 2022 | n/a — `certreq`/`certutil` (built-in Windows tools), driven over WinRM |

**Workload platforms** (`server_roles/` — see
[docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)):

| Platform | Version | Notes |
|---|---|---|
| Kubernetes | 1.28 - 1.34 (minor only — pins the `pkgs.k8s.io` repo; `k8s_supported_versions` in `defaults/main.yml` enumerates exactly these) | `k8s_version` in `inventories/<env>/group_vars/k8s_nodes.yml`; kubeadm config renders as v1beta3 below 1.31, v1beta4 from 1.31 on (mandatory from 1.32) — see [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) |
| Calico (CNI) | v3.28.0 | `k8s_cni_manifest_url_by_provider` — validated mid-range, not automatically matched to `k8s_version`; verify/bump for versions toward either end of 1.28-1.34 |
| Flannel (CNI) | v0.25.5 | same caveat as Calico above |
| MetalLB | v0.14.8 | off by default, L2 mode only; same "not auto-matched to `k8s_version`" caveat |
| PostgreSQL (PGDG) | 15+ (open-ended — no fixed ceiling, see `postgresql_min_supported_version`) | `postgresql_version` in `inventories/<env>/group_vars/dbservers.yml` (default `16`); see [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) for the one forward-compat risk (`postgresql_pgaudit_package`) |
| Microsoft SQL Server | 2019, 2022 (`mssql_supported_versions`, enumerated — see [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) for why this can't go open-ended the way PostgreSQL does); **EL9 only**, RHEL10/AlmaLinux10 not yet Microsoft-certified | `mssql_version` in `inventories/<env>/group_vars/mssql_hosts.yml` (default `2022`) |
| etcd (standalone, `server_roles/etcd_cluster`) | 3.5.17 | `etcd_version` in `inventories/<env>/group_vars/etcd_hosts.yml`; pinned to a specific upstream GitHub release, checksum-verified via `etcd_release_sha256` — not a distro package |
| Docker CE | latest from the distro-appropriate `docker-ce` repo | not independently version-pinned — tracks whatever that repo currently ships |
| Samba / nfs-utils (`server_roles/file_share_server`) | latest from the OS's own repos | not independently version-pinned, same posture as Docker CE above — no separate content source, rides `roles/patch_management` |

**Application container images** (`apps/`):

| App | Image | Notes |
|---|---|---|
| NetBox | `netboxcommunity/netbox:v4.1-3.0.2` | `apps/netbox` |
| NetBox's bundled Redis | `redis:7-alpine` | cache + task queue, always containerized |
| NetBox's bundled PostgreSQL | `postgres:16-alpine` | lab/evaluation fallback only — see [docs/APPS.md](docs/APPS.md) |
| Private registry | `registry:2.8.3` (`distribution/distribution`) | `apps/registry` |
| Registry web UI | `docker-registry-ui:2.5.7` | optional, loopback-bound by default |
| nginx (standalone and NetBox's reverse-proxy sidecar) | `nginxinc/nginx-unprivileged:1.27-alpine` | `apps/nginx`, `apps/netbox` |

## Assumptions baked into this design

These were explicit decisions, not defaults — revisit them if your
environment differs:

- **Baseline:** CIS Benchmark (Level 1 server + selected Level 2 controls),
  not DISA STIG. If you later need STIG too, `ansible-lockdown.RHEL9-STIG`
  slots in next to the CIS role the same way — see
  [docs/BASELINE_MAPPING.md](docs/BASELINE_MAPPING.md).
- **Execution:** plain `ansible-core`, run from a CLI/bastion or a CI
  runner — no Ansible Automation Platform (controller, EE, RBAC) assumed.
  Everything here still works fine *under* AAP later if you adopt it; you'd
  just import this repo as a project and wrap job templates around
  `playbooks/*.yml`.
- **Content source / patching:** Red Hat Satellite/Capsule. Hosts are
  expected to already be registered to a Satellite org with content views
  and lifecycle environments defined; `roles/satellite_registration` only
  handles the client-side `subscription-manager` registration against your
  Satellite, not Satellite server administration itself.
- **Logging & identity:** no specific SIEM or AD/IdM domain was named yet,
  so `roles/logging_forward` and `roles/identity_sssd` are built pluggable
  — real endpoints go in `inventories/production/group_vars/all.yml`
  (clearly marked `CHANGEME` placeholders) once you have them.
- **AD/IdM groups drive both SSH access and sudo, not just one.**
  `identity_allowed_groups` (who can log in) and `roles/user_access_gxp`'s
  `source: directory` sudo groups (what they can do) are separate,
  fail-closed allow-lists — an empty list locks everyone out rather than
  letting everyone in. ID mapping (SID-based vs. AD POSIX attributes) is
  a deliberate per-estate choice, not something this repo can default
  correctly for you — see [docs/IDENTITY.md](docs/IDENTITY.md) before
  joining a real domain.
- **RHEL 9 / AlmaLinux 9 CIS content:** `ansible-lockdown.RHEL9-CIS` is
  mature/stable content with no RHEL-specific dependency, so it's used
  directly by both the `rhel9` and `almalinux9` inventory groups — no
  fallback role needed, unlike the RHEL10/AlmaLinux10 case below.
- **RHEL 10 / AlmaLinux 10 CIS content:** `ansible-lockdown.RHEL10-CIS` was
  not yet a tagged, stable release at authoring time, and no CIS automation
  role exists for AlmaLinux at all. `roles/cis_hardening` detects whether
  RHEL10-CIS is installed and falls back to `roles/el10_baseline_hardening`,
  a hand-built role covering the same CIS domains (PAM/password quality,
  sysctl, filesystem/module restrictions, service disablement, account
  policy) — shared by both the `rhel10` and `almalinux10` inventory groups,
  since the two OSes are binary-compatible at this layer. Swap the fallback
  off for `rhel10` once upstream RHEL10-CIS is GA and you've validated it;
  `almalinux10` stays on the fallback (see `requirements.yml`).
- **AlmaLinux and Red Hat Satellite:** AlmaLinux carries no RHEL
  subscription entitlement, but Satellite/Katello can still register and
  content-manage it against a custom AlmaLinux content view. If your
  Satellite doesn't do that and you point AlmaLinux hosts at public
  mirrors instead, set `satellite_registration_enabled: false` in
  `inventories/<env>/group_vars/almalinux9.yml` /
  `inventories/<env>/group_vars/almalinux10.yml` to skip
  `roles/satellite_registration` for that group.
- **No playbook ever connects as root.** Every playbook here connects and
  `become`s-from a dedicated local account (`automation_user_name` in
  `group_vars/all.yml`, `svc_ansible` by default) created by
  `roles/automation_user` — key-auth only, locked password, NOPASSWD sudo,
  every action captured by `roles/user_access_gxp`'s sudo I/O logging. That
  account has to exist before anything else can run, which is what
  `playbooks/provision_automation_user.yml` is for — see "Usage" below.
  Rotating its SSH key is a three-stage, change-controlled process with
  its own tooling (`playbooks/rotate_automation_user_key.yml`,
  `playbooks/verify_automation_user_key.yml`) — see
  [docs/AUTOMATION_USER_KEY_ROTATION.md](docs/AUTOMATION_USER_KEY_ROTATION.md).
- **Workload-enablement roles (Docker, Kubernetes, PostgreSQL, ...) live
  in `server_roles/`, not `roles/`.** Different change-control weight,
  different cadence, not part of `playbooks/site.yml` — see
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md).
- **Kubernetes means vanilla kubeadm**, not OpenShift/k3s/a managed cloud
  offering, supporting 1.28-1.34 (`k8s_supported_versions` — the kubeadm
  config template is API-version-aware, v1beta3 below 1.31 and v1beta4
  from 1.31 on, since kubeadm itself can't parse v1beta3 at all from 1.32
  onward). SELinux stays enforcing (no `setenforce 0`) regardless of
  container runtime (containerd or CRI-O, pluggable), CNI (Calico or
  Flannel, pluggable) and any HA control-plane load balancer are
  bring-your-own, MetalLB (bare-metal `LoadBalancer` Services) is
  available but off by default and L2-mode-only, etcd defaults to
  kubeadm's stacked topology with an external (dedicated-host) etcd
  cluster available as an opt-in (`k8s_external_etcd_enabled`, a new
  `k8s_etcd` inventory group — bootstrap only, not ongoing membership
  management), and version changes go through an explicitly-gated,
  one-minor-at-a-time upgrade playbook, never routine patching — see
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s `kubernetes_node`
  section before using this on anything real.
- **PostgreSQL means PGDG packages** (not the OS's AppStream module),
  with pgAudit on by default for a GxP-relevant SQL-level audit trail.
  Replication is basic streaming primary/replica with no automatic
  failover, and WAL archiving is a mechanism, not a backup strategy — see
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s `postgresql_server`
  section before using this on anything real.
- **Microsoft SQL Server (`server_roles/mssql_server`) is a separate
  database-engine role and inventory group (`mssql_hosts`), not folded
  into `dbservers`/`postgresql_server`.** EL9 only — Microsoft doesn't
  certify RHEL 10 for `mssql-server` yet, and this role fails loudly
  rather than assuming it works. This role will not accept Microsoft's
  EULA for you (`mssql_accept_eula` must be set `true` deliberately),
  defaults to the Developer edition (non-production-licensed, warns
  loudly every run), and SQL Server Audit (the T-SQL equivalent of
  pgAudit) and native backup follow the same on-by-default-audit/
  off-by-default-backup-mechanism posture as `postgresql_server` — see
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s `mssql_server` section
  before using this on anything real.
- **`server_roles/etcd_cluster` is a standalone etcd cluster with
  no Kubernetes tooling at all** — plain upstream `etcd`/`etcdctl`
  binaries (a pinned, checksum-verified GitHub release, since etcd has
  no consistent official RPM across RHEL/AlmaLinux), its own systemd
  unit, mutual TLS via a small self-signed cluster CA by default. Runs
  against its own `etcd_hosts` inventory group, deliberately separate
  and non-interoperable with `kubernetes_node`'s own
  `k8s_external_etcd_enabled` option (which bootstraps etcd via kubeadm
  specifically for a Kubernetes control plane, needs kubelet + a
  container runtime, and generates Kubernetes-specific client certs) —
  use this role instead only when Kubernetes isn't the reason you want
  etcd. See [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s
  `etcd_cluster` section, including its comparison table against the
  `kubernetes_node` option.
- **`server_roles/file_share_server` is a TEMPORARY SMB/NFS file
  server — age-based automatic cleanup is on by default, not opt-in.**
  Files older than each share's own retention window (30 days unless
  overridden) are deleted permanently, with no recovery mechanism; this
  is transient working storage, not a GxP record repository. Both
  protocols always require authentication (no guest/anonymous access
  anywhere), SMB gets mandatory SMB3 transport encryption, and NFS is
  v4-only with no rpcbind/mountd — but NFS itself is never encrypted
  (`sec=sys`, host/IP trust only; Kerberos is a documented gap). SELinux
  stays enforcing via `public_content_t`/`public_content_rw_t` file
  labeling, not the blunt `nfs_export_all_rw` boolean. See
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s `file_share_server`
  section before using this on anything real.
- **Application deployments (NetBox, a private registry, standalone
  nginx, ...) live in `apps/`, a third tier below `server_roles/`.** An
  application runs *on top of* a `server_roles/` platform (e.g.
  `apps/netbox`, `apps/registry`, and `apps/nginx` all on a
  `server_roles/docker_host`) rather than replacing it — see
  [docs/APPS.md](docs/APPS.md). Same "not part of `site.yml`, own
  inventory group, own change-control weight" pattern, one step further
  down.
- **The private registry (`apps/registry`) requires authentication for
  every push and pull, always, and images are immutable by default**
  (deletion off, no garbage collection, until you set a documented
  retention policy). It terminates TLS itself — no reverse proxy needed,
  unlike NetBox. See [docs/APPS.md](docs/APPS.md)'s `registry` section,
  including how to point other roles' registry-mirror vars at it once
  it's running.
- **`apps/nginx` is a standalone reverse-proxy/static-content server,
  distinct from netbox's and registry's own bundled TLS handling.**
  Configurable per-vhost (`mode: static` or `mode: proxy`), default-deny
  (`444`) for any Host header that doesn't match a configured vhost,
  same self-signed-default/real-cert-pluggable TLS pattern as everything
  else in this repo. See [docs/APPS.md](docs/APPS.md)'s `nginx` section.
- **VM provisioning (`provisioning/vm_provision`) is a fourth tier that
  runs *before* everything above — it creates the VM itself** against
  vCenter, Hyper-V, or Proxmox VE, before any managed Linux host exists
  for the rest of this repo to touch. On-prem only (no cloud provider
  support), and it needs Python libraries on the **control node**
  (`pyvmomi`, `pywinrm`, `proxmoxer`/`requests`), not a managed target —
  the first dependency of that kind in this repo. It does not register a
  newly created host into the checked-in inventory for you; that stays a
  deliberate, reviewed change. See
  [docs/PROVISIONING.md](docs/PROVISIONING.md), including why several
  existing `hosts: all` playbooks changed to exclude `hyperv_hosts` and
  `localhost`, and why Proxmox VMIDs are never auto-assigned.
- **DNS records and Windows-PKI certificates (`provisioning/dns_registration`,
  `provisioning/certificate_enrollment`) connect to Windows over WinRM,
  same as `vm_provision`'s Hyper-V path** — `win_dns_record` for DNS,
  `certreq`/`certutil` for requesting and revoking certificates from an
  internal Active Directory Certificate Services CA. The private key
  never leaves the control node; only the CSR and the issued certificate
  cross the wire. Neither role deploys its output anywhere automatically
  — point the relevant app/role's own `*_ssl_cert_src`/`*_ssl_key_src`
  var at the fetched files yourself. See [docs/PKI_DNS.md](docs/PKI_DNS.md),
  including why WinRM was chosen over a Linux-native alternative (dynamic
  DNS updates, ADCS's web enrollment service) for both.
- **LVM, application and data on their own volumes, fleet-wide.**
  `roles/lvm_storage` builds a volume group on a dedicated additional
  disk (exactly what `provisioning/vm_provision`'s `vm_extra_disk_gb`
  creates) and mounts `/opt` (application) and `/data` (data)
  separately from root — it never touches the root disk's own
  partitioning, which stays a kickstart/image-template decision. Refuses
  to touch a disk that already has a filesystem/LVM signature unless
  explicitly forced. See [docs/STORAGE.md](docs/STORAGE.md), including
  how to add workload-specific volumes (e.g. `/var/lib/pgsql`) using the
  same mechanism per host group.

## Layout

```
ansible.cfg
requirements.yml              # pinned collections + external roles (change-controlled)
inventories/
  production/                 # the only inventory playbooks run against by default
  staging/                    # pre-prod qualification environment
  validation/                 # IQ/OQ/PQ execution environment (see docs/VALIDATION.md)
playbooks/
  provision_automation_user.yml # ONE-TIME per host, run before anything else — see "Usage"
  rotate_automation_user_key.yml # generates a new automation_user SSH keypair — see AUTOMATION_USER_KEY_ROTATION.md
  verify_automation_user_key.yml # confirms a candidate key works before you retire the old one — see AUTOMATION_USER_KEY_ROTATION.md
  site.yml                    # full build: common -> hardening -> compliance evidence
  bootstrap.yml                # first-boot: automation user + Satellite registration + baseline
  hardening.yml                # security roles only, safe to re-run on a schedule
  patch_management.yml         # controlled patch window playbook
  compliance_scan.yml          # OpenSCAP evidence generation
  docker_host.yml              # workload enablement, NOT part of site.yml — see SERVER_ROLES.md
  kubernetes_cluster.yml       # vanilla kubeadm cluster setup, NOT part of site.yml
  kubernetes_upgrade.yml       # explicitly-gated version upgrade, --tags upgrade required
  postgresql_server.yml        # workload enablement, NOT part of site.yml
  mssql_server.yml             # workload enablement, NOT part of site.yml
  etcd_cluster.yml             # workload enablement, NOT part of site.yml — standalone etcd, no Kubernetes involved
  file_share_server.yml        # workload enablement, NOT part of site.yml — temporary SMB/NFS file server, cleanup on by default
  netbox.yml                   # application deployment, NOT part of site.yml — see APPS.md
  registry.yml                 # application deployment, NOT part of site.yml — see APPS.md
  nginx.yml                    # application deployment, NOT part of site.yml — see APPS.md
  provision_vm_vcenter.yml     # creates a VM from a vCenter template — runs BEFORE any of the above, see PROVISIONING.md
  provision_vm_hyperv.yml      # creates a VM from a Hyper-V template — runs BEFORE any of the above, see PROVISIONING.md
  provision_vm_proxmox.yml     # creates a VM from a Proxmox VE template — runs BEFORE any of the above, see PROVISIONING.md
  register_dns.yml             # creates/removes DNS records on a Windows DNS Server — see PKI_DNS.md
  request_certificate.yml      # requests/revokes a certificate from a Windows CA (ADCS) — see PKI_DNS.md
  lvm_storage_grow.yml         # explicitly-gated LV/filesystem grow, --tags grow required
roles/
  automation_user/            # dedicated non-root local account every playbook connects as
  common/                     # hostname, timezone, chrony baseline, MOTD/banner, base packages
  satellite_registration/     # subscription-manager -> Satellite org/activation key
  patch_management/           # dnf update policy, reboot handling, maintenance windows
  lvm_storage/                 # fleet-wide LVM: /opt (application) + /data (data), separate from root — see STORAGE.md
  cis_hardening/               # thin wrapper: dispatches to lockdown roles per OS group
  el10_baseline_hardening/      # fallback CIS-equivalent controls for RHEL10 + AlmaLinux10
  ssh_hardening/                # OpenSSH server hardening beyond generic CIS defaults
  selinux_enforce/              # SELinux enforcing + policy drift detection
  firewalld_baseline/           # default-deny zones, only declared services open
  auditd_gxp/                   # audit rules mapped to GxP/21 CFR Part 11 event categories
  aide_fim/                     # AIDE file integrity monitoring, GxP-relevant paths
  chrony_time/                  # time sync — required for record integrity/timestamping
  logging_forward/              # rsyslog TLS forwarding to central log/SIEM (pluggable)
  identity_sssd/                # AD/IdM domain join, ID mapping, who-can-log-in groups (pluggable) — see IDENTITY.md
  fips_mode/                    # optional FIPS 140-2/3 mode enablement
  compliance_scan/              # OpenSCAP scan + report artifact, evidence for validation
  user_access_gxp/               # local + AD/IdM-group sudo (SoD), break-glass accounts — see IDENTITY.md
server_roles/                  # workload enablement — separate change-control weight, see SERVER_ROLES.md
  docker_host/                   # Docker CE, hardened daemon.json, audit/AIDE/firewall integration
  kubernetes_node/                # vanilla kubeadm, multi-version, pluggable runtime (containerd/CRI-O) + CNI (Calico/Flannel) + MetalLB + optional external etcd
  postgresql_server/               # PGDG PostgreSQL, pgAudit, TLS, basic primary/replica streaming replication
  mssql_server/                     # Microsoft SQL Server (mssql-server RPM), SQL Server Audit, TLS, EL9 only — see SERVER_ROLES.md
  etcd_cluster/                      # standalone etcd (pinned upstream binary release, no Kubernetes tooling), mutual TLS — see SERVER_ROLES.md
  file_share_server/                  # temporary SMB/NFSv4 file server, mandatory auth on both, age-based auto-cleanup on by default — see SERVER_ROLES.md
apps/                           # application deployments on top of server_roles/ — separate again, see APPS.md
  netbox/                         # NetBox Docker Compose stack: nginx TLS proxy, external-DB-recommended, containerized Redis
  registry/                       # Private Docker/OCI registry: native TLS, mandatory htpasswd auth, immutable images by default
  nginx/                          # Standalone containerized nginx: per-vhost static content or reverse proxy, default-deny unmatched Host
provisioning/                   # creates the VM itself, before any tier above applies — see PROVISIONING.md
  vm_provision/                    # vCenter (community.vmware), Hyper-V (ansible.windows + PowerShell), or Proxmox VE (community.proxmox), clone from template
  dns_registration/                # Windows DNS Server A/CNAME/PTR records via WinRM — see PKI_DNS.md
  certificate_enrollment/          # Windows CA (ADCS) certificate request/revoke via certreq/certutil over WinRM — see PKI_DNS.md
molecule/
  ssh_hardening/                 # example molecule scenario — pattern to replicate per role
docs/
  VALIDATION.md                  # how this repo maps to computerized system validation
  CHANGE_CONTROL.md              # branching/approval model expected around this repo
  AUTOMATION_USER_KEY_ROTATION.md # three-stage runbook for rotating automation_user_name's SSH key
  BASELINE_MAPPING.md            # CIS control -> role/task cross-reference
  SERVER_ROLES.md                # why server_roles/ is separate from roles/, and per-role specifics
  APPS.md                        # why apps/ is a third tier below server_roles/, plus netbox/registry/nginx specifics
  IDENTITY.md                    # AD/IdM join, SSH/sudo group mapping, ID mapping choice, GSSAPI SSH
  PROVISIONING.md                # why provisioning/ runs before every other tier, vCenter/Hyper-V/Proxmox specifics
  PKI_DNS.md                     # DNS record + Windows-PKI certificate provisioning, why WinRM was chosen
  STORAGE.md                     # fleet-wide LVM layout, safety checks, growing a volume, adding workload-specific mounts
```

## Usage

Install pinned dependencies:

```bash
ansible-galaxy install -r requirements.yml -p roles -f
ansible-galaxy collection install -r requirements.yml -p collections -f
```

**If the VM doesn't exist yet**, create it from a template first — see
[docs/PROVISIONING.md](docs/PROVISIONING.md) before using any of these
on anything real (control-node prerequisites, decommission caveats, the
guest-customization asymmetry across the three hypervisors):

```bash
ansible-playbook playbooks/provision_vm_vcenter.yml -i inventories/staging/hosts.yml \
  -e vm_name=app06 -e vcenter_template_name=rhel9-golden-2026-01

ansible-playbook playbooks/provision_vm_hyperv.yml -i inventories/staging/hosts.yml \
  --limit hyperv01.gxp.example.internal -e vm_name=app07 \
  -e hyperv_template_export_path='D:\HyperV\Templates\rhel9-golden'

ansible-playbook playbooks/provision_vm_proxmox.yml -i inventories/staging/hosts.yml \
  -e vm_name=app08 -e proxmox_api_host=pve01.gxp.example.internal \
  -e proxmox_node=pve01 -e proxmox_template_vmid=9000 -e proxmox_vmid=180 \
  -e proxmox_storage=local-lvm -e proxmox_bridge=vmbr0
```

**Give the new host a DNS name** before doing anything else with it —
see [docs/PKI_DNS.md](docs/PKI_DNS.md) for the PTR-record caveat and why
this connects to a Windows DNS Server over WinRM rather than something
Linux-native:

```bash
ansible-playbook playbooks/register_dns.yml -i inventories/staging/hosts.yml \
  --limit dns01.gxp.example.internal \
  -e dns_registration_zone=gxp.example.internal \
  -e dns_registration_record_name=app06 \
  -e dns_registration_ip_address=10.20.10.55 \
  -e dns_registration_reverse_zone=10.20.10.in-addr.arpa
```

**First, once per newly provisioned host:** populate
`automation_user_ssh_public_keys` in `inventories/<env>/group_vars/all.yml`
with a real key, then create the account every other playbook will use —
authenticating as whatever initial admin account your provisioning process
(kickstart/cloud-init/Satellite, or the VM just created above) already put
on the host:

```bash
ansible-playbook playbooks/provision_automation_user.yml \
  -i inventories/staging/hosts.yml --limit newhost01 \
  -e bootstrap_admin_user=cloud-user --ask-pass --ask-become-pass
```

From here on, every playbook connects as that account, never root — see
`roles/automation_user` and the "No playbook ever connects as root"
assumption above.

**Rotating that account's SSH key later** is a three-stage process — see
[docs/AUTOMATION_USER_KEY_ROTATION.md](docs/AUTOMATION_USER_KEY_ROTATION.md)
before using this on anything real (the exclusive-authorized-keys
lockout risk, and why you switch your own active key before the finalize
step, not after):

```bash
# Stage 1 — generate (control node only, private key never leaves it):
ansible-playbook playbooks/rotate_automation_user_key.yml \
  -e automation_user_rotation_key_label=svc_ansible-2026-07-27

# ... add the new public key to automation_user_ssh_public_keys
# alongside the existing one, PR + merge + re-run site.yml/bootstrap.yml
# using your CURRENT key, then:

# Stage 2 — verify the new key actually works before touching the old one:
ansible-playbook playbooks/verify_automation_user_key.yml \
  -i inventories/staging/hosts.yml --limit app06.gxp.example.internal \
  -e automation_user_rotation_verify_key_path=artifacts/automation_user_keys/svc_ansible-2026-07-27/id_ed25519.key

# Stage 3 — switch your own active key, THEN remove the old one from
# automation_user_ssh_public_keys in a follow-up PR and re-run
# site.yml/bootstrap.yml again.
```

Dry-run against staging first, always:

```bash
ansible-playbook playbooks/site.yml -i inventories/staging/hosts.yml --check --diff
```

Then apply for real, against the environment you intend (production runs
should only ever happen via your CI/CD pipeline or change-controlled job,
never ad hoc from a laptop — see [docs/CHANGE_CONTROL.md](docs/CHANGE_CONTROL.md)):

```bash
ansible-playbook playbooks/site.yml -i inventories/production/hosts.yml
```

Generate compliance evidence (OpenSCAP report) without changing anything:

```bash
ansible-playbook playbooks/compliance_scan.yml -i inventories/production/hosts.yml
```

Turn a hardened host into a Docker workload host (only after it's already
been through `site.yml` at least once; only affects hosts in the
`docker_hosts` group — see [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)
before using this on anything real):

```bash
ansible-playbook playbooks/docker_host.yml -i inventories/staging/hosts.yml --check --diff
```

Stand up a vanilla kubeadm Kubernetes cluster (same "already hardened
hosts, own inventory groups" caveat — `k8s_control_plane` and
`k8s_workers`; read [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) first,
especially the HA-load-balancer and CNI caveats):

```bash
ansible-playbook playbooks/kubernetes_cluster.yml -i inventories/staging/hosts.yml --check --diff
```

Upgrade an existing cluster to a new Kubernetes version (explicitly
gated, one node at a time — see the playbook's own header comment):

```bash
ansible-playbook playbooks/kubernetes_upgrade.yml -i inventories/staging/hosts.yml \
  --tags upgrade -e k8s_upgrade_confirmed=true -e k8s_target_version_full=1.31.4
```

Turn a hardened host into a PostgreSQL database server (same
already-hardened-host caveat, `dbservers` group; read
[docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) first — replication role,
TLS, and backup are all things you need to actually configure, not just
accept the defaults for):

```bash
ansible-playbook playbooks/postgresql_server.yml -i inventories/staging/hosts.yml --check --diff
```

Turn a hardened EL9 host into a Microsoft SQL Server database server
(same already-hardened-host caveat, `mssql_hosts` group, not
`dbservers`; read [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) first —
this role will not accept Microsoft's EULA for you, and the sa password
and edition/PID are things you need to actually configure, not just
accept the defaults for):

```bash
ansible-playbook playbooks/mssql_server.yml -i inventories/staging/hosts.yml \
  -e mssql_accept_eula=true --check --diff
```

Stand up a standalone etcd cluster — no Kubernetes involved (`etcd_hosts`
group, not `k8s_nodes`' own `k8s_etcd`; read
[docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) first, especially the
comparison table against `kubernetes_node`'s own external-etcd option —
`etcd_release_sha256` and `etcd_cluster_token` both fail the run loudly
until you supply them):

```bash
ansible-playbook playbooks/etcd_cluster.yml -i inventories/staging/hosts.yml --check --diff
```

Stand up a temporary SMB/NFS file server (`file_share_hosts` group;
read [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md) first — age-based
cleanup is on by default, and `file_share_shares` is empty until you
define at least one share):

```bash
ansible-playbook playbooks/file_share_server.yml -i inventories/staging/hosts.yml --check --diff
```

Deploy NetBox on top of an already-provisioned Docker host (`netbox_hosts`
group, every host in it should also be in `docker_hosts`; read
[docs/APPS.md](docs/APPS.md) first — the Django secret key, superuser
password, and Redis passwords all fail the run loudly until you supply
them):

```bash
ansible-playbook playbooks/netbox.yml -i inventories/staging/hosts.yml --check --diff
```

Deploy a private Docker/OCI registry (same already-a-Docker-host
caveat, `registry_hosts` group; read [docs/APPS.md](docs/APPS.md)
first — every push and pull requires authentication, and images are
immutable by default):

```bash
ansible-playbook playbooks/registry.yml -i inventories/staging/hosts.yml --check --diff
```

Deploy standalone nginx (same already-a-Docker-host caveat,
`nginx_hosts` group; read [docs/APPS.md](docs/APPS.md) first — an empty
`nginx_vhosts` is valid and deploys a healthy but content-free stack,
useful as a starting point before you populate real vhosts):

```bash
ansible-playbook playbooks/nginx.yml -i inventories/staging/hosts.yml --check --diff
```

Request a TLS certificate from an internal Windows CA (ADCS) for
whichever of the above needs one — see
[docs/PKI_DNS.md](docs/PKI_DNS.md) for the manager-approval-pending
workflow and how to plug the result into `netbox_ssl_cert_src`/
`registry_ssl_cert_src`/`nginx_ssl_cert_src`/`postgresql_ssl_cert_src`:

```bash
ansible-playbook playbooks/request_certificate.yml -i inventories/staging/hosts.yml \
  --limit ca01.gxp.example.internal \
  -e certificate_enrollment_common_name=app06.gxp.example.internal \
  -e certificate_enrollment_ca_config='ca01\GxP-Enterprise-Issuing-CA' \
  -e certificate_enrollment_template=WebServer
```

Grow an existing LVM volume (explicitly gated — read
[docs/STORAGE.md](docs/STORAGE.md) first; this only extends within the
volume group's existing free space):

```bash
ansible-playbook playbooks/lvm_storage_grow.yml -i inventories/staging/hosts.yml \
  --limit app06.gxp.example.internal --tags grow \
  -e lvm_storage_grow_confirmed=true -e lvm_storage_grow_lv_name=lv_data \
  -e lvm_storage_grow_size=+20G
```

## Secrets

No secret ever belongs in plaintext in this repo. Use `ansible-vault` for
anything in `group_vars`/`host_vars` that needs a real value (Satellite
activation keys, AD join credentials, TLS client certs for log forwarding).
`.gitignore` already excludes `*.vault.yml`, `*.vault_pass`, and `*.key` —
keep it that way.
