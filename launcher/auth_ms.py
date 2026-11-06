"""Microsoft authentication for NP Launcher.

Flow: user signs in at login.live.com in their browser, lands on a page whose
URL contains ``?code=...``, pastes that URL back into the launcher, and we do
code exchange -> Xbox Live -> XSTS -> Minecraft services -> profile.

Derived from crit-launcher (https://github.com/snigglaberry/crit-launcher),
MIT License, Copyright (c) 2026 CRIT. Authentication flow, endpoints and error
handling are adapted to requests + Flask-friendly helpers.
"""

import json
import re
import time
import urllib.parse
from pathlib import Path

import requests

from .download import USER_AGENT
from .paths import data_dir

# Public first-party client id used by several open-source launchers
# (not a secret; it is a public client).
MS_CLIENT_ID = "00000000402b5328"
MS_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
MS_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
MS_AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
MS_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"

_HEADERS = {"User-Agent": USER_AGENT}

_XBOX_ERRORS = {
    2148916227: "This account is not linked to an Xbox account. Create one at minecraft.net first.",
    2148916233: "This account is banned from Xbox Live.",
    2148916235: "This account needs to finish signing up (region/age verification).",
    2148916236: "This account needs to finish signing up (region/age verification).",
    2148916237: "Adult verification required for this Xbox account.",
    2148916238: "This account is under 13 and needs to be added to a Microsoft family group.",
    2148916265: "Xbox Live multiplayer isn't available for this account.",
    2148916276: "This Xbox account is from a country/region where Xbox Live isn't available.",
}


class AuthError(Exception):
    pass


def authorize_url() -> str:
    query = urllib.parse.urlencode({
        "client_id": MS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MS_REDIRECT_URI,
        "scope": MS_SCOPE,
        "prompt": "select_account",
    })
    return f"{MS_AUTHORIZE_URL}?{query}"


def code_from_paste(text: str) -> str:
    """Extract the OAuth code from a pasted URL (or a bare code)."""
    text = (text or "").strip()
    if not text:
        raise AuthError("Paste the URL from the final page first.")
    if "code=" in text:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if query.get("code"):
            return query["code"][0]
        error = (query.get("error_description") or query.get("error") or ["unknown error"])[0]
        raise AuthError(f"Microsoft sign-in failed: {error}")
    # bare code pasted directly
    if re.fullmatch(r"[A-Za-z0-9._-]{16,}", text):
        return text
    raise AuthError("Couldn't find ?code=… in that URL. Copy the full address bar after signing in.")


def _post_json(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    resp = requests.post(url, json=payload, timeout=30, headers={**_HEADERS, **(headers or {})})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data


def _token_request(fields: dict) -> dict:
    body = urllib.parse.urlencode({
        "client_id": MS_CLIENT_ID,
        "scope": MS_SCOPE,
        "redirect_uri": MS_REDIRECT_URI,
        **fields,
    })
    resp = requests.post(
        MS_TOKEN_URL, data=body, timeout=30,
        headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or "access_token" not in data:
        desc = data.get("error_description") if isinstance(data, dict) else None
        raise AuthError(f"Microsoft rejected the sign-in{(': ' + str(desc)) if desc else ''} (HTTP {resp.status_code}).")
    return data


def _xbl_login(ms_access_token: str) -> dict:
    last = None
    for prefix in ("", "d=", "t="):
        body = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": prefix + ms_access_token,
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        status, data = _post_json(
            XBL_AUTH_URL, body, headers={"x-xbl-contract-version": "1"},
        )
        if status == 200 and data.get("Token"):
            return data
        last = status
    raise AuthError(f"Xbox Live sign-in failed (HTTP {last}).")


def _xsts_login(xbl_token: str) -> str:
    body = {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }
    status, data = _post_json(XSTS_AUTH_URL, body, headers={"x-xbl-contract-version": "1"})
    if status == 401 and isinstance(data, dict):
        code = data.get("XErr")
        raise AuthError(_XBOX_ERRORS.get(code, f"Xbox Live refused the sign-in (error {code})."))
    if status != 200 or not data.get("Token"):
        raise AuthError(f"Xbox security token request failed (HTTP {status}).")
    return data["Token"]


def _mc_login(uhs: str, xsts_token: str) -> tuple[str, float]:
    body = {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
    status, data = _post_json(MC_LOGIN_URL, body)
    if status == 429:
        raise AuthError("Too many sign-in attempts. Wait a bit and try again.")
    if status != 200 or not data.get("access_token"):
        raise AuthError(
            f"Minecraft services refused the sign-in (HTTP {status}). "
            "This usually means the account doesn't own Minecraft: Java Edition."
        )
    return data["access_token"], time.time() + float(data.get("expires_in") or 86400)


def _mc_profile(mc_token: str) -> dict:
    resp = requests.get(
        MC_PROFILE_URL, timeout=30, headers={**_HEADERS, "Authorization": f"Bearer {mc_token}"},
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not data.get("id"):
        raise AuthError(
            f"Could not read the Minecraft profile (HTTP {resp.status_code}). "
            "This account doesn't own Minecraft: Java Edition."
        )
    uuid = str(data["id"])
    m = re.match(r"^([0-9a-fA-F]{8})([0-9a-fA-F]{4})([0-9a-fA-F]{4})([0-9a-fA-F]{4})([0-9a-fA-F]{12})$", uuid)
    if m:
        uuid = "-".join(m.groups()).lower()
    return {"name": data.get("name") or "Player", "uuid": uuid.lower()}


def ms_to_minecraft(ms_access_token: str) -> tuple[str, float, dict]:
    xbl = _xbl_login(ms_access_token)
    try:
        uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AuthError("Xbox Live gave an unexpected answer.") from exc
    xsts = _xsts_login(xbl["Token"])
    token, expires = _mc_login(uhs, xsts)
    profile = _mc_profile(token)
    return token, expires, profile


def sign_in(tokens: dict) -> dict:
    ms_token = tokens.get("access_token")
    expires = time.time() + float(tokens.get("expires_in") or 3600)
    mc_token, mc_expires, profile = ms_to_minecraft(ms_token)
    account = {
        "name": profile["name"],
        "uuid": profile["uuid"],
        "type": "microsoft",
        "mc_token": mc_token,
        "mc_expires": mc_expires,
        "refresh_token": tokens.get("refresh_token", ""),
        "ms_expires": expires,
    }
    save_account(account)
    return account


def sign_in_with_paste(paste: str) -> dict:
    """Full flow from a pasted URL: code -> tokens -> Minecraft services."""
    code = code_from_paste(paste)
    tokens = _token_request({"grant_type": "authorization_code", "code": code})
    return sign_in(tokens)


def refresh(account: dict) -> dict:
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        raise AuthError("Session expired and has no refresh token — sign in again.")
    tokens = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    mc_token, mc_expires, profile = ms_to_minecraft(tokens["access_token"])
    updated = {
        **account,
        "name": profile["name"],
        "uuid": profile["uuid"],
        "mc_token": mc_token,
        "mc_expires": mc_expires,
        "refresh_token": tokens.get("refresh_token", refresh_token),
        "ms_expires": time.time() + float(tokens.get("expires_in") or 3600),
    }
    save_account(updated)
    return updated


# --------------------------------------------------------------------------
# Account storage (accounts.json): {"active": "<uuid>", "accounts": [...]}
# --------------------------------------------------------------------------

def _store_path() -> Path:
    return data_dir() / "accounts.json"


def _load() -> dict:
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        accounts = data.get("accounts", [])
        active = data.get("active") or (accounts[0]["uuid"] if accounts else None)
        return {"active": active, "accounts": [a for a in accounts if a.get("type") == "microsoft"]}
    except (OSError, json.JSONDecodeError):
        return {"active": None, "accounts": []}


def _save(store: dict) -> None:
    tmp = str(_store_path()) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2)
    Path(tmp).replace(_store_path())


def save_account(account: dict) -> dict:
    store = _load()
    accounts = [a for a in store["accounts"] if a.get("uuid") != account.get("uuid")]
    accounts.append(account)
    _save({"active": account["uuid"], "accounts": accounts})
    return account


def list_accounts() -> dict:
    store = _load()
    out = []
    for a in store["accounts"]:
        out.append({
            "name": a.get("name"),
            "uuid": a.get("uuid"),
            "type": "microsoft",
            "active": a.get("uuid") == store.get("active"),
            "expires": a.get("mc_expires", 0),
        })
    return {"active": store.get("active"), "accounts": out}


def remove_account(uuid: str) -> bool:
    store = _load()
    before = len(store["accounts"])
    store["accounts"] = [a for a in store["accounts"] if a.get("uuid") != uuid]
    if store.get("active") == uuid:
        store["active"] = store["accounts"][0]["uuid"] if store["accounts"] else None
    _save(store)
    return len(store["accounts"]) != before


def set_active(uuid: str) -> bool:
    store = _load()
    if any(a.get("uuid") == uuid for a in store["accounts"]):
        store["active"] = uuid
        _save(store)
        return True
    return False


def active_microsoft_account() -> dict | None:
    store = _load()
    for a in store["accounts"]:
        if a.get("uuid") == store.get("active"):
            return a
    return None


def active_account_for_launch() -> dict | None:
    """Return a fresh (refreshed if needed) Microsoft account for launch, or None."""
    account = active_microsoft_account()
    if not account:
        return None
    if account.get("mc_expires", 0) - time.time() < 300:  # refresh if expiring within 5 min
        account = refresh(account)
    return account
