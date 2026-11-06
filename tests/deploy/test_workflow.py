"""Permissions and pinning of the CI workflows (S21 spec 5.7).

No YAML parser is installed, so these checks read the workflow text: a job is the block, under ``jobs:``, from its
two-space-indented ``<name>:`` line to the next line at that indentation.
"""

import re
from pathlib import Path

from tests.deploy.docker_cli import REPO

WORKFLOWS = REPO / ".github" / "workflows"
CHECK = WORKFLOWS / "check.yml"
IMAGES = WORKFLOWS / "images.yml"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def job(workflow: str, name: str) -> str:
    lines = workflow.splitlines()
    start = lines.index(f"  {name}:", lines.index("jobs:"))
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^  \S", lines[i]) or re.match(r"^\S", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def uses(workflow: str) -> list[str]:
    return re.findall(r"uses:\s*(\S+)", workflow)


def test_check_workflow_keeps_read_only_permissions() -> None:
    workflow = text(CHECK)
    assert "contents: read" in workflow
    assert "write" not in workflow


def test_images_workflow_grants_packages_write_only_to_the_push_job() -> None:
    workflow = text(IMAGES)
    assert "\npermissions:\n  contents: read\n" in workflow
    assert workflow.count("packages: write") == 1
    push = job(workflow, "push")
    assert "packages: write" in push
    assert "if: github.event_name == 'push'" in push
    assert "needs: test" in push
    assert "packages: write" not in job(workflow, "test")


def test_images_workflow_pins_every_action_by_hash() -> None:
    for workflow in (text(CHECK), text(IMAGES)):
        for action in uses(workflow):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_images_workflow_uses_no_third_party_action() -> None:
    assert {action.split("@")[0] for action in uses(text(IMAGES))} <= {"actions/checkout", "astral-sh/setup-uv"}


def test_images_workflow_reuses_the_check_workflow_action_hashes() -> None:
    assert set(uses(text(IMAGES))) <= set(uses(text(CHECK)))


def test_dockerfile_pins_base_images_by_digest() -> None:
    # The push job rebuilds the images; a digest keeps that rebuild on the bases the test job used.
    dockerfile = text(REPO / "Dockerfile")
    bases = re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE) + re.findall(r"COPY --from=(\S+)", dockerfile)
    external = [b for b in bases if "/" in b or ":" in b]
    assert len(external) == 4  # builder, overview and runtime bases plus the uv image
    for base in external:
        assert re.search(r"@sha256:[0-9a-f]{64}$", base), base


def test_tag_push_checks_the_project_version() -> None:
    push = job(text(IMAGES), "push")
    assert "GITHUB_REF_NAME" in push
    assert '"v${version}"' in push
    assert "['project']['version']" in push
