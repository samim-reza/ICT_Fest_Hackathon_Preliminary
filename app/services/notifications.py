"""Side effects that accompany booking lifecycle events.

Each booking change sends a (simulated) notification email and appends an
audit-log entry. Both resources are guarded by locks so their output stays
consistent when many requests are processed at once.
"""
import threading

_notify_lock = threading.Lock()


def _send_email(kind: str, booking) -> None:
    return None


def _write_audit(kind: str, booking) -> None:
    return None


def _notify(kind: str, booking) -> None:
    with _notify_lock:
        _write_audit(kind, booking)
        _send_email(kind, booking)


def notify_created(booking) -> None:
    _notify("created", booking)


def notify_cancelled(booking) -> None:
    _notify("cancelled", booking)
