from __future__ import annotations

import csv
import json
import math
import re
import statistics
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import ROOT
from .test_matrix import load_test_matrix


RESULTS_ROOT = ROOT / "results"
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-\d{6}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9.-]+$")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _confidence_value(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and 0 <= parsed <= 1 else None


def _confidence_summary(result: Any) -> dict[str, Any]:
    """Summarize word confidence and retain named prebuilt field confidence."""
    if not isinstance(result, dict):
        return {"word": None, "fields": []}

    word_confidences = [
        confidence
        for page in result.get("pages") or []
        if isinstance(page, dict)
        for word in page.get("words") or []
        if isinstance(word, dict)
        if (confidence := _confidence_value(word.get("confidence"))) is not None
    ]
    word_summary = None
    if word_confidences:
        word_summary = {
            "count": len(word_confidences),
            "mean": round(statistics.mean(word_confidences), 6),
            "median": round(statistics.median(word_confidences), 6),
            "minimum": round(min(word_confidences), 6),
            "below_80_count": sum(value < 0.8 for value in word_confidences),
        }

    fields: list[dict[str, Any]] = []

    def collect_field(name: str, field: Any) -> None:
        if not isinstance(field, dict):
            return
        confidence = _confidence_value(field.get("confidence"))
        if confidence is not None:
            fields.append({"name": name, "confidence": round(confidence, 6)})
        value_object = field.get("valueObject")
        if isinstance(value_object, dict):
            for child_name, child in value_object.items():
                collect_field(f"{name}.{child_name}", child)
        value_array = field.get("valueArray")
        if isinstance(value_array, list):
            for index, item in enumerate(value_array):
                item_name = f"{name}[{index}]"
                collect_field(item_name, item)

    for document_index, document in enumerate(result.get("documents") or []):
        if not isinstance(document, dict):
            continue
        document_fields = document.get("fields") or {}
        if isinstance(document_fields, dict):
            for name, field in document_fields.items():
                collect_field(f"Document {document_index + 1} · {name}", field)

    return {"word": word_summary, "fields": fields}


def _dashboard_payload() -> dict[str, Any]:
    cases = []
    matrix_path = ROOT / "data" / "test_matrix.csv"
    if matrix_path.is_file():
        for case in load_test_matrix(matrix_path):
            cases.append(
                {
                    "case_id": case.case_id,
                    "filename": case.filename,
                    "input_type": case.input_type,
                    "validation": case.validation,
                    "ground_truth": case.ground_truth,
                    "expected_page_units": case.expected_page_units,
                    "expected_table_count": case.expected_table_count,
                    "expected_figure_count": case.expected_figure_count,
                    "notes": case.notes,
                }
            )

    results: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    if RESULTS_ROOT.is_dir():
        for run_dir in sorted(RESULTS_ROOT.iterdir(), key=lambda item: item.name):
            if not run_dir.is_dir() or not (run_dir / "documents.csv").is_file():
                continue
            summary = _read_json(run_dir / "summary.json", {})
            run_id = str(summary.get("run_id") or run_dir.name)
            run_started = str(summary.get("run_started_utc") or "")
            runs.append(
                {
                    "run_id": run_id,
                    "run_started_utc": run_started,
                    "evaluation_type": str(summary.get("evaluation_type") or "azure_analysis"),
                    "summary": summary,
                }
            )
            try:
                with (run_dir / "documents.csv").open(newline="", encoding="utf-8-sig") as source:
                    for row in csv.DictReader(source):
                        row["run_id"] = row.get("run_id") or run_id
                        row["run_started_utc"] = row.get("run_started_utc") or run_started
                        row["evaluation_type"] = row.get("evaluation_type") or str(summary.get("evaluation_type") or "azure_analysis")
                        paths = _safe_result_paths(row["run_id"], row.get("model_id", ""), row.get("file", ""))
                        raw_result = _read_json(paths[0], {}) if paths else {}
                        row["confidence"] = _confidence_summary(raw_result)
                        results.append(row)
            except (OSError, UnicodeError, csv.Error):
                continue

    results.sort(key=lambda row: (row.get("run_started_utc", ""), row.get("run_id", "")))
    runs.sort(key=lambda row: (row.get("run_started_utc", ""), row.get("run_id", "")))
    return {
        "cases": cases,
        "results": results,
        "runs": runs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _safe_result_paths(run_id: str, model_id: str, filename: str) -> tuple[Path, Path] | None:
    if not RUN_ID_RE.fullmatch(run_id) or not MODEL_ID_RE.fullmatch(model_id):
        return None
    relative = PurePosixPath(filename)
    if not filename or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None

    run_dir = (RESULTS_ROOT / run_id).resolve()
    results_root = RESULTS_ROOT.resolve()
    try:
        run_dir.relative_to(results_root)
    except ValueError:
        return None

    output_name = Path(relative.as_posix() + ".parsed")
    json_path = (run_dir / "documents" / model_id / output_name).with_suffix(".json").resolve()
    text_path = (run_dir / "documents" / model_id / output_name).with_suffix(".txt").resolve()
    try:
        json_path.relative_to(results_root)
        text_path.relative_to(results_root)
    except ValueError:
        return None
    return json_path, text_path


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DocIntelPocDashboard/1.0"

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API name
        request = urlsplit(self.path)
        if request.path == "/":
            try:
                body = DASHBOARD_HTML.read_bytes()
            except OSError:
                self._send(500, "text/plain; charset=utf-8", b"Dashboard page is unavailable.")
                return
            self._send(200, "text/html; charset=utf-8", body)
            return

        if request.path == "/api/data":
            try:
                self._send_json(200, _dashboard_payload())
            except (OSError, ValueError, csv.Error) as exc:
                self._send_json(500, {"error": f"Could not read dashboard data: {type(exc).__name__}"})
            return

        if request.path == "/api/document":
            query = parse_qs(request.query)
            run_id = (query.get("run_id") or [""])[0]
            model_id = (query.get("model_id") or [""])[0]
            filename = (query.get("filename") or [""])[0]
            paths = _safe_result_paths(run_id, model_id, filename)
            if paths is None:
                self._send_json(400, {"error": "Invalid result selection."})
                return
            json_path, text_path = paths
            result = _read_json(json_path, None)
            if result is None:
                self._send_json(404, {"error": "Saved response JSON was not found for this run."})
                return
            try:
                text = text_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                text = str(result.get("content") or "")
            self._send_json(200, {"text": text, "result": result})
            return

        self._send(404, "text/plain; charset=utf-8", b"Not found.")

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the demo terminal readable without logging document content.
        super().log_message(format, *args)


def serve_dashboard(port: int = 8765) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Document Intelligence results dashboard: http://127.0.0.1:{port}")
    print("Local results only; this dashboard does not call Azure. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Show saved Azure Document Intelligence POC results locally.")
    parser.add_argument("--port", type=int, default=8765, help="Local server port (default: 8765)")
    args = parser.parse_args()
    serve_dashboard(args.port)


if __name__ == "__main__":
    main()
