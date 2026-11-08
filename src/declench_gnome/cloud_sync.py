import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import time
from typing import Any, Dict, List, Optional
import urllib.error
from urllib.parse import parse_qs, urlencode, urlparse
import urllib.request

from declench_gnome import CANONICAL_WEB_URL
from declench_gnome.local_store import LocalDeclenchStore


SIGNUP_TRIAL_URL = f"{CANONICAL_WEB_URL}/signup?utm_source=gnome_linux&utm_medium=desktop_app"
BILLING_WEB_URL = f"{CANONICAL_WEB_URL}/billing?utm_source=gnome_linux&utm_medium=desktop_app"
METHOD_WEB_URL = f"{CANONICAL_WEB_URL}/method"
HOMEPAGE_MOBILE_URL = f"{CANONICAL_WEB_URL}/?utm_source=gnome_linux#mobile-apps"

PENDING_AUTH_TTL_SECONDS = 900.0


class CloudSyncError(RuntimeError):
    """Raised when communicating with declench.app fails."""


def default_cloud_config_path() -> Path:
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_cfg) if xdg_cfg else (Path.home() / ".config")
    folder = base / "declench"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / "cloud_session.json"


class DeclenchCloudClient:
    """
    Optional Declench Pro (`https://declench.app`) cloud sync client.
    Authenticates via system browser + PKCE `declench://auth-callback` deep link
    (supporting Google Sign-In, Sign in with Apple, and Email/Password without typing
    credentials in the desktop app), using only Python stdlib modules.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        session_file: Optional[Path | str] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("DECLENCH_API_BASE_URL")
            or CANONICAL_WEB_URL
        ).rstrip("/")
        self.session_file = Path(session_file) if session_file else default_cloud_config_path()
        self.pending_auth_file = self.session_file.with_name("pending_auth.json")
        self._state = self._load_json_file(self.session_file)

    @staticmethod
    def _load_json_file(path: Path) -> Dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save_private_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)

    def _save_session_file(self, data: Dict[str, Any]) -> None:
        merged = dict(self._state)
        merged.update(data)
        self._save_private_json(self.session_file, merged)
        self._state = merged

    def _clear_pending_auth(self) -> None:
        try:
            if self.pending_auth_file.exists():
                self.pending_auth_file.unlink()
        except OSError:
            pass

    @staticmethod
    def default_device_name() -> str:
        try:
            host = (socket.gethostname() or "").strip()
        except Exception:
            host = ""
        if host and host.lower() not in ("localhost", "localhost.localdomain"):
            return f"Linux Desktop ({host[:40]})"
        return "Linux Desktop"

    @property
    def device_token(self) -> str:
        tok = str(self._state.get("device_token") or "").strip()
        if len(tok) >= 16:
            return tok
        tok = f"linux-gnome-{secrets.token_hex(12)}"
        self._state["device_token"] = tok
        if self.session_file.exists() or self._state.get("session_token"):
            self._save_private_json(self.session_file, self._state)
        return tok

    @property
    def cached_devices(self) -> List[Dict[str, Any]]:
        devs = self._state.get("devices")
        if not isinstance(devs, list):
            return []
        return [d for d in devs if isinstance(d, dict)]

    @property
    def this_device(self) -> Optional[Dict[str, Any]]:
        my_tok = str(self._state.get("device_token") or "").strip()
        if not my_tok:
            return None
        for d in self.cached_devices:
            if str(d.get("fcm_token") or "").strip() == my_tok:
                return d
        return None

    @property
    def is_this_device_registered(self) -> bool:
        return self.this_device is not None

    @property
    def is_this_device_enabled(self) -> bool:
        if not self.is_signed_in:
            return True
        dev = self.this_device
        if dev is not None:
            return bool(dev.get("enabled", True))
        if bool(self._state.get("device_removed_by_user")):
            return False
        return True

    @property
    def active_device_count(self) -> int:
        return sum(1 for d in self.cached_devices if d.get("enabled"))

    @property
    def session_token(self) -> Optional[str]:
        tok = str(self._state.get("session_token") or "").strip()
        return tok or None

    @property
    def cached_user(self) -> Optional[Dict[str, Any]]:
        user = self._state.get("user")
        return user if isinstance(user, dict) else None

    @property
    def is_signed_in(self) -> bool:
        return self.session_token is not None

    def sign_out(self) -> None:
        preserved_tok = self._state.get("device_token")
        self._state = {"device_token": preserved_tok} if preserved_tok else {}
        self._clear_pending_auth()
        try:
            if self.session_file.exists():
                self.session_file.unlink()
        except OSError:
            pass

    def _request_json(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Declench-GNOME-Linux/1.0",
        }
        if authenticated and self.session_token:
            headers["Authorization"] = f"Bearer {self.session_token}"

        raw_body: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            err_text = exc.read().decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(err_text)
                msg = str(parsed.get("error") or f"HTTP {exc.code}")
            except ValueError:
                msg = f"Server returned HTTP {exc.code}."
            if exc.code == 401 and authenticated:
                self.sign_out()
            raise CloudSyncError(msg) from exc
        except urllib.error.URLError as exc:
            raise CloudSyncError(f"Could not reach {self.base_url}: {exc.reason}") from exc

    # --- Browser PKCE Sign-In + Deep-Link Callback (`declench://auth-callback`) ---

    @staticmethod
    def compute_pkce_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def start_browser_sign_in(self) -> str:
        """
        Generate a fresh S256 PKCE verifier + state token, persist them to `pending_auth.json`
        (`0o600`), and return the browser authorization URL on `declench.app`.
        """
        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = self.compute_pkce_challenge(code_verifier)
        self._save_private_json(
            self.pending_auth_file,
            {
                "state": state,
                "code_verifier": code_verifier,
                "created_at": time.time(),
            },
        )
        qs = urlencode(
            {
                "state": state,
                "code_challenge": code_challenge,
                "platform": "gnome_linux",
            }
        )
        return f"{self.base_url}/auth/desktop/authorize?{qs}"

    def complete_browser_callback(self, callback_uri: str) -> Dict[str, Any]:
        """
        Validate a `declench://auth-callback?code=...&state=...` URI against the pending
        PKCE state and exchange the one-time authorization code for a Bearer session JWT.
        """
        cleaned = (callback_uri or "").strip()
        parsed = urlparse(cleaned)
        if parsed.scheme.lower() != "declench":
            raise CloudSyncError("Invalid callback link. Expected a declench:// sign-in link.")

        params = parse_qs(parsed.query)
        err = (params.get("error") or [""])[0].strip()
        if err:
            self._clear_pending_auth()
            raise CloudSyncError(err)

        code = (params.get("code") or [""])[0].strip()
        received_state = (params.get("state") or [""])[0].strip()
        if not code or not received_state:
            raise CloudSyncError("Sign-in callback link is missing code or state parameters.")

        pending = self._load_json_file(self.pending_auth_file)
        expected_state = str(pending.get("state") or "").strip()
        code_verifier = str(pending.get("code_verifier") or "").strip()
        created_at = float(pending.get("created_at") or 0.0)

        if not expected_state or not code_verifier:
            raise CloudSyncError(
                "No pending browser sign-in found. Click Sign In with Browser to start."
            )
        if time.time() - created_at > PENDING_AUTH_TTL_SECONDS:
            self._clear_pending_auth()
            raise CloudSyncError(
                "Browser sign-in session timed out. Click Sign In with Browser again."
            )
        if not hmac.compare_digest(expected_state, received_state):
            raise CloudSyncError("Sign-in state mismatch. Click Sign In with Browser again.")

        resp = self._request_json(
            "POST",
            "/api/v1/auth/desktop/exchange",
            body={"code": code, "code_verifier": code_verifier},
            authenticated=False,
        )
        self._clear_pending_auth()

        token = str(resp.get("session_token") or "").strip()
        user = resp.get("user")
        if not token or not isinstance(user, dict):
            raise CloudSyncError("Server did not return a desktop session token.")

        self._save_session_file({"session_token": token, "user": user})
        return user

    def sign_in_with_email(self, email: str, password: str) -> Dict[str, Any]:
        resp = self._request_json(
            "POST",
            "/auth/local/login",
            body={"email": email.strip(), "password": password},
            authenticated=False,
        )
        token = str(resp.get("session_token") or "").strip()
        user = resp.get("user")
        if not token or not isinstance(user, dict):
            raise CloudSyncError("Server did not return a session token.")
        self._save_session_file({"session_token": token, "user": user})
        return user

    def refresh_account_status(self) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        resp = self._request_json("GET", "/api/v1/auth/me")
        if not resp.get("authenticated") or not isinstance(resp.get("user"), dict):
            self.sign_out()
            return None
        user = resp["user"]
        self._save_session_file({"session_token": self.session_token, "user": user})
        return user

    def sync_with_local_store(self, store: LocalDeclenchStore) -> Dict[str, Any]:
        """
        Two-way sync check-in logs, progression, schedule, and theme with `declench.app`
        when the user has an active Declench Pro subscription or active 5-Day Free Trial.
        """
        user = self.refresh_account_status()
        if not user:
            raise CloudSyncError("Please sign in to sync with Declench Cloud.")
        if not user.get("has_active_access"):
            raise CloudSyncError(
                "Your Declench Cloud trial has ended. Local Linux mode remains 100% free, "
                "or upgrade to Declench Pro to sync across iPhone, Android, and Web."
            )

        # 1. Upload any unsynced local 5s urges and locally-answered cloud check-ins first
        mutations = store.get_unsynced_cloud_mutations()
        pushed_urges = 0
        for urge in mutations["urges"]:
            urge_resp = self._request_json(
                "POST", "/api/v1/urges", body={"note": urge["note"]}
            )
            created_log = urge_resp.get("log") if isinstance(urge_resp, dict) else None
            cloud_id = (
                int(created_log["id"])
                if isinstance(created_log, dict) and created_log.get("id") is not None
                else None
            )
            store.mark_urge_synced(urge["local_id"], cloud_id)
            pushed_urges += 1

        pushed_feedback = 0
        for fb in mutations["feedback"]:
            try:
                self._request_json(
                    "POST",
                    f"/api/v1/logs/{fb['cloud_log_id']}/feedback",
                    body={"outcome": fb["outcome"], "note": fb["note"]},
                )
                store.mark_feedback_synced(fb["local_id"])
                pushed_feedback += 1
            except CloudSyncError as exc:
                if "not found" in str(exc).lower():
                    store.mark_feedback_synced(fb["local_id"])
                else:
                    raise

        # 2. Pull canonical cloud schedule + progression state
        prog_resp = self._request_json("GET", "/api/v1/program")
        cloud_prog = prog_resp.get("program") or {}

        # 3. Pull full check-in history from /api/v1/logs/export?format=json (fallback /api/v1/logs)
        cloud_logs = None
        is_complete = True
        try:
            export_resp = self._request_json("GET", "/api/v1/logs/export?format=json")
            if isinstance(export_resp.get("checkins"), list):
                cloud_logs = export_resp["checkins"]
                is_complete = True
        except CloudSyncError:
            cloud_logs = None

        if cloud_logs is None:
            logs_resp = self._request_json("GET", "/api/v1/logs?limit=1000")
            if isinstance(logs_resp.get("logs"), list):
                cloud_logs = logs_resp["logs"]
                is_complete = len(cloud_logs) < 1000
            else:
                cloud_logs = []

        # 4. Sync connected notification devices & register/refresh this Linux desktop
        devices = self._sync_devices_on_poll()

        # 5. Hydrate local SQLite store idempotently
        snap = store.apply_cloud_snapshot(
            user=user,
            cloud_program=cloud_prog,
            cloud_logs=cloud_logs,
            is_complete_log_snapshot=is_complete,
        )

        return {
            "user": user,
            "devices": devices,
            "this_device": self.this_device,
            "this_device_enabled": self.is_this_device_enabled,
            "cloud_stats": prog_resp.get("stats") or snap["stats"],
            "program": snap["program"],
            "stats": snap["stats"],
            "imported_logs": snap["imported_logs"],
            "updated_logs": snap["updated_logs"],
            "pushed_urges": pushed_urges,
            "pushed_feedback": pushed_feedback,
        }

    # --- Notification Device Routing & Multi-Device Management ---

    def _sync_devices_on_poll(self) -> List[Dict[str, Any]]:
        if not self.is_signed_in:
            return []
        try:
            if bool(self._state.get("device_removed_by_user")):
                return self.fetch_devices()
            resp = self.register_this_device(enabled=None)
            return resp.get("devices") if isinstance(resp.get("devices"), list) else self.cached_devices
        except CloudSyncError:
            try:
                return self.fetch_devices()
            except CloudSyncError:
                return self.cached_devices

    def fetch_devices(self) -> List[Dict[str, Any]]:
        if not self.is_signed_in:
            return []
        resp = self._request_json("GET", "/api/v1/devices")
        devs = resp.get("devices") if isinstance(resp.get("devices"), list) else []
        self._save_session_file({"devices": devs})
        return self.cached_devices

    def register_this_device(
        self,
        *,
        enabled: Optional[bool] = None,
        only_this: bool = False,
        device_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.is_signed_in:
            raise CloudSyncError("Sign in to Declench Cloud to manage connected devices.")

        name = (device_name or self.default_device_name()).strip()[:120]
        payload: Dict[str, Any] = {
            "fcm_token": self.device_token,
            "platform": "linux",
            "device_name": name,
        }
        if only_this:
            payload["enabled"] = True
            payload["only_this"] = True
        elif enabled is not None:
            payload["enabled"] = bool(enabled)

        try:
            resp = self._request_json("POST", "/api/v1/devices", body=payload)
        except CloudSyncError as exc:
            if "platform must be one of" in str(exc).lower():
                fallback_payload = {**payload, "platform": "web"}
                resp = self._request_json("POST", "/api/v1/devices", body=fallback_payload)
            else:
                raise

        dev = resp.get("device") if isinstance(resp.get("device"), dict) else None
        devs = resp.get("devices") if isinstance(resp.get("devices"), list) else []

        # Ensure only_this mutes other devices even against older backend builds
        if only_this and dev and dev.get("id") is not None:
            other_active = [
                d for d in devs if d.get("id") != dev.get("id") and d.get("enabled")
            ]
            if other_active:
                patch_resp = self._request_json(
                    "PATCH",
                    f"/api/v1/devices/{int(dev['id'])}",
                    body={"only_this": True},
                )
                dev = (
                    patch_resp.get("device")
                    if isinstance(patch_resp.get("device"), dict)
                    else dev
                )
                devs = (
                    patch_resp.get("devices")
                    if isinstance(patch_resp.get("devices"), list)
                    else devs
                )

        self._save_session_file(
            {
                "devices": devs,
                "device_removed_by_user": False,
            }
        )
        return {"device": dev, "devices": self.cached_devices}

    def set_device_active(self, device_id: int, enabled: bool) -> Dict[str, Any]:
        if not self.is_signed_in:
            raise CloudSyncError("Sign in to Declench Cloud to manage connected devices.")
        resp = self._request_json(
            "PATCH",
            f"/api/v1/devices/{int(device_id)}",
            body={"enabled": bool(enabled)},
        )
        if isinstance(resp.get("devices"), list):
            self._save_session_file({"devices": resp["devices"]})
        return resp

    def set_only_active_device(self, device_id: int) -> Dict[str, Any]:
        if not self.is_signed_in:
            raise CloudSyncError("Sign in to Declench Cloud to manage connected devices.")
        resp = self._request_json(
            "PATCH",
            f"/api/v1/devices/{int(device_id)}",
            body={"only_this": True},
        )
        if isinstance(resp.get("devices"), list):
            self._save_session_file({"devices": resp["devices"]})
        return resp

    def remove_device(self, device_id: int) -> Dict[str, Any]:
        if not self.is_signed_in:
            raise CloudSyncError("Sign in to Declench Cloud to manage connected devices.")
        my_dev = self.this_device
        is_removing_self = my_dev is not None and int(my_dev.get("id") or -1) == int(device_id)
        resp = self._request_json("DELETE", f"/api/v1/devices/{int(device_id)}")
        updates: Dict[str, Any] = {}
        if isinstance(resp.get("devices"), list):
            updates["devices"] = resp["devices"]
        if is_removing_self:
            updates["device_removed_by_user"] = True
        if updates:
            self._save_session_file(updates)
        return resp

    # --- Live Two-Way Desktop Action Helpers ---

    def push_program_settings(
        self, store: LocalDeclenchStore, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        resp = self._request_json("PUT", "/api/v1/program", body=payload)
        if isinstance(resp.get("program"), dict):
            store.apply_cloud_program(resp["program"])
        return resp

    def push_pause(
        self, store: LocalDeclenchStore, mode: str, minutes: int = 60
    ) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        body: Dict[str, Any] = {"mode": mode}
        if mode == "minutes":
            body["minutes"] = int(minutes)
        resp = self._request_json("POST", "/api/v1/program/pause", body=body)
        if isinstance(resp.get("program"), dict):
            store.apply_cloud_program(resp["program"])
        return resp

    def push_resume(self, store: LocalDeclenchStore) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        resp = self._request_json("POST", "/api/v1/program/resume", body={})
        if isinstance(resp.get("program"), dict):
            store.apply_cloud_program(resp["program"])
        return resp

    def push_reset_interval(self, store: LocalDeclenchStore) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        resp = self._request_json("POST", "/api/v1/program/reset-interval", body={})
        if isinstance(resp.get("program"), dict):
            store.apply_cloud_program(resp["program"])
        return resp

    def push_restore_defaults(self, store: LocalDeclenchStore) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        resp = self._request_json("POST", "/api/v1/program/restore-defaults", body={})
        if isinstance(resp.get("program"), dict):
            store.apply_cloud_program(resp["program"])
        return resp

    def push_clear_unanswered(self) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        return self._request_json("POST", "/api/v1/logs/clear-unanswered", body={})

    def push_theme(self, theme_key: str) -> Optional[Dict[str, Any]]:
        if not self.is_signed_in:
            return None
        resp = self._request_json(
            "PUT", "/api/v1/account/theme", body={"theme": theme_key}
        )
        if isinstance(resp.get("user"), dict) and self.session_token:
            self._save_session_file({"session_token": self.session_token, "user": resp["user"]})
        return resp
