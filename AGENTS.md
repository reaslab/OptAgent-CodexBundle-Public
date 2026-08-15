# OptAgent shared workspace instructions

These repository instructions apply to the Coordinator and every configured native role. The complete Coordinator workflow contract is loaded from `.codex/config.toml`; do not duplicate it here.

- Use only the configured native roles: `opt-formulator`, `opt-solver`, `opt-verifier`, and `opt-reporter`. Leaf roles must not create or manage other agents.
- Keep internal state and evidence under `.optagent/cases/{case_id}/`; keep user-facing artifacts under `cases/{case_id}/`.
- Treat `.agents/skills/`, `.agents/tools/`, `.agents/schemas/`, and `.agents/solver-templates/` as bundled read-only resources during a case run.
- Resolve and use an available local Python interpreter through the shell. Do not rely on `python-execute`, `/app` mounts, container-only paths, or hosted-platform file mirroring.
- Preserve user inputs and unrelated files. Before editing an existing case, inspect its state and current artifacts; do not mix evidence across case IDs.
- Keep formulation, implementation, verification, and reporting claims tied to inspectable artifacts. Do not promote a solver result to completion before independent verification and the public-artifact audit.
- Follow the requested output language, solver constraints, approval checkpoints, and deliverable location.
