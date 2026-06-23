"""The provenance header writer round-trips with the reader."""

from __future__ import annotations

from datetime import UTC, datetime

from carve.integrations.dlt.code_emitter import emit_provenance_header, with_provenance_header
from carve.integrations.provenance import is_carve_generated, parse_provenance_header


def test_full_header_round_trips() -> None:
    header = emit_provenance_header(
        source="carve/sources/stripe",
        commit="abc1234",
        destination="duckdb.raw_stripe",
        generated_at=datetime(2026, 5, 19, 14, 23, 1, tzinfo=UTC),
        plan_id="plan_a1b2",
        build_id="build_c3d4",
    )
    parsed = parse_provenance_header(header)
    assert parsed is not None
    assert parsed.source == "carve/sources/stripe"
    assert parsed.commit == "abc1234"
    assert parsed.destination == "duckdb.raw_stripe"
    assert parsed.generated_at == "2026-05-19T14:23:01+00:00"
    assert parsed.plan_id == "plan_a1b2"
    assert parsed.build_id == "build_c3d4"


def test_minimal_header_is_still_recognized() -> None:
    header = emit_provenance_header()
    assert is_carve_generated(header)
    parsed = parse_provenance_header(header)
    assert parsed is not None
    assert parsed.source is None and parsed.plan_id is None


def test_generated_at_accepts_string_verbatim() -> None:
    header = emit_provenance_header(generated_at="2026-01-01T00:00:00Z")
    assert parse_provenance_header(header).generated_at == "2026-01-01T00:00:00Z"


def test_generated_at_defaults_to_now() -> None:
    header = emit_provenance_header()
    # A timestamp was stamped (non-empty, ISO-ish) even with no value passed.
    assert parse_provenance_header(header).generated_at


def test_with_header_preserves_body() -> None:
    body = "import dlt\n\n\n@dlt.source\ndef s(): ...\n"
    out = with_provenance_header(body, source="carve/sources/x", commit="deadbee")
    assert is_carve_generated(out)
    assert out.endswith(body)  # body verbatim below the header


def test_with_header_does_not_double_stamp() -> None:
    once = with_provenance_header("x = 1\n", source="carve/sources/x", commit="abc1234")
    twice = with_provenance_header(once, source="carve/sources/y", commit="ffffff0")
    assert twice == once  # already Carve-generated → unchanged


def test_commit_period_not_swept_into_sha() -> None:
    # The marker ends with a period; the reader must not capture it in commit.
    parsed = parse_provenance_header(emit_provenance_header(source="s", commit="abc1234"))
    assert parsed.commit == "abc1234"
