"""Write exact webhook bytes and print their HMAC-SHA256 hex signature."""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("payment_id")
parser.add_argument("status", choices=["pending", "succeeded", "failed", "refunded"])
parser.add_argument("--output", default="webhook.json")
args = parser.parse_args()
secret = os.getenv("WEBHOOK_SECRET")
if not secret:
    parser.error("Set WEBHOOK_SECRET first")
body = json.dumps(
    {"payment_id": args.payment_id, "status": args.status}, separators=(",", ":")
).encode()
Path(args.output).write_bytes(body)
print(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
