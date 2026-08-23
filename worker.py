"""Runs one polishing job inside GitHub Actions.

The workflow passes JOB_ID and the encrypted API keys as environment
variables; everything else (Sheet, Drive, Gmail) comes from Actions secrets.
"""
import json
import os

import keycrypto
from editor import run_job

if __name__ == "__main__":
    job_id = os.environ["JOB_ID"]
    keys = json.loads(keycrypto.decrypt(os.environ["KEYS_ENCRYPTED"]))
    print(f"Worker starting for job {job_id} with {len(keys)} key(s)", flush=True)
    run_job(job_id, keys)
