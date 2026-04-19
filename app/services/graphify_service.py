import subprocess
import json
import os
from pathlib import Path

GRAPHIFY_OUTPUT = Path("graphify_output.json")

async def run_graphify(target_path: str = ".") -> dict:
    """Run graphifyy on target directory and return parsed output"""
    try:
        # Run graphifyy CLI
        result = subprocess.run(
            ["graphifyy", target_path, "--output", str(GRAPHIFY_OUTPUT)],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            raise Exception(f"Graphify failed: {result.stderr}")

        return parse_graphify_output()

    except FileNotFoundError:
        raise Exception("graphifyy not found. Run: pip install graphifyy")

def parse_graphify_output() -> dict:
    """Parse existing graphify_output.json"""
    if not GRAPHIFY_OUTPUT.exists():
        raise Exception("graphify_output.json not found. Run graphifyy first.")

    with open(GRAPHIFY_OUTPUT) as f:
        return json.load(f)