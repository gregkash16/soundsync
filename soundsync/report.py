"""
Progress reporting.

Library code never prints. It calls a Reporter, and the caller decides where
that goes: straight to stdout for the command line, or onto a queue that the
GUI drains from the main thread.
"""

import sys


class Reporter:
    """Base class -- does nothing. Handy as a null object."""

    def log(self, message, level="info"):
        """level: info | good | warn | error | detail"""

    def stage(self, name, detail=""):
        """A new phase of work started."""

    def progress(self, done, total):
        """Position within the current stage. total=0 means indeterminate."""

    def row(self, kind, data):
        """A structured result the GUI can put in a table."""


class ConsoleReporter(Reporter):
    PREFIX = {"info": "  ", "good": "  ", "warn": "  ! ", "error": "  ERROR: ",
              "detail": "      "}

    def __init__(self, verbose=True, stream=None):
        self.verbose = verbose
        self.stream = stream or sys.stdout

    def log(self, message, level="info"):
        if level in ("detail", "info") and not self.verbose:
            return
        print(self.PREFIX.get(level, "  ") + str(message), file=self.stream, flush=True)

    def stage(self, name, detail=""):
        if not self.verbose:
            return
        print(f"\n=== {name} ===" + (f" {detail}" if detail else ""),
              file=self.stream, flush=True)


class QueueReporter(Reporter):
    """
    Pushes everything onto a queue.Queue as ("log"|"stage"|"progress"|"row", ...)
    tuples. The GUI polls it with after() and updates widgets on the main
    thread, which is the only thread tkinter tolerates.
    """

    def __init__(self, queue):
        self.queue = queue

    def log(self, message, level="info"):
        self.queue.put(("log", str(message), level))

    def stage(self, name, detail=""):
        self.queue.put(("stage", name, detail))

    def progress(self, done, total):
        self.queue.put(("progress", done, total))

    def row(self, kind, data):
        self.queue.put(("row", kind, data))
