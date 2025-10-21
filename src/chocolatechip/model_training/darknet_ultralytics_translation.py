# src/chocolatechip/model_training/darknet_ultralytics_translation.py
from __future__ import annotations
from pathlib import Path
from glob import glob
import math
import yaml  # requires PyYAML
from typing import Iterable, Dict, Any, List, Tuple
import os

# ---------- dataset size helpers (Ultralytics YAML) ----------

def _as_list(x):
    if x is None: return []
    if isinstance(x, (list, tuple)): return list(x)
    return [x]

def _count_images_in_source(src: str) -> int:
    p = Path(src)
    if p.is_dir():
        exts = ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.tif", "*.tiff")
        return sum(len(list(p.rglob(e))) for e in exts)
    if p.is_file():
        if p.suffix.lower() == ".txt":
            try:
                return sum(1 for ln in p.read_text(errors="ignore").splitlines()
                           if ln.strip() and not ln.strip().startswith("#"))
            except Exception:
                return 0
        return 1
    try:
        return len(glob(src))
    except Exception:
        return 0

def dataset_size_from_data_yaml(data_yaml: str) -> int:
    """
    Load an Ultralytics dataset YAML and return the number of *training* images.
    Handles:
      - relative or absolute YAML path (relative resolved to /ultralytics)
      - 'path:' root inside the YAML
      - 'train:' as dir(s), file list(s) (.txt), single files, or glob patterns
    """
    # --- resolve YAML file path ---
    p = Path(data_yaml)
    if not p.is_file():
        if not p.is_absolute():
            candidate = Path("/ultralytics") / data_yaml
            if candidate.is_file():
                p = candidate
    if not p.is_file():
        raise FileNotFoundError(f"Ultralytics data YAML not found: {data_yaml}")

    # --- load YAML ---
    doc = yaml.safe_load(p.read_text(encoding="utf-8", errors="ignore"))
    if not isinstance(doc, dict) or "train" not in doc:
        raise ValueError(f"Dataset YAML missing 'train' key: {p}")

    # Base for resolving relative entries in YAML
    base_dir = p.parent
    root = doc.get("path")
    if root:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = base_dir / root_path
    else:
        root_path = base_dir

    # --- normalize train sources to a list ---
    train_sources = _as_list(doc["train"])

    # --- resolve and count ---
    total = 0
    for src in train_sources:
        if not isinstance(src, str):
            continue
        # leave URLs alone, otherwise resolve relative to 'root_path'
        if not os.path.isabs(src) and not src.startswith(("http://", "https://")):
            src = str((root_path / src).resolve())
        total += _count_images_in_source(src)

    if total <= 0:
        raise ValueError(f"No train images found using '{doc['train']}' (YAML: {p})")
    return int(total)

# ---------- Darknet -> Ultralytics schedule translation ----------

def schedule_from_darknet(
    *,
    max_batches: int,
    batch: int,
    dataset_size: int,
    steps: Iterable[int] = (),
    burn_in: int = 0,
    subdivisions: int = 1,
    round_up_ultralytics: bool = True,
) -> Dict[str, Any]:
    """
    Map a Darknet schedule to Ultralytics (epochs) and report Darknet iterations.

    Returns a dict with:
      - darknet: total_iterations, images_per_iteration, microbatch, steps_iter, burn_in_iter
      - epochs: epochs, step_epochs, warmup_epochs, iters_per_epoch (float and ceil)
    """
    if dataset_size <= 0 or batch <= 0 or max_batches <= 0:
        raise ValueError("dataset_size, batch, and max_batches must be positive")

    total_iterations = int(max_batches)
    images_per_iteration = int(batch)
    microbatch = batch / max(1, subdivisions)
    steps_iter = list(map(int, steps))
    burn_in_iter = int(burn_in)

    iters_per_epoch_f = dataset_size / batch  # may be fractional
    epochs_f = total_iterations / iters_per_epoch_f
    step_epochs_f = [s / iters_per_epoch_f for s in steps_iter]
    warmup_epochs_f = burn_in_iter / iters_per_epoch_f if burn_in_iter else 0.0

    if round_up_ultralytics:
        epochs = math.ceil(epochs_f)
        step_epochs = [math.ceil(x) for x in step_epochs_f]
        warmup_epochs = math.ceil(warmup_epochs_f) if burn_in_iter else 0
        iters_per_epoch_ceil = math.ceil(iters_per_epoch_f)
    else:
        epochs = epochs_f
        step_epochs = step_epochs_f
        warmup_epochs = warmup_epochs_f
        iters_per_epoch_ceil = iters_per_epoch_f

    return {
        "darknet": {
            "total_iterations": total_iterations,
            "images_per_iteration": images_per_iteration,
            "microbatch": microbatch,
            "steps_iter": steps_iter,
            "burn_in_iter": burn_in_iter,
        },
        "epochs": {
            "epochs": epochs,
            "step_epochs": step_epochs,
            "warmup_epochs": warmup_epochs,
            "iters_per_epoch_float": iters_per_epoch_f,
            "iters_per_epoch_ceil": iters_per_epoch_ceil,
        },
    }

def auto_ultra_epochs_from_darknet(
    *,
    data_yaml: str,
    iterations: int,
    batch: int,
    subdivisions: int = 1,
    steps: Tuple[int, ...] = (),
    burn_in: int = 0,
    round_up: bool = True,
) -> int:
    ds_size = dataset_size_from_data_yaml(data_yaml)
    sched = schedule_from_darknet(
        max_batches=iterations,
        batch=batch,
        dataset_size=ds_size,
        steps=steps,
        burn_in=burn_in,
        subdivisions=subdivisions,
        round_up_ultralytics=round_up,
    )
    return int(sched["epochs"]["epochs"])

# ---------- Ultralytics CLI builder (no ultra_args string) ----------

def build_ultralytics_cmd(*, profile, device_indices: list[int], run_dir: str) -> str:
    device_str = ",".join(str(i) for i in device_indices) if device_indices else "0"
    imgsz = max(profile.width, profile.height)

    epochs = (
        int(profile.epochs)
        if profile.epochs is not None
        else auto_ultra_epochs_from_darknet(
            data_yaml=profile.ultra_data,
            iterations=profile.iterations,
            batch=profile.batch_size,
            subdivisions=profile.subdivisions,
            steps=(),
            burn_in=0,
            round_up=True,
            seed = profile.training_seed,
        )
    )

    # Point project at the specific benchmark run directory; keep a simple name
    project = run_dir
    run_name = "train"  # or f"{profile.name}_train" if you prefer

    core = (
        f"task=detect mode=train "
        f"data={profile.ultra_data} "
        f"model={profile.ultra_model} "
        f"epochs={epochs} "
        f"batch={profile.batch_size} "
        f"imgsz={imgsz} "
        f"lr0={profile.learning_rate} "
        f"project={project} name={run_name} exist_ok=True "
        f"device={device_str} "
    )

    return (
        "bash -lc "
        f"'set -o pipefail; "
        f"mkdir -p {project}; "                              # ensure run dir exists
        f"yolo settings runs_dir={project}; "                # align yolo’s default runs_dir too
        f"yolo {core} 2>&1 | tee training_output.log'"
    )
