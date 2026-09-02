from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_PATH = ROOT / "data" / "tailoring_knowledge.yaml"
CAREER_PATH = ROOT / "data" / "career_master.yaml"


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> int:
    knowledge = yaml.safe_load(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    career = yaml.safe_load(CAREER_PATH.read_text(encoding="utf-8"))

    if knowledge.get("candidate") != career["identity"]["name"]:
        fail("Candidate name does not match career_master.yaml")

    for label, rel in knowledge["source_of_truth"].items():
        if label == "precedence":
            continue
        target = (ROOT / rel).resolve()
        if not target.exists():
            fail(f"Missing source_of_truth file for {label}: {target}")

    experiences = {item["id"]: item for item in career.get("experience", [])}
    projects = {item["id"]: item for item in career.get("projects", [])}
    certifications = {item["name"] for item in career.get("certifications", [])}
    skill_groups = career.get("skills", {})
    skill_names = {skill for values in skill_groups.values() for skill in values}
    evidence_skills = knowledge.get("evidence_linked_skills", {})
    evidence_items = knowledge.get("experience_project_evidence_map", {})

    allowed_statuses = set(knowledge["evidence_status_strength"])
    allowed_statuses.discard("notes")

    for skill, entry in evidence_skills.items():
        status = entry.get("strongest_status")
        if status not in allowed_statuses:
            fail(f"Unknown evidence status for {skill}: {status}")
        for ref in entry.get("sources", []):
            parts = ref.split(".")
            if parts[0] == "experience":
                item = experiences.get(parts[1])
                if not item:
                    fail(f"Unknown experience reference: {ref}")
                index = int(parts[2].removeprefix("bullet_"))
                if not 1 <= index <= len(item.get("bullets", [])):
                    fail(f"Out-of-range experience bullet reference: {ref}")
            elif parts[0] == "projects":
                item = projects.get(parts[1])
                if not item:
                    fail(f"Unknown project reference: {ref}")
                index = int(parts[2].removeprefix("bullet_"))
                if not 1 <= index <= len(item.get("bullets", [])):
                    fail(f"Out-of-range project bullet reference: {ref}")
            elif parts[0] == "skills":
                if len(parts) < 3 or parts[1] not in skill_groups or ".".join(parts[2:]) not in skill_groups[parts[1]]:
                    fail(f"Unknown canonical skill reference: {ref}")
            elif parts[0] == "certifications":
                if ".".join(parts[1:]) not in certifications:
                    fail(f"Unknown certification reference: {ref}")
            elif parts[0] == "education":
                continue
            elif parts[0] == "evidence_register":
                continue
            else:
                fail(f"Unsupported evidence reference format: {ref}")

    for item_id in evidence_items:
        if item_id not in experiences and item_id not in projects:
            fail(f"Evidence map references unknown experience/project: {item_id}")

    for role_id, role in knowledge.get("role_archetypes", {}).items():
        for section in ("must_have_concepts", "preferred_concepts"):
            for concept in role.get(section, []):
                for ref in concept.get("evidence", []):
                    if ref not in evidence_skills and ref not in evidence_items and ref not in skill_names:
                        fail(f"Role {role_id} references unknown evidence key: {ref}")
        for ref in role.get("lead_evidence", []):
            if ref not in evidence_items:
                fail(f"Role {role_id} lead_evidence is unknown: {ref}")

    rubric = knowledge["scoring_rubric"]
    category_total = sum(category["points"] for category in rubric["categories"].values())
    if category_total != rubric["total_points"] or category_total != 100:
        fail(f"Scoring categories total {category_total}, expected 100")

    control = knowledge["application_control"]
    if control.get("submission_mode") != "manual_only" or control.get("final_approval_required") is not True:
        fail("Application control must remain manual_only with final approval required")

    print(
        "tailoring_knowledge_ok",
        f"skills={len(evidence_skills)}",
        f"evidence_items={len(evidence_items)}",
        f"roles={len(knowledge['role_archetypes'])}",
        f"score_total={category_total}",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"validation_failed: {exc}", file=sys.stderr)
        raise
