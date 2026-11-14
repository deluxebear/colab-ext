"""Non-intrusive hooks wiring multi-account isolation and `colab skill` augmentation into `colab_cli`.

Nothing here edits files inside `google-colab-cli`. All hooks are applied in-memory on
`colab_cli` objects after `colab_cli.cli` builds `app`, and never touch the OS keyring
eagerly during startup or `--help`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
import inspect
import io
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import typer
from typing_extensions import Annotated

from . import accounts, skill_doc

_DISABLED_LEGACY_TOKEN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__legacy_token_disabled__"
)
_AccountOpt = Annotated[
    Optional[str],
    typer.Option(
        "-a",
        "--account",
        help="Account profile name or email (or set COLAB_ACCOUNT).",
    ),
]
_SessionsAllOpt = Annotated[
    bool,
    typer.Option(
        "-A",
        "--all",
        "--all-accounts",
        help="List active sessions across all configured accounts.",
    ),
]
_UsageAllOpt = Annotated[
    bool,
    typer.Option(
        "-A",
        "--all",
        "--all-accounts",
        help="Show compute-unit usage across all configured accounts.",
    ),
]
_JsonOpt = Annotated[
    bool,
    typer.Option("--json", help="Print machine-readable JSON output."),
]
_DrivemountSessionOpt = Annotated[
    Optional[str],
    typer.Option("-s", "--session", help="Session name"),
]
_DrivemountPathArg = Annotated[
    str,
    typer.Argument(help="Mount path"),
]
_DriveAccountOpt = Annotated[
    Optional[str],
    typer.Option("--drive-account", help="Mount another profile's Drive credentials (DriveFS)"),
]
_ReadonlyOpt = Annotated[
    bool,
    typer.Option("--readonly", help="Mount Drive read-only (DriveFS)"),
]
_ForceOpt = Annotated[
    bool,
    typer.Option("--force", help="Remount even if already mounted (DriveFS)"),
]
_PurgeStateOpt = Annotated[
    bool,
    typer.Option("--purge-state", help="Delete the runtime's DriveFS local state first"),
]
_TimeoutOpt = Annotated[
    int,
    typer.Option("--timeout", help="Seconds to wait for DriveFS mount"),
]
_ModeOpt = Annotated[
    str,
    typer.Option("--mode", help="DriveFS token mode: shim (default) | rt"),
]
_FromEnvOpt = Annotated[
    bool,
    typer.Option("--from-env", help="Read COLAB_DRIVE_* env vars instead of stored credentials"),
]
_CliTokenOpt = Annotated[
    bool,
    typer.Option("--cli-token", help="Use the colab-cli OAuth token instead of dfs credentials"),
]
_DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", help="Print resolved DriveFS config and payload without mounting"),
]
_NoFallbackOpt = Annotated[
    bool,
    typer.Option("--no-fallback", help="Fail instead of falling back to interactive browser flow"),
]
_UpstreamOpt = Annotated[
    bool,
    typer.Option("--upstream", help="Force upstream interactive browser drivemount"),
]
_ADC_ENV_LOCK = threading.Lock()
_EXISTING_SESSION_TOP_COMMANDS = frozenset({
    "status",
    "exec",
    "repl",
    "ls",
    "rm",
    "upload",
    "download",
    "edit",
    "restart-kernel",
    "stop",
    "url",
    "log",
    "auth",
    "drivemount",
    "install",
    "ssh",
})
_EXISTING_SESSION_DFS_COMMANDS = frozenset({"mount", "umount", "status"})
_GLOBAL_VALUE_FLAGS = frozenset({
    "-c",
    "--client-oauth-config",
    "--config",
    "--auth",
    "-a",
    "--account",
})


# --------------------------------------------------------------------------------------
# Shared OAuth helpers (reused by `colab account login/add`)
# --------------------------------------------------------------------------------------


def load_colab_client_config(config_path: Optional[str] = None) -> dict[str, Any]:
    """Load the OAuth2 installed-app client config using colab_cli's search order."""
    from importlib import resources

    acct = accounts.current_account()
    candidates = []
    if config_path:
        candidates.append(os.path.expanduser(config_path))
    acct_cfg = accounts.oauth_config_path(acct)
    if os.path.exists(acct_cfg):
        candidates.append(acct_cfg)
    candidates.append(os.path.expanduser("~/.colab-cli-oauth-config.json"))

    for path in candidates:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    try:
        ref = resources.files("colab_cli").joinpath("oauth_config.json")
        if ref.is_file():
            return json.loads(ref.read_text(encoding="utf-8"))
    except Exception:
        pass

    raise FileNotFoundError(
        f"Client OAuth config not found at {config_path or candidates[-1]} and no inlined config available."
    )


def fetch_google_userinfo(access_token: str) -> dict[str, Any]:
    """Query Google's OIDC userinfo endpoint using a Bearer access token."""
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": "Bearer " + access_token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def record_authorized_identity(account: str, access_token: Optional[str]) -> Optional[dict[str, Any]]:
    """Best-effort fetch of userinfo + cross-account duplicate/mismatch warnings."""
    if not access_token:
        return None
    try:
        info = fetch_google_userinfo(access_token)
    except Exception:
        return None

    email = info.get("email")
    sub = info.get("sub")
    reg = accounts.load_registry()
    meta = reg.get("accounts", {}).get(account, {})
    expected_email = meta.get("email")

    if email and expected_email and email.strip().lower() != expected_email.strip().lower():
        print(
            f"[colab] Warning: account {account!r} expected {expected_email}, "
            f"but Google authorized {email}.",
            file=sys.stderr,
        )

    if email:
        dups = [
            k for k, m in reg.get("accounts", {}).items()
            if k != account
            and isinstance(m, dict)
            and (m.get("email") or "").strip().lower() == email.strip().lower()
        ]
        if dups:
            print(
                f"[colab] Warning: {email} is already linked to account(s): {', '.join(dups)}.",
                file=sys.stderr,
            )

    updates: dict[str, Any] = {}
    if email:
        updates["email"] = email
    if sub:
        updates["user_id"] = sub
    if updates:
        accounts.update_account_meta(account, **updates)
    return info


# --------------------------------------------------------------------------------------
# Auth & State hooks
# --------------------------------------------------------------------------------------


def _patch_auth(force: bool = False) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from colab_cli import auth

    if getattr(auth, "_colab_dfs_auth_patched", False) and not force:
        return

    if not hasattr(auth, "_colab_dfs_orig_get_token"):
        auth._colab_dfs_orig_get_token = auth._get_token_from_keyring
        auth._colab_dfs_orig_save_token = auth._save_token_to_keyring
        auth._colab_dfs_orig_remove_legacy = auth._remove_legacy_token
        auth._colab_dfs_orig_run_remote_flow = auth._run_remote_flow
        auth._colab_dfs_orig_get_adc = auth._get_adc_credentials

    # Prevent legacy ~/.config/colab-cli/token.json from leaking into non-default accounts.
    auth.LEGACY_TOKEN_PATH = _DISABLED_LEGACY_TOKEN
    auth._remove_legacy_token = lambda: None

    def _get_token_hook() -> Optional[str]:
        acct = accounts.current_account()
        tok, _ = accounts.get_colab_oauth_token(acct)
        return tok

    def _save_token_hook(token_json: str) -> None:
        acct = accounts.current_account()
        accounts.save_colab_oauth_token(acct, token_json)

    def _run_remote_flow_hook(client_config: dict):
        acct = accounts.current_account()
        if accounts.NON_INTERACTIVE_AUTH.get():
            raise RuntimeError(
                f"Account '{acct}' is not authenticated; run `colab account login {acct}`"
            )

        reg = accounts.load_registry()
        expected_email = (reg.get("accounts", {}).get(acct) or {}).get("email")

        flow = InstalledAppFlow.from_client_config(client_config, auth.PUBLIC_SCOPES)
        flow.redirect_uri = auth.REMOTE_REDIRECT_URI
        auth_kwargs: dict[str, Any] = {"prompt": "consent", "token_usage": "remote"}
        if expected_email:
            auth_kwargs["login_hint"] = expected_email
        auth_url, _ = flow.authorization_url(**auth_kwargs)

        hint_suffix = f" ({expected_email})" if expected_email else ""
        typer.echo(
            f"\n[colab] Authorizing account '{acct}'{hint_suffix}. "
            "Visit this URL in any browser:\n",
            err=True,
        )
        typer.echo("  " + auth_url + "\n", err=True)
        typer.echo("After approving, Google will display an authorization code.", err=True)
        code = input("Enter the authorization code: ").strip()

        flow.fetch_token(code=code)
        creds = flow.credentials
        record_authorized_identity(acct, getattr(creds, "token", None))
        return creds

    def _get_adc_hook():
        acct = accounts.current_account()
        reg = accounts.load_registry()
        adc_file = (reg.get("accounts", {}).get(acct) or {}).get("adc_credentials_file")

        def _invoke():
            try:
                return auth._colab_dfs_orig_get_adc()
            except SystemExit as exc:
                if accounts.NON_INTERACTIVE_AUTH.get():
                    raise RuntimeError(
                        f"Account '{acct}' has no valid ADC credentials"
                    ) from exc
                raise

        if adc_file and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            expanded = os.path.expanduser(adc_file)
            with _ADC_ENV_LOCK:
                old = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = expanded
                try:
                    return _invoke()
                finally:
                    if old is None:
                        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                    else:
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = old
        return _invoke()

    auth._get_token_from_keyring = _get_token_hook
    auth._save_token_to_keyring = _save_token_hook
    auth._run_remote_flow = _run_remote_flow_hook
    auth._get_adc_credentials = _get_adc_hook
    auth._colab_dfs_auth_patched = True


def _patch_state_history(force: bool = False) -> None:
    from colab_cli import common
    from colab_cli.history import HistoryLogger

    if getattr(common.State, "_colab_dfs_history_patched", False) and not force:
        return

    orig_prop = getattr(common.State, "_colab_dfs_orig_history", common.State.history)
    common.State._colab_dfs_orig_history = orig_prop

    def _history_getter(self):
        if self._history is None:
            if self is common.state and not getattr(self, "_colab_dfs_explicit_config", False):
                self._history = HistoryLogger(
                    log_dir=accounts.history_dir(accounts.current_account())
                )
            else:
                return orig_prop.fget(self)
        return self._history

    common.State.history = property(_history_getter)
    common.State._colab_dfs_history_patched = True


# --------------------------------------------------------------------------------------
# CLI argument inspection & global callback wrapping
# --------------------------------------------------------------------------------------


def _pre_sep_tokens(argv: list[str]) -> list[str]:
    out: list[str] = []
    for tok in argv:
        if tok == "--":
            break
        out.append(tok)
    return out


def _subcommand_tokens(argv: list[str], subcommand: Optional[str]) -> list[str]:
    """Return tokens belonging to `subcommand` (after `subcommand`, before `--`)."""
    tokens = _pre_sep_tokens(argv)
    if not subcommand:
        return tokens
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if any(tok.startswith(f + "=") for f in _GLOBAL_VALUE_FLAGS if f.startswith("--")):
            i += 1
            continue
        if tok == "--logtostderr" or tok.startswith("-"):
            i += 1
            continue
        if tok == subcommand:
            return tokens[i + 1 :]
        i += 1
    return tokens


def _extract_subcommand_session(
    argv: list[str], subcommand: Optional[str]
) -> tuple[Optional[str], bool, list[str]]:
    """Inspect CLI tokens up to `--` for `-s/--session` and routing eligibility."""
    sub_toks = _subcommand_tokens(argv, subcommand)
    if not subcommand:
        return None, False, sub_toks

    eligible = False
    if subcommand in _EXISTING_SESSION_TOP_COMMANDS:
        eligible = True
    elif subcommand == "dfs":
        dfs_sub = _first_positional(sub_toks, value_flags={"-a", "--account"})
        eligible = dfs_sub in _EXISTING_SESSION_DFS_COMMANDS

    session_name = _extract_option_value(sub_toks, "-s", "--session")
    return session_name, eligible, sub_toks


def _first_positional(tokens: list[str], *, value_flags: set[str]) -> Optional[str]:
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in value_flags:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return None


def _extract_option_value(tokens: list[str], short_flag: str, long_flag: str) -> Optional[str]:
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in (short_flag, long_flag):
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return None
        if tok.startswith(long_flag + "="):
            return tok.split("=", 1)[1]
        if tok.startswith(short_flag) and len(tok) > len(short_flag) and not tok.startswith("--"):
            return tok[len(short_flag) :]
        i += 1
    return None


def _wrap_group_parse_args(app: typer.Typer, force: bool = False) -> None:
    import colab_cli.cli as cli

    cls = getattr(cli, "AlphabeticalGroup", None) or app.info.cls
    if cls is None or (getattr(cls, "_colab_dfs_parse_wrapped", False) and not force):
        return
    orig_parse = getattr(cls, "_colab_dfs_orig_parse_args", cls.parse_args)
    cls._colab_dfs_orig_parse_args = orig_parse

    @functools.wraps(orig_parse)
    def wrapped_parse(self, ctx, args):
        ctx.meta["_colab_dfs_raw_args"] = list(args)
        return orig_parse(self, ctx, args)

    cls.parse_args = wrapped_parse
    cls._colab_dfs_parse_wrapped = True


def _apply_account_to_global_state(
    acct: str,
    *,
    ctx: typer.Context,
    explicit_config: Optional[str],
    reg: dict[str, Any],
) -> None:
    from click.core import ParameterSource
    from colab_cli.auth import AuthProvider
    from colab_cli.common import state

    sessions_file = accounts.sessions_path(acct)
    is_own_sessions = (
        explicit_config is not None
        and os.path.abspath(os.path.expanduser(explicit_config)) == os.path.abspath(sessions_file)
    )

    if explicit_config is None:
        state.config_path = sessions_file
        state._store = None
        state._colab_dfs_explicit_config = False
    else:
        state._colab_dfs_explicit_config = not is_own_sessions

    state._history = None
    state._client = None
    state._sessions = None

    if ctx.get_parameter_source("client_oauth_config") == ParameterSource.DEFAULT:
        acct_oauth = accounts.oauth_config_path(acct)
        if os.path.exists(acct_oauth):
            state.client_oauth_config = acct_oauth

    if ctx.get_parameter_source("auth") == ParameterSource.DEFAULT:
        meta = reg.get("accounts", {}).get(acct, {})
        if (meta.get("auth_provider") or "").lower() == "adc":
            state.auth_provider = AuthProvider.ADC


def _wrap_global_callback(app: typer.Typer, force: bool = False) -> None:
    reg_cb = getattr(app, "registered_callback", None)
    if reg_cb is None or (getattr(reg_cb.callback, "_colab_dfs_wrapped", False) and not force):
        return

    orig_cb = getattr(reg_cb.callback, "_colab_dfs_orig", reg_cb.callback)
    sig = inspect.signature(orig_cb, eval_str=True)
    account_param = inspect.Parameter(
        "account",
        kind=inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=_AccountOpt,
    )
    new_params = list(sig.parameters.values()) + [account_param]

    def wrapped_cb(*args, account: _AccountOpt = None, **kwargs):
        from colab_cli import auto_update

        ctx: Optional[typer.Context] = kwargs.get("ctx") or (args[0] if args else None)
        raw_args = (
            ctx.meta.get("_colab_dfs_raw_args")
            if ctx is not None and isinstance(getattr(ctx, "meta", None), dict)
            else None
        )
        if raw_args is None:
            raw_args = sys.argv[1:]

        subcommand = getattr(ctx, "invoked_subcommand", None) if ctx is not None else None
        reg = accounts.ensure_bootstrapped()

        session_name, allow_routing, sub_toks = _extract_subcommand_session(
            list(raw_args), subcommand
        )
        if not account and subcommand in ("dfs", "drivemount"):
            account = _extract_option_value(sub_toks, "-a", "--account")

        allow_create = False
        if subcommand in ("account", "acct"):
            acct_sub = _first_positional(sub_toks, value_flags={"-a", "--account"})
            allow_create = acct_sub in ("add", "login")
        elif subcommand == "dfs":
            dfs_sub = _first_positional(sub_toks, value_flags={"-a", "--account"})
            allow_create = dfs_sub == "consent"

        try:
            acct, source = accounts.resolve_account(
                account,
                session_name=session_name,
                allow_session_routing=allow_routing,
                allow_create=allow_create,
            )
        except accounts.AccountError as exc:
            typer.echo(f"[colab] {exc}", err=True)
            raise typer.Exit(code=1)

        prev_env = os.environ.get("COLAB_ACCOUNT")
        tok_acct = accounts.ACTIVE_ACCOUNT.set(acct)
        tok_src = accounts.ACTIVE_SOURCE.set(source)
        os.environ["COLAB_ACCOUNT"] = acct
        if ctx is not None and hasattr(ctx, "call_on_close"):
            def _restore_ctx() -> None:
                accounts.ACTIVE_ACCOUNT.reset(tok_acct)
                accounts.ACTIVE_SOURCE.reset(tok_src)
                if prev_env is None:
                    os.environ.pop("COLAB_ACCOUNT", None)
                else:
                    os.environ["COLAB_ACCOUNT"] = prev_env

            ctx.call_on_close(_restore_ctx)

        reg = accounts.load_registry()
        pre_sep = _pre_sep_tokens(list(raw_args))
        suppress_update = (
            subcommand in ("account", "acct", "dfs")
            or "--json" in pre_sep
        )

        orig_check = auto_update.run_background_check
        if suppress_update:
            auto_update.run_background_check = lambda: None
        try:
            res = orig_cb(*args, **kwargs)
        finally:
            if suppress_update:
                auto_update.run_background_check = orig_check

        if ctx is not None:
            _apply_account_to_global_state(
                acct,
                ctx=ctx,
                explicit_config=kwargs.get("config"),
                reg=reg,
            )

        if source.startswith("session:") and not (
            subcommand == "ssh" and not sys.stdout.isatty()
        ):
            print(
                f"[colab] Routing session {session_name!r} -> account {acct!r}.",
                file=sys.stderr,
            )
        return res

    wrapped_cb.__signature__ = sig.replace(parameters=new_params)
    ann = dict(inspect.get_annotations(orig_cb, eval_str=True))
    ann["account"] = _AccountOpt
    wrapped_cb.__annotations__ = ann
    wrapped_cb.__doc__ = orig_cb.__doc__
    wrapped_cb._colab_dfs_orig = orig_cb
    wrapped_cb._colab_dfs_wrapped = True
    reg_cb.callback = wrapped_cb


# --------------------------------------------------------------------------------------
# Parallel multi-account probes (`sessions --all`, `usage --all`, `account status`)
# --------------------------------------------------------------------------------------


def _probe_single_account(
    acct: str,
    meta: dict[str, Any],
    *,
    active_name: str,
    include_usage: bool,
    include_sessions: bool,
) -> dict[str, Any]:
    from colab_cli.auth import AuthProvider
    from colab_cli.common import State, _apply_proxy_info
    from colab_cli.history import HistoryLogger

    entry: dict[str, Any] = {
        "account": acct,
        "active": acct == active_name,
        "email": meta.get("email"),
        "auth_provider": (meta.get("auth_provider") or "oauth2").lower(),
        "drive_account": accounts.resolve_drive_account(acct, for_consent=False)[0],
        "drive_ready": False,
        "usage": None,
        "assignments": [],
        "error": None,
    }
    entry["drive_ready"] = os.path.exists(accounts.dfs_store_path(entry["drive_account"]))

    with accounts.account_context(acct, source="all", non_interactive=True):
        st = State()
        st.config_path = accounts.sessions_path(acct)
        st._history = HistoryLogger(log_dir=accounts.history_dir(acct))
        if entry["auth_provider"] == "adc":
            st.auth_provider = AuthProvider.ADC
        acct_oauth = accounts.oauth_config_path(acct)
        if os.path.exists(acct_oauth):
            st.client_oauth_config = acct_oauth

        try:
            if include_usage:
                info = st.client.get_consumption_user_info()
                entry["usage"] = {
                    "balance": info.paid_compute_units_balance,
                    "rate_hourly": info.consumption_rate_hourly,
                    "assignments_count": info.assignments_count,
                }

            if include_sessions:
                local_sessions = st.store.list()
                assignments = st.client.list_assignments()
                by_endpoint = {a.endpoint: a for a in assignments}

                if local_sessions:
                    for s_name, s in list(local_sessions.items()):
                        if s.endpoint not in by_endpoint:
                            st.prune_session(s_name)
                        else:
                            _apply_proxy_info(s, by_endpoint[s.endpoint].runtime_proxy_info)
                            st.store.add(s)

                refreshed = st.store.list()
                name_by_endpoint = {s.endpoint: s.name for s in refreshed.values()}
                for a in assignments:
                    s_name = name_by_endpoint.get(a.endpoint, "?")
                    accel = a.accelerator.value
                    entry["assignments"].append({
                        "name": s_name,
                        "endpoint": a.endpoint,
                        "hardware": "CPU" if accel == "NONE" else accel,
                        "accelerator": accel,
                        "shape": "High-RAM" if a.machine_shape.name == "HIGH_RAM" else "Standard",
                        "machine_shape": a.machine_shape.name,
                        "variant": a.variant.name,
                    })
        except (Exception, SystemExit) as exc:
            entry["error"] = str(exc) or exc.__class__.__name__

    return entry


def probe_all_accounts(
    *,
    include_usage: bool = True,
    include_sessions: bool = True,
) -> list[dict[str, Any]]:
    """Query usage and/or live assignments across all configured accounts in parallel."""
    reg = accounts.load_registry()
    accts = reg.get("accounts", {})
    active_name = reg.get("active") or accounts.DEFAULT_ACCOUNT
    items = list(accts.items())
    if not items:
        return []

    results: list[Optional[dict[str, Any]]] = [None] * len(items)
    workers = min(8, max(1, len(items)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        fut_to_idx = {
            pool.submit(
                _probe_single_account,
                acct,
                meta if isinstance(meta, dict) else {},
                active_name=active_name,
                include_usage=include_usage,
                include_sessions=include_sessions,
            ): idx
            for idx, (acct, meta) in enumerate(items)
        }
        for fut in concurrent.futures.as_completed(fut_to_idx):
            idx = fut_to_idx[fut]
            acct = items[idx][0]
            try:
                results[idx] = fut.result()
            except Exception as exc:  # pragma: no cover
                results[idx] = {
                    "account": acct,
                    "active": acct == active_name,
                    "email": None,
                    "usage": None,
                    "assignments": [],
                    "error": str(exc),
                }

    return [r for r in results if r is not None]


# --------------------------------------------------------------------------------------
# Built-in command wrappers (`skill`, `sessions`, `usage`, `whoami`, `url`)
# --------------------------------------------------------------------------------------


def _wrap_skill(app: typer.Typer, force: bool = False) -> None:
    for reg in list(getattr(app, "registered_commands", []) or []):
        name = getattr(reg, "name", None)
        if name not in ("skill", "SKILL"):
            continue
        cb = getattr(reg, "callback", None)
        if cb is None or (getattr(cb, "_colab_dfs_wrapped", False) and not force):
            continue
        original = getattr(cb, "_colab_dfs_orig", cb)

        def _make_wrapper(orig):
            @functools.wraps(orig)
            def wrapper(*args, **kwargs):
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        rc = orig(*args, **kwargs)
                    base = buf.getvalue()
                    augmented = skill_doc.augment_skill_markdown(base)
                    if not augmented.endswith("\n"):
                        augmented += "\n"
                    sys.stdout.write(augmented)
                    sys.stdout.flush()
                    return rc
                except typer.Exit:
                    raise
                except Exception:
                    raw = buf.getvalue()
                    if raw:
                        sys.stdout.write(raw)
                        sys.stdout.flush()
                        return None
                    return orig(*args, **kwargs)

            wrapper._colab_dfs_orig = orig
            wrapper._colab_dfs_wrapped = True
            return wrapper

        reg.callback = _make_wrapper(original)


def _wrap_sessions(app: typer.Typer, force: bool = False) -> None:
    for reg in list(getattr(app, "registered_commands", []) or []):
        if getattr(reg, "name", None) != "sessions":
            continue
        cb = getattr(reg, "callback", None)
        if cb is None or (getattr(cb, "_colab_dfs_wrapped", False) and not force):
            continue
        original = getattr(cb, "_colab_dfs_orig", cb)

        def wrapped_sessions(all_accounts: _SessionsAllOpt = False, json_out: _JsonOpt = False):
            if not all_accounts and not json_out:
                return original()

            if not all_accounts and json_out:
                acct = accounts.current_account()
                reg_data = accounts.load_registry()
                meta = reg_data.get("accounts", {}).get(acct, {})
                entry = _probe_single_account(
                    acct,
                    meta if isinstance(meta, dict) else {},
                    active_name=reg_data.get("active") or acct,
                    include_usage=False,
                    include_sessions=True,
                )
                typer.echo(json.dumps(entry, indent=2))
                return

            results = probe_all_accounts(include_usage=False, include_sessions=True)
            if json_out:
                typer.echo(json.dumps(results, indent=2))
                return

            any_printed = False
            for r in results:
                acct = r["account"]
                if r.get("error"):
                    typer.echo(f"[{acct}] ERROR: {r['error']}", err=True)
                    continue
                for a in r.get("assignments") or []:
                    any_printed = True
                    typer.echo(
                        f"[{acct}/{a['name']}] {a['endpoint']} | "
                        f"Hardware: {a['hardware']} | Shape: {a['shape']} | Variant: {a['variant']}"
                    )
            if not any_printed:
                typer.echo("[colab] No active sessions found across any account.")

        wrapped_sessions.__name__ = getattr(original, "__name__", "sessions")
        wrapped_sessions.__doc__ = getattr(original, "__doc__", None) or "List all active sessions"
        wrapped_sessions._colab_dfs_orig = original
        wrapped_sessions._colab_dfs_wrapped = True
        reg.callback = wrapped_sessions
        return


def _wrap_usage(app: typer.Typer, force: bool = False) -> None:
    for reg in list(getattr(app, "registered_commands", []) or []):
        raw_cb = getattr(reg, "callback", None)
        name = getattr(reg, "name", None) or getattr(raw_cb, "__name__", "")
        if name != "usage" or raw_cb is None or (getattr(raw_cb, "_colab_dfs_wrapped", False) and not force):
            continue
        cb = getattr(raw_cb, "_colab_dfs_orig", raw_cb)

        def wrapped_usage(all_accounts: _UsageAllOpt = False, json_out: _JsonOpt = False):
            if not all_accounts and not json_out:
                return cb()

            if not all_accounts and json_out:
                acct = accounts.current_account()
                reg_data = accounts.load_registry()
                meta = reg_data.get("accounts", {}).get(acct, {})
                entry = _probe_single_account(
                    acct,
                    meta if isinstance(meta, dict) else {},
                    active_name=reg_data.get("active") or acct,
                    include_usage=True,
                    include_sessions=False,
                )
                typer.echo(json.dumps(entry, indent=2))
                return

            results = probe_all_accounts(include_usage=True, include_sessions=False)
            if json_out:
                typer.echo(json.dumps(results, indent=2))
                return

            for r in results:
                mark = "*" if r.get("active") else " "
                email = r.get("email") or "-"
                if r.get("error"):
                    typer.echo(f"[{mark}{r['account']}] ({email}) ERROR: {r['error']}")
                else:
                    u = r.get("usage") or {}
                    typer.echo(
                        f"[{mark}{r['account']}] ({email}) | "
                        f"Balance: {u.get('balance', 0.0):.2f} CCU | "
                        f"Rate: {u.get('rate_hourly', 0.0):.2f}/hr | "
                        f"Active: {u.get('assignments_count', 0)}"
                    )

        wrapped_usage.__name__ = getattr(cb, "__name__", name)
        wrapped_usage.__doc__ = getattr(cb, "__doc__", None) or "Show account compute-unit usage"
        wrapped_usage._colab_dfs_orig = cb
        wrapped_usage._colab_dfs_wrapped = True
        reg.name = reg.name or name
        reg.callback = wrapped_usage
        return


def _wrap_whoami(app: typer.Typer, force: bool = False) -> None:
    for reg in list(getattr(app, "registered_commands", []) or []):
        if getattr(reg, "name", None) != "whoami":
            continue
        cb = getattr(reg, "callback", None)
        if cb is None or (getattr(cb, "_colab_dfs_wrapped", False) and not force):
            continue
        original = getattr(cb, "_colab_dfs_orig", cb)

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            acct = accounts.current_account()
            source = accounts.current_account_source()
            auth_info = accounts.inspect_colab_auth_nonblocking(acct)
            storage = auth_info.get("storage", "unknown")

            buf = io.StringIO()
            typer.echo(f"Account:       {acct} (source: {source}, storage: {storage})")
            try:
                with contextlib.redirect_stdout(buf):
                    return original(*args, **kwargs)
            finally:
                out = buf.getvalue()
                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()
                    m = re.search(r"^Email:\s*(\S+)", out, re.MULTILINE)
                    if m:
                        found_email = m.group(1).strip()
                        if found_email and not found_email.startswith("<"):
                            reg_data = accounts.load_registry()
                            meta = reg_data.get("accounts", {}).get(acct, {})
                            if not meta.get("email"):
                                with contextlib.suppress(Exception):
                                    accounts.update_account_meta(acct, email=found_email)

        wrapper._colab_dfs_orig = original
        wrapper._colab_dfs_wrapped = True
        reg.callback = wrapper
        return


def _inject_authuser_into_url(raw_url: str, email: str) -> str:
    if not raw_url or not email or "authuser=" in raw_url:
        return raw_url
    encoded = urllib.parse.quote(email, safe="")
    if "#" in raw_url:
        before_hash, frag = raw_url.split("#", 1)
        frag_part = "#" + frag
    else:
        before_hash, frag_part = raw_url, ""
    if "?" in before_hash:
        base, qs = before_hash.split("?", 1)
        return f"{base}?authuser={encoded}&{qs}{frag_part}"
    return f"{before_hash}?authuser={encoded}{frag_part}"


def _wrap_url(app: typer.Typer, force: bool = False) -> None:
    for reg in list(getattr(app, "registered_commands", []) or []):
        if getattr(reg, "name", None) != "url":
            continue
        cb = getattr(reg, "callback", None)
        if cb is None or (getattr(cb, "_colab_dfs_wrapped", False) and not force):
            continue
        original = getattr(cb, "_colab_dfs_orig", cb)

        @functools.wraps(original)
        def wrapper(
            session=None,
            host: str = "https://colab.research.google.com",
            open_browser: bool = False,
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                original(session=session, host=host, open_browser=False)

            raw_url = buf.getvalue().strip()
            acct = accounts.current_account()
            reg_data = accounts.load_registry()
            email = (reg_data.get("accounts", {}).get(acct) or {}).get("email")
            final_url = _inject_authuser_into_url(raw_url, email) if email else raw_url

            typer.echo(final_url)
            if open_browser and final_url:
                import webbrowser

                webbrowser.open(final_url)

        wrapper.__signature__ = inspect.signature(original, eval_str=True)
        wrapper.__annotations__ = dict(inspect.get_annotations(original, eval_str=True))
        wrapper._colab_dfs_orig = original
        wrapper._colab_dfs_wrapped = True
        reg.callback = wrapper
        return


def _call_upstream_drivemount(
    orig_cb,
    session: Optional[str],
    path: str,
    colab_acct: str,
):
    from colab_cli.commands import automation

    reg_data = accounts.load_registry()
    email = (reg_data.get("accounts", {}).get(colab_acct) or {}).get("email")
    orig_show = automation.show_drive_auth_url

    @functools.wraps(orig_show)
    def _show_with_acct_hint(uri: str) -> bool:
        who = f"'{colab_acct}' ({email})" if email else f"'{colab_acct}'"
        typer.echo(
            f"[colab] Drive auth is for account {who} — select this Google account in your browser.",
            err=True,
        )
        return orig_show(uri)

    automation.show_drive_auth_url = _show_with_acct_hint
    try:
        return orig_cb(session=session, path=path)
    finally:
        automation.show_drive_auth_url = orig_show


def _wrap_drivemount(app: typer.Typer, force: bool = False) -> None:
    """Enhance `colab drivemount` with zero-browser DriveFS, multi-account & safe fallback."""
    for reg in list(getattr(app, "registered_commands", []) or []):
        raw_cb = getattr(reg, "callback", None)
        name = getattr(reg, "name", None) or getattr(raw_cb, "__name__", "")
        if name != "drivemount" or raw_cb is None or (getattr(raw_cb, "_colab_dfs_wrapped", False) and not force):
            continue
        orig_cb = getattr(raw_cb, "_colab_dfs_orig", raw_cb)

        def wrapped_drivemount(
            session: _DrivemountSessionOpt = None,
            path: _DrivemountPathArg = "/content/drive",
            account: _AccountOpt = None,
            drive_account: _DriveAccountOpt = None,
            readonly: _ReadonlyOpt = False,
            force: _ForceOpt = False,
            purge_state: _PurgeStateOpt = False,
            timeout: _TimeoutOpt = 120,
            mode: _ModeOpt = "shim",
            from_env: _FromEnvOpt = False,
            cli_token: _CliTokenOpt = False,
            dry_run: _DryRunOpt = False,
            no_fallback: _NoFallbackOpt = False,
            upstream: _UpstreamOpt = False,
        ):
            from . import commands, drive_mount

            if " " in path:
                raise typer.BadParameter("mountpoint must not contain a space")

            try:
                colab_acct = commands._activate_dfs_account(
                    account,
                    session=session,
                    allow_routing=True,
                )
                drive_acct, _ = accounts.resolve_drive_account(
                    colab_acct,
                    explicit_drive=drive_account,
                    for_consent=False,
                )
            except accounts.AccountError as exc:
                typer.echo(f"[colab] {exc}", err=True)
                raise typer.Exit(code=1)

            dfs_disabled = upstream or (
                os.environ.get("COLAB_DFS_DEFAULT", "").strip().lower()
                in ("0", "false", "no", "off")
            )
            dfs_only_requested = (
                bool(drive_account)
                or (drive_acct != colab_acct)
                or readonly
                or purge_state
                or from_env
                or cli_token
                or (mode != "shim")
                or dry_run
            )

            if dfs_disabled:
                if dfs_only_requested:
                    typer.echo(
                        "[colab] --upstream / COLAB_DFS_DEFAULT=0 cannot be combined with "
                        "DriveFS-only options (--drive-account, --readonly, --purge-state, "
                        "--from-env, --cli-token, --mode, --dry-run).",
                        err=True,
                    )
                    raise typer.Exit(code=1)
                return _call_upstream_drivemount(orig_cb, session, path, colab_acct)

            if dry_run:
                argv = [
                    "--account", colab_acct,
                    "--path", path,
                    "--mode", mode,
                    "--timeout", str(timeout),
                    "--dry-run",
                ]
                if drive_account:
                    argv += ["--drive-account", drive_account]
                if session:
                    argv += ["-s", session]
                for flag, on in (
                    ("--force", force),
                    ("--readonly", readonly),
                    ("--from-env", from_env),
                    ("--cli-token", cli_token),
                    ("--purge-state", purge_state),
                ):
                    if on:
                        argv.append(flag)
                try:
                    drive_mount.main(argv)
                except SystemExit as exc:
                    if isinstance(exc.code, str):
                        typer.echo(f"[colab-dfs] {exc.code}", err=True)
                        raise typer.Exit(code=1)
                    raise typer.Exit(code=int(exc.code or 0))
                raise typer.Exit(code=0)

            try:
                cfg = drive_mount.make_cfg(
                    session=session,
                    path=path,
                    mode=mode,
                    force=force,
                    readonly=readonly,
                    timeout=timeout,
                    cli_token=cli_token,
                    from_env=from_env,
                    purge_state=purge_state,
                    account=colab_acct,
                    drive_account=drive_account,
                )
            except (SystemExit, Exception) as exc:
                consent_cmd = (
                    "colab dfs consent"
                    if drive_acct == accounts.DEFAULT_ACCOUNT
                    else f"colab -a {drive_acct} dfs consent"
                )
                if no_fallback or accounts.NON_INTERACTIVE_AUTH.get() or dfs_only_requested:
                    typer.echo(
                        f"[colab-dfs] Cannot mount via DriveFS ({exc}). Run '{consent_cmd}' first.",
                        err=True,
                    )
                    raise typer.Exit(code=1)
                typer.echo(
                    f"[colab-dfs] No stored DriveFS credentials for '{drive_acct}' "
                    f"(tip: run '{consent_cmd}' once for zero-browser mounts); "
                    "falling back to interactive browser drivemount...",
                    err=True,
                )
                return _call_upstream_drivemount(orig_cb, session, path, colab_acct)

            rc = commands._run_mount(cfg, session, verbose=True)
            if rc:
                raise typer.Exit(code=rc)

        wrapped_drivemount.__name__ = getattr(orig_cb, "__name__", "drivemount")
        wrapped_drivemount.__doc__ = (
            "Mount Google Drive at path (zero-browser via DriveFS when credentials exist; "
            "falls back to interactive browser flow)."
        )
        wrapped_drivemount._colab_dfs_orig = orig_cb
        wrapped_drivemount._colab_dfs_wrapped = True
        reg.name = reg.name or "drivemount"
        reg.callback = wrapped_drivemount
        return


def install_hooks(app: typer.Typer, force: bool = False) -> None:
    """Install all multi-account and skill hooks onto `colab_cli`."""
    if getattr(app, "_colab_dfs_hooks_installed", False) and not force:
        return
    _patch_auth(force=force)
    _patch_state_history(force=force)
    _wrap_group_parse_args(app, force=force)
    _wrap_global_callback(app, force=force)
    _wrap_skill(app, force=force)
    _wrap_sessions(app, force=force)
    _wrap_usage(app, force=force)
    _wrap_whoami(app, force=force)
    _wrap_url(app, force=force)
    _wrap_drivemount(app, force=force)
    setattr(app, "_colab_dfs_hooks_installed", True)
