"""Snapshot recent receipts + price drops (and receipt files) from AWS for offline model experiments.

Usage: python experiments/export_data.py [--days 60] [--no-files]
Writes to experiments/data/ (gitignored).
"""
import argparse
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
OUT = Path(__file__).parent / "data"


def stack_outputs():
    cf = boto3.client("cloudformation", region_name=REGION)
    outs = cf.describe_stacks(StackName="CostcoScannerCommon")["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def scan_all(table):
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def to_json(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(type(o))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--no-files", action="store_true")
    args = ap.parse_args()

    outs = stack_outputs()
    ddb = boto3.resource("dynamodb", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)

    cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    receipts = scan_all(ddb.Table(outs["ReceiptsTableName"]))
    recent = [r for r in receipts if r.get("receipt_date", r.get("upload_date", ""))[:10] >= cutoff]
    recent.sort(key=lambda r: r.get("receipt_date", ""), reverse=True)
    drops = scan_all(ddb.Table(outs["PriceDropsTableName"]))

    OUT.mkdir(exist_ok=True)
    (OUT / "receipts.json").write_text(json.dumps(recent, indent=2, default=to_json))
    (OUT / "price_drops.json").write_text(json.dumps(drops, indent=2, default=to_json))
    print(f"receipts: {len(recent)} of {len(receipts)} total (since {cutoff})")
    print(f"price drops: {len(drops)}")

    if not args.no_files:
        files_dir = OUT / "files"
        files_dir.mkdir(exist_ok=True)
        for r in recent:
            key = r.get("s3_key")
            if not key:
                continue
            dest = files_dir / Path(key).name
            if not dest.exists():
                s3.download_file(outs["ReceiptsBucketName"], key, str(dest))
        print(f"files: {len(list(files_dir.iterdir()))} in {files_dir}")


if __name__ == "__main__":
    main()
