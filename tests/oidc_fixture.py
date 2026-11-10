"""IdP OIDC finto ma reale: server HTTP locale con discovery, JWKS (RSA),
authorize (senza UI: sceglie l'utente dal parametro login_hint) e token
con verifica PKCE, refresh e revoca. Serve un server vero perché PyJWT
scarica le chiavi via HTTP."""
import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

CLIENT_ID = "wikibridge-test"


class FakeIdP:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.users = {
            "mario": {"sub": "sub-mario", "oid": "oid-mario", "groups": ["g-legal"],
                      "email": "mario@example.com", "name": "Mario Rossi"},
            "anna": {"sub": "sub-anna", "oid": "oid-anna", "groups": ["g-admins"],
                     "email": "anna@example.com", "name": "Anna Admin"},
        }
        self.disabled: set[str] = set()
        self.codes: dict[str, dict] = {}
        self.refresh: dict[str, str] = {}
        self.nonce_override = None
        app = Flask("fake-idp")
        app.add_url_rule("/.well-known/openid-configuration", view_func=self.meta)
        app.add_url_rule("/jwks", view_func=self.jwks)
        app.add_url_rule("/authorize", view_func=self.authorize)
        app.add_url_rule("/token", view_func=self.token, methods=["POST"])
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def id_token(self, user: str, nonce=None, **override) -> str:
        u = self.users[user]
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": CLIENT_ID, "sub": u["sub"],
                  "oid": u["oid"], "preferred_username": user, "email": u["email"],
                  "name": u["name"], "groups": list(u["groups"]),
                  "iat": now, "exp": now + 3600}
        if nonce:
            claims["nonce"] = nonce
        claims.update(override)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    # -- endpoint ---------------------------------------------------------------

    def meta(self):
        return jsonify({"issuer": self.issuer,
                        "authorization_endpoint": f"{self.issuer}/authorize",
                        "token_endpoint": f"{self.issuer}/token",
                        "jwks_uri": f"{self.issuer}/jwks"})

    def jwks(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="k1", use="sig", alg="RS256")
        return jsonify({"keys": [jwk]})

    def authorize(self):
        a = request.args
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"user": a["login_hint"], "nonce": a["nonce"],
                            "challenge": a["code_challenge"],
                            "redirect_uri": a["redirect_uri"]}
        return redirect(f"{a['redirect_uri']}?{urlencode({'code': code, 'state': a['state']})}")

    def token(self):
        f = request.form
        if f.get("client_id") != CLIENT_ID:
            return jsonify({"error": "invalid_client"}), 401
        if f["grant_type"] == "authorization_code":
            c = self.codes.pop(f["code"], None)
            digest = base64.urlsafe_b64encode(hashlib.sha256(
                f.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if not c or digest != c["challenge"] or f["redirect_uri"] != c["redirect_uri"]:
                return jsonify({"error": "invalid_grant"}), 400
            user, nonce = c["user"], self.nonce_override or c["nonce"]
        else:
            user = self.refresh.get(f.get("refresh_token", ""))
            nonce = None
            if not user or user in self.disabled:
                return jsonify({"error": "invalid_grant"}), 400
        rt = secrets.token_urlsafe(16)
        self.refresh[rt] = user
        return jsonify({"id_token": self.id_token(user, nonce), "refresh_token": rt,
                        "access_token": "x", "token_type": "Bearer"})


def configure(cfg, idp: FakeIdP):
    cfg.identity_provider = "oidc"
    cfg.oidc_issuer = idp.issuer
    cfg.oidc_client_id = CLIENT_ID
    cfg.oidc_allow_insecure = True
    cfg.oidc_user_id_claim = "oid"
    cfg.identity_role_map = {"g-legal": "team-legal", "G-ADMINS": "admin"}
    return cfg
