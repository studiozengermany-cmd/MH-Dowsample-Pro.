"""Calibrate the content classifier using a held-out test set."""

from __future__ import annotations

import argparse
import itertools
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import librosa
import numpy as np
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split

from config import AUDIO_EXTS

LABELS = ("loop", "one-shot", "fx")


@dataclass(frozen=True)
class Sample:
    path: Path
    label: str
    features: tuple[float, float, float, float]


@dataclass
class BenchmarkResult:
    seed: int
    train_count: int
    test_count: int
    class_counts: dict[str, int]
    weights: tuple[float, float, float, float]
    threshold: float
    train_accuracy: float
    test_accuracy: float
    report: dict[str, Any]
    confusion_matrix: list[list[int]]


def extract_features(path: Path) -> tuple[float, float, float, float]:
    y, sr = librosa.load(path, sr=None, mono=True)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    onset_score = 1 / (1 + float(np.var(onset) / (np.mean(onset) ** 2 + 1e-9)))
    frames = librosa.onset.onset_detect(onset_envelope=onset, sr=sr)
    intervals = np.diff(frames)
    regularity = (
        1 / (1 + float(np.std(intervals) / (np.mean(intervals) + 1e-9))) if intervals.size >= 2 else 0
    )
    _, beats = librosa.beat.beat_track(y=y, sr=sr)
    beat_score = min(1.0, len(beats) / max(len(y) / sr * 1.5, 1))
    zcr_score = 1 / (1 + float(np.var(librosa.feature.zero_crossing_rate(y)[0]) * 1000))
    return onset_score, regularity, beat_score, zcr_score


def load_samples(root: Path) -> list[Sample]:
    samples = []
    directories = {"loop": "loops", "one-shot": "oneshots", "fx": "fx"}
    for label in LABELS:
        directory = root / directories[label]
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() in AUDIO_EXTS:
                samples.append(Sample(path, label, extract_features(path)))
    return samples


def classify_with_weights(
    sample: Sample, weights: tuple[float, float, float, float], threshold: float
) -> str:
    duration = librosa.get_duration(path=sample.path)
    if duration < 0.8:
        return "one-shot"
    score = sum(feature * weight for feature, weight in zip(sample.features, weights, strict=True))
    return "loop" if duration >= 2 and score >= threshold else "fx"


def _accuracy(samples: list[Sample], weights: tuple[float, float, float, float], threshold: float) -> float:
    return sum(classify_with_weights(sample, weights, threshold) == sample.label for sample in samples) / len(
        samples
    )


def benchmark(samples: list[Sample], seed: int = 41) -> BenchmarkResult:
    counts = Counter(sample.label for sample in samples)
    if any(counts[label] < 2 for label in LABELS):
        raise ValueError("Each class needs at least two samples")
    if len(samples) < 15:
        raise ValueError("At least 15 total samples are required for a three-class held-out split")
    train, test = train_test_split(
        samples,
        test_size=max(len(LABELS), round(len(samples) * 0.2)),
        random_state=seed,
        stratify=[sample.label for sample in samples],
    )
    candidates = []
    values = (0.1, 0.2, 0.3, 0.4, 0.5)
    for first, second, third in itertools.product(values, repeat=3):
        fourth = round(1 - first - second - third, 1)
        if fourth < 0.1:
            continue
        weights = (first, second, third, fourth)
        for threshold in (0.45, 0.5, 0.55, 0.6, 0.65):
            candidates.append((_accuracy(train, weights, threshold), weights, threshold))
    train_accuracy, weights, threshold = max(candidates, key=lambda candidate: candidate[0])
    truth = [sample.label for sample in test]
    predicted = [classify_with_weights(sample, weights, threshold) for sample in test]
    return BenchmarkResult(
        seed=seed,
        train_count=len(train),
        test_count=len(test),
        class_counts=dict(counts),
        weights=weights,
        threshold=threshold,
        train_accuracy=train_accuracy,
        test_accuracy=float(accuracy_score(truth, predicted)),
        report=classification_report(
            truth, predicted, labels=list(LABELS), output_dict=True, zero_division=0
        ),
        confusion_matrix=confusion_matrix(truth, predicted, labels=list(LABELS)).tolist(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/benchmark_results.json"))
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args()
    result = benchmark(load_samples(args.input), args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
