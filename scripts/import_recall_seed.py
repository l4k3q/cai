#!/usr/bin/env python3
"""Import the security recall seed into the current PostgreSQL question bank."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from cai.question_bank.recall_import import import_recall_topics, load_recall_seed
from cai.question_bank.repository import DEFAULT_DB_URL, QuestionRepository

DEFAULT_SEED = Path(__file__).resolve().parents[1] / "seeds" / "security_recall_topics.json"


async def import_seed(seed_path: Path, db_url: str):
    topics = load_recall_seed(seed_path)
    repo = QuestionRepository(db_url)
    try:
        await repo.create_tables()
        await repo.ensure_recall_metadata_column()
        async with repo.session() as sess:
            async with sess.begin():
                return await import_recall_topics(repo, sess, topics)
    finally:
        await repo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument(
        "--db-url",
        default=os.getenv("CAI_DB_URL", DEFAULT_DB_URL),
        help="SQLAlchemy async PostgreSQL URL (default: CAI_DB_URL)",
    )
    args = parser.parse_args()
    summary = asyncio.run(import_seed(args.seed, args.db_url))
    print(json.dumps(summary.as_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
