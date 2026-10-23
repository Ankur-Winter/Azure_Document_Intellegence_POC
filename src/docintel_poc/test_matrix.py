from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TestCase:
    case_id: str
    filename: str
    input_type: str
    validation: str
    ground_truth: str = ""
    expected_page_units: int | None = None
    expected_table_count: int | None = None
    table_reference: str = ""
    expected_figure_count: int | None = None
    figure_model: str = ""
    expected_paragraph_count: int | None = None
    paragraph_model: str = ""
    embedded_image_marker: str = ""
    notes: str = ""


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def load_test_matrix(path: Path) -> list[TestCase]:
    if not path.is_file():
        raise ValueError(f"Test matrix not found: {path}")
    cases = []
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            case_id = (row.get("case_id") or "").strip().upper()
            filename = (row.get("filename") or "").strip().replace("\\", "/")
            if not case_id or not filename:
                raise ValueError("Every test matrix row needs a case_id and filename.")
            if case_id in seen_ids:
                raise ValueError(f"Duplicate test case ID in matrix: {case_id}")
            if filename.casefold() in seen_files:
                raise ValueError(f"Duplicate test filename in matrix: {filename}")
            seen_ids.add(case_id)
            seen_files.add(filename.casefold())
            cases.append(
                TestCase(
                    case_id=case_id,
                    filename=filename,
                    input_type=(row.get("input_type") or "Document").strip(),
                    validation=(row.get("validation") or "Text extraction accuracy").strip(),
                    ground_truth=(row.get("ground_truth") or "").strip(),
                    expected_page_units=_optional_int(row.get("expected_page_units")),
                    expected_table_count=_optional_int(row.get("expected_table_count")),
                    table_reference=(row.get("table_reference") or "").strip(),
                    expected_figure_count=_optional_int(row.get("expected_figure_count")),
                    figure_model=(row.get("figure_model") or "").strip(),
                    expected_paragraph_count=_optional_int(row.get("expected_paragraph_count")),
                    paragraph_model=(row.get("paragraph_model") or "").strip(),
                    embedded_image_marker=(row.get("embedded_image_marker") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return cases


def custom_test_case(filename: str) -> TestCase:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return TestCase(
        case_id="CUSTOM",
        filename=filename,
        input_type=suffix.upper() or "Document",
        validation="Text extraction accuracy",
        ground_truth=f"{filename}.txt",
        notes="User-selected file outside the supplied test matrix.",
    )

