#!/bin/bash
#
# Deploy Video Analytic Agent to ECS Fargate + ALB
#
# Usage:
#   ./deploy/deploy-ecs.sh [--profile PROFILE] [--region REGION] \
#                          [--allow-self-signup {true|false}]
#
# Options:
#   --profile              AWS CLI profile to use.
#   --region               AWS region (default: us-east-1 or $AWS_REGION).
#   --allow-self-signup    Whether to enable public self-signup on the
#                          Cognito user pool and show the Sign Up tab in
#                          the app UI. Default: false (admin-invite only;
#                          the operator creates users via the AWS console,
#                          CLI, or SDK). See T-06 in docs/threat-model.md.
#

set -e

STACK_NAME="video-analytic-agent-ecs"
INFRA_STACK_NAME="video-object-locator-infra"
REGION="${AWS_REGION:-us-east-1}"
IMAGE_TAG="latest"
REPO_NAME="video-analytic-agent"
PROFILE_FLAG=""
# T-06 default: admin-invite only. Set --allow-self-signup true to opt in.
ALLOW_SELF_SIGNUP="false"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --profile) PROFILE_FLAG="--profile $2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --allow-self-signup)
            ALLOW_SELF_SIGNUP="$2"
            if [[ "$ALLOW_SELF_SIGNUP" != "true" && "$ALLOW_SELF_SIGNUP" != "false" ]]; then
                echo "ERROR: --allow-self-signup must be 'true' or 'false' (got '$ALLOW_SELF_SIGNUP')"
                exit 1
            fi
            shift 2
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Preflight: local container runtime ────────────────────────────────────
#
# Checked before any AWS resource is created, so a missing runtime fails the
# run before CloudFormation or ECR are touched.
#
# The Dockerfile base image comes from ECR Public
# (public.ecr.aws/amazonlinux/amazonlinux), which allows anonymous pulls, so
# no registry credential helper is required here. The ECR login further down
# is for PUSHING the built image to this account's private repository.

if command -v finch >/dev/null 2>&1; then
    CONTAINER_CMD="finch"
elif command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD="docker"
else
    echo "ERROR: Neither finch nor docker found."
    exit 1
fi

echo "============================================"
echo "  Video Analytic Agent — ECS Fargate Deploy"
echo "============================================"
echo ""

# ── 1. Verify authentication ─────────────────────────────────────────────

echo "Checking AWS authentication..."
ACCOUNT_ID=$(aws sts get-caller-identity $PROFILE_FLAG --query "Account" --output text --region "$REGION")
if [ -z "$ACCOUNT_ID" ]; then
    echo "ERROR: Not authenticated."
    exit 1
fi
echo "  ✅ Account: $ACCOUNT_ID | Region: $REGION"

# ── 2. Deploy base infrastructure and read its S3 output ────────────────

# ECS depends on the storage/notification stack. Deploying it here keeps this
# package turnkey without requiring a separate local-development bootstrap.
echo ""
echo "Ensuring base infrastructure stack exists..."
aws cloudformation deploy $PROFILE_FLAG \
    --template-file cfn-video-locator-infra.yaml \
    --stack-name "$INFRA_STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION" \
    --no-fail-on-empty-changeset
echo "  ✅ Base infrastructure stack ready."

echo "Reading S3 bucket output..."
S3_BUCKET=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$INFRA_STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" \
    --output text 2>/dev/null || echo "")

if [ -z "$S3_BUCKET" ] || [ "$S3_BUCKET" = "None" ]; then
    echo "ERROR: Base infrastructure stack did not return BucketName."
    exit 1
fi
echo "  ✅ S3 Bucket: $S3_BUCKET"

# NOTE: BDA S3 bucket policy is now managed by CloudFormation (see
# VideoBucketPolicy in cfn-video-locator-infra.yaml). No manual
# put-bucket-policy call is needed — every infra stack deploy re-asserts
# the policy so it does not drift. If you need to inspect or verify it:
#   aws s3api get-bucket-policy --bucket $S3_BUCKET --profile <profile>

# ── 3. Create ECR repository ─────────────────────────────────────────────

echo ""
echo "Ensuring ECR repository exists..."
aws ecr describe-repositories $PROFILE_FLAG --repository-names "$REPO_NAME" --region "$REGION" >/dev/null 2>&1 || \
    aws ecr create-repository $PROFILE_FLAG \
        --repository-name "$REPO_NAME" \
        --image-scanning-configuration scanOnPush=true \
        --region "$REGION" >/dev/null
echo "  ✅ ECR repository ready."

# ── 4. Build and push Docker image ───────────────────────────────────────

ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO_NAME:$IMAGE_TAG"
echo ""
echo "Building and pushing Docker image..."
echo "  Using: $CONTAINER_CMD"

# Login to destination ECR
aws ecr get-login-password $PROFILE_FLAG --region "$REGION" | \
    $CONTAINER_CMD login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

# Build (amd64 for Fargate)
$CONTAINER_CMD build --platform linux/amd64 -t "$REPO_NAME:$IMAGE_TAG" .

# Tag and push
$CONTAINER_CMD tag "$REPO_NAME:$IMAGE_TAG" "$ECR_URI"
$CONTAINER_CMD push "$ECR_URI"
echo "  ✅ Image pushed: $ECR_URI"

echo ""
echo "Looking up SNS topic and Rekognition role..."
SNS_TOPIC_ARN=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$INFRA_STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='SNSTopicArn'].OutputValue" \
    --output text 2>/dev/null || echo "none")

REKOGNITION_ROLE_ARN=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$INFRA_STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='RekognitionRoleArn'].OutputValue" \
    --output text 2>/dev/null || echo "none")

if [[ -z "$SNS_TOPIC_ARN" || "$SNS_TOPIC_ARN" == "None" || "$SNS_TOPIC_ARN" == "none" || \
      -z "$REKOGNITION_ROLE_ARN" || "$REKOGNITION_ROLE_ARN" == "None" || "$REKOGNITION_ROLE_ARN" == "none" ]]; then
    echo "ERROR: Base infrastructure stack outputs are incomplete."
    exit 1
fi

echo "  ✅ SNS Topic: $SNS_TOPIC_ARN"
echo "  ✅ Rekognition Role: $REKOGNITION_ROLE_ARN"

# T-05: the ALB is Scheme=internal in private subnets, so the only
# ingress source that can reach it is the CloudFront managed prefix
# list. Those IDs are region-specific, so resolve it at deploy time.
#
# This replaces a previous `ALLOWED_CIDR="$(curl ifconfig.me)/32"` rule,
# which was broken by construction: a deployer's public /32 can never
# match traffic arriving at an internal ALB, so nothing could reach the
# application at all.
echo "Resolving CloudFront managed prefix list for $REGION..."
CF_PREFIX_LIST_ID=$(aws ec2 describe-managed-prefix-lists $PROFILE_FLAG \
    --region "$REGION" \
    --filters "Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing" \
    --query "PrefixLists[0].PrefixListId" \
    --output text 2>/dev/null || echo "")

if [[ -z "$CF_PREFIX_LIST_ID" || "$CF_PREFIX_LIST_ID" == "None" ]]; then
    echo "  ❌ Could not resolve the CloudFront origin-facing managed prefix list."
    echo "     Without it the ALB security group has no valid ingress source and"
    echo "     the application would deploy unreachable. Aborting rather than"
    echo "     shipping a broken stack."
    exit 1
fi
echo "  ✅ CloudFront prefix list: $CF_PREFIX_LIST_ID"

# ── 5. Deploy CloudFormation stack ───────────────────────────────────────

echo ""
# Creating the CloudFront distribution and its VPC origin dominates this
# step: the VPC origin can take up to 15 minutes to reach Deployed and
# the distribution another 5-15 to propagate.
# ecs-fargate-stack.yaml exceeds CloudFormation's 51,200-byte limit for an
# inline template body, so the CLI must stage it in S3 first. --s3-bucket
# makes it upload the template and pass a URL instead of the body. The video
# bucket is reused as the staging location because it already exists at this
# point in the run; its one-day lifecycle is harmless for a transient object.
echo "Deploying ECS + internal ALB + CloudFront stack (this takes ~15-20 minutes)..."
aws cloudformation deploy $PROFILE_FLAG \
    --template-file deploy/ecs-fargate-stack.yaml \
    --stack-name "$STACK_NAME" \
    --s3-bucket "$S3_BUCKET" \
    --s3-prefix cfn-templates \
    --parameter-overrides \
        ImageUri="$ECR_URI" \
        S3Bucket="$S3_BUCKET" \
        AwsRegion="$REGION" \
        SnsTopicArn="$SNS_TOPIC_ARN" \
        RekognitionRoleArn="$REKOGNITION_ROLE_ARN" \
        CloudFrontPrefixListId="$CF_PREFIX_LIST_ID" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION" \
    --no-fail-on-empty-changeset

echo "  ✅ Stack deployed."

# ── 6. Get the public URL ─────────────────────────────────────────────────
#
# The CloudFront domain is the application's only reachable address. The
# internal ALB DNS name is read too, but only for troubleshooting output.

echo ""
CF_DOMAIN=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" \
    --output text)

ALB_DNS=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerDNS'].OutputValue" \
    --output text)

if [[ -z "$CF_DOMAIN" || "$CF_DOMAIN" == "None" ]]; then
    echo "  ❌ Could not read the CloudFront domain from the stack outputs."
    echo "     Aborting before Cognito is configured with a wrong callback URL."
    exit 1
fi

# HTTPS here is load-bearing, not cosmetic. auth.py selects its auth mode
# from this scheme: an https:// AppUrl makes it use the Cognito Hosted UI
# OAuth code flow, so the application never handles the user's password.
# An http:// value would silently fall back to USER_PASSWORD_AUTH and
# collect credentials in a Streamlit form.
APP_URL="https://${CF_DOMAIN}"
echo "  ✅ CloudFront domain: $CF_DOMAIN"
echo "  ✅ App URL: $APP_URL"

# ── 7. Deploy Cognito User Pool ──────────────────────────────────────────

COGNITO_STACK_NAME="video-analytic-cognito"
DOMAIN_PREFIX="video-analytic-${ACCOUNT_ID}"

echo ""
# AppUrl must be the real CloudFront URL, not a placeholder. It becomes
# the user pool client's CallbackURLs/LogoutURLs entry, and the Hosted UI
# rejects any redirect_uri that is not an exact match.
#
# This previously passed http://localhost:8501 and never corrected it.
# That was survivable only because the old http:// ALB AppUrl sent the
# app down the direct-API auth path, where no redirect_uri is ever
# exchanged, leaving the wrong callback unused. Now that the app URL is
# HTTPS and the Hosted UI is active, the placeholder would break login
# outright.
echo "Deploying Cognito User Pool (AllowSelfSignup=$ALLOW_SELF_SIGNUP)..."
aws cloudformation deploy $PROFILE_FLAG \
    --template-file deploy/cognito-stack.yaml \
    --stack-name "$COGNITO_STACK_NAME" \
    --parameter-overrides \
        AppUrl="$APP_URL" \
        DomainPrefix="$DOMAIN_PREFIX" \
        AllowSelfSignup="$ALLOW_SELF_SIGNUP" \
    --region "$REGION" \
    --no-fail-on-empty-changeset

USER_POOL_ID=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$COGNITO_STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
    --output text)
CLIENT_ID=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$COGNITO_STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
    --output text)
COGNITO_DOMAIN=$(aws cloudformation describe-stacks $PROFILE_FLAG \
    --stack-name "$COGNITO_STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoDomain'].OutputValue" \
    --output text)

echo "  ✅ Cognito User Pool: $USER_POOL_ID"
echo "  ✅ Client ID: $CLIENT_ID"
echo "  ✅ Domain: $COGNITO_DOMAIN"
echo "  ✅ Self-signup: $ALLOW_SELF_SIGNUP"

# T-06: the previous version of this script overrode
# AllowAdminCreateUserOnly=false via aws cognito-idp update-user-pool
# right here, unconditionally forcing open self-signup regardless of
# what the CloudFormation template said. That override has been removed.
# The authoritative source is now the AllowSelfSignup parameter, passed
# consistently to both the Cognito and ECS stacks above.

# ── 8. Update ECS stack with Cognito params (triggers redeploy) ──────────

# APP_URL was computed in step 6 from the CloudFront domain.

echo ""
echo "Updating ECS stack with Cognito configuration..."
aws cloudformation deploy $PROFILE_FLAG \
    --template-file deploy/ecs-fargate-stack.yaml \
    --stack-name "$STACK_NAME" \
    --s3-bucket "$S3_BUCKET" \
    --s3-prefix cfn-templates \
    --parameter-overrides \
        ImageUri="$ECR_URI" \
        S3Bucket="$S3_BUCKET" \
        AwsRegion="$REGION" \
        SnsTopicArn="$SNS_TOPIC_ARN" \
        RekognitionRoleArn="$REKOGNITION_ROLE_ARN" \
        CloudFrontPrefixListId="$CF_PREFIX_LIST_ID" \
        CognitoUserPoolId="$USER_POOL_ID" \
        CognitoClientId="$CLIENT_ID" \
        CognitoDomain="$COGNITO_DOMAIN" \
        AllowSelfSignup="$ALLOW_SELF_SIGNUP" \
        AppUrl="$APP_URL" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION" \
    --no-fail-on-empty-changeset

echo "  ✅ ECS stack updated with Cognito auth."

echo "============================================"
echo "  Deployment Complete!"
echo "============================================"
echo ""
echo "  🌐 Public URL: $APP_URL"
echo "  🔒 Transport: HTTPS, terminated at CloudFront (*.cloudfront.net cert)"
echo "  🛡️  Origin:    internal ALB in private subnets — no public IP."
echo "                Ingress restricted to CloudFront ($CF_PREFIX_LIST_ID)."
echo "                Internal ALB DNS (VPC-only, troubleshooting): $ALB_DNS"

if [[ "$ALLOW_SELF_SIGNUP" == "true" ]]; then
    echo "  🔐 Auth: Cognito Hosted UI (public self-signup enabled)"
    echo ""
    echo "  CloudFront needs a few minutes to finish propagating, and the"
    echo "  service another 2-3 minutes to pass health checks."
    echo "  Open the URL, sign up with your email, verify with the code,"
    echo "  configure MFA, then sign in."
else
    echo "  🔐 Auth: Cognito Hosted UI (admin-invite only — see below)"
    echo ""
    echo "  CloudFront needs a few minutes to finish propagating, and the"
    echo "  service another 2-3 minutes to pass health checks."
    echo ""
    echo "  To invite a user (creates Cognito user, emails temp password):"
    echo "    aws cognito-idp admin-create-user $PROFILE_FLAG \\"
    echo "      --user-pool-id $USER_POOL_ID \\"
    echo "      --region $REGION \\"
    echo "      --username user@example.com \\"
    echo "      --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \\"
    echo "      --desired-delivery-mediums EMAIL"
    echo ""
    echo "  The invited user signs in with the temp password, sets a permanent"
    echo "  password, and configures MFA on first sign-in."
fi
echo ""
echo "  To update: rebuild image, push to ECR, then:"
echo "    aws ecs update-service $PROFILE_FLAG --cluster video-analytic-cluster --service video-analytic-agent --force-new-deployment --region $REGION"
echo ""
echo "  To stop (save cost):"
echo "    aws ecs update-service $PROFILE_FLAG --cluster video-analytic-cluster --service video-analytic-agent --desired-count 0 --region $REGION"
echo ""
echo "  To restart:"
echo "    aws ecs update-service $PROFILE_FLAG --cluster video-analytic-cluster --service video-analytic-agent --desired-count 1 --region $REGION"
echo ""
