"""The `colab dfs ...` command group."""

from __future__ import annotations

import datetime
import json
import os
import time
from typing import Optional

import typer
from typing_extensions import Annotated

from . import __version__

# Keep every command tolerant of unknown flags: they are forwarded to the underlying tool,
# so the CLI surface can never block access to a flag we did not think to expose.
EXTRA = {"allow_extra_args": True, "ignore_unknown_options": True,
         "help_option_names": ["-h", "--help"]}

dfs = typer.Typer(name="dfs", no_args_is_help=True,
                  context_settings={"help_option_names": ["-h", "--help"]})

Session = Annotated[Optional[str], typer.Option("-s", "--session",
                                                help="colab session name (default: the only running one)")]
Account = Annotated[Optional[str], typer.Option("-a", "--account",
                                                help="Colab account profile or email")]
DriveAccount = Annotated[Optional[str], typer.Option("--drive-account",
                                                     help="Mount/inspect another profile's Drive credentials")]
Path = Annotated[str, typer.Argument(help="mountpoint inside the runtime (default /content/drive)")]
Timeout = Annotated[int, typer.Option("--timeout", help="seconds to wait for the mount")]
Force = Annotated[bool, typer.Option("--force", help="remount even if already mounted")]


def _activate_dfs_account(
    account: Optional[str],
    *,
    session: Optional[str] = None,
    allow_routing: bool = False,
    allow_create: bool = False,
) -> str:
    """Resolve and bind the active account + colab_cli state for `colab dfs` commands."""
    from . import accounts

    if account is None and accounts.ACTIVE_ACCOUNT.get() is not None:
        acct = accounts.current_account()
    else:
        try:
            acct, src = accounts.resolve_account(
                account,
                session_name=session,
                allow_session_routing=allow_routing,
                allow_create=allow_create,
            )
        except accounts.AccountError as exc:
            typer.echo(f"[dfs] {exc}", err=True)
            raise typer.Exit(code=1)
        accounts.ACTIVE_ACCOUNT.set(acct)
        accounts.ACTIVE_SOURCE.set(src)

    try:
        from colab_cli.common import state

        if not getattr(state, "_colab_dfs_explicit_config", False):
            target = accounts.sessions_path(acct)
            if getattr(state, "config_path", None) != target:
                state.config_path = target
                state._store = None
                state._history = None
                state._client = None
                state._sessions = None
    except Exception:
        pass
    return acct


@dfs.callback()
def _dfs_callback(ctx: typer.Context, account: Account = None):
    """Apply `colab dfs -a <account>` if passed before the dfs subcommand."""
    if account is not None:
        allow_create = getattr(ctx, "invoked_subcommand", None) == "consent"
        _activate_dfs_account(account, allow_create=allow_create)


# --- mount --------------------------------------------------------------------------------
def _run_mount(cfg: dict, session: Optional[str], verbose: bool = True) -> int:
    """Execute the DriveFS mount payload in the runtime and report the result."""
    from . import drive_mount, remote

    name = remote.resolve_session(session)
    if verbose:
        colab_acct = cfg.get("colab_account")
        drive_acct = cfg.get("drive_account")
        if colab_acct and drive_acct:
            acct_tag = (
                f"{colab_acct} -> drive:{drive_acct}"
                if drive_acct != colab_acct
                else str(colab_acct)
            )
            print("[dfs] %s (%s) -> %s (%s mode), browser-free ..."
                  % (name, acct_tag, cfg["mountpoint"], cfg["mode"]))
        else:
            print("[dfs] %s -> %s (%s mode), browser-free ..."
                  % (name, cfg["mountpoint"], cfg["mode"]))
    t0 = time.time()
    text = remote.exec_code(name, drive_mount.payload(cfg), int(cfg["timeout"]) + 90)
    res = drive_mount.parse_result(text)
    if res and res.get("ok"):
        mp = res.get("mountpoint") or cfg["mountpoint"]
        try:
            from colab_cli.common import state

            s = state.store.get(name)
            if s is not None:
                s.last_execution = (
                    "automation:drivemount",
                    None,
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                state.store.add(s)
            state.history.log_event(
                name,
                "automation",
                {
                    "op": "drivemount",
                    "path": mp,
                    "code": f"from google.colab import drive\ndrive.mount({mp!r})",
                },
            )
            state.history.log_event(
                name,
                "automation_result",
                {
                    "op": "drivemount",
                    "outputs": [{"output_type": "stream", "text": f"Mounted at {mp}\n"}],
                },
            )
        except Exception:
            pass
        if verbose:
            if res.get("note"):
                print("[dfs] OK: %s" % res["note"])
            else:
                print("[dfs] OK in %.1fs: Drive at %s (%s entries top-level, mode=%s)"
                      % (time.time() - t0, mp,
                         res.get("entries"), res.get("mode") or cfg["mode"]))
            if res.get("shim"):
                print("[dfs] creds shim: %s  (keep the runtime alive; it re-serves tokens)"
                      % res["shim"])
        return 0
    if res:
        print("[dfs] FAILED: %s" % json.dumps(res, indent=2)[:4000])
        for key in ("drive_log", "dfs_log_errors", "log", "error", "hint"):
            if res.get(key):
                print("--- %s ---\n%s" % (key, res[key]))
    else:
        print("[dfs] the payload produced no DFS_RESULT; kernel output:\n%s" % text[-3000:])
    return 1


@dfs.command(context_settings=EXTRA)
def mount(
    ctx: typer.Context,
    session: Session = None,
    account: Account = None,
    drive_account: DriveAccount = None,
    path: Path = "/content/drive",
    mode: Annotated[str, typer.Option("--mode", help="shim (default, works with any client's token)"
                                           " | rt (pass --refresh_token to DriveFS)")] = "shim",
    force: Force = False,
    readonly: Annotated[bool, typer.Option("--readonly", help="read-only mount")] = False,
    timeout: Timeout = 120,
    port: Annotated[int, typer.Option("--port", help="shim port inside the runtime")] = 8012,
    from_env: Annotated[bool, typer.Option("--from-env",
                                           help="read COLAB_DRIVE_* env vars instead of the store")] = False,
    cli_token: Annotated[bool, typer.Option("--cli-token",
                                            help="use the colab-cli token (needs the drive scope; "
                                                 "default colab-cli tokens only have drive.file)")] = False,
    purge_state: Annotated[bool, typer.Option("--purge-state",
                                              help="delete the runtime's DriveFS local state first")] = False,
    user_id: Annotated[Optional[str], typer.Option("--user-id", help="account GaiaId override")] = None,
    email: Annotated[Optional[str], typer.Option("--email", help="account email override")] = None,
    drive_extra: Annotated[Optional[list[str]], typer.Option("--drive-extra",
                                                            help="extra flag for /opt/google/drive/drive")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print config + remote payload")] = False,
    via_cli: Annotated[bool, typer.Option("--via-cli",
                                          help="shell out to `colab exec` instead of the kernel API")] = False,
):
    """Mount Google Drive in the runtime with no browser and no dfs_ephemeral consent."""
    from . import drive_mount

    if " " in path:
        raise typer.BadParameter("mountpoint must not contain a space")
    acct = _activate_dfs_account(account, session=session, allow_routing=True)
    argv = ["--account", acct, "--path", path, "--mode", mode,
            "--timeout", str(timeout), "--port", str(port)]
    if drive_account:
        argv += ["--drive-account", drive_account]
    if session:
        argv += ["-s", session]
    for flag, on in (("--force", force), ("--readonly", readonly), ("--from-env", from_env),
                     ("--cli-token", cli_token), ("--purge-state", purge_state),
                     ("--dry-run", dry_run)):
        if on:
            argv.append(flag)
    for flag, val in (("--user-id", user_id), ("--email", email)):
        if val:
            argv += [flag, val]
    for extra in drive_extra or []:
        argv += ["--drive-extra", extra]
    argv += list(ctx.args)

    if dry_run or via_cli:
        raise typer.Exit(code=drive_mount.main(argv))

    cfg = drive_mount.make_cfg(session=session, path=path, mode=mode, force=force,
                               readonly=readonly, timeout=timeout, port=port,
                               cli_token=cli_token, from_env=from_env,
                               drive_extra=drive_extra or [], purge_state=purge_state,
                               user_id=user_id, email=email,
                               account=acct, drive_account=drive_account)
    raise typer.Exit(code=_run_mount(cfg, session))


@dfs.command(context_settings=EXTRA)
def umount(
    session: Session = None,
    account: Account = None,
    path: Path = "/content/drive",
):
    """Unmount Drive (and stop the in-runtime creds shim / drive processes)."""
    from . import remote

    _activate_dfs_account(account, session=session, allow_routing=True)
    name = remote.resolve_session(session)
    out = remote.exec_code(name, remote.render(remote.UMOUNT_REMOTE, path), timeout=120)
    line = [l for l in out.splitlines() if l.startswith("DFS_RESULT ")]
    if line:
        res = json.loads(line[0][len("DFS_RESULT "):])
        print("[dfs] %s %s (%s)" % (name, "unmounted" if not res.get("mounted") else "STILL MOUNTED",
                                    res.get("mountpoint")))
        raise typer.Exit(code=0 if not res.get("mounted") else 1)
    print(out[-2000:])
    raise typer.Exit(code=1)


@dfs.command(context_settings=EXTRA)
def status(
    session: Session = None,
    account: Account = None,
    drive_account: DriveAccount = None,
    path: Path = "/content/drive",
):
    """Is it mounted? Who is serving tokens? What does the runtime see?"""
    from . import accounts, remote

    acct = _activate_dfs_account(account, session=session, allow_routing=True)
    try:
        drive_acct, drive_src = accounts.resolve_drive_account(acct, explicit_drive=drive_account)
    except accounts.AccountError as exc:
        typer.echo(f"[dfs] {exc}", err=True)
        raise typer.Exit(code=1)

    name = remote.resolve_session(session)
    out = remote.exec_code(name, remote.render(remote.STATUS_REMOTE, path), timeout=90)
    line = [l for l in out.splitlines() if l.startswith("DFS_RESULT ")]
    if not line:
        print(out[-2000:])
        raise typer.Exit(code=1)
    res = json.loads(line[0][len("DFS_RESULT "):])
    print("account     : %s (drive: %s [%s])" % (acct, drive_acct, drive_src))
    print("session     : %s" % name)
    print("mountpoint  : %s  %s" % (res.get("mountpoint"),
                                    "MOUNTED" if res.get("mounted") else "not mounted"))
    for l in res.get("mount_line") or []:
        print("fuse        : %s" % l.strip()[:150])
    print("My Drive    : %s entries" % res.get("entries"))
    print("drive procs : %s   creds shim procs: %s" % (res.get("drive_procs"), res.get("shim_procs")))
    if res.get("shim_log"):
        print("shim log    : %s" % str(res["shim_log"]).replace("\n", " | ")[:200])
    print("colab env   : %s" % json.dumps(res.get("official_env") or {}))
    creds = accounts.dfs_store_path(drive_acct)
    print("local creds : %s (%s)" % (creds, "present" if os.path.exists(creds)
                                     else f"MISSING -> run `colab dfs consent -a {drive_acct}`"))


# --- credentials --------------------------------------------------------------------------
@dfs.command(context_settings=EXTRA)
def consent(
    ctx: typer.Context,
    account: Account = None,
    flavor: Annotated[Optional[str], typer.Option("--flavor",
                                                  help="dfs (default: loopback callback, nothing to "
                                                       "copy-paste) | sdk (auto-selected on --url-only) "
                                                       "| custom")] = None,
    minimal: Annotated[bool, typer.Option("--minimal", help="only openid + email + drive scopes")] = True,
    port: Annotated[int, typer.Option("--port", help="loopback port (0 = auto)")] = 0,
    user_id: Annotated[Optional[str], typer.Option("--user-id", help="account GaiaId")] = None,
    email: Annotated[Optional[str], typer.Option("--email", help="account email")] = None,
    login_hint: Annotated[Optional[str], typer.Option("--login-hint",
                                                      help="Google login_hint email pre-selected in browser")] = None,
    code: Annotated[Optional[str], typer.Option("--code", help="finish a --url-only flow later")] = None,
    url_only: Annotated[bool, typer.Option("--url-only", help="print the consent URL and exit")] = False,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="do not open the browser")] = False,
):
    """One-time Google consent -> refresh token in accounts/<account>/dfs_credentials.json."""
    from . import dfs_creds

    acct = _activate_dfs_account(account, allow_create=True)
    argv = ["consent", "--account", acct]
    if flavor is not None:
        argv += ["--flavor", flavor]
    elif not url_only and not code and "--flavor" not in ctx.args and "--client-id" not in ctx.args:
        argv += ["--flavor", "dfs"]
    if minimal:
        argv.append("--minimal")
    argv += ["--port", str(port)]
    for flag, val in (("--user-id", user_id), ("--email", email),
                      ("--login-hint", login_hint), ("--code", code)):
        if val:
            argv += [flag, val]
    for flag, on in (("--url-only", url_only), ("--no-browser", no_browser)):
        if on:
            argv.append(flag)
    argv += list(ctx.args)
    if not minimal:
        argv = [a for a in argv if a != "--minimal"]
    try:
        dfs_creds.main(argv)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


@dfs.command(context_settings=EXTRA)
def show(
    account: Account = None,
    drive_account: DriveAccount = None,
):
    """Summarise the stored refresh token (never prints the secret)."""
    from . import dfs_creds

    acct = _activate_dfs_account(account)
    argv = ["show", "--account", acct]
    if drive_account:
        argv += ["--drive-account", drive_account]
    try:
        dfs_creds.main(argv)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


@dfs.command(context_settings=EXTRA)
def token(
    account: Account = None,
    drive_account: DriveAccount = None,
    whoami: Annotated[bool, typer.Option("--whoami", help="also refresh/record user_id + email")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
):
    """Mint (and cache-check) an access token right now -- proves the refresh token works."""
    from . import dfs_creds

    acct = _activate_dfs_account(account)
    argv = ["token", "--account", acct]
    if drive_account:
        argv += ["--drive-account", drive_account]
    if whoami:
        argv.append("--whoami")
    if json_out:
        argv.append("--json")
    try:
        dfs_creds.main(argv)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


@dfs.command(context_settings=EXTRA)
def clear(account: Account = None):
    """Delete stored DriveFS refresh credentials for an account."""
    from . import dfs_creds

    acct = _activate_dfs_account(account)
    try:
        dfs_creds.main(["clear", "--account", acct])
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


# --- plumbing ----------------------------------------------------------------------------─
@dfs.command(context_settings=EXTRA)
def install(
    site_dir: Annotated[Optional[str], typer.Option("--site-dir",
                                                    help="site-packages to write the .pth into")] = None,
    quiet: Annotated[bool, typer.Option("--quiet")] = False,
):
    """(Re-)inject this extension into the colab-cli environment. Run after an upgrade."""
    from . import inject

    rep = inject.ensure(site_dir=site_dir, quiet=quiet)
    if rep.get("error"):
        typer.echo("ERROR: %s" % rep["error"], err=True)
        raise typer.Exit(code=1)
    if not quiet:
        typer.echo("[colab-dfs] .pth : %s (%s)"
                   % (rep.get("pth"), "updated" if rep.get("changed") else "already there"))
        typer.echo("[colab-dfs] source: %s" % rep.get("source"))
        typer.echo("[colab-dfs] `colab dfs` is available in the NEXT colab invocation.")


@dfs.command(context_settings=EXTRA)
def doctor(
    account: Account = None,
    drive_account: DriveAccount = None,
):
    """Diagnose the injection, multi-account setup, credentials, and runtime state."""
    from . import accounts, inject

    acct = _activate_dfs_account(account)
    reg = accounts.load_registry()
    acct_map = reg.get("accounts", {})
    acct_names = list(acct_map.keys())
    meta = acct_map.get(acct, {})
    auth_info = accounts.inspect_colab_auth_nonblocking(acct)
    try:
        drive_acct, drive_src = accounts.resolve_drive_account(acct, explicit_drive=drive_account)
    except accounts.AccountError as exc:
        typer.echo(f"[dfs] {exc}", err=True)
        raise typer.Exit(code=1)

    st = inject.status()
    print("colab dfs extension v%s" % __version__)
    print("  registered here : %s" % ("yes" if inject.reload_ok() else "no (fresh process needed)"))
    print("  colab shim      : %s" % st["colab_shim"])
    print("  interpreter     : %s" % st["python"])
    print("  site-packages   : %s%s" % (st["site_packages"],
                                        "" if st["colab_cli_here"] else "  <- colab_cli NOT here"))
    print("  hook (.pth)     : %s  [%s]" % (st["pth"], "ok" if st["pth_present"] else "MISSING"))
    print("  extension source: %s" % (st["source"] or "MISSING"))
    if not st["pth_present"]:
        print("\n  fix: python3 %s/install.py   (or `colab dfs install`)"
              % (st["source"] or "~/.colab-dfs-ext"))

    print("  accounts file   : %s (%d account(s): %s)"
          % (accounts.registry_path(), len(acct_names), ", ".join(acct_names) or "none"))
    print("  active account  : %s (source: %s, email: %s)"
          % (acct, accounts.current_account_source(), meta.get("email") or "unknown"))
    ready = auth_info.get("ready")
    auth_state = "ready" if ready is True else ("unknown" if ready is None else "MISSING")
    print("  colab auth      : %s (%s via %s: %s)"
          % (auth_state, auth_info.get("provider"), auth_info.get("storage"), auth_info.get("detail")))
    print("  drive account   : %s (%s)" % (drive_acct, drive_src))

    creds = accounts.dfs_store_path(drive_acct)
    if os.path.exists(creds):
        try:
            with open(creds, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            print("  credentials     : %s" % creds)
            print("  client          : %s" % data.get("client_id"))
            print("  scopes          : %s" % " ".join(data.get("scopes", []))[:150])
            print("  user id / email : %s / %s" % (data.get("user_id"), data.get("email")))
            if not data.get("user_id"):
                print("  WARN: no user_id -> DriveFS will CANNOT_START_CORE; "
                      "re-run `colab dfs consent -a %s`" % drive_acct)
            if "drive" not in " ".join(data.get("scopes", [])):
                print("  WARN: no full drive scope -> DriveFS 403 insufficient_scope")
        except Exception as exc:
            print("  credentials     : unreadable: %r" % exc)
    else:
        print("  credentials     : MISSING -> run `colab dfs consent -a %s`" % drive_acct)
    next_acct = f" -a {acct}" if acct != reg.get("active") else ""
    next_drv = f" --drive-account {drive_acct}" if drive_account else ""
    print("\n  next step       : colab dfs mount%s%s -s <session>" % (next_acct, next_drv))


@dfs.command(context_settings=EXTRA)
def version():
    """Extension version + where it came from."""
    from . import inject

    st = inject.status()
    print(json.dumps({"extension": __version__, **st}, indent=2, default=str))
