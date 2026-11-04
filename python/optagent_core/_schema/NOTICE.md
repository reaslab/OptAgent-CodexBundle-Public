# Native CpModel schema and runtime notices

`cp_model.proto` is copied from Google's official OR-Tools v9.12 source at
<https://github.com/google/or-tools/blob/v9.12/ortools/sat/cp_model.proto>.
`cp_model.descriptor` is the descriptor set generated from that file without
source information or additional imported schemas. The schema is licensed
under Apache-2.0; see `LICENSE`.

The CoreBridge host installs the official Protocol Buffers Python runtime
`protobuf==5.29.5` from the dependency declaration in
`python/requirements-core-host.txt`. The editable native CP-SAT worker also
declares that runtime because its `.textproto` export calls
`google.protobuf.text_format`. Its upstream project is
<https://github.com/protocolbuffers/protobuf> and its license is BSD-3-Clause.
The license text is retained in `protobuf-LICENSE`. The runtime is installed
during image construction or by the editable-workspace dependency manager; it
is not vendored in this source tree.
