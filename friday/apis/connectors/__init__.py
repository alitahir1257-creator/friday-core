"""Vetted, fixed-host public API connectors."""

from friday.apis.connectors.frankfurter import FrankfurterConnector
from friday.apis.connectors.nager_date import NagerDateConnector
from friday.apis.connectors.open_meteo import OpenMeteoConnector

__all__ = ["FrankfurterConnector", "NagerDateConnector", "OpenMeteoConnector"]
