"""A git repository, as a configuration source."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Optional, Union

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._tls import PathArgument, TlsConfig, unwrapped

#: The user name GitHub, GitLab and Azure DevOps all accept beside a token.
#: HTTP basic authentication has two halves and a token is one value, so every
#: host picks a filler for the other. Repeated from the Rust crate rather than
#: imported, because there is nothing to import it from on this side — the same
#: arrangement `SERVICE_ACCOUNT_TOKEN` has.
TOKEN_USERNAME = "x-access-token"

#: What a path may be given as. A repository path is `/`-separated and relative
#: to the repository root — it is a name in a git tree rather than a name on
#: this machine — so it is a `str`, a list of them, or a :class:`GitKeys`.
PathsArgument = Union[str, Sequence[str], "GitKeys"]


class GitKeys:
    """What a git source reads: one file, several named ones, or a directory.

    Mirrors ``dynamic_config_git::Keys``. A bare string is
    :meth:`one` and a list of strings is :meth:`several`, so both of those have
    a spelling that needs no import; :meth:`prefix` is the one that does,
    because a directory and a file are both strings and only the caller knows
    which was meant::

        Git(url, "services/api/config.yaml")                    # one file
        Git(url, ["services/api/base.yaml", "…/local.yaml"])    # merged, later wins
        Git(url, GitKeys.prefix("services/api"), format="yaml")  # a directory

    It is ``GitKeys`` rather than ``Keys`` because this package is eight stores
    in one namespace and only one of them has this: the other seven read a
    single key, and a bare ``Keys`` there would read as a vocabulary they
    share.

    **Only git can read a set and still be a set.** Every other store here
    would have to issue one request per key, so what came back could be a
    document that never existed at any instant. A git fetch resolves one
    commit, and a commit has one tree, so the whole set is read as of one
    instant with nothing arranged for it.
    """

    __slots__ = ("_kind", "_paths")

    def __init__(self, kind: str, paths: Sequence[str]) -> None:
        """Not for direct use — build one with the classmethods."""
        self._kind = kind
        self._paths = list(paths)

    @classmethod
    def one(cls, path: str) -> GitKeys:
        """One file, whose contents are the whole document.

        Handed to the loader byte for byte — never parsed and re-rendered — so
        comments and key order survive.
        """
        return cls("one", [path])

    @classmethod
    def several(cls, paths: Sequence[str]) -> GitKeys:
        """Several named files, merged **in the order given — later wins**.

        The rule a list of layered files already teaches: the caller wrote the
        list, so the list is the precedence. Tables merge deeply and arrays are
        replaced whole.
        """
        return cls("several", list(paths))

    @classmethod
    def prefix(cls, directory: str) -> GitKeys:
        """Every file under a directory, merged as **disjoint sections**.

        A caller naming a directory is not expressing an order — a tree lists
        its entries in the order git sorted them, which is nobody's precedence
        — so two files under it supplying the same path is a deployment bug and
        is reported as one rather than resolved.

        A **directory**, not a string prefix: ``prefix("services/api")`` reads
        ``services/api/db.yaml`` and does not read ``services/api-old.yaml``.
        The walk is recursive, an empty string is the repository root, and
        every file found has to parse — so point it at a directory that holds
        configuration and nothing else. It needs ``format``, because a
        directory has no extension to read one from.
        """
        return cls("prefix", [directory])

    def _resolve(self) -> tuple[str, list[str]]:
        """What the compiled store takes: the shape, and the paths."""
        return self._kind, list(self._paths)

    def __repr__(self) -> str:
        """The shape and the paths, which are names rather than secrets."""
        if self._kind == "prefix":
            return f"GitKeys.prefix({self._paths[0]!r})"

        if self._kind == "one":
            return f"GitKeys.one({self._paths[0]!r})"

        return f"GitKeys.several({self._paths!r})"


class GitAuth:
    """How to authenticate to a git host.

    Mirrors ``dynamic_config_git::Credential``. git has exactly two places a
    credential can go — the HTTP ``Authorization`` header, or the ``ssh``
    process that carries the stream — so this is those two plus the absence of
    both::

        GitAuth.anonymous()                     # a public repository
        GitAuth.token(token)                    # a PAT, an installation token
        GitAuth.basic(username, password)       # a host that reads the user half
        GitAuth.ssh_agent()                     # SSH_AUTH_SOCK and ~/.ssh/config
        GitAuth.ssh_key("/etc/myapp/id_ed25519")
        GitAuth.ssh_command("ssh -J bastion")

    **The credential arguments accept callables**, and here that is the whole
    point rather than a convenience: a GitHub App installation token lives one
    hour, a workload-identity token minutes, and a configuration watcher lives
    for the life of the process. The callable runs on every fetch, and a value
    that has changed is presented on the next one with **nothing rebuilt** —
    the source keeps its object database, so a rotation costs no transfer.

    :meth:`ssh_key` takes a path and no callable, deliberately: ``ssh`` opens
    the file itself at every fetch, so a key an operator replaces is already
    picked up. It is the same reason ``ConsulAuth.kubernetes`` carries a path.

    **A passphrase is not accepted, in any spelling.** ``ssh`` has no way to
    take one that does not put it on a command line where ``ps`` can read it,
    or in a file this package would have to write. A passphrase-protected key
    belongs in an agent — ``ssh-add`` it once — which is what
    :meth:`ssh_agent` is.

    The builder methods return a new ``GitAuth`` rather than mutating this one,
    so an auth shared between two stores cannot be changed by either.
    """

    __slots__ = ("_first", "_kind", "_second")

    def __init__(self, kind: str, first: Credential, second: Credential) -> None:
        """Not for direct use — build one with the classmethods."""
        self._kind = kind
        self._first = first
        self._second = second

    @classmethod
    def anonymous(cls) -> GitAuth:
        """No credential at all — a public repository over ``https``.

        What a source with no ``auth`` uses, matching the Rust builder.
        """
        return cls("anonymous", "", "")

    @classmethod
    def token(cls, token: Credential) -> GitAuth:
        """A token, over ``https``: a PAT, an installation token, a deploy token.

        It travels as HTTP basic authentication with ``x-access-token`` in the
        user half, which is what GitHub documents and what GitLab and Azure
        DevOps ignore. Pass a **callable** for anything that expires; a string
        is presented unchanged forever, because there is nothing here to obtain
        another one with.
        """
        return cls("token", TOKEN_USERNAME, token)

    @classmethod
    def basic(cls, username: str, password: Credential) -> GitAuth:
        """A user name and password (or token) of the caller's choosing.

        For the host that does read the user half: a GitLab deploy token is a
        real user name and a real token, and a CI job token is
        ``gitlab-ci-token`` with ``CI_JOB_TOKEN``.
        """
        return cls("basic", username, password)

    @classmethod
    def ssh_agent(cls) -> GitAuth:
        """Whatever ``ssh`` would do unaided.

        The agent in ``SSH_AUTH_SOCK``, the keys ``~/.ssh/config`` names, the
        defaults — and therefore the right choice for a passphrase-protected
        key, a hardware key or a ``ProxyJump`` somebody already configured.

        **The ``ssh`` binary must be on the host** for this and the two below:
        `gix` carries an SSH stream by spawning the system ``ssh``, exactly as
        ``git`` does.
        """
        return cls("ssh_agent", "", "")

    @classmethod
    def ssh_key(cls, path: PathArgument) -> GitAuth:
        """One private key file, and only that one.

        Adds ``-o IdentitiesOnly=yes``, so an agent holding other keys cannot
        offer them first and exhaust the server's ``MaxAuthTries`` before the
        intended key is tried. The key's *contents* are never read by this
        package and never printed.
        """
        return cls("ssh_key", os.fspath(path), "")

    @classmethod
    def ssh_command(cls, command: Credential) -> GitAuth:
        """Run this instead of ``ssh``.

        The escape hatch, and an explicit one: for a jump host, a vendored
        client, ``-o`` options this package has no opinion about, or a test
        double. It becomes ``core.sshCommand`` for one fetch and is never
        written to the working directory's config.

        It takes a callable like every other credential here, and it is
        **redacted whole** in a `repr` and in any message — a caller reaching
        for this hatch may well have put a secret in it, and nothing here can
        tell which word it is.
        """
        return cls("ssh_command", command, "")

    @property
    def _transport(self) -> str:
        """Which of git's two transports this credential is for."""
        if self._kind == "anonymous":
            return "any"

        return "https" if self._kind in ("token", "basic") else "ssh"

    def _resolve(self) -> tuple[str, str, str]:
        """This credential's current value, as the compiled store takes it.

        ``token`` and ``basic`` are one thing to the store crate — HTTP basic
        authentication — and two things here, because they are two spellings a
        caller chooses between and a `repr` has to tell them apart.
        """
        if self._kind in ("token", "basic"):
            return (
                "https",
                resolve(self._first, f"{self._kind}'s username"),
                resolve(self._second, f"{self._kind}'s token"),
            )

        if self._kind == "ssh_key":
            return "ssh_key", resolve(self._first, "ssh_key's path"), ""

        if self._kind == "ssh_command":
            return "ssh_command", resolve(self._first, "ssh_command's command"), ""

        return self._kind, "", ""

    def __repr__(self) -> str:
        """The method and its non-secret half. Never the credential.

        A key *path* is printed, because it names which key and is the first
        thing somebody debugging a refused login needs; a custom ``ssh``
        command is not, because it may be carrying one.
        """
        if self._kind in ("anonymous", "ssh_agent"):
            return f"GitAuth.{self._kind}()"

        if self._kind == "ssh_key":
            return f"GitAuth.ssh_key({self._first!r})"

        if self._kind == "ssh_command":
            return "GitAuth.ssh_command('***')"

        if self._kind == "basic":
            username = self._first if isinstance(self._first, str) else "<callable>"

            return f"GitAuth.basic({username!r}, '***')"

        return "GitAuth.token('***')"


class Git(RemoteSource):
    """A file, a list of them or a directory in a git repository.

    Mirrors ``dynamic_config_git::GitSource``: the repository, the ref, what to
    read, the format and the credential.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Git, GitAuth

        config = DynamicConfig(Database, key="db").remote(
            Git(
                "https://github.com/acme/config.git",
                "services/api/db.yaml",
                branch="main",
                auth=GitAuth.token(installation_token),
            )
        )
        config.refresh_remote()
        config.init()

    Configuration in git is how a great many teams already work: review,
    history, blame and rollback come free, and nobody runs etcd for a file that
    changes twice a month. A fetch is **shallow and single-ref** — one commit,
    no history, nothing checked out — and **an unchanged ref transfers
    nothing**, which is what makes polling a git host reasonable.

    Two things here have no equivalent in the other seven stores:

    - **Several files, read as one document, atomically.** One commit has one
      tree, so a list or a directory is read as of one instant. See
      :class:`~dynamic_config_remote.GitKeys`.
    - **The commit ends up in the provenance.** After a fetch,
      :meth:`describe` names the commit the document came from rather than the
      branch it was asked for, because *which commit is this program actually
      serving* is the first question of every configuration-in-git incident.

    ``watch()`` is **not** exposed, here or for any other store in this wheel,
    and git is the one where that is a real loss rather than a formality: it is
    the only store in the family whose multi-file sources can be watched at
    all, because what moves is a ref and what a ref names is a commit. It stays
    unexposed because a Rust callback on a Rust thread calling into Python is a
    second GIL story on top of this one, and making git the exception would be
    that story for one store. ``refresh_remote()`` on a timer is what Python
    has, and against git it costs one ref advertisement per tick.

    Nothing reaches the network here. The first :meth:`fetch` does.
    """

    __slots__ = ("_auth", "_store")

    def __init__(
        self,
        url: str,
        path: PathsArgument,
        *,
        branch: Optional[str] = None,
        tag: Optional[str] = None,
        commit: Optional[str] = None,
        format: Optional[str] = None,  # noqa: A002
        auth: Optional[GitAuth] = None,
        cache_dir: Optional[PathArgument] = None,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
        compact_after: Optional[int] = None,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``url`` is anything git understands: ``https://…``, ``ssh://…``,
        ``git@host:org/repo.git``, or a local path. ``path`` is one
        ``/``-separated path relative to the repository root, a list of them,
        or a :class:`~dynamic_config_remote.GitKeys`.

        ``branch``, ``tag`` and ``commit`` are three spellings of one
        reference, so **name at most one**; with none, the Rust crate's default
        branch (``main``) is read. A commit is the full hexadecimal object id —
        the protocol cannot ask for an abbreviation — and pinning one means a
        source that will never change.

        ``format`` is inferred from a path's extension when it has one, and has
        to be given for a file that does not say (``.config``, no extension),
        for a list whose members name two different formats, and always for a
        directory.

        ``auth`` defaults to :meth:`GitAuth.anonymous`, which is what a public
        repository wants. ``tls`` is a private certificate authority, a client
        certificate, or both — for an enterprise host over ``https``; unlike
        the other stores, a named authority is **added to** the platform's
        trust store rather than replacing it, so one source configuration
        reaches both a private GitLab and github.com.

        ``cache_dir`` keeps the object database somewhere that survives
        restarts, so a restarted process transfers almost nothing; without one
        it is a private temporary directory removed with the source. Two
        sources may not name the same directory. ``timeout`` bounds one fetch
        attempt, ``max_bytes`` is the largest single file that will be read,
        and ``compact_after`` is how many transfers a working directory may
        accumulate before it is emptied and refilled — each of them the Rust
        crate's default (thirty seconds, one megabyte, thirty-two transfers)
        when it is absent, so the number lives in one place.

        Raises `ValueError` if more than one reference is named, if a
        credential or ``tls`` is for the transport this url does not use, or if
        the source cannot be built — no path, a path that leaves the
        repository, a format nothing can infer, a commit id that is not one, or
        a working directory another source already holds. Raises `TypeError` if
        ``auth`` is not a `GitAuth`, ``tls`` is not a `TlsConfig`, or ``path``
        is not a path, a list of them or a `GitKeys`.
        """
        if auth is not None and not isinstance(auth, GitAuth):
            raise TypeError(
                f"auth has to be a GitAuth, not {type(auth).__name__}; build "
                f"one with GitAuth.token(...) or another of its methods"
            )

        self._auth = auth if auth is not None else GitAuth.anonymous()

        _one_transport(url, self._auth, tls)
        keys, paths = _keys(path)

        self._store = _core.GitStore(
            url,
            keys,
            paths,
            _reference(branch, tag, commit),
            format,
            None if cache_dir is None else os.fspath(cache_dir),
            timeout,
            max_bytes,
            compact_after,
            unwrapped(tls),
        )

    def fetch(self) -> tuple[str, Format]:
        """Fetches the ref and reads the document at that commit.

        The credential is resolved first, on this thread; the handshake, the
        transfer and the read release the GIL. A credential that has changed
        since the last fetch is presented on this one and **nothing is
        rebuilt** — unlike the six stores whose credential lives inside a
        client, this store's lives in a slot, so a rotation does not cost the
        object database.

        Raises :class:`dynamic_config.AuthError` if the host refused the
        credential and :class:`dynamic_config.RemoteError` for anything else —
        an unreachable host, a ref that does not exist, a file that is not
        there, a document that does not parse. Neither message carries a
        credential, and a git remote url routinely embeds one.
        """
        kind, first, second = self._auth._resolve()

        with translated():
            text, named = self._store.fetch(kind, first, second)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in the url removed.

        The commit, once one has been read; the ref that was asked for until
        then.
        """
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)


def _keys(path: PathsArgument) -> tuple[str, list[str]]:
    """What to read, as the compiled store takes it.

    A `pathlib.Path` is refused rather than accepted: a path *in a repository*
    is `/`-separated whatever this machine's separator is, so a `Path` would
    silently become a backslash-separated name on Windows and read nothing.
    """
    if isinstance(path, GitKeys):
        return path._resolve()

    if isinstance(path, str):
        return "one", [path]

    if isinstance(path, Sequence):
        paths = list(path)

        if all(isinstance(one, str) for one in paths):
            return "several", paths

    raise TypeError(
        f"path has to be a str, a list of them or a GitKeys, not "
        f"{type(path).__name__}; a path in a repository is `/`-separated and "
        f"relative to its root, so it is a str rather than a pathlib.Path"
    )


def _reference(
    branch: Optional[str], tag: Optional[str], commit: Optional[str]
) -> Optional[tuple[str, str]]:
    """The one reference to read, or `None` for the Rust crate's default.

    Rust's builder takes these as three calls, where the last one wins and the
    order is visible. Three keyword arguments have no order at all, so naming
    two of them is refused rather than resolved by a rule nobody wrote down.
    """
    named = [
        (kind, name)
        for kind, name in (("branch", branch), ("tag", tag), ("commit", commit))
        if name is not None
    ]

    if len(named) > 1:
        raise ValueError(
            f"git: {', '.join(kind for kind, _ in named)} name one reference "
            f"between them, and a call naming more than one does not say which "
            f"it means; pass exactly one of branch, tag or commit"
        )

    return named[0] if named else None


def _one_transport(url: str, auth: GitAuth, tls: Optional[TlsConfig]) -> None:
    """Refuses a credential or a `TlsConfig` for the transport this url is not.

    The url decides the transport, and each transport has exactly one place a
    credential goes: an ``Authorization`` header for ``https``, the ``ssh``
    process for everything else. A source configured for the other one is not
    half-configured — it is **anonymous**, silently, which for a private
    repository is an error a long way from its cause and for a public one is a
    program that works until the repository is made private.

    So it is refused here, naming the call and the way out, in the shape `Nats`
    and `S3` already use for the parts of TLS they cannot express. The store
    crate refuses the ``tls`` half again when it builds, which is the belt to
    this braces.

    **The url is never quoted in these messages.** It routinely carries a
    token, and the redaction that takes one out lives in Rust — so this side
    names the argument instead, which is the half worth reading anyway.
    """
    over_https = url.startswith(("https://", "http://"))
    ssh_spellings = (
        "GitAuth.ssh_agent(), GitAuth.ssh_key(path) or GitAuth.ssh_command(command)"
    )

    if auth._transport == "https" and not over_https:
        raise ValueError(
            f"git: this url is not an http(s) one, so an https credential "
            f"cannot be presented on it and is refused rather than ignored; an "
            f"ssh remote authenticates through a key — {ssh_spellings}"
        )

    if auth._transport == "ssh" and over_https:
        raise ValueError(
            "git: this url is an http(s) one, so an ssh credential cannot be "
            "presented on it and is refused rather than ignored; an https "
            "remote authenticates with a token — GitAuth.token(token), or "
            "GitAuth.basic(username, password) for a host that reads the user "
            "half"
        )

    if tls is not None and not tls.is_empty() and not url.startswith("https://"):
        raise ValueError(
            f"git: `tls` configures the https transport and this url is not an "
            f"https one, so it cannot be applied here and is refused rather "
            f"than ignored; an ssh remote authenticates its host through "
            f"`known_hosts` and its client through a key — {ssh_spellings}"
        )
