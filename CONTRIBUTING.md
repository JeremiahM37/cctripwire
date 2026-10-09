# Contributing to cctripwire

Thanks for looking under the hood. Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
then [docs/LIMITATIONS.md](docs/LIMITATIONS.md): most design questions are answered by one
of those two.

## Dev setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The suite needs no real `claude` and no network. `tests/fake_claude.py` stands in for the
binary (its behaviour is driven by `FAKE_*` environment variables), and quota readings are
mocked with `cctripwire mock-quota`. Every test cleans up the processes it starts; the CI job
fails if a fake `claude` is left running.

## Ground rules

- **Never limit normal work by default.** The runaway-only preset must stay quiet under
  ordinary use. A change that makes it fire more often needs a test showing the new case is a
  real runaway, and a test showing ordinary work still passes.
- **Fail open.** A crash in a hook or the status line must never block Claude Code. Log it to
  the state directory and exit 0 with no output.
- **Exact or labelled.** Never show a guess as an exact reading, and never show missing data as
  0 %. Estimates carry `~`, missing data shows `?`.
- **Standard library only.** No runtime dependencies.
- **Pause, don't kill.** Running work is suspended (SIGSTOP) and resumed, never killed, unless
  the user configured it.

## Trying a change against the real Claude Code

`scripts/sandbox.sh` opens a throwaway chat with its own state, project-only hooks, the shim
first on PATH and every `claude -p` forced onto Haiku. Use it for anything the fake binary
cannot show; keep drills to a handful of real runs, since each costs a fresh prompt cache.

## Pull requests

Keep them focused, include tests, and update the README or docs when behaviour changes.
`python -m pytest -q` must pass.
