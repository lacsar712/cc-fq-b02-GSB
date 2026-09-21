"""API + runner tests for job termination (cancellation)."""

import pytest
from sqlalchemy import update

from app.models import Job, JobStage
from app.pipeline import runner as runner_module
from app.pipeline.actors import QualityHistActor
from app.pipeline.runner import (
    CANCEL_MESSAGE,
    JobCancelled,
    _run_chain_cancellable,
    run_pipeline_sync,
)

from conftest import BROKEN_FASTQ, GOOD_FASTQ, auth_headers


BIOPS = auth_headers("bioops")
AUDITOR = auth_headers("auditor")


def _create_pending_job(session_local, fastq_text=GOOD_FASTQ, created_by="bioops"):
    db = session_local()
    try:
        job = Job(status="pending", created_by=created_by, fastq_snapshot=fastq_text)
        db.add(job)
        db.commit()
        db.refresh(job)
        runner_module.create_job_stages(db, job.id)
        return job.id
    finally:
        db.close()


def _set_status(session_local, job_id, status):
    db = session_local()
    try:
        db.execute(update(Job).where(Job.id == job_id).values(status=status))
        db.commit()
    finally:
        db.close()


def _get_job(session_local, job_id):
    db = session_local()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        return job.status, job.finished_at
    finally:
        db.close()


def _get_stages(session_local, job_id):
    db = session_local()
    try:
        return [
            (s.actor_name, s.status, s.message)
            for s in db.query(JobStage)
            .filter(JobStage.job_id == job_id)
            .order_by(JobStage.stage_order)
            .all()
        ]
    finally:
        db.close()


# ---------- permission & validation ----------

def test_cancel_requires_auth(client, db_session):
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)
    resp = client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 401


def test_auditor_cannot_cancel(client, db_session):
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=AUDITOR)
    assert resp.status_code == 403
    # Status untouched: direct call is rejected, no button hides it in the UI.
    assert _get_job(session_local, job_id)[0] == "pending"


def test_cancel_missing_job_404(client, db_session):
    resp = client.post("/api/jobs/999/cancel", headers=BIOPS)
    assert resp.status_code == 404


@pytest.mark.parametrize("terminal_status", ["success", "failed"])
def test_terminal_jobs_cannot_be_cancelled(client, db_session, terminal_status):
    _db, session_local = db_session
    job_id = _create_pending_job(
        session_local,
        GOOD_FASTQ if terminal_status == "success" else BROKEN_FASTQ,
    )
    _set_status(session_local, job_id, terminal_status)
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS)
    assert resp.status_code == 409
    assert _get_job(session_local, job_id)[0] == terminal_status


def test_cancelled_job_cannot_be_cancelled_again(client, db_session):
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS).status_code == 200
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS)
    assert resp.status_code == 409


# ---------- queued cancellation ----------

def test_cancel_pending_job(client, db_session):
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)

    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert {s["status"] for s in body["stages"]} <= {
        "cancelled",
        "skipped",
    }

    stages = _get_stages(session_local, job_id)
    statuses = [s[1] for s in stages]
    assert statuses[0] == "cancelled"
    assert statuses[1:] == ["skipped", "skipped", "skipped"]
    assert all(m == CANCEL_MESSAGE for _, st, m in stages if st == "cancelled")


def test_cancel_then_runner_starts_job_stays_cancelled(client, db_session):
    """Self-test: cancel immediately after submission, a late runner must not
    turn the job into success."""
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)

    assert client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS).status_code == 200

    # Simulate the background worker picking the job up afterwards.
    db = session_local()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        result = run_pipeline_sync(db, job)
    finally:
        db.close()
    assert result is None  # claim failed: not pending anymore
    assert _get_job(session_local, job_id)[0] == "cancelled"


# ---------- running cancellation ----------

def test_cancel_running_job(client, db_session):
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)
    _set_status(session_local, job_id, "running")

    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    statuses = [s[1] for s in _get_stages(session_local, job_id)]
    assert statuses == ["skipped", "skipped", "skipped", "skipped"] or statuses[0] == "cancelled"
    assert _get_job(session_local, job_id)[0] == "cancelled"


@pytest.mark.asyncio
async def test_chain_respects_cancellation_between_stages():
    calls = {"n": 0}

    async def is_cancelled():
        calls["n"] += 1
        return calls["n"] >= 2  # cancel right after ParseActor, before QualityHist

    with pytest.raises(JobCancelled) as exc:
        await _run_chain_cancellable(GOOD_FASTQ, is_cancelled)

    statuses = exc.value.stage_status
    assert statuses["ParseActor"]["status"] == "success"
    assert statuses["QualityHistActor"]["status"] == "cancelled"
    assert statuses["NContentActor"]["status"] == "skipped"
    assert statuses["ReportActor"]["status"] == "skipped"


def test_runner_mid_chain_cancel_keeps_job_cancelled(db_session, monkeypatch):
    """Full runner path: cancellation lands while stage 1 is finishing; the
    runner must stop later stages and never overwrite job status."""
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)

    real_run = QualityHistActor.run

    def cancel_during_stage(self_actor, in_q, out_q):
        cancel_db = session_local()
        try:
            cancel_db.execute(
                update(Job).where(Job.id == job_id).values(status="cancelled")
            )
            cancel_db.commit()
        finally:
            cancel_db.close()
        return real_run(self_actor, in_q, out_q)

    monkeypatch.setattr(QualityHistActor, "run", cancel_during_stage)

    db = session_local()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        run_pipeline_sync(db, job)
        db.refresh(job)
        assert job.status == "cancelled"
        stages = [
            (s.actor_name, s.status)
            for s in db.query(JobStage)
            .filter(JobStage.job_id == job_id)
            .order_by(JobStage.stage_order)
            .all()
        ]
    finally:
        db.close()

    assert stages[0] == ("ParseActor", "success")
    assert stages[1] == ("QualityHistActor", "success")
    assert stages[2] == ("NContentActor", "cancelled")
    assert stages[3] == ("ReportActor", "skipped")
    assert _get_job(session_local, job_id)[0] == "cancelled"


# ---------- regression: normal lifecycle through the API ----------

def test_good_job_completes_and_is_listed(client, db_session):
    resp = client.post("/api/jobs", headers=BIOPS, json={"fastqText": GOOD_FASTQ})
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    detail = client.get(f"/api/jobs/{job_id}", headers=BIOPS).json()
    assert detail["status"] == "success"
    assert detail["metrics"]["reads"] == 2
    assert detail["finished_at"] is not None
    assert all(s["status"] == "success" for s in detail["stages"])

    # Terminal success cannot be cancelled.
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS).status_code == 409


def test_broken_job_fails_and_cannot_be_cancelled(client, db_session):
    resp = client.post("/api/jobs", headers=BIOPS, json={"fastqText": BROKEN_FASTQ})
    job_id = resp.json()["id"]

    detail = client.get(f"/api/jobs/{job_id}", headers=BIOPS).json()
    assert detail["status"] == "failed"
    stages = {s["actor_name"]: s["status"] for s in detail["stages"]}
    assert stages["ParseActor"] == "failed"
    assert stages["ReportActor"] == "skipped"

    assert client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS).status_code == 409


def test_history_reflects_cancelled_status(client, db_session):
    _db, session_local = db_session
    job_id = _create_pending_job(session_local)
    client.post(f"/api/jobs/{job_id}/cancel", headers=BIOPS)

    rows = client.get("/api/jobs", headers=BIOPS).json()
    row = next(r for r in rows if r["id"] == job_id)
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
