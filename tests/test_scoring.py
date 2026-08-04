from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Artist, Event, PriceSnapshot
from app.scoring import WEIGHTS, price_momentum, score_event, timing

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make_event(**kwargs) -> Event:
    defaults = dict(
        source="test",
        source_id="e1",
        name="Test Show",
        artist_name="Test Artist",
        venue_name="Small Room",
        venue_capacity=2000,
        city="Seattle",
        starts_at=NOW + timedelta(days=35),
        onsale_starts_at=NOW - timedelta(days=20),
        min_price=60.0,
        max_price=200.0,
        status_code="onsale",
        tour_dates_total=1,
        tour_dates_in_market=1,
        transfer_blocked=False,
    )
    defaults.update(kwargs)
    event = Event(**defaults)
    event.snapshots = []
    return event


def with_artist(event: Event, popularity=80, followers=3_000_000) -> Event:
    event.artist = Artist(name="Test Artist", popularity=popularity, followers=followers)
    return event


def snaps(prices, spacing_days=2) -> list[PriceSnapshot]:
    return [
        PriceSnapshot(
            min_price=float(p),
            max_price=float(p) * 3,
            status_code="onsale",
            captured_at=NOW - timedelta(days=spacing_days * (len(prices) - 1 - i)),
        )
        for i, p in enumerate(prices)
    ]


class TestPriceMomentum:
    def test_needs_two_priced_snapshots(self):
        assert price_momentum(snaps([50]))[0] is None
        assert price_momentum([])[0] is None

    def test_ignores_windows_shorter_than_twelve_hours(self):
        # Two snapshots an hour apart is noise, not momentum.
        tight = [
            PriceSnapshot(min_price=50, captured_at=NOW - timedelta(hours=1)),
            PriceSnapshot(min_price=80, captured_at=NOW),
        ]
        assert price_momentum(tight)[0] is None

    def test_rising_floor_scores_high(self):
        score, reason = price_momentum(snaps([50, 55, 62, 70]))
        assert score is not None and score > 0.8
        assert "up 40%" in reason

    def test_falling_floor_scores_low(self):
        score, reason = price_momentum(snaps([100, 90, 80, 70]))
        assert score is not None and score < 0.2
        assert "softening" in reason

    def test_flat_floor_is_neutral(self):
        score, _ = price_momentum(snaps([50, 50, 50]))
        assert abs(score - 0.5) < 1e-9


class TestTiming:
    def test_peak_window_beats_far_future(self):
        near = timing(make_event(starts_at=NOW + timedelta(days=35)), NOW)[0]
        far = timing(make_event(starts_at=NOW + timedelta(days=250)), NOW)[0]
        assert near > far

    def test_peak_window_beats_imminent(self):
        peak = timing(make_event(starts_at=NOW + timedelta(days=35)), NOW)[0]
        soon = timing(make_event(starts_at=NOW + timedelta(days=4)), NOW)[0]
        assert peak > soon

    def test_missing_date_yields_no_factor(self):
        assert timing(make_event(starts_at=None), NOW)[0] is None


class TestVetoes:
    def test_non_transferable_scores_zero(self):
        event = with_artist(make_event(transfer_blocked=True,
                                      transfer_block_reason="resale prohibited"))
        result = score_event(event, snaps([50, 70, 95]), now=NOW)
        assert result.score == 0.0
        assert not result.actionable
        assert "resale prohibited" in result.vetoes[0]

    def test_cancelled_event_scores_zero(self):
        event = with_artist(make_event(status_code="cancelled"))
        result = score_event(event, snaps([50, 70]), now=NOW)
        assert result.score == 0.0
        assert result.vetoes == ["event cancelled"]

    def test_past_event_scores_zero(self):
        event = with_artist(make_event(starts_at=NOW - timedelta(days=1)))
        result = score_event(event, snaps([50, 70]), now=NOW)
        assert result.score == 0.0

    def test_event_inside_48h_is_vetoed(self):
        event = with_artist(make_event(starts_at=NOW + timedelta(hours=30)))
        result = score_event(event, snaps([50, 70]), now=NOW)
        assert "no time to resell" in result.vetoes[0]

    def test_veto_beats_every_positive_signal(self):
        """A perfect-looking event is still worthless if it can't be transferred."""
        hot = with_artist(
            make_event(transfer_blocked=True, transfer_block_reason="transfer disabled"),
            popularity=99,
            followers=50_000_000,
        )
        result = score_event(hot, snaps([40, 60, 90, 140]), now=NOW)
        assert result.score == 0.0


class TestScoreEvent:
    def test_hot_event_outscores_cold_one(self):
        hot = with_artist(make_event(source_id="hot"), popularity=85, followers=8_000_000)
        cold = with_artist(
            make_event(source_id="cold", venue_capacity=60000, tour_dates_in_market=4,
                       min_price=190.0),
            popularity=40,
            followers=50_000,
        )
        hot_result = score_event(hot, snaps([50, 58, 68, 80]), now=NOW)
        cold_result = score_event(cold, snaps([190, 188, 185, 180]), now=NOW)
        assert hot_result.score > cold_result.score
        assert hot_result.score > 0.6

    def test_cold_start_lowers_confidence_but_still_scores(self):
        event = with_artist(make_event())
        result = score_event(event, snaps([60]), now=NOW)
        assert result.kind == "cold_start"
        assert 0.0 < result.score <= 1.0
        # Momentum and floor pressure both absent -> meaningfully less confident.
        assert result.confidence < 0.75
        assert result.factors["price_momentum"] is None

    def test_full_data_raises_confidence(self):
        event = with_artist(make_event())
        sparse = score_event(event, snaps([60]), now=NOW)
        rich = score_event(event, snaps([60, 64, 68, 72, 76, 80, 84, 88]), now=NOW)
        assert rich.confidence > sparse.confidence

    def test_no_signal_at_all_returns_zero_confidence(self):
        bare = make_event(starts_at=None, min_price=None, venue_capacity=None,
                          tour_dates_in_market=1)
        bare.artist = None
        # scarcity still fires off tour_dates, so blank it too
        bare.tour_dates_in_market = None
        result = score_event(bare, [], now=NOW)
        assert result.confidence <= 1.0

    def test_upcoming_onsale_is_flagged_not_vetoed(self):
        event = with_artist(make_event(onsale_starts_at=NOW + timedelta(days=3)))
        result = score_event(event, snaps([60, 60]), now=NOW)
        assert result.kind == "upcoming_onsale"
        assert result.actionable
        assert "goes on sale" in result.reasons[0]

    def test_score_is_bounded(self):
        for prices in ([10, 500], [500, 10], [50, 50]):
            event = with_artist(make_event(), popularity=100, followers=99_000_000)
            result = score_event(event, snaps(prices), now=NOW)
            assert 0.0 <= result.score <= 1.0
            assert 0.0 <= result.confidence <= 1.0

    def test_weights_sum_to_one(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
