"""Tests for job termination (cancel): API rules + runner race invariants.

Uses a per-test file-based SQLite database so the FastAPI TestClient and the
pipeline runner share a real database without PostgreSQL.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import _run_job_background
from app.database import Base, get_db
from app.main import app
from app.models import Job, JobStage
from app.pipeline import runner
from app.pipeline.runner import _run_chain, create_job_stages, run_pipeline_sync


GOOD_FASTQ = """@SEQ1
ACGTACGT
+
IIIIHHHH
@SEQ2
NNNNACGT
+
IIIIIIII
"""

BROKEN_FASTQ = """@SEQ1
ACGT
NOTPLUS
IIII
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.engine", engine)  # lifespan create_all
    monkeypatch.setattr("app.api.SessionLocal", TestingSessionLocal)  # background task
    monkeypatch.setattr("app.config.settings.stage_delay_seconds", 0)  # fast tests
    with TestClient(app) as client:
        yield client, TestingSessionLocal
    app.dependency_overrides.pop(get_db, None)


def _token(client, username, password):
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _make_job(SessionLocal, fastq=GOOD_FASTQ, status="pending"):
    """Insert a job with its four pending stages, bypassing the API."""
    db = SessionLocal()
    try:
        job = Job(
            sample_name="t",
            status=status,
            created_by="bioops",
            fastq_snapshot=fastq,
        )
        db.add(job)
        db.commit()
        create_job_stages(db, job.id)
        return job.id
    finally:
        db.close()


def test_cancel_pending_job(env):
    client, SessionLocal = env
    token = _token(client, "bioops", "fastq123456")
    job_id = _make_job(SessionLocal)

    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert all(s["status"] == "skipped" for s in body["stages"])

    # 详情与历史立即反映
    detail = client.get(f"/api/jobs/{job_id}", headers=_auth(token)).json()
    assert detail["status"] == "cancelled"
    history = client.get("/api/jobs", headers=_auth(token)).json()
    assert history[0]["status"] == "cancelled"


def test_cancel_twice_conflict(env):
    client, SessionLocal = env
    token = _token(client, "bioops", "fastq123456")
    job_id = _make_job(SessionLocal)

    assert client.post(f"/api/jobs/{job_id}/cancel", headers=_auth(token)).status_code == 200
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=_auth(token))
    assert resp.status_code == 409


def test_cancel_success_or_failed_conflict(env):
    client, _ = env
    token = _token(client, "bioops", "fastq123456")

    # TestClient waits for background tasks: pipeline finishes before return.
    resp = client.post("/api/jobs", json={"fastqText": GOOD_FASTQ}, headers=_auth(token))
    assert resp.status_code == 201
    ok_id = resp.json()["id"]
    assert client.get(f"/api/jobs/{ok_id}", headers=_auth(token)).json()["status"] == "success"
    resp = client.post(f"/api/jobs/{ok_id}/cancel", headers=_auth(token))
    assert resp.status_code == 409

    resp = client.post("/api/jobs", json={"fastqText": BROKEN_FASTQ}, headers=_auth(token))
    assert resp.status_code == 201
    bad_id = resp.json()["id"]
    assert client.get(f"/api/jobs/{bad_id}", headers=_auth(token)).json()["status"] == "failed"
    resp = client.post(f"/api/jobs/{bad_id}/cancel", headers=_auth(token))
    assert resp.status_code == 409


def test_auditor_cannot_cancel(env):
    client, SessionLocal = env
    job_id = _make_job(SessionLocal)
    token = _token(client, "auditor", "audit123456")
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=_auth(token))
    assert resp.status_code == 403


def test_cancel_requires_login(env):
    client, SessionLocal = env
    job_id = _make_job(SessionLocal)
    resp = client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 401


def test_cancel_missing_job_404(env):
    client, _ = env
    token = _token(client, "bioops", "fastq123456")
    resp = client.post("/api/jobs/9999/cancel", headers=_auth(token))
    assert resp.status_code == 404


def test_cancelled_job_never_turns_success(env):
    """自测场景：提交后马上终止，随后才启动的后台流水线不得把它变成成功。"""
    client, SessionLocal = env
    token = _token(client, "bioops", "fastq123456")
    job_id = _make_job(SessionLocal)

    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=_auth(token))
    assert resp.status_code == 200

    # 后台任务姗姗来迟（模拟“提交后立刻终止”的竞态）
    _run_job_background(job_id)

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        assert job.status == "cancelled"
        assert job.metrics is None
        stages = db.query(JobStage).filter(JobStage.job_id == job_id).all()
        assert all(s.status == "skipped" for s in stages)
    finally:
        db.close()


def test_cancel_during_run_keeps_cancelled(env, monkeypatch):
    """流水线执行中被终止：即使链全部跑完，终态也不得覆盖为成功。"""
    client, SessionLocal = env
    job_id = _make_job(SessionLocal)
    db = SessionLocal()
    job = db.get(Job, job_id)

    real_run_chain = runner._run_chain

    async def fake_run_chain(fastq_text, should_cancel=None, stage_delay=0.0):
        # 模拟取消端点在流水线运行期间提交（另一事务的等效写入）
        (
            db.query(Job)
            .filter(Job.id == job_id, Job.status.in_(["pending", "running"]))
            .update({"status": "cancelled"}, synchronize_session=False)
        )
        (
            db.query(JobStage)
            .filter(JobStage.job_id == job_id, JobStage.status == "pending")
            .update(
                {"status": "skipped", "message": "因作业终止而跳过"},
                synchronize_session=False,
            )
        )
        db.commit()
        # 链本身完整跑完且成功 —— 若 runner 不做条件更新就会错误地置为 success
        return await real_run_chain(fastq_text)

    monkeypatch.setattr(runner, "_run_chain", fake_run_chain)
    run_pipeline_sync(db, job)
    db.close()

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        assert job.status == "cancelled"
        assert job.metrics is None
        stages = db.query(JobStage).filter(JobStage.job_id == job_id).all()
        assert all(s.status == "skipped" for s in stages)
    finally:
        db.close()


def test_runner_completes_pending_job(env):
    """未被终止的作业正常走完条件更新路径。"""
    _, SessionLocal = env
    job_id = _make_job(SessionLocal)
    db = SessionLocal()
    try:
        run_pipeline_sync(db, db.get(Job, job_id), stage_delay=0)
    finally:
        db.close()

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        assert job.status == "success"
        assert job.metrics["reads"] == 2
        stages = db.query(JobStage).filter(JobStage.job_id == job_id).all()
        assert all(s.status == "success" for s in stages)
    finally:
        db.close()


async def test_run_chain_stops_on_cancel():
    """链级行为：取消后未执行的 Actor 全部 skipped。"""
    calls = 0

    def should_cancel():
        nonlocal calls
        calls += 1
        return calls > 1  # 第一个 Actor 完成后取消

    ok, ctx, stages = await _run_chain(GOOD_FASTQ, should_cancel=should_cancel)
    assert ctx.cancelled is True
    assert stages["ParseActor"]["status"] == "success"
    assert stages["QualityHistActor"]["status"] == "skipped"
    assert stages["QualityHistActor"]["message"] == "因作业终止而跳过"
    assert stages["NContentActor"]["status"] == "skipped"
    assert stages["ReportActor"]["status"] == "skipped"
