"""Check what inspect_media needs on this computer and install a whisper.cpp model that fits it.

    uv run python setup_media.py            # show what is missing, recommend a model, download it after confirmation
    uv run python setup_media.py --check    # only report
    uv run python setup_media.py --model small --yes

The recommendation depends on the machine: Apple Silicon runs large models fast on the GPU (Metal), while a
CPU-only computer needs a smaller model to transcribe a voice note in reasonable time. Models are downloaded
from the whisper.cpp repository on Hugging Face and checked against pinned SHA-256 hashes.

When imported (by media.py), this module only detects and recommends: it never downloads anything.
"""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional

MODEL_DIR = os.path.expanduser("~/.cache/whisper-cpp")
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"


@dataclass(frozen=True)
class Model:
    name: str        # short name for --model
    file: str
    size: int        # bytes
    sha256: str
    note: str


# Sizes and hashes from the whisper.cpp repository on Hugging Face
MODELS = {m.name: m for m in (
    Model("base", "ggml-base-q5_1.bin", 59707625,
          "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
          "fast on any computer, many mistakes outside English"),
    Model("small", "ggml-small-q5_1.bin", 190085487,
          "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
          "good balance for computers without a GPU"),
    Model("turbo", "ggml-large-v3-turbo-q5_0.bin", 574041195,
          "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
          "very accurate in most languages, fast with a GPU"),
    Model("turbo-q8", "ggml-large-v3-turbo-q8_0.bin", 874188075,
          "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1",
          "slightly more accurate than turbo, larger"),
)}


@dataclass(frozen=True)
class Machine:
    system: str          # Darwin, Linux, Windows
    apple_silicon: bool
    nvidia: bool
    ram_gb: float
    cores: int           # cores worth using for whisper (performance cores on Apple Silicon)

    def describe(self) -> str:
        if self.apple_silicon:
            kind = "Apple Silicon Mac (GPU via Metal)"
        elif self.nvidia:
            kind = f"{self.system} with an NVIDIA GPU"
        else:
            kind = f"{self.system} without a supported GPU"
        return f"{kind}, {self.ram_gb:.0f} GB RAM, {self.cores} cores for transcription"


def _sysctl(name: str) -> Optional[str]:
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def detect() -> Machine:
    """Describe this computer. Fast and side-effect free, so it can run at every server start."""
    system = platform.system()
    apple_silicon, ram, cores = False, 0.0, os.cpu_count() or 1

    if system == "Darwin":
        # hw.optional.arm64 is also 1 when Python itself runs under Rosetta
        apple_silicon = _sysctl("hw.optional.arm64") == "1"
        ram = int(_sysctl("hw.memsize") or 0) / 2**30
        cores = int(_sysctl("hw.perflevel0.physicalcpu") or _sysctl("hw.physicalcpu") or cores)
    elif system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram = int(line.split()[1]) / 2**20
                        break
        except OSError:
            pass
        cores = max(1, cores // 2)  # count physical cores, not hyperthreads

    nvidia = False
    if not apple_silicon and shutil.which("nvidia-smi"):
        try:
            nvidia = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return Machine(system, apple_silicon, nvidia, ram, max(1, cores))


def recommend(machine: Machine) -> Model:
    """The most accurate model that still transcribes a voice note quickly on this machine."""
    if machine.apple_silicon:
        return MODELS["turbo"] if machine.ram_gb >= 8 else MODELS["small"]
    if machine.nvidia and machine.ram_gb >= 16:
        return MODELS["turbo-q8"]
    if machine.nvidia:
        return MODELS["turbo"]
    if machine.ram_gb >= 16 and machine.cores >= 8:
        return MODELS["small"]
    return MODELS["base"]


def threads(machine: Machine) -> int:
    """Threads for whisper-cli. Its default of 4 wastes larger CPUs, which matters most without a GPU."""
    return max(1, min(machine.cores, 8))


def find_tool(*names: str) -> Optional[str]:
    """Find a command, also in the Homebrew folders that MCP clients often leave out of PATH."""
    for name in names:
        for candidate in (shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
            if candidate and os.access(candidate, os.X_OK):
                return candidate
    return None


def setup_command(model: Optional[Model] = None) -> str:
    args = f" --model {model.name}" if model else ""
    return f"cd {SERVER_DIR} && uv run python setup_media.py{args}"


def install_hint(tool: str, machine: Machine) -> str:
    packages = {"whisper-cli": ("whisper-cpp", None), "ffmpeg": ("ffmpeg", "ffmpeg"), "pdftotext": ("poppler", "poppler-utils")}
    brew, apt = packages[tool]
    if machine.system == "Darwin":
        return f"brew install {brew}"
    if tool == "whisper-cli":
        return "build whisper.cpp (https://github.com/ggml-org/whisper.cpp) and put whisper-cli on PATH"
    return f"sudo apt install {apt} (or your distribution's package)"


# --- download ---------------------------------------------------------------------------------------

def installed_models() -> list:
    return [m for m in MODELS.values() if os.path.isfile(os.path.join(MODEL_DIR, m.file))]


def download(model: Model) -> str:
    """Download model into MODEL_DIR, verify its hash, and return its path."""
    os.makedirs(MODEL_DIR, exist_ok=True)
    target = os.path.join(MODEL_DIR, model.file)
    partial = target + ".part"
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(BASE_URL + model.file, headers={"User-Agent": "whatsapp-mcp-setup"})
    with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            out.write(block)
            digest.update(block)
            done += len(block)
            print(f"\r  {done / 2**20:.0f} / {model.size / 2**20:.0f} MB", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    if done != model.size or digest.hexdigest() != model.sha256:
        os.unlink(partial)
        raise RuntimeError(f"The download of {model.file} is corrupted or was changed (hash mismatch); nothing was installed")
    os.replace(partial, target)
    return target


# --- command line -----------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Check and set up media support for the WhatsApp MCP server.")
    parser.add_argument("--check", action="store_true", help="only report, do not download")
    parser.add_argument("--model", choices=sorted(MODELS), help="download this model instead of the recommended one")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    machine = detect()
    best = recommend(machine)
    print(f"This computer: {machine.describe()}")
    print(f"Recommended model: {best.name} ({best.file}, {best.size / 2**20:.0f} MB): {best.note}\n")

    tools = [("ffmpeg", "images, video and voice notes"), ("pdftotext", "PDF documents"),
             ("whisper-cli", "voice note transcription")]
    for tool, use in tools:
        found = find_tool(tool)
        print(f"  {'ok     ' if found else 'MISSING'} {tool:12} {use}" + ("" if found else f": {install_hint(tool, machine)}"))
    installed = installed_models()
    print(f"  {'ok     ' if installed else 'MISSING'} {'model':12} " +
          (", ".join(m.file for m in installed) if installed else f"no whisper.cpp model in {MODEL_DIR}"))

    model = MODELS[args.model] if args.model else best
    if args.check or model in installed:
        if model in installed and not args.check:
            print(f"\n{model.file} is already installed.")
        return 0

    print(f"\nDownload {model.file} ({model.size / 2**20:.0f} MB) to {MODEL_DIR}?")
    if not args.yes and input("[y/N] ").strip().lower() not in ("y", "yes", "s", "si", "sì"):
        print("Nothing downloaded.")
        return 1
    path = download(model)
    print(f"Installed {path}. Restart your MCP client so the WhatsApp server picks it up.")
    if not find_tool("whisper-cli"):
        print(f"whisper-cli is still missing: {install_hint('whisper-cli', machine)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
