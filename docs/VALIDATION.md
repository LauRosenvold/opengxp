# Computerized System Validation Mapping

Ansible configures the *technical* controls. It does not, by itself,
constitute a validated state — your Quality Management System still owns
that. This document maps this repo's artifacts onto the IQ/OQ/PQ model so
whoever writes the validation package doesn't have to reverse-engineer it.

## Installation Qualification (IQ)

Evidence that the intended software/configuration was installed as
specified.

- `requirements.yml` — pinned, versioned list of every external collection
  and role. This *is* the bill of materials for IQ. A version bump is a
  change-controlled event (see [CHANGE_CONTROL.md](CHANGE_CONTROL.md)).
- `ansible-playbook ... --check --diff` output, captured by the CI runner
  and archived, is the "as found vs as specified" evidence for a given run.
- `playbooks/compliance_scan.yml` output (OpenSCAP ARF/HTML report) is
  point-in-time evidence of the installed control state and should be
  archived alongside the deployment record.
- The one-time `playbooks/provision_automation_user.yml` run for each host
  is itself IQ-relevant evidence: it establishes the account (and only
  that account) with standing privileged access for all subsequent
  configuration management. Archive its change ticket and run log
  alongside the host's initial deployment record — see
  [CHANGE_CONTROL.md](CHANGE_CONTROL.md#provisioning-or-rotating-the-automation-account).

## Operational Qualification (OQ)

Evidence that the system operates according to the intended
configuration under normal conditions.

- `molecule/` scenarios exercise each role's idempotency (`converge` +
  re-`converge` produces zero changes) and assert the expected end state
  (`verify.yml`). Molecule test results are OQ evidence for the role
  itself, independent of any specific target.
- `roles/auditd_gxp` and `roles/logging_forward` together are what let you
  demonstrate that security-relevant events (login, privilege escalation,
  account changes, file access to GxP-relevant paths) are captured,
  timestamped against a synchronized clock (`roles/chrony_time`), and
  shipped off-box before an operator could tamper with the local copy.
- `roles/aide_fim` gives you the file-integrity evidence needed to show
  unauthorized changes to validated paths (application binaries,
  configuration, audit config itself) would be detected.

## Performance Qualification (PQ)

Evidence that the system performs correctly in the actual production
context, under real operational use.

- This is inherently a *procedural* activity your Quality/Validation team
  runs against the live environment — periodic review, access reviews,
  scheduled `compliance_scan.yml` runs compared over time, and incident
  review of any AIDE/auditd findings. This repo supplies the raw material
  (scan reports, audit logs, drift reports); it does not replace the
  review.

## Periodic review

- Re-run `playbooks/compliance_scan.yml` on a fixed cadence (monthly is a
  common GxP interval) and diff the report against the last approved
  baseline. A control regression that isn't explained by an approved
  change is a finding, not a shrug.
- Re-run `ansible-galaxy install -r requirements.yml` version checks
  quarterly to see whether pinned upstream content (especially the CIS
  roles) has moved, and whether RHEL10-CIS has reached a state where the
  `el10_baseline_hardening` fallback can be retired for the `rhel10`
  group. The `almalinux10` group stays on the fallback regardless, since
  no upstream CIS role targets AlmaLinux at all. The `rhel9` and
  `almalinux9` groups already use `ansible-lockdown.RHEL9-CIS` directly —
  still worth checking each quarter for a version bump to re-validate,
  but neither group is waiting on a fallback to be retired.

## What this repo deliberately does NOT attempt

- Electronic signatures / 21 CFR Part 11 Subpart C controls on
  *application* records — that's the application's job, not the OS layer.
- Automatic self-approval of production changes. `playbooks/site.yml`
  will happily run against `inventories/production` if you point it there;
  the gate is procedural (your CI/CD approval step), not technical. Don't
  rely on this repo to stop an unauthorized production run — your pipeline
  permissions have to do that.
