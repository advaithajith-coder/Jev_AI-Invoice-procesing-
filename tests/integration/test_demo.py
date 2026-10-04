from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from jev_invoice.config import Settings
from jev_invoice.db.models import AppSetting, Vendor
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.demo.__main__ import reset_database
from jev_invoice.demo.pack import SAMPLES, render_text, write_pack
from jev_invoice.ingest.parsing import parse_document

TODAY = date(2026, 10, 4)


def test_dates_are_relative_to_the_day_the_pack_is_built() -> None:
    text = "Date: {{date-2}} Due: {{date+28}} Rent for {{month}} (HP-{{ym}}) {{date}}"

    assert render_text(text, TODAY) == (
        "Date: 10/02/2026 Due: 11/01/2026 Rent for October 2026 (HP-2026-10) 10/04/2026"
    )


def test_every_sample_renders_to_a_file_the_app_can_read(tmp_path: Path) -> None:
    files = write_pack(tmp_path, TODAY)

    assert [path.name for _, path in files] == [sample.filename for sample in SAMPLES]
    for sample, path in files:
        parsed = parse_document(path.read_bytes(), path.name, sample.content_type)
        assert "{{" not in parsed.text, path.name
        if sample.form == "png":
            assert parsed.needs_ocr, "the scan must go through the vision model"
        else:
            assert "Northwind Analytics Inc" in parsed.text, path.name
        if sample.email:
            assert parsed.sender_email, path.name


def test_pack_covers_every_outcome_and_keeps_one_back() -> None:
    expected = {action for sample in SAMPLES for action in sample.expect}

    assert len(expected) == 5
    assert sum(sample.jev_only for sample in SAMPLES) == 2
    assert [sample.filename for sample in SAMPLES if not sample.expect] == ["northline-courier.pdf"]


async def test_reset_backs_up_empties_and_reseeds(seeded_settings: Settings) -> None:
    settings = seeded_settings
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            session.add(AppSetting(key="approval_limit", value="500", updated_by="test"))
        stored = settings.storage_dir / "ab" / "old-upload.pdf"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"%PDF-1.4")

        backup = await reset_database(engine, settings)

        async with create_session_factory(engine)() as session:
            assert await session.scalar(select(func.count()).select_from(AppSetting)) == 0
            assert await session.scalar(select(func.count()).select_from(Vendor)) == 11
        assert backup.exists()
        assert not stored.exists()
    finally:
        await engine.dispose()
