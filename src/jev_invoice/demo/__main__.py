"""Reset the local database to the demo state. Run with `uv run poe demo`.

1. Back up the SQLite database to data/jev_invoice.before-demo.db.
2. Empty every table and delete the stored originals.
3. Load the master data again (vendors, POs, deliveries, accounts).
4. Write the sample pack to data/demo-pack/ and run it through the real pipeline.
5. Report what each sample was meant to show and what actually happened.

The models are real (Groq and Jev), so a sample can land differently from what it was
written for; the report says so instead of hiding it. Only the local SQLite database is
reset: PostgreSQL keeps decisions and audit events append-only, and should.
"""

import asyncio
import logging
import shutil
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jev_invoice.config import Settings, get_settings
from jev_invoice.core.logging import configure_logging
from jev_invoice.db import models  # noqa: F401 - registers every table on Base.metadata
from jev_invoice.db.base import Base
from jev_invoice.db.models import Decision, Invoice
from jev_invoice.db.seed import seed
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.decision.jev_client import JevClient
from jev_invoice.decision.service import DecisionService
from jev_invoice.demo.pack import Sample, write_pack
from jev_invoice.domain.enums import DecisionAction
from jev_invoice.extraction.service import ExtractionService
from jev_invoice.extraction.vision import TranscriptionService
from jev_invoice.ingest.storage import LocalFileStorage
from jev_invoice.pipeline.service import InvoicePipeline
from jev_invoice.web.labels import ACTIONS, STATUSES, reason


@dataclass(frozen=True)
class Outcome:
    sample: Sample
    invoice_id: uuid.UUID | None = None
    action: DecisionAction | None = None
    reason_codes: list[str] = field(default_factory=list)
    code_findings: int = 0
    status: str | None = None

    @property
    def as_expected(self) -> bool:
        sample = self.sample
        return (
            self.action in sample.expect
            and (sample.expect_reason is None or sample.expect_reason in self.reason_codes)
            and (not sample.jev_only or self.code_findings == 0)
        )


def pack_directory(settings: Settings) -> Path:
    return settings.storage_dir.parent / "demo-pack"


async def reset_database(engine: AsyncEngine, settings: Settings) -> Path:
    """Back up the database, empty it, delete stored files, and seed master data."""
    database = Path(make_url(settings.database_url).database or "")
    backup = database.with_name(f"{database.stem}.before-demo{database.suffix}")
    backup.unlink(missing_ok=True)
    async with engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"VACUUM INTO '{backup.as_posix()}'")

    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    shutil.rmtree(settings.storage_dir, ignore_errors=True)

    async with create_session_factory(engine)() as session, session.begin():
        await seed(session, settings)
    return backup


async def load_pack(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    files: Sequence[tuple[Sample, Path]],
    *,
    extraction: ExtractionService | None,
    transcription: TranscriptionService | None,
    decisions: DecisionService,
) -> list[Outcome]:
    """Process the samples in order, as if each had just been uploaded."""
    storage = LocalFileStorage(settings.storage_dir)
    outcomes = []
    for sample, path in files:
        if not sample.expect:
            continue
        print(f"  {path.name:<32}", end="", flush=True)
        async with session_factory() as session:
            pipeline = InvoicePipeline(
                session,
                settings,
                storage=storage,
                extraction=extraction,
                decisions=decisions,
                transcription=transcription,
            )
            result = await pipeline.process_upload(
                path.read_bytes(), path.name, sample.content_type
            )
            outcomes.append(await _outcome(session, sample, result.invoice_id))
        print(_line(outcomes[-1]), flush=True)
    return outcomes


async def _outcome(session: AsyncSession, sample: Sample, invoice_id: uuid.UUID) -> Outcome:
    invoice = await session.get(Invoice, invoice_id)
    decision = await session.scalar(
        select(Decision)
        .where(Decision.invoice_id == invoice_id)
        .order_by(Decision.created_at.desc())
        .limit(1)
    )
    status = STATUSES[invoice.status][0] if invoice is not None else None
    if decision is None:
        return Outcome(sample, invoice_id, status=status)
    return Outcome(
        sample,
        invoice_id,
        action=decision.action,
        reason_codes=list(decision.reason_codes),
        code_findings=len(decision.findings),
        status=status,
    )


def _line(outcome: Outcome) -> str:
    sample = outcome.sample
    if outcome.action is None:
        got = f"no decision ({outcome.status or 'not received'})"
    else:
        got = ACTIONS[outcome.action][0]
        reasons = [reason(code).title for code in outcome.reason_codes]
        if reasons:
            got += ": " + ", ".join(reasons)
    if outcome.as_expected:
        mark = "ok "
    else:
        wanted = " or ".join(ACTIONS[action][0] for action in sample.expect)
        if sample.expect_reason:
            wanted += f" ({reason(sample.expect_reason).title})"
        if sample.jev_only:
            wanted += ", with every code check passing"
        got += f"\n{'':<37}expected: {wanted}"
        mark = "!! "
    return f"{mark}{got}"


async def main() -> int:
    from jev_invoice.main import build_extraction, build_transcription  # the app's wiring

    settings = get_settings()
    configure_logging("WARNING", json=False)
    logging.disable(logging.INFO)  # HTTP client chatter; warnings still show
    if make_url(settings.database_url).get_backend_name() != "sqlite":
        print("The demo reset only runs on the local SQLite database.")
        return 1
    extraction, groq_sdk = build_extraction(settings)
    jev = JevClient.from_settings(settings)
    if extraction is None or jev is None:
        print("The demo needs GROQ_API_KEY and TYPESAFE_API_KEY in .env.")
        return 1
    transcription, ollama_http = build_transcription(settings)

    engine = create_engine(settings)
    try:
        backup = await reset_database(engine, settings)
        print(f"Database emptied and master data reloaded (backup: {backup}).")
        files = write_pack(pack_directory(settings))
        print(f"Processing the sample pack (scans read locally by {settings.vision_model}):")
        outcomes = await load_pack(
            create_session_factory(engine),
            settings,
            files,
            extraction=extraction,
            transcription=transcription,
            decisions=DecisionService(jev, settings),
        )
    finally:
        await jev.aclose()
        if groq_sdk is not None:
            await groq_sdk.close()
        if ollama_http is not None:
            await ollama_http.aclose()
        await engine.dispose()

    missed = [o for o in outcomes if not o.as_expected]
    live = [sample.filename for sample, _ in files if not sample.expect]
    print()
    if missed:
        print(
            f"{len(missed)} of {len(outcomes)} samples did not land as written (marked !!)."
            " The models are live; run it again, or check those invoices' journeys."
        )
    else:
        print(f"All {len(outcomes)} samples landed as expected.")
    print(
        f"Every file is in {pack_directory(settings)}; {', '.join(live)} is kept back"
        " to upload live. Open http://localhost:8000"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
