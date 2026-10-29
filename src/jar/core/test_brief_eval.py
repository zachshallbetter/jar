# gate: hermetic
"""brief_eval's error kinds stay content-free: an exception name or "server"."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brief_eval


def test_error_kind():
    assert brief_eval.error_kind("TimeoutError: timed out") == "TimeoutError"
    assert brief_eval.error_kind("URLError: <urlopen error [Errno 61] Connection refused>") == "URLError"
    assert brief_eval.error_kind("{'message': 'local brain timed out'}") == "server"


if __name__ == "__main__":
    test_error_kind()
    print("ok")
