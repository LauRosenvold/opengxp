# `apps/` — Application Deployments, a Third Tier Below `server_roles/`

## Why a third directory instead of folding this into `server_roles/`

This repo already draws one boundary in [SERVER_ROLES.md](SERVER_ROLES.md):
`roles/` is the security/compliance baseline, `server_roles/` is
workload-enablement platforms (`docker_host`, `kubernetes_node`,
`postgresql_server` — turning a hardened host into *a kind of server*).

`apps/` is a further step down: a **specific, named application**
deployed *on top of* a `server_roles/` platform. `apps/netbox` doesn't
install Docker — it assumes `server_roles/docker_host` already did, and
only adds the containers, compose file, and secrets a running NetBox
instance needs. That's a meaningfully different kind of change again:

- **Cadence**: an application's compose file, image tag, and
  configuration change on the application team's schedule — potentially
  weekly — which is a different rhythm than a platform role
  (`docker_host`) or the compliance baseline (`roles/`).
- **Ownership**: whoever owns the NetBox instance (a network/DCIM team,
  in NetBox's case) is not necessarily who owns the Docker platform
  itself or the OS hardening baseline. Mixing their changes into
  `server_roles/` would mean unrelated reviewers gating each other.
- **Composability**: one `docker_host` can run several unrelated `apps/`
  entries. The platform/application split mirrors that reality —
  `docker_hosts` (platform) and `netbox_hosts` (application) are
  deliberately separate inventory groups, and a real host is typically in
  both.

`ansible.cfg`'s `roles_path = roles:server_roles:apps` means all three
are searched by name, same convenience/boundary trade-off already made
for `server_roles/` — see that file's own rationale section, which
applies here unchanged.

If an application grows enough to need its own dedicated team, pipeline,
and release cadence entirely independent of this repo, that's the same
signal [SERVER_ROLES.md](SERVER_ROLES.md) already describes for
splitting into a separate repo — `apps/` is the right amount of structure
for "an application this team also deploys," not for "a product with its
own engineering org."

## `netbox`

A [NetBox](https://netboxlabs.com/oss/netbox/) instance (IP address
management / DCIM), deployed as a Docker Compose stack on top of a
`server_roles/docker_host`. Run via `playbooks/netbox.yml` against the
`netbox_hosts` inventory group — every host in it should also be in
`docker_hosts` (that playbook must run first).

This is a **hand-built reconstruction** of a
`netbox-community/netbox-docker`-style stack, not a checkout of that
repo. That project's real compose setup spans several override files and
per-service env files; `apps/netbox` collapses it to one
Ansible-templated compose file and one `.env` file so the whole thing is
auditable and change-controlled the same way as everything else here.
Cross-check against upstream if you need something this doesn't cover.

### Things that are NOT obvious and will bite you if skipped

- **The database should be external, not the bundled container.**
  `netbox_external_database_enabled` defaults to `true` and expects
  `netbox_database_host` to point at a `server_roles/postgresql_server`
  host in the `dbservers` group — real backup mechanism, pgAudit, TLS,
  everything that role already gives a GxP-relevant data store. The
  bundled `postgres` container (`netbox_external_database_enabled:
  false`) is a lab/evaluation fallback only; `tasks/secrets.yml` warns
  about this every run it's used.
- **This role reaches across hosts to provision the database.** When
  `netbox_manage_external_database` is also true (default), `tasks/
  database.yml` connects to `netbox_database_host` via `delegate_to` and
  runs `CREATE ROLE`/`CREATE DATABASE` there directly (`become_user:
  postgres`, the same pattern `server_roles/postgresql_server` uses for
  its own replication role) — that host must be in the same inventory
  and reachable over SSH from wherever you run this playbook. It does
  **not**, however, add a `pg_hba.conf` entry for this host — that's
  still `postgresql_allowed_hosts` on the database side (see the
  reminder `tasks/database.yml` prints).
- **Redis is always containerized, deliberately not externalized.**
  Unlike PostgreSQL, Redis here holds ephemeral cache and RQ task-queue
  state, not a GxP record of anything — there's no equivalent case for a
  hardened, backed-up Redis tier. Two separate Redis instances
  (`redis`/`redis-cache`) with two separate, required passwords, matching
  NetBox's own separation of task queue from cache.
- **TLS terminates at an nginx reverse-proxy container**, not at NetBox
  itself — NetBox's own container serves plain HTTP internally. Same
  self-signed-default/real-cert-pluggable pattern as
  `server_roles/postgresql_server`'s TLS handling
  (`netbox_ssl_cert_src`/`_key_src`). The cert and key end up
  world-readable on the host (`0644`, not `0600`) because the nginx
  container reads them across a bind mount under its own non-root UID,
  which this role doesn't pin precisely — see the comment in
  `tasks/tls.yml` for the actual trade-off.
- **nginx config and TLS material changes need an explicit restart** —
  `docker compose up -d` (`tasks/compose.yml`) auto-recreates containers
  when the *compose service definition* changes, but nginx.conf and the
  cert/key are bind-mounted file *content*, invisible to that
  diffing. `tasks/tls.yml` and the nginx.conf render task both notify a
  dedicated `docker compose restart nginx` handler for exactly this
  reason.
- **The Django `SECRET_KEY` and superuser password are never generated
  automatically.** Both fail the run loudly if empty
  (`tasks/secrets.yml`) rather than silently generating something nobody
  has a record of — `SECRET_KEY` signs every session/cookie/CSRF token,
  and rotating it invalidates every active session, so it has to be a
  deliberate, recorded value from day one.
- **Bind mounts, not named Docker volumes**, for every piece of state
  (`netbox_data_dir` under `/opt/netbox/data` by default) — visible,
  auditable host paths that an actual backup job (still bring-your-own,
  same posture as `server_roles/postgresql_server`'s WAL archiving) can
  target directly, rather than opaque volume names.
- **Image references and NetBox's own environment variable names are
  version-sensitive.** The pinned tags in `defaults/main.yml` are a
  snapshot at authoring time; `templates/env.j2` reconstructs the
  environment variables netbox-docker conventionally expects, but that
  naming has changed across NetBox major versions before — verify
  against the current netbox-docker documentation for whatever tag you
  actually bump to, same caveat as this repo's other pinned
  external-content references (CRI-O's repo URL, pgAudit's package name,
  Calico's manifest format).

### What's intentionally out of scope

- **An actual backup solution and its retention policy** — bind mounts
  give you real paths to back up, not a backup job.
- **LDAP/SSO integration, plugins, custom scripts/reports content** — this
  role deploys the platform NetBox runs on; populating it with your
  organization's actual DCIM data, auth integration, or plugin set is a
  separate, application-specific exercise.
- **High availability** (multiple NetBox app instances behind a shared
  LB, Redis Sentinel/cluster) — single-instance-per-host, matching the
  "generic workloads" scope this was built for, not an HA design.
- **Automated NetBox version upgrades** — unlike
  `server_roles/kubernetes_node`'s gated upgrade playbook, bumping
  `netbox_image` and re-running `playbooks/netbox.yml` is the whole
  mechanism here. NetBox itself sometimes requires specific upgrade-path
  steps (skipping minor versions isn't always supported) — check NetBox's
  release notes before bumping more than one version at a time.

## `registry`

A private Docker/OCI registry (`distribution/distribution`, the
`registry:2` image — the same lineage Docker Hub itself runs on),
deployed on top of a `server_roles/docker_host`. Run via
`playbooks/registry.yml` against the `registry_hosts` inventory group —
every host in it should also be in `docker_hosts`. Once it's running,
see "Wiring other roles to this registry" below.

Unlike `apps/netbox`, this stack has **no reverse proxy** —
`distribution/distribution` terminates TLS natively
(`http.tls.certificate`/`key` in its own config), so there's nothing an
nginx sidecar would add here that the registry doesn't already do
itself.

### Things that are NOT obvious and will bite you if skipped

- **Authentication is mandatory, not optional.** `registry_users` (htpasswd
  basic auth, bcrypt-hashed — `distribution/distribution`'s auth handler
  only accepts bcrypt, not MD5-crypt or plain crypt, so
  `tasks/auth.yml` hardcodes that scheme) fails the run loudly if empty.
  There is no anonymous push or pull, ever, by design — an unauthenticated
  private registry is a supply-chain risk, not a convenience.
- **Images are immutable by default.** `registry_delete_enabled: false`
  means nothing pushed is ever removed — deliberate, since a validated
  deployment or an old `compliance_scan.yml` evidence artifact might
  reference a specific image digest that should never quietly vanish.
  Flip it on only with an actual, documented retention policy; doing so
  also enables a weekly offline garbage-collection cron
  (`tasks/gc.yml`) — "offline" meaning it briefly stops the registry
  container before running `registry garbage-collect`, per
  `distribution/distribution`'s own guidance about not GC'ing a store
  that's still being written to.
- **Other Docker hosts need to be told to trust this registry's
  certificate** — a private registry with a self-signed cert (the
  default) is TLS-valid to nobody else until you either give it a real
  CA-issued cert (`registry_ssl_cert_src`/`_key_src`) or distribute the
  self-signed one as a trusted CA via
  `server_roles/docker_host`'s new `docker_trusted_registry_cas` var
  (deploys to `/etc/docker/certs.d/<registry>/ca.crt` on every host that
  needs it). **Do not** reach for `docker_insecure_registries` instead —
  that disables TLS verification entirely rather than trusting one known
  cert, and defeats the point of running your own registry securely.
- **The web UI (`registry_ui_enabled`, on by default) is loopback-only**
  (`registry_ui_bind_address: "127.0.0.1"`) — it proxies through to the
  registry, which still enforces htpasswd auth for actual API calls, but
  an unauthenticated-by-default catalog browser reachable fleet-wide
  wasn't the right default either. Change the bind address (and open the
  matching firewalld port) if you want it reachable beyond the host
  itself.
- **Storage is filesystem-only.** `distribution/distribution` also
  supports S3/Azure/GCS backends; this role only wires up the bind-mounted
  local filesystem driver (`registry_storage_dir`, same "auditable host
  path, not an opaque volume" posture as `apps/netbox`). Adding a cloud
  storage backend is a documented gap — different config schema,
  credentials, and lifecycle-policy considerations this role doesn't
  attempt to cover.
- **Registry content isn't Satellite content** in the sense the rest of
  this repo means it (there's no Satellite-vs-public-repo toggle here at
  all) — `registry_image`/`registry_ui_image` are pulled from
  `docker.io` directly. Mirroring these two images into your own registry
  (this one, or another) before it exists yet is a bootstrapping
  chicken-and-egg problem every private-registry rollout has; most
  estates solve it by pulling these specific images from the public
  internet once, or via a one-time offline transfer, rather than trying
  to avoid it entirely.

### What's intentionally out of scope

- **Docker Content Trust / image signing infrastructure.**
  `server_roles/docker_host`'s `docker_content_trust_enabled` toggle pairs
  with a properly signed registry if your org sets up Notary (or an
  equivalent) separately — this role doesn't stand up a signing service.
- **Vulnerability scanning** (Trivy, Clair, or a scanning feature of a
  commercial registry product) — this is a plain, unmodified
  `distribution/distribution`, not Harbor/Artifactory/Nexus.
- **Replication/mirroring between multiple registry instances** (e.g. a
  DR copy) — single-instance-per-host, same posture as `apps/netbox`'s
  "no HA" scope note.
- **Repository-level or team-level access control.** htpasswd auth is
  all-or-nothing per user — anyone in `registry_users` can push/pull
  anything. Fine-grained per-repository permissions need a token-auth
  server (or a different product) this role doesn't implement.

## `nginx`

A standalone, general-purpose containerized nginx, deployed as a
one-service Docker Compose stack on top of a `server_roles/docker_host`.
Run via `playbooks/nginx.yml` against the `nginx_hosts` inventory
group — every host in it should also be in `docker_hosts`.

`apps/netbox` and `apps/registry` each already handle their own TLS
(netbox: a bundled nginx sidecar that only ever proxies to netbox
itself; registry: native TLS, no proxy at all) — this role is for
everything else: fronting a backend that doesn't terminate TLS itself,
serving static content (compliance documentation, internal artifacts,
...), or standing up more than one virtual host behind a single
entrypoint. It is not a replacement for either of those two roles' own
bundled nginx use — each is scoped tightly to its own application and
stays that way.

### Things that are NOT obvious and will bite you if skipped

- **`nginx_vhosts` is a flat list, and every vhost shares one TLS
  cert.** Each entry is either `mode: static` (serves a bind-mounted
  directory under `nginx_content_dir`) or `mode: proxy` (reverse-proxies
  to an upstream URL) — see `defaults/main.yml` for the full shape.
  `tasks/validate.yml` fails the run loudly on a missing `server_name`,
  an invalid/missing `mode`, a `static` vhost without `content_subdir`,
  a `proxy` vhost without `proxy_pass`, or two vhosts sharing the same
  `server_name`. If you need per-vhost certificates instead of one
  shared cert, that's a distinct enough design decision this role
  doesn't make for you — run a separate `nginx_hosts` instance.
- **Default-deny for any unmatched Host header**, same posture as
  `roles/firewalld_baseline`. A request that doesn't match any
  configured `server_name` gets `444` (connection closed, no response)
  rather than silently falling through to whichever vhost happens to be
  defined first in the rendered config. The one exception is
  `/healthz`, which always answers `200` regardless of Host header —
  that's what the compose healthcheck targets, deliberately independent
  of whether any vhost is configured yet (`nginx_vhosts: []` is valid).
- **This role does not populate vhost content.** `mode: static`
  vhosts get an empty, correctly-permissioned directory
  (`tasks/directories.yml`) — putting `index.html` and friends in it is
  a separate step, same "this role deploys the platform, not your
  content" posture as `apps/netbox` not shipping DCIM data.
- **TLS follows the same self-signed-default/real-cert-pluggable
  pattern** as `apps/netbox`/`apps/registry`/
  `server_roles/postgresql_server` (`nginx_ssl_cert_src`/`_key_src`).
  The self-signed certificate's CN comes from the first configured
  vhost's `server_name`, or this host's own `inventory_hostname` if
  `nginx_vhosts` is still empty. Cert and key end up world-readable
  (`0644`) for the same nginx-unprivileged-non-root-UID reason as
  `apps/netbox` — see the comment in `tasks/tls.yml`.
- **nginx config and TLS material changes need an explicit restart** —
  same reasoning, and the same dedicated `docker compose restart nginx`
  handler pattern, as `apps/netbox`.

### What's intentionally out of scope

- **Rate limiting, WAF rules, request authentication** — plain
  reverse-proxy/static-file nginx; anything beyond `proxy_pass` and
  `ssl_*` directives is a config change you make directly in
  `templates/nginx.conf.j2` (or a documented gap here to extend, not a
  feature this role tries to expose as its own variables for every
  possible nginx directive).
- **Per-vhost certificates** — see above; one cert per `nginx_hosts`
  instance, not per vhost.
- **High availability / load balancing across multiple nginx
  instances** — single-instance-per-host, same "no HA" scope note as
  `apps/netbox` and `apps/registry`.
- **Content deployment/sync for `mode: static` vhosts** — this role
  creates the directory; getting your actual files into it (rsync, a CI
  artifact push, another Ansible role) is a separate, application-specific
  exercise.

## Wiring other roles to a registry once one exists

Several CHANGEME/registry-mirror vars elsewhere in this repo exist
specifically so you can point them at `apps/registry` (or any other
private registry) once it's running, instead of pulling from public
registries directly:

| Var | Role | What it does |
|---|---|---|
| `docker_registry_mirrors` | `server_roles/docker_host` | Configures dockerd to try this registry as a pull-through mirror before `docker.io` |
| `docker_trusted_registry_cas` | `server_roles/docker_host` | Trusts this registry's TLS cert on every Docker host that needs it (see the `registry` caveats above) |
| `netbox_image` | `apps/netbox` | Point at `<registry_hostname>/netboxcommunity/netbox:<tag>` once you've mirrored it |
| `k8s_repo_baseurl` / `containerd_repo_baseurl` / `crio_repo_baseurl` | `server_roles/kubernetes_node` | RPM repos, not container images — a different kind of mirroring (Satellite, not this registry), listed here only so you don't confuse the two content-source mechanisms |

None of this rewiring happens automatically — every role above still
defaults to its own public source until you deliberately change it,
consistent with every other "off/public until you configure otherwise"
default in this repo.
