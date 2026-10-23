from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from azure.ai.documentintelligence import DocumentIntelligenceAdministrationClient, DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

from .config import Settings


def check_connection(settings: Settings) -> tuple[dict[str, Any], float]:
    """Call the authenticated resource-info operation to verify endpoint and key."""
    client = DocumentIntelligenceAdministrationClient(
        endpoint=settings.endpoint,
        credential=AzureKeyCredential(settings.key),
    )
    started = time.perf_counter()
    try:
        details = client.get_resource_details()
        elapsed = time.perf_counter() - started
        if hasattr(details, "as_dict"):
            details = details.as_dict()
        return details, elapsed
    finally:
        client.close()


def analyze_file(
    path: Path,
    settings: Settings,
    model_id: str,
    include_figures: bool = False,
) -> tuple[dict[str, Any], float]:
    """Analyze one supported file and return the SDK result plus service latency."""
    client = DocumentIntelligenceClient(
        endpoint=settings.endpoint,
        credential=AzureKeyCredential(settings.key),
    )
    try:
        with path.open("rb") as document:
            started = time.perf_counter()
            options: dict[str, Any] = {}
            if include_figures:
                options["output"] = ["figures"]
            poller = client.begin_analyze_document(model_id, body=document, **options)
            result = poller.result()
            elapsed = time.perf_counter() - started
        return result.as_dict(), elapsed
    finally:
        client.close()

