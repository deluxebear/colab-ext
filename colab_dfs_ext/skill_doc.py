"""Augments `colab skill` / `colab SKILL` output with multi-account and `colab dfs` guidance.

Pure Python standard library only.
"""

from __future__ import annotations

import re

_MARKER = "## Multi-Account & Zero-Browser Drive Extension (`colab-dfs-ext`)"

EXTENSION_SKILL_SECTION = """
## Multi-Account & Zero-Browser Drive Extension (`colab-dfs-ext`)

This environment has `colab-dfs-ext` active. It adds **non-interactive Google Drive mounting (`colab dfs`)** and **isolated multi-account profiles (`colab account`, `-a/--account`, `COLAB_ACCOUNT`)** on top of `colab` without modifying upstream commands.

### Zero-Browser Google Drive Mounting (`colab drivemount` & `colab dfs` — Agent-Runnable)
`colab-dfs-ext` upgrades the built-in `colab drivemount` command so it runs **zero-browser via DriveFS by default** whenever Drive credentials exist (falling back to interactive consent only when unconfigured, unless `--no-fallback` or `--drive-account` / `--readonly` / `--purge-state` / `--dry-run` is passed). You can also invoke `colab dfs` directly for subcommands like `status`, `umount`, and `consent`:

- **Pre-check credentials**: `colab dfs show` (or `colab dfs doctor` for full diagnostics).
- **Mount / inspect / unmount**:
  - `colab drivemount -s <name> [/content/drive] [-a <acct>] [--drive-account <acct>] [--readonly] [--force] [--purge-state] [--no-fallback]`
    - Agent runs should pass `--no-fallback` so missing credentials fail immediately instead of blocking on interactive `dfs_ephemeral` consent.
    - Pass `--upstream` (or set `COLAB_DFS_DEFAULT=0`) to force the original interactive browser flow.
  - `colab dfs mount -s <name> [/content/drive] [--readonly] [--force --purge-state] [--drive-account <acct>]`
  - `colab dfs status -s <name> [/content/drive]`
  - `colab dfs umount -s <name> [/content/drive]`
- **Agent-safe two-step Drive consent** (when `dfs show` / `doctor` reports missing credentials):
  1. `colab [-a <acct>] dfs consent --flavor sdk --url-only` → prints an authorization URL to send to the user.
  2. `colab [-a <acct>] dfs consent --code <PASTED_CODE>` → exchanges the code and persists `dfs_credentials.json`.

### Multi-Account Management (`colab account`, `-a/--account`, `COLAB_ACCOUNT`)
- **Select an account for any command**: pass the global option *before* the subcommand: `colab -a <account> <command> ...` (accepts profile name or email), or set `COLAB_ACCOUNT=<account>`.
- **Offline inventory (never blocks or hits the network)**:
  - `colab account list --json` — accounts, active `*`, emails, Colab/DriveFS credential readiness, and local session names.
  - `colab account current [--json]` — active account (single-line name when `--json` is omitted).
- **Quota-aware multi-account discovery (parallel live probes)**:
  - `colab account status --json` — parallel query across all accounts returning CCU balance, burn rate, server assignments, and DriveFS readiness.
  - `colab usage --all [--json]` — CCU balance and hourly burn rate across all accounts.
  - `colab sessions --all [--json]` — active GPU/TPU/CPU assignments across all accounts.
  - When `colab new` fails with `400` (no accelerator entitlement) or `412` (too many assignments), run `colab account status --json` to pick another account with quota and retry with `colab -a <other> new ...`.
- **Switch, rename, remove**:
  - `colab account use <name>` — switch the global default account.
  - `colab account rename <old> <new>` — rename a profile (e.g., `default` → `personal`).
  - `colab account remove <name> [--force]` — remove a profile (blocked if local sessions exist unless `--force`).
- **Two-step agent-safe Colab OAuth login** (and interactive setup):
  1. `colab account login <name> [--email <hint>] --url-only` → prints the Google consent URL.
  2. `colab account login <name> --code <PASTED_CODE>` → exchanges the code and stores the account token.
  - Interactive (human at terminal): `colab account add <name> [--email <hint>] [--with-dfs | --no-dfs] [--drive-account <main>]`.
- **Shared Drive across compute accounts** (N compute accounts × 1 storage Drive):
  - Persistent binding: `colab account set-drive <storage_acct> [-a <compute_acct> | --default | --clear]`.
  - One-off override: `colab -a <compute_acct> dfs mount -s <name> --drive-account <storage_acct>`.
- **State isolation & smart session routing**:
  - Each account has isolated state at `~/.config/colab-cli/accounts/<account>/` (`sessions.json`, `history/`, `dfs_credentials.json`, `oauth_token.json`).
  - Commands operating on an **existing** `-s <session>` (`exec`, `ssh`, `status`, `stop`, `ls`, `upload`, `download`, `dfs mount/umount/status`, `url`) auto-route to the unique owning account when `-a` / `COLAB_ACCOUNT` is omitted (printing the routing notice to `stderr`, keeping `stdout` clean).
  - `colab new` and `colab run` never auto-route and always target the active (or `-a` / `COLAB_ACCOUNT`) account.
"""

_FRONTMATTER_RE = re.compile(r"\A(---\r?\n.*?\r?\n---\r?\n)", re.DOTALL)
_DESC_LINE_RE = re.compile(r"^(description:\s*)(.+?)(\r?)$", re.MULTILINE)
_DRIVEMOUNT_LINE_RE = re.compile(
    r"^(- `colab drivemount -s <name> \[PATH\]`[^\r\n]*)$",
    re.MULTILINE,
)


def augment_skill_markdown(base_md: str) -> str:
    """Inject multi-account and `colab dfs` sections into upstream `SKILL.md` text."""
    if not base_md or _MARKER in base_md:
        return base_md

    out = base_md

    fm_match = _FRONTMATTER_RE.match(out)
    if fm_match:
        fm = fm_match.group(1)

        def _update_desc(m: re.Match[str]) -> str:
            prefix, val, cr = m.group(1), m.group(2).rstrip(), m.group(3)
            extra = (
                " Also supports multi-account profiles (`colab account`, `-a/--account`) "
                "and browser-free Drive mounting (`colab dfs`)."
            )
            if "colab dfs" in val or "colab account" in val:
                return m.group(0)
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                q = val[0]
                inner = val[1:-1]
                return f"{prefix}{q}{inner}{extra}{q}{cr}"
            return f"{prefix}{val}{extra}{cr}"

        new_fm = _DESC_LINE_RE.sub(_update_desc, fm, count=1)
        out = new_fm + out[len(fm) :]

    def _annotate_drivemount(m: re.Match[str]) -> str:
        line = m.group(1)
        if "colab dfs mount" in line:
            return line
        return (
            "- `colab drivemount -s <name> [PATH]` — upgraded by `colab-dfs-ext` to mount "
            "**zero-browser via DriveFS automatically** when credentials exist "
            "(supports `-a <acct>`, `--drive-account <storage>`, `--readonly`, `--force`, "
            "`--purge-state`, and `--no-fallback` for fail-fast agent runs; see `colab dfs mount` below)."
        )

    out = _DRIVEMOUNT_LINE_RE.sub(_annotate_drivemount, out, count=1)

    if not out.endswith("\n"):
        out += "\n"
    return out + EXTENSION_SKILL_SECTION.lstrip("\n")
