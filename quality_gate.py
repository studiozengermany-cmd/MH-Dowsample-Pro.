"""Audio quality inspection and lightweight content classification."""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import librosa
import numpy as np
import requests

from config import QUALITY
from exceptions import AudioAnalysisError, HTTPError, NetworkError
from utils.network import request_with_safe_redirects, validate_public_url

ONSET_WEIGHT = 0.35
REGULARITY_WEIGHT = 0.30
BEAT_WEIGHT = 0.25
ZCR_WEIGHT = 0.10
LOOP_THRESHOLD = 0.58
_AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"}


def _definitely_not_audio(content_type: str) -> bool:
    return content_type.startswith(("text/", "image/")) or any(
        marker in content_type for marker in ("javascript", "css", "json")
    )


class QualityGate:
    def __init__(self, quality: dict[str, float | int] | None = None) -> None:
        self.quality = dict(QUALITY if quality is None else quality)

    def pre_download_ok(self, url: str, session: requests.Session | None = None) -> tuple[bool, str]:
        client = session or requests.Session()
        try:
            response = request_with_safe_redirects(
                client,
                "HEAD",
                url,
                validator=validate_public_url,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise NetworkError(str(exc)) from exc
        try:
            # Signed object-storage URLs are often signed specifically for GET.
            # S3 then rejects HEAD with 403 even though the audio GET is valid.
            head_content_type = response.headers.get("Content-Type", "").lower()
            audio_suffix = Path(urlparse(url).path).suffix.lower() in _AUDIO_SUFFIXES
            if response.status_code in {401, 403, 405} or (
                audio_suffix and _definitely_not_audio(head_content_type)
            ):
                response.close()
                try:
                    response = request_with_safe_redirects(
                        client,
                        "GET",
                        url,
                        validator=validate_public_url,
                        headers={"Range": "bytes=0-0"},
                        stream=True,
                        timeout=10,
                    )
                except requests.RequestException as exc:
                    raise NetworkError(str(exc)) from exc
            if response.status_code >= 400:
                raise HTTPError(response.status_code)
            content_type = response.headers.get("Content-Type", "").lower()
            if _definitely_not_audio(content_type):
                return False, f"Not audio: {content_type}"
            content_range = response.headers.get("Content-Range", "")
            range_total = content_range.rpartition("/")[2]
            size_text = range_total if range_total.isdigit() else response.headers.get("Content-Length", "0")
            size = int(size_text or 0)
            limit = int(self.quality["max_file_mb"]) * 1024 * 1024
            if limit and size > limit:
                return False, f"File exceeds {self.quality['max_file_mb']} MB"
            return True, "ok"
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _get_bitrate(self, filepath: Path) -> int:
        try:
            command = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=bit_rate:stream=bit_rate",
                "-of",
                "json",
                str(filepath),
            ]
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
            data = json.loads(completed.stdout)
            bit_rate = int(data.get("format", {}).get("bit_rate") or 0)
            if not bit_rate:
                bit_rate = max(
                    (int(stream.get("bit_rate") or 0) for stream in data.get("streams", [])), default=0
                )
            if bit_rate:
                return round(bit_rate / 1000)
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass
        if filepath.suffix.lower() == ".wav":
            try:
                with wave.open(str(filepath), "rb") as wav:
                    return round(wav.getframerate() * wav.getsampwidth() * 8 * wav.getnchannels() / 1000)
            except (OSError, wave.Error):
                pass
        size_bits = filepath.stat().st_size * 8
        duration = librosa.get_duration(path=filepath)
        return round(size_bits / duration / 1000) if duration > 0 else 0

    def _classify_content(self, y: np.ndarray, sr: int) -> dict[str, Any]:
        duration = len(y) / sr
        if duration < 0.8:
            return {
                "content_type": "one-shot",
                "loop_score": 0.0,
                "is_loop": False,
                "is_oneshot": True,
                "is_fx": False,
            }
        onset = librosa.onset.onset_strength(y=y, sr=sr)
        if onset.size == 0:
            return {
                "content_type": "fx",
                "loop_score": 0.0,
                "is_loop": False,
                "is_oneshot": False,
                "is_fx": True,
            }
        onset_variance = float(np.var(onset) / (np.mean(onset) ** 2 + 1e-9))
        onset_score = 1.0 / (1.0 + onset_variance)
        frames = librosa.onset.onset_detect(onset_envelope=onset, sr=sr)
        intervals = np.diff(frames)
        regularity = (
            1.0 / (1.0 + float(np.std(intervals) / (np.mean(intervals) + 1e-9)))
            if intervals.size >= 2
            else 0.0
        )
        _, beats = librosa.beat.beat_track(y=y, sr=sr)
        beat_score = min(1.0, len(beats) / max(duration * 1.5, 1.0))
        zcr = librosa.feature.zero_crossing_rate(y)[0]
        zcr_var = float(np.var(zcr))
        zcr_score = 1.0 / (1.0 + zcr_var * 1000)
        loop_score = (
            onset_score * ONSET_WEIGHT
            + regularity * REGULARITY_WEIGHT
            + beat_score * BEAT_WEIGHT
            + zcr_score * ZCR_WEIGHT
        )
        is_loop = duration >= 2.0 and loop_score >= LOOP_THRESHOLD
        # One-shot detection for audio 0.8–3s: high ZCR variance (transient attack)
        # or low onset regularity (no repeating pattern) indicates a single hit.
        is_oneshot = not is_loop and duration < 3.0 and (zcr_var > 0.005 or onset_score < 0.3)
        is_fx = not is_loop and not is_oneshot
        content_type = "loop" if is_loop else "one-shot" if is_oneshot else "fx"
        return {
            "content_type": content_type,
            "loop_score": round(loop_score, 3),
            "is_loop": is_loop,
            "is_oneshot": is_oneshot,
            "is_fx": is_fx,
        }

    def _detect_key(self, y: np.ndarray, sr: int) -> str:
        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            profile = np.mean(chroma, axis=1)
            if not np.any(profile):
                return "Unknown"
            major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
            minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
            scores = [
                np.corrcoef(profile, np.roll(template, shift))[0, 1]
                for template in (major, minor)
                for shift in range(12)
            ]
            index = int(np.nanargmax(scores))
            names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
            mode, root = divmod(index, 12)
            return f"{names[root]}{'maj' if mode == 0 else 'min'}"
        except (ValueError, FloatingPointError):
            return "Unknown"

    def _detect_bpm(
        self, y: np.ndarray, sr: int, content: dict[str, Any], duration: float
    ) -> tuple[int, str]:
        if content["content_type"] != "loop" or duration < 2:
            return 0, "none"
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        bpm = int(round(float(np.asarray(tempo).reshape(-1)[0])))
        confidence = "high" if len(beats) >= max(4, duration) else "low"
        return bpm, confidence

    @staticmethod
    def _genre_hint(bpm: int, centroid: float, content: dict[str, Any]) -> str:
        kind = content["content_type"]
        if kind == "one-shot":
            return "one-shot"
        if kind == "fx":
            return "fx"
        if not bpm:
            return "ambient" if centroid < 2000 else "other"

        if bpm >= 160:
            return "dnb" if centroid > 3000 else "trap"
        if bpm >= 135:
            return "trap" if centroid < 3000 else "techno"
        if bpm >= 118:
            return "house" if centroid > 2000 else "deep-house"
        if bpm >= 80:
            return "hip-hop" if centroid < 2500 else "pop"
        return "lo-fi"

    def analyze(self, filepath: Path | str) -> dict[str, Any]:
        path = Path(filepath)
        result: dict[str, Any] = {
            "passed": False,
            "bitrate_kbps": 0,
            "duration_sec": 0.0,
            "silence_ratio": 1.0,
            "sample_rate": 0,
            "channels": 0,
            "rms_db": -120.0,
            "spectral_centroid_hz": 0.0,
            "bpm": 0,
            "bpm_confidence": "none",
            "key": "Unknown",
            "genre_hint": "other",
            "content_type": "unknown",
            "issues": [],
        }
        try:
            bitrate = self._get_bitrate(path)
            y, sr = librosa.load(path, sr=None, mono=False)
            sr = int(sr)
            channels = 1 if y.ndim == 1 else y.shape[0]
            mono = librosa.to_mono(y) if y.ndim > 1 else y
            duration = len(mono) / sr
            result.update(
                bitrate_kbps=bitrate, duration_sec=round(duration, 3), sample_rate=sr, channels=channels
            )
            rms = librosa.feature.rms(y=mono)[0]
            result["rms_db"] = round(
                float(librosa.amplitude_to_db(np.array([max(float(np.mean(rms)), 1e-12)]))[0]), 2
            )
            silence_ratio = float(np.mean(librosa.amplitude_to_db(np.maximum(rms, 1e-12), ref=np.max) < -45))
            result["silence_ratio"] = round(silence_ratio, 3)
            if bitrate < int(self.quality["min_bitrate_kbps"]):
                result["issues"].append(f"Bitrate below {self.quality['min_bitrate_kbps']} kbps")
            if duration < float(self.quality["min_oneshot_duration_sec"]):
                result["issues"].append("Duration too short")
                return result
            if silence_ratio > float(self.quality["max_silence_ratio"]):
                result["issues"].append("Too much silence")
                return result
            content = self._classify_content(mono, sr)
            result["content_type"] = content["content_type"]
            if content["content_type"] != "one-shot" and duration < float(self.quality["min_duration_sec"]):
                result["issues"].append("Duration too short")
            spectrum = np.abs(np.fft.rfft(mono)) ** 2
            freqs = np.fft.rfftfreq(len(mono), 1 / sr)
            total = float(np.sum(spectrum)) + 1e-12
            bass_ratio = float(np.sum(spectrum[freqs < 200]) / total)
            high_ratio = float(np.sum(spectrum[freqs > 4000]) / total)
            if bass_ratio < 0.002:
                result["issues"].append("No bass content")
            if high_ratio < 0.0001:
                result["issues"].append("No high-freq content")
            centroid = float(np.mean(librosa.feature.spectral_centroid(y=mono, sr=sr)))
            result["spectral_centroid_hz"] = round(centroid, 2)
            result["key"] = self._detect_key(mono, sr)
            result["bpm"], result["bpm_confidence"] = self._detect_bpm(mono, sr, content, duration)
            result["genre_hint"] = self._genre_hint(result["bpm"], centroid, content)
            result["passed"] = not result["issues"]
            return result
        except (OSError, ValueError, RuntimeError) as exc:
            result["issues"].append(f"Analysis failed: {exc}")
            return result
        except Exception as exc:  # Native audio libraries may expose backend-specific errors.
            raise AudioAnalysisError(str(exc)) from exc
