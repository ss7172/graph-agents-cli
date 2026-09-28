# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A fake OIDC issuer for tests of agents calling agents: a JWKS and RFC 8693 token exchange.

It signs RS256 tokens with one key, serves the key set at `/jwks` and exchanges
tokens at `/token` (`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`)
for the clients it knows (HTTP Basic client authentication, or the client id and
secret in the form: `client_secret_post`). An exchanged token
keeps the subject token's `sub`, names the calling client in `act` (the subject
token's own `act` nested inside it, RFC 8693 section 4.1), has the requested
`audience`, `azp` set to the client and `expires_in` of at most 300 s. It refuses
an audience the client may not act for (`may_act`), a service token (one whose
`sub` is its own client) and a token it did not issue. Every exchange request is
recorded (`requests`: the form, secrets included, and how the client
authenticated), and a test can make it misbehave: `hold` (a `threading.Event`
cleared: every `/token` request waits until it is set, a hung issuer), `answers`
(queued `(status, body)` answers returned instead of an exchange),
`expires_in` (the lifetime it grants; None leaves it out of the answer) and
`act` (False: exchanged tokens name no actor, only `azp`, as some issuers do).
Loopback only, on a free port; nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote_plus

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
EXCHANGED_TTL_S = 300


class FakeIssuer:
    """The issuer: `login` mints a person's token, `/token` exchanges it for an agent."""

    def __init__(self, clients: dict[str, tuple[str, set[str]]]) -> None:
        """`clients`: client id -> (secret, the audiences it may exchange tokens for)."""
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex[:8]
        self.clients = clients
        self.exchanges: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.hold = threading.Event()
        self.hold.set()
        self.answers: list[tuple[int, dict[str, Any]]] = []
        self.expires_in: int | None = EXCHANGED_TTL_S
        self.act = True
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/jwks":
                    self._answer(404, {"error": "not_found"})
                    return
                self._answer(200, issuer.jwks())

            def do_POST(self) -> None:
                if self.path != "/token":
                    self._answer(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("content-length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                authorization = self.headers.get("authorization") or ""
                issuer.requests.append({"form": form, "authorization": authorization})
                issuer.hold.wait(timeout=120)
                if issuer.answers:
                    status, body = issuer.answers.pop(0)
                else:
                    status, body = issuer.exchange(authorization, form)
                self._answer(status, body)

            def _answer(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.issuer = f"{self.url}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def jwks_url(self) -> str:
        return f"{self.url}/jwks"

    @property
    def token_url(self) -> str:
        return f"{self.url}/token"

    def close(self) -> None:
        self.hold.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def jwks(self) -> dict[str, Any]:
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def login(
        self,
        sub: str,
        *,
        audience: str,
        client: str = "web",
        roles: tuple[str, ...] = (),
        ttl_s: int = 3600,
    ) -> str:
        """A person's own token for `audience`, as a sign-in client obtains it."""
        now = int(time.time())
        return self.sign(
            {
                "iss": self.issuer,
                "sub": sub,
                "aud": audience,
                "azp": client,
                "roles": list(roles),
                "iat": now,
                "exp": now + ttl_s,
                "jti": uuid.uuid4().hex,
            }
        )

    def exchange(self, authorization: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        """RFC 8693 token exchange: `(status, body)`."""
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "basic":
            try:
                pair = base64.b64decode(credentials).decode()
            except Exception:
                pair = ""
            # RFC 6749 section 2.3.1: each part form-encoded, then joined and base64-encoded.
            client, _, secret = (unquote_plus(part) for part in pair.partition(":"))
        elif not authorization and "client_id" in form:
            client, secret = form.get("client_id") or "", form.get("client_secret") or ""
        else:
            client, secret = "", ""
        known = self.clients.get(client)
        if known is None or known[0] != secret:
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") != TOKEN_EXCHANGE:
            return 400, {"error": "unsupported_grant_type"}
        audience = form.get("audience") or ""
        if audience not in known[1]:
            return 400, {"error": "invalid_target"}
        try:
            subject = jwt.decode(
                form.get("subject_token") or "",
                self.key.public_key(),
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"verify_aud": False},
            )
        except jwt.PyJWTError:
            return 400, {"error": "invalid_grant"}
        if subject.get("sub") == subject.get("azp"):
            return 400, {"error": "invalid_grant"}  # no exchange of a service's own token
        actor: dict[str, Any] = {"sub": client}
        if isinstance(subject.get("act"), dict):
            actor["act"] = subject["act"]
        now = int(time.time())
        exchanged = {
            "iss": self.issuer,
            "sub": subject["sub"],
            "aud": audience,
            "azp": client,
            "act": actor,
            "roles": subject.get("roles") or [],
            "iat": now,
            "exp": min(now + (self.expires_in or 60), int(subject.get("exp") or now)),
            "jti": uuid.uuid4().hex,
        }
        if not self.act:
            del exchanged["act"]
        for claim in ("scope", "resource"):
            if form.get(claim):
                exchanged[claim] = form[claim]
        self.exchanges.append({"client": client, "audience": audience, "sub": subject["sub"]})
        answer: dict[str, Any] = {
            "access_token": self.sign(exchanged),
            "issued_token_type": ACCESS_TOKEN_TYPE,
            "token_type": "Bearer",
        }
        if self.expires_in is not None:
            answer["expires_in"] = exchanged["exp"] - now
        return 200, answer
