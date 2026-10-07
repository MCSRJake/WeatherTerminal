"""Official point-alert sets: authoritative success, conservative cached/failure states."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from weather_terminal.config import FreshnessSettings
from weather_terminal.freshness import assess_freshness, format_time
from weather_terminal.models import Component, ComponentState, ErrorKind, as_utc, parse_instant
from weather_terminal.services.http import ServiceFailure
from weather_terminal.text import plain_text

SEVERITY = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}
URGENCY = {"Immediate": 0, "Expected": 1, "Future": 2, "Past": 3, "Unknown": 4}
CERTAINTY = {"Observed", "Likely", "Possible", "Unlikely", "Unknown"}
NON_OPERATIONAL = {"Test", "Exercise", "Draft", "System"}


@dataclass(frozen=True)
class Alert:
    identifier: str
    sent: datetime
    event: str
    headline: str
    severity: str
    urgency: str
    certainty: str
    area: str
    effective: datetime | None
    onset: datetime | None
    expires: datetime | None
    ends: datetime | None
    description: str
    instruction: str
    source_url: str | None
    message_type: str
    references: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def expired(self, now: datetime) -> bool:
        return any(t is not None and t <= as_utc(now) for t in (self.expires, self.ends))


@dataclass(frozen=True)
class AlertSet:
    alerts: tuple[Alert, ...]
    notes: tuple[str, ...] = ()
    # Unknown status records prevent a clean all-clear, even if not safely
    # classifiable as Actual. Known Test/Exercise records do not.
    uncertain_status: bool = False


def source_link(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        p = urlsplit(value)
        if p.scheme == "https" and p.hostname and p.username is None and p.password is None and p.port in (None, 443):
            return value
    except ValueError:
        pass
    return None


def normalize_alerts(body: Any) -> AlertSet:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise ValueError
        # A partial/paginated response is not authoritative and must not clear old alerts.
        pagination = body.get("pagination")
        if pagination is not None and (not isinstance(pagination, dict) or pagination.get("next")):
            raise ValueError
        records = {}
        notes = []
        uncertain = False
        for feature in body["features"]:
            if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                raise ValueError
            p = feature["properties"]
            status = p.get("status")
            if status in NON_OPERATIONAL:
                continue
            if status != "Actual":
                if not isinstance(status, str) or not status:
                    raise ValueError
                notes.append(f"Unknown official status {plain_text(status)!r}; record not classified as operational.")
                uncertain = True
                continue
            identifier = p.get("id") or feature.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError
            identifier = identifier.strip()  # Keep exact official identity for reference matching.
            sent = parse_instant(p["sent"])
            event = p.get("event")
            if not isinstance(event, str) or not event.strip():
                raise ValueError
            def text(field):
                value = p.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError
                return plain_text(value) if value else "Unavailable"
            def instant(field):
                return parse_instant(p[field]) if p.get(field) is not None else None
            severity, urgency, certainty = text("severity"), text("urgency"), text("certainty")
            row_notes = []
            for field, value, known in (("severity", severity, SEVERITY), ("urgency", urgency, URGENCY),
                                        ("certainty", certainty, CERTAINTY)):
                if value not in known:
                    row_notes.append(f"Unknown official {field}: {value}")
            references = p.get("references") or []
            if not isinstance(references, list):
                raise ValueError
            refs = []
            for ref in references:
                if not isinstance(ref, dict):
                    raise ValueError
                for value in (ref.get("identifier"), ref.get("@id")):
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError
                        refs.append(value)
            record = Alert(identifier, sent, plain_text(event), text("headline"), severity, urgency, certainty,
                           text("areaDesc"), instant("effective"), instant("onset"), instant("expires"), instant("ends"),
                           text("description"), text("instruction"),
                           source_link(p.get("@id")) or source_link(feature.get("id")),
                           text("messageType"), tuple(refs), tuple(row_notes))
            old = records.get(identifier)
            if old is None or old.sent < record.sent:
                records[identifier] = record
            elif old.sent == record.sent and old != record:
                raise ValueError  # Conflicting same-version records cannot silently win.
        superseded = set()
        for row in records.values():
            if row.message_type in ("Update", "Cancel"):
                for ref in row.references:
                    target = next((old for old in records.values() if ref in (old.identifier, old.source_url)), None)
                    if target is not None and target.sent <= row.sent and target.identifier != row.identifier:
                        superseded.add(target.identifier)
        active = [row for row in records.values() if row.identifier not in superseded and row.message_type != "Cancel"]
        active.sort(key=lambda a: (SEVERITY.get(a.severity, 5), URGENCY.get(a.urgency, 5), a.sent, a.identifier))
        return AlertSet(tuple(active), tuple(notes), uncertain)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS active-alert response is incomplete or invalid; last alerts retained.") from None


def verified(state: ComponentState[AlertSet] | None, now: datetime, settings: FreshnessSettings) -> bool:
    return bool(state is not None and state.data is not None and state.provenance is not None
                and not state.loaded_from_disk and state.error is None and not state.data.uncertain_status
                and not assess_freshness(Component.ALERTS, state.provenance, now, settings).verification_stale)


def alert_status(state: ComponentState[AlertSet] | None, now: datetime, settings: FreshnessSettings) -> str:
    if not verified(state, now, settings):
        return "NOT VERIFIED"
    count = sum(not alert.expired(now) for alert in state.data.alerts)
    if not count and state.data.alerts:
        return "NOT VERIFIED — retrieved alerts expired; awaiting a new active set"
    return f"{count} active alerts for this point" if count else "No active alerts for this point"


def render_alerts(state: ComponentState[AlertSet], now: datetime, zone: str | None, settings: FreshnessSettings) -> str:
    p = state.provenance
    if state.data is None or p is None:
        lines = ["ALERTS — unavailable | NOT VERIFIED", "No current alert status has been verified.",
                 "Retrieved: never | Last checked: never"]
        if state.in_flight:
            lines.append("Loading")
        if state.error:
            lines.append(f"Refresh failed: {state.error.message}")
        lines.append(f"Last attempted: {format_time(state.last_attempted_at, zone) if state.last_attempted_at else 'never this session'}")
        return plain_text("\n".join(lines))
    current = verified(state, now, settings)
    title = "ALERTS — recently checked" if current else "Previously retrieved alerts — current status unverified"
    lines = [title, f"Retrieved: {format_time(p.retrieved_at, zone)}", f"Last checked: {format_time(p.last_checked_at, zone)}",
             f"Last attempted: {format_time(state.last_attempted_at, zone) if state.last_attempted_at else 'never this session'}"]
    if state.in_flight:
        lines.append("Loading (retaining last successful set)")
    if state.error:
        lines.append(f"Refresh failed: {state.error.message}")
    lines.extend(assess_freshness(Component.ALERTS, p, now, settings).warnings)
    lines.extend(state.data.notes)
    if not state.data.alerts:
        lines.append("No active alerts for this point" if current else "No alerts at last check — NOT a current all-clear.")
    for a in state.data.alerts:
        status = "Expired" if a.expired(now) else ("Returned active by NWS" if current else "Current status unverified")
        lines += [f"{a.event} | {status}", a.headline,
                  f"Severity: {a.severity} | Urgency: {a.urgency} | Certainty: {a.certainty}", f"Affected area: {a.area}",
                  f"Effective: {format_time(a.effective, zone)} | Onset: {format_time(a.onset, zone)}",
                  f"Expires: {format_time(a.expires, zone)} | Ends: {format_time(a.ends, zone)}"]
        lines.extend(a.notes)
    lines.append("Point-based coverage; not a guaranteed emergency-notification system. Full text available with A.")
    return plain_text("\n".join(lines))


def alert_details(a: Alert, now: datetime, zone: str | None) -> str:
    return plain_text("\n".join([
        a.event, a.headline, f"Official identifier: {a.identifier}",
        f"Severity: {a.severity} | Urgency: {a.urgency} | Certainty: {a.certainty}",
        f"Area: {a.area}", f"Sent: {format_time(a.sent, zone)}", f"Effective: {format_time(a.effective, zone)}",
        f"Onset: {format_time(a.onset, zone)}", f"Expires: {format_time(a.expires, zone)}",
        f"Ends: {format_time(a.ends, zone)}", "Expired" if a.expired(now) else "Consult dashboard for verification status.",
        "DESCRIPTION (official text)", a.description, "INSTRUCTIONS (official text)", a.instruction,
        f"Official supplied source/API link: {a.source_url or 'Unavailable'}", *a.notes,
    ]))
