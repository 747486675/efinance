"""Run legacy offline checks with a hard network prohibition."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def blocked(*args, **kwargs):
    raise AssertionError("Offline tests must not access the network")


socket.socket.connect = blocked
socket.create_connection = blocked
sys.argv = ["regression.py", "--offline"]
import regression

raise SystemExit(regression.main())
