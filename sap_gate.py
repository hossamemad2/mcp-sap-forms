"""
Per-call user approval for EVERY request sent to SAP -- reads included.

Every HTTP call in adt_client.py / soap_rfc_client.py goes through
ApprovalGate.check() immediately before it is sent. check() asks the human
directly (via MCP elicitation, see server.py), not the calling agent, so
the agent cannot approve on the user's behalf. There is deliberately no
"approve all" / session-wide bypass.

Fails closed: if the approver raises, times out, is declined/cancelled, or
the MCP client doesn't support elicitation, the call is NOT made.
Every decision is appended to an audit log (no credentials, no bodies).
"""
from __future__ import annotations

import datetime
import json
import os


class SapCallDenied(Exception):
    pass


class ApprovalGate:
    def __init__(self, approver, system: str = "", audit_path: str = None):
        """approver: sync callable(text) -> bool, blocks until the user answers."""
        self._approver = approver
        self.system = system
        self._audit_path = audit_path

    def check(self, method: str, path: str, purpose: str, kind: str, detail: str = "") -> None:
        text = (
            f"SAP call approval required ({kind})\n"
            f"System : {self.system or '(unknown)'}\n"
            f"Request: {method} {path}\n"
            f"Purpose: {purpose}\n"
            + (f"Details: {detail}\n" if detail else "")
            + "Approve this single call?"
        )
        approved = False
        reason = ""
        try:
            approved = bool(self._approver(text))
            if not approved:
                reason = "declined by user"
        except Exception as e:  # client without elicitation support, timeout, etc.
            reason = f"approval could not be obtained ({type(e).__name__}: {e})"
        self._audit(method, path, purpose, kind, approved, reason)
        if not approved:
            raise SapCallDenied(
                f"SAP call NOT made ({reason}): {method} {path} -- {purpose}. "
                f"Do not retry or work around this; ask the user how to proceed.")

    def _audit(self, method, path, purpose, kind, approved, reason):
        if not self._audit_path:
            return
        entry = {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "system": self.system, "kind": kind, "method": method, "path": path,
            "purpose": purpose, "approved": approved, "reason": reason,
        }
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
