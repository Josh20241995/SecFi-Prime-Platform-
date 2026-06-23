import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import CYCLE_AS_OF, load_corporate_action_events, load_positions  # noqa: E402
from secfi_platform.common.enums import ActionUrgency  # noqa: E402
from secfi_platform.corporate_actions.ca_impact_engine import (  # noqa: E402
    build_corporate_action_watchlist,
    watchlist_to_recommendations,
)


class TestCorporateActionEngine(unittest.TestCase):
    def setUp(self):
        self.events = load_corporate_action_events()
        self.positions = load_positions()

    def test_watchlist_includes_all_events_within_window(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=60)
        self.assertEqual(len(watchlist), len(self.events))

    def test_reverse_split_two_days_out_is_immediate_or_act_today(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=60)
        impact = next(i for i in watchlist if i.event.event_id == "CA002")
        self.assertIn(impact.urgency, (ActionUrgency.IMMEDIATE, ActionUrgency.ACT_TODAY))

    def test_far_out_split_is_lower_urgency_than_near_term_reverse_split(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=60)
        reverse_split = next(i for i in watchlist if i.event.event_id == "CA002")
        far_split = next(i for i in watchlist if i.event.event_id == "CA004")
        self.assertGreater(reverse_split.composite_risk_score, far_split.composite_risk_score)

    def test_window_filter_excludes_events_beyond_horizon(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=5)
        ids = {i.event.event_id for i in watchlist}
        self.assertNotIn("CA003", ids)   # merger record date is 22 days out

    def test_affected_positions_correctly_linked(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=60)
        gme_impact = next(i for i in watchlist if i.event.event_id == "CA002")
        position_ids = set(gme_impact.affected_position_ids)
        self.assertEqual(position_ids, {"P003", "P011"})

    def test_watchlist_sorted_by_composite_risk_descending(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=60)
        scores = [i.composite_risk_score for i in watchlist]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_informational_events_produce_no_recommendations(self):
        watchlist = build_corporate_action_watchlist(self.events, self.positions, CYCLE_AS_OF, window_days=60)
        recs = watchlist_to_recommendations(watchlist)
        monitor_event_ids = {i.event.event_id for i in watchlist if i.urgency == ActionUrgency.INFORMATIONAL}
        rec_event_ids = {r.supporting_metrics["event_id"] for r in recs}
        self.assertEqual(monitor_event_ids & rec_event_ids, set())


if __name__ == "__main__":
    unittest.main()
