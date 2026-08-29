# Tern for Hermes

Tern for Hermes is a native Hermes Agent plugin for durable, provider-neutral
issue delegation. One installation can serve many Tern projects and many local
Git checkouts without guessing where work belongs.

## Install

The release repository is designed to install directly with Hermes:

```sh
hermes plugins install jrmd/hermes-tern --enable --force
hermes plugins doctor tern --ci
```

Hermes marks community plugins that execute subprocesses as `CAUTION` and
requires the explicit `--force` acknowledgement. This plugin executes only
argument-vector subprocesses for exact Git-root validation and an optional
Hermes gateway restart. Review [SECURITY.md](SECURITY.md) and the source before
installing, as you should for any coding-agent plugin.

For development from the Tern monorepo:

```sh
ln -s /path/to/tern/integrations/hermes_tern ~/.hermes/plugins/tern
hermes plugins enable tern
hermes plugins doctor ~/.hermes/plugins/tern --ci
```

Hermes 0.20.6 or newer is supported. The plugin has no third-party Python
dependencies.

## Connect

In Tern, open **Settings → Agent runners**, create a pull runner, create a
Hermes instance, and copy the one-time instance credential. Then run:

```sh
hermes tern connect
```

The prompt is hidden. Hermes validates the credential before saving it through
its secret environment writer. The runner credential and private lease tokens
are never exposed to the model.

## Map projects to local checkouts

Tern knows the immutable organization and project IDs on a delegation. Your
Hermes installation owns the machine-specific checkout paths. Configure every
project that this machine may execute:

```sh
hermes tern projects add \
  --organization-id 00000000-0000-0000-0000-000000000000 \
  --project-id 11111111-1111-1111-1111-111111111111 \
  --path /absolute/path/to/repository \
  --label "My project"
```

For Hermes runners, Tern shows the workspace ID and every project ID in the
one-time credential dialog. The project ID is also the UUID in the project
URL. Repeat the command for every local checkout, then inspect and start all
routes:

```sh
hermes tern projects list
hermes tern start
hermes tern status --live
```

Paths must be absolute, existing Git repository or worktree roots. Symlinks
are canonicalized. Filesystem root, the user home directory, nested repository
directories, missing paths, and non-Git directories are rejected.

## How routing works

Each configured `(organizationId, projectId)` receives its own Hermes cron job:

1. The job is pinned to that project's checkout as its working directory.
2. Its cheap monitor requests only that route and emits minimal run IDs.
3. `tern_claim_next` requires the same exact IDs and revalidates the local route
   before claiming.
4. The claimed run and context must repeat the expected project identity.
5. An unmapped or mismatched project remains unclaimed.

There is no default checkout and no routing by project name, issue title,
profile name, current directory, or model judgement.

## Daily use

Mention the configured agent in a Tern issue or delegate to its profile. The
matching Hermes route wakes within the configured polling interval, claims one
run, applies the authority snapshot, reports progress, and records a durable
outcome in Tern.

```sh
hermes tern status --live
hermes tern stop
hermes tern start
hermes tern projects remove --organization-id ORG_ID --project-id PROJECT_ID
hermes tern disconnect
```

`disconnect` removes the saved credential, generated monitor wrappers,
scheduler skill copy, and Tern cron jobs. Project mappings remain in the
Hermes plugin settings so reconnecting does not require re-entering local
paths.

## Delegated workflow behavior

The plugin exposes `tern_update_issue_status`, but it can update only the
issue linked to the currently leased run. Tern must grant the run a
`safe_automatic` policy with the `issue_updates` capability, and the call must
include the issue version from the immutable context. A stale version is
rejected rather than silently overwriting a newer change.

For coding workflows, `tern_finish` accepts `succeeded` only when the result
contains observed `commit` and `pull_request` artifacts. The plugin does not
create, push, or verify those artifacts itself: Hermes must perform that work
with its local Git/provider tools, and should report `needs_review` or
`blocked` when delivery is incomplete.

## Update or remove

```sh
hermes plugins update tern
hermes plugins doctor tern --ci
```

To remove the integration cleanly, disconnect it before uninstalling the
plugin:

```sh
hermes tern disconnect
hermes plugins remove tern
```

## Security model

- Pull polling is the supported transport. No inbound public webhook is
  required.
- HTTPS is mandatory except for explicit loopback development endpoints.
- Redirects, URL credentials, URL fragments, oversized responses, and malformed
  envelopes are rejected.
- Monitors contain only non-secret route IDs and emit only run ID, version, and
  profile ID—not issue context.
- Tern owns admission, current authorization, leases, and durable results.
  Hermes owns local/provider execution inside the mapped checkout.

## Development checks

```sh
python -m unittest discover -s tests
hermes plugins doctor . --ci
```

The source tree and release package must exclude `__pycache__`, credentials,
Hermes profile data, generated route wrappers, and local checkout paths.
