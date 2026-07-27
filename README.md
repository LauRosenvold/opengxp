# RHEL 9/10 & AlmaLinux 10 Configuration & Hardening — GxP Enterprise Estate

Ansible automation for configuring and securing a large fleet of RHEL 9,
RHEL 10, and AlmaLinux 10 servers in a GxP-regulated environment
(GMP/GLP/GCP). This repo
handles the **technical control** layer of computerized system validation —
it does not replace the procedural controls (change control tickets, IQ/OQ/PQ
sign-off, periodic review) your Quality organization must run around it.
See [docs/VALIDATION.md](docs/VALIDATION.md) for how the two fit together.

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
  `inventories/<env>/group_vars/almalinux10.yml` to skip
  `roles/satellite_registration` for that group.
- **No playbook ever connects as root.** Every playbook here connects and
  `become`s-from a dedicated local account (`automation_user_name` in
  `group_vars/all.yml`, `svc_ansible` by default) created by
  `roles/automation_user` — key-auth only, locked password, NOPASSWD sudo,
  every action captured by `roles/user_access_gxp`'s sudo I/O logging. That
  account has to exist before anything else can run, which is what
  `playbooks/provision_automation_user.yml` is for — see "Usage" below.
- **Workload-enablement roles (Docker, Kubernetes, PostgreSQL, ...) live
  in `server_roles/`, not `roles/`.** Different change-control weight,
  different cadence, not part of `playbooks/site.yml` — see
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md).
- **Kubernetes means vanilla kubeadm**, not OpenShift/k3s/a managed cloud
  offering. SELinux stays enforcing (no `setenforce 0`) regardless of
  container runtime (containerd or CRI-O, pluggable), CNI (Calico or
  Flannel, pluggable) and any HA control-plane load balancer are
  bring-your-own, MetalLB (bare-metal `LoadBalancer` Services) is
  available but off by default and L2-mode-only, and version changes go
  through an explicitly-gated upgrade playbook, never routine patching —
  see [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s `kubernetes_node`
  section before using this on anything real.
- **PostgreSQL means PGDG packages** (not the OS's AppStream module),
  with pgAudit on by default for a GxP-relevant SQL-level audit trail.
  Replication is basic streaming primary/replica with no automatic
  failover, and WAL archiving is a mechanism, not a backup strategy — see
  [docs/SERVER_ROLES.md](docs/SERVER_ROLES.md)'s `postgresql_server`
  section before using this on anything real.
- **Application deployments (NetBox, a private registry, ...) live in
  `apps/`, a third tier below `server_roles/`.** An application runs *on
  top of* a `server_roles/` platform (e.g. `apps/netbox` and
  `apps/registry` both on a `server_roles/docker_host`) rather than
  replacing it — see [docs/APPS.md](docs/APPS.md). Same "not part of
  `site.yml`, own inventory group, own change-control weight" pattern,
  one step further down.
- **The private registry (`apps/registry`) requires authentication for
  every push and pull, always, and images are immutable by default**
  (deletion off, no garbage collection, until you set a documented
  retention policy). It terminates TLS itself — no reverse proxy needed,
  unlike NetBox. See [docs/APPS.md](docs/APPS.md)'s `registry` section,
  including how to point other roles' registry-mirror vars at it once
  it's running.
- **VM provisioning (`provisioning/vm_provision`) is a fourth tier that
  runs *before* everything above — it creates the VM itself** against
  vCenter or Hyper-V, before any managed Linux host exists for the rest
  of this repo to touch. On-prem only (no cloud provider support), and it
  needs Python libraries on the **control node** (`pyvmomi`, `pywinrm`),
  not a managed target — the first dependency of that kind in this repo.
  It does not register a newly created host into the checked-in
  inventory for you; that stays a deliberate, reviewed change. See
  [docs/PROVISIONING.md](docs/PROVISIONING.md), including why several
  existing `hosts: all` playbooks changed to exclude `hyperv_hosts` and
  `localhost`.
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
  site.yml                    # full build: common -> hardening -> compliance evidence
  bootstrap.yml                # first-boot: automation user + Satellite registration + baseline
  hardening.yml                # security roles only, safe to re-run on a schedule
  patch_management.yml         # controlled patch window playbook
  compliance_scan.yml          # OpenSCAP evidence generation
  docker_host.yml              # workload enablement, NOT part of site.yml — see SERVER_ROLES.md
  kubernetes_cluster.yml       # vanilla kubeadm cluster setup, NOT part of site.yml
  kubernetes_upgrade.yml       # explicitly-gated version upgrade, --tags upgrade required
  postgresql_server.yml        # workload enablement, NOT part of site.yml
  netbox.yml                   # application deployment, NOT part of site.yml — see APPS.md
  registry.yml                 # application deployment, NOT part of site.yml — see APPS.md
  provision_vm_vcenter.yml     # creates a VM from a vCenter template — runs BEFORE any of the above, see PROVISIONING.md
  provision_vm_hyperv.yml      # creates a VM from a Hyper-V template — runs BEFORE any of the above, see PROVISIONING.md
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
  kubernetes_node/                # vanilla kubeadm, multi-version, pluggable runtime (containerd/CRI-O) + CNI (Calico/Flannel) + MetalLB
  postgresql_server/               # PGDG PostgreSQL, pgAudit, TLS, basic primary/replica streaming replication
apps/                           # application deployments on top of server_roles/ — separate again, see APPS.md
  netbox/                         # NetBox Docker Compose stack: nginx TLS proxy, external-DB-recommended, containerized Redis
  registry/                       # Private Docker/OCI registry: native TLS, mandatory htpasswd auth, immutable images by default
provisioning/                   # creates the VM itself, before any tier above applies — see PROVISIONING.md
  vm_provision/                    # vCenter (community.vmware) or Hyper-V (ansible.windows + PowerShell), clone from template
molecule/
  ssh_hardening/                 # example molecule scenario — pattern to replicate per role
docs/
  VALIDATION.md                  # how this repo maps to computerized system validation
  CHANGE_CONTROL.md              # branching/approval model expected around this repo
  BASELINE_MAPPING.md            # CIS control -> role/task cross-reference
  SERVER_ROLES.md                # why server_roles/ is separate from roles/, and per-role specifics
  APPS.md                        # why apps/ is a third tier below server_roles/, plus netbox/registry specifics
  IDENTITY.md                    # AD/IdM join, SSH/sudo group mapping, ID mapping choice, GSSAPI SSH
  PROVISIONING.md                # why provisioning/ runs before every other tier, vCenter/Hyper-V specifics
  STORAGE.md                     # fleet-wide LVM layout, safety checks, growing a volume, adding workload-specific mounts
```

## Usage

Install pinned dependencies:

```bash
ansible-galaxy install -r requirements.yml -p roles -f
ansible-galaxy collection install -r requirements.yml -p collections -f
```

**If the VM doesn't exist yet**, create it from a template first — see
[docs/PROVISIONING.md](docs/PROVISIONING.md) before using either of
these on anything real (control-node prerequisites, decommission
caveats, the guest-customization asymmetry between the two hypervisors):

```bash
ansible-playbook playbooks/provision_vm_vcenter.yml -i inventories/staging/hosts.yml \
  -e vm_name=app06 -e vcenter_template_name=rhel9-golden-2026-01

ansible-playbook playbooks/provision_vm_hyperv.yml -i inventories/staging/hosts.yml \
  --limit hyperv01.gxp.example.internal -e vm_name=app07 \
  -e hyperv_template_export_path='D:\HyperV\Templates\rhel9-golden'
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
