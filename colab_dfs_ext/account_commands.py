"""The `colab account ...` subcommand group for multi-account management."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import typer
from typing_extensions import Annotated

from . import accounts

account_app = typer.Typer(
    name="account",
    no_args_is_help=True,
    help="Manage multiple Google Colab accounts and shared Drive bindings.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _drive_summary(acct: str, reg: dict[str, Any]) -> dict[str, Any]:
    drv_acct, drv_src = accounts.resolve_drive_account(acct, for_consent=False)
    present = os.path.exists(accounts.dfs_store_path(drv_acct))
    if drv_acct == acct:
        label = "ok" if present else "missing"
    elif drv_src.startswith("bound:"):
        label = f"->{drv_acct} ({'ok' if present else 'missing'})"
    else:
        label = f"default:{drv_acct} ({'ok' if present else 'missing'})"
    return {
        "account": drv_acct,
        "source": drv_src,
        "ready": present,
        "label": label,
    }


def _exit_account_error(exc: Exception) -> None:
    typer.echo(f"[account] {exc}", err=True)
    raise typer.Exit(code=1)


# --------------------------------------------------------------------------------------
# OAuth2 & ADC login helpers
# --------------------------------------------------------------------------------------


def _start_url_only_login(acct: str, email_hint: Optional[str]) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from colab_cli import auth
    from colab_cli.common import state
    from . import cli_hooks

    reg = accounts.load_registry()
    hint = email_hint or (reg.get("accounts", {}).get(acct) or {}).get("email")
    client_config = cli_hooks.load_colab_client_config(getattr(state, "client_oauth_config", None))

    flow = InstalledAppFlow.from_client_config(client_config, auth.PUBLIC_SCOPES)
    flow.redirect_uri = auth.REMOTE_REDIRECT_URI
    kwargs: dict[str, Any] = {"prompt": "consent", "token_usage": "remote"}
    if hint:
        kwargs["login_hint"] = hint
    auth_url, oauth_state = flow.authorization_url(**kwargs)

    accounts._atomic_write_json(
        accounts.oauth_pending_path(acct),
        {
            "account": acct,
            "client_config": client_config,
            "scopes": list(auth.PUBLIC_SCOPES),
            "redirect_uri": flow.redirect_uri,
            "state": oauth_state,
            "code_verifier": getattr(flow, "code_verifier", None),
            "email_hint": hint,
            "created_at": accounts._now_iso(),
        },
        mode=0o600,
    )
    typer.echo(auth_url)
    typer.echo(
        f"\n# Open the URL above, approve for account '{acct}', then finish with:\n"
        f"#   colab account login {acct} --code <PASTED_CODE>",
        err=True,
    )


def _finish_code_login(acct: str, code: str) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from . import cli_hooks

    pending_path = accounts.oauth_pending_path(acct)
    if not os.path.exists(pending_path):
        raise accounts.AccountError(
            f"Nothing to finish for account {acct!r}: {pending_path} missing. "
            f"Run `colab account login {acct} --url-only` first."
        )
    with open(pending_path, "r", encoding="utf-8") as f:
        pend = json.load(f)

    flow = InstalledAppFlow.from_client_config(pend["client_config"], pend["scopes"])
    flow.redirect_uri = pend["redirect_uri"]
    flow.oauth2state = pend.get("state")
    flow.code_verifier = pend.get("code_verifier")
    flow.fetch_token(code=code.strip())
    creds = flow.credentials

    storage = accounts.save_colab_oauth_token(acct, creds.to_json())
    cli_hooks.record_authorized_identity(acct, getattr(creds, "token", None))
    if os.path.exists(pending_path):
        os.unlink(pending_path)
    return storage


def _interactive_oauth_login(acct: str, email_hint: Optional[str]) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from colab_cli import auth
    from colab_cli.common import state
    from . import cli_hooks

    reg = accounts.load_registry()
    hint = email_hint or (reg.get("accounts", {}).get(acct) or {}).get("email")
    client_config = cli_hooks.load_colab_client_config(getattr(state, "client_oauth_config", None))

    flow = InstalledAppFlow.from_client_config(client_config, auth.PUBLIC_SCOPES)
    flow.redirect_uri = auth.REMOTE_REDIRECT_URI
    kwargs: dict[str, Any] = {"prompt": "consent", "token_usage": "remote"}
    if hint:
        kwargs["login_hint"] = hint
    auth_url, _ = flow.authorization_url(**kwargs)

    hint_suffix = f" ({hint})" if hint else ""
    typer.echo(
        f"\n[account] Authorizing '{acct}'{hint_suffix}. Visit this URL in any browser:\n",
        err=True,
    )
    typer.echo("  " + auth_url + "\n", err=True)
    typer.echo("After approving, Google will display an authorization code.", err=True)
    code = input("Enter the authorization code: ").strip()
    if not code:
        raise accounts.AccountError("Authorization code cannot be empty.")

    flow.fetch_token(code=code)
    creds = flow.credentials
    storage = accounts.save_colab_oauth_token(acct, creds.to_json())
    cli_hooks.record_authorized_identity(acct, getattr(creds, "token", None))

    pending_path = accounts.oauth_pending_path(acct)
    if os.path.exists(pending_path):
        os.unlink(pending_path)
    return storage


def _run_dfs_consent_for_account(
    acct: str,
    *,
    flavor: str,
    login_hint: Optional[str],
    no_browser: bool,
) -> int:
    from . import dfs_creds

    argv = ["consent", "--account", acct, "--flavor", flavor, "--minimal"]
    if login_hint:
        argv += ["--login-hint", login_hint]
    if no_browser:
        argv.append("--no-browser")
    try:
        dfs_creds.main(argv)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)


# --------------------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------------------


@account_app.command(name="list")
@account_app.command(name="ls", hidden=True)
def list_accounts(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")] = False,
):
    """List configured Colab accounts (offline, never blocks on locked keyrings)."""
    reg = accounts.load_registry()
    active = reg.get("active")
    rows: list[dict[str, Any]] = []
    for name, meta in reg.get("accounts", {}).items():
        meta = meta if isinstance(meta, dict) else {}
        auth_info = accounts.inspect_colab_auth_nonblocking(name)
        drv = _drive_summary(name, reg)
        sess_map = accounts.read_local_sessions(name)
        rows.append({
            "account": name,
            "active": name == active,
            "email": meta.get("email"),
            "user_id": meta.get("user_id"),
            "auth": auth_info,
            "drive": drv,
            "sessions": sorted(sess_map.keys()),
        })

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "active": active,
                    "default_drive_account": reg.get("default_drive_account"),
                    "accounts": rows,
                },
                indent=2,
            )
        )
        return

    for r in rows:
        mark = "*" if r["active"] else " "
        email = r["email"] or "-"
        ready = r["auth"].get("ready")
        if ready is True:
            auth_str = f"ok ({r['auth'].get('storage')})"
        elif ready is None:
            auth_str = f"? ({r['auth'].get('storage')})"
        else:
            auth_str = f"missing ({r['auth'].get('provider')})"
        sess_list = r["sessions"]
        sess_str = f"{len(sess_list)} ({', '.join(sess_list)})" if sess_list else "0"
        typer.echo(
            f"{mark} {r['account']:<14} | email: {email:<26} | "
            f"colab: {auth_str:<22} | drive: {r['drive']['label']:<18} | sessions: {sess_str}"
        )


@account_app.command(name="current")
def current(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")] = False,
):
    """Print the currently resolved account profile."""
    acct = accounts.current_account()
    if not json_out:
        typer.echo(acct)
        return

    reg = accounts.load_registry()
    meta = reg.get("accounts", {}).get(acct, {})
    drv_acct, drv_src = accounts.resolve_drive_account(acct, for_consent=False)
    typer.echo(
        json.dumps(
            {
                "account": acct,
                "source": accounts.current_account_source(),
                "email": meta.get("email"),
                "user_id": meta.get("user_id"),
                "auth_provider": (meta.get("auth_provider") or "oauth2").lower(),
                "drive_account": drv_acct,
                "drive_source": drv_src,
                "sessions_path": accounts.sessions_path(acct),
                "history_dir": accounts.history_dir(acct),
                "dfs_credentials_path": accounts.dfs_store_path(drv_acct),
                "oauth_token_file": accounts.oauth_token_file_path(acct),
                "auth": accounts.inspect_colab_auth_nonblocking(acct),
            },
            indent=2,
        )
    )


@account_app.command(name="use")
@account_app.command(name="switch", hidden=True)
def use_account(
    name: Annotated[str, typer.Argument(help="Account profile name or email")],
):
    """Switch the global default active Colab account."""
    try:
        resolved, reg = accounts.set_active_account(name)
    except accounts.AccountError as exc:
        _exit_account_error(exc)
        return
    email = (reg.get("accounts", {}).get(resolved) or {}).get("email")
    suffix = f" ({email})" if email else ""
    typer.echo(f"[account] Active account -> {resolved}{suffix}")


@account_app.command(name="add")
def add_account(
    name: Annotated[str, typer.Argument(help="New account profile name")],
    email: Annotated[Optional[str], typer.Option("--email", help="Account email hint (login_hint)")] = None,
    with_dfs: Annotated[
        bool,
        typer.Option("--with-dfs/--no-dfs", help="Also run DriveFS consent after Colab login"),
    ] = True,
    drive_account: Annotated[
        Optional[str],
        typer.Option("--drive-account", help="Bind Drive mounts to an existing storage account"),
    ] = None,
    flavor: Annotated[str, typer.Option("--flavor", help="DriveFS consent flavor: dfs | sdk | custom")] = "dfs",
    auth_method: Annotated[str, typer.Option("--auth", help="Colab auth strategy: oauth2 | adc")] = "oauth2",
    adc_file: Annotated[
        Optional[str],
        typer.Option("--adc-file", help="Path to ADC JSON credentials file (implies --auth adc)"),
    ] = None,
    use: Annotated[
        bool,
        typer.Option("--use/--no-use", help="Switch active account to this profile"),
    ] = True,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not auto-open browser")] = False,
    url_only: Annotated[
        bool,
        typer.Option("--url-only", help="Print Colab OAuth URL and exit (finish with `colab account login`)"),
    ] = False,
):
    """Register and authenticate a new Colab account profile."""
    try:
        accounts.validate_account_name(name)
        resolved_drive: Optional[str] = None
        if drive_account:
            resolved_drive = accounts.lookup_account(drive_account, allow_create=False)

        provider = auth_method.strip().lower()
        if adc_file:
            provider = "adc"
            expanded_adc = os.path.abspath(os.path.expanduser(adc_file))
            if not os.path.isfile(expanded_adc):
                raise accounts.AccountError(f"ADC credentials file not found: {expanded_adc}")
        else:
            expanded_adc = None

        if provider not in ("oauth2", "adc"):
            raise accounts.AccountError(f"Unsupported --auth {auth_method!r}; choose oauth2 or adc.")

        accounts.ensure_account(
            name,
            email=email,
            auth_provider=provider,
            adc_credentials_file=expanded_adc,
            drive_account=resolved_drive,
        )
    except accounts.AccountError as exc:
        _exit_account_error(exc)
        return

    if provider == "adc":
        from colab_cli.auth import AuthProvider, get_credentials
        from . import cli_hooks

        with accounts.account_context(name, source="add", non_interactive=True):
            try:
                sess = get_credentials(provider=AuthProvider.ADC)
                cli_hooks.record_authorized_identity(name, getattr(sess.credentials, "token", None))
            except Exception as exc:
                _exit_account_error(exc)
                return
        typer.echo(f"[account] Configured ADC account '{name}'.")
    elif url_only:
        try:
            _start_url_only_login(name, email)
        except Exception as exc:
            _exit_account_error(exc)
            return
        if use:
            accounts.set_active_account(name)
        return
    else:
        try:
            with accounts.account_context(name, source="add"):
                storage = _interactive_oauth_login(name, email)
        except Exception as exc:
            _exit_account_error(exc)
            return
        reg = accounts.load_registry()
        logged_email = (reg.get("accounts", {}).get(name) or {}).get("email")
        typer.echo(
            f"[account] Logged in '{name}'"
            + (f" ({logged_email})" if logged_email else "")
            + f" [storage: {storage}]"
        )
        if with_dfs and not resolved_drive:
            typer.echo(f"[account] Starting DriveFS consent for '{name}'...", err=True)
            rc = _run_dfs_consent_for_account(
                name,
                flavor=flavor,
                login_hint=logged_email or email,
                no_browser=no_browser,
            )
            if rc != 0:
                typer.echo(
                    f"[account] Warning: DriveFS consent exited with {rc}. "
                    f"Re-run later with `colab -a {name} dfs consent`.",
                    err=True,
                )

    if use:
        accounts.set_active_account(name)
        typer.echo(f"[account] Active account -> {name}")


@account_app.command(name="login")
def login_account(
    name: Annotated[
        Optional[str],
        typer.Argument(help="Account name (defaults to currently active account)"),
    ] = None,
    email: Annotated[Optional[str], typer.Option("--email", help="Account email hint (login_hint)")] = None,
    with_dfs: Annotated[
        bool,
        typer.Option("--with-dfs/--no-dfs", help="Also run DriveFS consent after login"),
    ] = False,
    flavor: Annotated[str, typer.Option("--flavor", help="DriveFS consent flavor: dfs | sdk | custom")] = "dfs",
    url_only: Annotated[
        bool,
        typer.Option("--url-only", help="Print OAuth consent URL and save PKCE state for --code"),
    ] = False,
    code: Annotated[
        Optional[str],
        typer.Option("--code", help="Finish a --url-only login using the pasted authorization code"),
    ] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not auto-open browser")] = False,
):
    """Log in or re-authenticate a Colab account (supports headless --url-only / --code)."""
    try:
        if name:
            acct = accounts.lookup_account(name, allow_create=not bool(code))
        else:
            acct = accounts.current_account()
        if email:
            accounts.ensure_account(acct, email=email)
    except accounts.AccountError as exc:
        _exit_account_error(exc)
        return

    try:
        if url_only:
            _start_url_only_login(acct, email)
            return
        if code:
            storage = _finish_code_login(acct, code)
        else:
            with accounts.account_context(acct, source="login"):
                storage = _interactive_oauth_login(acct, email)
    except Exception as exc:
        _exit_account_error(exc)
        return

    reg = accounts.load_registry()
    logged_email = (reg.get("accounts", {}).get(acct) or {}).get("email")
    typer.echo(
        f"[account] Logged in '{acct}'"
        + (f" ({logged_email})" if logged_email else "")
        + f" [storage: {storage}]"
    )
    if with_dfs:
        rc = _run_dfs_consent_for_account(
            acct,
            flavor=flavor,
            login_hint=logged_email or email,
            no_browser=no_browser,
        )
        if rc != 0:
            raise typer.Exit(code=rc)


@account_app.command(name="rename")
def rename_account_cmd(
    old: Annotated[str, typer.Argument(help="Existing account name or email")],
    new: Annotated[str, typer.Argument(help="New account profile name")],
):
    """Rename an existing account profile and migrate its sessions/tokens."""
    try:
        reg = accounts.rename_account(old, new)
    except accounts.AccountError as exc:
        _exit_account_error(exc)
        return
    typer.echo(f"[account] Renamed '{old}' -> '{new}' (active: {reg.get('active')})")


@account_app.command(name="remove")
@account_app.command(name="rm", hidden=True)
def remove_account_cmd(
    name: Annotated[str, typer.Argument(help="Account profile name or email")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Remove even if local sessions are recorded"),
    ] = False,
):
    """Remove an account profile and its isolated state directory."""
    try:
        resolved, reg = accounts.remove_account(name, force=force)
    except accounts.AccountError as exc:
        _exit_account_error(exc)
        return
    typer.echo(f"[account] Removed '{resolved}' (active: {reg.get('active')})")


@account_app.command(name="set-drive")
def set_drive_cmd(
    drive_account: Annotated[
        Optional[str],
        typer.Argument(help="Storage account supplying DriveFS credentials"),
    ] = None,
    account: Annotated[
        Optional[str],
        typer.Option("-a", "--account", help="Target compute account (default: active account)"),
    ] = None,
    as_default: Annotated[
        bool,
        typer.Option("--default", help="Set/clear global fallback Drive account"),
    ] = False,
    clear: Annotated[bool, typer.Option("--clear", help="Clear the Drive binding")] = False,
):
    """Bind a compute account (or global default) to a shared Drive storage account."""
    try:
        reg = accounts.set_drive_binding(
            drive_account,
            target_account=account,
            as_default=as_default,
            clear=clear,
        )
    except accounts.AccountError as exc:
        _exit_account_error(exc)
        return

    if as_default:
        val = reg.get("default_drive_account")
        typer.echo(f"[account] Default shared Drive account -> {val or '(cleared)'}")
    else:
        target = account or accounts.current_account()
        resolved_target = accounts.lookup_account(target, reg)
        val = (reg.get("accounts", {}).get(resolved_target) or {}).get("drive_account")
        typer.echo(
            f"[account] Drive binding for '{resolved_target}' -> {val or '(own credentials)'}"
        )


@account_app.command(name="status")
def status_accounts(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")] = False,
):
    """Concurrently query CCU balance, burn rate, and active sessions across all accounts."""
    from . import cli_hooks

    results = cli_hooks.probe_all_accounts(include_usage=True, include_sessions=True)
    if json_out:
        typer.echo(json.dumps(results, indent=2))
        return

    if not results:
        typer.echo("[account] No accounts configured.")
        return

    for r in results:
        mark = "*" if r.get("active") else " "
        email = r.get("email") or "-"
        drv = f"drive:{r['drive_account']}({'ok' if r.get('drive_ready') else 'missing'})"
        if r.get("error"):
            typer.echo(f"[{mark}{r['account']}] ({email}) | {drv} | ERROR: {r['error']}")
            continue
        u = r.get("usage") or {}
        assigns = r.get("assignments") or []
        sess_desc = (
            ", ".join(f"{a['name']}:{a['hardware']}" for a in assigns)
            if assigns
            else "none"
        )
        typer.echo(
            f"[{mark}{r['account']}] ({email}) | "
            f"CCU: {u.get('balance', 0.0):.2f} ({u.get('rate_hourly', 0.0):.2f}/hr) | "
            f"sessions: {sess_desc} | {drv}"
        )
