# CIS Benchmark Domain -> Role Cross-Reference

This is a domain-level map, not a control-by-control checklist (the CIS
Benchmark PDF and the `ansible-lockdown` role's own `defaults/main.yml`
are the source of truth for individual control IDs). Use this to answer
"which role is responsible for section N of the benchmark" during audit
prep, and to see where RHEL9, RHEL10, and AlmaLinux 10 diverge in this
repo. RHEL10 and AlmaLinux10 are listed as one column because they run
the identical `el10_baseline_hardening` fallback role — there is no
per-distro difference to call out at this domain level.

| CIS Benchmark domain                              | RHEL 9 (control host)                         | RHEL 10 / AlmaLinux 10 (until RHEL10-CIS is GA)  |
|-----------------------------------------------------|-------------------------------------------------|---------------------------------------------------|
| 1. Initial setup (filesystem, partitions, modules) | `ansible-lockdown.RHEL9-CIS` (section1)         | `el10_baseline_hardening` (filesystem/module tasks) |
| 1. Software updates / GPG / repos                  | `ansible-lockdown.RHEL9-CIS` + `satellite_registration` | same (`satellite_registration` is skippable per-group via `satellite_registration_enabled` — see almalinux10.yml) |
| 2. Services (disable unused)                        | `ansible-lockdown.RHEL9-CIS` (section2)         | `el10_baseline_hardening` (service tasks)        |
| 3. Network (sysctl, kernel params)                  | `ansible-lockdown.RHEL9-CIS` (section3)         | `el10_baseline_hardening` (sysctl tasks)         |
| 4. Logging & auditing                               | `auditd_gxp`, `logging_forward` (supersede the lockdown role's own section4 defaults — GxP event set is broader) | same, always these roles regardless of major version or distro |
| 5. Access, authentication, authorization (SSH, PAM, sudo) | `ssh_hardening`, `user_access_gxp` (supersede lockdown section5 for SSH/sudo specifics) | same |
| 5. Password/account policy                           | `ansible-lockdown.RHEL9-CIS` (section5 pwquality/faillock) | `el10_baseline_hardening` (pam tasks)            |
| 6. System maintenance (permissions, file integrity)  | `aide_fim` + `ansible-lockdown.RHEL9-CIS` (section6 perms) | `aide_fim` + `el10_baseline_hardening` (perms tasks) |
| SELinux                                              | `selinux_enforce` (independent of major version/distro — same role for all) | same |
| Firewall                                             | `firewalld_baseline` (independent of major version/distro) | same |
| Time sync                                            | `chrony_time` (independent of major version/distro)    | same |
| Compliance evidence (OpenSCAP)                       | `compliance_scan` against `ssg-rhel9-ds.xml`     | `compliance_scan` against `ssg-rhel10-ds.xml`, falling back to `ssg-almalinux10-ds.xml` if the RHEL-named file isn't present — see `roles/compliance_scan/defaults/main.yml` |

## Why some domains are handled outside the lockdown role at all

`auditd_gxp`, `ssh_hardening`, and `user_access_gxp` intentionally
override the equivalent lockdown-role sections rather than relying on
their defaults, because the GxP requirement set is a superset of generic
CIS in these three areas specifically:

- **Audit**: CIS section 4 gets you "auditd is running and rotates logs
  sanely." GxP needs a specific event taxonomy (who changed what
  record-relevant configuration, when, authenticated as whom) — see
  `roles/auditd_gxp/templates/audit_gxp.rules.j2` and
  [VALIDATION.md](VALIDATION.md).
- **SSH**: CIS gets you sane ciphers/protocol version. `ssh_hardening`
  additionally enforces the login banner text your Quality/Legal team
  signs off on, which CIS has no opinion about.
- **Access**: `user_access_gxp` adds segregation-of-duties groups and
  break-glass account handling that has no CIS equivalent at all — it's
  a GxP/SOX-style control, not a security-benchmark control. Those SoD
  groups can be local *or* AD/IdM directory groups (`source: directory`)
  once `roles/identity_sssd` has joined a domain — see
  [IDENTITY.md](IDENTITY.md).

Note that `roles/automation_user` is deliberately *not* in this list. It
grants NOPASSWD sudo across the board to a single account, which looks
like the opposite of segregation-of-duties at first glance — but it's a
different kind of account for a different purpose: `user_access_gxp`'s
groups are for human admins with a job-appropriate command subset;
`automation_user` is the one machine account that legitimately needs to
touch anything, because that's what configuration management is. The
trade-off is offset, not ignored: key-only auth, a locked password, and
every single command it runs captured by `user_access_gxp`'s sudo I/O
logging. Don't add human users to that account's group, and don't widen
any SoD group's command list "to match automation" — they're solving
different problems.

Where lockdown-role defaults and these roles could both touch the same
file (e.g. `/etc/ssh/sshd_config`), execution order in
`playbooks/hardening.yml` puts `cis_hardening` **before** `ssh_hardening`,
`auditd_gxp`, and `user_access_gxp`, so the GxP-specific roles are the
final, authoritative write.

## Why `almalinux10` is a separate inventory group, not folded into `rhel10`

Every role in this repo keys off OS major version or plain package/service
presence, never `ansible_distribution`, so nothing technically stops you
from putting AlmaLinux hosts in the `rhel10` group. It's kept separate
anyway because:

- An inventory listing should tell you the real OS/support vendor at a
  glance — that matters for audit prep and for anyone triaging a CVE that
  turns out to be Red-Hat-support-channel-specific.
- `satellite_registration_enabled` and any future AlmaLinux-specific
  variable belongs on its own group_vars file, not mixed into rhel10's.

If you later add AlmaLinux 9 or Rocky Linux, follow the same pattern: a
new inventory group, its own `group_vars/<group>.yml` pointing
`cis_hardening_role_name` at whichever engine actually applies, rather
than overloading an existing RHEL-named group.
