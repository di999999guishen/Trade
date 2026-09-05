from __future__ import annotations

import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from prediction_research.data import Bar, SeriesSnapshot
from prediction_research.evaluation import walk_forward
from prediction_research.events import _published_at, _rss_items, _score
from prediction_research.features import build_samples
from prediction_research.store import connect, insert_prediction
from prediction_research.taxonomy import classify_etf, market_scope
from prediction_research.tradingagents_adapter import decision_direction


def synthetic(symbol: str, length: int = 390) -> SeriesSnapshot:
    start = date(2024, 1, 1)
    bars = []
    price = 100.0
    for idx in range(length):
        drift = 0.0005 + math.sin(idx / 17) * 0.002
        opening = price * (1 + math.sin(idx / 11) * 0.0003)
        price = opening * (1 + drift)
        bars.append(Bar(start + timedelta(days=idx), opening, max(opening, price) * 1.002, min(opening, price) * 0.998, price, 1000 + idx, (1000 + idx) * price))
    return SeriesSnapshot(symbol, tuple(bars), f"{symbol}.csv", "test")


class CoreTests(unittest.TestCase):
    def test_label_starts_at_next_open(self):
        snapshot = synthetic("A", 90)
        sample = build_samples(snapshot, 5)[0]
        bars = snapshot.bars
        expected = bars[65].close / bars[61].open - 1
        self.assertAlmostEqual(sample.target_return, expected)
        self.assertEqual(sample.feature_date, bars[60].trading_date)

    def test_walk_forward_has_point_in_time_boundary(self):
        samples = build_samples(synthetic("A"), 5) + build_samples(synthetic("B"), 5)
        config = {"min_train_dates": 252, "max_train_dates": 300, "test_window_dates": 20, "calibration_fraction": 0.2, "min_calibration_rows": 20, "learning_rate": 0.05, "iterations": 50, "l2": 0.01}
        _, folds = walk_forward(samples, config)
        self.assertTrue(folds)
        for fold in folds:
            self.assertLess(fold["latest_observed_target"], fold["test_start"])

    def test_prediction_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            with connect(Path(directory) / "test.db") as connection:
                payload = {"symbol": "A", "feature_date": "2026-01-01", "horizon": 5, "probability_up": 0.6, "probability_status": "uncalibrated", "model_version": "v1", "data_snapshot": {}, "evidence": []}
                first_id, first_inserted = insert_prediction(connection, payload)
                second_id, second_inserted = insert_prediction(connection, payload)
                self.assertTrue(first_inserted)
                self.assertFalse(second_inserted)
                self.assertEqual(first_id, second_id)

    def test_rss_parsing_and_relevance_mapping(self):
        raw = b"<rss><channel><item><title>Oil inventories rise</title><description>petroleum stocks</description><link>https://example.test/a</link></item></channel></rss>"
        item = _rss_items(raw)[0]
        cfg = {"news": {"exposure_keywords": {"crude_oil": ["oil", "petroleum"], "gold": ["gold"]}}}
        source = {"reliability": 0.98, "directness": 0.95, "default_exposures": []}
        scored = _score(item, source, cfg)
        self.assertEqual(scored["exposures"], ["crude_oil"])
        self.assertGreater(scored["relevance_score"], 0.5)

    def test_broad_source_without_keyword_has_no_exposure(self):
        item = {"title": "Administrative appointment", "summary": "", "url": "", "published_at": None}
        cfg = {"news": {"exposure_keywords": {"crude_oil": ["oil"]}}}
        source = {"reliability": 0.99, "directness": 0.95, "default_exposures": []}
        scored = _score(item, source, cfg)
        self.assertEqual(scored["exposures"], [])
        self.assertEqual(scored["relevance_score"], 0.05)

    def test_rss_date_is_normalized_to_utc(self):
        self.assertEqual(_published_at("Fri, 04 Sep 2026 10:30:00 -0400"), "2026-09-04T14:30:00+00:00")

    def test_etf_taxonomy_uses_separate_asset_and_market_axes(self):
        self.assertEqual(classify_etf("黄金ETF")["asset_class"], "commodity")
        self.assertEqual(classify_etf("国债ETF")["asset_class"], "bond")
        self.assertEqual(classify_etf("沪深300ETF")["subtype"], "broad_market")
        self.assertEqual(market_scope("恒生科技ETF"), "cross_border")

    def test_agent_decision_direction_is_not_a_probability(self):
        self.assertEqual(decision_direction("BUY"), "up")
        self.assertEqual(decision_direction("建议观望"), "neutral")
        self.assertEqual(decision_direction("insufficient evidence"), "unclassified")


if __name__ == "__main__":
    unittest.main()
