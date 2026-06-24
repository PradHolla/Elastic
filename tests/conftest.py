from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["ELASTIC_STORE_BACKEND"] = "memory"
os.environ["ELASTIC_AUTO_CREATE_JOBS_TABLE"] = "false"
