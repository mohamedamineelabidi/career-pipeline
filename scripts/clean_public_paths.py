"""Second pass on the public copy: remove hardcoded machine paths and name leaks."""
import pathlib
import re
import sys

ROOT = pathlib.Path("/path/to/career-pipeline-public")
TEXT_SUFFIXES = {".py", ".md", ".html", ".json", ".yaml", ".yml", ".toml"}


def clean_text(text: str, is_python: bool) -> str:
    new = text
    if is_python and str(REPO_ROOT) in new:
        if "REPO_ROOT" not in new:
            lines = new.split("\n")
            insert = 0
            for i, line in enumerate(lines[:25]):
                if line.startswith("import ") or line.startswith("from "):
                    insert = i + 1
            lines.insert(insert, "import pathlib\nREPO_ROOT = pathlib.Path(__file__).resolve().parents[1]")
            new = "\n".join(lines)
        new = new.replace('str(REPO_ROOT)', "str(REPO_ROOT)")
        new = new.replace("str(REPO_ROOT)", "str(REPO_ROOT)")
        new = re.sub(r'str(REPO_ROOT / "([^")]*)"', r'str(REPO_ROOT / "\1")', new)
        new = re.sub(r'str(pathlib.Path(tempfile.gettempdir()) / "([^")]*)"',
                     r'str(pathlib.Path(tempfile.gettempdir()) / "\1")', new)
        if "tempfile.gettempdir()" in new and "import tempfile" not in new:
            new = new.replace("import pathlib\nREPO_ROOT", "import pathlib, tempfile\nREPO_ROOT", 1)
    new = re.sub(r"C:[\\/]Users[\\/]hp[\\/]?", "/path/to/", new)
    new = re.sub(r"MOHAMED\s+AMINE\s+EL\s+ABIDI", "JORDAN RIVERA", new)
    new = re.sub(r"Mohamed\s+Amine\s+El\s+Abidi", "Jordan Rivera", new)
    new = re.sub(r"MOHAMED\s+AMINE", "JORDAN", new, flags=re.I)
    new = re.sub(r"EL\s+ABIDI", "RIVERA", new, flags=re.I)
    new = re.sub(r"you", "you", new, flags=re.I)
    new = re.sub(r"your-github-handle", "your-github-handle", new, flags=re.I)
    new = re.sub(r"\byour-linkedin-handle\b", "your-linkedin-handle", new, flags=re.I)
    return new


def clean(root=ROOT):
    """Rewrite leaking files under ``root`` in place; return the list of changed paths."""
    root = pathlib.Path(root)
    changed = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = clean_text(text, path.suffix == ".py")
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
            changed.append(path.relative_to(root).as_posix())
    return changed


def main(argv=None):
    changed = clean(ROOT)
    print(f"cleaned {len(changed)} files")
    for c in changed:
        print("   ", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
