"""
SQLModel-based database agent for the accbase sample queue.

Owns the schema definition and provides a small interface for queueing
samples for downstream processing. Connection details come from
environment variables by default (mirroring the workflow secrets).
"""
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select


class QueuedSample(SQLModel, table=True):
    __tablename__ = "sample_queue"
    __table_args__ = (UniqueConstraint("sample_name", "project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    sample_name: str = Field(max_length=255, index=True)
    project_name: str = Field(max_length=255, index=True)
    pephub_registry: str = Field(max_length=255)
    status: str = Field(default="pending", max_length=50)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AccbaseDBAgent:
    """Wrapper around the accbase Postgres database."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: int = 5432,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: str = "accbase",
        echo: bool = False,
    ):
        host = host or os.environ["ACCBASE_POSTGRES_HOST"]
        user = user or os.environ["ACCBASE_POSTGRES_USER"]
        password = password or os.environ["ACCBASE_POSTGRES_PASSWORD"]

        url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"
        self.engine = create_engine(url, echo=echo)
        SQLModel.metadata.create_all(self.engine)

    @classmethod
    def from_config(cls, config: dict, **overrides) -> "AccbaseDBAgent":
        """Build an agent from a parsed accbase config dict."""
        db = config.get("database", {})
        kwargs = {
            "host": db.get("host"),
            "port": db.get("port", 5432),
            "user": db.get("user"),
            "password": db.get("password"),
            "database": db.get("database", "accbase"),
        }
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    def is_queued(self, sample_name: str, project_name: str) -> bool:
        with Session(self.engine) as session:
            return session.exec(
                select(QueuedSample).where(
                    QueuedSample.sample_name == sample_name,
                    QueuedSample.project_name == project_name,
                )
            ).first() is not None

    def queue_sample(
        self, sample_name: str, project_name: str, pephub_registry: str
    ) -> bool:
        """Insert a sample into the queue. Returns True if newly inserted."""
        with Session(self.engine) as session:
            existing = session.exec(
                select(QueuedSample).where(
                    QueuedSample.sample_name == sample_name,
                    QueuedSample.project_name == project_name,
                )
            ).first()
            if existing:
                return False

            session.add(QueuedSample(
                sample_name=sample_name,
                project_name=project_name,
                pephub_registry=pephub_registry,
            ))
            session.commit()
            return True

    def list_pending(self) -> list[QueuedSample]:
        with Session(self.engine) as session:
            return list(session.exec(
                select(QueuedSample).where(QueuedSample.status == "pending")
            ).all())

    def update_status(
        self, sample_name: str, project_name: str, status: str
    ) -> bool:
        """Update the status of a queued sample. Returns True if a row was updated."""
        with Session(self.engine) as session:
            entry = session.exec(
                select(QueuedSample).where(
                    QueuedSample.sample_name == sample_name,
                    QueuedSample.project_name == project_name,
                )
            ).first()
            if not entry:
                return False
            entry.status = status
            entry.updated_at = datetime.utcnow()
            session.add(entry)
            session.commit()
            return True
