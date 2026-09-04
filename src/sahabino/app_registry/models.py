from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sahabino.db.base import Base


# reason of defing constraint name here and not letting DBMS handle that
# is we may wanna do some bussiness stuff with these, working with these name is
# easy in alembic when wanna do some you know tests and may delete some of applications
class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("package_name", name="uq_applications_package_name"),
        CheckConstraint(
            "(is_active IS TRUE AND deactivated_at IS NULL) OR "
            "(is_active IS FALSE AND deactivated_at IS NOT NULL)",
            name="active_deactivation",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(Text)
    package_name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    category_assignments: Mapped[list[ApplicationCategory]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("code", name="uq_categories_code"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    application_assignments: Mapped[list[ApplicationCategory]] = relationship(
        back_populates="category",
        lazy="raise",
    )


class ApplicationCategory(Base):
    __tablename__ = "application_categories"
    __table_args__ = (
        Index(
            "uq_application_primary_category",
            "application_id",
            unique=True,
            postgresql_where=text("is_primary IS TRUE"),
        ),
        Index("ix_application_categories_category_id", "category_id"),
    )

    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("categories.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    application: Mapped[Application] = relationship(
        back_populates="category_assignments",
        lazy="raise",
    )
    category: Mapped[Category] = relationship(
        back_populates="application_assignments",
        lazy="raise",
    )
