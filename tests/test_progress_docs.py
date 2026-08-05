import hashlib
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "docs" / "PROGRESS.md"
PROGRESS_ROOT = ROOT / "docs" / "progress"
ARCHIVE_ROOT = PROGRESS_ROOT / "archive"
REQUIRED_ARCHIVES = {
    ARCHIVE_ROOT / "2026-06.md",
    ARCHIVE_ROOT / "2026-07.md",
    ARCHIVE_ROOT / "2026-08.md",
}
REQUIRED_CHANGE_SECTIONS = (
    "Resultado",
    "Mudanças de contrato",
    "RED → GREEN",
    "Falhas investigadas",
    "Verificação final",
    "Pendências ou bloqueios externos",
)
LINK_RE = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"seção ausente: {heading}"
    return match.group("body")


def _local_link_target(document: Path, raw_target: str) -> Path | None:
    target = raw_target.split(maxsplit=1)[0].strip("<>")
    if target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    relative_path = target.split("#", maxsplit=1)[0]
    return (document.parent / relative_path).resolve()


def _heading_anchors(markdown: str) -> set[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", markdown, flags=re.MULTILINE):
        slug = "".join(character for character in heading.lower() if character.isalnum() or character in " _-")
        slug = slug.replace(" ", "-")
        duplicate = occurrences.get(slug, 0)
        occurrences[slug] = duplicate + 1
        anchors.add(f"{slug}-{duplicate}" if duplicate else slug)
    return anchors


def test_progress_dashboard_is_short_and_links_exactly_ten_recent_deliveries():
    markdown = PROGRESS.read_text(encoding="utf-8")

    assert len(markdown.splitlines()) <= 250
    recent = _section(markdown, "Últimas 10 entregas")
    entries = re.findall(r"^- \[[^]]+\]\(([^)]+)\) — .+$", recent, flags=re.MULTILINE)
    assert len(entries) == 10
    assert all(_local_link_target(PROGRESS, target).is_file() for target in entries)


def test_progress_archive_preserves_every_legacy_line_and_heading_inventory():
    assert REQUIRED_ARCHIVES <= set(ARCHIVE_ROOT.glob("2026-*.md"))
    archive_texts = [path.read_text(encoding="utf-8") for path in sorted(REQUIRED_ARCHIVES)]
    assert sum(len(text.splitlines()) for text in archive_texts) == 3_254

    headings = [
        line.removeprefix("## ")
        for text in archive_texts
        for line in text.splitlines()
        if line.startswith("## ")
    ]
    assert len(headings) == 76

    manifest = (ARCHIVE_ROOT / "MANIFEST.md").read_text(encoding="utf-8")
    inventory_rows = re.findall(r"^\| \d+ \|", manifest, flags=re.MULTILINE)
    assert "3.254" in manifest
    assert "76 títulos" in manifest
    assert len(inventory_rows) == 76
    assert all(heading in manifest for heading in headings)


def test_progress_archive_matches_immutable_manifest_checksums():
    manifest = (ARCHIVE_ROOT / "MANIFEST.md").read_text(encoding="utf-8")
    checksums = dict(re.findall(r"^\| `(2026-\d{2}\.md)` \| `([a-f0-9]{64})` \|$", manifest, re.MULTILINE))

    assert set(checksums) == {path.name for path in REQUIRED_ARCHIVES}
    for filename, expected in checksums.items():
        actual = hashlib.sha256((ARCHIVE_ROOT / filename).read_bytes()).hexdigest()
        assert actual == expected


def test_progress_template_and_change_pages_have_every_required_section():
    documents = [PROGRESS_ROOT / "CHANGE-TEMPLATE.md", *sorted((PROGRESS_ROOT / "changes").glob("*.md"))]

    assert len(documents) > 1
    for document in documents:
        markdown = document.read_text(encoding="utf-8")
        for heading in REQUIRED_CHANGE_SECTIONS:
            assert f"## {heading}" in markdown, f"seção ausente em {document}: {heading}"
        assert re.search(
            r"\|\s*Sintoma\s*\|\s*Causa\s*\|\s*Correção\s*\|",
            markdown,
        )


def test_all_local_links_in_project_documentation_resolve():
    documents = [ROOT / "README.md", ROOT / "RESOURCES.md", *sorted((ROOT / "docs").rglob("*.md"))]

    for document in documents:
        markdown = document.read_text(encoding="utf-8")
        for raw_target in LINK_RE.findall(markdown):
            target = _local_link_target(document, raw_target)
            if target is not None:
                assert target.exists(), f"link quebrado em {document}: {raw_target}"
                fragment = raw_target.split(maxsplit=1)[0].strip("<>").partition("#")[2]
                if fragment and target.suffix == ".md":
                    anchors = _heading_anchors(target.read_text(encoding="utf-8"))
                    assert unquote(fragment) in anchors, (
                        f"âncora quebrada em {document}: {raw_target}"
                    )
