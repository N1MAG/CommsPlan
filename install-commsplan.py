"""CommsPlan source installer entry point using the verified installer."""
from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("install_freqinout.py")), run_name="__main__")
