"""Hermetic unit and CLI tests for colab-ext multi-account, DriveFS, and skill support.

All tests isolate filesystem state via COLAB_CLI_CONFIG_DIR in a temporary directory
and isolate credentials via an in-memory KeyringBackend so the user's real
~/.config/colab-cli and system SecretService keyring are never read or modified.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import keyring
import keyring.backend
import keyring.errors
from google.oauth2.credentials import Credentials
from typer.testing import CliRunner

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_reloaded_repo_ext = False
for _mod in [
    k for k in list(sys.modules)
    if k == "colab_dfs_ext" or k.startswith("colab_dfs_ext.")
]:
    del sys.modules[_mod]
    _reloaded_repo_ext = True

_loaded_ext = sys.modules.get("colab_ext")
if _loaded_ext is not None and not os.path.abspath(_loaded_ext.__file__).startswith(REPO_ROOT + os.sep):
    for _mod in [k for k in list(sys.modules) if k == "colab_ext" or k.startswith("colab_ext.")]:
        del sys.modules[_mod]
    _reloaded_repo_ext = True

import colab_cli.cli as colab_cli_mod  # noqa: E402
import colab_ext  # noqa: E402
from colab_ext import accounts, cli_hooks, drive_mount, inject, skill_doc  # noqa: E402

if not os.path.abspath(colab_ext.__file__).startswith(REPO_ROOT + os.sep):
    raise RuntimeError(f"Expected repo colab_ext, got {colab_ext.__file__}")

if _reloaded_repo_ext or getattr(colab_cli_mod.app, colab_ext.REGISTERED_ATTR, False) is not True:
    colab_ext.register(colab_cli_mod.app, force=True)


class MemoryKeyring(keyring.backend.KeyringBackend):
    """In-memory KeyringBackend that never touches OS SecretService."""

    priority = 10

    def __init__(self, *, fail_on_set: bool = False) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.fail_on_set = fail_on_set

    def get_password(self, service: str, username: str):
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_on_set:
            raise keyring.errors.KeyringError("simulated locked keyring")
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        key = (service, username)
        if key not in self.data:
            raise keyring.errors.PasswordDeleteError(f"missing {key}")
        del self.data[key]


def _make_valid_oauth_token_json(token: str = "ya29.fake-access-token") -> str:
    creds = Credentials(
        token=token,
        refresh_token="1//fake-colab-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="fake-client-id.apps.googleusercontent.com",
        client_secret="fake-client-secret",
        scopes=["profile", "email", "https://www.googleapis.com/auth/colaboratory"],
        expiry=datetime.datetime(2099, 1, 1, 0, 0, 0),
    )
    return creds.to_json()


def _make_session_dict(name: str, endpoint: str) -> dict:
    return {
        "name": name,
        "token": f"tok-{name}",
        "url": f"https://{endpoint}.example.invalid",
        "endpoint": endpoint,
        "variant": "DEFAULT",
        "accelerator": "T4",
        "machine_shape": "STANDARD",
    }


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_dfs_creds(
    acct: str,
    *,
    user_id: str = "111222333",
    email: str = "user@example.com",
    refresh_token: str = "1//fake-dfs-rt",
) -> str:
    path = accounts.dfs_store_path(acct)
    _write_json(
        path,
        {
            "flavor": "dfs",
            "client_id": "fake-dfs-client",
            "client_secret": "fake-dfs-secret",
            "refresh_token": refresh_token,
            "scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/drive",
            ],
            "token_uri": "https://oauth2.googleapis.com/token",
            "user_id": user_id,
            "email": email,
        },
    )
    return path


class BaseIsolatedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="colab-ext-test-")
        self.orig_keyring = keyring.get_keyring()
        self.mem_kr = MemoryKeyring()
        keyring.set_keyring(self.mem_kr)

        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "COLAB_CLI_CONFIG_DIR",
                "COLAB_ACCOUNT",
                "COLAB_DRIVE_ACCOUNT",
                "COLAB_DFS_ACCOUNT",
                "COLAB_DRIVE_DEFAULT",
                "COLAB_DFS_DEFAULT",
                "COLAB_TOKEN_STORAGE",
            )
        }
        os.environ["COLAB_CLI_CONFIG_DIR"] = self.tmpdir
        for k in self._env_backup:
            if k != "COLAB_CLI_CONFIG_DIR":
                os.environ.pop(k, None)

        self._tok_acct = accounts.ACTIVE_ACCOUNT.set(None)
        self._tok_src = accounts.ACTIVE_SOURCE.set("active")
        self._tok_ni = accounts.NON_INTERACTIVE_AUTH.set(False)

        self._reset_global_state()
        self.runner = CliRunner()

    def tearDown(self) -> None:
        accounts.ACTIVE_ACCOUNT.reset(self._tok_acct)
        accounts.ACTIVE_SOURCE.reset(self._tok_src)
        accounts.NON_INTERACTIVE_AUTH.reset(self._tok_ni)

        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        keyring.set_keyring(self.orig_keyring)
        self._reset_global_state()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _reset_global_state() -> None:
        from colab_cli.auth import AuthProvider
        from colab_cli.common import state

        state.config_path = None
        state._store = None
        state._settings_store = None
        state._history = None
        state._client = None
        state._sessions = None
        state.auth_provider = AuthProvider.OAUTH2
        state._colab_ext_explicit_config = False
        state._colab_dfs_explicit_config = False


class TestMultiAccountCore(BaseIsolatedTest):
    def test_bootstrap_and_lazy_legacy_migration_non_destructive(self) -> None:
        legacy_sessions = {"legacy-gpu": _make_session_dict("legacy-gpu", "ep-legacy")}
        legacy_sessions_path = os.path.join(self.tmpdir, "sessions.json")
        _write_json(legacy_sessions_path, legacy_sessions)

        legacy_dfs_path = os.path.join(self.tmpdir, "dfs_credentials.json")
        _write_json(
            legacy_dfs_path,
            {
                "flavor": "dfs",
                "refresh_token": "1//legacy-dfs",
                "user_id": "987654321",
                "email": "legacy@example.com",
                "scopes": ["https://www.googleapis.com/auth/drive"],
            },
        )
        legacy_hist_file = os.path.join(self.tmpdir, "history", "sess.jsonl")
        os.makedirs(os.path.dirname(legacy_hist_file), exist_ok=True)
        with open(legacy_hist_file, "w", encoding="utf-8") as f:
            f.write('{"cmd":"echo hi"}\n')

        legacy_tok = _make_valid_oauth_token_json("ya29.legacy")
        self.mem_kr.set_password(accounts.KEYRING_SERVICE_NAME, accounts.KEYRING_LEGACY_USERNAME, legacy_tok)

        reg = accounts.ensure_bootstrapped()
        self.assertEqual(reg["active"], "default")
        self.assertTrue(reg["accounts"]["default"]["migrated_legacy"])
        self.assertEqual(reg["accounts"]["default"]["email"], "legacy@example.com")
        self.assertEqual(reg["accounts"]["default"]["user_id"], "987654321")

        # Private permissions on registry and adopted files
        self.assertEqual(stat.S_IMODE(os.stat(accounts.registry_path()).st_mode), 0o600)
        self.assertTrue(os.path.exists(accounts.sessions_path("default")))
        self.assertTrue(os.path.exists(accounts.dfs_store_path("default")))
        self.assertEqual(stat.S_IMODE(os.stat(accounts.dfs_store_path("default")).st_mode), 0o600)
        self.assertTrue(os.path.exists(os.path.join(accounts.history_dir("default"), "sess.jsonl")))

        # Original legacy files remain completely intact
        self.assertTrue(os.path.exists(legacy_sessions_path))
        self.assertTrue(os.path.exists(legacy_dfs_path))

        # Lazy keyring migration copies legacy slot -> oauth_token:default and leaves legacy slot intact
        tok, backend = accounts.get_colab_oauth_token("default")
        self.assertEqual(tok, legacy_tok)
        self.assertEqual(backend, "keyring")
        self.assertEqual(
            self.mem_kr.get_password(accounts.KEYRING_SERVICE_NAME, "oauth_token:default"),
            legacy_tok,
        )
        self.assertEqual(
            self.mem_kr.get_password(accounts.KEYRING_SERVICE_NAME, accounts.KEYRING_LEGACY_USERNAME),
            legacy_tok,
        )
        reg_after = accounts.load_registry()
        self.assertFalse(reg_after["accounts"]["default"]["migrated_legacy"])

    def test_four_tier_resolution_email_lookup_and_routing_rules(self) -> None:
        accounts.ensure_bootstrapped()
        accounts.ensure_account("work", email="work.user@example.com")
        accounts.ensure_account("alt", email="alt.user@example.com")

        _write_json(accounts.sessions_path("work"), {"work-gpu": _make_session_dict("work-gpu", "ep-w")})
        _write_json(accounts.sessions_path("alt"), {"alt-gpu": _make_session_dict("alt-gpu", "ep-a")})

        # Tier 4: active default
        self.assertEqual(accounts.resolve_account(allow_session_routing=True, session_name="missing"), ("default", "active"))

        # Tier 3: unique existing session owner routes automatically
        self.assertEqual(
            accounts.resolve_account(None, session_name="work-gpu", allow_session_routing=True),
            ("work", "session:work-gpu"),
        )
        # Disallowed for creation commands (`new`, `run`)
        self.assertEqual(
            accounts.resolve_account(None, session_name="work-gpu", allow_session_routing=False),
            ("default", "active"),
        )
        _, new_allowed, _ = cli_hooks._extract_subcommand_session(["new", "-g", "T4", "-s", "work-gpu"], "new")
        _, run_allowed, _ = cli_hooks._extract_subcommand_session(["run", "-s", "work-gpu", "script.py"], "run")
        _, exec_allowed, _ = cli_hooks._extract_subcommand_session(["exec", "-s", "work-gpu", "--", "ls"], "exec")
        self.assertFalse(new_allowed)
        self.assertFalse(run_allowed)
        self.assertTrue(exec_allowed)

        # Tier 2: COLAB_ACCOUNT overrides session routing
        os.environ["COLAB_ACCOUNT"] = "alt"
        self.assertEqual(
            accounts.resolve_account(None, session_name="work-gpu", allow_session_routing=True),
            ("alt", "env"),
        )
        # Tier 1: explicit flag (including email lookup) overrides COLAB_ACCOUNT
        self.assertEqual(
            accounts.resolve_account("work.user@example.com", session_name="alt-gpu", allow_session_routing=True),
            ("work", "flag"),
        )
        os.environ.pop("COLAB_ACCOUNT", None)

        # Unknown account raises AccountError
        with self.assertRaises(accounts.AccountError):
            accounts.resolve_account("nonexistent")

        # Duplicate session across non-active accounts refuses ambiguous routing
        _write_json(
            accounts.sessions_path("work"),
            {
                "work-gpu": _make_session_dict("work-gpu", "ep-w"),
                "dup-gpu": _make_session_dict("dup-gpu", "ep-dup1"),
            },
        )
        _write_json(
            accounts.sessions_path("alt"),
            {
                "alt-gpu": _make_session_dict("alt-gpu", "ep-a"),
                "dup-gpu": _make_session_dict("dup-gpu", "ep-dup2"),
            },
        )
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.resolve_account(None, session_name="dup-gpu", allow_session_routing=True)
        self.assertIn("multiple accounts", str(ctx.exception))

        # Both `colab dfs show -a work` and `colab -a work dfs show` work via CliRunner
        _write_dfs_creds("work", user_id="555", email="work.user@example.com")
        r1 = self.runner.invoke(colab_cli_mod.app, ["dfs", "show", "-a", "work"])
        r2 = self.runner.invoke(colab_cli_mod.app, ["-a", "work", "dfs", "show"])
        self.assertEqual(r1.exit_code, 0, r1.output)
        self.assertEqual(r2.exit_code, 0, r2.output)
        self.assertIn("work.user@example.com", r1.stdout)
        self.assertIn("work.user@example.com", r2.stdout)

    def test_session_sync_isolation_across_accounts(self) -> None:
        accounts.ensure_bootstrapped()
        accounts.ensure_account("work", email="work@example.com")

        accounts.save_colab_oauth_token("default", _make_valid_oauth_token_json("ya29.default"))
        accounts.save_colab_oauth_token("work", _make_valid_oauth_token_json("ya29.work"))

        _write_json(accounts.sessions_path("default"), {"s-def": _make_session_dict("s-def", "ep-def")})
        _write_json(accounts.sessions_path("work"), {"s-work": _make_session_dict("s-work", "ep-work")})

        class FakeClient:
            def __init__(self, env, creds) -> None:
                pass

            def list_assignments(self):
                # Default no longer has ep-def running on server -> local s-def gets pruned.
                return []

        with mock.patch("colab_cli.common.Client", FakeClient):
            res = self.runner.invoke(colab_cli_mod.app, ["-a", "default", "sessions", "--json"])
            self.assertEqual(res.exit_code, 0, res.output)

        self.assertNotIn("s-def", accounts.read_local_sessions("default"))
        self.assertIn("s-work", accounts.read_local_sessions("work"))

    def test_keyring_failure_falls_back_to_0600_file(self) -> None:
        accounts.ensure_bootstrapped()
        accounts.ensure_account("work")
        failing_kr = MemoryKeyring(fail_on_set=True)
        keyring.set_keyring(failing_kr)

        payload = _make_valid_oauth_token_json("ya29.fallback")
        storage = accounts.save_colab_oauth_token("work", payload)
        self.assertEqual(storage, "file")

        tok_file = accounts.oauth_token_file_path("work")
        self.assertTrue(os.path.exists(tok_file))
        self.assertEqual(stat.S_IMODE(os.stat(tok_file).st_mode), 0o600)

        read_back, backend = accounts.get_colab_oauth_token("work")
        self.assertEqual(read_back, payload)
        self.assertEqual(backend, "file")


class TestMultiAccountCLI(BaseIsolatedTest):
    def test_account_crud_and_headless_url_only_code_login(self) -> None:
        accounts.ensure_bootstrapped()
        accounts.ensure_account("work", email="work@example.com")
        _write_json(accounts.sessions_path("work"), {"work-gpu": _make_session_dict("work-gpu", "ep-w")})

        # list --json
        res_list = self.runner.invoke(colab_cli_mod.app, ["account", "list", "--json"])
        self.assertEqual(res_list.exit_code, 0, res_list.output)
        data = json.loads(res_list.stdout)
        self.assertEqual(data["active"], "default")
        self.assertEqual({a["account"] for a in data["accounts"]}, {"default", "work"})

        # current
        res_cur = self.runner.invoke(colab_cli_mod.app, ["account", "current"])
        self.assertEqual(res_cur.exit_code, 0)
        self.assertEqual(res_cur.stdout.strip(), "default")

        # use + rename
        res_use = self.runner.invoke(colab_cli_mod.app, ["account", "use", "work"])
        self.assertEqual(res_use.exit_code, 0, res_use.output)
        self.assertEqual(accounts.load_registry()["active"], "work")

        res_ren = self.runner.invoke(colab_cli_mod.app, ["account", "rename", "work", "lab"])
        self.assertEqual(res_ren.exit_code, 0, res_ren.output)
        self.assertEqual(accounts.load_registry()["active"], "lab")
        self.assertIn("work-gpu", accounts.read_local_sessions("lab"))

        # remove refuses active account; switching away then without --force refuses local sessions
        res_rm_active = self.runner.invoke(colab_cli_mod.app, ["account", "remove", "lab"])
        self.assertNotEqual(res_rm_active.exit_code, 0)

        self.runner.invoke(colab_cli_mod.app, ["account", "use", "default"])
        res_rm_sess = self.runner.invoke(colab_cli_mod.app, ["account", "remove", "lab"])
        self.assertNotEqual(res_rm_sess.exit_code, 0)

        res_rm_force = self.runner.invoke(colab_cli_mod.app, ["account", "remove", "lab", "--force"])
        self.assertEqual(res_rm_force.exit_code, 0, res_rm_force.output)
        self.assertNotIn("lab", accounts.load_registry()["accounts"])
        self.assertFalse(os.path.exists(accounts.account_dir("lab")))

        # Headless two-step login: --url-only then --code
        res_url = self.runner.invoke(
            colab_cli_mod.app,
            ["account", "login", "headless_u", "--email", "head@example.com", "--url-only"],
        )
        self.assertEqual(res_url.exit_code, 0, res_url.output)
        self.assertIn("https://accounts.google.com/", res_url.stdout)
        self.assertIn("login_hint=head%40example.com", res_url.stdout)

        pend_path = accounts.oauth_pending_path("headless_u")
        self.assertTrue(os.path.exists(pend_path))
        with open(pend_path, "r", encoding="utf-8") as f:
            pend_data = json.load(f)
        self.assertTrue(pend_data.get("code_verifier"))

        seen: dict[str, object] = {}

        def fake_fetch_token(flow_self, **kwargs):
            seen["code"] = kwargs.get("code")
            seen["code_verifier"] = flow_self.code_verifier
            tok = {
                "access_token": "ya29.headless",
                "refresh_token": "1//fake-headless-rt",
                "token_type": "Bearer",
                "expires_at": 4070908800,
                "scope": list(pend_data["scopes"]),
            }
            flow_self.oauth2session.token = tok
            return tok

        with (
            mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.fetch_token", autospec=True, side_effect=fake_fetch_token),
            mock.patch.object(cli_hooks, "fetch_google_userinfo", return_value={"email": "head@example.com", "sub": "4242"}),
        ):
            res_code = self.runner.invoke(
                colab_cli_mod.app,
                ["account", "login", "headless_u", "--code", "4/fake-code-123"],
            )
            self.assertEqual(res_code.exit_code, 0, res_code.output)

        self.assertEqual(seen["code"], "4/fake-code-123")
        self.assertEqual(seen["code_verifier"], pend_data["code_verifier"])
        self.assertFalse(os.path.exists(pend_path))
        tok, _ = accounts.get_colab_oauth_token("headless_u")
        self.assertIn("ya29.headless", tok or "")
        self.assertEqual(accounts.load_registry()["accounts"]["headless_u"]["email"], "head@example.com")

    def test_parallel_multi_account_probes_and_non_interactive_guard(self) -> None:
        from colab_cli.client import Accelerator, AssignmentVariant, ListedAssignment, RuntimeProxyInfo, Shape
        from colab_cli.consumption import ConsumptionUserInfo

        accounts.ensure_bootstrapped()
        accounts.ensure_account("a2", email="a2@example.com")
        accounts.ensure_account("unauth", email="unauth@example.com")

        accounts.save_colab_oauth_token("default", _make_valid_oauth_token_json("ya29.a1"))
        accounts.save_colab_oauth_token("a2", _make_valid_oauth_token_json("ya29.a2"))
        _write_json(accounts.sessions_path("default"), {"gpu1": _make_session_dict("gpu1", "ep-1")})

        class FakeClient:
            def __init__(self, env, creds) -> None:
                self.tok = getattr(creds.credentials, "token", "")

            def get_consumption_user_info(self):
                if self.tok == "ya29.a1":
                    return ConsumptionUserInfo(
                        paidComputeUnitsBalance=42.5,
                        consumptionRateHourly=1.25,
                        assignmentsCount=1,
                    )
                return ConsumptionUserInfo(
                    paidComputeUnitsBalance=10.0,
                    consumptionRateHourly=0.0,
                    assignmentsCount=0,
                )

            def list_assignments(self):
                if self.tok == "ya29.a1":
                    return [
                        ListedAssignment(
                            accelerator=Accelerator.T4,
                            endpoint="ep-1",
                            variant=AssignmentVariant.DEFAULT,
                            machineShape=Shape.STANDARD,
                            runtimeProxyInfo=RuntimeProxyInfo(
                                token="t",
                                tokenExpiresInSeconds=3600,
                                url="https://ep-1.example.invalid",
                            ),
                        )
                    ]
                return []

        with (
            mock.patch("colab_cli.common.Client", FakeClient),
            mock.patch("builtins.input", side_effect=AssertionError("must not prompt for input")),
        ):
            r_status = self.runner.invoke(colab_cli_mod.app, ["account", "status", "--json"])
            r_sess = self.runner.invoke(colab_cli_mod.app, ["sessions", "--all", "--json"])
            r_usage = self.runner.invoke(colab_cli_mod.app, ["usage", "--all", "--json"])

        for res in (r_status, r_sess, r_usage):
            self.assertEqual(res.exit_code, 0, res.output)

        status_rows = {r["account"]: r for r in json.loads(r_status.stdout)}
        self.assertIsNone(status_rows["default"]["error"])
        self.assertIsNone(status_rows["a2"]["error"])
        self.assertAlmostEqual(status_rows["default"]["usage"]["balance"], 42.5)
        self.assertEqual(len(status_rows["default"]["assignments"]), 1)
        self.assertEqual(status_rows["default"]["assignments"][0]["name"], "gpu1")
        self.assertAlmostEqual(status_rows["a2"]["usage"]["balance"], 10.0)
        self.assertIn("not authenticated", status_rows["unauth"]["error"])

        sess_rows = {r["account"]: r for r in json.loads(r_sess.stdout)}
        self.assertEqual(sess_rows["default"]["assignments"][0]["endpoint"], "ep-1")
        self.assertEqual(sess_rows["a2"]["assignments"], [])
        self.assertIsNotNone(sess_rows["unauth"]["error"])

        usage_rows = {r["account"]: r for r in json.loads(r_usage.stdout)}
        self.assertAlmostEqual(usage_rows["a2"]["usage"]["balance"], 10.0)
        self.assertIsNotNone(usage_rows["unauth"]["error"])

    def test_cross_account_shared_drive_and_headless_dfs_consent(self) -> None:
        accounts.ensure_bootstrapped()
        accounts.ensure_account("compute", email="compute@example.com")
        accounts.ensure_account("storage", email="storage@example.com")
        accounts.ensure_account("other", email="other@example.com")

        _write_dfs_creds("storage", user_id="123456", email="storage@example.com", refresh_token="1//storage-rt")
        _write_dfs_creds("other", user_id="999999", email="other@example.com", refresh_token="1//other-rt")
        _write_json(accounts.sessions_path("compute"), {"c1": _make_session_dict("c1", "ep-c1")})

        # Persistent shared Drive binding: compute -> storage
        res_bind = self.runner.invoke(colab_cli_mod.app, ["account", "set-drive", "storage", "-a", "compute"])
        self.assertEqual(res_bind.exit_code, 0, res_bind.output)

        cfg = drive_mount.make_cfg(account="compute")
        self.assertEqual(cfg["colab_account"], "compute")
        self.assertEqual(cfg["drive_account"], "storage")
        self.assertEqual(cfg["user_id"], "123456")
        self.assertEqual(cfg["refresh_token"], "1//storage-rt")

        # One-off override -> other
        cfg_override = drive_mount.make_cfg(account="compute", drive_account="other")
        self.assertEqual(cfg_override["drive_account"], "other")
        self.assertEqual(cfg_override["user_id"], "999999")
        self.assertEqual(cfg_override["refresh_token"], "1//other-rt")

        # Dry-run via CLI shows storage's user_id and respects -a compute
        res_dry = self.runner.invoke(colab_cli_mod.app, ["dfs", "mount", "--dry-run", "-a", "compute", "-s", "c1"])
        self.assertEqual(res_dry.exit_code, 0, res_dry.output)
        self.assertIn('"user_id": "123456"', res_dry.stdout)

        # Clear binding
        res_clear = self.runner.invoke(colab_cli_mod.app, ["account", "set-drive", "--clear", "-a", "compute"])
        self.assertEqual(res_clear.exit_code, 0, res_clear.output)
        self.assertIsNone(accounts.load_registry()["accounts"]["compute"]["drive_account"])

        # Headless `colab drive consent -a headless --url-only` auto-selects `sdk` and writes pending state
        res_cons = self.runner.invoke(colab_cli_mod.app, ["drive", "consent", "-a", "headless", "--url-only"])
        self.assertEqual(res_cons.exit_code, 0, res_cons.output)
        self.assertIn("https://accounts.google.com/", res_cons.stdout)
        dfs_pend = accounts.dfs_pending_path("headless")
        self.assertTrue(os.path.exists(dfs_pend))
        with open(dfs_pend, "r", encoding="utf-8") as f:
            pend = json.load(f)
        self.assertEqual(pend["flavor"], "sdk")

        # `colab ext doctor` and legacy `colab dfs doctor` preserve `source: active`
        for doc_args in (["ext", "doctor"], ["dfs", "doctor"]):
            res_doc = self.runner.invoke(colab_cli_mod.app, doc_args)
            self.assertEqual(res_doc.exit_code, 0, res_doc.output)
            self.assertIn("colab-ext v", res_doc.stdout)
            self.assertIn("active account  : default (source: active,", res_doc.stdout)

    def test_skill_augmentation_and_url_authuser_clean_stdout(self) -> None:
        # Both `colab skill` and `colab SKILL` return augmented Markdown with valid frontmatter.
        for cmd in ("skill", "SKILL"):
            res = self.runner.invoke(colab_cli_mod.app, [cmd])
            self.assertEqual(res.exit_code, 0, res.output)
            text = res.stdout
            self.assertTrue(text.startswith("---\n"), f"{cmd} missing YAML frontmatter start")
            second_sep = text.find("\n---", 4)
            self.assertGreater(second_sep, 4)
            self.assertIn("colab account", text)
            self.assertIn("colab drive mount", text)
            self.assertIn("colab ext doctor", text)
            self.assertIn("--drive-account", text)
            # Idempotent
            self.assertEqual(skill_doc.augment_skill_markdown(text), text)

        # `colab url -s work-gpu` auto-routes from active `default` -> `work`,
        # injects URL-encoded `authuser`, keeps stdout clean, and prints notice only on stderr.
        accounts.ensure_bootstrapped()
        accounts.ensure_account("work", email="user+tag@example.com")
        _write_json(accounts.sessions_path("work"), {"work-gpu": _make_session_dict("work-gpu", "ep-work-123")})

        res_url = self.runner.invoke(colab_cli_mod.app, ["url", "-s", "work-gpu"])
        self.assertEqual(res_url.exit_code, 0, res_url.output)
        lines = [ln for ln in res_url.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"expected clean single-line URL on stdout, got: {res_url.stdout!r}")
        self.assertTrue(lines[0].startswith("https://colab.research.google.com/"))
        self.assertIn("authuser=user%2Btag%40example.com", lines[0])
        self.assertNotIn("[colab]", res_url.stdout)
        self.assertIn("Routing session 'work-gpu' -> account 'work'", res_url.stderr)

    def test_enhanced_drivemount_zero_browser_fallback_and_flags(self) -> None:
        import base64
        import re
        from colab_cli.commands import automation
        from colab_ext import remote

        accounts.ensure_bootstrapped()
        accounts.ensure_account("compute", email="compute@example.com")
        accounts.ensure_account("storage", email="storage@example.com")
        accounts.ensure_account("nostore", email="nostore@example.com")

        _write_dfs_creds("storage", user_id="123456", email="storage@example.com", refresh_token="1//storage-secret-rt")
        _write_json(accounts.sessions_path("compute"), {"c1": _make_session_dict("c1", "ep-c1")})
        _write_json(accounts.sessions_path("storage"), {"st1": _make_session_dict("st1", "ep-st1")})
        _write_json(accounts.sessions_path("nostore"), {"n1": _make_session_dict("n1", "ep-n1")})

        # Bind compute -> storage
        res_bind = self.runner.invoke(colab_cli_mod.app, ["account", "set-drive", "storage", "-a", "compute"])
        self.assertEqual(res_bind.exit_code, 0, res_bind.output)

        # 1. Zero-browser path + auto-routing (`c1` -> `compute` -> `storage`) + `--readonly` + history log
        captured_exec: list[tuple[str, str, int]] = []

        def fake_exec_code(name, code, timeout=180, retries=3):
            captured_exec.append((name, code, timeout))
            return 'DFS_RESULT {"ok": true, "mountpoint": "/content/drive", "entries": "5", "mode": "shim", "shim": "http://127.0.0.1:8012"}\n'

        with mock.patch.object(remote, "exec_code", side_effect=fake_exec_code):
            res_mount = self.runner.invoke(colab_cli_mod.app, ["drivemount", "-s", "c1", "--readonly"])
        self.assertEqual(res_mount.exit_code, 0, res_mount.output)
        self.assertEqual(len(captured_exec), 1)
        self.assertEqual(captured_exec[0][0], "c1")
        self.assertIn("Routing session 'c1' -> account 'compute'", res_mount.stderr)
        self.assertIn("compute -> drive:storage", res_mount.stdout)

        # Verify payload config contains storage user_id + readonly=True
        b64_matches = re.findall(r"[A-Za-z0-9+/=]{60,}", captured_exec[0][1])
        decoded_cfgs = []
        for tok in b64_matches:
            try:
                raw = base64.b64decode(tok).decode("utf-8")
                if raw.startswith("{"):
                    decoded_cfgs.append(json.loads(raw))
            except Exception:
                pass
        self.assertTrue(decoded_cfgs, "expected embedded base64 DFS config in remote payload")
        self.assertEqual(decoded_cfgs[0]["user_id"], "123456")
        self.assertIs(decoded_cfgs[0]["readonly"], True)

        # Verify secret-free history entry in compute/history/c1.jsonl
        hist_file = os.path.join(accounts.history_dir("compute"), "c1.jsonl")
        self.assertTrue(os.path.exists(hist_file))
        with open(hist_file, "r", encoding="utf-8") as f:
            hist_text = f.read()
        self.assertIn('"op": "drivemount"', hist_text)
        self.assertIn('"event_type": "automation"', hist_text)
        self.assertNotIn("1//storage-secret-rt", hist_text)

        # 2. `--dry-run` and `-a` after `drivemount`
        res_dry = self.runner.invoke(
            colab_cli_mod.app,
            ["drivemount", "-s", "c1", "-a", "compute", "--dry-run"],
        )
        self.assertEqual(res_dry.exit_code, 0, res_dry.output)
        self.assertIn('"user_id": "123456"', res_dry.stdout)

        # 3. Safety refusal when `--drive-account` or `--no-fallback` lacks credentials (no upstream call!)
        upstream_calls: list[tuple] = []

        def fake_run_automation(name, op, code, allow_stdin=False, path=None, timeout=None):
            upstream_calls.append((name, op, path))
            with mock.patch("colab_cli.commands.automation.open_url_in_browser", return_value=False):
                automation.show_drive_auth_url("https://example.invalid/drive-consent")

        with mock.patch.object(automation, "run_automation", side_effect=fake_run_automation):
            res_bad_drv = self.runner.invoke(
                colab_cli_mod.app,
                ["-a", "compute", "drivemount", "-s", "c1", "--drive-account", "nostore"],
            )
            self.assertEqual(res_bad_drv.exit_code, 1, res_bad_drv.output)
            self.assertEqual(upstream_calls, [])

            res_no_fb = self.runner.invoke(
                colab_cli_mod.app,
                ["-a", "nostore", "drivemount", "-s", "n1", "--no-fallback"],
            )
            self.assertEqual(res_no_fb.exit_code, 1, res_no_fb.output)
            self.assertIn("drive consent", res_no_fb.stderr)
            self.assertEqual(upstream_calls, [])

            # 4a. Graceful fallback to upstream when credentials are missing (prints tip + account hint)
            res_fb = self.runner.invoke(
                colab_cli_mod.app,
                ["-a", "nostore", "drivemount", "-s", "n1"],
            )
            self.assertEqual(res_fb.exit_code, 0, res_fb.output)
            self.assertIn("drive consent", res_fb.stderr)
            self.assertIn("nostore@example.com", res_fb.stderr)
            self.assertEqual(len(upstream_calls), 1)
            self.assertEqual(upstream_calls[0][0], "n1")

            # 4b. `--upstream` forces upstream flow even when `storage` has valid dfs credentials
            with mock.patch.object(remote, "exec_code", side_effect=AssertionError("must not run DriveFS")):
                res_up = self.runner.invoke(
                    colab_cli_mod.app,
                    ["-a", "storage", "drivemount", "-s", "st1", "--upstream"],
                )
                self.assertEqual(res_up.exit_code, 0, res_up.output)
                self.assertEqual(len(upstream_calls), 2)

                # 4c. `COLAB_DRIVE_DEFAULT=0` forces upstream flow as well
                os.environ["COLAB_DRIVE_DEFAULT"] = "0"
                try:
                    res_env_off = self.runner.invoke(
                        colab_cli_mod.app,
                        ["-a", "storage", "drivemount", "-s", "st1"],
                    )
                    self.assertEqual(res_env_off.exit_code, 0, res_env_off.output)
                    self.assertEqual(len(upstream_calls), 3)

                    # Reject DriveFS-only option combined with COLAB_DRIVE_DEFAULT=0
                    res_conflict = self.runner.invoke(
                        colab_cli_mod.app,
                        ["-a", "storage", "drivemount", "-s", "st1", "--readonly"],
                    )
                    self.assertEqual(res_conflict.exit_code, 1, res_conflict.output)
                finally:
                    os.environ.pop("COLAB_DRIVE_DEFAULT", None)

    def test_ext_drive_dfs_alias_and_legacy_pth_migration(self) -> None:
        from colab_cli.commands import automation
        from colab_ext import account_commands, remote

        accounts.ensure_bootstrapped()
        accounts.ensure_account("work", email="work@example.com")
        accounts.ensure_account("drv_new", email="drv_new@example.com")
        accounts.ensure_account("drv_old", email="drv_old@example.com")
        _write_dfs_creds("work", user_id="101", email="work@example.com", refresh_token="1//work-rt")
        _write_dfs_creds("drv_new", user_id="202", email="drv_new@example.com", refresh_token="1//new-rt")
        _write_dfs_creds("drv_old", user_id="303", email="drv_old@example.com", refresh_token="1//old-rt")
        _write_json(accounts.sessions_path("work"), {"w1": _make_session_dict("w1", "ep-w1")})

        # `colab ext` subcommands (`doctor`, `status`, `version`)
        for sub in ("doctor", "status"):
            r_ext = self.runner.invoke(colab_cli_mod.app, ["ext", sub])
            self.assertEqual(r_ext.exit_code, 0, r_ext.output)
            self.assertIn("colab-ext v", r_ext.stdout)

        r_ver = self.runner.invoke(colab_cli_mod.app, ["ext", "version"])
        self.assertEqual(r_ver.exit_code, 0, r_ver.output)
        ver_data = json.loads(r_ver.stdout)
        self.assertEqual(ver_data["extension"], colab_ext.__version__)

        # `colab drive` and backward-compatible `colab dfs` alias parity
        for grp in ("drive", "dfs"):
            r_show = self.runner.invoke(colab_cli_mod.app, [grp, "show", "-a", "work"])
            self.assertEqual(r_show.exit_code, 0, r_show.output)
            self.assertIn('"user_id": "101"', r_show.stdout)

            # Session auto-routing on `mount --dry-run` (omitting `-a`)
            r_dry = self.runner.invoke(colab_cli_mod.app, [grp, "mount", "--dry-run", "-s", "w1"])
            self.assertEqual(r_dry.exit_code, 0, r_dry.output)
            self.assertIn('"colab_account": "work"', r_dry.stdout)
            self.assertIn("Routing session 'w1' -> account 'work'", r_dry.stderr)

        # `colab drive status` with session auto-routing
        with mock.patch.object(
            remote,
            "exec_code",
            return_value='DFS_RESULT {"mountpoint": "/content/drive", "mounted": true, "mount_line": [], "drive_procs": "1", "shim_procs": "1", "entries": 3, "official_env": {}}\n',
        ) as m_exec:
            r_st = self.runner.invoke(colab_cli_mod.app, ["drive", "status", "-s", "w1"])
        self.assertEqual(r_st.exit_code, 0, r_st.output)
        self.assertEqual(m_exec.call_args.args[0], "w1")
        self.assertIn("account     : work", r_st.stdout)

        # `colab drive consent --url-only`
        r_cons = self.runner.invoke(colab_cli_mod.app, ["drive", "consent", "-a", "drv_cons", "--url-only"])
        self.assertEqual(r_cons.exit_code, 0, r_cons.output)
        self.assertIn("https://accounts.google.com/", r_cons.stdout)

        # `COLAB_DRIVE_ACCOUNT` + legacy `COLAB_DFS_ACCOUNT` resolution
        os.environ["COLAB_DFS_ACCOUNT"] = "drv_old"
        try:
            self.assertEqual(drive_mount.make_cfg(account="work")["drive_account"], "drv_old")
            os.environ["COLAB_DRIVE_ACCOUNT"] = "drv_new"
            self.assertEqual(drive_mount.make_cfg(account="work")["drive_account"], "drv_new")
        finally:
            os.environ.pop("COLAB_DRIVE_ACCOUNT", None)
            os.environ.pop("COLAB_DFS_ACCOUNT", None)

        # Legacy `COLAB_DFS_DEFAULT=0` disables zero-browser override in `drivemount`
        with (
            mock.patch.object(automation, "run_automation", return_value=None) as m_auto,
            mock.patch.object(remote, "exec_code", side_effect=AssertionError("must not run DriveFS")),
        ):
            os.environ["COLAB_DFS_DEFAULT"] = "0"
            try:
                r_leg_off = self.runner.invoke(colab_cli_mod.app, ["-a", "work", "drivemount", "-s", "w1"])
                self.assertEqual(r_leg_off.exit_code, 0, r_leg_off.output)
                self.assertEqual(m_auto.call_count, 1)
            finally:
                os.environ.pop("COLAB_DFS_DEFAULT", None)

        # `--with-drive/--no-drive` advertised in help; hidden `--no-dfs` alias still honored
        r_add_help = self.runner.invoke(colab_cli_mod.app, ["account", "add", "--help"])
        self.assertEqual(r_add_help.exit_code, 0, r_add_help.output)
        self.assertIn("--with-drive", r_add_help.stdout)
        self.assertIn("--no-drive", r_add_help.stdout)

        with (
            mock.patch.object(account_commands, "_interactive_oauth_login", return_value="keyring"),
            mock.patch.object(
                account_commands,
                "_run_dfs_consent_for_account",
                side_effect=AssertionError("--no-dfs must skip DriveFS consent"),
            ),
        ):
            r_no_dfs = self.runner.invoke(colab_cli_mod.app, ["account", "add", "compat_u", "--no-dfs"])
            self.assertEqual(r_no_dfs.exit_code, 0, r_no_dfs.output)
            self.assertIn("compat_u", accounts.load_registry()["accounts"])

        # `inject.ensure` writes `colab_ext.pth` and automatically removes legacy `colab_dfs.pth`
        fake_site = os.path.join(self.tmpdir, "fake-site-packages")
        os.makedirs(fake_site, exist_ok=True)
        legacy_pth = os.path.join(fake_site, "colab_dfs.pth")
        new_pth = os.path.join(fake_site, "colab_ext.pth")
        with open(legacy_pth, "w", encoding="utf-8") as f:
            f.write("# legacy hook\n")

        rep = inject.ensure(site_dir=fake_site, copy_source=False, quiet=True)
        self.assertEqual(rep["pth"], new_pth)
        self.assertTrue(os.path.exists(new_pth))
        self.assertFalse(os.path.exists(legacy_pth))
        self.assertEqual(rep.get("removed_legacy"), [legacy_pth])

        rm_rep = inject.remove(site_dir=fake_site)
        self.assertTrue(rm_rep["removed"])
        self.assertFalse(os.path.exists(new_pth))


if __name__ == "__main__":
    unittest.main()
