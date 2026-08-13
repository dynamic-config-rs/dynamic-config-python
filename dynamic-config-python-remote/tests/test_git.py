"""git: one commit, one tree, and a token that lives an hour.

The eighth store, and the one that is least like the other seven. Three claims
are made for it and they need three kinds of test.

**It reads a real repository.** Every test below that reads anything reads one
built by `git` in a temporary directory, over `file://` — no network, no
container, no mock of the thing under test. What that cannot cover is
authentication, because the file transport has none.

**A credential callable is what the store presents.** That is a claim about
the bytes on the wire, so it is tested against the scripted git host in
`conftest.py`, which records every password presented and delegates the git
half to a real `git upload-pack`. It is the same claim
`test_a_token_callable_returning_a_new_token_is_the_one_the_store_uses` makes
of Vault, against the store where it matters most: a GitHub App installation
token lives one hour and a watcher lives for the life of the process.

**What it cannot express is refused.** A reference named twice, a credential
for the transport this url does not use, a `TlsConfig` on a url with no TLS in
it — each a `ValueError` at construction naming the call and the way out,
rather than a source that quietly fetches anonymously.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from conftest import Certificates, GitLog, Repository, serving_git
from dynamic_config import AuthError, DynamicConfig, Format, RemoteError
from dynamic_config_remote import Git, GitAuth, GitKeys, TlsConfig, runtime_started

#: The document the repositories here hold, under the section key a
#: configuration would use.
DOCUMENT = json.dumps({"db": {"host": "from-the-repository", "port": 6000}})

#: A token in the shape GitHub issues one, long enough that scrubbing it does
#: not eat an ordinary word out of a message.
TOKEN = "ghs_a-planted-installation-token"


@dataclass
class FromGit:
    """The schema for the tests here that build a configuration.

    Declared once and used by the two that need it — a config type keys the
    engine's `static`s, so two tests sharing one would race. These two never
    run concurrently, and each installs the same source.
    """

    host: str = "localhost"
    port: int = 5432


class Rotating:
    """A credential that is different every time it is asked."""

    def __init__(self, *values: str) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> str:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1

        return value


# ── What it reads ──────────────────────────────────────────────────────


def test_a_file_in_a_repository_is_read_at_the_ref_it_names(
    repository: Repository,
) -> None:
    repository.commit({"services/api/db.json": DOCUMENT})

    text, read_as = repository_store(repository, "services/api/db.json").fetch()

    assert json.loads(text)["db"]["host"] == "from-the-repository"
    assert read_as is Format.JSON


def test_a_list_of_files_is_merged_in_the_order_given(
    repository: Repository,
) -> None:
    # The rule a list of layered files already teaches: the caller wrote the
    # list, so the list is the precedence.
    repository.commit(
        {
            "base.json": json.dumps({"db": {"host": "base", "port": 5432}}),
            "local.json": json.dumps({"db": {"host": "local"}}),
        }
    )

    text, _ = repository_store(repository, ["base.json", "local.json"]).fetch()
    merged = json.loads(text)["db"]

    assert merged["host"] == "local", "later wins"
    assert merged["port"] == 5432, "and the rest of the earlier one survives"


def test_a_directory_is_read_as_disjoint_sections(repository: Repository) -> None:
    repository.commit(
        {
            "services/db.json": DOCUMENT,
            "services/server.json": json.dumps({"server": {"port": 8080}}),
        }
    )

    store = Git(
        repository.url(), GitKeys.prefix("services"), format="json", timeout=30.0
    )
    document = json.loads(store.fetch()[0])

    assert document["db"]["host"] == "from-the-repository"
    assert document["server"]["port"] == 8080


def test_a_directory_supplying_one_path_twice_is_a_deployment_bug(
    repository: Repository,
) -> None:
    # A caller naming a directory wrote no order — a tree lists its entries in
    # the order git sorted them, which is nobody's precedence — so an overlap
    # is reported rather than resolved.
    repository.commit(
        {
            "services/one.json": json.dumps({"db": {"host": "one"}}),
            "services/two.json": json.dumps({"db": {"host": "two"}}),
        }
    )

    store = Git(repository.url(), GitKeys.prefix("services"), format="json")

    with pytest.raises(RemoteError, match="db"):
        store.fetch()


def test_the_set_is_read_out_of_one_commit(repository: Repository) -> None:
    # The property that makes a multi-file source defensible here and nowhere
    # else in this wheel: one fetch resolves one commit, and a commit has one
    # tree, so four files written together are read together with no
    # transaction and no listing race.
    repository.commit(
        {
            "a.json": json.dumps({"a": {"generation": 1}}),
            "b.json": json.dumps({"b": {"generation": 1}}),
        }
    )
    store = repository_store(repository, ["a.json", "b.json"])
    first = json.loads(store.fetch()[0])

    repository.commit(
        {
            "a.json": json.dumps({"a": {"generation": 2}}),
            "b.json": json.dumps({"b": {"generation": 2}}),
        }
    )
    second = json.loads(store.fetch()[0])

    assert first["a"]["generation"] == first["b"]["generation"] == 1
    assert second["a"]["generation"] == second["b"]["generation"] == 2


def test_a_tag_and_a_commit_pin_what_a_branch_does_not(
    repository: Repository,
) -> None:
    pinned = repository.commit({"db.json": DOCUMENT})
    repository.tag("v1.0")
    repository.commit({"db.json": json.dumps({"db": {"host": "moved", "port": 1}})})

    for reference in ({"tag": "v1.0"}, {"commit": pinned}):
        store = Git(repository.url(), "db.json", **reference)  # type: ignore[arg-type]

        assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-repository"

    # ...and the branch, which is the default, has moved on.
    assert (
        json.loads(repository_store(repository, "db.json").fetch()[0])["db"]["host"]
        == "moved"
    )


def test_the_commit_reaches_the_provenance_once_one_has_been_read(
    repository: Repository,
) -> None:
    # "Which commit is this program actually serving" is the first question of
    # every configuration-in-git incident, and a branch name does not answer
    # it.
    commit = repository.commit({"db.json": DOCUMENT})
    store = repository_store(repository, "db.json")

    assert "branch main" in store.describe()

    store.fetch()

    assert commit[:12] in store.describe(), store.describe()


def test_an_unchanged_ref_is_read_again_without_a_transfer(
    certificates: Certificates,
    repository: Repository,
) -> None:
    # What makes polling a git host reasonable, and the only place it can be
    # counted: the scripted host sees the ref advertisement either way and the
    # object request only when the commit it names is new.
    repository.commit({"db.json": DOCUMENT})

    with serving_git(certificates, repository) as host:
        store = hosted_store(host, certificates, "db.json")

        store.fetch()
        store.fetch()

        assert host.transfers == 1, "the second fetch found the commit it had"


def test_a_configuration_reads_itself_out_of_a_repository(
    repository: Repository,
) -> None:
    repository.commit({"services/api/db.json": DOCUMENT})

    config = DynamicConfig(FromGit, key="db").remote(
        repository_store(repository, "services/api/db.json")
    )
    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-repository"
    assert config.current().port == 6000


def test_a_git_store_starts_no_runtime(repository: Repository) -> None:
    # Blocking, like Vault, Consul, Firestore and Redis: a git fetch is a
    # negotiation and a decompression rather than a future, so a program
    # reading only git pays for no worker threads. `test_runtime.py` proves
    # the same thing in a fresh interpreter, which is where it is airtight;
    # this is the cheap guard beside the store itself.
    already = runtime_started()

    repository_store(repository, "db.json")

    assert runtime_started() == already


# ── The credential ─────────────────────────────────────────────────────


def test_a_token_callable_returning_a_new_token_is_the_one_the_store_uses(
    certificates: Certificates,
    repository: Repository,
) -> None:
    # The test the item exists for, in the shape every store in this wheel
    # answers to: what the *server saw* on the second fetch is what the
    # callable returned the second time. An installation token lives an hour
    # and a watcher outlives it.
    repository.commit({"db.json": DOCUMENT})

    with serving_git(certificates, repository) as host:
        token = Rotating("first-token", "right-token")
        store = hosted_store(host, certificates, "db.json", auth=GitAuth.token(token))

        with pytest.raises(AuthError):
            store.fetch()

        # The same store object, nothing rebuilt by hand.
        assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-repository"

    assert host.presented[0] == "first-token"
    assert host.presented[-1] == "right-token"
    assert token.calls >= 2, "a credential is resolved once per fetch"


def test_a_rotated_token_keeps_the_object_database(
    certificates: Certificates,
    repository: Repository,
) -> None:
    # What the slot buys, and the reason this store does not rebuild the way
    # the other six do: a `GitSource` owns a working directory, so rebuilding
    # one would throw away every object it holds and re-transfer the whole
    # tree. Here the rotation costs a mutex write, and the second fetch finds
    # the commit it already has.
    repository.commit({"db.json": DOCUMENT})

    with serving_git(certificates, repository) as host:
        tokens = Rotating("right-token", "right-token-again")
        store = hosted_store(host, certificates, "db.json", auth=GitAuth.token(tokens))

        store.fetch()
        host.accepted = "right-token-again"
        store.fetch()

        assert host.presented[-1] == "right-token-again", "the new one was presented"
        assert host.transfers == 1, "and nothing was transferred a second time"


def test_a_refused_token_raises_AuthError_and_never_repeats_it(  # noqa: N802
    certificates: Certificates,
    repository: Repository,
) -> None:
    repository.commit({"db.json": DOCUMENT})

    with serving_git(certificates, repository) as host:
        store = hosted_store(host, certificates, "db.json", auth=GitAuth.token(TOKEN))

        with pytest.raises(AuthError) as refusal:
            store.fetch()

    assert TOKEN not in str(refusal.value), refusal.value


def test_a_plain_string_token_still_works(
    certificates: Certificates,
    repository: Repository,
) -> None:
    repository.commit({"db.json": DOCUMENT})

    with serving_git(certificates, repository) as host:
        store = hosted_store(
            host, certificates, "db.json", auth=GitAuth.token("right-token")
        )

        assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-repository"

    # One per request rather than one per fetch: smart HTTP is a `GET` of the
    # ref advertisement and a `POST` of the negotiation, and both carry the
    # header.
    assert set(host.presented) == {"right-token"}


def test_a_basic_credential_presents_the_user_half_it_was_given(
    certificates: Certificates,
    repository: Repository,
) -> None:
    # For the host that does read the user name: a GitLab deploy token is a
    # real user and a real token, and a CI job token is `gitlab-ci-token`.
    repository.commit({"db.json": DOCUMENT})

    with serving_git(certificates, repository) as host:
        store = hosted_store(
            host,
            certificates,
            "db.json",
            auth=GitAuth.basic("gitlab-ci-token", lambda: "right-token"),
        )
        store.fetch()

    assert set(host.presented) == {"right-token"}


def test_a_callable_returning_something_other_than_a_str_says_which_argument() -> None:
    store = Git(
        "https://github.com/acme/config.git",
        "db.json",
        auth=GitAuth.token(lambda: 42),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(TypeError, match="token's token"):
        store.fetch()


def test_every_rust_credential_shape_has_a_python_spelling() -> None:
    # The mirror is the contract: a Rust variant with no Python spelling is a
    # way of authenticating a Python deployment cannot use, and nothing but
    # this notices when one is added.
    #
    # `Credential::from_fn` is absent from the list because it is not a shape —
    # it is what passing a callable to any of these already is.
    for auth in (
        GitAuth.anonymous(),
        GitAuth.token("t"),
        GitAuth.basic("u", "p"),
        GitAuth.ssh_agent(),
        GitAuth.ssh_key("/home/app/.ssh/id_ed25519"),
        GitAuth.ssh_command("ssh -J bastion"),
    ):
        assert isinstance(auth, GitAuth)


def test_an_ssh_key_carries_the_path_rather_than_the_key(tmp_path: Path) -> None:
    # `ssh` opens the file itself at every fetch, so a key an operator replaces
    # is picked up with no callable — the same reason `ConsulAuth.kubernetes`
    # carries a path.
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    kind, first, _ = GitAuth.ssh_key(key)._resolve()

    assert (kind, first) == ("ssh_key", str(key))


def test_a_path_object_is_a_key_path() -> None:
    assert str(GitAuth.ssh_key(Path("/k/id_rsa"))._resolve()[1]) == "/k/id_rsa"


# ── What it refuses ────────────────────────────────────────────────────


def test_two_references_are_refused_because_neither_call_has_an_order() -> None:
    # Rust takes these as three builder calls, where the last one wins and the
    # order is visible. Three keyword arguments have no order at all.
    with pytest.raises(ValueError, match="name one reference between them"):
        Git("https://github.com/acme/config.git", "db.json", branch="main", tag="v1")

    with pytest.raises(ValueError, match="pass exactly one"):
        Git(
            "https://github.com/acme/config.git",
            "db.json",
            tag="v1",
            commit="a" * 40,
        )


def test_an_https_credential_on_an_ssh_url_is_refused_rather_than_ignored() -> None:
    # Not half-configured — *anonymous*, which for a private repository is an
    # error a long way from its cause and for a public one is a program that
    # works until the repository is made private.
    for url in ("ssh://git@github.com/acme/config.git", "git@github.com:acme/cfg.git"):
        with pytest.raises(ValueError, match="refused rather than ignored") as refusal:
            Git(url, "db.json", auth=GitAuth.token(TOKEN))

        assert "GitAuth.ssh_agent()" in str(refusal.value)


def test_an_ssh_credential_on_an_https_url_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="refused rather than ignored") as refusal:
        Git(
            "https://github.com/acme/config.git",
            "db.json",
            auth=GitAuth.ssh_key("/home/app/.ssh/id_ed25519"),
        )

    assert "GitAuth.token(token)" in str(refusal.value)


def test_tls_on_a_url_with_no_tls_in_it_is_refused() -> None:
    # The store crate's own rule, restated in the spellings a Python caller can
    # write: an ssh remote authenticates its host through `known_hosts`, and a
    # certificate authority has nothing to do with it.
    with pytest.raises(ValueError, match="refused rather than ignored") as refusal:
        Git(
            "ssh://git@github.com/acme/config.git",
            "db.json",
            auth=GitAuth.ssh_agent(),
            tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
        )

    assert "known_hosts" in str(refusal.value)


def test_no_refusal_quotes_the_url_because_a_git_url_carries_a_token() -> None:
    # `https://x-access-token:ghs_…@github.com/acme/config.git` is what every
    # CI system writes. The redaction that takes one out lives in Rust, so
    # these messages name the argument instead — which is the half worth
    # reading anyway.
    with pytest.raises(ValueError, match="refused rather than ignored") as refusal:
        Git(
            f"https://x-access-token:{TOKEN}@github.com/acme/config.git",
            "db.json",
            auth=GitAuth.ssh_agent(),
        )

    assert TOKEN not in str(refusal.value), refusal.value
    assert "github.com" not in str(refusal.value), refusal.value


def test_a_path_that_tries_to_leave_the_repository_is_refused() -> None:
    for path in ("../../etc/shadow", "/etc/shadow", "services//db.json", ""):
        with pytest.raises(ValueError, match="not a file inside the repository"):
            Git("https://github.com/acme/config.git", path, format="json")


def test_a_file_whose_name_says_nothing_needs_a_format() -> None:
    with pytest.raises(ValueError, match="cannot tell what format"):
        Git("https://github.com/acme/config.git", "services/api/settings")

    # A directory never says, so it always needs one.
    with pytest.raises(ValueError, match="cannot tell what format"):
        Git("https://github.com/acme/config.git", GitKeys.prefix("services"))

    Git("https://github.com/acme/config.git", "services/api/settings", format="toml")


def test_two_paths_naming_two_formats_are_refused_where_they_were_written() -> None:
    # Parsing `server.toml` as JSON produces a syntax error about a file that
    # has no syntax error in it.
    with pytest.raises(ValueError, match="names Yaml"):
        Git("https://github.com/acme/config.git", ["db.yaml", "server.toml"])


def test_an_abbreviated_commit_is_refused_where_it_was_written() -> None:
    # The protocol cannot ask for an abbreviation, so accepting one here would
    # only move the failure to the first fetch.
    with pytest.raises(ValueError, match="not a full commit id"):
        Git("https://github.com/acme/config.git", "db.json", commit="deadbee")


def test_a_working_directory_belongs_to_one_source(
    repository: Repository,
    tmp_path: Path,
) -> None:
    # Two sources fetching into one object database would interleave their ref
    # updates and their packs, so the second is refused before anything is
    # written.
    cache = tmp_path / "objects"
    first = Git(repository.url(), "db.json", cache_dir=cache)

    with pytest.raises(ValueError, match="already the working directory"):
        Git(repository.url(), "db.json", cache_dir=cache)

    # ...and the claim ends with the source, so a program that replaces one can
    # reuse the directory it paid to fill.
    del first

    Git(repository.url(), "db.json", cache_dir=cache)


def test_a_path_that_is_not_a_repository_path_names_why() -> None:
    # A path *in a repository* is `/`-separated whatever this machine's
    # separator is, so a `pathlib.Path` would silently become a
    # backslash-separated name on Windows and read nothing.
    with pytest.raises(TypeError, match=r"rather than a pathlib\.Path"):
        Git("https://github.com/acme/config.git", Path("db.json"))  # type: ignore[arg-type]


def test_something_that_is_not_a_git_auth_is_refused() -> None:
    with pytest.raises(TypeError, match="has to be a GitAuth"):
        Git("https://github.com/acme/config.git", "db.json", auth=TOKEN)  # type: ignore[arg-type]


# ── Credentials never appear in a diagnostic ───────────────────────────


def test_a_repr_never_shows_a_token_in_the_url() -> None:
    # A git remote url carries one in the ordinary case rather than the exotic
    # one, and a token may itself contain `@` — so the split is on the last
    # one, which is the store crates' shared rule rather than a copy of it.
    store = Git(f"https://x-access-token:{TOKEN}@github.com/acme/config.git", "db.json")
    printed = f"{store!r} {store.describe()}"

    assert TOKEN not in printed, printed
    assert "ghs_" not in printed, printed
    # The half worth seeing survives.
    assert "x-access-token:***@github.com/acme/config.git" in printed, printed


def test_a_lone_authority_in_a_git_url_is_a_secret() -> None:
    # `https://ghp_…@github.com/acme/config.git` is a documented GitHub form in
    # which the whole authority *is* the token. Reading it as a user name would
    # print it.
    printed = repr(Git(f"https://{TOKEN}@github.com/acme/config.git", "db.json"))

    assert TOKEN not in printed, printed
    assert "https://***@github.com/acme/config.git" in printed, printed


def test_no_git_auth_repr_shows_its_credential() -> None:
    printed = " ".join(
        repr(auth)
        for auth in (
            GitAuth.anonymous(),
            GitAuth.token("ghs_hunter2-installation-token"),
            GitAuth.basic("gitlab-ci-token", "hunter2-job-token"),
            GitAuth.ssh_agent(),
            GitAuth.ssh_key("/home/app/.ssh/id_ed25519"),
            GitAuth.ssh_command("sshpass -p hunter2-passphrase ssh"),
        )
    )

    assert "hunter2" not in printed, printed
    # The halves worth seeing survive: a user name and a key path are what a
    # person debugging a refused login actually needs. A custom `ssh` command
    # is not one of them — a caller reaching for that hatch may well have put a
    # secret in it, and nothing here can tell which word.
    assert "gitlab-ci-token" in printed, printed
    assert "id_ed25519" in printed, printed
    assert "sshpass" not in printed, printed


def test_a_failed_fetch_repeats_neither_the_url_credential_nor_the_token() -> None:
    # Both rules at once: the token in the url goes by the shared redaction,
    # and the one resolved for this call goes by value.
    store = Git(
        f"https://x-access-token:{TOKEN}@127.0.0.1:1/acme/config.git",
        "db.json",
        auth=GitAuth.token("hunter2-argument-token"),
        timeout=5.0,
    )

    with pytest.raises(RemoteError) as failure:
        store.fetch()

    assert TOKEN not in str(failure.value), failure.value
    assert "hunter2" not in str(failure.value), failure.value
    assert "127.0.0.1" in str(failure.value), "the host is what a reader needs"


def test_the_document_itself_never_reaches_a_message(
    repository: Repository,
) -> None:
    repository.commit({"db.json": DOCUMENT})
    store = repository_store(repository, "db.json")
    text, _ = store.fetch()

    assert "from-the-repository" in text, "the document is what came back"
    assert "from-the-repository" not in repr(store), repr(store)
    assert "from-the-repository" not in store.describe(), store.describe()


# ── Helpers ────────────────────────────────────────────────────────────


def repository_store(repository: Repository, path: object) -> Git:
    """A source reading `path` out of a local repository over `file://`."""
    return Git(repository.url(), path)  # type: ignore[arg-type]


def hosted_store(
    host: GitLog,
    certificates: Certificates,
    path: str,
    auth: Optional[GitAuth] = None,
) -> Git:
    """A source reading the scripted host, behind the throwaway authority."""
    return Git(
        host.url,
        path,
        auth=auth if auth is not None else GitAuth.token("right-token"),
        tls=TlsConfig().with_ca_certificate_file(certificates.ca),
        timeout=30.0,
    )
