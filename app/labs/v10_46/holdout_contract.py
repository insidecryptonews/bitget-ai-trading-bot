"""Shared holdout exceptions with no data-loading capability."""


class HoldoutAccessDenied(RuntimeError):
    """Fail-closed holdout access or commitment violation."""
