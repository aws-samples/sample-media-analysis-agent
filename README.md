# ProServe Discovery Agent

A conversational AI agent that turns recorded discovery sessions, architecture diagrams, and working documents into a queryable knowledge base — so consultants can get answers in seconds instead of spending hours re-watching meetings and cross-referencing notes.

Powered by Amazon Rekognition, Amazon Transcribe, Amazon Bedrock (Claude), and Strands Agents SDK.

## Prerequisites

Install these before deployment:

```bash
brew install awscli        # AWS CLI v2
brew install --cask finch  # Or install Docker Desktop
```

You also need:
- An AWS account with access to the configured Amazon Bedrock model
- An authenticated AWS CLI profile
- A current Midway session for Amazon Container Registry (ACR) authentication
- The ACR credential helper configured for the approved internal base image:

  ```bash
  mwinit -o
  toolbox install acr
  docker-credential-acr-login --setup
  ```

- Permissions to deploy CloudFormation stacks and create IAM, S3, SNS, ECR, ECS, VPC, ALB, CloudFront, Cognito, CloudWatch, and KMS resources

## Quick Start

Run the deployment from the repository root:

```bash
git clone <repo-url>
cd video-analytic-agent

./deploy/deploy-ecs.sh --profile <your-aws-profile> --region us-east-1
```

The deployment script creates or updates the base storage and notification stack, builds and pushes the application image to ECR, deploys ECS with an internal ALB and CloudFront, configures Cognito, and updates ECS with the authentication settings. It prints the public HTTPS URL when deployment completes.

## What It Does

- **Video analysis** — Upload a video. The agent detects objects, faces, scenes, and activities using Amazon Rekognition.
- **Meeting transcription** — Transcribe recordings in 100+ languages with speaker diarization. Ask follow-up questions about the content.
- **Document analysis** — Upload PDFs, Word docs, Excel sheets, or diagrams. The agent reads and cross-references them with video content.
- **Reference image search** — Upload a photo of a person or object, find every timestamp they appear in the video.
- **Session export** — Save analysis outputs to Word documents and download transcripts from the sidebar.

## Architecture

```
User (Browser) → CloudFront → Internal ALB → ECS Fargate → Chat UI
                                                            └── Strands Agent (Claude on Bedrock)
                                                                  ├── Video Ingestion (S3)
                                                                  ├── Visual Analysis (Rekognition)
                                                                  ├── Audio Analysis (Transcribe)
                                                                  ├── Bedrock Data Automation
                                                                  ├── Document Analysis (PDF, Word, Excel)
                                                                  └── Reference Image Matching (Rekognition)
```

## Configuration

CloudFormation injects runtime configuration into the ECS task. Key parameters include:

| Parameter | Description | Default |
|----------|-------------|---------|
| `AwsRegion` | AWS Region used by the application | `us-east-1` |
| `ModelId` | Bedrock model or inference profile | Defined in `deploy/ecs-fargate-stack.yaml` |
| `UploadQuotaGb` | Per-user upload staging quota | `15` |
| `TaskEphemeralStorageGb` | Fargate task ephemeral storage | `100` |
| `AllowSelfSignup` | Permit public Cognito registration | `false` |

Use `--region` and `--allow-self-signup` with `deploy/deploy-ecs.sh`. Change other parameters in the template or deployment script before deployment.

## Infrastructure

`deploy/deploy-ecs.sh` creates or updates all required stacks. The base template (`cfn-video-locator-infra.yaml`) provides:

- Versioned S3 storage for temporary media with a one-day lifecycle
- An S3 access-log bucket
- An encrypted SNS topic for Rekognition job notifications
- An IAM service role for Rekognition video analysis

The deployment then creates ECR, ECS Fargate, networking, an internal ALB, CloudFront, Cognito, logging, and supporting IAM resources.

## Deployment files

- `cfn-video-locator-infra.yaml` — Base S3, SNS, and Rekognition infrastructure
- `deploy/deploy-ecs.sh` — Turnkey deployment orchestration
- `deploy/ecs-fargate-stack.yaml` — ECS, networking, internal ALB, CloudFront, and task configuration
- `deploy/cognito-stack.yaml` — Cognito user pool and hosted authentication

## Cloud Deployment (ECS Fargate + internal ALB + CloudFront + Cognito)

Deploy the agent as a web application with user authentication.

### Prerequisites

- AWS CLI v2 installed and configured
- Container runtime: Finch (`brew install --cask finch`) or Docker
- A current Midway session and the ACR credential helper configured for the internal base image:

  ```bash
  mwinit -o
  toolbox install acr
  docker-credential-acr-login --setup
  ```

- An AWS account with:
  - Bedrock model access enabled (Claude Sonnet in us-east-1)
  - Permissions to create CloudFormation stacks, ECS, ECR, ALB, Cognito

### Deploy

Run from the repository root:

```bash
./deploy/deploy-ecs.sh \
  --profile <your-aws-profile> \
  --region us-east-1
```

Public self-registration is disabled by default. To enable it explicitly:

```bash
./deploy/deploy-ecs.sh \
  --profile <your-aws-profile> \
  --region us-east-1 \
  --allow-self-signup true
```

The script performs the complete deployment:

1. Verifies the AWS identity.
2. Creates or updates `video-object-locator-infra` from `cfn-video-locator-infra.yaml`.
3. Creates the ECR repository if needed, pulls the approved Amazon Linux 2023 Python base image from ACR, builds the local Dockerfile for AMD64, and pushes the application image.
4. Deploys ECS Fargate, the internal ALB, CloudFront, networking, logging, and IAM resources.
5. Deploys Cognito using the generated CloudFront HTTPS URL.
6. Updates ECS with the Cognito configuration and prints the public URL.

CloudFront and its VPC origin dominate deployment time. Allow approximately 15–30 minutes for a new deployment and several additional minutes for ECS health checks.

The public entry point is the printed `https://<distribution>.cloudfront.net` URL. The ALB is internal and cannot be accessed directly from the internet.

### Create the first user

With the default admin-invite configuration, obtain the user pool ID and invite a user:

```bash
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --profile <your-aws-profile> \
  --stack-name video-analytic-cognito \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
  --output text)

aws cognito-idp admin-create-user \
  --profile <your-aws-profile> \
  --user-pool-id "$USER_POOL_ID" \
  --region us-east-1 \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

The user receives a temporary password and completes password replacement and MFA enrollment at first sign-in.

### Managing Users

```bash
# List users
aws cognito-idp list-users --user-pool-id <UserPoolId> --profile <your-aws-profile> --region us-east-1

# Reset a user's password — preferred.
# Cognito issues a temporary password and forces the user to set their own at
# next sign-in, so no administrator ever knows the account password.
aws cognito-idp admin-reset-user-password \
  --user-pool-id <UserPoolId> \
  --username "<user-sub>" \
  --profile <your-aws-profile> \
  --region us-east-1

# Disable a user
aws cognito-idp admin-disable-user --user-pool-id <UserPoolId> --username "<user-sub>" --profile <your-aws-profile> --region us-east-1
```

Avoid setting a password directly. `admin-set-user-password --permanent` requires the administrator to choose the user's password and pass it on the command line, where it is captured in shell history and is visible in the process list to other users on the host. Use `admin-reset-user-password` above instead.

If a specific password genuinely must be set — for an automated test account, for example — read it from a prompt rather than embedding a literal, and note that it must satisfy the user pool policy: at least 12 characters with uppercase, lowercase, numeric, and symbol characters (see `deploy/cognito-stack.yaml`).

```bash
read -rs -p "New password: " NEW_PASSWORD

aws cognito-idp admin-set-user-password \
  --user-pool-id <UserPoolId> \
  --username "<user-sub>" \
  --password "$NEW_PASSWORD" \
  --permanent \
  --profile <your-aws-profile> \
  --region us-east-1

unset NEW_PASSWORD
```

### Managing the Service

```bash
# Stop (save cost — no running tasks)
aws ecs update-service --profile <your-aws-profile> --cluster video-analytic-cluster --service video-analytic-agent --desired-count 0 --region us-east-1

# Restart
aws ecs update-service --profile <your-aws-profile> --cluster video-analytic-cluster --service video-analytic-agent --desired-count 1 --region us-east-1

# Redeploy after code changes (rebuild image, push to ECR, then)
aws ecs update-service --profile <your-aws-profile> --cluster video-analytic-cluster --service video-analytic-agent --force-new-deployment --region us-east-1
```

### Teardown / clean up

If you deployed the cloud stack and no longer need it, remove the provisioned resources to avoid ongoing charges. The deployment creates three CloudFormation stacks wired together by parameter values rather than cross-stack exports, so CloudFormation does not enforce a deletion order — delete them in the order below. Empty the S3 buckets first: the stacks declare no retention policies, so a bucket that still contains objects fails its stack deletion partway through and strands the remaining resources.

**1. Empty the three S3 buckets.** Substitute your account ID and AWS Region. The first two names derive from the `ProjectPrefix` parameter, which defaults to `video-object-locator`.

```bash
aws s3 rm s3://video-object-locator-<account-id>-<region> --recursive
aws s3 rm s3://video-object-locator-<account-id>-<region>-access-logs --recursive
aws s3 rm s3://video-analytic-alb-logs-<account-id>-<region> --recursive
```

All three buckets have versioning enabled, so the commands above leave noncurrent versions and delete markers behind and the buckets still refuse to delete. Remove those too (or use the **Empty** action in the S3 console, which handles versions and delete markers in one step). Run this for each bucket:

```bash
for BUCKET in \
  video-object-locator-<account-id>-<region> \
  video-object-locator-<account-id>-<region>-access-logs \
  video-analytic-alb-logs-<account-id>-<region>
do
  aws s3api delete-objects --bucket $BUCKET --delete "$(aws s3api \
    list-object-versions --bucket $BUCKET --output json \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}')"
  aws s3api delete-objects --bucket $BUCKET --delete "$(aws s3api \
    list-object-versions --bucket $BUCKET --output json \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}')"
done
```

**2. Delete the application stack** (ECS service and cluster, internal ALB and its access-logs bucket, the CloudFront distribution with its VPC origin and cache policies, the VPC and subnets, flow logs, and the KMS key). Expect 15–20 minutes — CloudFormation disables the CloudFront distribution and waits for that change to propagate before deleting it.

```bash
aws cloudformation delete-stack --stack-name video-analytic-agent-ecs
aws cloudformation wait stack-delete-complete --stack-name video-analytic-agent-ecs
```

**3. Delete the Cognito stack** (user pool, app client, hosted UI domain, and every user in the pool):

```bash
aws cloudformation delete-stack --stack-name video-analytic-cognito
```

**4. Delete the storage stack** (video bucket, its access-logs bucket, the Rekognition notification topic, and the Rekognition service role):

```bash
aws cloudformation delete-stack --stack-name video-object-locator-infra
```

**5. Delete the ECR repository.** The deployment script creates it outside CloudFormation, so stack deletion leaves it in place along with every image you pushed:

```bash
aws ecr delete-repository --repository-name video-analytic-agent --force
```

**6. Delete any Rekognition face collections.** These are created at runtime when a user uploads a reference photo, so they appear in no template and persist after everything else is gone. List them first — the collection IDs depend on how users exercised the application:

```bash
aws rekognition list-collections
aws rekognition delete-collection --collection-id <collection-id>
```

> **One resource outlives the teardown.** The KMS key that encrypts the CloudWatch log group is *scheduled* for deletion rather than deleted immediately. With the default 30-day pending window it continues to incur the customer-managed-key charge until the window expires. You can shorten the window to seven days when you schedule the deletion, but you cannot avoid the charge for the period already elapsed.

---

## Security Considerations for Production

This project ships as a **reference architecture**. The security posture is deliberate, not accidental: every Critical and High threat identified in [`docs/threat-model.md`](docs/threat-model.md) has been remediated, and the items that remain open are recorded there with an explicit disposition — accepted residual, or a production change documented here.

Read this section before promoting the pattern to a shared or production environment. Each item below states what the reference deployment does today, what to change, and why — so "adjust for production" is an instruction with a referent rather than a gesture.

> **A note on scope.** This is not a list of everything wrong with the sample. It is the set of decisions that were made for a deploy-and-discard reference deployment and that a real deployment should revisit. Where something has already been hardened, it says so, so you do not spend effort re-fixing it.

### Already hardened in the reference deployment

Worth knowing before you start, so you can skip these:

| Control | State |
|---|---|
| **Authentication fail-closed** | The app refuses to start on missing or partial Cognito configuration. Running without auth requires `ALLOW_INSECURE_LOCAL=true` **and** a loopback bind, simultaneously and deliberately. |
| **Public access to the load balancer** | The ALB is `Scheme: internal` in private subnets with no route to an internet gateway. CloudFront is the only public entry point. |
| **Viewer TLS** | HTTPS everywhere, using CloudFront's free `*.cloudfront.net` certificate. Plain HTTP is redirected. No ACM certificate or custom domain required. |
| **Account provisioning** | Admin-invite only by default (`AllowSelfSignup=false`). MFA (TOTP) is enforced. Password policy is 12 characters with complexity requirements. |
| **Session lifetime** | Token expiry is evaluated, with silent refresh 120 s ahead of expiry. A user disabled in Cognito loses access at the next refresh. |
| **Multi-tenant isolation** | Per-user S3 prefixes are **enforced**, not conventional. Every tool that accepts an S3 location validates ownership and fails closed. Document reads are confined to the caller's staging area. |
| **Upload limits** | Per-user staging quota (15 GB default) gives fault isolation between users. `maxUploadSize` is aligned to what the task's memory can actually hold. |
| **Agent output rendering** | Model output is not rendered as raw HTML, closing the browser-side exfiltration channel available to attacker-influenced content. |
| **Image scanning** | ECR repository is created with `scanOnPush=true`. |
| **Encryption and logging baseline** | KMS-encrypted log groups, ALB access logs, VPC flow logs, S3 public access blocked, TLS-only bucket policies. |

### 1. Transport security beyond the default

The reference deployment terminates viewer TLS at CloudFront with the default `*.cloudfront.net` certificate. That is genuinely secure for the browser hop and needs no domain purchase. Two residuals come with it:

**Viewer TLS minimum is TLSv1, not TLS 1.2.** CloudFront pins this when `CloudFrontDefaultCertificate: true`; the `MinimumProtocolVersion` property is rejected in that configuration. Raising it requires a custom domain:

1. Register or reuse a domain, and request an ACM certificate **in `us-east-1`** — CloudFront only accepts certificates from that region regardless of where your stack lives.
2. Add the domain as an `Alias` on the distribution and set `ViewerCertificate` to use the ACM certificate with `SslSupportMethod: sni-only` and `MinimumProtocolVersion: TLSv1.2_2021`.
3. Point DNS at the distribution.
4. Redeploy `cognito-stack.yaml` with `AppUrl=https://your-domain` so the Hosted UI callback matches. **The Hosted UI rejects any `redirect_uri` that is not an exact match**, so skipping this breaks login outright.

**CloudFront reaches the origin over HTTP.** That hop crosses the AWS network to a service-managed ENI inside your own VPC, never the public internet, which is why it is acceptable for a reference deployment. If your compliance position requires encryption in transit on every hop, you need the custom domain above **plus** a certificate on the ALB, because a publicly trusted certificate cannot be issued for an `*.elb.amazonaws.com` name.

### 2. Restrict the origin to your own distribution

ALB ingress is limited to the CloudFront managed prefix list (`com.amazonaws.global.cloudfront.origin-facing`). That permits *any* CloudFront distribution at the network layer, not only yours.

After the first deploy, CloudFront creates a service-managed security group named `CloudFront-VPCOrigins-Service-SG`. Replacing the prefix-list rule with a reference to that group narrows ingress to your distributions specifically. It cannot be done in a single CloudFormation pass because the group does not exist until the VPC origin is created — hence a post-deploy step:

```bash
# Find the service-managed group, then swap the ALB ingress rule to reference it
aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=CloudFront-VPCOrigins-Service-SG" \
  --query "SecurityGroups[].GroupId" --output text
```

### 3. Network placement of the compute

**Current state:** ECS tasks run in public subnets with `AssignPublicIp: ENABLED`. The ALB moved to private subnets; the tasks did not.

This is a cost decision. Fargate needs outbound reach to pull the image from ECR and to call Bedrock, Rekognition, Transcribe, S3, STS, CloudWatch Logs and Cognito — all public endpoints — and egress is required before any analysis begins. A public subnet is the free way to get it.

Nothing inbound reaches the tasks: the ECS security group admits only the ALB security group on tcp/8501, and egress is narrowed to tcp/443. The exposure is defence-in-depth — one security group misconfiguration from direct reachability, rather than two independent controls.

**Production change:** move tasks to private subnets and add either a NAT gateway (~$32/month per AZ plus data processing) or interface VPC endpoints for Bedrock, Bedrock Runtime, Rekognition, Transcribe, STS, CloudWatch Logs and ECR, plus the free S3 gateway endpoint (~$50-100/month for the set). This also closes the `0.0.0.0/0` egress rule on the ECS security group.

### 4. AWS WAF and edge protection

**Current state:** no WebACL. The surface is authenticated — reaching anything requires an admin-provisioned Cognito account, and self-signup is disabled — so this is not an anonymous public endpoint.

**Production change:** attach a WebACL to the **CloudFront distribution** (not the ALB, which is no longer internet-facing) with `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`, and a rate-based rule. Budget roughly $5/month baseline plus per-request charges. Enable Cognito Advanced Security Features (`UserPoolAddOns.AdvancedSecurityMode: ENFORCED`, a paid tier) for credential-stuffing and compromised-credential detection on the Hosted UI.

Note that WAF does **not** address the upload resource-exhaustion path — that is single-actor and authenticated, and is handled by the per-user quota in section 5.

### 5. Upload capacity and limits

Two settings, and the distinction between them matters:

**`UploadQuotaGb` (default 15) is the security control.** It bounds each user individually, so one user exhausting their allowance cannot deny service to anyone else. It holds regardless of how much disk you provision. Keyed per user, so multiple browser tabs share one allowance.

**`TaskEphemeralStorageGb` (default 100) is a capacity prerequisite, not a security control.** Size it as:

```
(max concurrent users per task × UploadQuotaGb) + 5 GB reserve
```

Under-provisioning degrades availability for legitimate users but does not reintroduce the security threat, because the quota still bounds each user. Fargate allows 21-200 GiB, so 200 is the ceiling — 13 users at the 15 GB default. Beyond that, scale out tasks.

**Raising the upload size limit requires two changes together.** `maxUploadSize` is 2000 MB because Streamlit buffers uploads entirely in memory and the task is allocated 4096 MB. Raising the limit alone recreates a promise the task cannot keep — a large upload OOM-kills the task and destroys every concurrent session on it. Raise task `Memory` in `deploy/ecs-fargate-stack.yaml` **first**, then raise `maxUploadSize` in **both** `.streamlit/config.toml` and the `Dockerfile` (see section 10).

**Larger media does not need a larger upload limit.** Bedrock Data Automation accepts files up to 10 GB. Place them in S3 directly under your own prefix and ask the agent to analyse the URI — this bypasses the in-memory buffer entirely:

```bash
aws s3 cp big-video.mp4 s3://<bucket>/<your-prefix>/videos/
```

**Not implemented, and worth adding for untrusted uploaders:** an extension allowlist with magic-byte verification (unrecognised files currently pass through as agent-readable documents), and malware scanning via GuardDuty Malware Protection for S3.

### 6. Authorization model

**Current state:** per-user isolation is enforced in application code. Every tool accepting an S3 location routes through an ownership validator that rejects other buckets, rejects traversal, requires the caller's own prefix, and fails closed when the caller cannot be identified. There is no role model — every authenticated user has identical capability.

**The maintenance rule that matters:** any new tool that touches S3 or the filesystem **must** call the ownership validator. Isolation holds only as long as every path routes through it; a tool added without it bypasses the boundary silently, and no test will catch that unless you extend `tests/test_s3_ownership.py`.

**Production change:** a Cognito Identity Pool issuing IAM session policies scoped to `${cognito-identity.amazonaws.com:sub}` moves enforcement from application code into IAM, so a forgotten validator call cannot leak data. This does not fit a single shared Fargate task serving multiple Streamlit sessions under one role, which is why the reference implementation does not use it. Add a role model (admin / analyst / read-only) if your users are not uniformly trusted.

### 7. Agent and prompt-injection posture

**What is contained.** Tool-boundary authorization bounds what a successful injection can reach to the caller's own data. Model output is not rendered as raw HTML, so attacker-influenced content cannot open a browser-side exfiltration channel.

**What is not, and cannot be.** An attacker who controls the content of a file your users upload — a video with spoken instructions, a PDF with an injected block, hidden text in a document — can influence the analysis the model produces. No perimeter control fixes this, because the payload arrives as legitimate user data. **Treat analysis output as advisory.** If you act on it for adversarial purposes such as content moderation, compliance review, or investigation, corroborate independently.

**Optional layer:** Bedrock Guardrails adds a prompt-attack filter on input and, if you call `ApplyGuardrail` explicitly on ingested transcripts and document text, a probabilistic check on tool results. Note that the model-invocation integration screens the *user turn*, not tool output, so tool-result screening is a code change rather than a configuration toggle. Do not overstate its effectiveness against an instruction buried in a long transcript.

### 8. Data protection — read this before pointing the app at real footage

This is the most substantive open item, and it is an obligation you own rather than a defect in the code.

The application runs Rekognition face detection and face search, maintains a **face collection**, and produces transcripts. Those are biometric identifiers and speech content — special-category data under GDPR Article 9, and within scope of BIPA and comparable state biometric statutes. The reference deployment implements **none** of the following:

- **Consent** — no mechanism for capturing or recording consent from people appearing in uploaded footage
- **Retention and deletion** — no S3 lifecycle policy, and the face collection persists across sessions independently of the S3 objects. Deleting uploads does not delete indexed faces.
- **Data-processing notice** — none
- **Residency** — the default model is a **cross-region inference profile** (`us.anthropic.claude-sonnet-4-5-...`), so prompt content may be processed in any of several US regions. This is material to a residency assessment. Use a single-region model ID if you need to pin processing to one region.

**Before production:** obtain a data-protection assessment, add an S3 lifecycle policy, document and implement a face-collection deletion procedure (`delete-collection` / `delete-faces`), and publish a processing notice. Bedrock Guardrails' PII detection and redaction can strip personal identifiers from transcripts and model output, and this is the clearest case for adopting it.

### 9. Logging, audit and monitoring

**Current state:** ALB access logs are enabled and record the true client IP, because CloudFront populates `X-Forwarded-For`. VPC flow logs are active to CloudWatch Logs. Container logs capture agent tool invocations. Authentication events are logged. Log groups are KMS-encrypted.

**Gaps and changes:**

| Area | Current | Production change |
|---|---|---|
| CloudFront logging | Not enabled | Enable standard logging — requests rejected at the edge are currently invisible |
| ECS log retention | 7 days | 90 days minimum; 365+ for regulated workloads |
| Flow log retention | 30 days (parameterised) | Match your incident-response window |
| Audit trail | Container logs only | Structured audit entries for uploads and agent queries, so "who analysed what" is queryable |
| CloudTrail | Assumed, not declared | Confirm an account-level trail with S3 and Bedrock **data events**. The stack deliberately does not declare one — it would collide with an organisation trail in a managed account. |
| Alerting | Container Insights metrics only | Alarms on ECS task failures, CloudFront and ALB 5xx rate, and Bedrock throttling |
| Data discovery | No Macie | Enable Macie on the upload bucket |

### 10. Streamlit configuration — a two-file gotcha

`Dockerfile` **regenerates** `/app/.streamlit/config.toml` after `COPY`. The repository's `.streamlit/config.toml` therefore governs **local development only**; the container runs on the values written in the Dockerfile.

**Any Streamlit setting you change must be changed in both files**, or it will silently not apply to the deployed application. This affects `maxUploadSize` (section 5) and XSRF below. The two files currently differ in several keys; consolidating them to one source is a known follow-up.

**XSRF protection is disabled** (`enableXsrfProtection = false`, `enableCORS = false`). The practical risk in this architecture is low: the application writes no cookies at all, and sessions are keyed to the Streamlit WebSocket connection, so a cross-origin request carries no credential to forge with. Both flags are the standard workaround for Streamlit uploads failing behind a reverse proxy.

**If you introduce cookie-based session handling, enable XSRF protection first** — it becomes load-bearing immediately. To enable it: set `enableXsrfProtection = true` in **both** files, rebuild, and **verify file upload end to end**, because enabling it behind a proxy is exactly where uploads tend to break.

### 11. IAM scoping

**Current state:** `sns:Publish` is scoped to the deployed topic ARN. `bedrock:InvokeModel` is scoped to this account and region for the inference profile, with the foundation-model region wildcarded — required because cross-region inference profiles route to foundation models in several regions, and the routing set is AWS-managed and can change. Foundation-model ARNs carry no account ID, so this cannot reach another account's resources.

Rekognition, Transcribe, the Bedrock Data Automation actions, `ListFoundationModels` and `sts:GetCallerIdentity` retain `Resource: '*'` because those APIs do not support resource-level ARNs per the IAM Service Authorization Reference. This is not fixable without breaking functionality.

**Production change:** if you pin to a single-region model ID rather than a cross-region profile, narrow the foundation-model ARN to that region. Add `aws:RequestedRegion` conditions to constrain the wildcard APIs to your deployment region.

### 12. Token verification for derived code

Three code paths decode the Cognito ID token without verifying its signature. In this deployment that is safe: every one receives the token directly from an SDK response or the Cognito token endpoint over TLS, and **no code path accepts a token from a request header, cookie or query parameter** — so an attacker cannot influence the value reaching the decoder.

**This does not travel.** If you reuse this code anywhere a token arrives from an untrusted source, verify the signature against the Cognito JWKS and check `iss`, `aud` and `exp`. Copying the unverified decode into a request-handling path produces a complete authentication bypass.

### 13. Cost governance

No Budgets alarm or anomaly detection is configured; a threshold hard-coded into a sample would be guesswork, and an alarm needs a notification target you have to supply.

The reference deployment bills continuously for a Fargate task, an ALB, a CloudFront distribution, a KMS key, and Container Insights, plus per-use Bedrock, Rekognition, Transcribe and BDA charges. A single long agentic session over a large video fans out across all four AI services.

**Add before production:** an `AWS::Budgets::Budget` with an SNS or email target, and Cost Anomaly Detection subscribed to the Bedrock service code — BDA and Bedrock invocations are the components that spike.

**To stop spending without tearing the stack down:**

```bash
aws ecs update-service --cluster video-analytic-cluster \
  --service video-analytic-agent --desired-count 0 --region us-east-1
```

### 14. Secrets management

All task-definition environment variables are non-secret configuration — region, bucket, model ID, topic and role ARNs, Cognito IDs, app URL. AWS authentication comes from the task role, and no credentials are inlined.

If you add anything genuinely sensitive — a third-party API key, a database password — put it in AWS Secrets Manager or SSM Parameter Store and reference it via the task definition's `secrets` block rather than `environment`.

### 15. Model lifecycle — this will break eventually

The default model ID is pinned (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) for reproducibility. **Providers retire model versions.** When a version is marked `LEGACY`, Bedrock refuses it for accounts that have not invoked it recently, and the failure appears at the first agent invocation as a `ResourceNotFoundException` — not at deploy time, so both stacks deploy green and the app breaks on first use.

This has already happened once to this project with Claude Sonnet 4. To find a current version:

```bash
aws bedrock list-foundation-models --by-provider anthropic \
  --query "modelSummaries[?modelLifecycle.status=='ACTIVE'].modelId" --output table
```

Then set `MODEL_ID` (env var or the `ModelId` stack parameter). If you use a `us.`-prefixed cross-region profile, confirm the IAM policy still covers the foundation-model regions it routes to — see section 11.

### ASH suppressions summary

`.ash/.ash.yaml` suppresses the findings below, each scoped to a specific rule and path with a `reason`. Inline `cfn_nag` suppressions in `deploy/ecs-fargate-stack.yaml` carry their rationale in `Metadata` alongside the resource.

| Rule | Path | Why suppressed |
|---|---|---|
| `SECRET-SECRET-KEYWORD` | `deploy/cognito-stack.yaml` | Cognito `GenerateSecret: false` flag matched as a secret name — false positive |
| `CKV_AWS_18` / `CKV_AWS_21` | both templates | Access-log destination buckets must not log to themselves (recursion) and do not need versioning (append-only) |
| `CKV_AWS_2` / `CKV_AWS_103` | `deploy/ecs-fargate-stack.yaml` | The HTTP listener is internal-only, reachable solely from the CloudFront VPC origin ENI. Viewer TLS is terminated at CloudFront. A publicly trusted certificate cannot be issued for `*.elb.amazonaws.com`, so ALB-side TLS requires a custom domain — see section 1 |
| `cfn_nag W70` | distribution (inline) | `MinimumProtocolVersion` cannot be set while `CloudFrontDefaultCertificate: true` — see section 1 |
| `cfn_nag W10` | distribution (inline) | CloudFront access logging not enabled; request-level coverage exists via ALB access logs with true client IP. WAF also not attached — see sections 4 and 9 |
| `cfn_nag W5` | ECS security group (inline) | `0.0.0.0/0` egress on tcp/443 is required for public AWS API endpoints; closing it needs VPC endpoints — see section 3 |
| `cfn_nag W11` / `AwsSolutions-IAM5` | `deploy/ecs-fargate-stack.yaml` | Rekognition, Transcribe, BDA, `ListFoundationModels` and `sts:GetCallerIdentity` do not support resource-level ARNs. `sns:Publish` and `bedrock:InvokeModel` **are** scoped — see section 11 |
| `AwsSolutions-COG8` | `deploy/cognito-stack.yaml` | Cognito Advanced Security is a paid tier; cost decision — see section 4 |
| `AwsSolutions-IAM4` | `deploy/ecs-fargate-stack.yaml` | `TaskExecutionRole` uses the AWS-recommended `AmazonECSTaskExecutionRolePolicy` baseline |
| `AwsSolutions-S1` | `deploy/ecs-fargate-stack.yaml` | `ALBAccessLogsBucket` is itself the log destination; self-logging would recurse |
| `AwsSolutions-ECS2` | `deploy/ecs-fargate-stack.yaml` | Task-definition environment variables are all non-secret configuration — see section 14 |
| `.venv` path ignore | — | Local virtualenv is not delivered code; container dependencies come from `requirements.txt` |

For the full analysis behind these decisions, including what was tried and rejected, see [`docs/threat-model.md`](docs/threat-model.md).
