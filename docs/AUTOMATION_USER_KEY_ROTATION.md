# Rotating `automation_user_name`'s SSH Key

`roles/automation_user` already enforces key-only auth (locked password,
`ansible.posix.authorized_key` with `exclusive: true` — see
`roles/automation_user/defaults/main.yml`) for the one account every
playbook in this repo connects and `become`s-from. This document is
about *changing* that key safely: `playbooks/rotate_automation_user_key.yml`
and `playbooks/verify_automation_user_key.yml` are tooling for the
rotation process `docs/CHANGE_CONTROL.md` already requires — they don't
change that process's shape, they remove the toil and guesswork from it.

## Why this needs its own runbook

This account has standing, fleet-wide, `NOPASSWD` sudo. Its key is the
single credential every automated change in this estate depends on.
Getting rotation wrong has two failure modes, both bad:

- **Lock yourself out.** `automation_user_authorized_keys_exclusive: true`
  means `authorized_keys` on every managed host always matches
  `automation_user_ssh_public_keys` *exactly* — remove a key from that
  list before the replacement is confirmed working, and the next
  playbook run can't connect at all. Recovering from that means falling
  back to `playbooks/provision_automation_user.yml`'s initial-admin-account
  path (see `docs/CHANGE_CONTROL.md`'s note on that being the *only*
  workflow that connects as something other than `automation_user_name`)
  — annoying, and exactly the kind of self-inflicted incident this
  runbook exists to prevent.
- **Leave a compromised or over-privileged key valid longer than
  necessary.** The whole point of rotating is to retire an old key — a
  "stage" step nobody follows through on isn't rotation, it's just
  adding keys forever.

## The three stages

```
1. STAGE     playbooks/rotate_automation_user_key.yml
             generates a new keypair on the control node, tells you
             the group_vars snippet to add — private key never leaves
             this host
                    |
                    v
             PR: add the new key to automation_user_ssh_public_keys
             ALONGSIDE the existing one(s) — see docs/CHANGE_CONTROL.md
                    |
                    v
             playbooks/site.yml or bootstrap.yml, using the OLD key
             (still valid) — authorizes the new key on every target host
                    |
                    v
2. VERIFY    playbooks/verify_automation_user_key.yml
             connects using ONLY the new key (ansible_ssh_private_key_file
             pinned explicitly), confirms SSH auth AND sudo both work,
             per host
                    |
                    v
3. FINALIZE  Switch your control node's active key for this environment
             to the new one (see "Switching your active key" below)
                    |
                    v
             PR: remove the OLD key from automation_user_ssh_public_keys
                    |
                    v
             playbooks/site.yml or bootstrap.yml, now using the NEW key
             — authorized_keys reconciles down to exactly the new key
```

Nothing here runs the next stage automatically — same "propose, don't
silently chain" posture as every other generated-credential workflow in
this repo (`provisioning/vm_provision`, `provisioning/certificate_enrollment`).
Stage 1 only writes files under `artifacts/automation_user_keys/` and
prints next steps; stages 2 and 3 are separate, deliberate playbook
runs you invoke yourself, each preceded by its own reviewed PR.

## Stage 1: generate

```bash
ansible-playbook playbooks/rotate_automation_user_key.yml \
  -e automation_user_rotation_key_label=svc_ansible-2026-07-27
```

`automation_user_rotation_key_label` is required and deliberately not
auto-generated from a timestamp — same "explicit, not silently guessed"
posture this repo already takes with e.g. Proxmox VMIDs (see
`provisioning/vm_provision/tasks/proxmox.yml`). Pick something that
tells a future reader which environment/date/change-ticket this key
belongs to; it also becomes the new key's own OpenSSH comment field.

Output lands in `artifacts/automation_user_keys/<label>/`:
`id_ed25519.key` (private, mode `0600`, never commit — already covered
by `.gitignore`'s blanket `*.key` rule), `id_ed25519.key.pub` (public,
safe to commit, though this role doesn't commit it for you), and a
`README.txt` with the exact `automation_user_ssh_public_keys` snippet
to paste into a PR.

**Refuses to overwrite an existing generated key under the same
label** — regenerating silently would invalidate a key someone may have
already been handed. Pick a new label instead.

Add the new public key to `inventories/<env>/group_vars/all.yml`'s
`automation_user_ssh_public_keys` **alongside** the current key(s), open
a normal reviewed PR (`docs/CHANGE_CONTROL.md`), merge, then run
`playbooks/site.yml` (or just `playbooks/bootstrap.yml`, which is where
`roles/automation_user` actually runs) against the target environment
using your **current, still-valid** key — this authorizes the new key on
every host without touching the old one.

## Stage 2: verify

```bash
ansible-playbook playbooks/verify_automation_user_key.yml \
  -i inventories/staging/hosts.yml --limit app06.gxp.example.internal \
  -e automation_user_rotation_verify_key_path=artifacts/automation_user_keys/svc_ansible-2026-07-27/id_ed25519.key
```

This pins the connection to *exactly* the candidate key (`ansible_ssh_private_key_file`
set at the play level, outranking whatever your control node would
normally offer) and confirms both SSH auth and `sudo` actually work — a
pass here means that specific key works against that specific host, not
"some key worked." Run it against every host (or representative sample
per tier) you're about to cut over before touching the old key at all.
`--limit` is not optional in practice for the same reason it isn't for
`playbooks/register_dns.yml`/`provision_vm_hyperv.yml` — you're usually
verifying one host or one tier at a time, not the whole environment in
one shot.

## Stage 3: finalize

**Switching your active key first matters.** The playbook run that
removes the old key from `authorized_keys` still has to *connect* using
some key — if your control node's ssh-agent/config is still offering the
old one when that run starts, the connection itself works fine (the old
key is still valid at connection time), but every run *after* that one
needs the new key, since the old one won't be in `authorized_keys`
anymore once this run finishes. Switch whatever your control node
uses for this environment (ssh-agent, `ansible_ssh_private_key_file` in
`group_vars`, `--private-key` on the CLI) to the new key **before**
running the finalize step, not after.

Once switched: remove the old key from `automation_user_ssh_public_keys`
in a follow-up PR (never in the same PR as stage 1's addition — see
`docs/CHANGE_CONTROL.md`), merge, then run `playbooks/site.yml` or
`playbooks/bootstrap.yml` again. `automation_user_authorized_keys_exclusive:
true` reconciles `authorized_keys` down to exactly the new key on every
target host.

## Multiple keys, multiple environments

`automation_user_ssh_public_keys` is set per-environment in each
`inventories/<env>/group_vars/all.yml`, deliberately not shared — see
that file's own comment on segregating which CI runner/bastion can reach
staging vs. production. Rotate each environment's key independently;
there's no requirement (or benefit) to keep them in lockstep. If more
than one credential legitimately needs standing access at once (e.g. a
CI runner and a human's break-glass bastion key), that's what the list
already supports — every entry in it stays authorized simultaneously,
this runbook only concerns itself with *replacing* one entry for
another, not the list's cardinality.

## What this tooling does not do

- **Automatic/scheduled rotation.** Every stage is a deliberate,
  change-ticketed action a person runs — see `docs/CHANGE_CONTROL.md`'s
  requirement that provisioning/rotating this account is tied to a
  ticket. A cron job silently rotating fleet-wide sudo credentials is
  the opposite of the auditability this repo is built around.
- **Key expiry enforcement.** Nothing here tracks a key's age or warns
  when it's "due" for rotation — `automation_user_ssh_public_keys`
  entries carry no metadata, deliberately, so there's no parallel record
  that can drift from the truth. The git history of
  `inventories/<env>/group_vars/all.yml` (blame/log on the file) is the
  authoritative record of when a given key was added — same "git history
  is the audit trail" posture `docs/VALIDATION.md` and `ansible.cfg`
  already take for everything else in this repo. If your organization
  needs a periodic-rotation *policy*, that's a periodic-review item (see
  `docs/VALIDATION.md`), not a technical control this tooling enforces.
- **Emergency revocation of a compromised key under attack.** Removing a
  known-compromised key needs to happen immediately, not through the
  patient stage/verify/finalize flow above — that's the
  `docs/CHANGE_CONTROL.md` "Emergency changes" path (make the change,
  tag the incident ID, retroactive ticket within one business day): edit
  `automation_user_ssh_public_keys` directly, drop the compromised entry,
  and run `playbooks/bootstrap.yml` immediately using a key you already
  know is still good.
