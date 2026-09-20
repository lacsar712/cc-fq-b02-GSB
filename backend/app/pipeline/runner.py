"""Orchestrate Actor chain with asyncio queues and persist stage status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _run_chain(fastq_text: str) -> tuple[bool, PipelineContext, dict[str, dict]]:
    """
    Run Parse → QualityHist → NContent → Report via asyncio queues.
    Returns (success, context, stage_status keyed by actor name).
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


def run_pipeline_sync(db: Session, job: Job) -> Job:
    """Execute pipeline for a job and update DB stages/metrics."""
    stages = (
        db.query(JobStage)
        .filter(JobStage.job_id == job.id)
        .order_by(JobStage.stage_order)
        .all()
    )
    stage_by_name = {s.actor_name: s for s in stages}

    job.status = "running"
    db.commit()

    success, ctx, stage_status = asyncio.run(_run_chain(job.fastq_snapshot))

    for name, info in stage_status.items():
        st = stage_by_name[name]
        st.status = info["status"]
        st.message = info["message"]
        if info["status"] in ("running", "success", "failed"):
            st.started_at = st.started_at or _utcnow()
        if info["status"] in ("success", "failed", "skipped"):
            st.finished_at = _utcnow()
            if info["status"] == "skipped" and st.started_at is None:
                st.started_at = st.finished_at

    if success:
        job.status = "success"
        job.metrics = ctx.metrics
        job.error_message = None
    else:
        job.status = "failed"
        job.metrics = ctx.metrics or None
        job.error_message = ctx.error or "流水线失败"
    job.finished_at = _utcnow()
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
