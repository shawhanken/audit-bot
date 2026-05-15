from __future__ import annotations

import pytest

from common.config import parse_config


def test_parse_minimal_config():
    raw = {
        "targets": [
            {
                "name": "cips",
                "paths": ["docs/cips/**"],
                "dimensions": {
                    "consistency": {"enabled": True, "severity_gate": "block"},
                    "security": {"enabled": False},
                },
            }
        ],
        "global": {"max_usd_per_run": 1.0},
    }
    cfg = parse_config(raw)
    assert cfg.global_.max_usd_per_run == 1.0
    t = cfg.target_by_name("cips")
    assert t.dimensions["consistency"].enabled is True
    assert t.dimensions["security"].enabled is False


def test_parse_rejects_unknown_dimension():
    with pytest.raises(ValueError):
        parse_config({"targets": [{"name": "x", "paths": [], "dimensions": {"bogus": {}}}]})


def test_parse_off_gate_disables_dim():
    cfg = parse_config({
        "targets": [{
            "name": "t",
            "paths": [],
            "dimensions": {"style": {"enabled": True, "severity_gate": "off"}},
        }]
    })
    assert cfg.target_by_name("t").dimensions["style"].enabled is False
