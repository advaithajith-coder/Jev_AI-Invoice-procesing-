"""Prepare runs or import saved responses without invoking any model."""

import argparse
import json

from jev_invoice.config import get_settings
from jev_invoice.evaluation.service import STAGES, EvaluationStore, GeneratedOutput


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "report", "submit"])
    parser.add_argument("--run")
    parser.add_argument("--case")
    parser.add_argument("--response", type=argparse.FileType("r", encoding="utf-8"))
    parser.add_argument("--name", default="100-invoice baseline")
    args = parser.parse_args()
    settings = get_settings()
    db = EvaluationStore(settings.storage_dir / "evaluations.sqlite3")
    if args.action == "prepare":
        dataset = db.import_dataset(settings.evaluation_dataset_dir)
        result = db.prepare(dataset["dataset_id"], args.name, list(STAGES))
        result = db.summary(result["run_id"])
    elif args.action == "report":
        if not args.run:
            parser.error("--run is required")
        result = db.summary(args.run)
    else:
        if not args.run or not args.case or not args.response:
            parser.error("--run, --case and --response are required")
        result = db.submit(
            args.run, args.case, GeneratedOutput.model_validate(json.load(args.response))
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
