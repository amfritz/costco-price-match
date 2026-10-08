# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Costco Receipt Scanner & Price Match Agent — AI-powered tool that scans Costco receipts (PDF or camera photo), cross-references purchases against active US deals from 5 sources, and identifies price adjustment opportunities. Includes a web UI and a weekly automated email agent.

BYOI (Bring Your Own Infrastructure) model: users deploy CDK to their own AWS account. No SaaS backend.

## Commands

### Local development
```bash
python3 -m venv .venv && source .venv/bin/activate  # Linux/Mac
# Windows (Git Bash): source .venv/Scripts/activate
pip install -r requirements.txt
./run.sh  # starts uvicorn on localhost:8000, auto-fetches resource names from CloudFormation
```

### Deploy (all stacks)
```bash
cd infra && npm install && cd ..
./deploy.sh                                       # web app only
NOTIFY_EMAIL=your-email@example.com ./deploy.sh  # also deploys AgentCore (first time only)
```

After AgentCore is deployed, recipients and Resend API key live in SSM — no redeploy needed:
- `/costco-scanner/resend-api-key` (SecureString) — Resend API key
- `/costco-scanner/notify-emails` (String) — comma-separated recipient list

Receipt parsing uses a 30-day Anthropic API key, created by hand in SSM (not by CDK) and rotated by overwriting it — no redeploy; the Lambda re-reads SSM when a key is rejected:
- `/costco-scanner/anthropic-api-key` (SecureString) — Anthropic API key

The web UI shows a red banner when parsing is down (key rejected, out of credit, API outage) and an amber one once the parameter is 25+ days old (`GET /api/parser-status`).

### Users
Sign-up is disabled (Cognito `AllowAdminCreateUserOnly`, no Sign Up UI) because every user sees every receipt. Add or remove users with `aws cognito-idp admin-create-user` / `admin-delete-user` — the README "Users" section has the exact commands. Users sign in with an email OTP; there are no passwords. The notify-emails list is unrelated to who can log in.

### Deploy static files only (frontend changes)
```bash
./deploy.sh --static-only
```

### Deploy AgentCore separately
```bash
cd infra && npx cdk deploy CostcoScannerAgentCore \
  -c region=us-east-1 -c notifyEmail=your-email@example.com --require-approval never
```

### Cleanup
```bash
cd infra
npx cdk destroy CostcoScannerAgentCore -c region=us-east-1 -c notifyEmail=your-email
npx cdk destroy CostcoScannerAmplify -c region=us-east-1
npx cdk destroy CostcoScannerCommon -c region=us-east-1
```

No automated test suite exists.

## Architecture

### Two entry points
- **`app.py`** — FastAPI web API, runs on Lambda (ARM64, Mangum adapter) behind API Gateway v2 with Cognito JWT auth. Serves uploads (PDF + images), receipt CRUD, price scanning, and SSE streaming analysis.
- **`agent.py`** — AgentCore Runtime entry point, triggered by EventBridge Scheduler every Friday 9pm ET. Scans deals, runs analysis, emails HTML report via SES.

### Backend services (`services/`)
- **`db.py`** — DynamoDB (CostcoReceipts, CostcoPriceDrops tables) + S3 (receipt files with presigned URLs). Deduplicates receipts by file hash.
- **`receipt_parser.py`** — Parses receipts with Claude Haiku 5.5 on the Anthropic API (not Bedrock — the 5.x models aren't enabled there for this account), then self-checks the items against the receipt's printed subtotal and item count, asking once more if they disagree. API failures raise `ParserUnavailable` with a UI-ready message (503). Photos are EXIF-rotated upright and downscaled first (phone photos are stored sideways; sending raw pixels wrecked accuracy); PDFs are rendered to page images. Post-processes TPD merging, item number extraction with OCR correction (O→0, B→8). Model choice is backed by `experiments/parse_bench.py`.
- **`price_scanner.py`** — Scrapes 5 US deal sources (Reddit r/Costco, Reddit r/CostcoDeals, KCL Costco Deals, KCL Coupon Book, CostcoFan). Returns `(deals, source_results)` tuple for per-source observability. Caches per calendar day, deduplicates by (item_name, promo_end).
- **`analyzer.py`** — Strands Agents framework with Nova 2 Lite. Tool-based matching: exact item number → partial item number → keyword overlap. Streams results via SSE.

### Infrastructure (`infra/`, AWS CDK TypeScript)
Three stacks: **CommonStack** (DynamoDB, S3, ECR), **AmplifyStack** (Cognito with email OTP, Lambda, API Gateway, Amplify hosting), **AgentCoreStack** (runtime, EventBridge scheduler, SES).

### Frontend
- **Web** (`static/index.html`) — Single HTML file, SPA with passwordless email OTP auth (Cognito USER_AUTH flow, no SDK dependency). Config injected at deploy via `static/config.js`. Mobile-responsive with camera capture for receipt photos.

### Key patterns
- Every API route requires a Cognito JWT except CORS preflight (OPTIONS). The iOS app this was forked with was removed along with its unauthenticated `/api/config` credentials endpoint — don't reintroduce one.
- `/api/upload` accepts PDF, JPG, PNG, WebP, GIF — the original file is stored in S3 under `receipts/<id>.pdf` even when it is an image, so detect type by magic bytes
- `/api/analyze` uses Server-Sent Events for streaming agent output
- Receipt files stored in S3 with 7-day presigned URLs
- Price scanner uses random User-Agent rotation and 1-second rate limiting between sources
- CORS locked to `https://costco.dunkinspeeps.com` + `localhost:8000`
- All resources tagged with `project: costco-price-match` for cost tracking
