"""cv_render: single local renderer from a RenderCV YAML to a one-page PDF.

Inspired by RenderCV (YAML -> Typst -> PDF). Enforces the hard CV rules of the
Career Pipeline: one-column theme, Arial-like restrained font, no photo, locale
matching the vacancy language, and a one-page limit (two pages only with an
explicitly approved exception). Never sends or applies to anything.
"""
from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml
from pypdf import PdfReader

SUPPORTED_LANGUAGES = ("en", "fr")
ONE_COLUMN_THEMES = ("engineeringresumes", "classic")
DEFAULT_THEME = "engineeringresumes"
FONT_FAMILY = "Arial"
RENDER_TIMEOUT_SECONDS = 300

LOCALES: dict[str, dict[str, Any]] = {
    "en": {
        "language": "en",
        "phone_number_format": "international",
        "present": "present",
        "to": "–",
        "month": "month",
        "months": "months",
        "year": "year",
        "years": "years",
        "date_template": "MONTH_ABBREVIATION YEAR",
        "abbreviations_for_months": [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ],
    },
    "fr": {
        "language": "fr",
        "phone_number_format": "international",
        "present": "présent",
        "to": "–",
        "month": "mois",
        "months": "mois",
        "year": "an",
        "years": "ans",
        "date_template": "MONTH_ABBREVIATION YEAR",
        "abbreviations_for_months": [
            "janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.",
        ],
        "full_names_of_months": [
            "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
            "septembre", "octobre", "novembre", "décembre",
        ],
    },
}

Runner = Callable[[Path, Path, Path, int], None]


class CvRenderError(RuntimeError):
    """Raised when rendering fails or the rendered PDF violates the CV rules."""


DENSITIES: dict[str, dict[str, str]] = {
    "normal": {"font_size": "10pt", "leading": "0.55em", "vmargin": "0.9cm", "hmargin": "1.2cm"},
    "compact": {"font_size": "9pt", "leading": "0.4em", "vmargin": "0.7cm", "hmargin": "1.0cm"},
}


def _design(theme: str, density: str = "normal") -> dict[str, Any]:
    d = DENSITIES[density]
    return {
        "theme": theme,
        "page": {
            "size": "a4",
            "top_margin": d["vmargin"],
            "bottom_margin": d["vmargin"],
            "left_margin": d["hmargin"],
            "right_margin": d["hmargin"],
            "show_page_numbering": False,
            "show_last_updated_date": False,
        },
        "text": {"font_family": FONT_FAMILY, "font_size": d["font_size"], "leading": d["leading"], "alignment": "left"},
        "header": {
            "name_font_family": FONT_FAMILY,
            "connections_font_family": FONT_FAMILY,
            "name_font_size": "20pt",
            "use_icons_for_connections": False,
            "vertical_space_between_name_and_connections": "0.35cm",
            "vertical_space_between_connections_and_first_section": "0.4cm",
        },
    }


def prepare_document(document: dict[str, Any], language: str, theme: str = DEFAULT_THEME, density: str = "normal") -> dict[str, Any]:
    """Return a copy of a RenderCV document normalized to the hard CV rules."""
    if language not in SUPPORTED_LANGUAGES:
        raise CvRenderError("language must be one of: " + ", ".join(SUPPORTED_LANGUAGES))
    if theme not in ONE_COLUMN_THEMES:
        raise CvRenderError("theme must be one-column: " + ", ".join(ONE_COLUMN_THEMES))
    if density not in DENSITIES:
        raise CvRenderError("density must be one of: " + ", ".join(DENSITIES))
    prepared = copy.deepcopy(document)
    cv = prepared.setdefault("cv", {})
    if not isinstance(cv, dict):
        raise CvRenderError("RenderCV document must contain a 'cv' mapping")
    cv.pop("photo", None)
    prepared["design"] = _design(theme, density)
    prepared["locale"] = copy.deepcopy(LOCALES[language])
    prepared.pop("rendercv_settings", None)
    return prepared


def yaml_visible_text(document: dict[str, Any]) -> str:
    """Collect the human-visible strings of a RenderCV document (cv section only)."""
    parts: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_key == "photo":
                    continue
                walk(sub_value, str(sub_key))
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, (str, int, float)) and key not in {"start_date", "end_date", "date"}:
            parts.append(str(value))

    walk(document.get("cv", {}))
    return "\n".join(parts)


def _default_runner(yaml_path: Path, out_dir: Path, pdf_path: Path, timeout: int) -> None:
    command = [
        sys.executable, "-m", "rendercv", "render", str(yaml_path),
        "-o", str(out_dir / "rendercv_output"),
        "-pdf", str(pdf_path),
        "-nomd", "-nohtml", "-nopng",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=str(out_dir))
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr)[-1500:]
        raise CvRenderError(f"rendercv render failed ({result.returncode}): {tail}")


def pdf_page_count(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def extract_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def render_cv_yaml(
    yaml_path: Path | str,
    out_dir: Path | str,
    language: str,
    *,
    approved_two_page_exception: bool = False,
    theme: str = DEFAULT_THEME,
    stem: str | None = None,
    runner: Runner | None = None,
    timeout: int = RENDER_TIMEOUT_SECONDS,
    density: str = "normal",
) -> dict[str, Any]:
    """Render a RenderCV YAML locally to PDF and validate page count.

    Returns {pdf_path, pages, yaml_path, text_path, language, theme}.
    """
    yaml_path = Path(yaml_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not yaml_path.is_file():
        raise CvRenderError(f"yaml not found: {yaml_path}")
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise CvRenderError("RenderCV yaml must be a mapping")
    prepared = prepare_document(loaded, language, theme, density)
    stem = stem or yaml_path.stem
    prepared_yaml = out_dir / f"{stem}.yaml"
    prepared_yaml.write_text(yaml.safe_dump(prepared, allow_unicode=True, sort_keys=False), encoding="utf-8")
    pdf_path = out_dir / f"{stem}.pdf"
    if pdf_path.exists():
        pdf_path.unlink()
    (runner or _default_runner)(prepared_yaml, out_dir, pdf_path, timeout)
    if not pdf_path.is_file():
        raise CvRenderError("renderer did not produce a PDF")
    shutil.rmtree(out_dir / "rendercv_output", ignore_errors=True)
    pages = pdf_page_count(pdf_path)
    allowed = 2 if approved_two_page_exception else 1
    if pages > allowed:
        raise CvRenderError(
            f"CV rendered to {pages} pages; limit is {allowed} page(s)"
            + ("" if approved_two_page_exception else " (no approved_two_page_exception)")
        )
    text_path = out_dir / f"{stem}.txt"
    text_path.write_text(extract_pdf_text(pdf_path), encoding="utf-8")
    return {
        "pdf_path": str(pdf_path),
        "pages": pages,
        "yaml_path": str(prepared_yaml),
        "text_path": str(text_path),
        "language": language,
        "theme": theme,
        "density": density,
    }
