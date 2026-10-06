"""Import every module's models here so Alembic autogenerate can see them."""

from app.modules.auth.models import RefreshToken  # noqa: F401
from app.modules.users.models import User  # noqa: F401
