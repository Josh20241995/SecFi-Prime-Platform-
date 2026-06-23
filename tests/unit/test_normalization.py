import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests._helpers import load_securities  # noqa: E402
from secfi_platform.common.enums import DataQualityFlag  # noqa: E402
from secfi_platform.ingestion.base import DataQualityError  # noqa: E402
from secfi_platform.normalization.schema_mapping import (  # noqa: E402
    parse_position,
    parse_rows,
    parse_security,
)


class TestNormalizationLayer(unittest.TestCase):
    def test_parse_security_round_trips_valid_row(self):
        row = {
            "internal_id": "SECTEST", "cusip": "123456789", "isin": "", "sedol": "",
            "ticker": "TEST", "description": "Test Co", "product_type": "EQUITY",
            "currency": "USD", "country_of_risk": "US", "issuer_id": "ISSU_TEST",
            "gics_sector": "Technology", "is_adr": "false", "adr_ratio": "",
            "index_memberships": "SPX|NDX",
        }
        security = parse_security(row)
        self.assertEqual(security.internal_id, "SECTEST")
        self.assertEqual(security.index_memberships, ("SPX", "NDX"))
        self.assertIsNone(security.isin)

    def test_parse_security_missing_required_field_raises(self):
        row = {"internal_id": "SECTEST", "ticker": "", "product_type": "EQUITY", "currency": "USD",
               "country_of_risk": "US", "issuer_id": "ISSU_TEST"}
        with self.assertRaises(DataQualityError):
            parse_security(row)

    def test_parse_security_invalid_enum_value_raises(self):
        row = {"internal_id": "SECTEST", "ticker": "TEST", "product_type": "NOT_A_REAL_TYPE",
               "currency": "USD", "country_of_risk": "US", "issuer_id": "ISSU_TEST"}
        with self.assertRaises(ValueError):
            parse_security(row)

    def test_parse_position_unknown_security_raises(self):
        row = {
            "position_id": "PTEST", "trade_date": "2026-01-01", "value_date": "2026-01-03",
            "direction": "LEND", "security_internal_id": "DOES_NOT_EXIST", "counterparty_id": "CPTY001",
            "booking_entity": "US_BROKER_DEALER", "quantity": "100", "market_value": "1000",
            "currency": "USD", "rate_bps": "10", "term_type": "OPEN", "desk_id": "X", "trader_id": "Y",
        }
        with self.assertRaises(DataQualityError):
            parse_position(row, {})

    def test_parse_rows_isolates_bad_rows_without_failing_batch(self):
        good_row = {
            "internal_id": "SECGOOD", "ticker": "GOOD", "product_type": "EQUITY", "currency": "USD",
            "country_of_risk": "US", "issuer_id": "ISSU_GOOD",
        }
        bad_row = {"internal_id": "SECBAD", "ticker": "", "product_type": "EQUITY", "currency": "USD",
                   "country_of_risk": "US", "issuer_id": "ISSU_BAD"}
        parsed, errors = parse_rows([good_row, bad_row], parse_security)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(parsed[0].internal_id, "SECGOOD")

    def test_real_fixture_securities_all_parse_cleanly(self):
        securities = load_securities()
        self.assertEqual(len(securities), 8)
        for sec in securities.values():
            self.assertTrue(sec.ticker)
            self.assertTrue(sec.issuer_id)


if __name__ == "__main__":
    unittest.main()
