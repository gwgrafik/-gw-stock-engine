"""Brokerzy: interfejs `Broker` z deklaracją zdolności i adaptery (`fake`, później `capitalcom`)."""

from gwstock.broker.base import Broker, Capabilities

__all__ = ["Broker", "Capabilities"]
