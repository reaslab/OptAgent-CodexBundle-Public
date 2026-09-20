# Public solver usage

Run these commands from this directory (`cases/case_003_4x4x4_standard_lines/code`):

```text
python run.py --dry-run
python run.py --time-limit 300
```

The dry run validates the public input snapshot without solving. The full run builds and solves the 4x4x4 MILP with the selected backend and a 300-second time limit by default. Use `--check-inputs` to verify that the required files are present.

The solver reads `data/manifest.json`, which identifies `data/line_rules.json` as the deterministic 76-line geometry snapshot. Model exports are written to `model.lp` and `model.mps`; run summaries are written to `run_outputs/dry_run_summary.json` and `run_outputs/solution_summary.json`. Final case result artifacts are stored in the sibling `outputs/` directory.
