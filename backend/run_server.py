#!/usr/bin/env python3
"""Simple server runner for the Lumibot backend."""
import subprocess
import sys
import time
import urllib.request
import json

def main():
    # Install dependencies using uv
    print("Installing dependencies...")
    subprocess.run(["uv", "pip", "install", "-q", "fastapi", "uvicorn", "pydantic"], check=True)
    
    # Start the server
    print("Starting server on port 8000...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    # Wait for server to be ready
    print("Waiting for server to be ready...")
    for i in range(30):
        try:
            resp = urllib.request.urlopen("http://localhost:8000/status", timeout=2)
            data = json.loads(resp.read().decode())
            if data.get("status") == "running":
                print(f"Server is ready! Status: {data['status']}")
                return 0
        except Exception:
            pass
        time.sleep(1)
    
    print("Server failed to start.")
    proc.terminate()
    return 1

if __name__ == "__main__":
    sys.exit(main())
