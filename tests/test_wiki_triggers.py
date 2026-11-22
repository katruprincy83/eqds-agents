import datetime as dt

from wiki.triggers import Evidence, evaluate

D = dt.date
TODAY = D(2026, 11, 20)


def ev(**k):
    base = dict(company_id="C0101", last_build=D(2026, 9, 1), last_full_build=D(2026, 8, 1))
    return Evidence(**{**base, **k})


def test_earnings_trigger_after_two_days():
    assert evaluate(ev(reported_events=[D(2026, 11, 13)]), TODAY).triggers[0] == "earnings"
    assert evaluate(ev(reported_events=[D(2026, 11, 19)]), TODAY).mode == "none"


def test_broker_six_reports_waits_14_days_and_pauses_before_release():
    dates = [D(2026, 10, d) for d in (1, 5, 9, 13, 17, 21)]
    assert "broker_6_reports" in evaluate(ev(broker_dates=dates), TODAY).triggers
    assert evaluate(ev(broker_dates=dates, last_build=D(2026, 11, 10)), TODAY).mode == "none"
    assert "broker_6_reports" not in evaluate(ev(broker_dates=dates, scheduled_events=[D(2026, 11, 24)]), TODAY).triggers


def test_three_reports_in_two_days_and_rating_changes():
    assert "broker_3_in_2_days" in evaluate(ev(broker_dates=[D(2026, 11, 1), D(2026, 11, 2), D(2026, 11, 3)]), TODAY).triggers
    assert "broker_3_changes" in evaluate(ev(rating_target_changes=3), TODAY).triggers


def test_stale_30_needs_something_new_and_full_rebuild_after_120():
    assert evaluate(ev(other_new_items=1), TODAY).triggers == ["stale30"]
    assert evaluate(ev(), TODAY).mode == "none"
    d = evaluate(ev(last_full_build=D(2026, 7, 1)), TODAY)
    assert d.mode == "full" and "Company" in d.stale_pages
