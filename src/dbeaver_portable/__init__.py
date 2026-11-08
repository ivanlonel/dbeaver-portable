"""Build a no-admin DBeaver Community package for Windows."""


class PortableError(Exception):
    """A build step failed in a way the user can act on (the message says how)."""
