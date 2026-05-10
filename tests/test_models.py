from homelabsage.models import (
    Analysis,
    AnalyzedUpdate,
    Severity,
    Update,
    UpdateStatus,
)


def test_severity_ordering():
    assert Severity.CRITICAL.order > Severity.HIGH.order
    assert Severity.HIGH.order > Severity.MEDIUM.order
    assert Severity.MEDIUM.order > Severity.INFO.order


def test_update_id_is_stable_across_runs():
    u = Update(source="docker", subject="mealie", current_version="3.16.0", new_version="3.17.0")
    a = AnalyzedUpdate(update=u)
    b = AnalyzedUpdate(update=u)
    assert a.id == b.id == "docker:mealie:3.17.0"


def test_update_id_changes_with_new_version():
    u1 = Update(source="docker", subject="mealie", current_version="3.16.0", new_version="3.17.0")
    u2 = Update(source="docker", subject="mealie", current_version="3.16.0", new_version="3.18.0")
    assert AnalyzedUpdate(update=u1).id != AnalyzedUpdate(update=u2).id


def test_analysis_defaults_are_safe():
    a = Analysis(severity=Severity.INFO, summary="ok")
    assert a.action_required is False
    assert a.breaking_changes == []
    assert a.config_obsolete == []
    assert a.new_features_relevant == []
    assert a.recommended_action is None


def test_update_status_enum_round_trip():
    for s in UpdateStatus:
        assert UpdateStatus(s.value) is s
