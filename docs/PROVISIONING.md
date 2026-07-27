# `provisioning/` — Creating the VM Itself, Before Any of This Repo's Other Tiers Apply

## Why yet another top-level directory

This repo already has three tiers, each narrower than the last:
`roles/` (compliance baseline) -> `server_roles/` (workload-enablement
platforms — see [SERVER_ROLES.md](SERVER_ROLES.md)) -> `apps/`
(applications on top of a platform — see [APPS.md](APPS.md)).

`provisioning/` sits **before all of them**, not below them. Every other
tier configures a host that already exists and is reachable over
SSH. `provisioning/vm_provision` does the opposite: it calls a
**hypervisor management API** (vCenter or Hyper-V) to create the VM in
the first place. There is no managed Linux host for Ansible to talk to
yet — the "target" of this role is vCenter or a Hyper-V host, not the
new VM itself.

That's a different enough kind of automation that it doesn't belong in
any of the existing tiers:

- It runs from the control node (vCenter) or against the *hypervisor*
  host via WinRM (Hyper-V) — never via SSH to the thing being created.
- It needs Python libraries on the **control node** (`pyvmomi`,
  `pywinrm`) rather than packages on a managed target — a first for this
  repo, worth calling out clearly rather than burying in a role's
  comments.
- Its output feeds the *start* of the lifecycle every other tier assumes
  already happened, not a step within it.

## The full lifecycle

```
provisioning/vm_provision          create the VM (vCenter or Hyper-V)
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

One role, two task files (`tasks/vcenter.yml`, `tasks/hyperv.yml`),
dispatched by `provisioning_hypervisor` — set explicitly at the play
level in each of the two playbooks below rather than left to the
role's own default, so running the "wrong" playbook for your hypervisor
can't silently do nothing.

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

## Decommissioning (`vm_state: absent`)

Both hypervisor paths support removing a VM — power off (Hyper-V:
`Stop-VM -TurnOff -Force`; vCenter: `force: true` on the delete call) and
delete its disks. This is a genuinely destructive, hard-to-reverse
action; nothing about `vm_state: absent` prompts for confirmation beyond
what running the playbook itself already implies, so treat it with the
same care as `git reset --hard` or dropping a database table — confirm
you have the right `vm_name` before running it, especially since VM
names aren't validated against anything except "did a VM with this exact
name exist."

## The `localhost` inventory entry

`inventories/<env>/hosts.yml` now has an explicit `localhost` entry
(`ansible_connection: local`) rather than relying on Ansible's ad-hoc
"implicit localhost" behavior — explicit so `group_vars/all.yml`'s
vCenter connection vars reliably apply to it, which implicit localhost
handling doesn't reliably guarantee.

**This has a consequence for every existing `hosts: all` playbook**:
`bootstrap.yml`, `hardening.yml`, `compliance_scan.yml`,
`patch_management.yml`, and `provision_automation_user.yml` were all
updated to `hosts: all:!hyperv_hosts:!localhost` — otherwise those plays
would now also try to CIS-harden, Satellite-register, or AD-join
whatever machine happens to be running `ansible-playbook`, which is
obviously wrong. If you add a new `hosts: all` playbook to this repo
later, remember both exclusions.

## What's intentionally out of scope

- **Cloud providers** (AWS/Azure/GCP/etc.) — this was specifically asked
  for as on-prem vCenter and Hyper-V; a cloud provisioning path is a
  different set of collections/modules (and IAM/credential model)
  entirely, not a small extension of this role.
- **Building or maintaining the golden template itself** — both paths
  assume a template (vCenter) or exported template VM (Hyper-V) already
  exists and is kept current (patched, hardened baseline pre-applied if
  you want a head start, correct guest tools installed). Template
  lifecycle — how often it's rebuilt, from what, by whom — is a
  separate, real process this repo doesn't attempt to own.
- **Automatic inventory registration** — see "The full lifecycle" above;
  folding a newly created host into the checked-in inventory is a
  deliberate, separate, reviewed change.
- **DHCP reservation management, DNS record creation** — `vm_provision`
  can set a static MAC (Hyper-V) or static IP via guest customization
  (vCenter), but reserving that address in DHCP or creating the matching
  DNS record is your network infrastructure's job, not this role's.
- **Multi-VM/bulk provisioning orchestration** (spinning up N identical
  VMs, e.g. for a Kubernetes cluster's worker nodes, in one invocation) —
  this role provisions one VM per run; looping it (via `-e vm_name=...`
  in a shell loop, or an Ansible `loop` wrapping the role) is a usage
  pattern you can build on top, not something templated here.
