# colab-ext — multi-account, zero-browser Drive & agent-skill extension for google-colab-cli

> [中文](README.zh-CN.md) | English

*(Formerly `colab-dfs-ext`; all legacy `colab dfs ...` commands and `COLAB_DFS_*` env vars remain supported.)*

Adds **isolated multi-account profiles (`colab account`, `-a / --account`)**, **browser-less Google Drive mounting (`colab drivemount` & `colab drive`)**, **extension plumbing (`colab ext`)**, and **extended AI-agent guidance (`colab skill`)** to the `colab` CLI —
**without touching a single file of google-colab-cli**: colab-cli upgrades are unaffected,
no re-integration is needed, and worst case a single command repairs it. All extension modules
aside from `google-colab-cli` runtime integrations use pure Python standard library.

```
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

# Zero-browser Drive mounting (built-in `drivemount` + `drive`; `dfs` is a legacy alias)
colab drivemount [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab drive mount   [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab drive umount  [-a ACCT] [-s SESSION] [path]
colab drive status  [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab drive consent [-a ACCT] [--flavor dfs|sdk|custom] [--url-only | --code CODE]
colab drive show | token | clear [-a ACCT] [--drive-account DRV]

# Extension diagnostics & AI-agent guidance
colab ext doctor [-a ACCT]              # Health check: injection / accounts / credentials
colab ext install                       # Re-inject `colab_ext.pth` after a colab-cli upgrade
colab ext version                       # Print extension version and path info JSON
colab skill                             # Upstream SKILL.md augmented with colab-ext docs
```

Every subcommand accepts `-- -<any args>` to pass extra flags through to the underlying tools —
flags not exposed by this extension remain usable.

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-ext/main/install.sh | sh
```

(wget works too: `wget -qO- ... | sh`. The script downloads the repo archive, extracts to a
temp dir, and runs `python3 install.py` — falling back to `deluxebear/colab-dfs-ext` if needed.
Use `COLAB_EXT_REPO` / `COLAB_EXT_REF` to override the source.)

## Requirements

- Python 3.8+ (both `install.py` and the core modules are pure standard library)
- [`google-colab-cli`](https://pypi.org/project/google-colab-cli/) installed and working
  (`uv tool install google-colab-cli`), i.e. the `colab` command
- A connected colab session (`colab connect -s <session>`)
- Colab runtimes ship `/opt/google/drive` (DriveFS) already — no upload needed

## Usage

### Step 1 · Install the extension

One-line install (above), or manually:

```bash
git clone https://github.com/deluxebear/colab-ext
cd colab-ext                # or your checkout dir
python3 install.py          # any python3 works
colab ext doctor            # verify the injection succeeded
```

`install.py` does two things:

1. Copies the extension source to `~/.colab-ext/` (**outside** colab-cli's venv — upgrades
   never touch it) and cleans up any legacy `~/.colab-dfs-ext/` directory;
2. Writes **a single file**, `colab_ext.pth` (~600 bytes), into the colab-cli environment's
   site-packages (and automatically removes any legacy `colab_dfs.pth`).

Useful install flags:

```bash
python3 install.py --status        # where the .pth / source / colab_cli live
python3 install.py --link          # dev mode: symlink this repo; code changes take effect immediately
python3 install.py --site-dir DIR  # target a specific site-packages
```

### Step 2 · One-time authorization (only once, ever)

```bash
colab drive consent          # browser pops the Google consent page -> allow
colab drive show             # inspect stored credentials (refresh token / GaiaId / email)
```

By default this uses the reverse-engineered DriveFS built-in client (flavor `dfs` — Google's own
"Google Drive for desktop", a verified app); the localhost callback is caught automatically, so
there is no authorization code to copy-paste. Other flavors: `--flavor sdk` (colab-cli's bundled
client, works headless but easily blocked by Google) and `--flavor custom --client-id ...
--client-secret ...` (your own GCP project — most robust long-term). On machines that have
already consented once, `colab drive consent --import` works with no browser at all.

DriveFS credentials live in `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`
(with automatic non-destructive adoption from legacy `~/.config/colab-cli/dfs_credentials.json` into
`default`); every mount afterwards is zero-browser.

### Step 3 · Everyday mounting

```bash
colab connect -s test        # connect to the colab session (if not already)
colab drivemount -s test     # mounts at /content/drive via zero-browser DriveFS
colab drive status -s test   # mount status
colab drive umount -s test   # unmount

colab drivemount -s test --force --purge-state  # remount: clear stale processes/dirs/local state
colab drive mount -s test --dry-run             # only print config and the remote payload
colab drivemount -s test --readonly             # read-only mount
```

### Built-in `colab drivemount` enhanced by default

`colab drivemount` is automatically upgraded to use the zero-browser DriveFS backend when
credentials are present, supporting `-a/--account`, `--drive-account`, `--readonly`, `--force`,
`--purge-state`, `--timeout`, `--dry-run`, and automatic session-owner routing:

```bash
colab drivemount -s test                             # zero-browser DriveFS + auto session routing
colab drivemount -s test --drive-account storage     # mount another profile's Drive
colab drivemount -s test --readonly --no-fallback    # fail fast if DriveFS credentials are missing
colab drivemount -s test --upstream                  # force original interactive browser flow
COLAB_DRIVE_DEFAULT=0 colab drivemount -s test       # disable zero-browser override via env var
```

When DriveFS credentials are missing (and no DriveFS-only flag or `--no-fallback` is given),
`colab drivemount` falls back to the upstream interactive flow with an account reminder before the URL.

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
colab account add gpu2 --no-drive --drive-account work   # reuse work's Drive across compute accounts

# Headless / SSH / agent two-step login (works for both Colab auth and DriveFS)
colab account login work --url-only
colab account login work --code <PASTED_CODE>
colab -a work drive consent --flavor sdk --url-only
colab -a work drive consent --code <PASTED_CODE>

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
colab -a gpu2 drivemount -s gpu-a --drive-account main   # one-off override
```

When running AI agents against `colab`, `colab skill` / `colab SKILL` automatically includes full
`colab-ext` instructions for `colab drivemount`, `colab drive`, `colab account`, and `colab ext`
(preserving valid YAML frontmatter).

## Environment variables (with legacy compatibility)

| Variable | Purpose | Legacy alias |
|---|---|---|
| `COLAB_ACCOUNT` | Default account profile / email override | — |
| `COLAB_DRIVE_ACCOUNT` | Override Drive credential owner account | `COLAB_DFS_ACCOUNT` |
| `COLAB_DRIVE_DEFAULT` | Set `0` to disable zero-browser `drivemount` override | `COLAB_DFS_DEFAULT` |
| `COLAB_EXT_SOURCE` | Override extension source directory lookup | `COLAB_DFS_EXT` |
| `COLAB_EXT_DEBUG` | Set `1` for verbose loader & registration diagnostics | `COLAB_DFS_DEBUG` |
| `COLAB_EXT_REPO` / `COLAB_EXT_REF` | Override GitHub repo/ref in `install.sh` | `COLAB_DFS_REPO` / `COLAB_DFS_REF` |

## Coexisting with upgrades (verified in practice)

| Scenario | Result |
|---|---|
| `uv tool install google-colab-cli --reinstall` (rebuild the tool env) | `.pth` **survives** (uv only reinstalls its own packages, doesn't clean foreign files); extension commands work right away |
| `colab update --install` | Internally runs `uv tool install -U`; the extension wraps the `update` command at registration time and re-checks/re-wires itself once the upgrade finishes (`[colab-ext] extension still wired: ...`) |
| Worst case (Python version change, venv manually wiped) | Extension disappears, but `~/.colab-ext` is still there: `python3 ~/.colab-ext/install.py` restores it in one command |

## Uninstall

```bash
python3 install.py --uninstall          # remove colab_ext.pth (and any legacy .pth)
rm -rf ~/.colab-ext                     # (optional) delete the extension source copy
```

## Troubleshooting

```bash
colab ext doctor                # health check: injection / accounts / credentials, with fix hints
COLAB_EXT_DEBUG=1 colab ext doctor   # verbose diagnostics from the loader/injection
```

- Extension commands gone → re-wire with `python3 ~/.colab-ext/install.py`.
- Changed this repo's code but behavior didn't change → `~/.colab-ext` holds the snapshot
  from install time; re-run `python3 install.py` (or switch to `--link`). The `source_dir()`
  lookup order is: `COLAB_EXT_SOURCE` (`COLAB_DFS_EXT`) env var → current code location → `~/.colab-ext`.

## How the injection works (why upgrades don't break it)

- colab-cli is a Typer app: `colab_cli.cli` exposes a module-level `app` object, and the official
  command modules themselves follow the `register(app)` convention — the extension wraps the
  callback/auth/state hooks cleanly and registers `account` (`acct`), `drive` (hidden alias `dfs`),
  and `ext` subgroups on that same `app`. **Zero intrusion.**
- A `.pth` file is the only hook site.py executes automatically: one line,
  `import os, sys; exec(compile("try: ... import colab_ext_loader ..."))`, which adds
  `~/.colab-ext` to `sys.path` and eagerly imports the loader; the loader calls
  `colab_ext.register(app)`.
- Defensive: the whole snippet is wrapped in try/except. If the extension is missing or broken it
  is silently skipped, and **`colab` itself can never be broken by it** (set `COLAB_EXT_DEBUG=1`
  to see why it was skipped).

## Known implementation details / gotchas

- `remote.exec_code` uses colab_cli's own `ColabRuntime.execute_code()` (no subprocess, no temp
  files) and **must call `runtime.stop()` in a finally block** — otherwise the kernel client's
  non-daemon threads hang the `colab` process on exit.
- Per-account DriveFS credentials are stored in `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`
  (legacy `~/.config/colab-cli/dfs_credentials.json` is automatically adopted into `default` and kept intact).

## Layout

```
colab-ext/
├── install.sh               # curl | sh one-line bootstrap installer (POSIX sh)
├── install.py               # install / repair / uninstall / status (pure stdlib)
├── colab_ext_loader.py      # loader imported by the colab_ext.pth startup hook
├── tests/
│   └── test_colab_ext.py    # hermetic multi-account, DriveFS, drivemount, and skill tests
└── colab_ext/               # the extension itself (copied/symlinked to ~/.colab-ext/)
    ├── __init__.py          # register(app) entry point
    ├── inject.py            # colab_ext.pth writing, legacy cleanup, and state management
    ├── accounts.py          # account registry, isolation paths, migration, keyring/file backend
    ├── account_commands.py  # `colab account` subcommands (typer)
    ├── cli_hooks.py         # global -a/--account, auth/state isolation, --all, drivemount & skill hooks
    ├── skill_doc.py         # augments `colab skill` / `colab SKILL` output dynamically
    ├── commands.py          # `colab drive` (`dfs`) & `colab ext` subcommands (typer)
    ├── dfs_creds.py         # OAuth consent / credential storage
    ├── drive_mount.py       # remote mount script (metadata shim + DriveFS)
    └── remote.py            # executes code inside the runtime via the colab_cli API
```
