# `provisioning/` — Creating the VM Itself, Before Any of This Repo's Other Tiers Apply

## Why yet another top-level directory

This repo already has three tiers, each narrower than the last:
`roles/` (compliance baseline) -> `server_roles/` (workload-enablement
platforms — see [SERVER_ROLES.md](SERVER_ROLES.md)) -> `apps/`
(applications on top of a platform — see [APPS.md](APPS.md)).

`provisioning/` sits **before all of them**, not below them. Every other
tier configures a host that already exists and is reachable over
SSH. `provisioning/vm_provision` does the opposite: it calls a
**hypervisor management API** (vCenter, Hyper-V, or Proxmox VE) to
create the VM in the first place. There is no managed Linux host for
Ansible to talk to yet — the "target" of this role is vCenter, a
Hyper-V host, or a Proxmox node, not the new VM itself.

That's a different enough kind of automation that it doesn't belong in
any of the existing tiers:

- It runs from the control node against a management API (vCenter,
  Proxmox VE) or against the *hypervisor* host via WinRM (Hyper-V) —
  never via SSH to the thing being created.
- It needs Python libraries on the **control node** (`pyvmomi`,
  `pywinrm`, `proxmoxer`/`requests`) rather than packages on a managed
  target — a first for this repo, worth calling out clearly rather than
  burying in a role's comments.
- Its output feeds the *start* of the lifecycle every other tier assumes
  already happened, not a step within it.

Two more roles share this tier for the same reason —
`provisioning/dns_registration` and `provisioning/certificate_enrollment`
also target infrastructure that isn't the managed host itself (a Windows
DNS Server, a Windows CA), over the same WinRM connection model this
role established for Hyper-V. See [PKI_DNS.md](PKI_DNS.md) for both.

## The full lifecycle

```
provisioning/vm_provision          create the VM (vCenter, Hyper-V, or Proxmox VE)
        |
        v
provisioning/dns_registration       give it a resolvable DNS name (see PKI_DNS.md)
        |
        v
playbooks/provision_automation_user.yml   one-time: create svc_ansible (roles/automation_user)
        |
        v
playbooks/site.yml                  bootstrap + hardening + compliance evidence (roles/)
        |
        v
server_roles/ playbooks             docker_host, kubernetes_node, postgresql_server, ...
        |
        v
apps/ playbooks                     netbox, registry, ...
        |
        v
provisioning/certificate_enrollment  request a Windows-PKI cert once something needs TLS (see PKI_DNS.md)
```

Nothing here runs the next step automatically — same "propose, don't
silently chain" posture as everywhere else in this repo. After a
successful create, `vm_provision` writes a plain-text summary (reported
IP, suggested `host_vars` starting point, a reminder of the next
commands to run) to `artifacts/provisioning/<vm_name>.txt` — **not** to
the checked-in inventory. Folding a new host into
`inventories/<env>/hosts.yml` is a deliberate, reviewed, separate change
(see [CHANGE_CONTROL.md](CHANGE_CONTROL.md)), same as any other inventory
edit; this role doesn't presume to make that change for you.

## `vm_provision`

One role, three task files (`tasks/vcenter.yml`, `tasks/hyperv.yml`,
`tasks/proxmox.yml`), dispatched by `provisioning_hypervisor` — set
explicitly at the play level in each of the three playbooks below rather
than left to the role's own default, so running the "wrong" playbook for
your hypervisor can't silently do nothing.

### vCenter — `playbooks/provision_vm_vcenter.yml`

Uses `community.vmware.vmware_guest` to clone a named template, running
on `localhost` (an explicit inventory entry — see "The `localhost`
inventory entry" below). Supports real Linux guest customization
(hostname, static IP, DNS) via VMware Tools/open-vm-tools, since vCenter
actually has that mechanism — see `vcenter_network_type`/
`vcenter_customization_*` in `defaults/main.yml`. `vm_state: absent`
decommissions (powers off and deletes) the VM — treat that with the same
care as any other irreversible action in this repo.

**Control-node prerequisite**: `pip install pyvmomi` (or your distro's
package) wherever `ansible-playbook` actually runs — the module fails to
even import without it. This is different in kind from every other
package-install task in this repo, which installs onto a *managed
target*, not the control node itself.

### Hyper-V — `playbooks/provision_vm_hyperv.yml`

Deliberately built on `ansible.windows` (mature, official, WinRM-based)
plus a templated PowerShell script, rather than a smaller/less
established Hyper-V-specific Ansible collection — the PowerShell itself
(`Import-VM -Copy -GenerateNewId` against an `Export-VM`'d template) is
Microsoft's own documented way to clone a VM, so this trades a bit of
verbosity for depending only on tooling with a long track record.

**Template prerequisite**: you need an already-`Export-VM`'d template
directory on (or reachable from) the Hyper-V host —
`hyperv_template_export_path`. This role doesn't create that template
for you; building and maintaining the golden VM is out of scope (see
below).

**Control-node prerequisite**: `pip install "pywinrm>=0.3.0"` wherever
`ansible-playbook` runs — required for the `winrm` connection plugin to
work at all.

**No native Linux guest customization.** Unlike vCenter, Hyper-V has no
built-in equivalent of VMware Tools' customization engine for a Linux
guest. The new VM boots with whatever the template itself defaults to —
commonly DHCP, plus cloud-init if the template has it configured. Real
hostname/network configuration happens on the VM's first actual Ansible
run (`provision_automation_user.yml` onward), once it's reachable over
SSH with *some* IP. If you need deterministic addressing before that
point, set `hyperv_static_mac_address` and pair it with a DHCP
reservation on your network side — this role stops at setting the MAC,
the reservation itself is your DHCP infrastructure's job.

**CredSSP / double-hop.** If `hyperv_template_export_path` points at a
network share rather than a path local to the Hyper-V host, `Import-VM`
reading across that share as the WinRM-authenticated user is a classic
"double-hop" scenario that NTLM/Kerberos alone can't satisfy — you'll
need `ansible_winrm_transport: credssp` (and CredSSP enabled on both
ends) in that case. A local path avoids the whole problem; prefer that
if you can.

### Proxmox VE — `playbooks/provision_vm_proxmox.yml`

Uses `community.proxmox.proxmox_kvm`/`proxmox_disk` against the Proxmox
REST API, running on `localhost` — architecturally closer to the vCenter
path than to Hyper-V's WinRM one, since Proxmox exposes a management API
directly rather than requiring an agent-style connection to the
hypervisor host itself.

**Explicit VMIDs, no auto-assignment.** Proxmox clones by numeric VMID,
not name (names aren't even guaranteed unique). `proxmox_template_vmid`
(the source) and `proxmox_vmid` (the new VM) are both required —
deliberately no "let Proxmox pick the next free ID" fallback, so the ID
that ends up in a change ticket is always the one an operator actually
typed, not something this role guessed on your behalf.

**Guest customization via cloud-init**, if the template has a cloud-init
drive already configured (`qm set <vmid> --ide2 <storage>:cloudinit`
when the template was built) — set `proxmox_cloudinit_ip_config` (and
optionally `_nameserver`/`_searchdomain`/`_sshkeys`/`_user`) to use it;
leave it empty to skip entirely and let the template's own defaults
apply, same posture as vCenter's `vcenter_network_type: dhcp` default.
This role does not add a cloud-init drive to a template that doesn't
already have one.

**Full vs. linked clones.** `proxmox_full_clone: true` is the default —
an independent copy of the template's disk. Setting it `false` creates a
linked clone instead: faster and smaller, but permanently dependent on
the source template's disk continuing to exist, which is a fragile
production posture this role doesn't default to.

**Extra disk.** `vm_extra_disk_gb > 0` attaches an additional disk at
`scsi1` via `community.proxmox.proxmox_disk` after the clone — verify
your template's boot disk is actually `scsi0` (the common default) if
you're getting a collision.

**Best-effort guest IP lookup**, via the QEMU Guest Agent's REST
endpoint — requires `agent: 1` set on the VM and the guest agent
actually running in the template (a decent template should already
carry both), and only works with API-token auth (password auth has no
simple bearer credential this role's direct API call can use). Same
"may not be up yet, check the UI directly" posture as the Hyper-V IP
lookup.

**Control-node prerequisite**: `pip install proxmoxer requests` wherever
`ansible-playbook` runs — `proxmoxer` is what actually talks to the PVE
API; the module fails to import without it.

## Decommissioning (`vm_state: absent`)

All three hypervisor paths support removing a VM — power off (Hyper-V:
`Stop-VM -TurnOff -Force`; vCenter: `force: true` on the delete call;
Proxmox: `force: true` stops then deletes) and delete its disks. This is
a genuinely destructive, hard-to-reverse action; nothing about
`vm_state: absent` prompts for confirmation beyond what running the
playbook itself already implies, so treat it with the same care as `git
reset --hard` or dropping a database table — confirm you have the right
`vm_name`/`proxmox_vmid` before running it, especially since VM names
aren't validated against anything except "did a VM with this exact name
(or, for Proxmox, this exact ID) exist."

## The `localhost` inventory entry

`inventories/<env>/hosts.yml` now has an explicit `localhost` entry
(`ansible_connection: local`) rather than relying on Ansible's ad-hoc
"implicit localhost" behavior — explicit so `group_vars/all.yml`'s
vCenter/Proxmox connection vars reliably apply to it, which implicit
localhost handling doesn't reliably guarantee.

**This has a consequence for every existing `hosts: all` playbook**:
`bootstrap.yml`, `hardening.yml`, `compliance_scan.yml`,
`patch_management.yml`, `lvm_storage_grow.yml`, and
`provision_automation_user.yml` were all updated to
`hosts: all:!hyperv_hosts:!dns_servers:!ca_servers:!localhost` —
otherwise those plays would now also try to CIS-harden,
Satellite-register, or AD-join whatever machine happens to be running
`ansible-playbook`, or any Windows DNS/CA server in inventory (see
[PKI_DNS.md](PKI_DNS.md)), which is obviously wrong. If you add a new
`hosts: all` playbook to this repo later, remember all four exclusions.

## What's intentionally out of scope

- **Cloud providers** (AWS/Azure/GCP/etc.) — this was specifically asked
  for as on-prem vCenter, Hyper-V, and Proxmox VE; a cloud provisioning
  path is a different set of collections/modules (and IAM/credential
  model) entirely, not a small extension of this role.
- **Building or maintaining the golden template itself** — all three
  paths assume a template (vCenter, Proxmox) or exported template VM
  (Hyper-V) already exists and is kept current (patched, hardened
  baseline pre-applied if you want a head start, correct guest
  tools/cloud-init installed). Template lifecycle — how often it's
  rebuilt, from what, by whom — is a separate, real process this repo
  doesn't attempt to own.
- **Automatic inventory registration** — see "The full lifecycle" above;
  folding a newly created host into the checked-in inventory is a
  deliberate, separate, reviewed change.
- **DHCP reservation management** — `vm_provision` can set a static MAC
  (Hyper-V) or static IP via guest customization (vCenter), but reserving
  that address in DHCP is your network infrastructure's job, not this
  role's. **DNS record creation and certificate issuance** used to be
  listed here too, but are now covered by
  `provisioning/dns_registration` and `provisioning/certificate_enrollment`
  — see [PKI_DNS.md](PKI_DNS.md).
- **Multi-VM/bulk provisioning orchestration** (spinning up N identical
  VMs, e.g. for a Kubernetes cluster's worker nodes, in one invocation) —
  this role provisions one VM per run; looping it (via `-e vm_name=...`
  in a shell loop, or an Ansible `loop` wrapping the role) is a usage
  pattern you can build on top, not something templated here.
