"""Claude Code session tags (DESIGN §4.4). Claude Code puts the session id in metadata.user_id;
it is recorded in the ledger now and drives the session-aware queue in the second version."""
import re

_SESSION = re.compile(r"session_([0-9a-fA-F-]{8,})")


def session_tag(user_id):
    """The session part of a metadata.user_id; '' when there is none."""
    if not isinstance(user_id, str) or not user_id:
        return ""
    if user_id.lstrip().startswith("{"):
        # Newer Claude Code versions send a JSON object as a string.
        import json
        try:
            obj = json.loads(user_id)
            return str(obj.get("session_id") or obj.get("sessionId") or "")[:64]
        except ValueError:
            return ""
    m = _SESSION.search(user_id)
    return m.group(1)[:64] if m else ""
