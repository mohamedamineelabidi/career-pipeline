"""Build a clean, public-safe copy of the Career Pipeline repo in a separate folder.

Never touches the private repo. Copies only git-tracked files, skips the files
that hold personal data or runtime state, depersonalizes prose, and leaves the
public folder's own .git and public-only files (README, LICENSE, examples) alone.
`scripts/verify_public_safe.py` is the gate that runs before anything is pushed.
"""
import fnmatch
import pathlib
import re
import shutil
import subprocess
import sys
import pathlib
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

SRC = pathlib.Path(str(REPO_ROOT))
DST = pathlib.Path("/path/to/career-pipeline-public")

# Files that ARE personal data (your real CV facts) - excluded entirely.
EXCLUDE_EXACT = {
    "reference_cv_2027/data/career_master.yaml",
    "reference_cv_2027/data/evidence_register.yaml",
    "reference_cv_2027/data/tailoring_knowledge.yaml",
    "reference_cv_2027/data/matching_progress.yaml",
    "reference_cv_2027/README.md",
    "reference_cv_2027/docs/ROLE_MATCHING_AND_SCORING.md",
    "reference_cv_2027/docs/CV_TAILORING_AGENT.md",
    "reference_cv_2027/tests/test_rendered_content.py",
    "reference_cv_2027/tests/test_profile_validation.py",
    "reference_cv_2027/tests/test_tailor_cv_agent.py",
    "AGENT_REACH.md",
    "hub.html",
    "jobhunt.html",
    "reach/about_me.json",  # private fact sheet: phone number, signature
}
EXCLUDE_PREFIX = ("reference_cv_2027/",)
# Glob patterns matched against the basename: databases, env files, DB backups.
EXCLUDE_GLOBS = ("*.sqlite3", "*.sqlite3-*", "*.db", ".env", ".env.*", "career_pipeline_v2*.json")

# Files that live only in the public copy and must survive a rebuild.
PUBLIC_ONLY = {"README.md", "LICENSE", ".env.example", "ARCHITECTURE.md"}
PUBLIC_ONLY_GLOBS = ("reference_cv_2027/data/*.example.yaml",)

# Personal identifiers -> neutral placeholders
SUBS = [
    (r"you@gmail\.com", "you@example.com"),
    (r"\+212\s?638[-\s]?906320", "+000 000000000"),
    (r"the candidate", "the candidate"),
    (r"the candidate", "the candidate"),
    (r"the candidate", "the candidate"),
    (r"your-github-handle", "your-github-handle"),
    (r"your-linkedin-handle", "your-linkedin-handle"),
]
TEXT_EXT = {".py", ".html", ".md", ".json", ".yaml", ".yml", ".toml", ".j2", ".txt", ".lock", ".gitignore"}


def is_excluded(rel: str) -> bool:
    name = pathlib.PurePosixPath(rel).name
    if rel in EXCLUDE_EXACT or rel.startswith(EXCLUDE_PREFIX):
        return True
    return any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS)


def select_files(tracked):
    return [f for f in tracked if f and not is_excluded(f)]


def tracked_files(src=SRC):
    out = subprocess.run(["git", "ls-files"], cwd=str(src), capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f]


def _is_public_only(rel: str) -> bool:
    return rel in PUBLIC_ONLY or any(fnmatch.fnmatch(rel, g) for g in PUBLIC_ONLY_GLOBS)


def _clear_destination(dst: pathlib.Path):
    """Remove everything in dst except .git and the public-only files."""
    if not dst.exists():
        return
    for path in sorted(dst.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(dst).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_file():
            if not _is_public_only(rel):
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def build(src=SRC, dst=DST, tracked=None):
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    tracked = tracked_files(src) if tracked is None else list(tracked)
    keep = select_files(tracked)
    _clear_destination(dst)
    dst.mkdir(parents=True, exist_ok=True)

    changed = []
    for rel in keep:
        s, d = src / rel, dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.suffix in TEXT_EXT or s.name == ".gitignore":
            try:
                text = s.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                shutil.copy2(s, d)
                continue
            new = text
            for pat, repl in SUBS:
                new = re.sub(pat, repl, new)
            if new != text:
                changed.append(rel)
            d.write_text(new, encoding="utf-8", newline="\n")
        else:
            shutil.copy2(s, d)
    return {"tracked": len(tracked), "copied": len(keep), "excluded": len(tracked) - len(keep), "changed": changed}


def main(argv=None):
    stats = build()
    print(f"tracked in source : {stats['tracked']}")
    print(f"copied to public  : {stats['copied']}")
    print(f"excluded          : {stats['excluded']}")
    print(f"depersonalized    : {len(stats['changed'])}")
    for c in stats["changed"]:
        print("   ", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
