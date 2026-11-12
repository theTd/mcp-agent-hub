"""What a DSH job's mailbox replay may skip when the hub re-attaches to the job.

A DSH job's mailbox (worker messages to the parent, worker questions and approval requests) is
read through `/_dsh/dsh-crew/jobs/<id>/messages?after=<sequence>`; the hub keeps the sequence only
in memory. After a hub restart the resumed session re-attaches to the same job and reads the
mailbox from the start, so every earlier message and question would come back: worker messages
reach the parent a second time and questions the parent already answered become pending again.

The hub's own records say what it already handled: each question it registered is a worker event
carrying the message id as `request_id`, each message it passed on a `message_to_parent` event, each
auto-approval notice a stored transcript message. Only the first batch read after re-attaching is
the replay; anything later is new.
"""
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, Optional

# Stored event text may be shortened or re-spaced; compare whitespace-normalized heads.
TEXT_HEAD = 400


def _head(text: Any) -> str:
    return " ".join(str(text or "").split())[:TEXT_HEAD]


@dataclass(frozen=True)
class DshMailboxReplay:
    handled_requests: FrozenSet[str]
    passed_on: FrozenSet[str]

    @classmethod
    def from_history(cls, history: Optional[Dict[str, Any]], still_pending: Iterable[str] = ()) -> "DshMailboxReplay":
        """`history` is HubDatabase.get_mailbox_history; `still_pending` the request ids of the
        interaction that was waiting for an answer when the hub stopped — it must come back."""
        history = history or {}
        pending = {str(item) for item in still_pending if item}
        requests = frozenset(str(item) for item in history.get("requests") or () if item and str(item) not in pending)
        texts = frozenset(head for head in (_head(item) for item in history.get("texts") or ()) if head)
        return cls(requests, texts)

    def already_handled(self, message_id: Any, text: Any, expects_reply: bool) -> bool:
        if expects_reply:
            return str(message_id or "") in self.handled_requests
        head = _head(text)
        return bool(head) and head in self.passed_on
