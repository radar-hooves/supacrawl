#!/usr/bin/env python3
"""Map a diff to the members it reaches and print one JSON plan.

Spec: docs/research/2026-10-11-estate-ci-design.md §1.2. Reads git objects only,
never the working tree, so any SHA pair plans without a checkout.
"""

from __future__ import annotations

import argparse
import functools
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# A member's own .md can be a runtime file (a prompt), so only these are ignored.
IGNORED = re.compile(
    r"(docs|\.github|\.vscode)/.*|\.claude/(?!hooks/).*|[^/]*\.md|(.*/)?docs/.*\.md"
    r"|(.*/)?(README\.md|CHANGELOG\.md|CLAUDE\.md|AGENTS\.md|\.gitignore|\.gitattributes"
    r"|LICENSE.*|renovate\.json.*|\.pre-commit-config\.yaml|\.editorconfig|\.mcp\.json)"
)
SMOKE = ".github/image-smoke.sh"
PINS = {".python-version": "uv", ".node-version": "pnpm", ".nvmrc": "pnpm"}
PINS |= {"rust-toolchain": "cargo", "rust-toolchain.toml": "cargo"}
PINS |= {"Cargo.lock": "cargo", ".go-version": "go", "go.work": "go"}
TYPECHECK = {"mypy.ini", "pyrightconfig.json", "tsconfig.base.json"}
PROJECT = {"pyproject.toml", "uv.lock", "package.json", "pnpm-lock.yaml"}
READ = PROJECT - {"pnpm-lock.yaml"} | {".python-version", "pnpm-workspace.yaml"}
READ |= {"go.mod", "Dockerfile", "Cargo.toml"}
SCRIPT = (".gradle", ".gradle.kts")
# A Gradle build's own files, under its settings dir: every member builds against them.
GRADLE = ("settings.gradle*", "build.gradle*", "gradle.properties", "gradle/**")
GRADLE += ("gradlew*", "build-logic/**", "buildSrc/**")
INCLUDE = re.compile(r"\binclude\s*\(?((?:\s*,?\s*[\"'][^\"'\n]*[\"'])+)")
# A string (its text in group 1 or 2, a ${...} template whole), else a comment: strings
# first, so a // or /* inside one is no comment.
CODE = re.compile(
    r"\"((?:[^\"\\\n$]|\\.|\$(?!\{)|\$\{[^}\n]*\})*)\"|'((?:[^'\\\n]|\\.)*)'"
    r"|//[^\n]*|/\*[\s\S]*?\*/"
)
TESTS = re.compile(r"(?:.*/)?src/(?:test|androidTest)/")
APPLY = re.compile(r"\bid\s*\(?\s*[\"']([^\"']+)")
# A precompiled script plugin's id is its file's name, under any package it declares.
PLUGIN = re.compile(
    r"(?:.*/)?src/main/(?:kotlin|groovy)/(?:.*/)?([^/]+)\.gradle(?:\.kts)?"
)
GLOB = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
TMP = tempfile.TemporaryDirectory(prefix="plan-")
TIMES: list[float] = []


def name_of(p: str) -> str:
    return p.rpartition("/")[2]


def dir_of(p: str) -> str:
    return p.rpartition("/")[0]


def join(d: str, f: str) -> str:
    return f"{d}/{f}" if d else f


def within(p: str, d: str) -> bool:
    return not d or p == d or p.startswith(d + "/")


def rel(p: str, d: str) -> str:
    return p[len(d) + 1 :] if d else p


def dig(d: dict, *keys: str) -> dict:
    for k in keys:
        d = d.get(k, {})
    return d


def norm(req: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else ""


def cap(xs: list[str], n: int = 8) -> list[str]:
    return xs[:n] + [f"+{len(xs) - n} more"] * (len(xs) > n)


def repo_name(repo: str) -> str:
    return os.environ.get("GITHUB_REPOSITORY", "").rpartition("/")[2] or name_of(repo)


@functools.cache
def glob_re(pat: str) -> re.Pattern[str]:
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", pat.strip("/").removeprefix("./"))
    return re.compile("".join(GLOB.get(s, re.escape(s)) for s in parts))


def copied(dockerfile: bytes) -> list[str]:
    """The build-context paths a Dockerfile's COPY, ADD and context bind mounts take, up
    to any glob; a path it cannot resolve (a $variable) counts as the whole context."""
    out = []
    for line in re.sub(r"\\\n", " ", dockerfile.decode()).splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].upper() == "RUN":
            for m in re.findall(r"--mount=(\S+)", line):
                opts = dict(o.partition("=")[::2] for o in m.split(","))
                if opts.get("type") == "bind" and "from" not in opts:
                    out.append(opts.get("source") or opts.get("src") or ".")
            continue
        if words[0].upper() not in ("COPY", "ADD"):
            continue
        if any(w.startswith("--from") for w in words):
            continue
        args = " ".join(w for w in words[1:] if not w.startswith("--"))
        srcs = json.loads(args)[:-1] if args.startswith("[") else args.split()[:-1]
        out += [s for s in srcs if "://" not in s]
    return [
        "."
        if "$" in s
        else posixpath.normpath(re.split(r"[*?[]", s)[0].lstrip("/") or ".")
        for s in out
    ]


def glob(p: str, pats) -> bool:
    return any(glob_re(g).fullmatch(p) for g in pats)


def pnpm_globs(text: str) -> list[str]:
    """The `packages:` globs of a pnpm-workspace.yaml, block or flow style."""
    m = re.search(
        r"^packages:[ \t]*(\[[^\]]*\])?[^\n]*\n?((?:[ \t]*(?:-.*|#.*)?\n?)*)",
        text,
        re.M,
    )
    if not m:
        return []
    if m.group(1):
        return re.findall(r"[^\s,'\"\[\]]+", m.group(1))
    return re.findall(r"^\s*-\s*['\"]?([^'\"\s#]+)", m.group(2), re.M)


def parse(raw: bytes) -> dict | str:
    """The TOML, or why it is not TOML."""
    try:
        return tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return str(e)


def git(repo: str, *args: str) -> bytes:
    cmd = ["git", "-C", repo, "--literal-pathspecs", *args]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def resolve(repo: str, ref: str | None) -> str | None:
    """None for no base (absent or all zeros); a ref that names nothing is fatal."""
    if not ref or set(ref) == {"0"}:
        return None
    try:
        return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    except subprocess.CalledProcessError:
        raise SystemExit(f"plan: {ref!r} is not a commit in {repo}") from None


@functools.cache
def tree(repo: str, sha: str) -> frozenset[str]:
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", sha).decode()
    return frozenset(out.split("\0")) - {""}


@functools.cache
def diff(repo: str, base: str, head: str) -> tuple[str, ...]:
    out = git(repo, "diff", "--no-renames", "--name-only", "-z", base, head).decode()
    return tuple(p for p in out.split("\0") if p)


@functools.cache
def manifests(repo: str, sha: str) -> dict[str, bytes]:
    paths = sorted(
        p for p in tree(repo, sha) if name_of(p) in READ or p.endswith(SCRIPT)
    )
    if not paths:
        return {}
    raw = git(repo, "archive", "--format=tar", sha, "--", *paths)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


@functools.cache
def workdir(repo: str, sha: str) -> str:
    root = os.path.join(TMP.name, sha)
    for p, raw in manifests(repo, sha).items():
        if name_of(p) in ("pyproject.toml", "uv.lock", ".python-version"):
            os.makedirs(os.path.join(root, dir_of(p)), exist_ok=True)
            with open(os.path.join(root, p), "wb") as f:
                f.write(raw)
    return root


@functools.cache
def export(
    repo: str, sha: str, lock: str, name: str | None, group: str | None
) -> str | None:
    """uv's own answer to "which third-party set does this member resolve to".

    A package's runtime set with every extra (an image installs some); never the
    root's default groups, which ride along otherwise and moved every member when
    one gained a sibling. A group member compares its group; name=None is the
    lock root's dev group.
    """
    cwd = os.path.join(workdir(repo, sha), lock)
    if not os.path.exists(os.path.join(cwd, "uv.lock")):
        return None
    started = time.monotonic()
    pick = (
        ["--package", name, "--no-default-groups", "--all-extras"]
        if name
        else ["--only-dev"]
    )
    r = subprocess.run(
        ["uv", "export", "--frozen", "--no-hashes", "--no-header", "--no-annotate"]
        + ["--no-emit-workspace", *(["--only-group", group] if group else pick)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "UV_OFFLINE": "1"},
    )
    TIMES.append(time.monotonic() - started)
    return r.stdout if r.returncode == 0 else None


@dataclass(eq=False)
class Member:
    name: str
    path: str
    toolchain: str
    lock: str | None  # the directory of the lockfile it resolves from
    reads: tuple[str, ...] = ()
    # Its dependency groups' too: a test harness is a library.
    deps: frozenset[str] = frozenset()
    runtime: frozenset[str] = frozenset()  # what it ships with
    group: str | None = None  # a lockless excluded member's root dependency group
    library: bool = False
    deploy: str = "none"


def discover(
    repo: str, sha: str, bases: tuple[str, ...] = ()
) -> tuple[list[Member], dict]:
    files, paths = manifests(repo, sha), tree(repo, sha)
    parsed = {
        dir_of(p): parse(v) for p, v in files.items() if name_of(p) == "pyproject.toml"
    }
    tomls = {d: t for d, t in parsed.items() if isinstance(t, dict)}
    locks = {dir_of(p) for p in paths if name_of(p) == "uv.lock"}

    def taken(d: str) -> bool:
        """A workspace root, or a dir a workspace root's members take."""
        for r in locks | {""}:
            ws = dig(tomls.get(r, {}), "tool", "uv", "workspace")
            if d == r or (within(d, r) and glob(rel(d, r), ws.get("members", []))):
                return True
        return False

    # A template is skipped; a member or a workspace root fails the plan, which would
    # otherwise drop it, and with a root every member it names, in silence.
    for d, err in sorted((d, t) for d, t in parsed.items() if isinstance(t, str)):
        where = join(d, "pyproject.toml")
        if taken(d):
            raise SystemExit(
                f"plan: {where} is a member or a workspace root, and not TOML: {err}"
            )
        print(f"plan: skipping unparseable {where}: {err}", file=sys.stderr)
    found: dict[str, Member] = {}

    def uv(d: str, lock: str | None, excluded: bool = False) -> Member:
        proj, change = tomls[d].get("project", {}), dig(tomls[d], "tool", "change")
        reqs = [*proj.get("dependencies", [])]
        reqs += sum(proj.get("optional-dependencies", {}).values(), [])
        runtime = frozenset(map(norm, reqs))
        reqs += [
            r
            for g in tomls[d].get("dependency-groups", {}).values()
            for r in g
            if isinstance(r, str)
        ]
        reads = change.get("reads", [])
        reads = (reads,) if isinstance(reads, str) else tuple(reads)
        name = proj.get("name") or name_of(d) or repo_name(repo)
        group = change.get("group") if excluded else None
        lock = lock if group or not excluded else None
        return Member(
            name, d, "uv", lock, reads, frozenset(map(norm, reqs)), runtime, group
        )

    # Deepest lock first, so a nested project with its own lock owns its directory.
    for root in sorted(locks | {""}, key=len, reverse=True):
        cfg, lock = tomls.get(root, {}), root if root in locks else None
        ws = dig(cfg, "tool", "uv", "workspace")
        for d in tomls:
            if (
                d != root
                and within(d, root)
                and glob(rel(d, root), ws.get("members", []))
            ):
                # An excluded dir (mcp-servers' embedded images) is still a member.
                found.setdefault(
                    d, uv(d, lock, glob(rel(d, root), ws.get("exclude", [])))
                )
        if "project" in cfg and (lock is not None or not root):
            found.setdefault(root, uv(root, lock))

    for root in sorted(dir_of(p) for p in paths if name_of(p) == "pnpm-lock.yaml"):
        if join(root, "package.json") not in files:
            continue
        dirs = [root]
        # A settings-only pnpm-workspace.yaml (no `packages:`) leaves the root the member.
        if pats := pnpm_globs(
            files.get(join(root, "pnpm-workspace.yaml"), b"").decode()
        ):
            keep = [g for g in pats if g[0] != "!"]
            drop = [g[1:] for g in pats if g[0] == "!"]
            pkgs = [dir_of(f) for f in files if name_of(f) == "package.json"]
            dirs = [d for d in pkgs if d != root and within(d, root)]
            dirs = [
                d
                for d in dirs
                if glob(rel(d, root), keep) and not glob(rel(d, root), drop)
            ]
        for d in dirs:
            pkg = json.loads(files[join(d, "package.json")])
            found.setdefault(
                d,
                Member(
                    pkg.get("name") or name_of(d) or repo_name(repo), d, "pnpm", root
                ),
            )

    for p in sorted(p for p in paths if name_of(p) == "go.mod"):
        d = dir_of(p)
        mod = re.search(r"^module\s+\"?([^\s\"]+)", files[p].decode(), re.M)
        last = name_of(re.sub(r"/v\d+$", "", mod.group(1))) if mod else ""
        found.setdefault(d, Member(last or name_of(d) or repo_name(repo), d, "go", d))

    # A Cargo workspace root, or a crate no workspace above it takes, owns all below it.
    crates = {
        dir_of(p): parse(v) for p, v in files.items() if name_of(p) == "Cargo.toml"
    }
    spaces = [d for d, t in crates.items() if isinstance(t, dict) and "workspace" in t]
    for d, t in sorted(crates.items()):
        top = not any(within(d, w) for w in spaces)
        if isinstance(t, str) and top:
            raise SystemExit(
                f"plan: {join(d, 'Cargo.toml')} is a member, and not TOML: {t}"
            )
        if isinstance(t, dict) and ("workspace" in t or ("package" in t and top)):
            # A Tauri crate shares its frontend's name, so a taken name falls to the dir's.
            name = dig(t, "package").get("name")
            if not name or name in {m.name for m in found.values()}:
                name = name_of(d) or repo_name(repo)
            found.setdefault(d, Member(name, d, "cargo", d))

    # A Gradle build's members are its includes, at the dirs Gradle gives them by default,
    # and its included builds, whose own includes are none.
    texts = {
        p: CODE.sub(
            lambda c: c[0] if c[0][0] in "\"'" else "", v.decode(errors="replace")
        )
        for p, v in files.items()
        if p.endswith(SCRIPT)
    }
    settings = sorted(p for p in texts if name_of(p).startswith("settings.gradle"))
    builds = r"\bincludeBuild\(\s*[\"']([^\"']+)"
    nested = {
        posixpath.normpath(join(dir_of(p), b)): dir_of(p)
        for p in settings
        for b in ("build-logic", "buildSrc", *re.findall(builds, texts[p]))
    }
    plugins = {}
    for p, text in texts.items():
        top = max((n for n in nested if within(p, n)), key=len, default=None)
        if top and (m := PLUGIN.fullmatch(p)):
            pkg = re.search(r"(?m)^\s*package\s+([\w.]+)", text)
            plugins[f"{pkg[1]}.{m[1]}" if pkg else m[1]] = (text, top)

    def applied(text: str) -> dict[str, tuple[str, str]]:
        """The convention plugins a script applies, through the plugins they apply."""
        seen, todo = {}, [text]
        while todo:
            for i in APPLY.findall(todo.pop()):
                if i in plugins and i not in seen:
                    seen[i] = plugins[i]
                    todo.append(plugins[i][0])
        return seen

    # An input deleted or moved at head still exists at a base.
    known = paths.union(*(tree(repo, b) for b in bases if settings))
    dirs = (
        {p[:i] for p in known for i, c in enumerate(p) if c == "/"} if settings else ()
    )
    for p in settings:
        root, text = dir_of(p), texts[p]
        # Named by its path, since two builds may each include a build-logic; one included
        # from outside its parent's tree has a leg that cannot find the parent.
        parent = nested.get(root)
        if parent is not None and within(root, parent):
            found.setdefault(
                root, Member(root.replace("/", "-"), root, "gradle", parent)
            )
        elif parent is not None:
            print(f"plan: skipping {root}, included from outside it", file=sys.stderr)
        if any(within(root, n) for n in nested):
            continue
        if re.search(r"\bprojectDir\s*=(?!=)", text):
            raise SystemExit(
                f"plan: {p} moves a projectDir, which the plan does not follow"
            )
        incs = [
            g.lstrip(":")
            for a in INCLUDE.findall(text)
            for g in re.findall(r"[\"']([^\"']*)[\"']", a)
        ]
        # The typesafe accessor: projects.core.designSystem is :core:design-system.
        acc = {
            re.sub(r"[-_]+(\w)", lambda c: c[1].upper(), g.replace(":", ".")): g
            for g in incs
        }
        for g in incs:
            d = join(root, g.replace(":", "/"))
            f = join(d, "build.gradle")
            text = texts.get(f) or texts.get(f + ".kts", "")
            conv = applied(text).values()
            text = "\n".join([text, *(t for t, _ in conv)])
            # Its inputs: its build's included builds, and any path outside it that a literal
            # (no URL or coordinate) names from it, its build's root or a plugin's build.
            near = {n for n, r in nested.items() if r == root} | {
                posixpath.normpath(join(b, s))
                for c in CODE.finditer(text)
                if (s := c[1] or c[2]) and ":" not in s
                for b in {d, root, *(top for _, top in conv)}
            }
            # A CMakeLists.txt stands for its CMake project: the dir it builds.
            near = {dir_of(t) if name_of(t) == "CMakeLists.txt" else t for t in near}
            reads = tuple(
                t + "/**" * (t in dirs)
                for t in sorted(near)
                if (t in known or t in dirs)
                and not (within(t, d) or within(d, t))
                and not (
                    within(t, root) and glob(rel(t, root) + "/" * (t in dirs), GRADLE)
                )
            )
            uses = re.findall(
                r"\bproject\(\s*(?:path\s*[=:]\s*)?[\"']:?([^\"']+)", text
            )
            uses += [
                i
                for a, i in acc.items()
                if re.search(rf"\bprojects\.{re.escape(a)}(?!\.?\w)", text)
            ]
            deps = frozenset(norm(u.replace(":", "-")) for u in uses)
            found.setdefault(
                d, Member(g.replace(":", "-"), d, "gradle", root, reads, deps)
            )

    members = sorted(found.values(), key=lambda m: m.path)
    names = [m.name for m in members]
    twins = [f"{m.name} ({m.path or '.'})" for m in members if names.count(m.name) > 1]
    if twins:
        raise SystemExit(f"plan: members share a name, which keys a leg: {twins}")
    sources = [
        s for c in tomls.values() for s in dig(c, "tool", "uv", "sources").items()
    ]
    sourced = {
        norm(n) for n, s in sources if isinstance(s, dict) and s.get("workspace")
    }
    change = dig(tomls.get("", {}), "tool", "change")
    for m in members:
        key = norm(m.name)
        used = any(key in o.deps for o in members if o is not m)
        m.library = m.toolchain == "uv" and key in sourced and used
        if m.path and join(m.path, "Dockerfile") in paths:
            m.deploy = "image"
        elif join(m.path, "server_config.json") in paths:
            m.deploy = "mcp"
        elif m.toolchain == "uv" and change.get("publish") == "wheels":
            m.deploy = "wheel"
    return members, change


def plan(repo: str, head: str, bases: list[str], roll: str = "") -> dict:
    repo = os.path.abspath(repo)
    if not (head := resolve(repo, head)):
        raise SystemExit(f"plan: head is not a commit in {repo}")
    own = {
        k: resolve(repo, v) for k, _, v in (b.partition("=") for b in bases if "=" in b)
    }
    default = resolve(repo, next((b for b in reversed(bases) if "=" not in b), None))
    members, change = discover(
        repo, head, tuple(b for b in {default, *own.values()} if b)
    )
    names = {m.name for m in members}
    rolled = {r.strip() for r in roll.split(",") if r.strip()}
    rolled = names if rolled == {"all"} else rolled
    if rolled - names:
        raise SystemExit(f"plan: --roll names no member: {sorted(rolled - names)}")
    by_depth = sorted(members, key=lambda m: -len(m.path))
    lock_dirs = {m.lock for m in members if m.lock is not None} | {""}

    def owner(p: str) -> Member | None:
        return next((m for m in by_depth if within(p, m.path)), None)

    def tests_only(p: str, o: Member | None) -> bool:
        """p is in the tests of o, a Gradle member: no change its users build against."""
        return bool(o and o.toolchain == "gradle" and TESTS.match(rel(p, o.path)))

    def build_wide(p: str, m: Member) -> bool:
        return (
            m.toolchain == "gradle"
            and within(p, m.lock)
            and glob(rel(p, m.lock), GRADLE)
            and not tests_only(p, owner(p))
        )

    def used(m: Member) -> list[Member]:
        """The Gradle members m uses, transitively: its build compiles their source."""
        build = {
            norm(o.name): o
            for o in members
            if o.toolchain == "gradle" and o.lock == m.lock
        }
        seen, todo = [m], [m]
        while todo:
            new = [o for k in todo.pop().deps if (o := build.get(k)) and o not in seen]
            seen, todo = seen + new, todo + new
        return seen[1:]

    def repo_level(p: str) -> bool:
        """A non-ignored path outside every member that no other rule consumes."""
        n = name_of(p)
        consumed = (
            n in PINS or n in TYPECHECK or (n in PROJECT and dir_of(p) in lock_dirs)
        ) or any(build_wide(p, m) for m in members)
        return not (IGNORED.fullmatch(p) or owner(p) or consumed or p == "Dockerfile")

    def lock_touched(m: Member, paths: tuple[str, ...]) -> bool:
        if m.toolchain != "uv" or m.lock is None:
            return False
        lock = join(m.lock, "uv.lock")
        return any(
            p == lock or (name_of(p) == "pyproject.toml" and within(p, m.lock))
            for p in paths
        )

    @functools.cache
    def settings_moved(d: str, base: str) -> bool:
        """Workspace root d's pyproject.toml moved in what neither the lock nor a root
        member's own leg answers for: its tool settings and uv's default groups."""

        def rest(sha: str) -> dict | str:
            t = parse(manifests(repo, sha).get(join(d, "pyproject.toml"), b""))
            if isinstance(t, dict):
                for k in ("project", "build-system", "dependency-groups"):
                    t.pop(k, None)
                tool = dig(t, "tool")
                # uv.lock does not record default-groups, yet every leg's sync installs them.
                uv = tool.pop("uv", {})
                if "default-groups" in uv:
                    tool["uv"] = {"default-groups": uv["default-groups"]}
            return t

        return rest(base) != rest(head)

    def package_manager(sha: str) -> str | None:
        raw = manifests(repo, sha).get("package.json")
        return json.loads(raw).get("packageManager") if raw else None

    @functools.cache
    def newer(base: str | None) -> bool:
        """The live artefact's commit descends from head: shipping head rolls it back."""
        if base is None or base == head:
            return False
        cmd = ["git", "-C", repo, "merge-base", "--is-ancestor", head, base]
        return subprocess.run(cmd, capture_output=True).returncode == 0

    @functools.cache
    def changed_in(base: str) -> frozenset[Member]:
        """The members a diff changes for their users: an owned path outside its tests, or an
        input it reads (a CMake source, a codegen file), which is built into it."""
        paths = diff(repo, base, head)
        owned = {
            o
            for p in paths
            if (o := owner(p)) and not IGNORED.fullmatch(p) and not tests_only(p, o)
        }
        return frozenset(
            owned | {o for o in members if any(glob(p, o.reads) for p in paths)}
        )

    @functools.cache
    def evaluate(m: Member, base: str | None) -> tuple[bool, bool, tuple[str, ...]]:
        """(test, ship, reasons) for one member against one base."""
        if base is None:
            # Deploy only on positive evidence: no base tests all, or only the roll.
            return (False, False, ()) if rolled else (True, False, ("no base",))
        paths = diff(repo, base, head)
        lock = join(m.lock, "uv.lock") if m.lock is not None else None
        mine = [
            p for p in paths if not IGNORED.fullmatch(p) and owner(p) is m and p != lock
        ]
        ship = any(not within(p, join(m.path, "tests")) for p in mine)
        pins = [
            p for p in paths if PINS.get(name_of(p)) == m.toolchain and p not in mine
        ]
        why = [f"pin {p}" for p in pins if within(m.path, dir_of(p))]
        tests = [f"reads {p}" for p in cap([p for p in paths if glob(p, m.reads)])]
        if m.toolchain == "pnpm":
            # pnpm has no lock-aware selector, so a workspace lock marks every package.
            if m.lock != m.path and join(m.lock, "pnpm-lock.yaml") in paths:
                why.append(f"lock {join(m.lock, 'pnpm-lock.yaml')}")
            moved = package_manager(base) != package_manager(head)
            if moved and owner("package.json") is not m:
                why.append("package.json packageManager")
        if m.toolchain == "gradle":
            wide = [p for p in paths if build_wide(p, m) and not IGNORED.fullmatch(p)]
            touched = changed_in(base)
            tests += [f"build {p}" for p in cap(wide)]
            tests += cap(sorted(f"uses {u.name}" for u in used(m) if u in touched))
        if m.toolchain in ("cargo", "gradle"):
            # Both legs run inside the repo root's flake shell.
            tests += [f"flake {p}" for p in paths if p in ("flake.nix", "flake.lock")]
        if lock_touched(m, paths):
            old, new = (export(repo, s, m.lock, m.name, m.group) for s in (base, head))
            if old is None or new is None:
                why.append(
                    f"lock: export failed at {'base' if old is None else 'head'}"
                )
            elif old != new:
                why.append(
                    f"lock: {f'group {m.group}' if m.group else 'runtime set'} moved"
                )
            # A workspace setting (pytest, mypy, tool.change) tests it, ships nothing.
            conf = join(m.lock, "pyproject.toml")
            if conf in paths and conf not in mine and settings_moved(m.lock, base):
                tests.append(f"{conf} settings moved")
            # The dev toolchain is tested with every member but deployed with none.
            if not m.group and export(repo, base, m.lock, None, None) != export(
                repo, head, m.lock, None, None
            ):
                tests.append("lock: dev group moved")
        test, ship = bool(mine or why or tests), (ship or bool(why)) and not newer(base)
        held = ("base descends from head: not shipped",) if test and newer(base) else ()
        return test, ship, (*cap(mine), *why, *tests, *held)

    artefacts = []
    if "Dockerfile" in tree(repo, head):
        # The root image holds the members its Dockerfile copies from the context.
        srcs = copied(manifests(repo, head)["Dockerfile"])
        image = change.get("image") or repo_name(repo)
        held = [
            m
            for m in members
            if m.deploy not in ("image", "mcp")
            and any(s == "." or within(s, m.path) or within(m.path, s) for s in srcs)
        ]
        artefacts.append((image, held))
    base_for = {m: own.get(m.name, default) for m in members}
    for image, ms in artefacts:
        base_for |= {m: own[image] for m in ms if image in own and m.name not in own}

    TIMES.clear()
    started = time.monotonic()
    jobs = {
        (b, m.lock, m.name, m.group)
        for m, b in base_for.items()
        if b and lock_touched(m, diff(repo, b, head))
    }
    jobs |= {(b, lock, None, None) for b, lock, _, group in jobs if not group}
    jobs |= {(head, *rest) for _, *rest in jobs}
    for sha in {j[0] for j in jobs}:
        workdir(repo, sha)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda j: export(repo, *j), jobs))
    wall = time.monotonic() - started

    out, reasons, shipped = [], {}, set()
    for m in members:
        test, ship, why = evaluate(m, base_for[m])
        if m.name in rolled:
            test, ship, why = True, not newer(base_for[m]), (*why, "roll")
        if why:
            reasons[m.name] = list(why)
        if ship:
            shipped.add(m.name)
        row = {
            k: getattr(m, k) for k in ("name", "path", "toolchain", "library", "deploy")
        }
        out.append(
            row
            | {
                "test": test,
                "deploy_now": ship and m.deploy != "none",
                "base": base_for[m],
                "dependents": [
                    o.name
                    for o in members
                    if o is not m and o.lock == m.lock and norm(m.name) in o.deps
                ],
                # The members that ship it, so ship only once it passed. Never a dependency
                # group's user: mcp-common's tests mount servers that use it, so each waited.
                "carried_by": [
                    o.name
                    for o in members
                    if o is not m and o.lock == m.lock and norm(m.name) in o.runtime
                ],
            }
        )

    out_artefacts = []
    for image, ms in artefacts:
        base = own.get(image, default)
        why = [m.name for m in ms if m.name in rolled]
        if base is not None:
            paths = diff(repo, base, head)
            hits = [
                p
                for p in paths
                if p in (SMOKE, "Dockerfile")
                or (repo_level(p) and not within(p, "tests"))
            ]
            why = cap(hits) + [
                m.name for m in ms if m.name in rolled or evaluate(m, base)[1]
            ]
        build = bool(why) and not newer(base)
        if why:
            reasons[f"image:{image}"] = (
                why if build else [*why, "base descends from head: not built"]
            )
        out_artefacts.append(
            {"image": image, "members": [m.name for m in ms], "build": build}
        )

    paths = diff(repo, default, head) if default else ()
    repo_paths = (
        [p for p in paths if repo_level(p)]
        if default
        else ([] if rolled else ["no base"])
    )
    typecheck = [p for p in paths if name_of(p) in TYPECHECK and owner(p) is None]
    if repo_paths:
        reasons["repo_suites"] = cap(repo_paths)
    if typecheck:
        reasons["typecheck_all"] = typecheck
    legs = [o["name"] for o in out if o["test"]]
    print(
        f"plan: {len(TIMES)} uv exports, {sum(TIMES):.2f}s summed, {wall:.2f}s wall",
        file=sys.stderr,
    )
    return {
        "members": out,
        "legs": legs,
        "libraries": [m.name for m in members if m.library and m.name in shipped],
        "artefacts": out_artefacts,
        "repo_suites": bool(repo_paths),
        "typecheck_all": bool(typecheck),
        "changed_members": ",".join(legs),
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--base", action="append", default=[], help="SHA, or member=SHA")
    ap.add_argument("--roll", default="", help="'all' or a comma list of member names")
    args = ap.parse_args(argv)
    print(json.dumps(plan(args.repo, args.head, args.base, args.roll)))


if __name__ == "__main__":
    main()
