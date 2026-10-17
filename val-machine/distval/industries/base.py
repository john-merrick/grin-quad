from abc import ABC, abstractmethod

from distval.schema import EngineInputs


class BaseAdapter(ABC):

    @abstractmethod
    def to_engine_inputs(self, drivers) -> EngineInputs:
        """
        Translate industry-specific Drivers into the common EngineInputs.
        Pure function of its argument — no I/O, no network, no clock.
        """
        ...

    @abstractmethod
    def drivers_schema(self):
        """Return the Pydantic model class for this industry's Drivers."""
        ...

    def coerce_raw(self, raw: dict) -> dict:
        """
        Coerce raw YAML-loaded values to the correct Python types for the
        drivers schema. Override in each adapter to handle Decimal fields.
        """
        return raw
