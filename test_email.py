"""Test script: run the weekly report locally and optionally send via Resend."""
import os
import re
import boto3

SEND_EMAIL = True
SENDER = os.environ.get("RESEND_FROM_EMAIL", "")
RECIPIENTS = [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()]
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Auto-fetch resource names from CloudFormation BEFORE importing services
# (db.py reads env vars at import time)
if not os.environ.get("DYNAMODB_RECEIPTS_TABLE"):
    print(f"Fetching resource names from CloudFormation ({REGION})...")
    cf = boto3.client("cloudformation", region_name=REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName="CostcoScannerCommon")["Stacks"][0]["Outputs"]
    }
    os.environ["DYNAMODB_RECEIPTS_TABLE"] = outputs["ReceiptsTableName"]
    os.environ["DYNAMODB_PRICE_DROPS_TABLE"] = outputs["PriceDropsTableName"]
    os.environ["S3_BUCKET"] = outputs["ReceiptsBucketName"]
    print("Fetched tables/bucket from CloudFormation")

# Import services after env vars are set
import resend
from services.price_scanner import scan_price_drops
from services.analyzer import run_analysis

S3_BUCKET = os.environ.get("S3_BUCKET", "")

s3 = boto3.client("s3", region_name=REGION)
_ssm = boto3.client("ssm", region_name=REGION)


def _presign_links(text: str) -> str:
    def _replace(m):
        rid = m.group(1)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": f"receipts/{rid}.pdf"},
            ExpiresIn=604800,
        )
        return f"]({url})"
    return re.sub(r"\]\(/api/receipt/([^/]+)/pdf\)", _replace, text)


def _md_to_html(md: str) -> str:
    lines = md.split("\n")
    html_lines = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\|[-| ]+\|$", stripped):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not in_table:
                in_table = True
                html_lines.append('<table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px">')
                html_lines.append("<tr>" + "".join(f'<th style="border:1px solid #ddd;padding:8px;background:#f4f4f4;text-align:left">{c}</th>' for c in cells) + "</tr>")
            else:
                row_cells = []
                for c in cells:
                    c = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', c)
                    row_cells.append(f'<td style="border:1px solid #ddd;padding:8px">{c}</td>')
                html_lines.append("<tr>" + "".join(row_cells) + "</tr>")
        else:
            if in_table:
                html_lines.append("</table><br>")
                in_table = False
            if stripped.startswith(">"):
                stripped = stripped.lstrip("> ")
            if stripped.startswith("### "):
                html_lines.append(f"<h3 style='font-family:Arial,sans-serif;margin:16px 0 8px'>{stripped[4:]}</h3>")
            elif stripped.startswith("## "):
                html_lines.append(f"<h2 style='font-family:Arial,sans-serif;margin:20px 0 8px'>{stripped[3:]}</h2>")
            else:
                converted = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", stripped)
                if converted:
                    html_lines.append(f"<p style='font-family:Arial,sans-serif;font-size:14px;margin:4px 0'>{converted}</p>")
    if in_table:
        html_lines.append("</table>")
    return "\n".join(html_lines)


deals, sr = scan_price_drops(force_refresh=False)
print(f"{len(deals)} deals from {len(sr)} sources")
for s in sr:
    print(f"  {s['name']}: {s['count']} ({s['status']})")

print("\nRunning analysis (this may take 1-2 minutes)...")
report = run_analysis()
print("--- REPORT ---")
print(report[:3000])
print(f"\n(full report: {len(report)} chars)")

if SEND_EMAIL:
    resend.api_key = os.environ.get("RESEND_API_KEY") or _ssm.get_parameter(
        Name="/costco-scanner/resend-api-key", WithDecryption=True
    )["Parameter"]["Value"]

    email_report = _presign_links(report)
    html = _md_to_html(email_report)
    response = resend.Emails.send({
        "from": SENDER,
        "to": RECIPIENTS,
        "subject": "Costco Weekly Price Match Report (TEST)",
        "html": html,
    })
    print("\nEmail sent!")
    print(response)
else:
    print("\nSet SEND_EMAIL = True to send the email")
