"""Safe domain errors shared by parsing, transport and orchestration."""


class ImportProblem(Exception):
    """A user-facing explanation without credentials or response bodies."""


class ExcludedActivity(ImportProblem):
    """An intentional scope exclusion, counted separately from invalid input."""
