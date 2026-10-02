from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.common.models import TimeStampMixin, UUIDPrimaryKeyMixin
from app.core.database import Base

class User(UUIDPrimaryKeyMixin, TimeStampMixin, Base):
    __tablename__ = "users"
    # Fetch server-generated values (updated_at) after UPDATE via RETURNING, so reading them
    # never triggers an implicit lazy load (which raises MissingGreenlet in async code).
    __mapper_args__ = {"eager_defaults": True}

    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=expression.true())
    is_superuser: Mapped[bool] = mapped_column(default=False, server_default=expression.false())