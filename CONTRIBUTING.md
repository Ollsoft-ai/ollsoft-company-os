# Contributing to Company OS

Thanks for looking. A few things about this codebase that will save you time.

## The one rule

**There is no permission table, and adding one is almost always the wrong fix.**

Access control is the kernel's job here: Unix modes, POSIX ACLs, group
membership, and a Postgres RLS policy that re-implements the same check in SQL.
If a feature seems to need an `if user.can_edit(...)` branch, the answer is
usually a mode change, an ACL entry, or a `GRANT` — not application logic. Code
that re-decides access in Python is a bug even when it produces the right answer,
because it will drift from what the filesystem says.

## Development setup

You need a VM you can throw away — the platform creates real accounts and
services, so it isn't something to install on your laptop.

```bash
sudo bash scripts/install.sh --admin $USER
sudo bash scripts/seed-demo.sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium
```

Edit, then redeploy:

```bash
(cd frontend && node build.mjs)     # only if you touched frontend/src
sudo bash scripts/deploy.sh
```

`deploy.sh` reads `/etc/kb/kb.env`, so it follows a non-default `--prefix`.

## Tests

```bash
.venv/bin/python -m pytest tests/cli -q     # fast, no browser
.venv/bin/python -m pytest tests/ -q        # everything, needs chromium
```

`tests/cli/` is the loop you want while working. The suite assumes the demo
users `alice`, `bob` and `carol` exist — that's deliberate: most of these tests
assert that *one user cannot see another's data*, which needs real accounts.

Please add a test for behaviour you change. The security-relevant ones
(`test_rls.py`, `test_visibility.py`, `test_security_fixes.py`) are the ones that
make the claims in the README credible.

## Gotchas that have bitten people

- **Install into the platform venv by interpreter.** Use
  `sudo /opt/kb-venv/bin/python -m pip install <pkg>` so you always hit the right
  interpreter — console scripts carry a baked-in shebang and break if the venv is
  ever relocated.
- **Group membership is cached per process.** A user added to a group won't see
  the change until their backend restarts. The hub kills the backend on
  membership change for this reason.
- **Root code must be symlink-safe.** Anything in `hub.py` or `syncd.py` that
  touches a user-controlled path uses `openat`/`O_NOFOLLOW` helpers in
  `common.py`. Use them; a plain `open()` in root code is a privilege-escalation
  bug waiting to happen.
- **The index is disposable, personal schemas are not.** `kb.*` can be dropped and
  rebuilt from markdown. A user's `u_<name>` schema holds data that exists nowhere
  else — treat it accordingly.
- **`.git` in the knowledgebase is root-only, always.** Its objects contain every
  committed version of every file; group access there would bypass file
  permissions and RLS for all private content ever committed. `syncd` re-asserts
  this on start, and `install.sh` re-asserts it on every run.

## Style

Match the surrounding code. The Python is standard library-heavy and comment-dense
where the *why* is non-obvious — keep that. The frontend is deliberately
dependency-light vanilla JS; please don't introduce a framework.

## Reporting security issues

Privately, not as a public issue. See [docs/SECURITY.md](docs/SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0, the same as the rest of the project.
