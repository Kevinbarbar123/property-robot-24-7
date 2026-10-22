class SourceResponseError(RuntimeError):
    def __init__(self, message, *, kind="schema_changed", url="", status=None, retry_at=0, provider=""):
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.status = status
        self.retry_at = retry_at
        self.provider = provider


class SourceAccessError(SourceResponseError):
    def __init__(self, message, **values):
        values.setdefault("kind", "access_restricted")
        super().__init__(message, **values)
