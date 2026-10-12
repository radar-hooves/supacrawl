"""Planner cases on throwaway git repos. Needs git and uv on PATH."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import plan as planner  # noqa: E402

ENV = {
    **os.environ,
    "UV_OFFLINE": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        args, cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def commit(repo: Path, msg: str = "c") -> str:
    run(repo, "git", "add", "-A")
    run(repo, "git", "commit", "-q", "--allow-empty", "-m", msg)
    return run(repo, "git", "rev-parse", "HEAD")


def init(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir()
    run(repo, "git", "init", "-q", "-b", "main")
    write(repo, files)


def wheel(dist: Path, version: str, name: str = "dep") -> None:
    dist.mkdir(exist_ok=True)
    info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(dist / f"{name}-{version}-py3-none-any.whl", "w") as z:
        z.writestr(
            f"{info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        z.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        z.writestr(f"{info}/RECORD", "")


def lock(repo: Path, dist: Path, *extra: str) -> None:
    run(repo, "uv", "lock", "--no-index", "--find-links", str(dist), *extra)


def bump(repo: Path, dist: Path, name: str) -> str:
    wheel(dist, "2.0", name)
    lock(repo, dist, "--upgrade-package", name)
    return commit(repo)


def project(name: str, *deps: str, extra: str = "") -> str:
    return f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = {list(deps)!r}\n{extra}'


ROOT = """[tool.uv.workspace]
members = ["packages/*"]
exclude = ["packages/emb"]
[tool.uv]
default-groups = ["dev", "clients"]
[tool.uv.sources]
lib = { workspace = true }
ext = { path = "ext", editable = true }
[dependency-groups]
dev = ["tool"]
clients = [CLIENTS]
embedded = ["edep"]
"""
B_EXTRA = '[project.optional-dependencies]\nstealth = ["xdep"]\n[tool.change]\nreads = "packages/*/server_config.json"\n'


@pytest.fixture(autouse=True)
def no_github_repository(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


@pytest.fixture
def mono(tmp_path: Path) -> tuple[Path, str]:
    """A uv workspace shaped like mcp-servers: a library, two MCP servers, one embedded image."""
    repo, dist = tmp_path / "mono", tmp_path / "dist"
    for name in ("dep", "tool", "edep", "xdep"):
        wheel(dist, "1.0", name)
    init(
        repo,
        {
            "pyproject.toml": ROOT.replace("CLIENTS", ""),
            "ext/pyproject.toml": project("ext"),
            ".python-version": "3.12\n",
            "packages/lib/pyproject.toml": project("lib"),
            "packages/lib/lib.py": "x = 1\n",
            "packages/a/pyproject.toml": project("a", "lib", "dep"),
            "packages/a/server_config.json": "{}\n",
            "packages/a/a.py": "x = 1\n",
            "packages/a/prompt.md": "p\n",
            "packages/a/tests/test_a.py": "x = 1\n",
            "packages/b/pyproject.toml": project("b", "lib", extra=B_EXTRA),
            "packages/b/server_config.json": "{}\n",
            "packages/b/b.py": "x = 1\n",
            "packages/emb/pyproject.toml": project(
                "emb", extra='[tool.change]\ngroup = "embedded"\n'
            ),
            "packages/emb/Dockerfile": "FROM scratch\n",
            "tools/x.py": "x = 1\n",
            "README.md": "r\n",
        },
    )
    lock(repo, dist)
    return repo, commit(repo)


@pytest.fixture
def app(tmp_path: Path) -> tuple[Path, str]:
    """A stamped app: backend/ (uv, own lock), frontend/ (pnpm), one root image."""
    repo, dist = tmp_path / "godswood", tmp_path / "dist"
    wheel(dist, "1.0")
    wheel(dist, "1.0", "tool")
    init(
        repo,
        {
            "Dockerfile": 'FROM scratch AS f\nCOPY frontend/src \\\n  /f\nFROM scratch\nCOPY --chown=1:1 ["backend/", "/b"]\nCOPY --from=f /f /f\n',
            "cli/go.mod": "module github.com/x/godswood/cli/v2\n\ngo 1.25.0\n",
            "cli/main.go": "package main\n",
            ".github/image-smoke.sh": "true\n",
            "backend/pyproject.toml": project(
                "godswood", "dep", extra='[dependency-groups]\ndev = ["tool"]\n'
            ),
            "backend/app.py": "x = 1\n",
            "frontend/package.json": '{"name": "frontend", "packageManager": "pnpm@10.0.0"}\n',
            "frontend/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "frontend/src/x.ts": "1\n",
            "config/x.yaml": "a: 1\n",
        },
    )
    lock(repo / "backend", dist)
    return repo, commit(repo)


def change(
    repo: Path, files: dict[str, str] | None = None, delete: tuple[str, ...] = ()
) -> str:
    write(repo, files or {})
    for path in delete:
        run(repo, "git", "rm", "-q", path)
    return commit(repo)


def plan(repo: Path, head: str, *bases: str, roll: str = "") -> dict:
    return planner.plan(str(repo), head, list(bases), roll)


def member(p: dict, name: str) -> dict:
    return next(m for m in p["members"] if m["name"] == name)


def shipped(p: dict) -> list[str]:
    return [m["name"] for m in p["members"] if m["deploy_now"]]


def test_discovery(mono, app):
    p = plan(mono[0], mono[1], mono[1])
    assert {
        m["name"]: (m["path"], m["deploy"], m["library"]) for m in p["members"]
    } == {
        "lib": ("packages/lib", "none", True),
        "a": ("packages/a", "mcp", False),
        "b": ("packages/b", "mcp", False),
        "emb": ("packages/emb", "image", False),
    }
    p = plan(app[0], app[1], app[1])
    assert [(m["name"], m["toolchain"]) for m in p["members"]] == [
        ("godswood", "uv"),
        ("cli", "go"),
        ("frontend", "pnpm"),
    ]
    assert p["artefacts"] == [
        {"image": "godswood", "members": ["godswood", "frontend"], "build": False}
    ]


def test_image_name_from_github_repository(app, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "radar-hooves/godswood-app")
    assert plan(app[0], app[1], app[1])["artefacts"][0]["image"] == "godswood-app"


def test_pnpm_workspace_without_packages_keeps_the_root(tmp_path):
    repo = tmp_path / "web"
    settings = "onlyBuiltDependencies:\n  - esbuild\n"
    init(
        repo,
        {
            "package.json": '{"name": "web"}',
            "pnpm-lock.yaml": "",
            "pnpm-workspace.yaml": settings,
        },
    )
    head = commit(repo)
    assert [m["name"] for m in plan(repo, head, head)["members"]] == ["web"]
    flow = "packages: ['apps/*', \"!apps/skip\"]\n"
    write(
        repo, {"pnpm-workspace.yaml": flow, "apps/one/package.json": '{"name": "one"}'}
    )
    write(repo, {"apps/skip/package.json": '{"name": "skip"}'})
    head = commit(repo)
    assert [m["name"] for m in plan(repo, head, head)["members"]] == ["one"]


def test_one_member_edit(mono):
    repo, base = mono
    p = plan(repo, change(repo, {"packages/a/a.py": "x = 2\n"}), base)
    assert p["legs"] == ["a"] and shipped(p) == ["a"] and not p["repo_suites"]


def test_a_members_markdown_is_runtime_but_docs_are_not(mono):
    repo, base = mono
    mid = change(repo, {"packages/a/prompt.md": "q\n"})
    assert shipped(plan(repo, mid, base)) == ["a"]
    p = plan(
        repo,
        change(
            repo,
            {
                "NOTES.md": "n\n",
                "packages/b/CLAUDE.md": "c\n",
                "packages/b/README.md": "r\n",
            },
        ),
        mid,
    )
    assert p["legs"] == [] and not p["repo_suites"]


def test_tests_only_edit(mono):
    repo, base = mono
    p = plan(repo, change(repo, {"packages/a/tests/test_a.py": "x = 2\n"}), base)
    assert p["legs"] == ["a"] and shipped(p) == []


def test_deletion(mono):
    repo, base = mono
    p = plan(repo, change(repo, delete=("packages/b/b.py",)), base)
    assert p["legs"] == ["b"] and shipped(p) == ["b"]


def test_rename_across_members(mono):
    repo, base = mono
    run(repo, "git", "mv", "packages/a/a.py", "packages/b/moved.py")
    assert plan(repo, commit(repo), base)["legs"] == ["a", "b"]


def test_merge_commit_plans_against_first_parent(mono):
    repo, base = mono
    run(repo, "git", "checkout", "-q", "-b", "side")
    change(repo, {"packages/b/b.py": "x = 2\n"})
    run(repo, "git", "checkout", "-q", "main")
    first_parent = change(repo, {"packages/a/a.py": "x = 2\n"})
    run(repo, "git", "merge", "-q", "--no-ff", "-m", "merge", "side")
    assert plan(repo, run(repo, "git", "rev-parse", "HEAD"), first_parent)["legs"] == [
        "b"
    ]


def test_third_party_lock_bump_marks_only_its_member(mono, tmp_path):
    repo, base = mono
    head = bump(repo, tmp_path / "dist", "dep")
    assert planner.diff(str(repo), base, head) == ("uv.lock",)
    p = plan(repo, head, base)
    assert p["legs"] == ["a"] and shipped(p) == ["a"]
    assert p["reasons"]["a"] == ["lock: runtime set moved"] and not p["repo_suites"]


def test_an_extra_bump_marks_its_member(mono, tmp_path):
    repo, base = mono
    p = plan(repo, bump(repo, tmp_path / "dist", "xdep"), base)
    assert p["legs"] == ["b"] and shipped(p) == ["b"]


def test_an_excluded_members_group_bump_marks_it(mono, tmp_path):
    # mcp-servers b0d1f6216: supacrawl's image installs the root `embedded` group.
    repo, base = mono
    p = plan(repo, bump(repo, tmp_path / "dist", "edep"), base)
    assert p["legs"] == ["emb"] and shipped(p) == ["emb"]
    assert p["reasons"]["emb"] == ["lock: group embedded moved"]


def test_dev_group_bump_tests_every_member_and_deploys_none(mono, tmp_path):
    repo, base = mono
    p = plan(repo, bump(repo, tmp_path / "dist", "tool"), base)
    assert p["legs"] == ["a", "b", "lib"] and shipped(p) == [] and p["libraries"] == []


def test_root_group_gaining_a_sibling_marks_nothing(mono, tmp_path):
    # mcp-servers 46188d49e: a new api-client in the root `clients` group.
    repo, base = mono
    write(repo, {"pyproject.toml": ROOT.replace("CLIENTS", '"ext"')})
    lock(repo, tmp_path / "dist")
    p = plan(repo, commit(repo), base)
    assert p["legs"] == [] and not p["repo_suites"]


def test_a_members_own_lock_goes_through_the_export(app, tmp_path):
    repo, base = app
    mid = bump(repo / "backend", tmp_path / "dist", "dep")
    p = plan(repo, mid, base)
    assert (
        p["reasons"]["godswood"] == ["lock: runtime set moved"]
        and p["artefacts"][0]["build"]
    )
    p = plan(repo, bump(repo / "backend", tmp_path / "dist", "tool"), mid)
    assert (
        p["legs"] == ["godswood"]
        and shipped(p) == []
        and not p["artefacts"][0]["build"]
    )


def test_python_version_marks_every_uv_member(mono):
    repo, base = mono
    p = plan(repo, change(repo, {".python-version": "3.13\n"}), base)
    assert p["legs"] == ["a", "b", "emb", "lib"] and not p["repo_suites"]


def test_image_smoke_builds_image_and_tests_nothing(app):
    repo, base = app
    p = plan(repo, change(repo, {".github/image-smoke.sh": "false\n"}), base)
    assert p["legs"] == [] and p["artefacts"][0]["build"]


def test_app_member_and_repo_paths_build_the_image(app):
    repo, base = app
    mid = change(repo, {"frontend/src/x.ts": "2\n"})
    p = plan(repo, mid, base)
    assert p["legs"] == ["frontend"] and p["artefacts"][0]["build"]
    p = plan(repo, change(repo, {"config/x.yaml": "a: 2\n"}), mid)
    assert p["legs"] == [] and p["repo_suites"] and p["artefacts"][0]["build"]


def test_a_go_member_stays_out_of_an_image_that_does_not_copy_it(app):
    repo, base = app
    mid = change(repo, {"cli/main.go": "package main // 2\n"})
    p = plan(repo, mid, base)
    assert p["legs"] == ["cli"] and not p["artefacts"][0]["build"]
    assert (member(p, "cli")["toolchain"], member(p, "cli")["deploy"]) == ("go", "none")
    assert p["artefacts"][0]["members"] == ["godswood", "frontend"]
    p = plan(repo, change(repo, {"backend/app.py": "x = 2\n"}), mid)
    assert p["legs"] == ["godswood"] and p["artefacts"][0]["build"]


def test_copied_reads_the_context_paths_a_dockerfile_takes():
    text = b"FROM x\nCOPY . .\nADD https://e/x /x\nCOPY --from=a /a /a\nCOPY ./src/*.py \\\n  pyproject.toml /app/\n"
    assert planner.copied(text) == [".", "src", "pyproject.toml"]


def test_go_version_and_workspace_mark_every_go_member(app):
    repo, prev = app
    for pin in (".go-version", "go.work"):
        head = change(repo, {pin: "1\n"})
        p = plan(repo, head, prev)
        assert p["legs"] == ["cli"] and not p["repo_suites"], pin
        prev = head


def crate(name: str) -> str:
    return f'[package]\nname = "{name}"\nversion = "0.1.0"\n'


@pytest.fixture
def rust(tmp_path: Path) -> tuple[Path, str]:
    """A Cargo workspace, a standalone crate, and a Tauri app whose crate shares its name."""
    repo = tmp_path / "rust"
    init(
        repo,
        {
            "engine/Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
            "engine/Cargo.lock": "",
            "engine/crates/core/Cargo.toml": crate("core"),
            "engine/crates/core/src/lib.rs": "",
            "cli/Cargo.toml": crate("cli"),
            "cli/Cargo.lock": "",
            "desk/package.json": '{"name": "desk"}',
            "desk/pnpm-lock.yaml": "",
            "desk/src-tauri/Cargo.toml": crate("desk"),
        },
    )
    return repo, commit(repo)


def test_cargo_members_are_workspaces_and_standalone_crates(rust):
    repo, base = rust
    p = plan(repo, base, base)
    assert [(m["name"], m["path"], m["toolchain"]) for m in p["members"]] == [
        ("cli", "cli", "cargo"),
        ("desk", "desk", "pnpm"),
        ("src-tauri", "desk/src-tauri", "cargo"),
        ("engine", "engine", "cargo"),
    ]
    assert {m["deploy"] for m in p["members"]} == {"none"}
    p = plan(repo, change(repo, {"engine/crates/core/src/lib.rs": "//\n"}), base)
    assert p["legs"] == ["engine"]


def test_a_cargo_lock_or_toolchain_marks_the_crates_under_it(rust):
    repo, base = rust
    mid = change(repo, {"cli/Cargo.lock": "#\n"})
    p = plan(repo, mid, base)
    assert p["legs"] == ["cli"] and p["reasons"]["cli"] == ["cli/Cargo.lock"]
    p = plan(repo, change(repo, {"rust-toolchain.toml": "[toolchain]\n"}), mid)
    assert p["legs"] == ["cli", "src-tauri", "engine"] and not p["repo_suites"]
    assert p["reasons"]["engine"] == ["pin rust-toolchain.toml"]


SETTINGS = """rootProject.name = "droid"
includeBuild("build-logic")
includeBuild("tooling")
include(":core:design-system", ":core:data")
include(":apps:notes", ":apps:clock")
// include(":apps:old")
println(rootProject.projectDir)
"""
CONVENTION = "build-logic/convention/src/main/kotlin"
MODULES = ["apps-clock", "apps-notes", "core-data", "core-design-system"]
DROID = [
    "apps-clock",
    "apps-notes",
    "build-logic",
    "core-data",
    "core-design-system",
    "tooling",
]


@pytest.fixture
def droid(tmp_path: Path) -> tuple[Path, str]:
    """A Gradle build shaped like rootstock: two apps over two core modules, build-logic's
    convention plugins (one reading a root input, one adding a dependency), a root input."""
    repo = tmp_path / "droid"
    init(
        repo,
        {
            "settings.gradle.kts": SETTINGS,
            "gradle/libs.versions.toml": "",
            "build-logic/settings.gradle.kts": 'rootProject.name = "logic"\ninclude(":convention")\n',
            "build-logic/convention/build.gradle.kts": "",
            "build-logic/src/x.kt": "",
            f"{CONVENTION}/base.gradle.kts": 'package droid\nval t = rootProject.file("design/tokens.json")\n',
            f"{CONVENTION}/droid.app.gradle.kts": 'plugins { id("droid.base") }\ndependencies { add("implementation", project(":core:data")) }\n',
            "tooling/settings.gradle": "include ':gen'\n",
            "tooling/gen/G.kt": "",
            "design/tokens.json": "{}\n",
            "schema/notes.json": "{}\n",
            "native/CMakeLists.txt": "add_subdirectory(src)\n",
            "native/src/a.cpp": "",
            "core/data/build.gradle.kts": 'android { externalNativeBuild { cmake { path = file("../../native/CMakeLists.txt") } } }\n',
            "core/data/src/D.kt": "",
            "core/design-system/build.gradle.kts": 'dependencies { api(project(":core:data")) }\n',
            "apps/notes/build.gradle.kts": 'val n = "it\'s ${uri("https://x.invalid")}"; val s = file("../../schema") // "design/tokens.json"\ndependencies { implementation(projects.core.designSystem) }\n',
            "apps/notes/src/N.kt": "",
            "apps/clock/build.gradle.kts": 'plugins { id("droid.app") }\n',
        },
    )
    return repo, commit(repo)


def test_gradle_includes_are_members(droid):
    repo, base = droid
    p = plan(repo, base, base)
    assert [(m["name"], m["path"], m["deploy"]) for m in p["members"]] == [
        ("apps-clock", "apps/clock", "none"),
        ("apps-notes", "apps/notes", "none"),
        ("build-logic", "build-logic", "none"),
        ("core-data", "core/data", "none"),
        ("core-design-system", "core/design-system", "none"),
        ("tooling", "tooling", "none"),
    ]
    assert member(p, "core-data")["dependents"] == ["apps-clock", "core-design-system"]
    assert member(p, "core-design-system")["dependents"] == ["apps-notes"]


def test_a_gradle_build_file_tests_every_member_and_no_repo_suite(droid):
    repo, prev = droid
    for path in ("gradle/libs.versions.toml", "build-logic/src/x.kt", "gradlew"):
        head = change(repo, {path: "2\n"})
        p = plan(repo, head, prev)
        assert p["legs"] == DROID, path
        assert p["reasons"]["core-data"] == [f"build {path}"] and not p["repo_suites"]
        prev = head


def test_a_gradle_module_change_tests_its_users_transitively(droid):
    repo, base = droid
    mid = change(repo, {"core/data/src/D.kt": "//\n"})
    p = plan(repo, mid, base)
    assert p["legs"] == MODULES
    # Clock's use of it is its convention plugin's.
    assert (
        p["reasons"]["apps-notes"] == p["reasons"]["apps-clock"] == ["uses core-data"]
    )
    p = plan(repo, change(repo, {"apps/notes/src/N.kt": "//\n"}), mid)
    assert p["legs"] == ["apps-notes"]


@pytest.mark.parametrize(
    ("path", "leg"),
    [
        ("core/data/src/test/golden.png", "core-data"),
        ("core/data/src/androidTest/golden.png", "core-data"),
        ("build-logic/convention/src/test/T.kt", "build-logic"),
    ],
)
def test_a_gradle_members_own_tests_test_only_it(droid, path, leg):
    repo, base = droid
    assert plan(repo, change(repo, {path: "2\n"}), base)["legs"] == [leg]


@pytest.mark.parametrize(
    ("path", "reader", "legs"),
    [
        ("schema/notes.json", "apps-notes", ["apps-notes"]),
        ("design/tokens.json", "apps-clock", ["apps-clock"]),
        # A CMakeLists.txt names its whole project, built into core-data for its users.
        ("native/src/a.cpp", "core-data", MODULES),
    ],
)
def test_a_root_input_a_module_or_its_plugin_names_tests_it(droid, path, reader, legs):
    repo, base = droid
    p = plan(repo, change(repo, {path: "{1}\n"}), base)
    assert p["legs"] == legs and p["reasons"][reader] == [f"reads {path}"]
    p = plan(repo, change(repo, delete=(path,)), base)
    assert p["legs"] == legs and p["reasons"][reader] == [f"reads {path}"]


def test_an_included_build_is_a_member_run_from_its_parent(droid):
    repo, base = droid
    p = plan(repo, change(repo, {"tooling/gen/G.kt": "//\n"}), base)
    assert p["legs"] == [*MODULES, "tooling"]
    assert p["reasons"]["core-data"] == ["reads tooling/gen/G.kt"]
    write(repo, {"gradlew": '#!/bin/sh\necho "$@"\n'})
    (repo / "gradlew").chmod(0o755)
    for d, task in (
        ("build-logic", "-p build-logic check"),
        ("core/data", ":core:data:check"),
    ):
        out = run(repo / d, "bash", str(HERE / "gradle.sh"), ".")
        assert out == f"--no-daemon --stacktrace {task}"


def test_included_builds_are_named_by_path_and_one_from_outside_is_skipped(
    tmp_path, capsys
):
    repo = tmp_path / "g"
    init(repo, {"a/settings.gradle": "", "a/build-logic/settings.gradle": ""})
    write(repo, {"b/settings.gradle": 'includeBuild("../shared")\n'})
    write(repo, {"b/build-logic/settings.gradle": "", "shared/settings.gradle": ""})
    head = commit(repo)
    names = [m["name"] for m in plan(repo, head, head)["members"]]
    assert names == ["a-build-logic", "b-build-logic"]
    assert "skipping shared" in capsys.readouterr().err


def test_the_root_flake_tests_every_cargo_and_gradle_member(rust, droid):
    cargo = ["cli", "src-tauri", "engine"]
    for (repo, base), legs in ((rust, cargo), (droid, DROID)):
        p = plan(repo, change(repo, {"flake.lock": "{}\n"}), base)
        assert p["legs"] == legs and p["reasons"][legs[0]] == ["flake flake.lock"]


@pytest.mark.parametrize(
    ("path", "text", "error"),
    [
        ("settings.gradle", "include ':a-b', ':a:b'\n", "share a name"),
        (
            "settings.gradle",
            'include(":a")\nproject(":a").projectDir = file("x")\n',
            "projectDir",
        ),
        ("x/Cargo.toml", "name = {{ name }}\n", "x/Cargo.toml is a member"),
    ],
)
def test_a_name_clash_a_moved_project_or_a_broken_crate_is_fatal(
    tmp_path, path, text, error
):
    repo = tmp_path / "g"
    init(repo, {path: text})
    head = commit(repo)
    with pytest.raises(SystemExit, match=error):
        plan(repo, head, head)


def test_declared_reads_test_the_reader_and_never_ship_it(mono):
    repo, base = mono
    p = plan(repo, change(repo, {"packages/a/server_config.json": "{1}\n"}), base)
    assert p["legs"] == ["a", "b"] and shipped(p) == ["a"]
    assert p["reasons"]["b"] == ["reads packages/a/server_config.json"]


def test_claude_only_commit_selects_nothing_but_hooks_run_repo_suites(mono):
    repo, base = mono
    mid = change(
        repo, {".claude/settings.json": "{}\n", "docs/x.txt": "d\n", "README.md": "s\n"}
    )
    p = plan(repo, mid, base)
    assert p["legs"] == [] and not p["repo_suites"] and p["reasons"] == {}
    p = plan(repo, change(repo, {".claude/hooks/guard.py": "x = 1\n"}), mid)
    assert p["legs"] == [] and p["reasons"]["repo_suites"] == [".claude/hooks/guard.py"]


def test_library_change_leaves_consumers_alone(mono):
    repo, base = mono
    p = plan(repo, change(repo, {"packages/lib/lib.py": "x = 2\n"}), base)
    assert p["legs"] == ["lib"] and p["libraries"] == ["lib"]
    assert member(p, "lib")["dependents"] == ["a", "b"]
    assert member(p, "a")["dependents"] == []


def test_a_dev_group_edge_is_no_ship_edge(mono, tmp_path):
    """mcp-common's dev group mounts servers that use it: as ship edges, each waited on the other."""
    repo, _ = mono
    write(
        repo,
        {
            "packages/lib/pyproject.toml": project(
                "lib",
                extra='[tool.uv.sources]\na = { workspace = true }\n[dependency-groups]\ndev = ["a"]\n',
            )
        },
    )
    lock(repo, tmp_path / "dist")
    base = commit(repo)
    p = plan(repo, change(repo, {"packages/a/a.py": "x = 2\n"}), base)
    assert p["libraries"] == ["a"] and member(p, "a")["dependents"] == ["lib"]
    assert member(p, "a")["carried_by"] == []
    assert member(p, "lib")["carried_by"] == ["a", "b"]


def test_repo_level_path(mono):
    repo, base = mono
    p = plan(repo, change(repo, {"tools/x.py": "x = 2\n"}), base)
    assert p["legs"] == [] and p["repo_suites"]


def test_unparseable_pyproject_is_skipped_with_a_warning(mono, capsys):
    repo, base = mono
    p = plan(
        repo, change(repo, {"templates/x/pyproject.toml": "name = {{ name }}\n"}), base
    )
    assert p["legs"] == [] and p["repo_suites"]
    assert "skipping unparseable templates/x/pyproject.toml" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "packages/a/pyproject.toml", "packages/emb/pyproject.toml"],
)
def test_an_unparseable_member_or_workspace_root_is_fatal(mono, path):
    repo, base = mono
    with pytest.raises(SystemExit, match=f"^plan: {path} is a member"):
        plan(repo, change(repo, {path: "name = {{ name }}\n"}), base)


def test_a_workspace_roots_settings_test_every_member_and_ship_none(mono, tmp_path):
    repo, base = mono
    root = ROOT.replace("CLIENTS", "") + "[tool.pytest.ini_options]\nx = 1\n"
    mid = change(repo, {"pyproject.toml": root})
    p = plan(repo, mid, base)
    assert p["legs"] == ["a", "b", "emb", "lib"] and shipped(p) == []
    assert p["reasons"]["a"] == ["pyproject.toml settings moved"]
    assert not p["repo_suites"]
    # The lock does not record default-groups, though every leg's sync installs them.
    root = root.replace(', "clients"', "")
    groups = change(repo, {"pyproject.toml": root})
    p = plan(repo, groups, mid)
    assert p["legs"] == ["a", "b", "emb", "lib"] and shipped(p) == []
    # The rest of tool.uv the lock records, so the export judges it.
    constraint = '[tool.uv]\nconstraint-dependencies = ["dep>=1"]\n'
    write(repo, {"pyproject.toml": root.replace("[tool.uv]\n", constraint)})
    lock(repo, tmp_path / "dist")
    head = commit(repo)
    assert planner.diff(str(repo), groups, head) == ("pyproject.toml", "uv.lock")
    assert plan(repo, head, groups)["legs"] == []


def test_a_root_members_version_bump_tests_only_it(tmp_path):
    """redactyl 5a9747b: the root member's [project] is its own leg's, not its workspace's."""
    repo, dist = tmp_path / "top", tmp_path / "dist"
    wheel(dist, "1.0")
    ws = '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    init(
        repo,
        {
            "pyproject.toml": project("top", extra=ws),
            "packages/core/pyproject.toml": project("core", "dep"),
        },
    )
    lock(repo, dist)
    base = commit(repo)
    write(repo, {"pyproject.toml": project("top", extra=ws).replace("0.1.0", "0.2.0")})
    lock(repo, dist)
    p = plan(repo, commit(repo), base)
    assert p["legs"] == ["top"] and shipped(p) == []


@pytest.fixture
def rooted(tmp_path: Path) -> Path:
    """Shaped like caspo: the repo root is a uv member beside two others."""
    repo, ok = tmp_path / "caspo", "def test_ok():\n    pass\n"
    init(
        repo,
        {
            "pyproject.toml": project("scripts"),
            "uv.lock": "",
            "tests/test_r.py": ok,
            "scripts/x/tests/test_x.py": ok,
            "backend/pyproject.toml": project("app"),
            "backend/uv.lock": "",
            "backend/tests/test_b.py": ok,
            "frontend/package.json": '{"name": "web"}',
            "frontend/pnpm-lock.yaml": "",
            "frontend/tests/a.ts": "",
            "tools/gen/copier.yaml": "",
            "tools/gen/tests/test_t.py": ok,
            "docs/tests/test_d.py": ok,
        },
    )
    commit(repo)
    return repo


def test_a_root_members_leg_runs_every_tests_dir_no_other_member_or_project_holds(
    rooted,
):
    """caspo: scripts/*/tests sat outside every other member, so no leg ran them."""
    head = run(rooted, "git", "rev-parse", "HEAD")
    paths = [m["path"] for m in plan(rooted, head, head)["members"]]
    assert paths == ["", "backend", "frontend"]
    script = f'. "{HERE}/lib.sh"; repo_tests "$@"; printf "found %s\\n" "${{found[@]}}"'
    out = run(rooted, "bash", "-c", script, "-", str(rooted), *paths).splitlines()
    assert [o[6:] for o in out if o.startswith("found ")] == [
        "scripts/x/tests",
        "tests",
    ]
    assert "tools/gen/tests is inside tools/gen" in out[0]
    # The repo leg leaves them to that member's.
    out = run(rooted, "bash", str(HERE / "repo.sh"), str(rooted), "suites")
    assert "the repo root is the uv member scripts" in out


def test_a_root_members_suite_runs_the_nested_tests_and_fails_with_them(
    rooted, tmp_path
):
    # uv run is this interpreter, which has pytest: what is tested is the suite's choice.
    shim = tmp_path / "bin" / "uv"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\nwhile [ "$1" != python ]; do shift; done\nshift\nexec "{sys.executable}" "$@"\n'
    )
    shim.chmod(0o755)
    env = {**ENV, "PATH": f"{shim.parent}{os.pathsep}{ENV['PATH']}"}
    top = os.path.realpath(rooted)

    def suite() -> subprocess.CompletedProcess[str]:
        script = f'. "{HERE}/lib.sh"; suite "$1" "$1" scripts'
        cmd = ["bash", "-c", script, "-", top]
        return subprocess.run(cmd, cwd=top, env=env, capture_output=True, text=True)

    r = suite()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "scripts/x/tests/test_x.py ." in r.stdout and "tests/test_r.py ." in r.stdout
    assert "test_b.py" not in r.stdout and "test_t.py" not in r.stdout
    write(rooted, {"scripts/x/tests/test_x.py": "def test_x():\n    assert False\n"})
    r = suite()
    assert r.returncode != 0 and "scripts/x/tests/test_x.py F" in r.stdout


def test_a_pnpm_root_members_repo_leg_runs_no_pytest_without_a_uv_workspace(tmp_path):
    """thoth: tests/e2e is its root pnpm member's, and no root uv workspace exists."""
    repo = tmp_path / "thoth"
    init(
        repo,
        {
            "package.json": '{"name": "thoth"}',
            "pnpm-lock.yaml": "",
            "tests/e2e/smoke.spec.ts": "",
        },
    )
    commit(repo)
    out = run(repo, "bash", str(HERE / "repo.sh"), str(repo), "suites")
    assert "tests lie in the pnpm member thoth" in out


@pytest.mark.parametrize("bases", [(), ("0" * 40,)])
def test_no_base_tests_everything_and_deploys_nothing(mono, bases):
    repo, head = mono
    p = plan(repo, head, *bases)
    assert p["legs"] == ["a", "b", "emb", "lib"] and p["repo_suites"]
    assert shipped(p) == [] and p["libraries"] == [] and p["artefacts"] == []


def test_roll_without_a_base_selects_only_the_roll(mono):
    repo, head = mono
    p = plan(repo, head, roll="a")
    assert p["legs"] == ["a"] and shipped(p) == ["a"] and not p["repo_suites"]


@pytest.mark.parametrize("bad", ["deadbeef", "a=deadbeef"])
def test_a_base_that_names_nothing_is_fatal(mono, bad):
    repo, head = mono
    with pytest.raises(SystemExit, match="deadbeef"):
        plan(repo, head, head, bad)


def test_a_base_newer_than_head_never_ships(mono):
    repo, base = mono
    live = change(repo, {"packages/a/a.py": "x = 2\n"})
    p = plan(repo, base, live, roll="b")
    assert p["legs"] == ["a", "b"] and shipped(p) == []


def test_per_member_base_and_roll(mono):
    repo, base = mono
    mid = change(repo, {"packages/a/a.py": "x = 2\n"})
    head = change(repo, {"packages/b/b.py": "x = 2\n"})
    assert plan(repo, head, mid, f"a={base}")["legs"] == ["a", "b"]
    assert plan(repo, head, f"a={head}", f"b={head}")["legs"] == ["emb", "lib"]
    assert plan(repo, head, head, roll="lib")["legs"] == ["lib"]
    with pytest.raises(SystemExit):
        plan(repo, head, head, roll="nope")
