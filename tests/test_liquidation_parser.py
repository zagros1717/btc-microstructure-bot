"""
Tests for the liquidation field parser.

Validates that the parser:
  - correctly identifies dict-shaped liquidation fields
  - correctly identifies bool-shaped (True) liquidation fields
  - correctly returns None for missing/false/null values
  - does not silently misclassify normal trades

These tests are pure — no WebSocket required.
"""
from __future__ import annotations
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.ws.parsers import detect_liquidation_field as _detect_liquidation_field


def test_normal_trade_no_liquidation_field():
    tr = {"coin": "BTC", "px": "60000", "sz": "0.5", "side": "B", "time": 123456}
    assert _detect_liquidation_field(tr) is None


def test_normal_trade_explicit_null():
    tr = {"coin": "BTC", "px": "60000", "sz": "0.5", "side": "B", "liquidation": None}
    assert _detect_liquidation_field(tr) is None


def test_liquidation_dict_shape_with_metadata():
    tr = {
        "coin": "BTC", "px": "59900", "sz": "1.5", "side": "A",
        "liquidation": {
            "liquidatedUser": "0xabc...",
            "markPx": "59900.5",
            "method": "market",
        },
    }
    res = _detect_liquidation_field(tr)
    assert isinstance(res, dict)
    assert res.get("liquidatedUser") == "0xabc..."
    assert res.get("method") == "market"


def test_liquidation_dict_shape_empty():
    tr = {"coin": "BTC", "px": "59900", "sz": "1.5", "side": "A", "liquidation": {}}
    res = _detect_liquidation_field(tr)
    assert isinstance(res, dict)
    assert res == {}


def test_liquidation_bool_shape_true():
    tr = {"coin": "BTC", "px": "59900", "sz": "1.5", "side": "A", "liquidation": True}
    res = _detect_liquidation_field(tr)
    assert isinstance(res, dict)


def test_liquidation_bool_shape_false():
    tr = {"coin": "BTC", "px": "59900", "sz": "1.5", "side": "A", "liquidation": False}
    res = _detect_liquidation_field(tr)
    assert res is None


def test_liquidation_string_method_shape():
    tr = {"coin": "BTC", "px": "59900", "sz": "1.5", "side": "A", "liquidation": "market"}
    res = _detect_liquidation_field(tr)
    assert isinstance(res, dict)
    assert res.get("method") == "market"


def test_malformed_input_does_not_crash():
    assert _detect_liquidation_field(None) is None
    assert _detect_liquidation_field("not a dict") is None
    assert _detect_liquidation_field([]) is None
    assert _detect_liquidation_field({}) is None
