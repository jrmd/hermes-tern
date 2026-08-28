# Security

## Trust boundary

The plugin receives a Tern runner credential, polls the HTTPS runner protocol,
and lets Hermes work only in explicitly mapped Git checkout roots. It never
stores provider credentials in Tern or embeds the runner credential in monitor
scripts, prompts, tool output, or route configuration.

## Hermes install scan

Hermes currently reports this community plugin as `CAUTION`, so installation
requires `--force`. The relevant source behavior is intentional and bounded:

- `routing.py` invokes `git -C <checkout> rev-parse --show-toplevel` with an
  argument vector, `shell=False`, and a timeout to prove a configured path is
  the exact Git root.
- `cli.py` can invoke `hermes gateway restart` with an argument vector and a
  timeout after connection changes.
- Tests create disposable Git repositories and compile generated, non-secret
  monitor wrappers to verify their behavior.

No subprocess command is built from a Tern issue body or model output.

## Reporting

Report suspected vulnerabilities privately through GitHub Security Advisories
for `jrmd/hermes-tern`. Do not include live runner credentials or lease tokens
in a report.
