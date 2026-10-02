class RepositoryError(Exception):
    """Expected persistence or lookup failure suitable for a CLI response."""


class DatabaseNotInitialized(RepositoryError):
    pass


class UnsafeDatabase(RepositoryError):
    pass


class DatasetConflict(RepositoryError):
    pass


class RecordNotFound(RepositoryError):
    pass


class OrderAccessDenied(RepositoryError):
    pass
