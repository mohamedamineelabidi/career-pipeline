from __future__ import annotations

import argparse
import base64
import mimetypes
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

try:
    from scripts.validate_profile import validate_profile
except ModuleNotFoundError:  # Direct script execution on Windows.
    from validate_profile import validate_profile

MONTHS = {
    "01": "Jan",
    "02": "Feb",
    "03": "Mar",
    "04": "Apr",
    "05": "May",
    "06": "Jun",
    "07": "Jul",
    "08": "Aug",
    "09": "Sep",
    "10": "Oct",
    "11": "Nov",
    "12": "Dec",
}


def format_date(value) -> str:
    text = str(value)
    if text.lower() == "present":
        return "Present"
    if len(text) == 7 and text[4] == "-":
        year, month = text.split("-", 1)
        return f"{MONTHS[month]} {year}"
    if len(text) == 4 and text.isdigit():
        return text
    return text


def date_range(start, end) -> str:
    return f"{format_date(start)} – {format_date(end)}"


def unique_technologies(item: dict) -> list[str]:
    values: list[str] = []
    for bullet in item.get("bullets", []):
        for technology in bullet.get("technologies", []):
            if technology not in values:
                values.append(technology)
    return values


def image_data_uri(path: str) -> str:
    image_path = Path(path)
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def render_reference(
    profile_path: Path,
    template_path: Path,
    selection_key: str = "selected_for_reference",
) -> str:
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    errors = validate_profile(profile)
    if errors:
        raise ValueError("Profile validation failed:\n" + "\n".join(errors))

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["date_range"] = date_range
    env.filters["format_date"] = format_date
    template = env.get_template(template_path.name)
    experience = [item for item in profile["experience"] if item.get(selection_key)]
    projects = [item for item in profile["projects"] if item.get(selection_key)]
    if selection_key == "selected_for_data_ai":
        order = profile.get("data_ai_variant", {}).get("project_order", [])
        rank = {project_id: index for index, project_id in enumerate(order)}
        projects.sort(key=lambda item: rank.get(item["id"], len(rank)))
    for item in [*experience, *projects]:
        item["display_technologies"] = unique_technologies(item)
    return template.render(
        profile=profile,
        experience=experience,
        projects=projects,
        photo_src=image_data_uri(profile["identity"]["photo_path"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Reference CV HTML")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--selection-key",
        default="selected_for_reference",
        help="Boolean item field used to select experience and projects",
    )
    args = parser.parse_args()

    html = render_reference(
        args.profile.resolve(), args.template.resolve(), args.selection_key
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
