# Contributing to Jar

Thanks for your interest. This guide covers setup, the test command and the conventions
the project works under.

[AUTHORITY.md](AUTHORITY.md) is the hierarchy of authority for this repository, and
[PROJECT_INTENT.md](PROJECT_INTENT.md) states its purpose and boundaries. Read both before a
non-trivial change.

## Setup

Requires Python 3.10 or later on macOS or Linux (see the README).

```sh
git clone https://github.com/zachshallbetter/jar.git
cd jar
./install.sh        # creates a virtual environment, ~/.jar, and registers the daemon
```

`install.sh` changes the local system (it registers a `launchd` or `systemd` service);
read it before running. To work on the code without installing the daemon, install the
dependencies from `requirements.txt` into a virtual environment and run from `src/`.

## Verifying a change

```sh
PYTHONPATH=src python3 -m unittest discover tests
```

The suite is `tests/test_jar.py`. It sets `JAR_HOME` to a temporary directory, so it does not
touch `~/.jar`. The repository has no CI workflow, so this local check is the gate.

## Conventions

- One process, SQLite only: all persistent state lives under `$JAR_HOME` (default `~/.jar`).
- Local first: cloud capabilities are optional accelerators, never a requirement.
- Privacy fences filter at read time; do not add a path that bypasses them.
- Documented is not implemented, and implemented is not tested: mark features by their
  real state (AUTHORITY.md, section 2).
- Commit messages are short imperative summaries. Use a conventional prefix such as
  `docs:` or `fix:` when it fits.

## Pull requests

- Branch off `main`; keep PRs focused.
- Describe the change and how you verified it. The PR template prompts for this.
- Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
