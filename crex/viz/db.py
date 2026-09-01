"""SQLite 기반 관제 대시보드 로컬 데이터베이스.

브라우저 localStorage 의 5MB 용량 한계와 6,000자 프롬프트 절단을 없애고,
폐쇄망 환경에서 Python 표준 라이브러리 `sqlite3` 만으로 실행 기록 및 이벤트를
영속적으로 안전하게 보관한다 (외부 의존성 없음).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


class VizDB:
    """워크스페이스 단위의 SQLite 저장소 (<repo_root>/.crex/viz.db)."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    agent_summary TEXT,
                    metrics_json TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_run_id ON events (run_id);
                CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);

                CREATE TABLE IF NOT EXISTS prefs (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                """
            )

    def save_run(self, run_dict: dict[str, Any], metrics: dict[str, Any] | None = None) -> None:
        """실행 메타데이터를 저장하거나 갱신한다."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, kind, params_json, label, created_at, status, error, agent_summary, metrics_json)
                VALUES (:id, :kind, :params_json, :label, :created_at, :status, :error, :agent_summary, :metrics_json)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    error = excluded.error,
                    agent_summary = excluded.agent_summary,
                    metrics_json = COALESCE(excluded.metrics_json, runs.metrics_json)
                """,
                {
                    "id": run_dict["id"],
                    "kind": run_dict.get("kind", "staged"),
                    "params_json": json.dumps(run_dict.get("params", {}), ensure_ascii=False),
                    "label": run_dict.get("label", ""),
                    "created_at": run_dict.get("created_at", time.time()),
                    "status": run_dict.get("status", "running"),
                    "error": run_dict.get("error"),
                    "agent_summary": run_dict.get("agent_summary"),
                    "metrics_json": json.dumps(metrics, ensure_ascii=False) if metrics is not None else None,
                },
            )

    def save_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        """실행의 이벤트 목록을 일괄 저장한다."""
        if not events:
            return
        rows = [
            (
                run_id,
                e.get("seq", i),
                e.get("type", ""),
                json.dumps(e.get("data", {}), ensure_ascii=False),
                e.get("created_at", time.time()),
            )
            for i, e in enumerate(events)
        ]
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            conn.executemany(
                """
                INSERT INTO events (run_id, seq, type, data_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """과거 실행 목록을 최신순으로 반환한다."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, kind, params_json, label, created_at, status, error, agent_summary, metrics_json
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            results = []
            for row in cur.fetchall():
                params = json.loads(row["params_json"]) if row["params_json"] else {}
                metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else None
                results.append(
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "params": params,
                        "label": row["label"],
                        "created_at": row["created_at"],
                        "status": row["status"],
                        "error": row["error"],
                        "agent_summary": row["agent_summary"],
                        "metrics": metrics,
                    }
                )
            return results

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """특정 실행의 메타데이터를 가져온다."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, kind, params_json, label, created_at, status, error, agent_summary, metrics_json
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "kind": row["kind"],
                "params": json.loads(row["params_json"]) if row["params_json"] else {},
                "label": row["label"],
                "created_at": row["created_at"],
                "status": row["status"],
                "error": row["error"],
                "agent_summary": row["agent_summary"],
                "metrics": json.loads(row["metrics_json"]) if row["metrics_json"] else None,
            }

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        """특정 실행의 전체 이벤트를 순서대로 반환한다."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT seq, type, data_json, created_at
                FROM events
                WHERE run_id = ?
                ORDER BY seq ASC, id ASC
                """,
                (run_id,),
            )
            return [
                {
                    "seq": row["seq"],
                    "type": row["type"],
                    "data": json.loads(row["data_json"]) if row["data_json"] else {},
                    "created_at": row["created_at"],
                }
                for row in cur.fetchall()
            ]

    def delete_run(self, run_id: str) -> bool:
        """실행 기록 1건과 그에 속한 이벤트를 삭제한다."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            return cur.rowcount > 0

    def clear_runs(self) -> None:
        """모든 실행 기록과 이벤트를 삭제한다."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM runs")

    def get_prefs(self) -> dict[str, Any]:
        """저장된 UI 폼 설정들을 반환한다."""
        with self._lock, self._connect() as conn:
            cur = conn.execute("SELECT key, value_json FROM prefs")
            result = {}
            for row in cur.fetchall():
                try:
                    result[row["key"]] = json.loads(row["value_json"])
                except Exception:
                    pass
            return result

    def set_prefs(self, patch: dict[str, Any]) -> None:
        """UI 폼 설정들을 저장하거나 갱신한다."""
        with self._lock, self._connect() as conn:
            for k, v in patch.items():
                conn.execute(
                    """
                    INSERT INTO prefs (key, value_json) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                    """,
                    (k, json.dumps(v, ensure_ascii=False)),
                )


__all__ = ["VizDB"]
