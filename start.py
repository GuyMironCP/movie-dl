"""
Movie Downloader launcher.

  python start.py   — starts server and opens browser at http://localhost:9000
"""
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT    = Path(__file__).parent
BACKEND = ROOT / "backend"
REQ     = ROOT / "requirements.txt"
PORT    = 9000
URL     = f"http://localhost:{PORT}"


def install_deps():
    print("📦 Checking dependencies...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", str(REQ), "-q"],
        cwd=str(ROOT),
    )


def main():
    install_deps()

    threading.Thread(
        target=lambda: (time.sleep(1.5), webbrowser.open(URL)),
        daemon=True,
    ).start()

    print(f"\n🎬 Movie Downloader starting on {URL}")
    print("   Opening browser...\n")

    subprocess.run(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--reload"],
        cwd=str(BACKEND),
    )


if __name__ == "__main__":
    main()
