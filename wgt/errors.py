"""The error type. Raised anywhere; the command line prints it as
`ERROR: <message>` and exits 1."""


class WgtError(Exception):
    pass


class CommandFailed(WgtError):
    """A system command failed (as opposed to, say, the user answering no)."""
