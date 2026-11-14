import datetime

import requests
from flask import has_app_context
from sqlalchemy.orm import joinedload, sessionmaker

from CTFd.models import (
    Challenges,
    Fails,
    Hints,
    Partials,
    Ratelimiteds,
    Solves,
    Teams,
    Unlocks,
    Users,
)
from CTFd.utils import get_config

from .constants import DELIVERY_TIMEOUT_SECONDS, EVENT_FIRST_BLOOD, EVENT_LABELS
from .message_templates import render_event_message
from .models import WebhookEndpoint, WebhookSubscription
from .providers import build_provider_payload

_APP = None
_SESSION_FACTORY = None


def configure_dispatch(app, db):
    global _APP, _SESSION_FACTORY

    _APP = app
    with app.app_context():
        _SESSION_FACTORY = sessionmaker(bind=db.engine, expire_on_commit=False)


def dispatch_pending_events(pending_events):
    if not pending_events or _APP is None or _SESSION_FACTORY is None:
        return

    if has_app_context():
        _dispatch_all(pending_events)
    else:
        with _APP.app_context():
            _dispatch_all(pending_events)


def _dispatch_all(pending_events):
    for event in pending_events:
        try:
            _dispatch_event(event)
        except Exception:
            _APP.logger.exception("ctfd-plugin-webhooks failed to dispatch event")


def _dispatch_event(event):
    session = _SESSION_FACTORY()
    try:
        record = _load_record(session, event)
        if record is None:
            return

        _deliver_to_subscribers(
            session,
            event["event_type"],
            record,
            event.get("base_url"),
            occurred_at=event.get("occurred_at"),
        )

        if event["event_type"] == "challenge_solved" and _is_first_blood(session, record):
            _deliver_to_subscribers(
                session,
                EVENT_FIRST_BLOOD,
                record,
                event.get("base_url"),
            )
    finally:
        session.close()


def build_event_payload(event_type, record, base_url=None, is_private=False, occurred_at=None):
    challenge = getattr(record, "challenge", None)
    user = getattr(record, "user", record if isinstance(record, Users) else None)
    team = getattr(record, "team", record if isinstance(record, Teams) else None)
    hint = getattr(record, "hint", None)

    if team is None and user is not None:
        team = getattr(user, "team", None)

    if occurred_at is not None:
        event_date = occurred_at
    else:
        event_date = getattr(record, "date", None) or getattr(record, "created", None)
    account = _serialize_account(user=user, team=team)

    payload = {
        "event": event_type,
        "event_label": EVENT_LABELS.get(event_type, event_type),
        "occurred_at": _isoformat(event_date),
        "visibility": "private" if is_private else "public",
        "ctfd": {
            "name": get_config("ctf_name"),
            "user_mode": get_config("user_mode"),
        },
        "account": account,
        "user": _serialize_user(user, is_private=is_private),
        "team": _serialize_team(team, is_private=is_private),
        "challenge": _serialize_challenge(challenge),
        "hint": _serialize_hint(hint),
        "submission": _serialize_submission(record),
        "links": _build_links(
            challenge=challenge,
            user=user,
            team=team,
            base_url=base_url,
            is_private=is_private,
        ),
    }
    payload["actor"] = _serialize_actor(user=user, team=team)
    return payload


def _deliver_to_subscribers(session, event_type, record, base_url, occurred_at=None):
    subscribers = (
        session.query(WebhookEndpoint)
        .join(WebhookSubscription)
        .filter(WebhookEndpoint.is_paused.is_(False))
        .filter(WebhookSubscription.event_type == event_type)
        .order_by(WebhookEndpoint.id.asc())
        .all()
    )

    for webhook in subscribers:
        subscription = next(
            (sub for sub in webhook.subscriptions if sub.event_type == event_type),
            None,
        )
        payload = build_event_payload(
            event_type,
            record,
            base_url=base_url,
            is_private=webhook.is_private,
            occurred_at=occurred_at,
        )
        payload["message"] = render_event_message(
            event_type,
            payload,
            template=subscription.template if subscription else None,
        )
        _deliver_webhook(
            webhook,
            payload,
            payload_template=subscription.payload_template if subscription else None,
        )


def _deliver_webhook(webhook, payload, payload_template=None):
    try:
        provider_payload = build_provider_payload(
            webhook.provider,
            payload,
            payload_template=payload_template,
        )
        response = requests.post(
            webhook.target_url,
            json=provider_payload,
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
        if response.ok:
            _mark_delivery_result(webhook.id, success=True, error_message=None)
            return

        error_message = f"HTTP {response.status_code}: {response.text[:200]}".strip()
        _APP.logger.warning(
            "ctfd-plugin-webhooks delivery failed for webhook %s: %s",
            webhook.id,
            error_message,
        )
        _mark_delivery_result(webhook.id, success=False, error_message=error_message)
    except requests.RequestException as exc:
        _APP.logger.warning(
            "ctfd-plugin-webhooks request error for webhook %s: %s",
            webhook.id,
            exc,
        )
        _mark_delivery_result(webhook.id, success=False, error_message=str(exc))
    except ValueError as exc:
        _APP.logger.warning(
            "ctfd-plugin-webhooks payload error for webhook %s: %s",
            webhook.id,
            exc,
        )
        _mark_delivery_result(webhook.id, success=False, error_message=str(exc))


def _mark_delivery_result(webhook_id, success, error_message):
    session = _SESSION_FACTORY()
    try:
        webhook = session.query(WebhookEndpoint).filter_by(id=webhook_id).first()
        if webhook is None:
            return

        now = datetime.datetime.utcnow()
        webhook.last_attempt_at = now
        if success:
            webhook.last_success_at = now
            webhook.last_error = None
        else:
            webhook.last_error = error_message
        session.commit()
    except Exception:
        session.rollback()
        _APP.logger.exception(
            "ctfd-plugin-webhooks failed to persist delivery status for webhook %s",
            webhook_id,
        )
    finally:
        session.close()


# Unlocks rows have no relationships; this exposes the related rows as attributes.
class _HintUnlockEvent:

    def __init__(self, unlock, hint, user, team):
        self.id = unlock.id
        self.date = unlock.date
        self.user = user
        self.team = team
        self.hint = hint
        self.challenge = hint.challenge if hint is not None else None


def _load_record(session, event):
    event_type = event["event_type"]
    record_id = event["record_id"]

    if event_type == "challenge_solved":
        return (
            session.query(Solves)
            .options(
                joinedload(Solves.user),
                joinedload(Solves.team),
                joinedload(Solves.challenge),
            )
            .filter_by(id=record_id)
            .first()
        )
    if event_type == "failed_flag":
        return (
            session.query(Fails)
            .options(
                joinedload(Fails.user),
                joinedload(Fails.team),
                joinedload(Fails.challenge),
            )
            .filter_by(id=record_id)
            .first()
        )
    if event_type == "challenge_partial":
        return (
            session.query(Partials)
            .options(
                joinedload(Partials.user),
                joinedload(Partials.team),
                joinedload(Partials.challenge),
            )
            .filter_by(id=record_id)
            .first()
        )
    if event_type == "rate_limited":
        return (
            session.query(Ratelimiteds)
            .options(
                joinedload(Ratelimiteds.user),
                joinedload(Ratelimiteds.team),
                joinedload(Ratelimiteds.challenge),
            )
            .filter_by(id=record_id)
            .first()
        )
    if event_type == "new_registration":
        return (
            session.query(Users)
            .options(joinedload(Users.team))
            .filter_by(id=record_id)
            .first()
        )
    if event_type == "hint_used":
        return _load_hint_unlock(session, record_id)
    if event_type == "team_created":
        return session.query(Teams).filter_by(id=record_id).first()
    if event_type == "user_joined_team":
        return (
            session.query(Users)
            .options(joinedload(Users.team))
            .filter_by(id=record_id)
            .first()
        )
    return None


def _load_hint_unlock(session, record_id):
    unlock = session.query(Unlocks).filter_by(id=record_id).first()
    if unlock is None:
        return None

    hint = session.query(Hints).filter_by(id=unlock.target).first()
    user = (
        session.query(Users).filter_by(id=unlock.user_id).first()
        if unlock.user_id
        else None
    )
    team = (
        session.query(Teams).filter_by(id=unlock.team_id).first()
        if unlock.team_id
        else None
    )
    return _HintUnlockEvent(unlock, hint, user, team)


def _is_first_blood(session, solve):
    first_solve = (
        session.query(Solves)
        .filter_by(challenge_id=solve.challenge_id)
        .order_by(Solves.date.asc(), Solves.id.asc())
        .first()
    )
    return first_solve is not None and first_solve.id == solve.id


def _serialize_account(user, team):
    user_mode = get_config("user_mode")
    if user_mode == "teams" and team is not None:
        return {"mode": "teams", "id": team.id, "name": team.name}
    if user is not None:
        return {"mode": user_mode or "users", "id": user.id, "name": user.name}
    return None


def _serialize_actor(user, team):
    if team and user:
        return f"{team.name} / {user.name}"
    if team:
        return team.name
    if user:
        return user.name
    return "Someone"


def _serialize_user(user, is_private=False):
    if user is None:
        return None
    data = {
        "id": user.id,
        "name": user.name,
    }
    if is_private:
        data.update(
            {
                "email": user.email,
                "affiliation": user.affiliation,
                "country": user.country,
            }
        )
    return data


def _serialize_team(team, is_private=False):
    if team is None or not isinstance(team, Teams):
        return None
    data = {
        "id": team.id,
        "name": team.name,
    }
    if is_private:
        data.update(
            {
                "email": team.email,
                "affiliation": team.affiliation,
                "country": team.country,
            }
        )
    return data


def _serialize_challenge(challenge):
    if challenge is None or not isinstance(challenge, Challenges):
        return None
    return {
        "id": challenge.id,
        "name": challenge.name,
        "category": challenge.category,
        "type": challenge.type,
        "value": challenge.value,
        "state": challenge.state,
    }


def _serialize_hint(hint):
    if hint is None:
        return None
    data = {
        "id": hint.id,
        "title": hint.title,
        "name": hint.name,
        "challenge_id": hint.challenge_id,
    }
    if hint.cost is not None:
        data["cost"] = hint.cost
    return data


def _serialize_submission(record):
    if isinstance(record, (Users, Teams, Unlocks, _HintUnlockEvent)):
        return None
    data = {
        "id": record.id,
        "status": getattr(record, "type", None),
        "date": _isoformat(getattr(record, "date", None)),
    }
    if isinstance(record, Fails):
        data["provided"] = getattr(record, "provided", None)
    return data


def _build_links(challenge, user, team, base_url, is_private):
    if not is_private or not base_url:
        return None

    links = {
        "plugin_admin": f"{base_url}/admin/plugins/ctfd-plugin-webhooks",
    }
    if challenge is not None:
        links["challenge_admin"] = f"{base_url}/admin/challenges/{challenge.id}"
    if user is not None:
        links["user_admin"] = f"{base_url}/admin/users/{user.id}"
    if team is not None:
        links["team_admin"] = f"{base_url}/admin/teams/{team.id}"

    if "challenge_admin" in links:
        links["primary"] = links["challenge_admin"]
    elif "team_admin" in links:
        links["primary"] = links["team_admin"]
    elif "user_admin" in links:
        links["primary"] = links["user_admin"]
    else:
        links["primary"] = links["plugin_admin"]
    return links


def _isoformat(value):
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"