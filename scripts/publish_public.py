"""Publish the public-safe copy of Career Pipeline in one command.

Steps: rebuild the filtered copy -> depersonalise it -> the safety gate must be
CLEAN -> commit in the public folder with the private HEAD message -> push.
The private repo itself never gets a remote.

Flags: --dry-run (build + verify, no git writes), --no-push, --if-behind
(exit 0 silently when the public HEAD already carries the private HEAD message).
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_public_repo  # noqa: E402
import clean_public_paths  # noqa: E402
import verify_public_safe  # noqa: E402

PRIVATE = build_public_repo.SRC
REPO_URL = "https://github.com/your-github-handle/career-pipeline"


def public_dir_from_env() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PUBLIC_REPO_DIR") or build_public_repo.DST)


# Indirections so the tests can swap the heavy steps.
def build_public(src, dst):
    return build_public_repo.build(src=src, dst=dst)


def clean_public(dst):
    return clean_public_paths.clean(dst)


def verify_public(dst):
    return verify_public_safe.verify(dst)


def _git(args: list[str], cwd=None, input_text: str | None = None) -> str:
    kwargs = {"cwd": str(cwd) if cwd else None, "text": True, "capture_output": True}
    if input_text is not None:
        kwargs["input"] = input_text
    proc = subprocess.run(["git", *args], **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def private_head_message() -> str:
    return subprocess.run(
        ["git", "-C", str(PRIVATE).replace("\\", "/"), "log", "-1", "--pretty=%B"],
        text=True, capture_output=True,
    ).stdout


def public_head_message(public: pathlib.Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=str(public), text=True, capture_output=True,
    ).stdout


def main(argv=None, public_dir=None, out=print) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    no_push = "--no-push" in argv
    if_behind = "--if-behind" in argv
    public = pathlib.Path(public_dir) if public_dir else public_dir_from_env()

    message = private_head_message()
    if if_behind and public_head_message(public).strip() == message.strip():
        return 0

    stats = build_public(PRIVATE, public)
    cleaned = clean_public(public)
    failures = verify_public(public)
    if failures:
        out(f"NOT CLEAN: {len(failures)} blocking issue(s); nothing committed or pushed")
        for f in failures[:40]:
            out("  " + f)
        return 2

    out(f"copied {stats.get('copied', '?')} files, depersonalised {len(cleaned)}, verify CLEAN")
    if dry_run:
        out("dry-run: would commit in the public folder with this message:")
        out(message.strip())
        return 0

    dirty = _git(["status", "--porcelain"], cwd=public)
    if not dirty.strip():
        out("nothing to publish: public copy already matches")
        return 0

    _git(["add", "-A"], cwd=public)  # safe here only: this folder IS the filtered copy
    _git(["commit", "-F", "-"], cwd=public, input_text=message)
    sha = _git(["rev-parse", "HEAD"], cwd=public).strip()
    if not no_push:
        _git(["push", "origin", "main"], cwd=public)
    out(f"published {sha} -> {REPO_URL}/commit/{sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
