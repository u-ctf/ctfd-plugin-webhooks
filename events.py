import datetime

from flask import has_request_context, request
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.attributes import get_history

from CTFd.models import Fails, Partials, Ratelimiteds, Solves, Teams, Unlocks, Users, db

from .dispatch import configure_dispatch, dispatch_pending_events

_EVENTS_REGISTERED = False


def register_event_listeners(app):
    global _EVENTS_REGISTERED

    if _EVENTS_REGISTERED:
        return

    configure_dispatch(app, db)

    event.listen(Solves, "after_insert", _enqueue_solve_event)
    event.listen(Fails, "after_insert", _enqueue_fail_event)
    event.listen(Partials, "after_insert", _enqueue_partial_event)
    event.listen(Ratelimiteds, "after_insert", _enqueue_ratelimited_event)
    event.listen(Users, "after_insert", _enqueue_registration_event)
    event.listen(Users, "after_update", _enqueue_user_joined_team_event)
    event.listen(Teams, "after_insert", _enqueue_team_created_event)
    event.listen(Unlocks, "after_insert", _enqueue_hint_event)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)

    _EVENTS_REGISTERED = True


def _enqueue_solve_event(_mapper, _connection, target):
    _push_pending_event(target, event_type="challenge_solved", record_id=target.id)


def _enqueue_fail_event(_mapper, _connection, target):
    _push_pending_event(target, event_type="failed_flag", record_id=target.id)


def _enqueue_partial_event(_mapper, _connection, target):
    _push_pending_event(target, event_type="challenge_partial", record_id=target.id)


def _enqueue_ratelimited_event(_mapper, _connection, target):
    _push_pending_event(target, event_type="rate_limited", record_id=target.id)


def _enqueue_registration_event(_mapper, _connection, target):
    if not _is_public_registration_request():
        return
    _push_pending_event(target, event_type="new_registration", record_id=target.id)


def _enqueue_user_joined_team_event(_mapper, _connection, target):
    team_id_history = get_history(target, "team_id")
    if not team_id_history.has_changes() or target.team_id is None:
        return
    _push_pending_event(
        target,
        event_type="user_joined_team",
        record_id=target.id,
        occurred_at=datetime.datetime.utcnow(),
    )


def _enqueue_team_created_event(_mapper, _connection, target):
    _push_pending_event(target, event_type="team_created", record_id=target.id)


def _enqueue_hint_event(_mapper, _connection, target):
    if target.type != "hints":
        return
    _push_pending_event(target, event_type="hint_used", record_id=target.id)


def _after_commit(session):
    pending_events = session.info.pop("ctfd_plugin_webhooks_pending_events", [])
    if pending_events:
        dispatch_pending_events(pending_events)


def _after_rollback(session):
    session.info.pop("ctfd_plugin_webhooks_pending_events", None)


def _push_pending_event(target, event_type, record_id, occurred_at=None):
    session = object_session(target)
    if session is None:
        return

    pending_events = session.info.setdefault(
        "ctfd_plugin_webhooks_pending_events", []
    )
    pending_events.append(
        {
            "event_type": event_type,
            "record_id": record_id,
            "occurred_at": occurred_at,
            "base_url": _current_base_url(),
        }
    )


def _is_public_registration_request():
    return has_request_context() and request.endpoint == "auth.register"


def _current_base_url():
    if not has_request_context():
        return None

    base_url = request.host_url.rstrip("/")
    script_root = request.script_root.rstrip("/")
    if script_root:
        base_url = f"{base_url}{script_root}"
    return base_url