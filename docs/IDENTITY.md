# Active Directory / IdM Integration

Three roles cooperate here, each owning a different piece:

| Role | Owns |
|---|---|
| `roles/identity_sssd` | Domain join, who can log in at all (`identity_allowed_groups`) |
| `roles/user_access_gxp` | What a logged-in user can do — sudo, via local **or** directory groups |
| `roles/ssh_hardening` | Optional Kerberos SSO for SSH (`GSSAPIAuthentication`) |

None of this runs until `identity_backend_enabled: true` is set (see
`inventories/<env>/group_vars/all.yml`) — every host defaults to local
accounts only, consistent with every other "off until you configure it"
default in this repo.

## Joining the domain

`roles/identity_sssd` handles both AD (`realm join` via `realmd`) and
IdM/FreeIPA (`ipa-client-install`), selected by `identity_backend`. Join
credentials are vault-only, never plaintext — a missing credential fails
the run loudly rather than silently skipping the join (see that role's
`assert` task).

**Prerequisites this repo does not manage:**

- **DNS.** The host's resolvers must be able to find the domain's SRV
  records (`_ldap._tcp.<domain>`, `_kerberos._tcp.<domain>`, etc.) —
  however this estate hands out DNS (DHCP, cloud-init, kickstart), that's
  outside Ansible's remit here. A join fails immediately and obviously if
  this is wrong, which is at least a clear signal.
- **Clock sync.** Kerberos tickets are only valid within a tight clock
  skew of the KDC (typically 5 minutes). `roles/chrony_time` already
  handles this fleet-wide for GxP record-integrity reasons, which
  happens to also make Kerberos work — but it's worth knowing *why* time
  sync matters twice over on a domain-joined host.
- **Firewalld**: no changes needed. AD/Kerberos/LDAP traffic (DNS,
  Kerberos 88, LDAP 389/636, SMB 445) is all outbound from the Linux
  client to the DC — `roles/firewalld_baseline`'s default-deny zone only
  restricts *inbound* connections, so it doesn't need a rule for this.

## Who can log in: `identity_allowed_groups`

SSSD's `access_provider=simple` with `simple_allow_groups` — an explicit
allow-list, fail-closed. An empty list (the default) means **no domain
user can log in**, not "everyone can." Populate it with real AD/IdM group
names once you have them.

`identity_sssd` verifies each group in the list actually resolves via
`getent group` after the join and warns (doesn't fail) if one doesn't —
a non-resolving group in `simple_allow_groups` is a silent no-op, not an
error, so this check exists specifically to catch a typo or a
qualified-names mismatch before it becomes "nobody in gxp-linux-admins
can log in and nobody knows why."

## Who can sudo: directory-sourced groups in `roles/user_access_gxp`

`user_access_sod_groups` (the same segregation-of-duties mechanism
covering local groups like `gxp-db-admins`) now accepts
`source: directory` entries — an AD/IdM security group instead of a
locally-created one:

```yaml
user_access_sod_groups:
  - name: gxp-linux-admins
    source: directory
    commands:
      - ALL
```

`source: directory` skips the `ansible.builtin.group` creation task
(creating a same-named local group would shadow the directory one
instead of using it) and just writes the sudoers.d entry referencing
`%gxp-linux-admins` — the same template as every local SoD group, since
sudoers doesn't care whether a `%group` resolves via `/etc/group` or via
SSSD. This role verifies directory-sourced groups resolve the same way
`identity_sssd` verifies `identity_allowed_groups`, and for the same
reason: a sudoers entry referencing a group that doesn't resolve is
silently dead, not an error.

**This is a genuinely different mechanism from AD-schema-based sudo**
(SSSD's `sudo_provider = ad`/`ldap`, which reads `sudoRole` objects
defined *in* the directory via a schema extension). That approach
centralizes sudo policy in AD itself, which some estates prefer — but it
requires AD schema work most orgs haven't done. This repo's approach
(map an existing, ordinary AD *security* group to a local sudoers.d file
via Ansible) needs zero AD-side schema changes and keeps sudo policy
version-controlled and change-reviewed in this repo like everything
else — the trade-off is that a policy change means an Ansible run, not
an AD-side edit that takes effect everywhere instantly. Pick based on
which trade-off your org prefers; the schema-based approach is a
documented gap, not implemented here.

### The qualified-names / spaces-in-group-names gotcha

`identity_use_fully_qualified_names` (default `false`) controls whether
SSSD resolves `jdoe`/`gxp-linux-admins` or
`jdoe@corp.example.internal`/`gxp-linux-admins@corp.example.internal`.
Whatever you write in `identity_allowed_groups` and
`user_access_sod_groups`' directory entries **must match that setting**,
or the group won't resolve (see the verification warnings above).

Separately: sudoers' `%group` syntax doesn't handle unquoted spaces, and
AD happily allows spaces in group names (e.g. "Linux Admins"). Sudoers
does support `%"Group Name"` quoting, but this repo's sudoers template
doesn't attempt that escaping — simplest fix is to not put spaces in AD
groups you intend to map to sudo (`gxp-linux-admins`, not
"Linux Admins"), which is entirely within your control when creating the
group in the first place.

## ID mapping: `identity_ad_id_mapping_enabled`

The single most consequential AD-integration decision, and one this repo
can't make for you:

- **`true` (default):** SSSD derives a POSIX UID/GID from each object's
  SID itself — a stable hash, same result on every SSSD-joined host,
  zero AD-side schema work. Right default for the common case: an AD
  forest that has never had the "Identity Management for Unix"/RFC2307
  schema extension applied.
- **`false`:** use `uidNumber`/`gidNumber` attributes already populated
  in AD. Only correct if your AD team has actually done that — if they
  haven't, affected users get no usable UID and can't log in at all, a
  much worse failure mode than just picking the wrong mapping mode by
  accident. Confirm with whoever runs AD before flipping this.

Why it matters for GxP specifically: a UID has to mean the same person
consistently across every host's audit trail (`roles/auditd_gxp`,
`roles/user_access_gxp`'s sudo logging) for that trail to be worth
anything under review. SSSD's SID-based mapping guarantees that
consistency automatically; the POSIX-attribute mode only guarantees it if
AD's attributes are populated correctly and consistently, which is a
people/process problem outside this repo's control.

## GPO access control: `identity_ad_gpo_access_control`

SSSD can partially honor Windows Group Policy — specifically the
"Allow/Deny log on locally" and "...through Remote Desktop Services"
rights. Default is `permissive` (SSSD's own default: evaluate and log,
don't enforce). `enforcing` actually denies logon per GPO, which only
makes sense if your AD team already scopes GPOs to these specific Linux
hosts — unusual, since most estates manage Linux access purely through
the AD-group mechanism this document already covers
(`identity_allowed_groups`). `disabled` skips GPO evaluation outright.
Leave at `permissive` or set `disabled` unless you specifically know you
want `enforcing`.

## Kerberos SSO for SSH: `ssh_gssapi_auth_enabled`

Off by default. `realmd`'s domain join already deposits a working
`/etc/krb5.keytab` — there's no separate keytab-distribution step needed
— but this is still opt-in rather than automatic on every domain-joined
host, because it only actually works once DNS and clock sync (see
"Prerequisites" above) are confirmed good, and a wrong assumption there
is a confusing SSH auth failure, not a clean one. Turn it on
per-`gxp_tier`/host once you've verified both.

## Offline access / break-glass

If the DC/IdM server is briefly unreachable, `identity_cache_credentials`
(default on, 3-day expiration) lets already-authenticated domain users
keep working. If the domain itself is unreachable for longer than that —
or a host is being stood up before the join has happened — local
break-glass accounts (`roles/user_access_gxp`'s
`user_access_break_glass_accounts`) are the documented fallback, same as
they are for any other identity-outage scenario. Nothing here changes
that story; it's worth knowing both exist for the same underlying
concern (this host's admin access shouldn't have a single point of
failure in one directory service).

## What's intentionally out of scope

- **AD-schema-based sudo** (`sudo_provider = ad`/`ldap`, `sudoRole`
  objects) — see the callout above.
- **GPO enforcement beyond logon rights** — Windows GPOs cover a huge
  surface (software deployment, registry policy, etc.) that has no Linux
  equivalent at all; only the logon-rights subset SSSD itself understands
  is even a candidate for `identity_ad_gpo_access_control`.
- **Multi-forest/multi-domain trust configuration, cross-forest group
  resolution** — `identity_domain` is a single domain; a more complex
  trust topology is a `sssd.conf` hand-tuning exercise beyond what this
  role templates.
- **Any AD-side administration** (creating the security groups
  themselves, DNS, schema, GPOs, trust relationships) — entirely out of
  scope for an Ansible repo that manages Linux hosts, not Windows
  infrastructure.
