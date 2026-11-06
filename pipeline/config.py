"""
CONFIG - loads config.json, the single place where every setting lives.

Why a config file?  (decision D-02 in README)
    Business people change their minds: "Gold should start at 1.5 lakh",
    "add a new source file". If those values are written inside the code,
    every change needs a developer. In config.json, it is a one-line edit
    and NO code changes. That is what "configuration driven" means.
"""
import json
from pathlib import Path

# The project folder (the folder that holds config.json).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"

# Sections config.json must have. If one is missing we stop early with a clear message,
# instead of crashing later with a confusing KeyError deep inside some step.
# >>> TO CHANGE: if you add a new top-level section that the code needs, add its name here.
REQUIRED_SECTIONS = ["input_sources", "column_types", "quality", "segments", "insights"]


def load_config(path=None):
    """Read config.json and return it as a Python dict.

    Relative folders inside the config (input_dir, output_dir, log_dir) are turned
    into full paths based on the project folder, so the pipeline works no matter
    which folder you start it from.
    """
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        config = json.load(f)

    # WHAT: fail fast if a required section is missing.
    missing = [s for s in REQUIRED_SECTIONS if s not in config]
    if missing:
        from pipeline.diagnostics import PipelineDataError
        raise PipelineDataError(f"config file {path.name} is missing section(s): {missing}",
                                hint="Compare your config with config.json in the repository.")

    # WHAT: turn "data/raw" into "D:/.../customer360/data/raw".
    # WHY: so `python run_pipeline.py` works from any working directory, and on any machine.
    for key, default in [("input_dir", "data/raw"), ("output_dir", "output"), ("log_dir", "logs")]:
        folder = Path(config.get(key, default))
        config[key] = folder if folder.is_absolute() else PROJECT_ROOT / folder
    return config
