"""Load the trusted OR-Tools CpModel schema without importing the solver SDK."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any


_DESCRIPTOR_SET_RESOURCE = ("_schema", "cp_model.descriptor")
_DESCRIPTOR_FILE_NAME = "ortools/sat/cp_model.proto"
_MODEL_MESSAGE_NAME = "operations_research.sat.CpModelProto"


@lru_cache(maxsize=1)
def cp_model_proto_class() -> type[Any]:
    """Return CpModelProto from an isolated pool and the bundled v9.12 schema.

    The checked descriptor is built from Google's official v9.12 ``cp_model.proto``.
    It is read from this trusted package, never from a Case or model artifact.
    The caller must already have verified the evidence Ref before parsing its bytes.
    """
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    try:
        resource = resources.files(__package__).joinpath(*_DESCRIPTOR_SET_RESOURCE)
        raw = resource.read_bytes()
        descriptor_set = descriptor_pb2.FileDescriptorSet()
        descriptor_set.ParseFromString(raw)
        if descriptor_set.SerializeToString(deterministic=True) != raw:
            raise ValueError("trusted CpModel descriptor set is not canonical")
        if len(descriptor_set.file) != 1:
            raise ValueError("trusted CpModel descriptor set must contain exactly one file")
        file_descriptor = descriptor_set.file[0]
        if file_descriptor.name != _DESCRIPTOR_FILE_NAME or file_descriptor.package != "operations_research.sat":
            raise ValueError("trusted CpModel descriptor has an unexpected identity")

        pool = descriptor_pool.DescriptorPool()
        pool.AddSerializedFile(file_descriptor.SerializeToString(deterministic=True))
        descriptor = pool.FindMessageTypeByName(_MODEL_MESSAGE_NAME)
        return message_factory.GetMessageClass(descriptor)
    except ImportError:
        raise
    except Exception as exc:
        raise ValueError("trusted CpModel schema descriptor is invalid") from exc
