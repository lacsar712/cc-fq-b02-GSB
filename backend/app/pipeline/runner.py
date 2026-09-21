"""Orchestrate Actor chain with asyncio queues and persist stage status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Job, JobStage
from app.pipeline.actors import (
    ACTOR_CHAIN,
    NContentActor,
    ParseActor,
    PipelineContext,
    QualityHistActor,
    QueueMessage,
    ReportActor,
)


STAGE_NAMES = [cls.name for cls in ACTOR_CHAIN]
CANCEL_MESSAGE = "作业已被运维终止"
SKIP_MESSAGE = "因作业取消而跳过"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobCancelled(Exception):
    """Raised inside the chain when the job is cancelled between stages."""

    def __init__(self, stage_status: dict[str, dict]):
        self.stage_status = stage_status
        super().__init__("job cancelled")


async def _run_chain(fastq_text: str) -> tuple[bool, PipelineContext, dict[str, dict]]:
    """
    Run Parse → QualityHist → NContent → Report via asyncio queues.
    Returns (success, context, stage_status keyed by actor name).
    """
    return await _run_chain_cancellable(fastq_text, is_cancelled=None)


async def _run_chain_cancellable(
    fastq_text: str, is_cancelled
) -> tuple[bool, PipelineContext, dict[str, dict]]:
    """
    Same as _run_chain, but `is_cancelled()` is awaited before every actor:
    if it returns True, the about-to-run stage is marked cancelled, the rest
    skipped, and JobCancelled (carrying partial stage status) is raised.
    """
    actors = [ParseActor(), QualityHistActor(), NContentActor(), ReportActor()]
    queues: list[asyncio.Queue] = [asyncio.Queue() for _ in range(len(actors) + 1)]
    stage_status: dict[str, dict] = {
        a.name: {"status": "pending", "message": None} for a in actors
    }

    ctx = PipelineContext(fastq_text=fastq_text)
    await queues[0].put(QueueMessage(ok=True, context=ctx))

    final = QueueMessage(ok=False, context=ctx, error="流水线未执行")
    # Queue-driven chain: each actor consumes from queues[i] and produces to queues[i+1]
    for i, actor in enumerate(actors):
        if is_cancelled is not None and await is_cancelled():
            stage_status[actor.name]["status"] = "cancelled"
            stage_status[actor.name]["message"] = CANCEL_MESSAGE
            for later in actors[i + 1 :]:
                stage_status[later.name]["status"] = "skipped"
                stage_status[later.name]["message"] = SKIP_MESSAGE
            raise JobCancelled(stage_status)

        stage_status[actor.name]["status"] = "running"
        await actor.run(queues[i], queues[i + 1])
        result: QueueMessage = await queues[i + 1].get()
        final = result
        if result.ok:
            stage_status[actor.name]["status"] = "success"
            stage_status[actor.name]["message"] = "完成"
            # Forward to next actor's input (same queue slot for the next hop)
            if i + 1 < len(actors):
                await queues[i + 1].put(result)
        else:
            stage_status[actor.name]["status"] = "failed"
            stage_status[actor.name]["message"] = result.error or "失败"
            for later in actors[i + 1 :]:
                stage_status[later.name]["status"] = "skipped"
                stage_status[later.name]["message"] = f"因 {actor.name} 失败而跳过"
            break

    return final.ok, final.context, stage_status


def _claim_job(db: Session, job_id: int) -> Job | None:
    """
    Conditionally move pending → running. Returns the job only if THIS runner
    claimed it; a job cancelled while still queued is never executed.
    """
    result = db.execute(
        update(Job).where(Job.id == job_id, Job.status == "pending").values(status="running")
    )
    db.commit()
    if result.rowcount == 0:
        return None
    return db.query(Job).filter(Job.id == job_id).first()


def _persist_stage_results(db: Session, job_id: int, stage_status: dict[str, dict]) -> None:
    stages = (
        db.query(JobStage)
        .filter(JobStage.job_id == job_id)
        .order_by(JobStage.stage_order)
        .all()
    )
    now = _utcnow()
    for st in stages:
        info = stage_status.get(st.actor_name)
        if not info:
            continue
        st.status = info["status"]
        st.message = info["message"]
        if info["status"] in ("running", "success", "failed", "cancelled"):
            st.started_at = st.started_at or now
        if info["status"] in ("success", "failed", "skipped", "cancelled"):
            st.finished_at = st.finished_at or now
            if info["status"] == "skipped" and st.started_at is None:
                st.started_at = st.finished_at
    db.commit()


def finalize_cancelled_stages(db: Session, job_id: int) -> None:
    """
    Mark not-yet-terminal stages after a cancellation so detail/history views
    reflect it immediately: a running stage becomes cancelled, pending stages
    become skipped (the first pending becomes cancelled when no stage actually
    started, e.g. cancellation while still queued).
    """
    now = _utcnow()
    stages = (
        db.query(JobStage)
        .filter(JobStage.job_id == job_id)
        .order_by(JobStage.stage_order)
        .all()
    )
    for st in stages:
        if st.status == "running":
            st.status = "cancelled"
            st.message = CANCEL_MESSAGE
            st.finished_at = st.finished_at or now

    pending = [st for st in stages if st.status == "pending"]
    for st in pending:
        st.status = "skipped"
        st.message = SKIP_MESSAGE
        st.started_at = st.started_at or now
        st.finished_at = st.finished_at or now
    if pending and not any(st.status == "cancelled" for st in stages):
        first = pending[0]
        first.status = "cancelled"
        first.message = CANCEL_MESSAGE

    db.commit()


def _finalize_job(db: Session, job_id: int, success: bool, ctx: PipelineContext) -> bool:
    """
    Conditionally write the terminal status; returns False if the job was
    cancelled concurrently (its status must stay cancelled).
    """
    terminal = "success" if success else "failed"
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(
            status=terminal,
            metrics=ctx.metrics if (success or ctx.metrics) else None,
            error_message=None if success else (ctx.error or "流水线失败"),
            finished_at=_utcnow(),
        )
    )
    db.commit()
    return result.rowcount > 0


def run_pipeline_sync(db: Session, job: Job) -> Job | None:
    """
    Execute pipeline for a job and update DB stages/metrics.

    Cancellation-safe via conditional UPDATEs:
    - claim: only a pending job starts running (cancelled-while-queued is skipped);
    - finalize: only a still-running job receives success/failed (a cancelled job
      is never overwritten with success).
    Returns the refreshed job, or None if it never claimed the job.
    """

    async def is_cancelled() -> bool:
        # Column query always emits SELECT, so this reads committed status
        # written by the cancel endpoint on another session/transaction.
        current = db.query(Job.status).filter(Job.id == job.id).scalar()
        return current == "cancelled"

    claimed = _claim_job(db, job.id)
    if claimed is None:
        return None
    job = claimed

    try:
        success, ctx, stage_status = asyncio.run(
            _run_chain_cancellable(job.fastq_snapshot, is_cancelled)
        )
    except JobCancelled as exc:
        # Cancellation won: persist partial results (completed stages stay
        # success, current one cancelled, rest skipped); job stays cancelled.
        _persist_stage_results(db, job.id, exc.stage_status)
        db.refresh(job)
        return job

    _persist_stage_results(db, job.id, stage_status)
    _finalize_job(db, job.id, success, ctx)
    db.refresh(job)
    return job


def create_job_stages(db: Session, job_id: int) -> list[JobStage]:
    stages = []
    for order, cls in enumerate(ACTOR_CHAIN):
        st = JobStage(
            job_id=job_id,
            actor_name=cls.name,
            stage_order=order,
            status="pending",
        )
        db.add(st)
        stages.append(st)
    db.commit()
    return stages
