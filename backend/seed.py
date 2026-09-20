"""Seed database with demo users' samples (accounts are in-memory)."""

import os
import time
from pathlib import Path

from sqlalchemy.exc import OperationalError

from app.database import Base, SessionLocal, engine
from app.models import Sample


DATA_DIR = Path(__file__).resolve().parent / "data"


def wait_for_db(retries: int = 30, delay: float = 1.0) -> None:
    for i in range(retries):
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except OperationalError:
            print(f"waiting for db... ({i + 1}/{retries})")
            time.sleep(delay)
    raise RuntimeError("database not ready")


def seed() -> None:
    wait_for_db()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Sample).count() > 0:
            print("samples already seeded, skip")
            return

        good = (DATA_DIR / "good.fastq").read_text(encoding="utf-8")
        broken = (DATA_DIR / "broken.fastq").read_text(encoding="utf-8")

        db.add(
            Sample(
                name="demo-good-r1",
                description="合格小型 FASTQ 样例（含少量 N）",
                is_broken=False,
                fastq_content=good,
            )
        )
        db.add(
            Sample(
                name="demo-broken-malformed",
                description="损坏样例：缺少 + 分隔行 / 长度不一致，ParseActor 应失败",
                is_broken=True,
                fastq_content=broken,
            )
        )
        db.commit()
        print("seeded 2 samples")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
