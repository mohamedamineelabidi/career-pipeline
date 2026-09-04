"""Hard gate: refuse the public push if any personal identifier or secret survives."""
import pathlib
import re
import sys

ROOT = pathlib.Path("/path/to/career-pipeline-public")

BANNED = {
    "real email": re.compile(r"you", re.I),
    "real phone": re.compile(r"638[\s-]?906320"),
    "real name": re.compile(r"Mohamed\s+Amine|El\s+Abidi", re.I),
    "linkedin handle": re.compile(r"\byour-linkedin-handle\b", re.I),
    "github handle": re.compile(r"your-github-handle", re.I),
    "windows user path": re.compile(r"C:[\\/]Users[\\/]hp", re.I),
    "groq key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "openai key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache"}

FORBIDDEN_FILES = [
    "reference_cv_2027/data/career_master.yaml",
    "reference_cv_2027/data/evidence_register.yaml",
    "reference_cv_2027/data/tailoring_knowledge.yaml",
    "reach/about_me.json",
    ".env",
]


def verify(root=ROOT):
    """Return a list of blocking findings for the tree at ``root``; empty means CLEAN."""
    root = pathlib.Path(root)
    failures = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        rel = path.relative_to(root).as_posix()
        for label, rx in BANNED.items():
            for m in rx.finditer(text):
                line = text[: m.start()].count("\n") + 1
                failures.append(f"{label:18} {rel}:{line}  ->  {m.group(0)[:50]}")
    for forbidden in FORBIDDEN_FILES:
        if (root / forbidden).exists():
            failures.append(f"FORBIDDEN FILE PRESENT: {forbidden}")
    db = [p for p in list(root.rglob("*.sqlite3")) + list(root.rglob("*.db")) if ".git" not in p.parts]
    if db:
        failures.append(f"DATABASE FILES PRESENT: {[str(d) for d in db]}")
    return failures


def main(argv=None):
    failures = verify(ROOT)
    if failures:
        print(f"\n!!! {len(failures)} BLOCKING ISSUES, DO NOT PUSH\n")
        for f in failures[:60]:
            print("  ", f)
        return 1
    print("\nCLEAN: no personal identifiers, secrets, databases or private profiles found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
