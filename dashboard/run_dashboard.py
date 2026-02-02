"""Run the Streamlit dashboard."""
import os
import subprocess
import sys


def main() -> int:
    here = os.path.dirname(__file__)
    app_path = os.path.join(here, "app.py")
    return subprocess.call([sys.executable, "-m", "streamlit", "run", app_path])


if __name__ == "__main__":
    raise SystemExit(main())
