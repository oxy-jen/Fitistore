"""Durable liveness records for explicit, one-shot outbox workers."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from uuid import uuid4

from flask import current_app

from app.extensions import db
from app.models import EmailWorkerRun

logger = logging.getLogger(__name__)


class EmailWorkerHeartbeat:
    def __init__(self, *, interval_seconds: int | None = None, instance_id: str | None = None,
                 worker_type: str = "queue"):
        self.app = current_app._get_current_object()
        self.instance_id = instance_id or str(uuid4())
        self.worker_type = worker_type
        self.interval_seconds = max(2, int(interval_seconds or self.app.config.get("EMAIL_WORKER_HEARTBEAT_SECONDS", 15)))
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        now = datetime.utcnow()
        db.session.add(EmailWorkerRun(
            instance_id=self.instance_id, worker_type=self.worker_type, status="STARTING",
            started_at=now, last_heartbeat_at=now,
        ))
        db.session.commit()
        EmailWorkerRun.query.filter_by(instance_id=self.instance_id, status="STARTING").update({
            EmailWorkerRun.status: "RUNNING", EmailWorkerRun.last_heartbeat_at: datetime.utcnow(),
        }, synchronize_session=False)
        db.session.commit()
        self._thread = threading.Thread(target=self._pulse, name="fity-email-heartbeat", daemon=True)
        try:
            self._thread.start()
        except Exception:
            db.session.rollback()
            EmailWorkerRun.query.filter_by(instance_id=self.instance_id).update({
                EmailWorkerRun.status: "FAILED", EmailWorkerRun.stopped_at: datetime.utcnow(),
                EmailWorkerRun.last_error_category: "heartbeat_thread_start_failed",
            }, synchronize_session=False)
            db.session.commit()
            raise

    def _pulse(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self.app.app_context():
                    EmailWorkerRun.query.filter_by(instance_id=self.instance_id, status="RUNNING").update(
                        {EmailWorkerRun.last_heartbeat_at: datetime.utcnow()}, synchronize_session=False,
                    )
                    db.session.commit()
            except Exception as exc:
                with self.app.app_context():
                    db.session.rollback()
                logger.error("email_worker_heartbeat_failed instance_id=%s error_type=%s", self.instance_id, type(exc).__name__)
            finally:
                with self.app.app_context():
                    db.session.remove()

    def mark_attempt(self, count: int) -> None:
        now = datetime.utcnow()
        EmailWorkerRun.query.filter_by(instance_id=self.instance_id, status="RUNNING").update({
            EmailWorkerRun.last_attempt_at: now,
            EmailWorkerRun.current_batch_count: int(count),
            EmailWorkerRun.last_heartbeat_at: now,
        }, synchronize_session=False)
        db.session.commit()

    def mark_success(self) -> None:
        now = datetime.utcnow()
        EmailWorkerRun.query.filter_by(instance_id=self.instance_id, status="RUNNING").update({
            EmailWorkerRun.last_success_at: now, EmailWorkerRun.last_heartbeat_at: now,
        }, synchronize_session=False)
        db.session.commit()

    def stop(self, *, failed: bool = False, error_category: str | None = None) -> None:
        try:
            if not failed:
                EmailWorkerRun.query.filter_by(instance_id=self.instance_id, status="RUNNING").update({
                    EmailWorkerRun.status: "STOPPING", EmailWorkerRun.last_heartbeat_at: datetime.utcnow(),
                }, synchronize_session=False)
                db.session.commit()
        except Exception:
            db.session.rollback()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=min(2, self.interval_seconds))
        try:
            now = datetime.utcnow()
            EmailWorkerRun.query.filter_by(instance_id=self.instance_id).update({
                EmailWorkerRun.status: "FAILED" if failed else "STOPPED",
                EmailWorkerRun.stopped_at: now,
                EmailWorkerRun.last_heartbeat_at: now,
                EmailWorkerRun.last_error_category: error_category,
                EmailWorkerRun.current_batch_count: 0,
            }, synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.error("email_worker_shutdown_record_failed instance_id=%s", self.instance_id)


def worker_health(*, timeout_seconds: int = 90, limit: int = 10, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    timeout_seconds = max(5, min(int(timeout_seconds), 3600))
    runs = EmailWorkerRun.query.order_by(EmailWorkerRun.started_at.desc()).limit(max(1, min(int(limit), 50))).all()
    results = []
    for run in runs:
        stale = run.status in {"STARTING", "RUNNING", "STOPPING"} and run.last_heartbeat_at < now - timedelta(seconds=timeout_seconds)
        status = "STALE" if stale else run.status
        results.append({
            "instance_id": run.instance_id,
            "worker_type": run.worker_type,
            "status": status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "last_heartbeat_at": run.last_heartbeat_at.isoformat() if run.last_heartbeat_at else None,
            "last_success_at": run.last_success_at.isoformat() if run.last_success_at else None,
            "last_attempt_at": run.last_attempt_at.isoformat() if run.last_attempt_at else None,
            "last_error_category": run.last_error_category,
            "current_batch_count": run.current_batch_count,
        })
    live = [row for row in results if row["status"] in {"STARTING", "RUNNING", "STOPPING"}]
    return {"heartbeat_timeout_seconds": timeout_seconds, "workers": results,
            "live_count": len(live), "stale_count": sum(row["status"] == "STALE" for row in results)}
