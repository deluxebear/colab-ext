"""Multi-account registry, isolated state paths, and token storage.

Pure Python 3.8+ standard library at module import time. Optional keyring
access is performed lazily inside credential functions so CLI startup and
non-auth commands (`--help`, `account list`, `skill`, `ext doctor`) never
block on locked OS keyrings.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import shutil
import sys
import threading
import time
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover (non-POSIX)
    fcntl = None  # type: ignore[assignment]

ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
KEYRING_SERVICE_NAME = "google-colab-cli"
KEYRING_LEGACY_USERNAME = "oauth_token"
KEYRING_PREFIX = "oauth_token:"
DEFAULT_ACCOUNT = "default"
REGISTRY_VERSION = 1

ACTIVE_ACCOUNT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "colab_dfs_active_account", default=None
)
ACTIVE_SOURCE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "colab_dfs_active_source", default=None
)
NON_INTERACTIVE_AUTH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "colab_dfs_non_interactive_auth", default=False
)


class AccountError(Exception):
    """Raised on invalid account names, unknown profiles, or guarded removals."""


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------


def config_dir() -> str:
    base = os.environ.get("COLAB_CLI_CONFIG_DIR") or "~/.config/colab-cli"
    return os.path.abspath(os.path.expanduser(base))


def registry_path() -> str:
    return os.path.join(config_dir(), "accounts.json")


def accounts_dir() -> str:
    return os.path.join(config_dir(), "accounts")


def account_dir(name: str) -> str:
    validate_account_name(name)
    return os.path.join(accounts_dir(), name)


def sessions_path(name: str) -> str:
    return os.path.join(account_dir(name), "sessions.json")


def history_dir(name: str) -> str:
    return os.path.join(account_dir(name), "history")


def dfs_store_path(name: str) -> str:
    return os.path.join(account_dir(name), "dfs_credentials.json")


def dfs_pending_path(name: str) -> str:
    return os.path.join(account_dir(name), "dfs_pending.json")


def oauth_token_file_path(name: str) -> str:
    return os.path.join(account_dir(name), "oauth_token.json")


def oauth_pending_path(name: str) -> str:
    return os.path.join(account_dir(name), "oauth_pending.json")


def oauth_config_path(name: str) -> str:
    return os.path.join(account_dir(name), "oauth_config.json")


def validate_account_name(name: str) -> str:
    if not name or not isinstance(name, str) or ".." in name or not ACCOUNT_RE.fullmatch(name):
        raise AccountError(
            f"Invalid account name {name!r}: must start with alphanumeric and contain "
            "only letters, digits, '.', '_', '@', '-' (max 64 chars, no '..')."
        )
    return name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------------------
# Atomic private I/O & registry locking
# --------------------------------------------------------------------------------------


def _atomic_write_text(path: str, content: str, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _atomic_write_json(path: str, data: Any, mode: int = 0o600) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n", mode=mode)


def _copy_private_file(src: str, dst: str) -> bool:
    if not os.path.isfile(src) or os.path.exists(dst):
        return False
    with open(src, "r", encoding="utf-8") as f:
        content = f.read()
    _atomic_write_text(dst, content, mode=0o600)
    return True


@contextlib.contextmanager
def _registry_lock() -> Iterator[None]:
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_file = path + ".lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "a+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        raise


def _empty_account_meta(*, migrated_legacy: bool = False) -> dict[str, Any]:
    now = _now_iso()
    return {
        "email": None,
        "user_id": None,
        "dfs_email": None,
        "auth_provider": "oauth2",
        "adc_credentials_file": None,
        "drive_account": None,
        "migrated_legacy": migrated_legacy,
        "created_at": now,
        "updated_at": now,
    }


def _read_registry_unlocked() -> dict[str, Any]:
    path = registry_path()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise AccountError(f"Corrupt registry at {path}: expected JSON object")
    data.setdefault("version", REGISTRY_VERSION)
    accounts = data.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        raise AccountError(f"Corrupt registry at {path}: 'accounts' must be an object")
    if not accounts:
        accounts[DEFAULT_ACCOUNT] = _empty_account_meta(migrated_legacy=False)
        data["active"] = DEFAULT_ACCOUNT
    if data.get("active") not in accounts:
        data["active"] = next(iter(accounts))
    data.setdefault("default_drive_account", None)
    return data


def _save_registry_unlocked(reg: dict[str, Any]) -> None:
    reg["version"] = REGISTRY_VERSION
    _atomic_write_json(registry_path(), reg, mode=0o600)


def _bootstrap_default_unlocked() -> dict[str, Any]:
    root = config_dir()
    os.makedirs(root, exist_ok=True)
    os.makedirs(account_dir(DEFAULT_ACCOUNT), exist_ok=True)
    os.makedirs(history_dir(DEFAULT_ACCOUNT), exist_ok=True)

    meta = _empty_account_meta(migrated_legacy=True)

    _copy_private_file(os.path.join(root, "sessions.json"), sessions_path(DEFAULT_ACCOUNT))
    legacy_dfs = os.path.join(root, "dfs_credentials.json")
    if _copy_private_file(legacy_dfs, dfs_store_path(DEFAULT_ACCOUNT)):
        with contextlib.suppress(Exception):
            with open(legacy_dfs, "r", encoding="utf-8") as f:
                dfs_data = json.load(f)
            if isinstance(dfs_data, dict):
                meta["dfs_email"] = dfs_data.get("email") or None
                meta["email"] = meta["email"] or dfs_data.get("email") or None
                meta["user_id"] = dfs_data.get("user_id") or None
    _copy_private_file(os.path.join(root, "token.json"), oauth_token_file_path(DEFAULT_ACCOUNT))

    legacy_hist = os.path.join(root, "history")
    if os.path.isdir(legacy_hist):
        for fname in os.listdir(legacy_hist):
            if fname.endswith(".jsonl"):
                _copy_private_file(
                    os.path.join(legacy_hist, fname),
                    os.path.join(history_dir(DEFAULT_ACCOUNT), fname),
                )

    reg: dict[str, Any] = {
        "version": REGISTRY_VERSION,
        "active": DEFAULT_ACCOUNT,
        "default_drive_account": None,
        "accounts": {DEFAULT_ACCOUNT: meta},
    }
    _save_registry_unlocked(reg)
    return reg


def ensure_bootstrapped() -> dict[str, Any]:
    """Lazily initialize accounts.json from any existing single-account config."""
    path = registry_path()
    if os.path.exists(path):
        with contextlib.suppress(Exception):
            return _read_registry_unlocked()
    with _registry_lock():
        if os.path.exists(path):
            return _read_registry_unlocked()
        return _bootstrap_default_unlocked()


def load_registry() -> dict[str, Any]:
    return ensure_bootstrapped()


# --------------------------------------------------------------------------------------
# Registry mutations
# --------------------------------------------------------------------------------------


def lookup_account(
    identifier: str,
    reg: Optional[dict[str, Any]] = None,
    *,
    allow_create: bool = False,
) -> str:
    """Resolve an account name or email (case-insensitive) to a profile key."""
    if not identifier or not isinstance(identifier, str):
        raise AccountError("Account identifier cannot be empty.")
    ensure_bootstrapped()
    if reg is None:
        reg = _read_registry_unlocked()
    accounts = reg.get("accounts", {})
    if identifier in accounts:
        return identifier

    needle = identifier.strip().lower()
    email_matches = [
        k for k, meta in accounts.items()
        if isinstance(meta, dict) and (meta.get("email") or "").strip().lower() == needle
    ]
    if len(email_matches) == 1:
        return email_matches[0]
    if len(email_matches) > 1:
        raise AccountError(
            f"Email {identifier!r} matches multiple accounts ({', '.join(email_matches)}); "
            "specify the profile name explicitly."
        )

    if allow_create:
        name = validate_account_name(identifier)
        ensure_account(name)
        return name

    configured = ", ".join(accounts.keys()) or "none"
    raise AccountError(
        f"Account {identifier!r} not found (configured: {configured}). "
        f"Run `colab account add {identifier}` first."
    )


def ensure_account(name: str, **meta: Any) -> dict[str, Any]:
    validate_account_name(name)
    ensure_bootstrapped()
    with _registry_lock():
        reg = _read_registry_unlocked()
        accounts = reg["accounts"]
        entry = accounts.get(name)
        if entry is None:
            entry = _empty_account_meta(migrated_legacy=False)
            accounts[name] = entry
        for k, v in meta.items():
            if v is not None:
                entry[k] = v
        entry["updated_at"] = _now_iso()
        os.makedirs(account_dir(name), exist_ok=True)
        os.makedirs(history_dir(name), exist_ok=True)
        _save_registry_unlocked(reg)
        return reg


def update_account_meta(name: str, **fields: Any) -> dict[str, Any]:
    validate_account_name(name)
    ensure_bootstrapped()
    with _registry_lock():
        reg = _read_registry_unlocked()
        if name not in reg["accounts"]:
            raise AccountError(f"Account {name!r} does not exist.")
        entry = reg["accounts"][name]
        for k, v in fields.items():
            entry[k] = v
        entry["updated_at"] = _now_iso()
        _save_registry_unlocked(reg)
        return reg


def set_active_account(name: str) -> tuple[str, dict[str, Any]]:
    ensure_bootstrapped()
    with _registry_lock():
        reg = _read_registry_unlocked()
        resolved = lookup_account(name, reg, allow_create=False)
        reg["active"] = resolved
        _save_registry_unlocked(reg)
        return resolved, reg


def set_drive_binding(
    drive_account: Optional[str],
    *,
    target_account: Optional[str] = None,
    as_default: bool = False,
    clear: bool = False,
) -> dict[str, Any]:
    ensure_bootstrapped()
    with _registry_lock():
        reg = _read_registry_unlocked()
        resolved_drive: Optional[str] = None
        if not clear:
            if not drive_account:
                raise AccountError("Specify a storage account name or pass --clear.")
            resolved_drive = lookup_account(drive_account, reg, allow_create=False)

        if as_default:
            reg["default_drive_account"] = resolved_drive
        else:
            target = target_account or current_account()
            resolved_target = lookup_account(target, reg, allow_create=False)
            reg["accounts"][resolved_target]["drive_account"] = resolved_drive
            reg["accounts"][resolved_target]["updated_at"] = _now_iso()

        _save_registry_unlocked(reg)
        return reg


def read_local_sessions(name: str) -> dict[str, Any]:
    path = sessions_path(name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------------------
# Keyring + file token backend
# --------------------------------------------------------------------------------------


def _token_storage_mode() -> str:
    mode = (os.environ.get("COLAB_TOKEN_STORAGE") or "auto").strip().lower()
    return mode if mode in ("auto", "keyring", "file") else "auto"


def _run_with_timeout_compat(func, *args, timeout: float = 30.0):
    try:
        from colab_cli import auth as cli_auth

        return cli_auth._run_with_timeout(func, *args, timeout=timeout)
    except ImportError:
        pass

    res = None
    err: Optional[BaseException] = None

    def target() -> None:
        nonlocal res, err
        try:
            res = func(*args)
        except BaseException as exc:
            err = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"Operation timed out after {timeout:.1f}s")
    if err is not None:
        raise err
    return res


def _check_linux_keyring_locked_compat() -> None:
    with contextlib.suppress(ImportError):
        from colab_cli import auth as cli_auth

        cli_auth._check_linux_keyring_locked()


def _keyring_get_raw(username: str, timeout: float = 30.0) -> Optional[str]:
    import keyring

    def _get() -> Optional[str]:
        _check_linux_keyring_locked_compat()
        return keyring.get_password(KEYRING_SERVICE_NAME, username)

    return _run_with_timeout_compat(_get, timeout=timeout)


def _keyring_set_raw(username: str, value: str, timeout: float = 30.0) -> None:
    import keyring

    def _set() -> None:
        _check_linux_keyring_locked_compat()
        keyring.set_password(KEYRING_SERVICE_NAME, username, value)

    _run_with_timeout_compat(_set, timeout=timeout)


def _keyring_delete_raw(username: str, timeout: float = 10.0) -> bool:
    import keyring

    def _delete() -> bool:
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, username)
            return True
        except Exception:
            return False

    return bool(_run_with_timeout_compat(_delete, timeout=timeout))


def _clear_migrated_flag(account: str) -> None:
    with contextlib.suppress(Exception):
        with _registry_lock():
            reg = _read_registry_unlocked()
            meta = reg["accounts"].get(account)
            if meta and meta.get("migrated_legacy"):
                meta["migrated_legacy"] = False
                meta["updated_at"] = _now_iso()
                _save_registry_unlocked(reg)


def get_colab_oauth_token(account: str) -> tuple[Optional[str], str]:
    """Read an account's serialized Colab OAuth token from keyring or file."""
    reg = ensure_bootstrapped()
    meta = reg["accounts"].get(account, {})
    mode = _token_storage_mode()
    slot = f"{KEYRING_PREFIX}{account}"

    if mode != "file":
        try:
            tok = _keyring_get_raw(slot)
            if tok:
                return tok, "keyring"
            if meta.get("migrated_legacy"):
                legacy_tok = _keyring_get_raw(KEYRING_LEGACY_USERNAME)
                if legacy_tok:
                    with contextlib.suppress(Exception):
                        _keyring_set_raw(slot, legacy_tok)
                        _clear_migrated_flag(account)
                    return legacy_tok, "keyring"
        except Exception:
            if mode == "keyring":
                raise

    fpath = oauth_token_file_path(account)
    if os.path.isfile(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = f.read().strip()
            if data:
                return data, "file"
        except OSError:
            pass

    return None, "none"


def save_colab_oauth_token(account: str, token_json: str) -> str:
    """Persist an account's serialized Colab OAuth token to keyring or 0600 file."""
    ensure_account(account)
    mode = _token_storage_mode()
    slot = f"{KEYRING_PREFIX}{account}"
    fpath = oauth_token_file_path(account)

    if mode != "file":
        try:
            _keyring_set_raw(slot, token_json)
            _clear_migrated_flag(account)
            if os.path.exists(fpath):
                with contextlib.suppress(OSError):
                    os.unlink(fpath)
            return "keyring"
        except Exception:
            if mode == "keyring":
                raise

    _atomic_write_text(fpath, token_json, mode=0o600)
    _clear_migrated_flag(account)
    return "file"


def delete_colab_oauth_token(account: str) -> bool:
    """Delete an account's per-profile OAuth token (never deletes legacy 'oauth_token')."""
    removed = False
    slot = f"{KEYRING_PREFIX}{account}"
    if _token_storage_mode() != "file":
        with contextlib.suppress(Exception):
            if _keyring_delete_raw(slot):
                removed = True
    fpath = oauth_token_file_path(account)
    if os.path.exists(fpath):
        with contextlib.suppress(OSError):
            os.unlink(fpath)
            removed = True
    return removed


def _linux_secretservice_locked() -> Optional[bool]:
    """Return True if Linux SecretService is locked, False if unlocked, None if N/A."""
    if sys.platform != "linux":
        return None
    try:
        import keyring
        from keyring.backends import SecretService, chainer
        import secretstorage

        kr = keyring.get_keyring()
        primary = kr.backends[0] if isinstance(kr, chainer.ChainerBackend) and kr.backends else kr
        if not isinstance(primary, SecretService.Keyring):
            return False
        with contextlib.closing(secretstorage.dbus_init()) as bus:
            cpath = getattr(primary, "preferred_collection", None)
            coll = (
                secretstorage.Collection(bus, cpath)
                if cpath
                else secretstorage.Collection(bus)
            )
            return bool(coll.is_locked())
    except Exception:
        return None


def _file_has_token(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(isinstance(data, dict) and (data.get("refresh_token") or data.get("token")))
    except Exception:
        return False


def inspect_colab_auth_nonblocking(account: str) -> dict[str, Any]:
    """Inspect auth readiness without triggering interactive login or keyring unlock."""
    reg = ensure_bootstrapped()
    meta = reg["accounts"].get(account, {})
    provider = (meta.get("auth_provider") or "oauth2").lower()

    if provider == "adc":
        adc_file = meta.get("adc_credentials_file") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        default_adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        if adc_file and os.path.isfile(os.path.expanduser(adc_file)):
            return {"provider": "adc", "ready": True, "storage": "adc-file", "detail": adc_file}
        if not meta.get("adc_credentials_file") and os.path.isfile(default_adc):
            return {"provider": "adc", "ready": True, "storage": "gcloud-adc", "detail": default_adc}
        return {"provider": "adc", "ready": False, "storage": "adc", "detail": "missing ADC file"}

    fpath = oauth_token_file_path(account)
    if _file_has_token(fpath):
        return {"provider": "oauth2", "ready": True, "storage": "file", "detail": fpath}

    mode = _token_storage_mode()
    if mode == "file":
        return {"provider": "oauth2", "ready": False, "storage": "none", "detail": "no token file"}

    locked = _linux_secretservice_locked()
    if locked is True:
        return {
            "provider": "oauth2",
            "ready": None,
            "storage": "keyring (locked)",
            "detail": "SecretService locked",
        }

    try:
        import keyring

        def _probe() -> bool:
            slot = f"{KEYRING_PREFIX}{account}"
            if keyring.get_password(KEYRING_SERVICE_NAME, slot):
                return True
            if meta.get("migrated_legacy") and keyring.get_password(
                KEYRING_SERVICE_NAME, KEYRING_LEGACY_USERNAME
            ):
                return True
            return False

        found = bool(_run_with_timeout_compat(_probe, timeout=1.5))
        return {
            "provider": "oauth2",
            "ready": found,
            "storage": "keyring" if found else "none",
            "detail": f"{KEYRING_PREFIX}{account}" if found else "not logged in",
        }
    except Exception as exc:
        return {
            "provider": "oauth2",
            "ready": None,
            "storage": "keyring (unavailable)",
            "detail": str(exc),
        }


# --------------------------------------------------------------------------------------
# Account rename & remove
# --------------------------------------------------------------------------------------


def rename_account(old: str, new: str) -> dict[str, Any]:
    ensure_bootstrapped()
    new = validate_account_name(new)
    with _registry_lock():
        reg = _read_registry_unlocked()
        old_name = lookup_account(old, reg, allow_create=False)
        if old_name == new:
            return reg
        if new in reg["accounts"]:
            raise AccountError(f"Target account {new!r} already exists.")

        meta = dict(reg["accounts"][old_name])
        old_dir = account_dir(old_name)
        new_dir = account_dir(new)
        if os.path.isdir(old_dir):
            os.makedirs(os.path.dirname(new_dir), exist_ok=True)
            os.replace(old_dir, new_dir)
        else:
            os.makedirs(new_dir, exist_ok=True)
            os.makedirs(history_dir(new), exist_ok=True)

        if _token_storage_mode() != "file":
            with contextlib.suppress(Exception):
                old_slot = f"{KEYRING_PREFIX}{old_name}"
                new_slot = f"{KEYRING_PREFIX}{new}"
                tok = _keyring_get_raw(old_slot, timeout=5.0)
                if not tok and meta.get("migrated_legacy"):
                    tok = _keyring_get_raw(KEYRING_LEGACY_USERNAME, timeout=5.0)
                if tok:
                    _keyring_set_raw(new_slot, tok, timeout=5.0)
                    _keyring_delete_raw(old_slot, timeout=5.0)

        meta["migrated_legacy"] = False
        meta["updated_at"] = _now_iso()

        # Preserve order while replacing old_name with new
        new_accounts: dict[str, Any] = {}
        for k, v in reg["accounts"].items():
            if k == old_name:
                new_accounts[new] = meta
            else:
                if isinstance(v, dict) and v.get("drive_account") == old_name:
                    v["drive_account"] = new
                new_accounts[k] = v
        reg["accounts"] = new_accounts
        if reg.get("active") == old_name:
            reg["active"] = new
        if reg.get("default_drive_account") == old_name:
            reg["default_drive_account"] = new
        _save_registry_unlocked(reg)
        return reg


def remove_account(name: str, *, force: bool = False) -> tuple[str, dict[str, Any]]:
    ensure_bootstrapped()
    with _registry_lock():
        reg = _read_registry_unlocked()
        resolved = lookup_account(name, reg, allow_create=False)
        local_sessions = read_local_sessions(resolved)
        if local_sessions and not force:
            names = ", ".join(sorted(local_sessions.keys()))
            raise AccountError(
                f"Account {resolved!r} still has local session(s): {names}. "
                f"Stop them first (`colab -a {resolved} stop -s <session>`) or pass --force."
            )

        delete_colab_oauth_token(resolved)
        shutil.rmtree(account_dir(resolved), ignore_errors=True)
        del reg["accounts"][resolved]

        if reg.get("default_drive_account") == resolved:
            reg["default_drive_account"] = None
        for meta in reg["accounts"].values():
            if isinstance(meta, dict) and meta.get("drive_account") == resolved:
                meta["drive_account"] = None

        if not reg["accounts"]:
            reg["accounts"][DEFAULT_ACCOUNT] = _empty_account_meta(migrated_legacy=False)
            os.makedirs(account_dir(DEFAULT_ACCOUNT), exist_ok=True)
            os.makedirs(history_dir(DEFAULT_ACCOUNT), exist_ok=True)
            reg["active"] = DEFAULT_ACCOUNT
        elif reg.get("active") == resolved:
            reg["active"] = next(iter(reg["accounts"]))

        _save_registry_unlocked(reg)
        return resolved, reg


# --------------------------------------------------------------------------------------
# ContextVars & 4-tier account resolution
# --------------------------------------------------------------------------------------


def current_account() -> str:
    acct = ACTIVE_ACCOUNT.get()
    if acct:
        return acct
    reg = ensure_bootstrapped()
    return reg["active"]


def current_account_source() -> str:
    return ACTIVE_SOURCE.get() or "active"


@contextlib.contextmanager
def account_context(
    account: str,
    source: str = "context",
    non_interactive: Optional[bool] = None,
) -> Iterator[None]:
    tok_acct = ACTIVE_ACCOUNT.set(account)
    tok_src = ACTIVE_SOURCE.set(source)
    tok_ni = None
    if non_interactive is not None:
        tok_ni = NON_INTERACTIVE_AUTH.set(non_interactive)
    try:
        yield
    finally:
        ACTIVE_ACCOUNT.reset(tok_acct)
        ACTIVE_SOURCE.reset(tok_src)
        if tok_ni is not None:
            NON_INTERACTIVE_AUTH.reset(tok_ni)


def resolve_account(
    explicit: Optional[str] = None,
    *,
    session_name: Optional[str] = None,
    allow_session_routing: bool = False,
    allow_create: bool = False,
) -> tuple[str, str]:
    """4-tier account resolution: flag > COLAB_ACCOUNT > session owner > active."""
    reg = ensure_bootstrapped()
    if explicit:
        return lookup_account(explicit, reg, allow_create=allow_create), "flag"

    env_acct = (os.environ.get("COLAB_ACCOUNT") or "").strip()
    if env_acct:
        return lookup_account(env_acct, reg, allow_create=allow_create), "env"

    active = reg["active"]
    if allow_session_routing and session_name:
        if session_name in read_local_sessions(active):
            return active, "active"
        owners = [
            acct for acct in reg["accounts"]
            if acct != active and session_name in read_local_sessions(acct)
        ]
        if len(owners) == 1:
            return owners[0], f"session:{session_name}"
        if len(owners) > 1:
            raise AccountError(
                f"Session {session_name!r} exists in multiple accounts ({', '.join(owners)}); "
                "specify one with `colab -a <account> ...`."
            )

    return active, "active"


def resolve_drive_account(
    colab_account: Optional[str] = None,
    explicit_drive: Optional[str] = None,
    *,
    for_consent: bool = False,
) -> tuple[str, str]:
    """Resolve which account supplies/stores DriveFS credentials."""
    reg = ensure_bootstrapped()
    acct = colab_account or current_account()

    if explicit_drive:
        return lookup_account(explicit_drive, reg, allow_create=for_consent), "flag"

    env_drv = (
        os.environ.get("COLAB_DRIVE_ACCOUNT")
        or os.environ.get("COLAB_DFS_ACCOUNT")
        or ""
    ).strip()
    if env_drv:
        return lookup_account(env_drv, reg, allow_create=for_consent), "env"

    if for_consent:
        return acct, "self"

    accounts = reg.get("accounts", {})
    bound = (accounts.get(acct) or {}).get("drive_account")
    if bound and bound in accounts:
        return bound, f"bound:{acct}"

    if os.path.exists(dfs_store_path(acct)):
        return acct, "self"

    default_drive = reg.get("default_drive_account")
    if default_drive and default_drive in accounts:
        return default_drive, "default"

    return acct, "self"
