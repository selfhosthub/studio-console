# studio_console/cloudflare/cf_api.py
"""Cloudflare API v4 client — stdlib only, no third-party deps."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class CloudflareError(Exception):
    def __init__(self, message: str, status_code: int = 0, errors: list | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []

    def __str__(self) -> str:
        if self.errors:
            details = "; ".join(
                e.get("message", str(e)) for e in self.errors if isinstance(e, dict)
            )
            return f"{super().__str__()} ({details})"
        return super().__str__()


class CloudflareAPI:
    """Thin wrapper around the Cloudflare API v4 endpoints used by studio-console."""

    BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, token: str, account_id: str = "") -> None:
        self.token = token
        self.account_id = account_id

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> Any:
        url = f"{self.BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                result = json.loads(e.read().decode())
            except Exception:
                raise CloudflareError(str(e), e.code)
            errors = result.get("errors", [])
            msg = errors[0].get("message", str(e)) if errors else str(e)
            raise CloudflareError(msg, e.code, errors)
        except (urllib.error.URLError, OSError) as e:
            raise CloudflareError(f"Network error: {e}")

        if not result.get("success"):
            errors = result.get("errors", [])
            msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
            raise CloudflareError(msg, errors=errors)

        return result.get("result")

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body)

    def _put(self, path: str, body: dict) -> Any:
        return self._request("PUT", path, body)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def _acct(self, path: str) -> str:
        """Build an account-scoped path."""
        if not self.account_id:
            raise CloudflareError("account_id is required for this operation")
        return f"/accounts/{self.account_id}{path}"

    # ------------------------------------------------------------------
    # Account discovery
    # ------------------------------------------------------------------

    def verify_token(self) -> bool:
        """Verify the token is valid via /user/tokens/verify. Returns True if active.

        Raises CloudflareError on auth/permission/network failures (carrying
        status_code) so callers can distinguish 'expired/invalid' from
        'insufficient scopes' (403). A non-active-but-successful response
        returns False without raising.
        """
        result = self._get("/user/tokens/verify")
        return isinstance(result, dict) and result.get("status") == "active"

    def list_accounts(self) -> list[dict]:
        """List accounts. Requires Account:Read permission — may return [] with tunnel-only tokens."""
        result = self._get("/accounts")
        return result if isinstance(result, list) else []

    def list_memberships(self) -> list[dict]:
        """List account memberships. Works with tokens that have no Account:Read permission."""
        result = self._get("/memberships?status=accepted")
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Tunnel management
    # ------------------------------------------------------------------

    def list_tunnels(self) -> list[dict]:
        result = self._get(self._acct("/cfd_tunnel?is_deleted=false"))
        return result if isinstance(result, list) else []

    def create_tunnel(self, name: str) -> dict:
        return self._post(self._acct("/cfd_tunnel"), {"name": name, "tunnel_secret": _random_secret()})

    def get_tunnel_token(self, tunnel_id: str) -> str:
        result = self._get(self._acct(f"/cfd_tunnel/{tunnel_id}/token"))
        return result if isinstance(result, str) else ""

    def delete_tunnel(self, tunnel_id: str) -> None:
        self._delete(self._acct(f"/cfd_tunnel/{tunnel_id}"))

    def get_tunnel_config(self, tunnel_id: str) -> list[dict]:
        """Return the tunnel's current ingress rules (without the catch-all)."""
        result = self._get(self._acct(f"/cfd_tunnel/{tunnel_id}/configurations"))
        if not isinstance(result, dict):
            return []
        rules = result.get("config", {}).get("ingress", []) or []
        return [r for r in rules if "hostname" in r]

    def put_tunnel_config(self, tunnel_id: str, ingress: list[dict]) -> dict:
        """Replace the full ingress rule set for a tunnel.

        ingress is a list of dicts: {"hostname": "...", "service": "..."}.
        A catch-all rule {"service": "http_status:404"} is appended automatically
        if not already present (Cloudflare requires it as the last rule).
        """
        rules = list(ingress)
        if not rules or "hostname" in rules[-1]:
            rules.append({"service": "http_status:404"})
        body = {"config": {"ingress": rules}}
        return self._put(self._acct(f"/cfd_tunnel/{tunnel_id}/configurations"), body)

    # ------------------------------------------------------------------
    # DNS records
    # ------------------------------------------------------------------

    def list_zones(self) -> list[dict]:
        result = self._get("/zones")
        return result if isinstance(result, list) else []

    def list_dns_records(self, zone_id: str, name: str = "") -> list[dict]:
        path = f"/zones/{zone_id}/dns_records"
        if name:
            path += f"?name={name}"
        result = self._get(path)
        return result if isinstance(result, list) else []

    def create_dns_record(self, zone_id: str, name: str, tunnel_id: str) -> dict:
        """Create a CNAME record pointing a hostname at a Cloudflare tunnel."""
        return self._post(
            f"/zones/{zone_id}/dns_records",
            {
                "type": "CNAME",
                "name": name,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True,
                "ttl": 1,  # Auto
            },
        )

    def delete_dns_record(self, zone_id: str, record_id: str) -> None:
        self._delete(f"/zones/{zone_id}/dns_records/{record_id}")

    def upsert_dns_record(self, zone_id: str, name: str, tunnel_id: str) -> dict:
        """Create or replace a CNAME record for the given hostname."""
        existing = self.list_dns_records(zone_id, name=name)
        for record in existing:
            if record.get("type") == "CNAME":
                self.delete_dns_record(zone_id, record["id"])
        return self.create_dns_record(zone_id, name, tunnel_id)

    # ------------------------------------------------------------------
    # Zero Trust Access — Applications
    # ------------------------------------------------------------------

    def list_access_apps(self) -> list[dict]:
        result = self._get(self._acct("/access/apps"))
        return result if isinstance(result, list) else []

    def create_access_app(self, name: str, domains: list[str]) -> dict:
        """Create a self-hosted Zero Trust Access application."""
        # Primary domain is the first in the list; others added as destinations
        destinations = [{"type": "public", "uri": d} for d in domains]
        return self._post(
            self._acct("/access/apps"),
            {
                "name": name,
                "type": "self_hosted",
                "domain": domains[0],
                "destinations": destinations,
                "session_duration": "24h",
                "allowed_idps": [],
                "auto_redirect_to_identity": False,
            },
        )

    def update_access_app_domains(self, app_id: str, domains: list[str]) -> dict:
        """Replace the domain list on an existing Access app."""
        # CF requires a full PUT — fetch existing to preserve required fields (type, name, etc.)
        existing = self._get(self._acct(f"/access/apps/{app_id}"))
        if not isinstance(existing, dict):
            existing = {}
        destinations = [{"type": "public", "uri": d} for d in domains]
        body = {**existing, "domain": domains[0], "destinations": destinations}
        return self._put(self._acct(f"/access/apps/{app_id}"), body)

    def delete_access_app(self, app_id: str) -> None:
        self._delete(self._acct(f"/access/apps/{app_id}"))

    # ------------------------------------------------------------------
    # Zero Trust Access — Policies
    # ------------------------------------------------------------------

    def list_access_policies(self, app_id: str) -> list[dict]:
        result = self._get(self._acct(f"/access/apps/{app_id}/policies"))
        return result if isinstance(result, list) else []

    def create_ip_bypass_policy(self, app_id: str, name: str, ip_ranges: list[str]) -> dict:
        """Create a Bypass policy that allows listed IPs without authentication."""
        include = [{"ip": {"ip": cidr}} for cidr in ip_ranges]
        return self._post(
            self._acct(f"/access/apps/{app_id}/policies"),
            {
                "name": name,
                "decision": "bypass",
                "include": include,
                "exclude": [],
                "require": [],
                "precedence": 1,
            },
        )

    def update_ip_bypass_policy(
        self, app_id: str, policy_id: str, name: str, ip_ranges: list[str]
    ) -> dict:
        """Replace the IP list on an existing bypass policy."""
        include = [{"ip": {"ip": cidr}} for cidr in ip_ranges]
        return self._put(
            self._acct(f"/access/apps/{app_id}/policies/{policy_id}"),
            {
                "name": name,
                "decision": "bypass",
                "include": include,
                "exclude": [],
                "require": [],
            },
        )

    def delete_access_policy(self, app_id: str, policy_id: str) -> None:
        self._delete(self._acct(f"/access/apps/{app_id}/policies/{policy_id}"))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _random_secret() -> str:
    """Generate a 32-byte random secret encoded as base64 (required by CF tunnel create)."""
    import base64
    import secrets as _secrets
    return base64.b64encode(_secrets.token_bytes(32)).decode()
