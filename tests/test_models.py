from filings_library.models import Citation, CompanyTarget, LibraryTargetsPayload


def test_models_roundtrip() -> None:
    payload = LibraryTargetsPayload.model_validate(
        {
            "schema": 1,
            "generated_at": "2026-11-26T00:00:00Z",
            "companies": [
                {
                    "source_key": "example-corp",
                    "ticker": "EXMP",
                    "exchange": "XNAS",
                    "name": "Example Corp",
                    "sec_cik": 123456,
                }
            ],
            "scopes": [{"person": "alice", "source_keys": ["example-corp"]}],
        }
    )
    assert payload.schema_version == 1
    assert isinstance(payload.companies[0], CompanyTarget)
    c = Citation(
        document_id="doc1",
        source_key="example-corp",
        form_type="10-Q",
        filing_date="2026-11-01",
        source_sha256="0" * 64,
        locator="section:revenue",
        source_url="https://sec.gov/example",
        converter_id="html-ixbrl@1",
    )
    assert c.locator == "section:revenue"
