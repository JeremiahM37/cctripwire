# Security

cctripwire is a usage guard for cooperative agents, not a security boundary. An agent with
shell access can disable it (`CCTRIPWIRE_DISABLE=1`, `cctripwire breaker off`), and every such
override is recorded in the intervention log. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

What it does touch:

- it reads Claude Code transcripts and status-line data on the local machine;
- it writes its own config, state and logs under `~/.config/cctripwire` and
  `~/.local/state/cctripwire`;
- `setup` edits `~/.claude/settings.json` (after a backup) and, with `--edit-rc`, your shell rc
  files;
- the daemon sends SIGSTOP / SIGCONT to `claude -p` processes;
- it makes no network requests except to a `sources.usage_url` you configure, and the opt-in
  OAuth usage source, which is off by default.

To report a vulnerability, open a private security advisory on GitHub
(Security → Report a vulnerability) rather than a public issue.
