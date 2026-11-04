"""Deterministic business core for OptAgent."""

from .contracts import ContractError
from .data import DataBundle, InstanceContext, load_data_bundle

__all__ = ["CaseStore", "ContractError", "DataBundle", "InstanceContext", "load_data_bundle"]


def __getattr__(name: str):
    if name == "CaseStore":
        from .store import CaseStore

        return CaseStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
