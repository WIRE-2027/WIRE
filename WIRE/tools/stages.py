"""Validate reused stages against public inputs and upstream requirements."""

import json
from pathlib import Path
from ..contracts import build_for_tb, digest


def input_signature(value):
    """Compare public Spec, RTL and normalized testbench build settings."""
    return digest(
        {
            "spec": value["spec"],
            "sources": value["sources"],
            "build": build_for_tb(value["build"], {}),
        }
    )


def load_stage(ctx, path, stage):
    """Require the matching input.json alongside an external stage result."""
    path = Path(path).expanduser().resolve()
    input_path = next(
        (
            folder / "input.json"
            for folder in list(path.parents)[:3]
            if (folder / "input.json").is_file()
        ),
        None,
    )
    if input_path is None:
        raise ValueError("Reused result needs its source input.json: " + str(path))
    if input_signature(json.loads(input_path.read_text())) != input_signature(
        ctx.frozen
    ):
        raise ValueError(
            "Reused result has different Spec, RTL or build inputs: " + str(path)
        )
    value = json.loads(path.read_text())
    if not isinstance(value.get("requirements"), list):
        raise ValueError("Reused result needs requirements: " + str(path))
    if stage == "m1" and not isinstance(value.get("tests"), list):
        raise ValueError("M1 result needs saved tests")
    if stage == "m2" and not isinstance(
        value.get("final_candidates", value.get("candidates")), list
    ):
        raise ValueError("M2 result needs saved candidates")
    ctx.save(
        "reuse/" + stage + ".json",
        {
            "source": str(path),
            "sha256": digest(value),
            "input_source": str(input_path),
            "input_signature": input_signature(ctx.frozen),
        },
    )
    return value


def validate_original_snapshot(value):
    """Accept only a pre-augmentation snapshot for the original-only ablation."""
    if value.get("scope") != "tba-original" or value.get("augmentations"):
        raise ValueError(
            "m2-original requires tba/original_result.json saved before augmentation"
        )
