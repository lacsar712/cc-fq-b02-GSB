"""Orchestrate Actor chain with asyncio queues and persist stage status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
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

# Statuses that can still be terminated by the cancel endpoint.
CANCELLABLE_STATUSES = ("pending", "running")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _run_chain(
    fastq_text: str, should_cancel=None, stage_delay: float = 0.0
) -> tuple[bool, PipelineContext, dict[str, dict]]:
    """
    Run Parse → QualityHist → NContent → Report via asyncio queues.
    Returns (success, context, stage_status keyed by actor name).

    If ``should_cancel`` is provided, it is consulted between actors; when it
    returns True the chain stops, remaining actors are marked skipped and
    ``context.cancelled`` is set. ``stage_delay`` simulates per-stage compute
    time so the running state is observable.
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
        if should_cancel is not None and should_cancel():
            ctx.cancelled = True
            for later in actors[i:]:
                stage_status[later.name]["status"] = "skipped"
                stage_status[later.name]["message"] = "因作业终止而跳过"
            break
        if stage_delay > 0:
            await asyncio.sleep(stage_delay)
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


def run_pipeline_sync(db: Session, job: Job, stage_delay: float | None = None) -> Job:
    """Execute pipeline for a job and update DB stages/metrics.

    Cancellation-safe: the job is claimed atomically (pending → running) and
    every later write is conditional, so a job cancelled at any point keeps
    its ``cancelled`` status and never flips to success/failed.
    """
    if stage_delay is None:
        stage_delay = settings.stage_delay_seconds
    # Atomic claim: only a still-pending job may start. If it was cancelled
    # while queued, this matches nothing and we must not run at all.
    claimed = (
        db.query(Job)
        .filter(Job.id == job.id, Job.status == "pending")
        .update({"status": "running"}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        db.refresh(job)
        return job

    stages = (
        db.query(JobStage)
        .filter(JobStage.job_id == job.id)
        .order_by(JobStage.stage_order)
        .all()
    )
    stage_by_name = {s.actor_name: s for s in stages}

    def _is_cancelled() -> bool:
        # Fresh read per call: sees the cancel endpoint's committed update.
        return db.query(Job.status).filter(Job.id == job.id).scalar() == "cancelled"

    success, ctx, stage_status = asyncio.run(
        _run_chain(job.fastq_snapshot, should_cancel=_is_cancelled, stage_delay=stage_delay)
    )

    now = _utcnow()
    for name, info in stage_status.items():
        st = stage_by_name[name]
        values: dict = {"status": info["status"], "message": info["message"]}
        if info["status"] in ("running", "success", "failed"):
            values["started_at"] = st.started_at or now
        if info["status"] in ("success", "failed", "skipped"):
            values["finished_at"] = now
            if info["status"] == "skipped" and values.get("started_at") is None:
                values["started_at"] = now
        # Only write stages that are still pending: a stage already marked
        # skipped by the cancel endpoint must stay skipped.
        (
            db.query(JobStage)
            .filter(JobStage.id == st.id, JobStage.status == "pending")
            .update(values, synchronize_session=False)
        )

    if ctx.cancelled:
        # The cancel endpoint already owns the job row (status='cancelled');
        # just persist the stage writes above and leave the job untouched.
        db.commit()
        db.refresh(job)
        return job

    final_values: dict = {
        "metrics": ctx.metrics or None,
        "finished_at": now,
    }
    if success:
        final_values["status"] = "success"
        final_values["error_message"] = None
    else:
        final_values["status"] = "failed"
        final_values["error_message"] = ctx.error or "流水线失败"
    # Only a still-running job may be finalized; a concurrent cancel wins and
    # the job keeps its cancelled status instead of flipping to success/failed.
    (
        db.query(Job)
        .filter(Job.id == job.id, Job.status == "running")
        .update(final_values, synchronize_session=False)
    )
    db.commit()
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
