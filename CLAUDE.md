# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Costco Receipt Scanner & Price Match Agent — AI-powered tool that scans Costco receipts (PDF or camera photo), cross-references purchases against active US deals from 7 sources, and identifies price adjustment opportunities. Includes a web UI and a weekly automated email agent.

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

Receipt parsing and deal matching use a 30-day Anthropic API key, created by hand in SSM (not by CDK) and rotated by overwriting it — no redeploy; the Lambda and the AgentCore runtime re-read SSM when a key is rejected:
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

### Receipt parsing benchmark
```bash
python experiments/parse_bench.py --models app --runs 3            # the app's own parser
python experiments/parse_bench.py --models app,api:sonnet-5.5 --recheck  # compare models
```
Scores parsing against hand-checked answer keys in `experiments/data/ground_truth/` with the photos in `experiments/data/files/`. Both live only on this machine (gitignored — they contain member and card numbers); `experiments/export_data.py` re-downloads receipts from DynamoDB/S3. Every run calls real models and costs money (the summary prints the spend). Use it to check any change to `receipt_parser.py` or its prompt.

No automated test suite exists.

## Architecture

### Two entry points
- **`app.py`** — FastAPI web API, runs on Lambda (ARM64, Mangum adapter) behind API Gateway v2 with Cognito JWT auth. Serves uploads (PDF + images), receipt CRUD, price scanning, and SSE streaming analysis.
- **`agent.py`** — AgentCore Runtime entry point, triggered by EventBridge Scheduler every Friday 9pm ET. Scans deals, runs analysis, emails HTML report via Resend.

### Backend services (`services/`)
- **`db.py`** — DynamoDB (CostcoReceipts, CostcoPriceDrops tables) + S3 (receipt files with presigned URLs). Deduplicates receipts by file hash.
- **`receipt_parser.py`** — Parses receipts with Claude Haiku 5.5 on the Anthropic API (not Bedrock — the 5.x models aren't enabled there for this account), then self-checks the items against the receipt's printed subtotal and item count, asking once more if they disagree. API failures raise `ParserUnavailable` with a UI-ready message (503). Photos are EXIF-rotated upright and downscaled first (phone photos are stored sideways; sending raw pixels wrecked accuracy); PDFs are rendered to page images. Post-processes TPD merging, item number extraction with OCR correction (O→0, B→8). Model choice is backed by `experiments/parse_bench.py`.
- **`price_scanner.py`** — Scrapes US deal sources (costco.com Warehouse Savings — the authoritative one, with item numbers and dates — plus Costco Insider, Reddit r/Costco + r/CostcoDeals via one RSS search, KCL Costco Deals, KCL Coupon Book, CostcoFan). A deal has a `sale_price`, a `discount` ("$4 off" / "Save $4" — never store a discount as a price), or both; `_prices()` does that parsing for free text. CostcoFan is mostly reviews, so only sale-announcing titles count, priced from the title, never the article body. Reddit's .json API returns 403 and its RSS rate-limits (429), so keep Reddit to one request. Returns `(deals, source_results)` tuple for per-source observability. Caches per calendar day, deduplicates by (item_name, promo_end).
- **`analyzer.py`** — Code builds the candidates and does all the arithmetic; Claude Haiku 5.5 (Anthropic API, same SSM key via `receipt_parser.with_client`) only judges whether a name-similar deal is the same product. A deal must have run within 30 days after the purchase (undated forum deals count for 14 days after posting). Exact item-number matches skip the model; a deal listing *other* item numbers is never a name match. Savings = paid − sale price, or the discount if no TPD was applied. The report tables are built in code ("Match" column shows Item # or the deal's name). Streams one status event, then the report, via SSE.

### Infrastructure (`infra/`, AWS CDK TypeScript)
Three stacks: **CommonStack** (DynamoDB, S3, ECR), **AmplifyStack** (Cognito with email OTP, Lambda, API Gateway, Amplify hosting), **AgentCoreStack** (runtime, EventBridge scheduler, SSM parameters for Resend). Docker builds run on Colima locally.

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
- `agentcore.Dockerfile` does `COPY . .`, so `.dockerignore` must keep `.env` (API key), `.venv/`, `experiments/` and receipt photos out of the images — add any new local-only secret or data path there too
