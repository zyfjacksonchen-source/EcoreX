#!/usr/bin/env python3
"""Strip legacy/private branding from EcoreX release runtime staging trees."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys


TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".plist",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {"LICENSE", "README", "README.txt", "runtime-manifest.json"}
VOLATILE_DIR_NAMES = {"capability-state"}
VOLATILE_FILE_SUFFIXES = {".log"}
WINDOWS_PYTHON_LAUNCHER_DIR = pathlib.PurePosixPath("python/Scripts")
SITE_PACKAGES_PARTS = {"site-packages", "dist-packages"}


def _s(*parts: str) -> str:
    return "".join(parts)


LEGACY_AGENT_MIXED = _s("Cow", "Agent")
LEGACY_AGENT_UPPER = LEGACY_AGENT_MIXED.upper()
LEGACY_AGENT_LOWER = LEGACY_AGENT_MIXED.lower()
LEGACY_AUTHOR = _s("zha", "yuj", "ie")
LEGACY_CHAT_DASH = _s("chat", "gpt", "-on-", "we", "chat")
LEGACY_CHAT_UNDERSCORE = LEGACY_CHAT_DASH.replace("-", "_")
LEGACY_CHAT_UPPER = LEGACY_CHAT_DASH.upper()
LEGACY_LOCAL_WIN = _s("C:", "\\", LEGACY_AGENT_MIXED)
LEGACY_LOCAL_POSIX = _s("C:/", LEGACY_AGENT_MIXED)
LEGACY_REPO_HOST = _s("github.com/", LEGACY_AUTHOR)
LEGACY_GITEE_HOST = _s("gitee.com/", LEGACY_AUTHOR)
LEGACY_DOCS_HOST = _s("docs.", LEGACY_AGENT_LOWER, ".ai")
LEGACY_SKILLS_HOST = _s("skills.", LEGACY_AGENT_LOWER, ".ai")
LEGACY_ROOT_HOST = _s(LEGACY_AGENT_LOWER, ".ai")
LEGACY_CLI_PLUGIN_SNAKE = _s("cow", "_cli")
LEGACY_CLI_PLUGIN_CAMEL = _s("Cow", "Cli")
LEGACY_CLI_PLUGIN_UPPER = _s("COW", "_CLI")
LEGACY_CLI_PRODUCT = _s("Cow", " CLI")
FORBIDDEN = (
    LEGACY_AGENT_MIXED,
    LEGACY_AGENT_UPPER,
    LEGACY_AGENT_LOWER,
    LEGACY_LOCAL_WIN,
    LEGACY_LOCAL_POSIX,
    LEGACY_AUTHOR,
    LEGACY_CHAT_DASH,
    LEGACY_CHAT_UNDERSCORE,
    LEGACY_CHAT_UPPER,
    LEGACY_CLI_PLUGIN_SNAKE,
    LEGACY_CLI_PLUGIN_CAMEL,
    LEGACY_CLI_PLUGIN_UPPER,
    LEGACY_CLI_PRODUCT,
)
FORBIDDEN_RE = re.compile("|".join(re.escape(item) for item in FORBIDDEN))
MIGRATION_README_NAMES = {"README.txt", "README-migration.txt"}
MIGRATION_README_REQUIRED_FRAGMENTS = (
    _s("The original v0.1.0 project name was ", LEGACY_AGENT_MIXED, " and has been renamed to EcoreX."),
    _s(LEGACY_AGENT_MIXED, " is a historical project name / development stack name only. It does not indicate plagiarism, copying, or third-party ownership."),
    _s("原始 v0.1.0 版本项目名 ", LEGACY_AGENT_MIXED, " 已改名为 EcoreX；", LEGACY_AGENT_MIXED, " 是历史项目名称/开发栈名称，不代表抄袭或第三方归属。"),
)


def is_allowed_migration_readme(path: pathlib.Path, text: str) -> bool:
    if path.name not in MIGRATION_README_NAMES:
        return False
    remaining = text
    for fragment in MIGRATION_README_REQUIRED_FRAGMENTS:
        if fragment not in remaining:
            return False
        remaining = remaining.replace(fragment, "")
    return FORBIDDEN_RE.search(remaining) is None


def is_text_candidate(path: pathlib.Path) -> bool:
    return path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES


def sanitize_text(text: str) -> str:
    replacements = (
        (LEGACY_CLI_PLUGIN_UPPER, "ECOREX_CLI"),
        (LEGACY_CLI_PLUGIN_CAMEL, "EcoreXCli"),
        (LEGACY_CLI_PLUGIN_SNAKE, "ecorex_cli"),
        (LEGACY_CLI_PRODUCT, "EcoreX CLI"),
        ("cow/slash", "EcoreX/slash"),
        ("cow supports", "EcoreX supports"),
        (LEGACY_LOCAL_WIN, "EcoreX"),
        (LEGACY_LOCAL_POSIX, "EcoreX"),
        (_s(LEGACY_REPO_HOST, "/", LEGACY_CHAT_DASH), "github.com/zhangyifanjackson-dotcom/EcoreX"),
        (_s("github.com/JS00000/", LEGACY_CHAT_DASH), "github.com/zhangyifanjackson-dotcom/EcoreX"),
        (_s(LEGACY_REPO_HOST, "/", LEGACY_AGENT_MIXED), "www.ecoreai.cn/ecorex-agent"),
        (_s(LEGACY_REPO_HOST, "/", LEGACY_AGENT_LOWER), "www.ecoreai.cn/ecorex-agent"),
        (_s(LEGACY_GITEE_HOST, "/", LEGACY_AGENT_MIXED), "www.ecoreai.cn/ecorex-agent"),
        (_s(LEGACY_GITEE_HOST, "/", LEGACY_AGENT_LOWER), "www.ecoreai.cn/ecorex-agent"),
        (LEGACY_DOCS_HOST, "www.ecoreai.cn/ecorex-agent"),
        (LEGACY_SKILLS_HOST, "www.ecoreai.cn/ecorex-agent/skills"),
        (LEGACY_ROOT_HOST, "www.ecoreai.cn/ecorex-agent"),
        (LEGACY_AUTHOR, "zhangyifanjackson-dotcom"),
        (LEGACY_CHAT_UPPER, "ECOREX"),
        (LEGACY_CHAT_DASH, "EcoreX"),
        (LEGACY_CHAT_UNDERSCORE, "ecorex"),
        (LEGACY_AGENT_UPPER, "ECOREX"),
        (LEGACY_AGENT_MIXED, "EcoreX"),
        (LEGACY_AGENT_LOWER, "ecorex"),
    )
    for before, after in replacements:
        text = text.replace(before, after)
    return text


def sanitize_paths(root: pathlib.Path) -> list[pathlib.Path]:
    changed: list[pathlib.Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        name = path.name
        new_name = (
            name.replace(LEGACY_CLI_PLUGIN_UPPER, "ECOREX_CLI")
            .replace(LEGACY_CLI_PLUGIN_CAMEL, "EcoreXCli")
            .replace(LEGACY_CLI_PLUGIN_SNAKE, "ecorex_cli")
        )
        if new_name == name:
            continue
        target = path.with_name(new_name)
        if target.exists():
            raise RuntimeError(f"cannot rename legacy release path {path} to existing {target}")
        path.rename(target)
        changed.append(target)
    return changed


def is_portable_release_path(path: pathlib.Path, root: pathlib.Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return all(ord(char) < 128 for char in rel)


def remove_non_portable_paths(root: pathlib.Path) -> list[pathlib.Path]:
    removed: list[pathlib.Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if is_portable_release_path(path, root):
            continue
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
            removed.append(path)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                continue
            removed.append(path)
    return removed


def sanitize_runtime_manifest(path: pathlib.Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key in ("repoRoot", "pythonHome", "sourceRoot", "buildRoot"):
        data.pop(key, None)
    data["product"] = "EcoreX"
    data["runtime"] = data.get("runtime") or "compatible-agent-runtime"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sanitize_tree(root: pathlib.Path) -> list[pathlib.Path]:
    changed: list[pathlib.Path] = []
    changed.extend(remove_non_portable_paths(root))
    for path in sorted(root.rglob("*"), reverse=True):
        rel_parts = set(path.relative_to(root).parts)
        if path.is_dir() and path.name in {"test", "tests"} and rel_parts.intersection(SITE_PACKAGES_PARTS):
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
            changed.append(path)
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir() and path.name in VOLATILE_DIR_NAMES:
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
            changed.append(path)
        elif path.is_file() and path.suffix.lower() in VOLATILE_FILE_SUFFIXES:
            path.unlink(missing_ok=True)
            changed.append(path)
    scripts_dir = root / WINDOWS_PYTHON_LAUNCHER_DIR
    if scripts_dir.is_dir():
        for path in scripts_dir.glob("*.exe"):
            path.unlink(missing_ok=True)
            changed.append(path)
    if (root / "runtime-manifest.json").is_file():
        sanitize_runtime_manifest(root / "runtime-manifest.json")
        changed.append(root / "runtime-manifest.json")
    for path in root.rglob("*"):
        if not path.is_file() or not is_text_candidate(path):
            continue
        try:
            original = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        except Exception:
            continue
        if is_allowed_migration_readme(path, original):
            continue
        sanitized = sanitize_text(original)
        if sanitized != original:
            path.write_text(sanitized, encoding="utf-8")
            changed.append(path)
    changed.extend(sanitize_paths(root))
    return changed


def find_non_portable_paths(root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if not is_portable_release_path(path, root):
            hits.append(path.relative_to(root).as_posix())
    return hits


def find_forbidden(root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if FORBIDDEN_RE.search(rel):
            hits.append(rel)
            continue
        if not path.is_file() or not is_text_candidate(path):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                data = path.read_bytes()
            except Exception:
                continue
            if any(item.encode("utf-8") in data for item in FORBIDDEN):
                hits.append(rel)
            continue
        except Exception:
            continue
        if FORBIDDEN_RE.search(text) and not is_allowed_migration_readme(path, text):
            hits.append(rel)
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", help="Runtime staging root to sanitize")
    parser.add_argument("--check", action="store_true", help="Only check for forbidden strings")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"runtime root not found: {root}")
    if not args.check:
        sanitize_tree(root)
    non_portable = find_non_portable_paths(root)
    if non_portable:
        print("Non-portable release paths remain:", file=sys.stderr)
        for hit in non_portable[:80]:
            print(f"  {hit}", file=sys.stderr)
        if len(non_portable) > 80:
            print(f"  ... {len(non_portable) - 80} more", file=sys.stderr)
        return 1
    hits = find_forbidden(root)
    if hits:
        print("Forbidden legacy/private release strings remain:", file=sys.stderr)
        for hit in hits[:80]:
            print(f"  {hit}", file=sys.stderr)
        if len(hits) > 80:
            print(f"  ... {len(hits) - 80} more", file=sys.stderr)
        return 1
    print(f"PASS sanitized EcoreX release runtime: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
