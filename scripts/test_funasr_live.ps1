param(
    [ValidateSet("sensevoice-small", "funasr-nano")]
    [string]$Model = "sensevoice-small",

    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Device = "gpu",

    [int]$Runs = 3
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $Python)) {
    throw "Missing .venv Python. Run scripts\install_funasr.ps1 first."
}

$TempDir = Join-Path $env:TEMP "Whisper Anywhere"
$Sample = Join-Path $TempDir "funasr_sample.wav"
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

if (!(Test-Path $Sample)) {
    Write-Host "Downloading small public FunASR sample..."
    Invoke-WebRequest `
        -Uri "https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/BAC009S0764W0121.wav" `
        -OutFile $Sample
}

$env:PYTHONPATH = Join-Path $Root "src"
$env:PYTHONIOENCODING = "utf-8"
$env:WA_FUNASR_TEST_MODEL = $Model
$env:WA_FUNASR_TEST_DEVICE = $Device
$env:WA_FUNASR_TEST_SAMPLE = $Sample
$env:WA_FUNASR_TEST_RUNS = $Runs

@'
import os
import time
from core.model_manager import ModelManager
from core.settings_manager import DEFAULT_SETTINGS, default_model_path
from core.transcriber import Transcriber


class ProbeSettings:
    def __init__(self) -> None:
        self.values = dict(DEFAULT_SETTINGS)
        device = os.environ["WA_FUNASR_TEST_DEVICE"]
        self.values.update(
            {
                "selected_model": os.environ["WA_FUNASR_TEST_MODEL"],
                "model_path": default_model_path(),
                "device": device,
                "use_gpu_if_available": device != "cpu",
                "fallback_to_cpu": True,
                "compute_type": "auto",
                "performance_preset": "balanced",
                "low_ram_mode": False,
                "low_vram_mode": False,
                "cpu_threads": "auto",
                "keep_model_loaded": True,
                "language_mode": "auto",
                "forced_language": "",
                "translate_to_english": False,
            }
        )

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value, save=True):
        self.values[key] = value


settings_manager = ProbeSettings()
model_manager = ModelManager(settings_manager)
transcriber = Transcriber(model_manager)
runs = max(1, int(os.environ.get("WA_FUNASR_TEST_RUNS", "3")))

print(f"model: {model_manager.selected_model()}")
print(f"engine: {model_manager.model_engine()}")
for index in range(runs):
    start = time.perf_counter()
    text = transcriber.transcribe(
        os.environ["WA_FUNASR_TEST_SAMPLE"], settings_manager.values
    )
    elapsed = time.perf_counter() - start
    print(f"run {index + 1}: {elapsed:.3f}s")
    print(f"runtime_message: {transcriber.runtime_message}")
    print(f"text: {text}")
    if not text:
        raise SystemExit("FunASR returned an empty transcript.")
'@ | & $Python -
