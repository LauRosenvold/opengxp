# Storage: LVM for Application and Data, Separate From the Root Filesystem

`roles/lvm_storage` is a fleet-wide baseline — every Linux host this repo
manages gets application (`/opt`) and data (`/data`) on their own LVM
logical volumes, separate from the root filesystem. It lives in `roles/`
(not `server_roles/`) because it's a base OS concern that applies
regardless of what the host ends up running, same tier as
`roles/ssh_hardening` or `roles/auditd_gxp`.

## Why a separate disk, not repartitioning root

This role **cannot** and does not attempt to reshape the root disk's own
partitioning after install — the same limitation
`roles/el10_baseline_hardening`'s `/tmp` mount-point check already
documents (Ansible can't safely repartition a live root filesystem
without unmounting things out from under a running system). Instead,
`lvm_storage` consumes an **additional block device**
(`lvm_storage_data_disk`, `/dev/sdb` by default) and builds a volume
group there.

This is exactly what `provisioning/vm_provision`'s `vm_extra_disk_gb` is
for (see [PROVISIONING.md](PROVISIONING.md)) — set it > 0 when creating a
new VM, and this role picks the disk up automatically on that host's
first `playbooks/bootstrap.yml` run. If you forgot, or the host predates
this role, `lvm_storage` warns clearly rather than silently doing
nothing (see "What happens without the disk" below) — attach the disk
and re-run.

## The default layout

| LV | Mount | Size | Purpose |
|---|---|---|---|
| `lv_app` | `/opt` | 30% of the VG | Application software — this repo's own `apps/netbox` and `apps/registry` already install to `/opt/<name>`, so they benefit automatically with no code changes |
| `lv_data` | `/data` | remaining free space | Application data |

Both `xfs` (RHEL/AlmaLinux's default and recommended filesystem).
`/data` gets `noexec` in its mount options — data files, never binaries,
so this is safe and deliberate. `/opt` does not, since it holds
executables.

## The safety check that matters most

Before creating anything, `tasks/checks.yml` inspects the target disk
with `blkid` and `pvs`. If it already has **any** signature that isn't
this role's own volume group, the run fails loudly rather than silently
repurposing a disk that might hold real data:

- Truly blank disk -> proceeds.
- Disk already an LVM PV belonging to `vg_data` (this role's own name) ->
  proceeds — this is what makes re-running the role idempotent.
- Anything else (a filesystem, a different VG, a partition table) ->
  fails, and only `lvm_storage_force: true` (a one-shot override you set
  and then unset, not a persistent setting) unblocks it.

Treat that override with the same care as any other destructive action
in this repo — confirm you actually know what's on that disk before
using it.

## SELinux and AIDE integration

- **SELinux** stays enforcing fleet-wide (`roles/selinux_enforce`) — a
  freshly mounted filesystem doesn't automatically inherit the context
  you'd expect just from its path, so `lvm_storage` sets one explicitly
  per mount point (`lvm_storage_selinux_context_by_mount`: `usr_t` for
  `/opt`, `var_t` for `/data`) via `community.general.sefcontext` +
  `restorecon`, rather than leaving that to chance and finding out via a
  confusing AVC denial later.
- **AIDE** (`roles/aide_fim`) watches `/opt` (application binaries should
  rarely change — full integrity check) and explicitly *excludes*
  `/data` (data files change constantly and legitimately; watching them
  would drown real signal in noise, the same reasoning
  `server_roles/docker_host` already applies to `/var/lib/docker`). This
  lives in `aide_fim`'s **own** default `aide_gxp_extra_paths` list, not
  in `lvm_storage` itself — `lvm_storage` runs early in
  `playbooks/bootstrap.yml`, `aide_fim` runs later in
  `playbooks/hardening.yml` and owns `/etc/aide.conf` and the
  package-install/`aide --init` ordering around it; reaching into that
  file from `lvm_storage` would risk exactly the kind of "who wrote this
  file first" ordering conflict this repo tries hard to avoid elsewhere.
  The defaults are correct and harmless even on a host where
  `lvm_storage` never actually ran (AIDE just watches whatever's really
  at `/opt` on the root filesystem, and finds nothing at `/data`).

## What happens without the disk

`lvm_storage_enabled: true` is the fleet-wide default, but the role
doesn't hard-fail a host that has no second disk attached — it warns
clearly instead, every run, until you either attach the disk or set
`lvm_storage_enabled: false` for that host to silence the warning
deliberately. A hard failure here would break `playbooks/site.yml` for
every host that predates this role or was provisioned without
`vm_extra_disk_gb`, which is far more disruptive than a persistent,
honest warning.

## Adding workload-specific mounts

`lvm_storage_volumes` is a plain list — the fleet-wide baseline
(`lv_app`/`lv_data`) is `roles/lvm_storage`'s own default, but a
specific host group can **redefine the whole list** (group_vars
overriding a role default, standard Ansible precedence — see
`inventories/<env>/group_vars/gxp_critical.yml`'s own pattern for
per-tier overrides) to add more, workload-specific volumes using the
exact same mechanism. For example, in
`inventories/<env>/group_vars/dbservers.yml`:

```yaml
lvm_storage_volumes:
  - { name: lv_app,  mount_point: /opt,           size: "20%VG",  fs_type: xfs, mount_options: "defaults,nodev,nosuid" }
  - { name: lv_pgsql, mount_point: /var/lib/pgsql, size: "60%VG", fs_type: xfs, mount_options: "defaults,nodev,nosuid" }
  - { name: lv_data, mount_point: /data,           size: "100%FREE", fs_type: xfs, mount_options: "defaults,nodev,nosuid,noexec" }
```

`server_roles/postgresql_server` already writes to `/var/lib/pgsql/...`
unconditionally — if that path happens to be a separate LVM mount by the
time that role runs (it does, since `lvm_storage` runs in
`bootstrap.yml`, well before any `server_roles/apps` playbook), the role
works exactly the same, just onto dedicated storage. The same pattern
applies to `docker_hosts` (`/var/lib/docker`) or any other role with a
fixed, well-known path. No role code changes needed — this is entirely
an inventory/group_vars-level decision, same as everything else this
repo treats as "config, not code."

## Growing a volume

`playbooks/lvm_storage_grow.yml` extends an existing LV and its
filesystem in one step (`community.general.lvol`'s `resizefs: true`,
which handles `xfs_growfs`/`resize2fs` as appropriate for the
filesystem type). Explicitly gated the same way
`server_roles/kubernetes_node`'s upgrade path and `roles/fips_mode` are —
`--tags grow` plus `-e lvm_storage_grow_confirmed=true` required, because
this is exactly the kind of action that should never happen as a side
effect of a routine run:

```bash
ansible-playbook playbooks/lvm_storage_grow.yml \
  -i inventories/staging/hosts.yml --limit app06.gxp.example.internal \
  --tags grow \
  -e lvm_storage_grow_confirmed=true \
  -e lvm_storage_grow_lv_name=lv_data \
  -e lvm_storage_grow_size=+20G
```

This only grows the LV within the volume group's existing free space —
if `vg_data` itself is out of room, attach a larger or additional disk
and extend the VG onto it first (`vgextend`, or
`community.general.lvg`'s `pvs:` list) before this playbook has anything
to work with.

## What's intentionally out of scope

- **Repartitioning/resizing the root disk.** Set the *initial* LVM
  layout you want (root/swap/var/tmp sizing) at kickstart/image-template
  time — same posture `roles/el10_baseline_hardening` already documents
  for `/tmp`.
- **Shrinking a volume.** LVM/XFS make growing safe and easy; shrinking
  an XFS filesystem isn't supported at all (XFS has no shrink), and
  shrinking most filesystems safely requires unmounting and is far
  riskier than this role attempts to automate.
- **Encryption at rest (LUKS)** for these volumes — a real, valid GxP
  consideration for a host storing sensitive data, but a different
  variable set (passphrase/key management, boot-time unlock) this role
  doesn't wire up. Layer LUKS underneath the PV yourself if you need it;
  `lvm_storage` doesn't know or care what's beneath the volume group.
- **RAID, multi-disk striping, or thin provisioning** — `lvm_storage`
  builds a plain volume group from whatever disk(s) you list in
  `lvm_storage_data_disk`; redundancy/performance engineering across
  multiple physical disks is a storage-design decision made before this
  role ever runs, not something it attempts to configure.
- **Non-XFS filesystems.** `ext4` (or others) will generally work if you
  set `fs_type` accordingly, but the grow path's `resizefs: true`
  behavior and this documentation's assumptions are written against
  `xfs`, this repo's actual default.
