from copy import deepcopy
from datetime import timedelta

import pytest

from weather_terminal.alerts import normalize_alerts, render_alerts, alert_details, alert_status, verified
from weather_terminal.config import FreshnessSettings
from weather_terminal.models import Component, ComponentState, ErrorKind, OperationalError, Provenance
from weather_terminal.services.http import ServiceFailure
from tests.phase2_helpers import LOCATION, NOW
from tests.phase4_helpers import ALERTS, alert_feature


def state_for(features, *, cached=False, checked=NOW):
    body = {"features": features}
    data = normalize_alerts(body)
    state = ComponentState(Component.ALERTS, LOCATION.key, 1)
    state.accept_body(data=data, raw_body=body, endpoint=ALERTS, provenance=Provenance(NOW, checked))
    state.loaded_from_disk = cached
    return state


def test_null_geometry_alerts_retained_and_ordered_by_official_fields():
    rows = [alert_feature("moderate"), alert_feature("severe", severity="Severe"),
            alert_feature("extreme", severity="Extreme"), alert_feature("immediate", severity="Severe", urgency="Immediate")]
    data = normalize_alerts({"features": rows})
    assert [a.identifier for a in data.alerts] == ["extreme", "immediate", "severe", "moderate"]
    assert all(a.source_url.startswith("https://api.weather.gov/alerts/") for a in data.alerts)


@pytest.mark.parametrize("status", ["Test", "Exercise", "Draft", "System"])
def test_nonoperational_records_excluded(status):
    state = state_for([alert_feature(status=status)])
    assert not state.data.alerts and verified(state, NOW, FreshnessSettings())
    assert alert_status(state, NOW, FreshnessSettings()) == "No active alerts for this point"


def test_unknown_enums_remain_visible_and_unknown_status_prevents_all_clear():
    row = alert_feature(severity="NewSeverity", urgency="NewUrgency")
    row["properties"]["certainty"] = "NewCertainty"
    state = state_for([row])
    text = render_alerts(state, NOW, None, FreshnessSettings())
    assert "Unknown official severity: NewSeverity" in text and "NewCertainty" in text
    unknown = state_for([alert_feature(status="NewStatus")])
    assert unknown.data.uncertain_status and alert_status(unknown, NOW, FreshnessSettings()) == "NOT VERIFIED"
    assert "No active alerts for this point" not in render_alerts(unknown, NOW, None, FreshnessSettings())


def test_duplicate_identifier_newest_version_wins_and_reference_update_supersedes():
    old = alert_feature("old")
    replacement = deepcopy(old)
    replacement["properties"]["sent"] = (NOW + timedelta(minutes=1)).isoformat()
    replacement["properties"]["headline"] = "Updated headline"
    assert normalize_alerts({"features": [old, replacement]}).alerts[0].headline == "Updated headline"
    new = alert_feature("new", sent=NOW + timedelta(minutes=2), message="Update",
                        references=[{"identifier": "old", "@id": old["id"]}])
    assert [a.identifier for a in normalize_alerts({"features": [old, new]}).alerts] == ["new"]
    cancel = alert_feature("cancel", sent=NOW + timedelta(minutes=3), message="Cancel", references=[{"identifier": "new"}])
    assert not normalize_alerts({"features": [old, new, cancel]}).alerts


def test_authoritative_empty_success_failure_and_cached_empty_are_distinct():
    state = state_for([])
    assert "No active alerts for this point" in render_alerts(state, NOW, None, FreshnessSettings())
    before = state.provenance
    state.fail(OperationalError(ErrorKind.NETWORK, "Synthetic alert outage"))
    text = render_alerts(state, NOW, None, FreshnessSettings())
    assert "current status unverified" in text and "No alerts at last check" in text and "Refresh failed" in text
    assert "No active alerts for this point" not in text and state.provenance is before
    cached = state_for([], cached=True)
    assert not verified(cached, NOW, FreshnessSettings())
    assert "No alerts at last check" in render_alerts(cached, NOW, None, FreshnessSettings())


def test_stale_or_future_success_times_never_verify_all_clear():
    state = state_for([])
    for now in (NOW + timedelta(minutes=4), NOW - timedelta(minutes=1)):
        assert alert_status(state, now, FreshnessSettings()) == "NOT VERIFIED"


def test_expired_entries_not_presented_as_current_warnings_or_all_clear():
    state = state_for([alert_feature(expires=NOW - timedelta(seconds=1))])
    text = render_alerts(state, NOW, None, FreshnessSettings())
    assert "Expired" in text and "Returned active by NWS" not in text
    assert "NOT VERIFIED" in alert_status(state, NOW, FreshnessSettings())
    # An old cached warning is retained but never becomes a verified warning.
    state.loaded_from_disk = True
    assert "Previously retrieved alerts" in render_alerts(state, NOW, None, FreshnessSettings())


def test_full_text_lines_and_safe_supplied_source_link_are_preserved():
    row = alert_feature()
    row["properties"]["description"] += "\n[red]literal[/red]\x1b[2J"
    row["properties"]["@id"] = "javascript:bad"
    row["id"] = "synthetic-not-a-url"
    a = normalize_alerts({"features": [row]}).alerts[0]
    text = alert_details(a, NOW, None)
    assert "Keep all lines." in text and "Do not paraphrase." in text and "[red]" in text
    assert "\x1b" not in text and a.source_url is None and "link: Unavailable" in text


@pytest.mark.parametrize("body", [None, {}, {"features": None}, {"features": [{}]},
    {"features": [], "pagination": {"next": "https://api.weather.gov/alerts?cursor=next"}}])
def test_incomplete_response_cannot_become_empty_success(body):
    with pytest.raises(ServiceFailure):
        normalize_alerts(body)


@pytest.mark.parametrize("field,value", [("id", None), ("sent", "bad"), ("event", None), ("expires", "bad"),
                                        ("instruction", {}), ("references", ["bad"]), ("status", None)])
def test_malformed_actual_alert_is_failure(field, value):
    row = alert_feature()
    row["properties"][field] = value
    if field == "id":
        row["id"] = None
    with pytest.raises(ServiceFailure):
        normalize_alerts({"features": [row]})
