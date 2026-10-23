from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError

from .config import ROOT, Settings, load_settings
from .dashboard import serve_dashboard
from .metrics import compare_text, normalize_text
from .service import analyze_file, check_connection
from .test_matrix import TestCase, custom_test_case, load_test_matrix


DEFAULT_MODELS = ("prebuilt-read", "prebuilt-layout")
SUPPORTED_MODELS = (*DEFAULT_MODELS, "prebuilt-invoice")
SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".heif", ".docx", ".xlsx", ".pptx"}
CSV_FIELDS = [
    "run_id",
    "run_started_utc",
    "model_id",
    "case_id",
    "file",
    "input_type",
    "validation",
    "status",
    "latency_seconds",
    "page_units",
    "expected_page_units",
    "page_unit_count_match",
    "paragraphs_detected",
    "expected_paragraph_count",
    "paragraph_count_match",
    "tables_detected",
    "expected_table_count",
    "detected_table_shapes",
    "expected_table_shapes",
    "table_count_match",
    "table_shape_match",
    "table_cell_accuracy",
    "figures_detected",
    "expected_figure_count",
    "figure_detection_match",
    "embedded_image_marker",
    "embedded_image_marker_found",
    "embedded_image_absent",
    "reference_available",
    "exact_match",
    "character_error_rate",
    "character_accuracy",
    "word_error_rate",
    "word_accuracy",
    "error",
]
RUN_HISTORY_FIELDS = [
    "run_id",
    "run_started_utc",
    "model_id",
    "workers",
    "documents_total",
    "documents_succeeded",
    "documents_failed",
    "page_units_succeeded",
    "wall_seconds",
    "throughput_documents_per_second",
    "latency_mean_seconds",
    "latency_median_seconds",
    "latency_p95_seconds",
    "documents_with_ground_truth",
    "character_accuracy_weighted",
    "word_accuracy_weighted",
    "table_cases",
    "table_shape_matches",
    "table_cell_accuracy_weighted",
    "figure_cases",
    "figure_detection_matches",
    "embedded_image_cases",
    "embedded_image_absence_passes",
]
RESCORE_CSV_FIELDS = CSV_FIELDS + ["evaluation_type", "source_run_id", "source_run_started_utc"]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open(newline="", encoding="utf-8") as source:
            existing = next(csv.reader(source), [])
        if existing != fieldnames:
            raise ValueError(
                f"History CSV schema does not match {path}. Preserve it and choose a new --output folder."
            )
    with path.open("a", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames, extrasaction="ignore")
        if path.stat().st_size == 0:
            writer.writeheader()
        writer.writerows(rows)


def _safe_relative_file(value: str, input_dir: Path) -> tuple[str, Path]:
    candidate = (input_dir / value).resolve()
    try:
        relative = candidate.relative_to(input_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Document path must stay inside the input folder: {value}") from exc
    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported input format: {candidate.suffix or '(no extension)'}")
    if not candidate.is_file():
        raise ValueError(f"Document not found: {relative.as_posix()}")
    return relative.as_posix(), candidate


def select_cases(
    matrix: list[TestCase],
    input_dir: Path,
    requested_case_ids: list[str] | None,
    requested_files: list[str] | None,
) -> list[TestCase]:
    by_id = {case.case_id.casefold(): case for case in matrix}
    by_file = {case.filename.casefold(): case for case in matrix}
    selected: dict[str, TestCase] = {}

    if not requested_case_ids and not requested_files:
        candidates = matrix
    else:
        candidates = []
        for requested_id in requested_case_ids or []:
            case = by_id.get(requested_id.casefold())
            if case is None:
                known = ", ".join(case.case_id for case in matrix)
                raise ValueError(f"Unknown test case '{requested_id}'. Available cases: {known}")
            candidates.append(case)
        for filename in requested_files or []:
            relative, _ = _safe_relative_file(filename, input_dir)
            candidates.append(by_file.get(relative.casefold()) or custom_test_case(relative))

    for case in candidates:
        relative, _ = _safe_relative_file(case.filename, input_dir)
        selected[relative.casefold()] = case
    return sorted(selected.values(), key=lambda case: (case.case_id, case.filename.casefold()))


def _shape(table: dict[str, Any]) -> tuple[int, int]:
    return int(table.get("rowCount") or 0), int(table.get("columnCount") or 0)


def _table_grid(table: dict[str, Any]) -> list[list[str]]:
    rows, columns = _shape(table)
    grid = [["" for _ in range(columns)] for _ in range(rows)]
    for cell in table.get("cells") or []:
        row_index = int(cell.get("rowIndex") or 0)
        column_index = int(cell.get("columnIndex") or 0)
        if 0 <= row_index < rows and 0 <= column_index < columns:
            grid[row_index][column_index] = str(cell.get("content") or "")
    return grid


def table_scores(result: dict[str, Any], case: TestCase, ground_truth_dir: Path) -> dict[str, Any]:
    actual_tables = result.get("tables") or []
    scores: dict[str, Any] = {
        "tables_detected": len(actual_tables),
        "expected_table_count": case.expected_table_count if case.expected_table_count is not None else "",
        "detected_table_shapes": json.dumps([f"{_shape(table)[0]}x{_shape(table)[1]}" for table in actual_tables]),
        "expected_table_shapes": "",
        "table_count_match": "",
        "table_shape_match": "",
        "table_cell_accuracy": "",
        "_table_correct_cells": 0,
        "_table_expected_cells": 0,
        "_table_reference_available": False,
    }
    if case.expected_table_count is not None:
        scores["table_count_match"] = len(actual_tables) == case.expected_table_count

    if not case.table_reference:
        return scores
    reference_path = (ground_truth_dir / case.table_reference).resolve()
    try:
        reference_path.relative_to(ground_truth_dir.resolve())
    except ValueError:
        return scores
    if not reference_path.is_file():
        return scores

    expected_tables = json.loads(reference_path.read_text(encoding="utf-8")).get("tables") or []
    expected_shapes = [(len(table), max((len(row) for row in table), default=0)) for table in expected_tables]
    actual_shapes = [_shape(table) for table in actual_tables]
    scores["expected_table_shapes"] = json.dumps([f"{rows}x{cols}" for rows, cols in expected_shapes])
    scores["table_shape_match"] = actual_shapes == expected_shapes

    correct = 0
    expected_count = 0
    for table_index, expected in enumerate(expected_tables):
        actual = _table_grid(actual_tables[table_index]) if table_index < len(actual_tables) else []
        for row_index, expected_row in enumerate(expected):
            for column_index, expected_cell in enumerate(expected_row):
                expected_count += 1
                actual_cell = ""
                if row_index < len(actual) and column_index < len(actual[row_index]):
                    actual_cell = actual[row_index][column_index]
                if normalize_text(actual_cell) == normalize_text(str(expected_cell)):
                    correct += 1
    scores["_table_correct_cells"] = correct
    scores["_table_expected_cells"] = expected_count
    scores["_table_reference_available"] = True
    scores["table_cell_accuracy"] = round(correct / expected_count, 6) if expected_count else ""
    return scores


def _blank_row(run_id: str, run_started: str, case: TestCase, model_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run_started_utc": run_started,
        "model_id": model_id,
        "case_id": case.case_id,
        "file": case.filename,
        "input_type": case.input_type,
        "validation": case.validation,
        "status": "failed",
        "latency_seconds": "",
        "page_units": "",
        "expected_page_units": case.expected_page_units if case.expected_page_units is not None else "",
        "page_unit_count_match": "",
        "paragraphs_detected": "",
        "expected_paragraph_count": case.expected_paragraph_count if case.expected_paragraph_count is not None else "",
        "paragraph_count_match": "",
        "tables_detected": "",
        "expected_table_count": "",
        "detected_table_shapes": "",
        "expected_table_shapes": "",
        "table_count_match": "",
        "table_shape_match": "",
        "table_cell_accuracy": "",
        "figures_detected": "",
        "expected_figure_count": case.expected_figure_count if case.expected_figure_count is not None else "",
        "figure_detection_match": "",
        "embedded_image_marker": case.embedded_image_marker,
        "embedded_image_marker_found": "",
        "embedded_image_absent": "",
        "reference_available": False,
        "exact_match": "",
        "character_error_rate": "",
        "character_accuracy": "",
        "word_error_rate": "",
        "word_accuracy": "",
        "error": "",
        "_reference_chars": 0,
        "_char_errors": 0,
        "_reference_words": 0,
        "_word_errors": 0,
        "_table_correct_cells": 0,
        "_table_expected_cells": 0,
        "_table_reference_available": False,
    }


def analyze_one(
    case: TestCase,
    model_id: str,
    input_dir: Path,
    ground_truth_dir: Path,
    output_dir: Path,
    settings: Settings,
    run_id: str,
    run_started: str,
) -> dict[str, Any]:
    row = _blank_row(run_id, run_started, case, model_id)
    path = input_dir / case.filename
    include_figures = case.expected_figure_count is not None and model_id == case.figure_model
    try:
        result, latency = analyze_file(path, settings, model_id, include_figures=include_figures)
    except Exception as exc:
        safe_error = str(exc).replace(settings.key, "[REDACTED]")[:600]
        row["error"] = f"{type(exc).__name__}: {safe_error}"
        return row

    text = str(result.get("content") or "")
    page_units = len(result.get("pages") or [])
    paragraphs = result.get("paragraphs") or []
    figures = result.get("figures") or []
    row.update(
        status="succeeded",
        latency_seconds=round(latency, 4),
        page_units=page_units,
        paragraphs_detected=len(paragraphs),
        tables_detected=len(result.get("tables") or []),
        figures_detected=len(figures),
    )
    if case.expected_page_units is not None:
        row["page_unit_count_match"] = page_units == case.expected_page_units
    if case.expected_paragraph_count is not None and model_id == case.paragraph_model:
        row["paragraph_count_match"] = len(paragraphs) == case.expected_paragraph_count
    if case.expected_figure_count is not None and model_id == case.figure_model:
        row["figure_detection_match"] = len(figures) == case.expected_figure_count

    row.update(table_scores(result, case, ground_truth_dir))
    if case.embedded_image_marker:
        marker_found = normalize_text(case.embedded_image_marker) in normalize_text(text)
        row["embedded_image_marker_found"] = marker_found
        row["embedded_image_absent"] = not marker_found

    if case.ground_truth:
        reference_path = (ground_truth_dir / case.ground_truth).resolve()
        try:
            reference_path.relative_to(ground_truth_dir.resolve())
        except ValueError:
            reference_path = ground_truth_dir / "__invalid_reference_path__"
        if reference_path.is_file():
            try:
                scores = compare_text(text, reference_path.read_text(encoding="utf-8"))
                row.update(
                    reference_available=True,
                    exact_match=scores["exact_match"],
                    character_error_rate=round(scores["character_error_rate"], 6),
                    character_accuracy=round(scores["character_accuracy"], 6),
                    word_error_rate=round(scores["word_error_rate"], 6),
                    word_accuracy=round(scores["word_accuracy"], 6),
                    _reference_chars=scores["reference_chars"],
                    _char_errors=scores["char_errors"],
                    _reference_words=scores["reference_words"],
                    _word_errors=scores["word_errors"],
                )
            except (OSError, UnicodeError, ValueError) as exc:
                row["error"] = f"Ground-truth scoring failed: {type(exc).__name__}: {exc}"

    relative = Path(case.filename)
    output_base = output_dir / "documents" / model_id / Path(relative.as_posix() + ".parsed")
    try:
        write_json(output_base.with_suffix(".json"), result)
        text_path = output_base.with_suffix(".txt")
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        row["error"] = f"Could not save service result: {exc}"
    return row


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((percent / 100) * len(ordered) + 0.999999) - 1))
    return ordered[index]


def _model_summary(rows: list[dict[str, Any]], wall_seconds: float, workers: int) -> dict[str, Any]:
    succeeded = [row for row in rows if row["status"] == "succeeded"]
    latencies = [float(row["latency_seconds"]) for row in succeeded]
    references = [row for row in succeeded if row["reference_available"]]
    total_chars = sum(int(row["_reference_chars"]) for row in references)
    char_errors = sum(int(row["_char_errors"]) for row in references)
    total_words = sum(int(row["_reference_words"]) for row in references)
    word_errors = sum(int(row["_word_errors"]) for row in references)
    text_accuracy = None
    if references:
        cer = char_errors / max(1, total_chars)
        wer = word_errors / max(1, total_words)
        text_accuracy = {
            "documents_scored": len(references),
            "character_error_rate_weighted": round(cer, 6),
            "character_accuracy_weighted": round(max(0.0, 1 - cer), 6),
            "word_error_rate_weighted": round(wer, 6),
            "word_accuracy_weighted": round(max(0.0, 1 - wer), 6),
            "exact_match_documents": sum(bool(row["exact_match"]) for row in references),
        }

    table_cases = [row for row in succeeded if row["_table_reference_available"]]
    figure_cases = [row for row in succeeded if row["figure_detection_match"] != ""]
    embedded_image_cases = [row for row in succeeded if row["embedded_image_absent"] != ""]
    cell_count = sum(int(row["_table_expected_cells"]) for row in table_cases)
    correct_cells = sum(int(row["_table_correct_cells"]) for row in table_cases)

    return {
        "documents_total": len(rows),
        "documents_succeeded": len(succeeded),
        "documents_failed": len(rows) - len(succeeded),
        "page_units_succeeded": sum(int(row["page_units"]) for row in succeeded),
        "wall_seconds_shared_run": round(wall_seconds, 4),
        "throughput_documents_per_second_shared_run": round(len(succeeded) / wall_seconds, 4) if wall_seconds else 0,
        "workers": workers,
        "latency_seconds": {
            "mean": round(statistics.mean(latencies), 4) if latencies else None,
            "median": round(statistics.median(latencies), 4) if latencies else None,
            "p95": round(_percentile(latencies, 95), 4) if latencies else None,
            "min": round(min(latencies), 4) if latencies else None,
            "max": round(max(latencies), 4) if latencies else None,
        },
        "text_accuracy": text_accuracy,
        "table_structure": {
            "reference_cases": len(table_cases),
            "shape_matches": sum(row["table_shape_match"] is True for row in table_cases),
            "weighted_cell_accuracy": round(correct_cells / cell_count, 6) if cell_count else None,
        },
        "figure_detection": {
            "cases": len(figure_cases),
            "exact_count_matches": sum(row["figure_detection_match"] is True for row in figure_cases),
        },
        "office_embedded_image_limit": {
            "cases": len(embedded_image_cases),
            "marker_absent_as_expected": sum(row["embedded_image_absent"] is True for row in embedded_image_cases),
        },
    }


def _saved_response_path(run_dir: Path, row: dict[str, str]) -> Path | None:
    model_id = row.get("model_id", "")
    filename = row.get("file", "")
    relative = Path(filename)
    if model_id not in SUPPORTED_MODELS or not filename or relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (run_dir / "documents" / model_id / Path(relative.as_posix() + ".json")).resolve()
    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _rescore_saved_result(
    case: TestCase,
    model_id: str,
    result: dict[str, Any],
    source_row: dict[str, str],
    run_id: str,
    run_started: str,
    ground_truth_dir: Path,
) -> dict[str, Any]:
    row = _blank_row(run_id, run_started, case, model_id)
    row.update(
        evaluation_type="offline_rescore",
        source_run_id=source_row.get("run_id", ""),
        source_run_started_utc=source_row.get("run_started_utc", ""),
        status="succeeded",
        latency_seconds=source_row.get("latency_seconds", ""),
    )

    pages = result.get("pages") or []
    paragraphs = result.get("paragraphs") or []
    tables = result.get("tables") or []
    figures = result.get("figures") or []
    text = str(result.get("content") or "")
    row.update(
        page_units=len(pages),
        page_unit_count_match=(len(pages) == case.expected_page_units) if case.expected_page_units is not None else "",
        paragraphs_detected=len(paragraphs),
        expected_paragraph_count=(case.expected_paragraph_count if model_id == case.paragraph_model else ""),
        paragraph_count_match=(len(paragraphs) == case.expected_paragraph_count)
        if case.expected_paragraph_count is not None and model_id == case.paragraph_model
        else "",
        tables_detected=len(tables),
        figures_detected=len(figures),
        expected_figure_count=(case.expected_figure_count if model_id == case.figure_model else ""),
        figure_detection_match=(len(figures) == case.expected_figure_count)
        if case.expected_figure_count is not None and model_id == case.figure_model
        else "",
    )
    row.update(table_scores(result, case, ground_truth_dir))

    if case.embedded_image_marker:
        marker_found = normalize_text(case.embedded_image_marker) in normalize_text(text)
        row["embedded_image_marker_found"] = marker_found
        row["embedded_image_absent"] = not marker_found

    if case.ground_truth:
        reference_path = (ground_truth_dir / case.ground_truth).resolve()
        try:
            reference_path.relative_to(ground_truth_dir.resolve())
        except ValueError:
            reference_path = ground_truth_dir / "__invalid_reference_path__"
        if reference_path.is_file():
            scores = compare_text(text, reference_path.read_text(encoding="utf-8"))
            row.update(
                reference_available=True,
                exact_match=scores["exact_match"],
                character_error_rate=round(scores["character_error_rate"], 6),
                character_accuracy=round(scores["character_accuracy"], 6),
                word_error_rate=round(scores["word_error_rate"], 6),
                word_accuracy=round(scores["word_accuracy"], 6),
                _reference_chars=scores["reference_chars"],
                _char_errors=scores["char_errors"],
                _reference_words=scores["reference_words"],
                _word_errors=scores["word_errors"],
            )
    return row


def command_rescore(args: argparse.Namespace) -> int:
    """Recalculate matrix metrics from saved Azure JSON without making service calls."""
    try:
        matrix = load_test_matrix(args.matrix)
        by_id = {case.case_id.casefold(): case for case in matrix}
        selected_cases: dict[str, TestCase] = {}
        for requested in args.case:
            case = by_id.get(requested.casefold())
            if case is None:
                known = ", ".join(item.case_id for item in matrix)
                raise ValueError(f"Unknown test case '{requested}'. Available cases: {known}")
            selected_cases[case.case_id] = case
        if not selected_cases:
            raise ValueError("Select at least one test case to re-score.")

        source_root = args.source_results.resolve()
        candidates: dict[tuple[str, str], list[tuple[str, str, Path, dict[str, str], Path]]] = {}
        for run_dir in source_root.iterdir():
            csv_path = run_dir / "documents.csv"
            if not run_dir.is_dir() or not csv_path.is_file():
                continue
            try:
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                summary = {}
            if summary.get("evaluation_type") == "offline_rescore":
                continue
            with csv_path.open(newline="", encoding="utf-8-sig") as source:
                for source_row in csv.DictReader(source):
                    case_id = source_row.get("case_id", "")
                    model_id = source_row.get("model_id", "")
                    if case_id not in selected_cases or source_row.get("status") != "succeeded":
                        continue
                    raw_path = _saved_response_path(run_dir, source_row)
                    if raw_path is None:
                        continue
                    source_run_id = source_row.get("run_id") or run_dir.name
                    candidates.setdefault((case_id, model_id), []).append(
                        (source_row.get("run_started_utc", ""), source_run_id, run_dir, source_row, raw_path)
                    )

        chosen: list[tuple[TestCase, str, str, Path, dict[str, str], Path]] = []
        for key, records in sorted(candidates.items()):
            started, source_run_id, source_dir, source_row, raw_path = max(records, key=lambda record: (record[0], record[1]))
            source_row = {**source_row, "run_id": source_run_id}
            chosen.append((selected_cases[key[0]], key[1], source_run_id, source_dir, source_row, raw_path))
        missing = [case_id for case_id in selected_cases if not any(case.case_id == case_id for case, *_ in chosen)]
        if missing:
            raise ValueError(f"No successful saved Azure response JSON was found for: {', '.join(missing)}")

        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_started = datetime.now(timezone.utc).isoformat()
        output_root = args.output.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        output_dir = output_root / run_id
        stage_dir = output_root / f".{run_id}.staging"
        if output_dir.exists() or stage_dir.exists():
            raise ValueError(f"Re-score output already exists: {output_dir}")
        stage_dir.mkdir()

        rows: list[dict[str, Any]] = []
        source_run_ids: set[str] = set()
        for case, model_id, source_run_id, _source_dir, source_row, raw_path in chosen:
            try:
                result = json.loads(raw_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read saved response JSON for {case.case_id} / {model_id}.") from exc
            if not isinstance(result, dict):
                raise ValueError(f"Saved response JSON is invalid for {case.case_id} / {model_id}.")
            row = _rescore_saved_result(case, model_id, result, source_row, run_id, run_started, args.ground_truth.resolve())
            row["source_run_id"] = source_run_id
            source_run_ids.add(source_run_id)
            rows.append(row)
            relative = Path(case.filename)
            output_base = stage_dir / "documents" / model_id / Path(relative.as_posix() + ".parsed")
            write_json(output_base.with_suffix(".json"), result)
            text_path = output_base.with_suffix(".txt")
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(str(result.get("content") or ""), encoding="utf-8")

        rows.sort(key=lambda row: (row["case_id"], row["model_id"], row["file"].casefold()))
        models = sorted({row["model_id"] for row in rows}, key=lambda model: SUPPORTED_MODELS.index(model))
        per_model = {
            model_id: _model_summary([row for row in rows if row["model_id"] == model_id], 0.0, 0)
            for model_id in models
        }
        summary = {
            "run_id": run_id,
            "run_started_utc": run_started,
            "rescored_at_utc": run_started,
            "evaluation_type": "offline_rescore",
            "selected_cases": sorted(selected_cases),
            "models": models,
            "requests_total": 0,
            "wall_seconds": 0,
            "source_run_ids": sorted(source_run_ids),
            "performance_note": "No Azure requests were made. Document latency values are carried over from the source Azure runs; offline re-scoring is not a new performance sample.",
            "accuracy_note": "Text accuracy uses the current human-reviewed transcripts after case and whitespace normalization; CER/WER are text similarity, not semantic field accuracy.",
            "by_model": per_model,
        }
        with (stage_dir / "documents.csv").open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=RESCORE_CSV_FIELDS)
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in RESCORE_CSV_FIELDS} for row in rows)
        write_json(stage_dir / "summary.json", summary)
        stage_dir.rename(output_dir)
    except (OSError, ValueError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        if "stage_dir" in locals() and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        print(f"Offline re-score failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Offline re-score complete: {len(rows)} saved response(s); 0 Azure requests.")
    for model_id, model_summary in per_model.items():
        accuracy = model_summary["text_accuracy"]
        text_accuracy = "text accuracy: not scored"
        if accuracy:
            text_accuracy = (
                f"weighted char/word accuracy: {accuracy['character_accuracy_weighted']:.2%}/"
                f"{accuracy['word_accuracy_weighted']:.2%}"
            )
        print(f"{model_id}: {model_summary['documents_succeeded']}/{model_summary['documents_total']} responses; {text_accuracy}")
    print(f"Re-score snapshot: {output_dir}")
    print("Original runs and cumulative API history were left unchanged.")
    return 0


def _run_history_row(run_id: str, run_started: str, model_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    text = summary["text_accuracy"] or {}
    table = summary["table_structure"]
    figure = summary["figure_detection"]
    office = summary["office_embedded_image_limit"]
    latency = summary["latency_seconds"]
    return {
        "run_id": run_id,
        "run_started_utc": run_started,
        "model_id": model_id,
        "workers": summary["workers"],
        "documents_total": summary["documents_total"],
        "documents_succeeded": summary["documents_succeeded"],
        "documents_failed": summary["documents_failed"],
        "page_units_succeeded": summary["page_units_succeeded"],
        "wall_seconds": summary["wall_seconds_shared_run"],
        "throughput_documents_per_second": summary["throughput_documents_per_second_shared_run"],
        "latency_mean_seconds": latency["mean"],
        "latency_median_seconds": latency["median"],
        "latency_p95_seconds": latency["p95"],
        "documents_with_ground_truth": text.get("documents_scored", 0),
        "character_accuracy_weighted": text.get("character_accuracy_weighted", ""),
        "word_accuracy_weighted": text.get("word_accuracy_weighted", ""),
        "table_cases": table["reference_cases"],
        "table_shape_matches": table["shape_matches"],
        "table_cell_accuracy_weighted": table["weighted_cell_accuracy"] if table["weighted_cell_accuracy"] is not None else "",
        "figure_cases": figure["cases"],
        "figure_detection_matches": figure["exact_count_matches"],
        "embedded_image_cases": office["cases"],
        "embedded_image_absence_passes": office["marker_absent_as_expected"],
    }


def command_check() -> int:
    try:
        settings = load_settings()
        details, elapsed = check_connection(settings)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except AzureError as exc:
        safe_error = str(exc).replace(settings.key, "[REDACTED]")
        print(f"Connection failed: {type(exc).__name__}: {safe_error}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Connection check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("Connection successful: authenticated resource-info request returned successfully.")
    print(f"Check latency: {elapsed:.3f} seconds")
    print(f"Resource details: {json.dumps(details, ensure_ascii=False)}")
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    input_dir = args.input.resolve()
    ground_truth_dir = args.ground_truth.resolve()
    output_root = args.output.resolve()
    if args.workers < 1:
        print("--workers must be at least 1.", file=sys.stderr)
        return 2
    models = list(dict.fromkeys(args.models or DEFAULT_MODELS))
    try:
        matrix = load_test_matrix(args.matrix.resolve())
        cases = select_cases(matrix, input_dir, args.case, args.file)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Selection error: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("No documents selected.", file=sys.stderr)
        return 2

    request_count = len(cases) * len(models)
    if args.dry_run:
        print(f"Dry run: {len(cases)} document(s) x {len(models)} model(s) = {request_count} request(s).")
        print(f"Input folder: {input_dir}")
        print(f"Models: {', '.join(models)}")
        for case in cases:
            reference = f"; ground truth: {case.ground_truth}" if case.ground_truth else ""
            print(f"{case.case_id}: {case.filename} — {case.validation}{reference}")
        print("No Azure request was made.")
        return 0

    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    run_started = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Analyzing {len(cases)} document(s) with {len(models)} model(s) ({request_count} request(s), {args.workers} worker(s))...")

    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                analyze_one,
                case,
                model_id,
                input_dir,
                ground_truth_dir,
                output_dir,
                settings,
                run_id,
                run_started,
            ): (case, model_id)
            for case in cases
            for model_id in models
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            latency = f"{row['latency_seconds']}s" if row["latency_seconds"] != "" else "failed"
            print(f"[{row['status']}] {row['case_id']} {row['model_id']} {row['file']} ({latency})")

    wall_seconds = time.perf_counter() - started
    rows.sort(key=lambda row: (row["case_id"], row["model_id"], row["file"].casefold()))
    per_model: dict[str, dict[str, Any]] = {}
    for model_id in models:
        per_model[model_id] = _model_summary([row for row in rows if row["model_id"] == model_id], wall_seconds, args.workers)
    summary = {
        "run_id": run_id,
        "run_started_utc": run_started,
        "input_folder": str(input_dir),
        "selected_cases": [case.case_id for case in cases],
        "models": models,
        "requests_total": request_count,
        "wall_seconds": round(wall_seconds, 4),
        "accuracy_note": "Text accuracy uses human-checked transcripts after case and whitespace normalization; CER/WER are text similarity, not semantic field accuracy.",
        "format_notes": [
            "For XLSX, v4 Layout/Read return worksheet text; structured table analysis is not supported for XLSX.",
            "Embedded or linked images in Word and Excel are not analyzed by Read/Layout.",
            "T04 figure detection is scored for prebuilt-layout; figure crop output is requested for that case.",
        ],
        "by_model": per_model,
    }

    clean_rows = [{key: row.get(key, "") for key in CSV_FIELDS} for row in rows]
    with (output_dir / "documents.csv").open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(clean_rows)
    write_json(output_dir / "summary.json", summary)

    history_rows = [
        _run_history_row(run_id, run_started, model_id, per_model[model_id])
        for model_id in models
    ]
    try:
        append_csv(output_root / "history" / "document_metrics.csv", CSV_FIELDS, clean_rows)
        append_csv(output_root / "history" / "run_metrics.csv", RUN_HISTORY_FIELDS, history_rows)
    except (OSError, ValueError) as exc:
        print(f"Could not update cumulative metrics history: {exc}", file=sys.stderr)
        return 1

    print(f"\nRun complete: {sum(row['status'] == 'succeeded' for row in rows)} succeeded, {sum(row['status'] == 'failed' for row in rows)} failed")
    for model_id, model_summary in per_model.items():
        latency = model_summary["latency_seconds"]
        accuracy = model_summary["text_accuracy"]
        accuracy_text = "text accuracy: not scored"
        if accuracy:
            accuracy_text = (
                f"weighted char/word accuracy: {accuracy['character_accuracy_weighted']:.2%}/"
                f"{accuracy['word_accuracy_weighted']:.2%}"
            )
        print(
            f"{model_id}: {model_summary['documents_succeeded']}/{model_summary['documents_total']} succeeded; "
            f"median/p95 {latency['median']}s/{latency['p95']}s; {accuracy_text}"
        )
    print(f"Results: {output_dir}")
    print(f"Cumulative metrics: {output_root / 'history'}")
    return 0 if all(row["status"] == "succeeded" for row in rows) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docintel-poc", description="Azure AI Document Intelligence benchmark POC")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Test endpoint/key connectivity with a resource-info call")

    dashboard = subparsers.add_parser("dashboard", help="Browse saved results in a local dashboard")
    dashboard.add_argument("--port", type=int, default=8765, help="Local dashboard port (default: 8765)")

    analyze = subparsers.add_parser("analyze", help="Analyze selected test cases and append model metrics")
    analyze.add_argument("--input", type=Path, default=ROOT / "data" / "documents", help="Folder containing test documents")
    analyze.add_argument("--ground-truth", type=Path, default=ROOT / "data" / "ground_truth", help="Folder containing matching reference files")
    analyze.add_argument("--matrix", type=Path, default=ROOT / "data" / "test_matrix.csv", help="Test matrix CSV")
    analyze.add_argument("--output", type=Path, default=ROOT / "results", help="Folder for run results and cumulative metrics")
    analyze.add_argument("--case", action="extend", nargs="+", help="Test case ID(s), such as --case T01 T03")
    analyze.add_argument("--file", action="append", help="Additional input filename relative to --input; repeat for multiple files")
    analyze.add_argument(
        "--models",
        nargs="+",
        choices=SUPPORTED_MODELS,
        default=None,
        help="Models to compare; defaults to prebuilt-read and prebuilt-layout",
    )
    analyze.add_argument("--workers", type=int, default=1, help="Concurrent requests (start at 1; increase carefully)")
    analyze.add_argument("--dry-run", action="store_true", help="List selected inputs and models without calling Azure")

    rescore = subparsers.add_parser("rescore", help="Recalculate saved Azure responses against the current matrix without calling Azure")
    rescore.add_argument("--case", nargs="+", required=True, help="Test case ID(s), such as --case T01 T03")
    rescore.add_argument("--matrix", type=Path, default=ROOT / "data" / "test_matrix.csv", help="Test matrix CSV")
    rescore.add_argument("--ground-truth", type=Path, default=ROOT / "data" / "ground_truth", help="Folder containing matching reference files")
    rescore.add_argument("--source-results", type=Path, default=ROOT / "results", help="Folder containing saved Azure results")
    rescore.add_argument("--output", type=Path, default=ROOT / "results", help="Folder for the offline re-score snapshot")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "check":
        raise SystemExit(command_check())
    if args.command == "analyze":
        raise SystemExit(command_analyze(args))
    if args.command == "rescore":
        raise SystemExit(command_rescore(args))
    if args.command == "dashboard":
        serve_dashboard(args.port)
        return
    parser.error("Unknown command")


if __name__ == "__main__":
    main()
