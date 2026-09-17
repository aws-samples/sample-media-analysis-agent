# Security Scan Exceptions

Requested dispositions for static-analysis findings that remain open on this
package after remediation. Each entry states the finding, the requested
disposition, the compensating control where one exists, the residual risk being
accepted, and the evidence a reviewer can check in this repository.

**Scope.** The slim version of the Video Analytic Agent: the CloudFront-fronted
ECS Fargate deployment, with local deployment, the highlight feature, and the
generated-diagram feature omitted.

**Templates assessed**

- `deploy/ecs-fargate-stack.yaml` — VPC, ALB, ECS, CloudFront, KMS, logging
- `cfn-video-locator-infra.yaml` — S3 storage, SNS, Rekognition service role
- `deploy/cognito-stack.yaml` — user pool and hosted authentication

**A note on classification.** None of the entries below is filed as a false
positive. Every check listed detects accurately what it claims to detect. What
is in dispute is whether the control applies to the resource, or whether the
prescribed remediation is available without a net loss of security posture.
Where a real gap exists, it is stated as such.

---

## Summary

| Finding | Resource | Requested disposition |
|---|---|---|
| `CKV_AWS_18` | `VideoBucketAccessLogs` | Not applicable — control cannot exist on this resource |
| `CKV_AWS_18` | `ALBAccessLogsBucket` | Not applicable — control cannot exist on this resource |
| `CKV_AWS_2` | `ALBListener` | Risk accepted — compensating control |
| `CKV_AWS_103` | `ALBListener` | Risk accepted — compensating control |
| `CKV_AWS_174` | `CloudFrontDistribution` | Risk accepted — remediation unavailable in this configuration |
| `CKV_AWS_86` | `CloudFrontDistribution` | Risk accepted — compensating control, real residual gap |

`CKV_AWS_2`, `CKV_AWS_103`, and `CKV_AWS_174` share a single root cause: the
deployment intentionally requires no custom domain. Adopting a custom domain
with an ACM certificate resolves all three and is recorded as the remediation
path in each entry.

---

## CKV_AWS_18 — Ensure the S3 bucket has access logging enabled

**Resources**

- `VideoBucketAccessLogs` — `cfn-video-locator-infra.yaml:14`
- `ALBAccessLogsBucket` — `deploy/ecs-fargate-stack.yaml:500`

**Requested disposition:** Not applicable — the control cannot exist on this
resource type.

**Accuracy of the finding.** Correct. Neither bucket has access logging
configured.

**Why the control does not apply.** Both resources are log-destination buckets
rather than application data stores. `VideoBucketAccessLogs` receives S3 server
access logs from `VideoBucket`; `ALBAccessLogsBucket` receives ALB access logs.
Enabling access logging on a bucket that receives access logs creates a
recursive loop, because each log object written generates a further log entry
describing that write. AWS guidance is not to use a source bucket as its own
logging target. Directing these buckets to a third bucket does not resolve the
finding either — it relocates it, because the third bucket then becomes an
unlogged log destination. The chain must terminate somewhere, and it terminates
here.

**Where the control does exist.** Every bucket holding application or request
data has logging enabled:

- `VideoBucket` logs to `VideoBucketAccessLogs` under the `video-bucket-access/`
  prefix — `cfn-video-locator-infra.yaml:77-79`
- The ALB writes access logs to `ALBAccessLogsBucket` via
  `access_logs.s3.enabled=true` — `deploy/ecs-fargate-stack.yaml:661`

**Residual risk accepted.** Object-level access to the two log buckets is not
recorded by S3 server access logging. Both buckets block all public access, use
`BucketOwnerEnforced` object ownership (`cfn-video-locator-infra.yaml:59`,
`deploy/ecs-fargate-stack.yaml:546`), apply server-side encryption, carry a
bucket policy denying requests over non-TLS transport, and expire objects after
30 days. Where object-level audit of the log buckets is required, CloudTrail S3
data events are the appropriate mechanism, as they do not depend on a bucket
logging to itself.

**Revalidation.** None proposed. The condition is structural and will not change
while these buckets remain log destinations.

---

## CKV_AWS_2 and CKV_AWS_103 — ALB listener protocol and TLS policy

**Resource:** `ALBListener` — `deploy/ecs-fargate-stack.yaml:700`

**Requested disposition:** Risk accepted — compensating control in place.

**Accuracy of the findings.** Correct. The listener serves HTTP on port 80 and
therefore has no TLS policy to evaluate.

**Compensating control.** Client-facing transport is encrypted, terminated at
CloudFront rather than at the load balancer. The distribution sets
`ViewerProtocolPolicy: redirect-to-https`
(`deploy/ecs-fargate-stack.yaml:851`), so a browser arriving over plain HTTP is
redirected. The plaintext listener is not reachable from the internet:

- The ALB is `Scheme: internal` — `deploy/ecs-fargate-stack.yaml:644`
- It is placed in `PrivateSubnet1` and `PrivateSubnet2`, whose route table
  (`PrivateRouteTable`, `deploy/ecs-fargate-stack.yaml:325`) has no
  `0.0.0.0/0` route. The only such route in the template belongs to the public
  route table — `deploy/ecs-fargate-stack.yaml:262`
- The ALB security group admits inbound TCP 80 only from
  `SourcePrefixListId: CloudFrontPrefixListId`, the CloudFront origin-facing
  managed prefix list — `deploy/ecs-fargate-stack.yaml:432`

The only sender able to reach the listener is CloudFront's own infrastructure,
over the AWS network, via the VPC origin.

**Why the prescribed remediation is not applied.** An HTTPS listener requires a
certificate valid for the name the client connects to. This load balancer is
addressed only by its AWS-assigned `*.elb.amazonaws.com` name, for which a
publicly trusted certificate cannot be issued. ALB-side TLS therefore requires
acquiring a custom domain and provisioning an ACM certificate — a change to the
deployment's prerequisites rather than a template setting. A self-signed or
private certificate would not satisfy CloudFront's origin certificate
validation.

**Residual risk accepted.** Traffic between the CloudFront VPC origin and the
ALB traverses the AWS network unencrypted. Exposure is bounded by the network
controls above; no path exists from the public internet to this listener.

**Revalidation.** Revisit if a custom domain and ACM certificate are adopted, at
which point both checks can be satisfied directly. This justification is void if
the ALB is ever changed to `internet-facing`, or if its security group is
widened beyond the CloudFront prefix list.

---

## CKV_AWS_174 — CloudFront viewer certificate minimum TLS version

**Resource:** `CloudFrontDistribution` — `deploy/ecs-fargate-stack.yaml:798`

**Requested disposition:** Risk accepted — remediation unavailable in this
configuration.

**Accuracy of the finding.** Correct. The effective viewer security policy is
`TLSv1`.

**Why the setting cannot be applied.** The distribution uses the default
CloudFront certificate, `CloudFrontDefaultCertificate: true`
(`deploy/ecs-fargate-stack.yaml:871`), which is what permits public HTTPS access
with no custom domain. AWS documentation states that when a distribution uses
the CloudFront domain name, CloudFront automatically sets the security policy to
`TLSv1` regardless of the value supplied for `MinimumProtocolVersion` — the
property is accepted and silently ignored. Setting it would cause this check to
pass while changing nothing about negotiated TLS. That is a worse outcome than an
accepted finding, because it records a control that does not exist.

**What is in place.** Viewer traffic is encrypted from the browser to CloudFront,
and `ViewerProtocolPolicy: redirect-to-https` prevents plaintext viewer
sessions. Only the minimum negotiated protocol floor is lower than the rule
requires.

**Residual risk accepted.** A client supporting only TLS 1.0 or 1.1 could
negotiate a session at that version. Current browsers do not do so by default,
so practical exposure is limited to deliberately downgraded clients.

**Remediation path.** Raising the floor to `TLSv1.2_2021` requires a custom
domain and an ACM certificate in `us-east-1`, replacing the default certificate.
That change also resolves `CKV_AWS_2` and `CKV_AWS_103`.

**Revalidation.** At the next architecture review, or when a custom domain is
adopted.

---

## CKV_AWS_86 — CloudFront access logging

**Resource:** `CloudFrontDistribution` — `deploy/ecs-fargate-stack.yaml:798`

**Requested disposition:** Risk accepted — compensating control in place, with a
real residual gap.

**Accuracy of the finding.** Correct, and this is the one entry in this document
describing a genuine control gap rather than an inapplicable check. No
CloudFront-side logging is configured in either available form: the distribution
has no legacy `Logging` block, and the template contains no
`AWS::Logs::DeliverySource`, `AWS::Logs::DeliveryDestination`, or
`AWS::Logs::Delivery` resources for standard logging v2.

**Compensating control.** ALB access logging is enabled and delivered to a
dedicated S3 bucket (`deploy/ecs-fargate-stack.yaml:661`) with server-side
encryption, all public access blocked, a TLS-only bucket policy, versioning, and
a 30-day lifecycle. CloudFront is the only public entry point — the ALB is
`Scheme: internal` in private subnets with no internet gateway route — so every
request that reaches the application is recorded at the ALB. CloudFront
populates `X-Forwarded-For`, so those records carry the originating client
address rather than the CloudFront edge address.

**Residual risk accepted.** Requests that CloudFront rejects before forwarding to
the origin are not logged anywhere. Exposure is bounded by the access model: the
application requires an administrator-provisioned Amazon Cognito account, public
self-registration is disabled by default via the `AllowSelfSignup` parameter,
and MFA is enforced (`deploy/cognito-stack.yaml:51`). There is no anonymous
functionality behind the distribution.

**Why the prescribed remediation is not applied.** This check inspects the
distribution's legacy `Logging` property. Per AWS documentation, legacy
CloudFront logging requires the destination bucket to permit ACL-based grants to
the CloudFront log-delivery account, and a bucket using `BucketOwnerEnforced`
cannot receive those logs. Both S3 log buckets in this deployment use
`BucketOwnerEnforced` with ACLs disabled. Satisfying this check would require
provisioning a bucket with ACLs re-enabled — weakening an existing control to
satisfy a check written around the older logging mechanism.

**Planned improvement, tracked separately.** CloudFront standard logging v2
delivers access logs without ACLs, via `AWS::Logs::DeliverySource`,
`AWS::Logs::DeliveryDestination`, and `AWS::Logs::Delivery`. It closes the
residual gap described above but does not satisfy this check, which inspects only
the legacy property. Adoption is gated on three prerequisites:

1. The delivery source for a CloudFront distribution must be created in
   `us-east-1`, which constrains a single-region stack.
2. The destination bucket policy requires a statement that does not impose the
   `s3:x-amz-acl` condition, or vended-log delivery is denied. The existing
   `ALBAccessLogsBucketPolicy` statement for `delivery.logs.amazonaws.com`
   carries that condition for ALB log delivery.
3. CloudFront standard logs require the Pro plan or pay-as-you-go pricing.

**Revalidation.** At the next architecture review; earlier if the distribution
begins serving unauthenticated content, or if an AWS WAF web ACL is attached, as
WAF logging would supply the edge-side visibility currently missing.

---

## Verification notes

Line references above were confirmed against the files in this directory at the
time of writing. Two claims rest on AWS documentation rather than this
repository: that `MinimumProtocolVersion` is ignored when the default CloudFront
certificate is used, and that legacy CloudFront logging cannot write to a
`BucketOwnerEnforced` bucket.

One claim could not be verified from this repository: whether the target AWS
account is on a CloudFront plan that permits standard logs. That affects only the
`CKV_AWS_86` improvement path, not the requested disposition.

The assertion that every request reaching the application is captured in ALB
access logs depends on the ALB remaining internal with its security group scoped
to the CloudFront prefix list. Both are cited above with line references so a
reviewer can confirm them directly.
