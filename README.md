# colab-dfs-ext — zero-browser Drive mount & multi-account extension for google-colab-cli

> [中文](README.zh-CN.md) | English

Adds **browser-less Google Drive mounting (`colab dfs`)**, **isolated multi-account profiles (`colab account`, `-a / --account`)**, and **extended AI-agent guidance (`colab skill`)** to the `colab` CLI —
**without touching a single file of google-colab-cli**: colab-cli upgrades are unaffected,
no re-integration is needed, and worst case a single command repairs it. All extension modules
aside from `google-colab-cli` runtime integrations use pure Python standard library.

```
# Zero-browser Drive mounting
colab dfs mount   [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab dfs umount  [-a ACCT] [-s SESSION] [path]
colab dfs status  [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab dfs consent [-a ACCT] [--flavor dfs|sdk|custom] [--url-only | --code CODE]
colab dfs show | token | clear [-a ACCT] [--drive-account DRV]
colab dfs doctor  [-a ACCT]             # Health check: injection / accounts / credentials
colab dfs install                       # Re-inject this extension if `dfs` disappears after upgrade

# Multi-account management (`acct` is a hidden alias for `account`)
colab -a <account> <any-colab-command>  # Target profile by name or email (or set COLAB_ACCOUNT)
colab account list [--json]             # Offline inventory (never blocks on locked keyrings)
colab account current [--json]          # Show currently resolved profile
colab account add <name> [--email ...]  # Register + login (OAuth2 or --auth adc / --adc-file)
colab account login [<name>] [--url-only | --code CODE]   # Interactive or headless PKCE login
colab account use <name>                # Switch default active profile
colab account status [--json]           # Parallel probe: CCU, burn rate, sessions, Drive across all
colab account set-drive <storage> [-a <compute> | --default | --clear]  # Shared Drive binding
colab account rename <old> <new>        # Rename profile & migrate isolated state/tokens
colab account remove <name> [--force]   # Remove profile & isolated state
colab sessions --all [--json]           # List active sessions across all accounts
colab usage --all [--json]              # Check CCU quota across all accounts
colab skill                             # Upstream SKILL.md augmented with dfs & multi-account docs
```

Every subcommand accepts `-- -<any args>` to pass extra flags through to the underlying tools —
flags not exposed by this extension remain usable.

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
```

(wget works too: `wget -qO- ... | sh`. The script does only three things: download the repo
tarball → extract to a temp dir → run `python3 install.py`, then print the next steps.
Use `COLAB_DFS_REPO` / `COLAB_DFS_REF` to override the source.)

## Requirements

- Python 3.8+ (both `install.py` and the extension are pure standard library)
- [`google-colab-cli`](https://pypi.org/project/google-colab-cli/) installed and working
  (`uv tool install google-colab-cli`), i.e. the `colab` command
- A connected colab session (`colab connect -s <session>`)
- Colab runtimes ship `/opt/google/drive` (DriveFS) already — no upload needed

## Usage

### Step 1 · Install the extension

One-line install (above), or manually:

```bash
git clone https://github.com/deluxebear/colab-dfs-ext
cd colab-dfs-ext            # this directory
python3 install.py          # any python3 works
colab dfs doctor            # verify the injection succeeded
```

`install.py` does two things:

1. Copies the extension source to `~/.colab-dfs-ext/` (**outside** colab-cli's venv — upgrades
   never touch it);
2. Writes **a single file**, `colab_dfs.pth` (~600 bytes), into the colab-cli environment's
   site-packages.

Useful install flags:

```bash
python3 install.py --status        # where the .pth / source / colab_cli live
python3 install.py --link          # dev mode: symlink this repo; code changes take effect immediately
python3 install.py --site-dir DIR  # target a specific site-packages
```

### Step 2 · One-time authorization (only once, ever)

```bash
colab dfs consent            # browser pops the Google consent page -> allow
colab dfs show               # inspect stored credentials (refresh token / GaiaId / email)
```

By default this uses the reverse-engineered DriveFS built-in client (flavor `dfs` — Google's own
"Google Drive for desktop", a verified app); the localhost callback is caught automatically, so
there is no authorization code to copy-paste. Other flavors: `--flavor sdk` (colab-cli's bundled
client, works headless but easily blocked by Google) and `--flavor custom --client-id ...
--client-secret ...` (your own GCP project — most robust long-term). On machines that have
already consented once, `colab dfs consent --import` works with no browser at all.

DriveFS credentials live in `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`
(with automatic non-destructive adoption from legacy `~/.config/colab-cli/dfs_credentials.json` into
`default`); every mount afterwards is zero-browser.

### Step 3 · Everyday mounting

```bash
colab connect -s test        # connect to the colab session (if not already)
colab dfs mount -s test      # mounts at /content/drive, done in seconds
colab dfs status -s test     # mount status
colab dfs umount -s test     # unmount

colab dfs mount -s test --force --purge-state   # remount: clear stale processes/dirs/local state
colab dfs mount -s test --dry-run               # only print config and the remote payload
colab dfs mount -s test --readonly              # read-only mount
```

### Multi-account workflows & shared Drive mounting

Existing `~/.config/colab-cli/{sessions.json,history/,dfs_credentials.json}` and keyring
`oauth_token` are automatically and **non-destructively** adopted into profile `default` on first
use (the original files are preserved; rename anytime with `colab account rename default personal`).

Each account has isolated state under `~/.config/colab-cli/accounts/<name>/`
(`sessions.json`, `history/`, `dfs_credentials.json`, `dfs_pending.json`, `oauth_pending.json`,
and `0o600` `oauth_token.json` fallback when keyring is unavailable/locked) plus keyring entry
`oauth_token:<name>`, indexed in `~/.config/colab-cli/accounts.json`.

```bash
# Add accounts interactively (runs Colab OAuth2 + DriveFS consent by default)
colab account add work --email me@work.com
colab account add gpu2 --no-dfs --drive-account work   # reuse work's Drive across compute accounts

# Headless / SSH / agent two-step login (works for both Colab auth and DriveFS)
colab account login work --url-only
colab account login work --code <PASTED_CODE>
colab -a work dfs consent --flavor sdk --url-only
colab -a work dfs consent --code <PASTED_CODE>

# Target accounts via flag, env var, active switch, or smart -s <session> routing
colab -a work new -g T4 -s gpu-a             # creates session inside account `work`
export COLAB_ACCOUNT=work                    # optional shell default override
colab account use personal                   # switch persistent default account
colab exec -s gpu-a -- nvidia-smi            # auto-routes -s gpu-a to `work` (notice on stderr)
colab url -s gpu-a                           # injects authuser=<email> for browser convenience

# Cross-account views & shared Drive
colab account status                         # parallel probe: CCU + rate + sessions + Drive
colab sessions --all [--json]                # sessions across all accounts
colab usage --all [--json]                   # CCU quota across all accounts
colab account set-drive main -a gpu2         # bind gpu2's Drive mounts to `main`
colab -a gpu2 dfs mount -s gpu-a --drive-account main   # one-off override
```

When running AI agents against `colab`, `colab skill` / `colab SKILL` automatically includes full
`colab dfs` and `colab account` instructions (preserving valid YAML frontmatter).

### Built-in `colab drivemount` enhanced by default

`colab drivemount` is automatically upgraded to use the zero-browser DriveFS backend when
credentials are present, supporting `-a/--account`, `--drive-account`, `--readonly`, `--force`,
`--purge-state`, `--timeout`, `--dry-run`, and automatic session-owner routing:

```bash
colab drivemount -s test                             # zero-browser DriveFS + auto session routing
colab drivemount -s test --drive-account storage     # mount another profile's Drive
colab drivemount -s test --readonly --no-fallback    # fail fast if DriveFS credentials are missing
colab drivemount -s test --upstream                  # force original interactive browser flow
COLAB_DFS_DEFAULT=0 colab drivemount -s test         # disable zero-browser override via env var
```

When DriveFS credentials are missing (and no DriveFS-only flag or `--no-fallback` is given),
`colab drivemount` falls back to the upstream interactive flow with an account reminder before the URL.

## Coexisting with upgrades (verified in practice)

| Scenario | Result |
|---|---|
| `uv tool install google-colab-cli --reinstall` (rebuild the tool env) | The `.pth` **survives** (uv only reinstalls its own packages, doesn't clean foreign files); `colab dfs` works right away |
| `colab update --install` | Internally runs `uv tool install -U`; the extension wraps the `update` command at registration time and re-checks/re-wires itself once the upgrade finishes (`[colab-dfs] extension still wired: ...`) |
| Worst case (Python version change, venv manually wiped) | `colab dfs` disappears, but `~/.colab-dfs-ext` is still there: `python3 ~/.colab-dfs-ext/install.py` restores it in one command |

## Uninstall

```bash
python3 install.py --uninstall          # remove the .pth
rm -rf ~/.colab-dfs-ext                 # (optional) delete the extension source copy
```

## Troubleshooting

```bash
colab dfs doctor                # health check: injection / credentials / session, with fix hints
COLAB_DFS_DEBUG=1 colab dfs …   # verbose diagnostics from the loader/injection
```

- `colab dfs` commands gone → re-wire with `python3 ~/.colab-dfs-ext/install.py`.
- Changed this repo's code but behavior didn't change → `~/.colab-dfs-ext` holds the snapshot
  from install time; re-run `python3 install.py` (or switch to `--link`). The `source_dir()`
  lookup order is: `COLAB_DFS_EXT` env var → current code location → `~/.colab-dfs-ext`.

## How the injection works (why upgrades don't break it)

- colab-cli is a Typer app: `colab_cli.cli` exposes a module-level `app` object, and the official
  command modules themselves follow the `register(app)` convention — the extension wraps the
  callback/auth/state hooks cleanly and registers the `account` (`acct`) and `dfs` subgroups on
  that same `app`. **Zero intrusion.**
- A `.pth` file is the only hook site.py executes automatically: one line,
  `import os, sys; exec(compile("try: ... import colab_dfs_loader ..."))`, which adds
  `~/.colab-dfs-ext` to `sys.path` and eagerly imports the loader; the loader calls
  `colab_dfs_ext.register(app)`. Measured startup overhead: **≈0** (323 ms vs 324 ms — colab
  imports these modules anyway).
- Defensive: the whole snippet is wrapped in try/except. If the extension is missing or broken it
  is silently skipped, and **`colab` itself can never be broken by it** (set `COLAB_DFS_DEBUG=1`
  to see why it was skipped).

## Known implementation details / gotchas

- `remote.exec_code` uses colab_cli's own `ColabRuntime.execute_code()` (no subprocess, no temp
  files) and **must call `runtime.stop()` in a finally block** — otherwise the kernel client's
  non-daemon threads hang the `colab` process on exit (symptom seen in practice: all output
  printed but the command never returns).
- Per-account DriveFS credentials are stored in `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`
  (legacy `~/.config/colab-cli/dfs_credentials.json` is automatically adopted into `default` and kept intact).
- For the underlying technical background (DriveFS's credentials contract, the metadata shim,
  the three de-browsering approaches), see the parent repo's
  `colab-drivemount-re/docs/REVERSE.md`.

## Layout

```
colab-dfs-ext/
├── install.sh               # curl | sh one-line bootstrap installer (POSIX sh)
├── install.py               # install / repair / uninstall / status (pure stdlib)
├── colab_dfs_loader.py      # loader imported by the .pth startup hook
├── tests/
│   └── test_multi_account.py # hermetic multi-account, DriveFS, and skill tests
└── colab_dfs_ext/           # the extension itself (copied/symlinked to ~/.colab-dfs-ext/)
    ├── __init__.py          # register(app) entry point
    ├── inject.py            # .pth writing and state management
    ├── accounts.py          # account registry, isolation paths, migration, keyring/file backend
    ├── account_commands.py  # `colab account` subcommands (typer)
    ├── cli_hooks.py         # global -a/--account, auth/state isolation, --all, skill hooks
    ├── skill_doc.py         # augments `colab skill` / `colab SKILL` output dynamically
    ├── commands.py          # the `colab dfs` subcommands (typer)
    ├── dfs_creds.py         # OAuth consent / credential storage
    ├── drive_mount.py       # remote mount script (metadata shim + DriveFS)
    └── remote.py            # executes code inside the runtime via the colab_cli API
```
