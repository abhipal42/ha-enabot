#!/usr/bin/env python3
"""
ebo_se_cloud.py — ROLA cloud client for EBO SE auto-discovery (pure Python, stdlib + cryptography).

Logs into the Enabot ROLA cloud with YOUR email + password (exactly like the ROLA app) and returns,
for every EBO SE on your account, the TUTK values its LAN bridge needs — so you no longer hand-extract
UID / identity / token / the stream-start blob per robot. This mirrors how ebo_cloud.py already does
cloud discovery for the EBO HOME models.

What is interoperability vs. what is yours to supply
----------------------------------------------------
This file reimplements the ROLA request *scheme* (endpoints, field names, the sign + body-encrypt
algorithm). The algorithm is parameterized by the app's symmetric keys, which are NOT shipped here —
you provide your own (extracted once from your own app, like the EBO HOME keys; see docs/EBO-SE.md).
Nothing proprietary is embedded. Keys come from the environment:

  EBO_SE_BODY_KEY   AES-128-GCM key for the login body      (ROLA: bodyEncryptKeyS2)
  EBO_SE_SIGN_KEY   HMAC-SHA1 key for the request signature (ROLA: accessKeySecretS2)
  EBO_SE_UAT_KEY    AES-128-CFB key for the stream-start token (ROLA: userAuthTokenKey)
  EBO_SE_LICENSE    TUTK license key (app constant you supply)
  EBO_SE_AUTHKEY    TUTK connect authKey (app constant you supply)

Account/env:
  EBO_SE_EMAIL, EBO_SE_PASSWORD      your ROLA login
  EBO_SE_CLOUD_HOST=ebo.enabotserverintl.com   (CN: ebo.enabotserver.com)
  EBO_SE_LOGIN_REGION=GB, EBO_SE_LANG=en

Known ROLA endpoints (host above):
  POST /api/v1/s1/users/login/                     encrypted+signed body -> Set-Cookie: sessionid
  GET  /api/v1/robots/robot/                       cookie -> robot list (plaintext JSON)
  POST /api/v1/robots/robot/{id}/user_auth_token/  signed -> {data:{user_auth_token}}
"""
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import secrets
import struct
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HOST = os.environ.get("EBO_SE_CLOUD_HOST", "ebo.enabotserverintl.com")
BASE = "https://" + HOST
LANG = os.environ.get("EBO_SE_LANG", "en")
LOGIN_REGION = os.environ.get("EBO_SE_LOGIN_REGION", "GB")

BODY_KEY = os.environ.get("EBO_SE_BODY_KEY", "").encode()
SIGN_KEY = os.environ.get("EBO_SE_SIGN_KEY", "").encode()
UAT_KEY = os.environ.get("EBO_SE_UAT_KEY", "").encode()

_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class CloudError(RuntimeError):
    pass


def _nonce(n=11):
    return "".join(secrets.choice(_ALNUM) for _ in range(n))


def _q(s):
    """Percent-encode a single component the way the app's signer does (RFC3986, nothing kept safe).
    If live login fails on signature, this is the first knob to check against the app."""
    return urllib.parse.quote(str(s), safe="")


def _sign(method, path, tree):
    """HMAC-SHA1 over METHOD & path & (urlencoded, &-joined, key-sorted tree) — the ROLA 'sign'."""
    if not SIGN_KEY:
        raise CloudError("EBO_SE_SIGN_KEY not set")
    kv = "&".join("%s=%s" % (_q(k), _q(json.dumps(v, separators=(",", ":"))
                                       if isinstance(v, (dict, list)) else v))
                  for k, v in sorted(tree.items()))
    canonical = "%s&%s&%s" % (method.upper(), _q(path), _q(kv))
    return base64.b64encode(hmac.new(SIGN_KEY, canonical.encode(), hashlib.sha1).digest()).decode()


def _encrypt_body(obj):
    """AES-128-GCM(body) with a random 16-byte IV prepended, base64 — the login 'data' field."""
    if len(BODY_KEY) not in (16, 24, 32):
        raise CloudError("EBO_SE_BODY_KEY must be 16/24/32 bytes (got %d)" % len(BODY_KEY))
    iv = secrets.token_bytes(16)
    ct = AESGCM(BODY_KEY).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    return base64.b64encode(iv + ct).decode()


class RolaCloud:
    def __init__(self, email, password, device_id=None):
        self.email = email
        self.password = password
        self.device_id = device_id or _nonce(32)
        self.user_id = None
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # ---- transport ----
    def _post(self, path, tree):
        tree = dict(tree)
        tree["sign"] = _sign("POST", path, tree)
        body = json.dumps(tree, separators=(",", ":")).encode()
        req = urllib.request.Request(BASE + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8",
                                              "Accept-Language": LANG})
        with self._opener.open(req, timeout=20) as r:  # nosec B310 - https, literal BASE host
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    def _get(self, path):
        req = urllib.request.Request(BASE + path, method="GET",
                                     headers={"Accept-Language": LANG})
        with self._opener.open(req, timeout=20) as r:  # nosec B310 - https, literal BASE host
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    # ---- flow ----
    def login(self):
        tree = {
            "data": _encrypt_body({
                "app_kind": "android", "app_token": "", "device_id": self.device_id,
                "language": LANG, "account": self.email, "password": self.password,
                "login_region": LOGIN_REGION,
            }),
            "app_type": 2, "e_ver": "1.0", "nonce": _nonce(11),
            "timestamp": int(time.time() * 1000),
            "signature_version": "1.1", "signature_method": "SHA1",
        }
        resp = self._post("/api/v1/s1/users/login/", tree)
        # the session is the sessionid cookie (set in the response headers). The body is encrypted,
        # but we don't need it — a set cookie means success. user_id comes from the robot list.
        if not any(c.name == "sessionid" for c in self.jar):
            code = resp.get("code")
            raise CloudError("login failed (no session cookie; code=%s)" % code)
        return True

    def robots(self):
        """Return the raw robot list (plaintext JSON 'data' array)."""
        resp = self._get("/api/v1/robots/robot/")
        data = resp.get("data")
        robots = data if isinstance(data, list) else (data or {}).get("robots") or []
        if robots and self.user_id is None:
            # the list carries the robot_user_id / user id used for user_auth_token
            self.user_id = (robots[0].get("robot_user_id")
                            or robots[0].get("user_id") or resp.get("user_id"))
        return robots

    def user_auth_token(self, robot_id):
        resp = self._post("/api/v1/robots/robot/%s/user_auth_token/" % robot_id,
                          {"user_id": self.user_id, "device_id": robot_id})
        d = resp.get("data") or {}
        tok = d.get("user_auth_token")
        if not tok:
            raise CloudError("no user_auth_token for robot %s (code=%s)" % (robot_id, resp.get("code")))
        return tok


def build_ioctl9930(user_id, user_auth_token):
    """Generate the EBO SE stream-start payload (avSendIOCtrl 0x9930), fresh per connect — so you
    never capture ioctl9930.bin. Layout (from the app): 1016-byte buffer:
        [0:4]    little-endian length of (iv + ciphertext)
        [4: ]    ASCII str(user_id)
        [25: ]   iv(16) + AES-128-CFB(user_auth_token, iv, UAT_KEY)
        [1012:4] 01 00 00 00
    """
    if len(UAT_KEY) not in (16, 24, 32):
        raise CloudError("EBO_SE_UAT_KEY must be 16/24/32 bytes (got %d)" % len(UAT_KEY))
    iv = secrets.token_bytes(16)
    enc = Cipher(algorithms.AES(UAT_KEY), modes.CFB(iv)).encryptor()
    ct = enc.update(user_auth_token.encode()) + enc.finalize()
    blob = iv + ct
    buf = bytearray(1016)
    struct.pack_into("<I", buf, 0, len(blob))
    uid = str(user_id).encode()
    buf[4:4 + len(uid)] = uid
    buf[25:25 + len(blob)] = blob
    buf[1012:1016] = bytes([1, 0, 0, 0])
    return bytes(buf)


def discover(email, password):
    """High-level: log in and return a normalized list of EBO SE robots with everything the native
    bridge needs. Raises CloudError on failure. Keys/credentials come from the environment."""
    c = RolaCloud(email, password)
    c.login()
    out = []
    for r in c.robots():
        rid = r.get("robot_id") or r.get("id")
        uid = r.get("tutk_uid")
        identity = r.get("tutk_token_identity")
        token = r.get("tutk_token_value")
        if not (uid and identity and token and rid is not None):
            continue  # not a TUTK/SE robot (e.g. a cloud Air 2 on the same account)
        try:
            uat = c.user_auth_token(rid)
            ioctl = build_ioctl9930(c.user_id, uat)
        except CloudError:
            ioctl = None
        out.append({
            "robot_id": rid,
            "name": r.get("robot_name") or ("EBO SE %s" % rid),
            "uid": uid,
            "identity": identity,
            "token": token,
            "sn": r.get("machine_code") or r.get("factory_machine_code") or "",
            "model": r.get("model_marketing_name") or "EBO SE",
            "ioctl9930": ioctl,   # bytes, or None if the token call failed
        })
    return out


def emit(email, password):
    """Machine-readable discovery for run.sh: one TSV line per SE robot on STDOUT:
        SE<TAB>robot_id<TAB>name<TAB>uid<TAB>identity<TAB>token<TAB>sn<TAB>model<TAB>ioctl9930_b64
    (ioctl9930_b64 is empty if the user_auth_token call failed — the bridge then falls back to the
    ioctl9930.bin file in se_dir, if present.) Diagnostics go to stderr.
    """
    import sys
    for r in discover(email, password):
        ioctl_b64 = base64.b64encode(r["ioctl9930"]).decode() if r["ioctl9930"] else ""
        line = "\t".join(str(x) for x in ("SE", r["robot_id"], r["name"], r["uid"],
                                          r["identity"], r["token"], r["sn"], r["model"], ioctl_b64))
        sys.stdout.write(line + "\n")


if __name__ == "__main__":
    import sys
    email = os.environ.get("EBO_SE_EMAIL", "")
    password = os.environ.get("EBO_SE_PASSWORD", "")
    if not email or not password:
        sys.stderr.write("set EBO_SE_EMAIL / EBO_SE_PASSWORD (and the key env vars)\n")
        sys.exit(2)
    try:
        if "--emit" in sys.argv:
            emit(email, password)
        else:
            for r in discover(email, password):
                sys.stderr.write("SE robot_id=%s name=%s uid=%s ioctl=%s\n"
                                 % (r["robot_id"], r["name"], r["uid"],
                                    "yes" if r["ioctl9930"] else "no"))
    except CloudError as e:
        sys.stderr.write("discovery failed: %s\n" % e)
        sys.exit(1)
