from uuid import UUID

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError


class UserNotFoundError(NotFoundError):
    code = "user_not_found"

    def __init__(self, user_id: UUID | None = None) -> None:
        super().__init__("User not found", {"user_id": str(user_id)} if user_id else None)


class EmailAlreadyExistsError(ConflictError):
    code = "email_already_exists"

    def __init__(self) -> None:
        super().__init__("A user with this email already exists")


class SelfModificationError(BadRequestError):
    """An admin tried to lock themselves out (deactivate, demote or delete themselves)."""

    code = "self_modification_forbidden"

    def __init__(self, message: str) -> None:
        super().__init__(message)