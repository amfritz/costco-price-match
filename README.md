# Costco Receipt Scanner & Price Match Agent

AI-powered tool that scans your Costco receipts, cross-references purchases against active US deals, and tells you exactly which items dropped in price and how much you can get back at the membership counter.

A weekly agent runs every Friday at 9pm ET, generates a formatted HTML report, and emails it to you via Resend.

Forked from [the original Canadian version](https://github.com/waltsims/costco-price-match) and adapted for US Costco deal sources.

![Architecture](diagrams/architecture.png)

## How It Works

1. Upload receipt PDFs or snap a photo with your phone's camera
2. Claude Haiku 5.5 (Anthropic API) parses every line item, price, item number, and TPD (Temporary Price Drop)
3. Scrapers pull current deals from Reddit r/Costco, Reddit r/CostcoDeals, KrazyCouponLady, and CostcoFan
4. AI cross-references your purchases against active deals
5. Weekly agent emails you a report with price adjustment opportunities and TPD savings already applied

![Weekly Flow](diagrams/weekly-flow.png)

## What's Different from the Original

- **US deal sources** — Replaced 6 Canadian sources (CocoWest, CocoEast, RedFlagDeals, SmartCanucks, etc.) with 5 US sources (Reddit r/Costco, Reddit r/CostcoDeals, KCL Costco Deals, KCL Coupon Book, CostcoFan)
- **Camera upload** — Snap a photo of your receipt directly from the web app on mobile, no PDF scanning needed
- **Image support** — Upload JPG, PNG, WebP alongside PDFs; photos are auto-rotated upright before parsing
- **Per-source observability** — Scan results show status, deal count, and duration for each scraper
- **Passwordless auth** — Email OTP sign-in via Cognito (no passwords to manage)
- **Mobile-responsive UI** — Styled for phone use with camera capture, touch-friendly modals
- **Deploy improvements** — `--static-only` flag for quick frontend deploys, Windows/Git Bash compatibility

## Architecture

- **Web Frontend**: Static HTML on AWS Amplify with Cognito email OTP authentication
- **API**: API Gateway HTTP API → Lambda (FastAPI + Mangum), streaming analysis responses
- **AI**: Claude Haiku 5.5 (Anthropic API) for receipt parsing, Amazon Nova 2 Lite (Bedrock) for analysis
- **Automation**: AgentCore Runtime triggered by EventBridge Scheduler universal target (no Lambda middleman), Resend for email
- **Storage**: DynamoDB (receipts + deals), S3 (receipt files with presigned URLs)
- **Infrastructure**: CDK (TypeScript), 3 stacks, deploy to any region

## Prerequisites

- AWS CLI configured with credentials
- Node.js 18+ and npm
- Docker running (on macOS, Colima works: `brew install colima docker docker-buildx && colima start`)
- Python 3.12+

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
./run.sh
```

Opens on `http://localhost:8000`. Auto-fetches DynamoDB/S3 resource names from the CDK stack.

## Deploy

```bash
cd infra && npm install && cd ..

# Deploy web app (Lambda, Amplify, API Gateway, Cognito, DynamoDB, S3)
./deploy.sh

# Also deploy the weekly email agent (first time only)
NOTIFY_EMAIL=your-email@example.com ./deploy.sh

# Deploy frontend changes only (no CDK/Docker rebuild)
./deploy.sh --static-only
```

`NOTIFY_EMAIL` is only required on the first deploy of AgentCore. After that, recipients and the Resend API key live in SSM Parameter Store and can be updated without redeploying.

### SSM Parameter Store

**Anthropic API key** — required for receipt parsing. Create it by hand (CDK doesn't manage it) as a SecureString before uploading receipts:
```bash
# Set or rotate (get a key at platform.claude.com) -- no redeploy needed
aws ssm put-parameter --name /costco-scanner/anthropic-api-key \
  --value "sk-ant-YOUR_KEY_HERE" --type SecureString --overwrite
```
The key used here is a 30-day key. The web app shows an amber banner once the parameter is 25+ days old, and a red one if parsing fails (key rejected, out of credit, API outage). Overwriting the parameter clears the warning; the Lambda re-reads SSM when a key is rejected.

The weekly agent reads these two parameters at runtime:

**Resend API key** — stored as SecureString, set after first deploy:
```bash
# Set (get your key at resend.com)
aws ssm put-parameter --name /costco-scanner/resend-api-key \
  --value "re_YOUR_KEY_HERE" --type SecureString --overwrite

# Get
aws ssm get-parameter --name /costco-scanner/resend-api-key \
  --with-decryption --query Parameter.Value --output text
```

**Email recipients** — comma-separated, change anytime without redeploying:
```bash
# Set
aws ssm put-parameter --name /costco-scanner/notify-emails \
  --value "you@example.com,other@example.com" --type String --overwrite

# Get
aws ssm get-parameter --name /costco-scanner/notify-emails \
  --query Parameter.Value --output text
```

### Users

Sign-up is disabled: the Cognito pool is invite-only and the login page has no Sign Up link, so only users you create can sign in. They sign in with an emailed code — there are no passwords. Every user sees every receipt (see Per-user data isolation under Backlog), so only add people you'd share receipts with.

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name CostcoScannerAmplify \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)

# Add a user
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username someone@example.com \
  --user-attributes Name=email,Value=someone@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS

# List users
aws cognito-idp list-users --user-pool-id $POOL_ID \
  --query 'Users[].Attributes[?Name==`email`].Value' --output text

# Remove a user
aws cognito-idp admin-delete-user --user-pool-id $POOL_ID --username someone@example.com
```

This is separate from the weekly email recipients in `/costco-scanner/notify-emails` — being on that list doesn't grant a login, and a login doesn't add you to the email.

## Cleanup

```bash
cd infra
npx cdk destroy CostcoScannerAgentCore -c region=us-east-1 -c notifyEmail=your-email@example.com
npx cdk destroy CostcoScannerAmplify -c region=us-east-1
npx cdk destroy CostcoScannerCommon -c region=us-east-1
```

## Backlog

- **Custom domain SSL** — `costco.dunkinspeeps.com` is configured and working. Consider moving the main domain DNS to Route53 for tighter integration.
- **Scraper resilience** — Deal sources change HTML structure without warning. The per-source observability helps detect failures, but scrapers may need periodic updates when sites change.
- **Receipt parsing accuracy** — Parsing self-checks against the receipt's printed subtotal and item count and asks the model again when they disagree. `experiments/parse_bench.py` scores models against hand-checked answer keys, but only two receipts have keys so far; more (long receipts, quantities, coupons) would make model comparisons more trustworthy.
- **Per-user data isolation** — All authenticated users share all receipts. Fine for family use with signup disabled, but would need row-level filtering (e.g., by Cognito sub) if opened to more users.
- **Activate cost allocation tag** — The `project: costco-price-match` tag is on all resources but needs to be activated in AWS Billing as a cost allocation tag (takes 24h after first tagging) to filter in Cost Explorer.

## Cost

Under $1/month for personal use. Model tokens are the main cost: receipt parsing is ~$0.0013 per receipt with Haiku 5.5, plus ~$0.10-0.20/week of Bedrock Nova analysis. Lambda, SES, DynamoDB, API Gateway, and Amplify fall within free tier. All resources are tagged with `project: costco-price-match` for cost tracking in Cost Explorer.

## Built With

- [Claude Code](https://claude.ai/code) — AI coding assistant by Anthropic (US adaptation)
- [Kiro CLI](https://kiro.dev) — AI coding assistant by AWS (original version)
- [Anthropic API](https://platform.claude.com/) — Claude Haiku 5.5 (receipt parsing)
- [Amazon Bedrock](https://aws.amazon.com/bedrock/) — Nova 2 Lite (analysis)
- [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) — Runtime for the weekly agent
- [AWS CDK](https://aws.amazon.com/cdk/) — Infrastructure as code
