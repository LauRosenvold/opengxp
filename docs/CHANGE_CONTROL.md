# Change Control Model for This Repository

This describes the branching/approval flow the repo assumes. Adapt names
(branch protection tool, ticket system) to whatever your org already runs —
the shape is what matters for GxP defensibility, not the tool.

## Environments = branches = inventories

| Inventory                  | Branch      | Purpose                                             |
|-----------------------------|-------------|------------------------------------------------------|
| `inventories/validation`    | `validation`| IQ/OQ/PQ execution target, never receives untested content |
| `inventories/staging`       | `staging`   | Pre-prod soak, mirrors production host tiers at smaller scale |
| `inventories/production`    | `main`      | The regulated estate                                 |

A change only reaches `inventories/production` after it has run, unchanged,
against `staging` and (for anything touching a validated control) against
`validation` with a recorded, reviewed `--check --diff` output attached to
the change ticket.

## Required for every merge to `main`

1. A linked change ticket (CAB/CR number) in the description — no ticket,
   no merge, no exceptions for "quick fixes."
2. At least one peer review approval from someone other than the author.
   For anything touching `roles/cis_hardening`, `roles/auditd_gxp`,
   `roles/ssh_hardening`, `roles/selinux_enforce`, `roles/automation_user`,
   or `requirements.yml`, require a second approval from the
   security/compliance owner specifically — these are the controls your
   validation package cites.
3. A green CI run of, at minimum:
   - `ansible-lint` across `roles/` and `playbooks/`
   - `ansible-playbook --syntax-check`
   - the `molecule` scenarios for any role that changed
   - `ansible-playbook playbooks/site.yml -i inventories/staging/hosts.yml --check --diff`,
     with the diff output attached to the ticket
4. No direct commits to `main` — protected branch, PR only.

## Provisioning or rotating the automation account

Running `playbooks/provision_automation_user.yml` against a host is a
privileged, security-relevant action in its own right — it creates the
account that will hold standing NOPASSWD sudo on that host from then on.
Treat it like any other production change:

- Tie every run to a change ticket, same as a PR — record which host(s),
  which `bootstrap_admin_user` was used to authenticate, and who ran it.
- Rotating the SSH keypair (`automation_user_ssh_public_keys`) is a normal
  PR through the usual review process like any other `group_vars` change
  — but the rollout itself is two change-controlled steps, not one: add
  the new key and confirm a run succeeds with it, *then* remove the old
  key in a follow-up change. Never ship both in one step (see the warning
  in `roles/automation_user/defaults/main.yml` about `exclusive: true`
  locking every playbook out at once if you get this wrong).
- This is the one workflow in the repo that connects as something other
  than `automation_user_name` — never let that initial admin credential
  become a standing, reusable "just SSH in as root" habit. It's for this
  one bootstrap step, once per host, full stop.

## Emergency changes

GxP doesn't forbid emergency fixes, it forbids *undocumented* ones. If a
production security incident requires an out-of-band change:

1. Make the change directly if truly urgent, but tag the commit message
   with the incident ID.
2. Open the retroactive change ticket and PR within one business day.
3. Run a `compliance_scan.yml` pass immediately after, and again once the
   retroactive PR merges, to confirm converged state.

## Version pinning discipline

`requirements.yml` pins every external collection and role by exact
version (SHA/tag, not a floating branch — the one exception,
`ansible-lockdown.RHEL10-CIS` pinned to `main`, is a known, called-out
exception documented in that file and should be tightened the moment a
tagged release exists). Bumping any pin is itself a change-controlled
event: it changes what's actually being enforced on the estate, so it
needs the same ticket + review + staging-soak treatment as a role change,
not a drive-by dependency bump.
