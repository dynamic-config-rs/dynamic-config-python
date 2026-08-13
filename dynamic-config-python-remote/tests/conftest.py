"""Scripted stores, and real ones when Docker is about.

The scripted servers are the important ones. Every credential in this package
may be a callable, and the promise a callable makes is that the store uses
what it *last* returned — which is a promise about the bytes on the wire, so
the only honest test of it is a server that records the bytes and is asked
twice. A container proves the protocol; only this proves the rotation.

Four of the seven stores speak HTTP and are scripted here: Vault, Consul,
Firestore and S3. The other three speak binary protocols — etcd's gRPC,
NATS', Redis' RESP — where a scripted server would be a protocol
implementation rather than a fixture, so those prove their rotation against a
real server instead, by rotating from a credential it *refuses* to one it
accepts.

The same scripted Vault serves the TLS tests, over `https` and behind a
throwaway certificate authority this file mints with `openssl`. A claim about
TLS is a claim about a handshake, so the only honest test of one is a server
that will not complete it — which is what `serving_vault_tls` is for.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import pytest


class VaultLog:
    """What the scripted Vault was asked, in order."""

    def __init__(self) -> None:
        self.tokens: list[Optional[str]] = []
        self.logins: list[dict[str, Any]] = []
        self.address = ""
        #: Tokens this server answers 403 to. A set rather than one magic
        #: string, so a test can choose a token whose *value* is what it
        #: wants to see removed from the refusal.
        self.refuse = {"refused"}

    def secret_for(self, token: Optional[str]) -> dict[str, Any]:
        """The document a given token sees.

        Keyed by the credential on purpose: an assertion that the *document*
        changed is an assertion that the new credential reached the server,
        and it cannot be satisfied by a client that cached the old answer.
        """
        return {"host": f"host-for-{token}", "port": 5432}


class _Handler(BaseHTTPRequestHandler):
    """Enough of Vault's KV v2 and login API to be read by the real client."""

    log: VaultLog

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        token = self.headers.get("X-Vault-Token")
        self.log.tokens.append(token)

        # A token the scripted server does not like, so the auth path has
        # something to be refused by. A token *bought* with a refused
        # credential is refused too — `issued-for-refused` contains it —
        # which is what makes the AppRole path testable.
        if token is not None and any(bad in token for bad in self.log.refuse):
            self._answer(403, {"errors": ["permission denied"]})
            return

        self._answer(
            200,
            {
                "data": {
                    "data": self.log.secret_for(token),
                    "metadata": {"version": len(self.log.tokens)},
                }
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.log.logins.append(body)

        # The token a login yields names the credential it was bought with,
        # so a read afterwards shows which login it came from.
        issued = f"issued-for-{body.get('secret_id') or body.get('password') or 'anon'}"

        self._answer(
            200,
            {
                "auth": {
                    "client_token": issued,
                    "lease_duration": 3600,
                    "renewable": False,
                }
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silent: pytest's captured output is not a place for an access log.

        The signature is `BaseHTTPRequestHandler`'s, name for name — an
        override that narrowed it to `*args` reads fine and fails pyright,
        which is what a user's IntelliSense runs.
        """


@pytest.fixture
def vault() -> Iterator[VaultLog]:
    """A scripted Vault on a loopback port, and the log of what it was asked."""
    log = VaultLog()

    with _serving(_Handler, log) as address:
        log.address = address

        yield log


class ConsulLog:
    """What the scripted Consul agent was asked, in order."""

    def __init__(self) -> None:
        self.tokens: list[Optional[str]] = []
        self.logins: list[dict[str, Any]] = []
        self.address = ""
        #: ACL tokens this agent answers 403 to.
        self.refuse = {"refused"}

    def document_for(self, token: Optional[str]) -> str:
        """The configuration document a given ACL token sees.

        Keyed by the credential, like the Vault fixture's and for the same
        reason: an assertion that the *document* changed cannot be satisfied
        by a client that cached the old answer.
        """
        return json.dumps({"db": {"host": f"host-for-{token}", "port": 5432}})


class _ConsulHandler(BaseHTTPRequestHandler):
    """Enough of Consul's KV and login API to be read by the real client."""

    log: ConsulLog

    def _answer(self, status: int, body: Any) -> None:
        encoded = json.dumps(body).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        token = self.headers.get("X-Consul-Token")
        self.log.tokens.append(token)

        # An ACL token bought with a refused bearer is refused too — the
        # issued token's name contains the bearer's — which is what makes the
        # login path testable.
        if token is not None and any(bad in token for bad in self.log.refuse):
            self._answer(403, {"errors": ["ACL not found"]})
            return

        key = self.path.split("?", 1)[0].removeprefix("/v1/kv/")
        value = base64.b64encode(self.log.document_for(token).encode()).decode()

        self._answer(200, [{"Key": key, "Value": value, "CreateIndex": 1}])

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.log.logins.append(body)

        # The ACL token a login yields names the bearer it was bought with,
        # so a read afterwards shows which login it came from.
        self._answer(
            200,
            {
                "SecretID": f"issued-for-{body.get('BearerToken')}",
                "ExpirationTTL": 3600 * 1_000_000_000,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silent, for the reason the Vault handler's override gives."""


@pytest.fixture
def consul() -> Iterator[ConsulLog]:
    """A scripted Consul agent, and the log of what it was asked."""
    log = ConsulLog()

    with _serving(_ConsulHandler, log) as address:
        log.address = address

        yield log


class FirestoreLog:
    """What the scripted Firestore was asked, in order."""

    def __init__(self) -> None:
        self.tokens: list[Optional[str]] = []
        #: One entry per trip to the metadata server. The list a caller asserts
        #: on to see that a token was minted once rather than per fetch.
        self.mints: list[str] = []
        self.address = ""
        #: Access tokens this service answers 401 to.
        self.refuse = {"refused"}


class _FirestoreHandler(BaseHTTPRequestHandler):
    """Enough of Firestore's REST API, and of a metadata server, to be read."""

    log: FirestoreLog

    def _answer(self, status: int, body: Any) -> None:
        encoded = json.dumps(body).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/token":
            self._mint()
            return

        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        self.log.tokens.append(bearer or None)

        if any(bad in bearer for bad in self.log.refuse):
            # 401, not 403: Firestore's word for a token it will not take, and
            # the one the Rust crate trades for a fresh one where it can.
            self._answer(401, {"error": {"status": "UNAUTHENTICATED"}})
            return

        self._answer(
            200,
            {
                "fields": {
                    "host": {"stringValue": f"host-for-{bearer}"},
                    "port": {"integerValue": "5432"},
                },
                "updateTime": f"2026-01-01T00:00:{len(self.log.tokens):02d}Z",
            },
        )

    def _mint(self) -> None:
        """The metadata server's half.

        The `Metadata-Flavor` header is checked because the real one checks
        it: that header is what stops a confused browser or a proxied request
        from reading a workload's credentials, and a fixture that ignored it
        would let a client forget to send it.
        """
        if self.headers.get("Metadata-Flavor") != "Google":
            self._answer(403, {"error": "the Metadata-Flavor header is missing"})
            return

        minted = f"minted-{len(self.log.mints)}"
        self.log.mints.append(minted)

        self._answer(200, {"access_token": minted, "expires_in": 3600})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silent, for the reason the Vault handler's override gives."""


@pytest.fixture
def firestore() -> Iterator[FirestoreLog]:
    """A scripted Firestore and metadata server, and the log of both."""
    log = FirestoreLog()

    with _serving(_FirestoreHandler, log) as address:
        log.address = address

        yield log


class S3Log:
    """What the scripted S3 was asked, in order."""

    def __init__(self) -> None:
        #: The access key id out of each request's SigV4 `Authorization`
        #: header. The only place S3 shows which credential signed a request,
        #: and therefore the only place a rotation can be proven.
        self.signers: list[Optional[str]] = []
        self.address = ""
        #: Access key ids this store answers `InvalidAccessKeyId` to.
        self.refuse = {"refused"}

    def document_for(self, signer: Optional[str]) -> str:
        """The configuration document a given key pair sees."""
        return json.dumps({"db": {"host": f"host-for-{signer}", "port": 5432}})


class _S3Handler(BaseHTTPRequestHandler):
    """Enough of S3's GET to be read by the real AWS SDK.

    Path-style addressing only, which is what the Rust crate forces on: the
    virtual-host form needs DNS entries only AWS has.
    """

    log: S3Log

    def do_GET(self) -> None:
        signer = _access_key_id(self.headers.get("Authorization"))
        self.log.signers.append(signer)

        if signer is not None and any(bad in signer for bad in self.log.refuse):
            # The SDK sorts a refusal on the error *code*, not on the 403 that
            # carries it, so the body is what makes this an `AuthError`.
            self._refuse()
            return

        encoded = self.log.document_for(signer).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _refuse(self) -> None:
        body = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<Error><Code>InvalidAccessKeyId</Code>"
            b"<Message>The access key id does not exist</Message></Error>"
        )

        self.send_response(403)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silent, for the reason the Vault handler's override gives."""


def _access_key_id(authorization: Optional[str]) -> Optional[str]:
    """The access key id out of a SigV4 `Authorization` header.

    `AWS4-HMAC-SHA256 Credential=AKID/20260101/us-east-1/s3/aws4_request, ...`
    — the key id is the first element of the credential scope, and it is the
    only part of a signed request that names the credential.
    """
    if authorization is None:
        return None

    for part in authorization.split(" "):
        if part.startswith("Credential="):
            return part.removeprefix("Credential=").split("/", 1)[0]

    return None


@pytest.fixture
def s3() -> Iterator[S3Log]:
    """A scripted S3, and the log of which key signed each request."""
    log = S3Log()

    with _serving(_S3Handler, log) as address:
        log.address = address

        yield log


class Repository:
    """A git repository somebody could push configuration to.

    Built with the `git` binary rather than with a library, for the reason the
    certificates below are built with `openssl`: this package has no
    dependencies and a fixture is a poor place to acquire one. The store under
    test reads it with `gix`, so nothing here is the thing being tested.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

        self._git("init", "--initial-branch=main")
        # Committing needs an identity, and the host's must not be borrowed:
        # a run on a machine with no `user.email` would fail for a reason that
        # has nothing to do with this package.
        self._git("config", "user.email", "tests@dynamic-config.invalid")
        self._git("config", "user.name", "dynamic-config tests")

    def commit(self, files: dict[str, str], message: str = "a deployment") -> str:
        """Writes every file and commits them **as one commit**, answering its id.

        One commit, because that is the shape a multi-file source exists to
        read: files that change together arrive together.
        """
        for name, contents in files.items():
            written = self.path / name
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(contents)

        self._git("add", "--all")
        self._git("commit", "--quiet", "--message", message)

        return self._git("rev-parse", "HEAD").strip()

    def tag(self, name: str) -> None:
        """Tags the current commit."""
        self._git("tag", name)

    def url(self) -> str:
        """The `file://` url a source would name."""
        return f"file://{self.path}"

    def _git(self, *arguments: str) -> str:
        finished = subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            capture_output=True,
            check=True,
            text=True,
            timeout=60,
        )

        return finished.stdout


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    """A git repository in this test's own temporary directory."""
    if shutil.which("git") is None:
        pytest.skip("the git tests need the git binary to build a repository")

    return Repository(tmp_path / "config")


class GitLog:
    """What the scripted git host was asked, in order."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        #: Every password the client presented, in order. An unauthenticated
        #: first request is not one: that is how the client learns a challenge
        #: exists.
        self.presented: list[str] = []
        #: The password that works. Changing it is how a test expires a token.
        self.accepted = "right-token"
        #: Requests that asked for objects, as opposed to listing refs. What
        #: makes "an unchanged ref transfers nothing" an assertion.
        self.transfers = 0
        self.url = ""


class _GitHandler(BaseHTTPRequestHandler):
    """Smart HTTP, in the two requests it actually consists of.

    The git half is delegated to `git upload-pack --stateless-rpc` over a real
    repository, so the *protocol* is the one GitHub serves and only the parts a
    test needs to control are scripted: which token is accepted, and what was
    asked for.
    """

    log: GitLog
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if not self._authorized():
            return

        body = _pkt_line("# service=git-upload-pack\n") + b"0000"
        body += self._upload_pack(advertise=True, payload=b"")

        self._answer(200, "application/x-git-upload-pack-advertisement", body)

    def do_POST(self) -> None:
        if not self._authorized():
            return

        payload = self._body()

        # A fetch asks for objects; `ls-refs` and the capability round do not.
        if b"command=fetch" in payload or b"want " in payload:
            self.log.transfers += 1

        self._answer(
            200,
            "application/x-git-upload-pack-result",
            self._upload_pack(advertise=False, payload=payload),
        )

    def _authorized(self) -> bool:
        """Whether this request presented the password the host accepts.

        Checked before anything else: a host that leaks the refs to an
        unauthenticated client is not testing anything.
        """
        presented = None
        offered = self.headers.get("Authorization", "")

        if offered.startswith("Basic "):
            decoded = base64.b64decode(offered.removeprefix("Basic ")).decode()
            presented = decoded.split(":", 1)[1]
            self.log.presented.append(presented)

        if presented == self.log.accepted:
            return True

        self._answer(401, "text/plain", b"bad credentials", authenticate=True)

        return False

    def _upload_pack(self, *, advertise: bool, payload: bytes) -> bytes:
        """Runs the real `git upload-pack`, which is what makes this a git host."""
        # The host's own git configuration is not this test's, and a `[url]`
        # rewrite or a credential helper in it would be a failure nobody could
        # attribute to this file.
        environment = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
        )
        protocol = self.headers.get("Git-Protocol")

        if protocol:
            environment["GIT_PROTOCOL"] = protocol

        arguments = ["git", "upload-pack", "--stateless-rpc"]

        if advertise:
            arguments.append("--advertise-refs")

        finished = subprocess.run(
            [*arguments, str(self.log.repository.path)],
            input=payload,
            capture_output=True,
            check=False,
            env=environment,
            timeout=120,
        )

        return finished.stdout

    def _body(self) -> bytes:
        """The request body, however the client chose to frame it.

        `gix`'s own transport streams a chunked request and the transport this
        crate's TLS path builds sends a bounded one, so both framings arrive
        here depending on whether a `TlsConfig` was configured.
        """
        length = self.headers.get("Content-Length")

        if length is not None:
            return self.rfile.read(int(length))

        if (self.headers.get("Transfer-Encoding") or "").lower() != "chunked":
            return b""

        body = b""

        while True:
            size = int(self.rfile.readline().strip() or b"0", 16)

            if size == 0:
                self.rfile.readline()

                return body

            body += self.rfile.read(size)
            self.rfile.read(2)

    def _answer(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        authenticate: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")

        if authenticate:
            self.send_header("WWW-Authenticate", 'Basic realm="git"')

        # One request per connection, so there is no keep-alive state machine
        # to get wrong.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silent, for the reason the Vault handler's override gives."""


def _pkt_line(text: str) -> bytes:
    """One git protocol packet: its length in hex, then its bytes."""
    return f"{len(text) + 4:04x}{text}".encode()


@contextmanager
def serving_git(certificates: Certificates, repository: Repository) -> Iterator[GitLog]:
    """The scripted git host, over TLS, behind the throwaway authority.

    **Over TLS rather than plain HTTP, and not for symmetry.** `gix` refuses to
    put a credential on an unencrypted connection unless it is compiled with
    `gix-transport/http-client-insecure-credentials`, which this wheel is not
    and should not be — so a scripted host over `http://` would never be shown
    a token at all, and the rotation this proves could not be observed.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates.server_certificate, certificates.server_key)

    log = GitLog(repository)

    with _serving(_GitHandler, log, context) as address:
        log.url = f"{address}/config.git"

        yield log


@contextmanager
def _serving(
    handler: type[BaseHTTPRequestHandler],
    log: Any,
    tls: Optional[ssl.SSLContext] = None,
) -> Iterator[str]:
    """Runs `handler` on a loopback port for the length of a test.

    One copy, because four fixtures wanted the same eight lines and the part
    worth reading in each of them is the protocol rather than the socket.

    With `tls`, the same handler behind a real TLS handshake — which is the
    only thing that can prove a certificate authority reached the client,
    because a handshake either completes or does not.
    """
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), type("Handler", (handler,), {"log": log})
    )
    # Read before the socket is wrapped: the port is what a caller needs and
    # the wrapper is what a caller must not have to know about.
    port = server.server_address[1]

    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ── A throwaway certificate authority, for the TLS tests ───────────────


class Certificates:
    """A private certificate authority, and the two certificates it signed.

    Minted per session with the `openssl` binary rather than with a library,
    because this package has no dependencies and a test fixture is a poor
    place to acquire one. Two days of validity: long enough for any run, short
    enough that material escaping the temporary directory is worthless.

    The server certificate carries `subjectAltName=IP:127.0.0.1` because
    rustls — which every client in this wheel reaches TLS through — reads the
    SAN and ignores the common name entirely.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.ca = directory / "ca.pem"
        self.server_certificate = directory / "server.crt"
        self.server_key = directory / "server.key"
        self.client_certificate = directory / "client.crt"
        self.client_key = directory / "client.key"

        self._authority()
        self._signed(
            "server",
            "/CN=127.0.0.1",
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=IP:127.0.0.1\n",
        )
        self._signed(
            "client",
            "/CN=dynamic-config-test-client",
            "keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n",
        )

    def _authority(self) -> None:
        """The self-signed root everything else here chains to."""
        _openssl(
            "req",
            "-x509",
            *_KEY,
            "-keyout",
            str(self.directory / "ca.key"),
            "-out",
            str(self.ca),
            "-days",
            "2",
            "-subj",
            "/CN=dynamic-config-test-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )

    def _signed(self, name: str, subject: str, extensions: str) -> None:
        """One leaf certificate, signed by the authority."""
        request = self.directory / f"{name}.csr"
        extension_file = self.directory / f"{name}.ext"
        extension_file.write_text(f"basicConstraints=critical,CA:FALSE\n{extensions}")

        _openssl(
            "req",
            *_KEY,
            "-keyout",
            str(self.directory / f"{name}.key"),
            "-out",
            str(request),
            "-subj",
            subject,
        )
        _openssl(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(self.ca),
            "-CAkey",
            str(self.directory / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(self.directory / f"{name}.crt"),
            "-days",
            "2",
            "-extfile",
            str(extension_file),
        )


#: P-256 rather than RSA: every TLS stack in this wheel accepts it, and it is
#: generated in milliseconds where a 2048-bit RSA key is seconds.
_KEY = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")


def _openssl(*arguments: str) -> None:
    """One `openssl` invocation, with its noise on stderr swallowed."""
    subprocess.run(
        ["openssl", *arguments],
        capture_output=True,
        check=True,
        timeout=60,
    )


@pytest.fixture(scope="session")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Certificates:
    """A throwaway certificate authority, minted once for the session."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to mint the TLS fixtures' certificates")

    return Certificates(tmp_path_factory.mktemp("certificates"))


@contextmanager
def serving_vault_tls(
    certificates: Certificates,
    *,
    require_client_certificate: bool = False,
) -> Iterator[VaultLog]:
    """The scripted Vault, over TLS, behind the throwaway authority.

    With `require_client_certificate`, the server refuses to complete a
    handshake with a client that presents none — which is the only way to
    prove that a client certificate was actually presented rather than
    accepted and dropped.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates.server_certificate, certificates.server_key)

    if require_client_certificate:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(certificates.ca)

    log = VaultLog()

    with _serving(_Handler, log, context) as address:
        log.address = address

        yield log


def _docker() -> bool:
    """Whether Docker is here and answering."""
    if shutil.which("docker") is None:
        return False

    return (
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


@pytest.fixture(scope="session")
def docker() -> bool:
    """Whether the container tests can run at all."""
    return _docker()


def container(
    image: str,
    *args: str,
    port: int,
    env: Optional[list[str]] = None,
) -> tuple[str, int]:
    """Starts a container, and answers its id and the host port `port` is on."""
    settings = [flag for value in env or [] for flag in ("-e", value)]
    started = subprocess.run(
        ["docker", "run", "-d", "-P", "--rm", *settings, image, *args],
        capture_output=True,
        check=True,
        text=True,
        timeout=300,
    )
    identifier = started.stdout.strip()

    mapped = subprocess.run(
        ["docker", "port", identifier, str(port)],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )

    return identifier, int(mapped.stdout.strip().rsplit(":", 1)[1])


def stop(identifier: str) -> None:
    """Stops a container started by `container`."""
    subprocess.run(
        ["docker", "rm", "-f", identifier],
        capture_output=True,
        check=False,
        timeout=120,
    )
