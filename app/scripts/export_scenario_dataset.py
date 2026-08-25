"""Export a hand-rolled scenario module to a scenario dataset document.

Usage:
    python scripts/export_scenario_dataset.py <module_path> <output.json>

Imports the module by path (it must expose the scenario surface —
utils/scenario.py and the extracted 2CR Suwałki module both do) and
serializes utils.project_scenario.SCENARIO_KEYS to JSON. This is THE
tool for turning a hand-rolled scenario into per-project data; new
events either pick a shipped dataset or run their hand-rolled module
through this exporter.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from utils.project_scenario import SCENARIO_KEYS  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    module_path, out_path = sys.argv[1], sys.argv[2]
    spec = importlib.util.spec_from_file_location("scenario_dataset_source",
                                                   module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    doc = {}
    for key in SCENARIO_KEYS:
        if hasattr(mod, key):
            doc[key] = getattr(mod, key)
    missing = [k for k in ("KG", "AO_BBOX", "TERRAIN", "OPERATIONS")
               if k not in doc]
    if missing:
        raise SystemExit(f"{module_path} is missing required scenario "
                         f"surface keys: {missing}")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(doc, f, default=str)
    sizes = {k: len(json.dumps(doc[k], default=str)) for k in doc}
    print(f"wrote {out_path}: " + ", ".join(
        f"{k}={v}B" for k, v in sorted(sizes.items())))


if __name__ == "__main__":
    main()
