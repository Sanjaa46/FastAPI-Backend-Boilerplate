from app.core.exceptions import BadRequestError, ForbiddenError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    """Wrong email or password. Deliberately identical for both cases (no enumation)."""

    code = "invalid_credentials"

    def __init__(self) -> None:
        super().__init__("Incorrect email or password")


class InactiveUserError(ForbiddenError):
    code = "inactive_user"

    def __init__(self) -> None:
        super().__init__("This account is deactivated")


class RegisterationDisabledError(ForbiddenError):
    code = "registration_disabled"

    def __init__(self) -> None:
        super().__init__("Self-registration is disabled")


class InvalidCurrentPasswordError(BadRequestError):
    """400, nor 401: a 401 would make clients drop a perfectly valid session."""

    code = "invalid_current_password"

    def __init__(self) -> None:
        super().__init__("Current password is incorrect")
