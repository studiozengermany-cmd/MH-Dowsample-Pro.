"""
Benchmark script for QualityGate.
Tests accuracy and performance on a simulated large dataset to ensure readiness for commercial scale.
"""
import time
import numpy as np
import soundfile as sf
import tempfile
from pathlib import Path
from quality_gate import QualityGate

def generate_test_audio(duration_sec=2.0, sr=44100, hz=440):
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False)
    y = 0.5 * np.sin(2 * np.pi * hz * t)
    return y.astype(np.float32)

def run_benchmark(num_samples=100):
    print(f"Starting QualityGate benchmark for {num_samples} samples...")
    gate = QualityGate()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        test_files = []
        for i in range(min(num_samples, 50)): # create unique test files max to save disk IO
            filepath = tmp_path / f"test_{i}.wav"
            y = generate_test_audio(duration_sec=2.5 + (i * 0.1), hz=440 + (i*10))
            sf.write(str(filepath), y, 44100)
            test_files.append(filepath)
            
        start_time = time.time()
        passed = 0
        failed = 0
        
        for i in range(num_samples):
            f = test_files[i % len(test_files)]
            result = gate.analyze(f)
            if result["passed"]:
                passed += 1
            else:
                failed += 1
                
        elapsed = time.time() - start_time
        
        print(f"Benchmark Complete!")
        print(f"Processed {num_samples} files in {elapsed:.2f} seconds.")
        print(f"Throughput: {num_samples / elapsed:.2f} files/sec")
        print(f"Passed: {passed}, Failed/Rejected: {failed}")

if __name__ == "__main__":
    run_benchmark(100)
