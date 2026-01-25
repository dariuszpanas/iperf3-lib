"""Unit tests for ClientConfig and Protocol validation."""

import pytest
from pydantic import ValidationError

from iperf3_lib.config import ClientConfig, Protocol


def test_config_defaults():
    """Test that ClientConfig sets expected defaults."""
    cfg = ClientConfig(server="127.0.0.1")
    assert cfg.port == 5201
    assert cfg.protocol == Protocol.TCP
    assert cfg.duration == 10
    assert not cfg.bidirectional


def test_config_validation():
    """Test that ClientConfig raises ValidationError for invalid tos value."""
    # Pydantic should raise a ValidationError for out-of-range `tos` (>255)
    with pytest.raises(ValidationError):
        ClientConfig(server="127.0.0.1", tos=300)  # >255 invalid
