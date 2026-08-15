# OptAgent Codex-base migration

This local branch copy packages the existing OptAgent workflow as a Codex-native multi-agent workspace. The source Rust/ACP implementation remains intact; the added files are a native configuration layer that can be reviewed independently before any commit or push.

## Mapping from the original agent

| Original source | Codex-native location | Purpose |
| --- | --- | --- |
| MMA `agent.yaml` manifest pattern | `agent.yaml` | RA/ACP agent identity, native role declarations, and configurable user requirements |
| `assets/optagent/prompts/opt-coordinator.txt` | `.codex/config.toml` | Root `developer_instructions` and native multi-agent settings |
| `assets/optagent/prompts/opt-formulator.txt` | `.codex/agents/opt-formulator.toml` | Formulation role |
| `assets/optagent/prompts/opt-solver.txt` | `.codex/agents/opt-solver.toml` | Solver implementation and execution role |
| `assets/optagent/prompts/opt-verifier.txt` | `.codex/agents/opt-verifier.toml` | Independent verification and diagnosis role |
| `assets/optagent/prompts/opt-reporter.txt` | `.codex/agents/opt-reporter.toml` | Paper/report and public-artifact role |
| `skills/` | `.agents/skills/` | Discoverable local skills |
| `assets/optagent/tools/` | `.agents/tools/` | Case state, solver, verification, and reporting helpers |
| `assets/schemas/` | `.agents/schemas/` | Structured artifact examples |
| `assets/solver-templates/` | `.agents/solver-templates/` | Portable solver scaffolds and backend probe |

## Platform coupling removed

- Hosted `/app/...` mounts and runtime mirroring are replaced by repository-local `.agents/...` resources.
- The platform-only `python-execute` adapter is replaced by the available local Python interpreter invoked through the shell.
- Coordinator dispatch uses the configured Codex native roles and retained native-agent lifecycle instead of legacy task/ACP dispatch.
- Platform UI narration assumptions are removed while the optimization case-state and approval semantics remain intact.
- Public solver code continues to stay under `cases/{case_id}/code/`; internal evidence remains under `.optagent/cases/{case_id}/`.

## Local validation

From the repository root, use any Python 3.11+ interpreter (including Codex's bundled Python) for the TOML check:

```powershell
python -c "import pathlib,tomllib; [tomllib.loads(p.read_text(encoding='utf-8')) for p in pathlib.Path('.codex').rglob('*.toml')]; print('TOML OK')"
python -m unittest discover -s tests -p "test_*.py" -v
```

The migration is intentionally local-only until its diff is reviewed. No remote repository operation is required to load or inspect these files.
