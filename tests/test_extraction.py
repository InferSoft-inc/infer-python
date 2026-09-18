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
                "display_type": "currency",
                "group_name": "Financials",
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
    assert item.value.display_type == "currency"
    assert item.value.group_name == "Financials"
    assert item.value.parsed_value == 1234.5
    assert item.value.raw_value == "$1,234.50"
    tb = item.value.traceback[0]
    assert tb.page == 2 and tb.confidence == 0.98
    # bbox PascalCase fields are aliased to snake_case.
    assert (tb.bbox.top, tb.bbox.left, tb.bbox.width, tb.bbox.height) == (0.1, 0.2, 0.3, 0.05)


def test_extraction_results_absent_without_prompts(client, httpx_mock):
    doc_json = {k: v for k, v in _DOC_WITH_EXTRACTION.items() if k != "extraction_results"}
    httpx_mock.add_response(method="GET", url="https://api.test/api/documents/1", json=doc_json)

    doc = client.documents.get(1)
    assert doc.extraction_results is None


def test_typed_value_fields_keep_precision_and_dates(client, httpx_mock):
    from datetime import date
    from decimal import Decimal

    doc = {
        **_DOC_WITH_EXTRACTION,
        "extraction_results": [
            {
                "prompt_id": 5,
                "value": {
                    "name": "Royalty",
                    "data_type": "Number",
                    "parsed_value": 0.125,
                    "value_number": "0.12345678901234567890123",
                    "raw_value": "1/8",
                },
            },
            {
                "prompt_id": 6,
                "value": {
                    "name": "Effective Date",
                    "data_type": "Date",
                    "parsed_value": "2024-02-29",
                    "value_date": "2024-02-29",
                    "raw_value": "02/29/2024",
                },
            },
            {
                "prompt_id": 7,
                "value": {
                    "name": "Has Pugh",
                    "data_type": "Boolean",
                    "value_bool": False,
                    "raw_value": "No",
                },
            },
            {
                "prompt_id": 8,
                "value": {
                    "name": "Executed Date",
                    "data_type": "Date",
                    "parsed_value": None,
                    "raw_value": "Not Found",
                },
            },
        ],
    }
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/documents/1?prompt_ids=5%2C6%2C7%2C8",
        json=doc,
    )

    got = client.documents.get(1, prompt_ids=[5, 6, 7, 8])
    by_prompt = {item.prompt_id: item.value for item in got.extraction_results}

    assert by_prompt[5].value_number == Decimal("0.12345678901234567890123")
    assert by_prompt[5].value == Decimal("0.12345678901234567890123")
    assert by_prompt[6].value_date == date(2024, 2, 29)
    assert by_prompt[6].value == date(2024, 2, 29)
    assert by_prompt[7].value_bool is False
    assert by_prompt[7].value is False
    assert by_prompt[8].value is None
    assert by_prompt[8].raw_value == "Not Found"


def test_value_falls_back_to_parsed_value_for_older_servers():
    from infersoft.models import ExtractionResultValue

    legacy = ExtractionResultValue.model_validate({"data_type": "Number", "parsed_value": 1234.5})
    assert legacy.value == 1234.5
