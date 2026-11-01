"""The one error type. Raised anywhere; the command line prints it as
`ERROR: <message>` and exits 1."""


class WgtError(Exception):
    pass
