"""Phase 5: typed extraction results returned by documents.get/search."""

from __future__ import annotations

from infersoft import DataTypeName

_DOC_WITH_EXTRACTION = {
    "id": 1,
    "organization_id": "org",
    "name": "invoice.pdf",
    "status": "ready",
    "is_valid": True,
    "created_at": "2026-05-29T00:00:00Z",
    "has_active_workflow": False,
    "extraction_results": [
        {
            "prompt_id": 5,
            "value": {
                "name": "Total",
                "data_type": "Number",
                "parsed_value": 1234.5,
                "raw_value": "$1,234.50",
                "traceback": [
                    {
                        "bbox": {"Top": 0.1, "Left": 0.2, "Width": 0.3, "Height": 0.05},
                        "page": 2,
                        "confidence": 0.98,
                        "issues": [],
                    }
                ],
                "issues": [],
                "readability": 0.9,
            },
        }
    ],
}


def test_get_with_prompts_returns_typed_extraction(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/documents/1?prompt_ids=5",
        json=_DOC_WITH_EXTRACTION,
    )

    doc = client.documents.get(1, prompt_ids=[5])

    assert doc.extraction_results is not None
    item = doc.extraction_results[0]
    assert item.prompt_id == 5
    assert item.value.data_type is DataTypeName.number
    assert item.value.parsed_value == 1234.5
    assert item.value.raw_value == "$1,234.50"
    tb = item.value.traceback[0]
    assert tb.page == 2 and tb.confidence == 0.98
    # bbox PascalCase fields are aliased to snake_case.
    assert (tb.bbox.top, tb.bbox.left, tb.bbox.width, tb.bbox.height) == (0.1, 0.2, 0.3, 0.05)


def test_extraction_results_absent_without_prompts(client, httpx_mock):
    doc_json = {k: v for k, v in _DOC_WITH_EXTRACTION.items() if k != "extraction_results"}
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/documents/1", json=doc_json
    )

    doc = client.documents.get(1)
    assert doc.extraction_results is None
