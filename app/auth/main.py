"""Direct Microsoft Entra ID OIDC authentication for CloudFront Lambda@Edge.

Two viewer-request Lambda@Edge entry points:
  - auth_handler: default/protected cache behaviours
  - callback_handler: /auth/callback cache behaviour

SSM parameters, with NAMESPACE inferred from `auth-handler-<namespace>` or
`auth-callback-<namespace>` Lambda function name:
  /<namespace>/auth/config          Entra tenant-specific OIDC discovery URL
  /<namespace>/auth/client_id       App registration client ID
  /<namespace>/auth/client_secret   SecureString, Entra web app secret
  /<namespace>/auth/redirect        E.g. /auth/callback
  /<namespace>/auth/public_url      E.g. https://docs.example.com
  /<namespace>/auth/scope           E.g. openid profile email
  /<namespace>/auth/session_secret  SecureString, `openssl rand -hex 32`

Dependencies: boto3, requests, python-jose[cryptography].
Deployment: publish *numbered* versions in us-east-1, then associate CloudFront
viewer-request behaviours. Keep S3 private (OAC) and protect all behaviours.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import boto3
import requests
from jose import JWTError, jwt

logger = logging.getLogger(__name__)
ssm = boto3.client("ssm", region_name="us-east-1")

SESSION_COOKIE = "__Host-ATOM_SESSION"
STATE_COOKIE = "__Host-ATOM_OIDC_STATE"
NONCE_COOKIE = "__Host-ATOM_OIDC_NONCE"
PKCE_COOKIE = "__Host-ATOM_PKCE_VERIFIER"
RETURN_COOKIE = "__Host-ATOM_RETURN_PATH"

LOGIN_TTL = 300  # Five minutes to complete sign-in
SESSION_TTL = 3600  # Reauthenticate at most one hour after login
SSM_CACHE_TTL = 300  # SSM changes propagate within five minutes
OIDC_CACHE_TTL = 3600  # JWKS/config refresh hourly; retry on key rotation

_cache = {}  # Warm Lambda execution environment only


def cached(key: str, ttl: int, loader):
    now = time.monotonic()
    previous = _cache.get(key)
    if previous is not None and now < previous[0]:
        return previous[1]
    value = loader()
    _cache[key] = (now + ttl, value)
    return value


def get_param(namespace: str, name: str) -> str:
    path = f"/{namespace}/auth/{name}"
    return cached(
        f"ssm:{path}", SSM_CACHE_TTL, lambda: ssm.get_parameter(Name=path, WithDecryption=True)["Parameter"]["Value"]
    )


def get_oidc(url: str, *, force=False) -> tuple[dict, dict]:
    def fetch():
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        config = response.json()
        keys_response = requests.get(config["jwks_uri"], timeout=8)
        keys_response.raise_for_status()
        return config, keys_response.json()

    key = f"oidc:{url}"
    if force:
        _cache.pop(key, None)
    return cached(key, OIDC_CACHE_TTL, fetch)


def namespace_for(context) -> str:
    # Keep the original name extraction convention, but fail closed if it fails.
    match = re.search(r"auth-(?:handler|callback)-([a-zA-Z0-9_-]+)", context.function_name)
    if not match:
        raise ValueError("Lambda name must include auth-handler-<namespace> or auth-callback-<namespace>")
    return match.group(1)


def settings(namespace: str) -> dict:
    base_url = get_param(namespace, "public_url").rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("public_url must be an HTTPS origin, e.g. https://docs.example.com")

    path = get_param(namespace, "redirect")
    if not valid_path(path):
        raise ValueError("redirect must be an absolute local path")

    scope = get_param(namespace, "scope")
    if "openid" not in scope.split():
        raise ValueError("The scope must contain openid")

    secret_hex = get_param(namespace, "session_secret")
    secret = bytes.fromhex(secret_hex)
    if len(secret) != 32:
        raise ValueError("session_secret must be 32 bytes encoded as 64 hex characters")

    return {
        "client_id": get_param(namespace, "client_id"),
        "client_secret": get_param(namespace, "client_secret"),
        "discovery_url": get_param(namespace, "config"),
        "redirect_uri": base_url + path,
        "scope": scope,
        "secret": secret,
    }


def valid_path(path: str) -> bool:
    # Require a same-origin *relative* path; reject //host and backslashes.
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and "\r" not in path
        and "\n" not in path
        and len(path) <= 1800
    )


def destination(request: dict) -> str:
    path = request.get("uri", "/")
    query = request.get("querystring", "")
    result = path + ("?" + query if query else "")
    return result if valid_path(result) else "/"


def cookie_value(headers: dict, name: str) -> str:
    for header in headers.get("cookie", []):
        for raw_pair in header["value"].split(";"):
            key, sep, value = raw_pair.strip().partition("=")
            if sep and key == name:
                return value
    return ""


def set_cookie(name: str, value: str, max_age: int) -> dict:
    return {
        "key": "Set-Cookie",
        "value": (f"{name}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Lax"),
    }


def clear_cookie(name: str) -> dict:
    return set_cookie(name, "", 0)


def response(status: int, message: str, *, headers=None, cookies=None) -> dict:
    result = {
        "status": str(status),
        "statusDescription": message,
        "headers": {"cache-control": [{"key": "Cache-Control", "value": "no-store"}]},
    }
    if headers:
        result["headers"].update(headers)
    if cookies:
        result["headers"]["set-cookie"] = cookies
    return result


def redirect(location: str, *, cookies=None) -> dict:
    return response(
        302,
        "Found",
        headers={
            "location": [{"key": "Location", "value": location}],
        },
        cookies=cookies,
    )


def error(status: int, message: str) -> dict:
    result = response(status, "Error")
    result["headers"]["content-type"] = [{"key": "Content-Type", "value": "text/plain; charset=utf-8"}]
    result["body"] = message
    return result


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def session_cookie(claims: dict, signing_key: bytes) -> tuple[str, int]:
    now = int(time.time())
    expiry = min(int(claims["exp"]), now + SESSION_TTL)
    if expiry <= now:
        raise ValueError("ID token has expired")
    payload = {"sub": claims["sub"], "exp": expiry}
    payload_data = b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = b64url(hmac.new(signing_key, payload_data.encode(), hashlib.sha256).digest())
    return f"{payload_data}.{signature}", expiry - now


def session_is_valid(value: str, signing_key: bytes) -> bool:
    try:
        payload, signature = value.split(".", 1)
        expected = b64url(hmac.new(signing_key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        data = json.loads(b64url_decode(payload))
        return (
            isinstance(data.get("sub"), str)
            and bool(data["sub"])
            and isinstance(data.get("exp"), int)
            and data["exp"] > int(time.time())
        )
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def decode_id_token(id_token: str, cfg: dict, oidc: dict, keys: dict) -> dict:
    # Never trust algorithms from the token header.
    return jwt.decode(
        id_token,
        keys,
        algorithms=["RS256"],
        audience=cfg["client_id"],
        issuer=oidc["issuer"],
        options={"require_exp": True, "require_iat": True, "require_sub": True},
    )


def validate_id_token(id_token: str, cfg: dict, nonce: str) -> dict:
    oidc, keys = get_oidc(cfg["discovery_url"])
    try:
        claims = decode_id_token(id_token, cfg, oidc, keys)
    except JWTError:
        # Support signing-key rotation without waiting for the normal cache TTL.
        oidc, keys = get_oidc(cfg["discovery_url"], force=True)
        claims = decode_id_token(id_token, cfg, oidc, keys)
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("Invalid OIDC nonce")
    return claims


def start_login(request: dict, cfg: dict) -> dict:
    oidc, _ = get_oidc(cfg["discovery_url"])
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    query = urlencode(
        {
            "client_id": cfg["client_id"],
            "response_type": "code",
            "response_mode": "query",  # callback viewer-request sees query, not POST body
            "redirect_uri": cfg["redirect_uri"],
            "scope": cfg["scope"],
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return redirect(
        oidc["authorization_endpoint"] + "?" + query,
        cookies=[
            set_cookie(STATE_COOKIE, state, LOGIN_TTL),
            set_cookie(NONCE_COOKIE, nonce, LOGIN_TTL),
            set_cookie(PKCE_COOKIE, verifier, LOGIN_TTL),
            set_cookie(RETURN_COOKIE, b64url(destination(request).encode()), LOGIN_TTL),
        ],
    )


def auth_handler(event: dict, context) -> dict:
    try:
        request = event["Records"][0]["cf"]["request"]
        cfg = settings(namespace_for(context))
        if session_is_valid(cookie_value(request["headers"], SESSION_COOKIE), cfg["secret"]):
            return request
        return start_login(request, cfg)
    except Exception:
        logger.exception("Authentication handler failed")
        return error(503, "Authentication is temporarily unavailable")


def callback_handler(event: dict, context) -> dict:
    request = event["Records"][0]["cf"]["request"]
    try:
        cfg = settings(namespace_for(context))
        if request["uri"] != urlsplit(cfg["redirect_uri"]).path:
            return error(404, "Not found")
        query = parse_qs(request.get("querystring", ""), keep_blank_values=True)
        if "error" in query:
            return error(403, "Authentication was denied or cancelled")
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        expected_state = cookie_value(request["headers"], STATE_COOKIE)
        nonce = cookie_value(request["headers"], NONCE_COOKIE)
        verifier = cookie_value(request["headers"], PKCE_COOKIE)
        if not (code and state and expected_state and nonce and verifier):
            return error(400, "Missing login state; start sign-in again")
        if not hmac.compare_digest(state, expected_state):
            return error(400, "Login state mismatch; start sign-in again")

        oidc, _ = get_oidc(cfg["discovery_url"])
        token_response = requests.post(
            oidc["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code": code,
                "redirect_uri": cfg["redirect_uri"],
                "code_verifier": verifier,
                "scope": cfg["scope"],
            },
            timeout=10,
        )
        if not token_response.ok:
            # Never log the response body: it can contain secrets or tokens.
            logger.warning("Token endpoint rejected authorization code (HTTP %s)", token_response.status_code)
            return error(401, "Sign-in could not be completed; start again")
        id_token = token_response.json()["id_token"]
        claims = validate_id_token(id_token, cfg, nonce)

        raw_return = cookie_value(request["headers"], RETURN_COOKIE)
        try:
            return_path = b64url_decode(raw_return).decode() if raw_return else "/"
        except (ValueError, UnicodeDecodeError):
            return_path = "/"
        if not valid_path(return_path):
            return_path = "/"

        value, age = session_cookie(claims, cfg["secret"])
        return redirect(
            return_path,
            cookies=[
                set_cookie(SESSION_COOKIE, value, age),
                clear_cookie(STATE_COOKIE),
                clear_cookie(NONCE_COOKIE),
                clear_cookie(PKCE_COOKIE),
                clear_cookie(RETURN_COOKIE),
            ],
        )
    except (JWTError, ValueError, KeyError):
        logger.warning("OIDC callback validation failed")
        return error(401, "Invalid sign-in response; start again")
    except Exception:
        logger.exception("OIDC callback failed")
        return error(503, "Authentication is temporarily unavailable")
