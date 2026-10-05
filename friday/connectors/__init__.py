"""FRIDAY-native connector contracts; external sources remain unimplemented."""

from friday.connectors.base import Connector
from friday.connectors.registry import ConnectorRegistry

__all__ = ["Connector", "ConnectorRegistry"]
