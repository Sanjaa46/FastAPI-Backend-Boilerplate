class AppError(Exception):
    """Base for all expected errors. Subclasses set status_code and code."""
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

class NotFoundError(AppError):
    status_code = 404
    code = "not_found"