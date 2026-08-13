r"""git: one commit, one tree — and a token that lives an hour.

Configuration in git is how a great many teams already work: review, history,
blame and rollback come free, and nobody runs etcd for a file that changes twice
a month. This store fetches **one ref, shallowly**, reads one blob — or one
whole directory — out of that commit, and never checks anything out.

Three things are characteristic, and this example shows all three:

- **A set of files is read as of one instant.** One fetch resolves one commit,
  and a commit has one tree, so `["base.yaml", "local.yaml"]` or a whole
  directory is atomic with nothing arranged for it. Every other store in this
  wheel reads one key, and says so.
- **The credential is a callable because installation tokens expire.** A GitHub
  App token lives one hour; a watcher lives for the life of the process. A
  value that has moved is presented on the next fetch with **nothing rebuilt** —
  the object database survives, so a rotation costs no transfer.
- **The commit ends up in the provenance.** *Which commit is this program
  actually serving* is the first question of every configuration-in-git
  incident, and a branch name does not answer it.

**This example needs no server**: it builds a repository in a temporary
directory with the `git` binary and reads it over `file://`. With no `git` on
the host it says so and carries on. Point it at a real repository instead with
`GIT_URL`, `GIT_PATH` and `GITHUB_TOKEN`.
"""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import Git, GitAuth, GitKeys, TlsConfig


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def a_repository_to_read(directory: Path) -> str:
    """A repository with three files in one commit, and its `file://` url.

    Built with the `git` binary because a fixture that needed a git server
    would be a fixture nobody runs. The store reads it with `gix`, which is
    pure Rust and no part of what is built here.
    """

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            check=True,
            text=True,
            timeout=60,
        ).stdout

    directory.mkdir(parents=True, exist_ok=True)
    git("init", "--initial-branch=main")
    git("config", "user.email", "examples@dynamic-config.invalid")
    git("config", "user.name", "an example")

    # Two layers of one section, where an overlap is the *point*…
    layers = directory / "services" / "api"
    layers.mkdir(parents=True)
    (layers / "base.json").write_text(
        json.dumps({"db": {"host": "from-base", "port": 5432}})
    )
    (layers / "local.json").write_text(json.dumps({"db": {"host": "from-local"}}))

    # …and a directory of disjoint sections, where an overlap is a mistake.
    sections = directory / "sections"
    sections.mkdir()
    (sections / "db.json").write_text(
        json.dumps({"db": {"host": "from-the-directory", "port": 6000}})
    )
    (sections / "server.json").write_text(json.dumps({"server": {"port": 8080}}))

    git("add", "--all")
    git("commit", "--quiet", "--message", "a deployment")

    return f"file://{directory}"


def one_file_a_list_and_a_directory(url: str) -> None:
    """The three shapes, and what each of them means about precedence."""
    one = Git(url, "services/api/base.json")
    print("  one file:      ", json.loads(one.fetch()[0]))

    # A list is a precedence: the caller wrote the order, so later wins where
    # two files supply the same path — the rule layered files already teach.
    several = Git(url, ["services/api/base.json", "services/api/local.json"])
    print("  a list:        ", json.loads(several.fetch()[0])["db"])

    # A directory is not an order — a tree lists its entries the way git sorted
    # them, which is nobody's precedence — so these are disjoint sections.
    directory = Git(url, GitKeys.prefix("sections"), format="json")
    print("  a directory:   ", sorted(json.loads(directory.fetch()[0])))
    print("  …read out of one commit, so a deployment that writes four files")
    print("   in one commit is delivered as one document and never as a torn one.")

    # And two files under a directory supplying one path is therefore a
    # deployment bug, reported as one rather than resolved by an order nobody
    # wrote down. The same two files as a *list* merged happily above.
    try:
        Git(url, GitKeys.prefix("services/api"), format="json").fetch()
    except DynamicConfigError as overlap:
        print(f"  an overlap under a directory: {overlap.__cause__ or overlap}")


def the_commit_is_the_provenance(url: str) -> None:
    """A branch says where to look; a commit says what is being served."""
    config = DynamicConfig(Database, key="db").remote(
        Git(url, "services/api/base.json", branch="main")
    )

    store = Git(url, "services/api/base.json")
    print("  before a fetch:", store.describe())

    store.fetch()
    print("  after one:     ", store.describe())

    config.refresh_remote()
    config.init()
    print("  through a configuration:", config.current())
    print("  provenance:", config.source_of("host"))


def a_token_that_lives_an_hour() -> None:
    """The credential story, which is the reason a callable is the default shape.

    A GitHub App installation token expires in an hour, and the exchange that
    mints one belongs in the caller's own closure — signing an app JWT means an
    RS256 stack, which a configuration library should not be. What this package
    owes that flow is the *refresh*, and a callable resolved on every fetch is
    it.
    """
    minted = []

    def installation_token() -> str:
        # Yours would sign the app JWT and POST for an installation token,
        # keeping it until it is nearly expired. Whatever it does, it is
        # called on the thread that asked for the refresh, before the GIL is
        # released for the fetch — so it may block and it may raise.
        minted.append(len(minted))

        return os.environ.get("GITHUB_TOKEN", "ghs_not-a-real-token")

    store = Git(
        os.environ.get("GIT_URL", "https://github.com/acme/config.git"),
        os.environ.get("GIT_PATH", "services/api/db.yaml"),
        auth=GitAuth.token(installation_token),
        # A cache directory survives restarts, so a restarted process transfers
        # almost nothing. Without one it is a private temporary directory
        # removed with the store.
        cache_dir=os.environ.get("GIT_CACHE") or None,
    )

    print("   ", store)
    print("  the token is never in that line, and a git remote url routinely")
    print("  carries one: `https://x-access-token:ghs_…@github.com/acme/config.git`")
    print("  is what every CI system writes.")

    if os.environ.get("GIT_URL"):
        try:
            store.fetch()
            print("  read:", store.describe())
        except DynamicConfigError as unreachable:
            print(f"  not reachable: {unreachable.__cause__ or unreachable}")
    else:
        print("  (set GIT_URL and GITHUB_TOKEN to point this at a real repository)")


def the_other_ways_to_prove_who_is_asking() -> None:
    """ssh, where the credential is a key rather than a header."""
    for auth in (
        GitAuth.anonymous(),
        GitAuth.basic("gitlab-ci-token", lambda: os.environ.get("CI_JOB_TOKEN", "")),
        GitAuth.ssh_agent(),
        GitAuth.ssh_key("/etc/myapp/id_ed25519"),
        GitAuth.ssh_command("ssh -J bastion.internal"),
    ):
        print("   ", auth)

    print("  a key path takes no callable and needs none: `ssh` opens the file")
    print("  itself at every fetch, so a key an operator replaces is already")
    print("  picked up. A passphrase is refused in every spelling — put the key")
    print("  in an agent, which is the one place a passphrase is entered once.")


def what_is_refused_rather_than_ignored() -> None:
    """Each of these would otherwise be a source that quietly fetches anonymously."""
    refusals = (
        lambda: Git(
            "https://github.com/acme/config.git", "db.yaml", branch="main", tag="v1"
        ),
        lambda: Git(
            "git@github.com:acme/config.git", "db.yaml", auth=GitAuth.token("t")
        ),
        lambda: Git(
            "https://github.com/acme/config.git", "db.yaml", auth=GitAuth.ssh_agent()
        ),
        lambda: Git(
            "ssh://git@github.com/acme/config.git",
            "db.yaml",
            auth=GitAuth.ssh_agent(),
            tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
        ),
        lambda: Git("https://github.com/acme/config.git", "services/api/settings"),
        lambda: Git("https://github.com/acme/config.git", "../../etc/shadow"),
    )

    for refused in refusals:
        try:
            refused()
        except ValueError as refusal:
            print(f"  {refusal}")
            print()


def main() -> None:
    if shutil.which("git") is None:
        print("no `git` on this host, so there is no repository to build.")
        print("everything below this line needs one; everything after runs anyway.")
        print()
    else:
        with tempfile.TemporaryDirectory() as temporary:
            url = a_repository_to_read(Path(temporary) / "config")

            print("what a git source reads:")
            one_file_a_list_and_a_directory(url)
            print()

            print("which commit is being served:")
            the_commit_is_the_provenance(url)
            print()

    print("a credential that outlives nothing on its own:")
    a_token_that_lives_an_hour()
    print()

    print("the ways to authenticate:")
    the_other_ways_to_prove_who_is_asking()
    print()

    print("what is refused, and why refusing is the only honest answer:")
    what_is_refused_rather_than_ignored()

    print("`watch()` is not exposed — for git or for any other store in this")
    print("wheel — and git is the one where that is a real loss rather than a")
    print("formality: it is the only store in the family whose multi-file")
    print("sources can be watched at all, because what moves is a ref and what")
    print("a ref names is a commit. It stays unexposed because a Rust callback")
    print("on a Rust thread calling into Python is a second GIL story on top of")
    print("this one, and making git the exception would be that story for one")
    print("store. `refresh_remote()` on a timer is what Python has, and against")
    print("git each tick is one ref advertisement — only a ref that moved costs")
    print("a transfer.")


if __name__ == "__main__":
    main()
