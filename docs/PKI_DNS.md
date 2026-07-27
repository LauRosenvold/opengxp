# `provisioning/dns_registration` & `provisioning/certificate_enrollment` — DNS Records and Windows-PKI Certificates

## What these add

Two more roles in the `provisioning/` tier (see
[PROVISIONING.md](PROVISIONING.md) for why that tier exists at all):

- **`dns_registration`** creates/removes a forward (A) record, optional
  CNAME aliases, and an optional reverse (PTR) record on a Windows DNS
  Server — AD-integrated or standalone.
- **`certificate_enrollment`** requests (or revokes) a TLS certificate
  from an internal Windows CA (Active Directory Certificate Services),
  the "Windows PKI" most GxP estates already run for exactly this.

Both connect to a Windows target over WinRM, the same connection model
`provisioning/vm_provision` already uses for Hyper-V — see "Why WinRM,
not something Linux-native" below for why that's the deliberate choice
here too, not just consistency for its own sake.

## The lifecycle, extended

```
provisioning/vm_provision           create the VM (vCenter or Hyper-V)
        |
        v
provisioning/dns_registration       give it a resolvable name          <- NEW
        |
        v
playbooks/provision_automation_user.yml   one-time: create svc_ansible
        |
        v
playbooks/site.yml                  bootstrap + hardening + compliance evidence
        |
        v
server_roles/ playbooks             docker_host, kubernetes_node, postgresql_server, ...
        |
        v
apps/ playbooks                     netbox, registry, ...
        |
        v
provisioning/certificate_enrollment  issue a cert for whichever of the above needs TLS   <- NEW
```

`dns_registration` slots in early, right after the VM exists, because
almost everything downstream benefits from the host having a real DNS
name (SSH by hostname, the CA issuing a cert against that name, an app's
`ALLOWED_HOSTS`/`server_name`). `certificate_enrollment` slots in later,
whenever the specific workload that needs a certificate is actually
running — there's no fixed point in the lifecycle for it, unlike
`dns_registration`. Nothing here runs the next step automatically, same
"propose, don't silently chain" posture as `vm_provision`'s own output.

## `dns_registration` — `playbooks/register_dns.yml`

Runs `community.windows.win_dns_record` against a host in the
`dns_servers` inventory group (WinRM — see
`inventories/<env>/group_vars/dns_servers.yml`). One host's records per
invocation, driven by extra-vars, same style as
`provision_vm_vcenter.yml`/`provision_vm_hyperv.yml`. `dns_registration_state:
absent` removes what it created; `dns_registration_ip_address` is required
either way since the PTR record name is derived from it.

**PTR/reverse-zone limitation**: only handles a classful (`/24`-aligned)
IPv4 reverse zone, where the record name is just the address's last
octet — true for the common single-site case, not for a classless
(RFC 2317) delegated reverse zone or IPv6. Set
`dns_registration_create_ptr: false` and manage that record another way
if your reverse zone doesn't fit this shape.

## `certificate_enrollment` — `playbooks/request_certificate.yml`

Uses `certreq`/`certutil` (Microsoft's own long-standing CLI tools for
exactly this — no dedicated Ansible module for ADCS enrollment exists in
`community.windows`/`ansible.windows`) against a host in the
`ca_servers` inventory group.

**The private key never leaves the control node.** The role generates
the key and CSR locally with `openssl` (same idiom `apps/netbox` and
`apps/registry` already use for their own self-signed-certificate
generation), copies only the CSR — public by design — to the CA host,
submits it with `certreq -submit`, and copies only the issued
certificate back. Both land in
`certificate_enrollment_output_dir` (`artifacts/certificates/` by
default) — control-node-local files, `.key` and `.pem` already excluded
from git by `.gitignore`.

**Manager-approval workflow.** If the target certificate template
requires manager approval, `certreq -submit` reports the request as
"Taken Under Submission" instead of issuing immediately. The role
extracts and prints the request ID rather than failing — approve the
request in the Certification Authority console (or `certutil -resubmit
<id>` on the CA), then re-run the playbook with
`-e certificate_enrollment_request_id=<id>` to retrieve the now-issued
certificate without regenerating the key/CSR.

**Encoding.** Neither `certreq -submit` nor `-retrieve` pass `-binary`,
so the fetched `.cer` is Base64-encoded X.509 (PEM — readable text with
`-----BEGIN/END CERTIFICATE-----` headers), certreq's documented
default and what every `*_ssl_cert_src` consumer in this repo assumes.
Confirm with `openssl x509 -in <file>.cer -noout -text` the first time
you run this against a given CA — that command fails loudly on a
binary/DER file instead of silently breaking TLS on whatever consumes
it later.

**Plugging the result into an app.** This role does not deploy the
certificate to any managed target — same "propose, don't silently chain"
posture as everywhere else in this repo. Point the relevant role's own
pluggable cert vars at the fetched files, as a normal reviewed
`group_vars`/`host_vars` change (see
[CHANGE_CONTROL.md](CHANGE_CONTROL.md)):

| Role | Vars |
|---|---|
| `apps/netbox` | `netbox_ssl_cert_src` / `netbox_ssl_key_src` |
| `apps/registry` | `registry_ssl_cert_src` / `registry_ssl_key_src` |
| `server_roles/postgresql_server` | `postgresql_ssl_cert_src` / `postgresql_ssl_key_src` |

**CA chain.** The role also exports and fetches the issuing CA's own
certificate (`certutil -ca.cert`). In a multi-tier hierarchy (offline
root + issuing subordinate), this is the *issuing* CA's cert, not the
root's — `certutil -ca.chain` on the CA gives the full chain as a
`.p7b` if a relying party needs more than one level. Verify against your
actual hierarchy before trusting the fetched file blindly.

**Revocation.** `certificate_enrollment_state: revoked` (with
`certificate_enrollment_revoke_serial` set — see the issued cert's own
`.txt` summary, or `certutil -view` on the CA) runs `certutil -revoke`
and then `certutil -crl` to publish a new CRL, since revoking a
certificate in the CA database has no effect on relying parties until
the CRL (or OCSP responder, if you run one) actually reflects it — the
step a manual "just revoke it" runbook most often skips.

## Why WinRM, not something Linux-native

Both DNS zones and an ADCS CA are Windows-native concepts, and there's
no supported way to drive either from the control node without going
through Windows in some form. Three options existed:

1. **WinRM to the Windows host itself** (chosen) — `win_dns_record` for
   DNS, `certreq`/`certutil` for PKI. Reuses the exact connection model,
   collections, and control-node prerequisites (`pywinrm`)
   `provisioning/vm_provision` already established for Hyper-V — no new
   moving parts, no new class of thing to secure or troubleshoot.
2. **Dynamic DNS updates from Linux** (`nsupdate`, RFC 2116/2136,
   optionally GSS-TSIG for Kerberos-authenticated updates) — would avoid
   WinRM for the DNS half specifically, but introduces a second, separate
   trust/auth model (TSIG keys or a Kerberos keytab on the control node)
   for one role only, and does nothing for certificate issuance, which
   still needs a Windows-side answer regardless.
3. **ADCS's Certificate Enrollment Web Service** (SOAP/WS-Trust over
   HTTPS) — the "proper" way to enroll from a non-Windows client without
   touching WinRM at all, but there's no mature Ansible/Python tooling
   for it, and hand-rolling WS-Trust XML/NTLM-or-Kerberos-over-HTTP is a
   lot of fragile surface for this repo to own and maintain safely.

WinRM to the actual Windows box, using Microsoft's own CLI tools once
there, was the option that added the least new risk for the most
reliable outcome — consistent with this repo's general preference
(see `docs/PROVISIONING.md`'s Hyper-V rationale) for depending on
mature, official tooling over a smaller/cleverer alternative.

## Inventory: `dns_servers` / `ca_servers`

Two more Windows, WinRM-connected inventory groups, same pattern as
`hyperv_hosts` (see `inventories/<env>/group_vars/{dns_servers,
ca_servers}.yml`): not part of any `rhel9`/`rhel10`/`almalinux9`/
`almalinux10` or `gxp_critical`/`gxp_standard` group, and every
`hosts: all` playbook in `playbooks/` now excludes both alongside
`hyperv_hosts` and `localhost`
(`hosts: all:!hyperv_hosts:!dns_servers:!ca_servers:!localhost`) — if
you add a new `hosts: all` playbook later, remember all four exclusions,
not just the two `PROVISIONING.md` originally called out.

A DNS server and a CA can be the same physical/virtual host if your
estate is small enough that separating them isn't worth it — list that
one hostname under both `dns_servers` and `ca_servers`; Ansible merges
group membership normally, and as long as one account has rights to
both roles nothing here assumes they're different boxes. Microsoft's own
guidance (don't run ADCS on a domain controller) is a reason to keep
`ca_servers` pointed at a dedicated member server once your estate is
large enough to justify it, not something this repo enforces for you.

## What's intentionally out of scope

- **DHCP reservation management** — still not this repo's job (see
  `PROVISIONING.md`); `dns_registration` only manages DNS.
- **Building/maintaining the CA hierarchy itself** (root/issuing tier
  design, template creation and permissioning, CA disaster recovery) —
  assumed to already exist and be operated by whoever runs your AD/PKI
  estate, the same assumption `PROVISIONING.md` makes about the golden
  VM template.
- **Non-Windows DNS/CA** (BIND, HashiCorp Vault PKI, Let's Encrypt/ACME,
  ...) — explicitly asked for as Windows DNS + Windows PKI; a different
  backend is a different set of modules/collections entirely, not a
  small extension of these two roles.
- **Automatic certificate renewal** — `certificate_enrollment` requests
  or revokes on demand; tracking expiry and re-running before it happens
  is a periodic-review process (see `docs/VALIDATION.md`), not something
  this role schedules for you.
- **ECDSA / non-RSA keys** — `certificate_enrollment_key_algorithm`
  exists for `openssl req -newkey`'s benefit, but the role's own
  defaults/docs only assume RSA; verify your CA template supports
  whatever you point it at instead.
