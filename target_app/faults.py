"""Injectable runtime faults.

The brief is explicit that the interesting failures in this environment are not
layout drift -- they are runtime conditions: validation errors, "record not
found", permission denials, unexpected dialogs, session timeout, transient
slowness, outright app errors.

A capability that only works on the happy path is not useful in production, so
the target app must be able to produce each of those on demand. Faults are
selected per-request via the `fault` query parameter (or the FAULT env var as a
default), which keeps the evidence runs reproducible.
"""

from __future__ import annotations

import os
from enum import StrEnum


class Fault(StrEnum):
    NONE = "none"

    # A legitimate business answer, not a crash. The caller needs to hear it.
    MEMBER_NOT_FOUND = "member_not_found"
    ACCOUNT_CLOSED = "account_closed"
    VALIDATION_ERROR = "validation_error"

    # Recoverable: bounded retry / known-interstitial dismissal should clear it.
    SLOW_LOAD = "slow_load"
    UNEXPECTED_CONFIRM_DIALOG = "unexpected_confirm_dialog"
    SESSION_TIMEOUT = "session_timeout"

    # Hard failures / human-only situations.
    PERMISSION_DENIED = "permission_denied"
    APP_ERROR_500 = "app_error_500"


#: Faults the replay engine is expected to surface as a *business outcome*
#: (status=business_outcome, exit 0) rather than an execution failure.
BUSINESS_OUTCOME_FAULTS = {
    Fault.MEMBER_NOT_FOUND,
    Fault.ACCOUNT_CLOSED,
    Fault.VALIDATION_ERROR,
}

#: Faults a bounded recovery rule should be able to clear without a human.
RECOVERABLE_FAULTS = {
    Fault.SLOW_LOAD,
    Fault.UNEXPECTED_CONFIRM_DIALOG,
    Fault.SESSION_TIMEOUT,
}

#: Faults that must stop the run with a clear, debuggable error or escalate.
HARD_FAULTS = {
    Fault.PERMISSION_DENIED,
    Fault.APP_ERROR_500,
}


#: Cookie used to keep a selected fault active for a whole session.
FAULT_COOKIE = "cua_fault"


def active_fault(request_arg: str | None, cookie: str | None = None) -> Fault:
    """Resolve the fault for a request: query param, then cookie, then env.

    The cookie matters: a flow navigates several pages, and a fault chosen with
    `?fault=session_timeout` on the entry point has to still be in force when the
    search form does its own GET three requests later. Without stickiness only
    the first request of a run could ever be faulted, which is not how a real
    runtime condition behaves.
    """
    raw = request_arg or cookie or os.environ.get("FAULT") or Fault.NONE
    try:
        return Fault(raw)
    except ValueError:
        return Fault.NONE
