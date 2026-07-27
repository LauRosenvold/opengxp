# host_vars

Per-host overrides go here as `<inventory_hostname>.yml`, named exactly as
the host appears in `hosts.yml`. Keep these to genuine exceptions (a
specific host that needs a different firewalld port, a one-off NTP source)
— if more than a couple of hosts need the same override, that's a sign it
belongs in a new inventory group instead, not repeated host_vars files.

Anything containing a secret (a host-specific TLS key, a local root
password hash) must be named `<hostname>.vault.yml` and encrypted with
`ansible-vault`. `.gitignore` at the repo root already excludes
`*.vault.yml`.
