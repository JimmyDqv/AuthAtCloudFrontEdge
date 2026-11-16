# Authorization with Lambda@Edge

Add Microsoft Entra ID authentication to content served through AWS CloudFront using Lambda@Edge and OpenID Connect (OIDC).

This application implements the OAuth 2.0 Authorization Code flow with PKCE, allowing CloudFront to enforce authentication before serving content from protected origins.

It is particularly useful for protecting static websites hosted in S3, such as VitePress documentation, without requiring a dedicated authentication proxy or Amazon Cognito.

**Note:** This application must be deployed in `us-east-1`, as required by Lambda@Edge.

## Architecture

The application uses three Lambda@Edge functions:

- **Auth Handler:** Validates signed session cookies and redirects unauthenticated users to Microsoft Entra ID.
- **Callback Handler:** Processes the OAuth authorization callback, exchanges the authorization code for tokens, validates the ID token and establishes an authenticated session.
- **ReWrite Handler:** Optionally rewrites static website requests to their corresponding `index.html` files.

Authentication is enforced at the CloudFront viewer-request stage, before requests are served from the cache or forwarded to an origin.

### Authentication flow

1. A user requests protected content through CloudFront.
2. The Auth Handler checks for a valid signed session cookie.
3. If the session is missing or expired, the user is redirected to Microsoft Entra ID.
4. The user authenticates using their Microsoft account, subject to applicable Entra Conditional Access policies.
5. Entra redirects the user to `/auth/callback` with an authorization code.
6. The Callback Handler exchanges the code using PKCE and the configured client credentials.
7. The returned ID token is validated against Entra's signing keys, issuer, audience, expiry and nonce.
8. A signed, short-lived session cookie is issued, and the user is redirected to their original destination.
9. Subsequent requests are permitted while the session remains valid.

The application does not retain Entra access or refresh tokens in browser cookies. Authentication sessions expire after one hour, after which the user is redirected through Entra authentication again.

## Publishing

The application is distributed through the AWS Serverless Application Repository.

[View Atom-Auth-at-Edge in the Serverless Application Repository](https://us-east-1.console.aws.amazon.com/serverlessrepo/home?region=us-east-1#/published-applications/arn:aws:serverlessrepo:us-east-1:539182614893:applications~Atom-Auth-at-Edge)

### Publishing a new version

Increment `SemanticVersion` in `template.yaml` before publishing a new application version.

For example:

```yaml
Metadata:
  AWS::ServerlessRepo::Application:
    Name: Atom-Auth-at-Edge
    SemanticVersion: 2.1.0
```

Build, package and publish the application:

```bash
uv run sam build

uv run sam package --output-template-file .aws-sam/packaged.yaml --s3-bucket ada-mode-atom
uv run sam publish --template .aws-sam/packaged.yaml --region us-east-1
```

Publishing creates a new version in the Serverless Application Repository. **It does not automatically update existing deployed instances or their CloudFront integrations.**

### Updating an existing deployment

To update an existing instance:

1. Publish the new application version using the commands above.
2. Open the AWS Serverless Application Repository.
3. Select the newly published application version.
4. Deploy it using the same application name and stack configuration as the existing instance, following the repository's update workflow.
5. Supply any new or updated application parameters.
6. Once deployment completes, obtain the newly published numbered Lambda version ARNs.
7. Update the relevant CloudFront behaviours to reference the new Lambda versions.

Existing CloudFront associations continue using their previously configured Lambda versions until they are explicitly updated.

## Requirements

- An AWS CloudFront distribution.
- A Microsoft Entra tenant.
- A Microsoft Entra app registration configured for OpenID Connect.
- An Entra application Client ID and client secret.
- A public HTTPS hostname associated with the CloudFront distribution.
- Permissions to deploy Lambda, IAM and SSM resources in `us-east-1`.
- Permissions to update the CloudFront distribution.

### Microsoft Entra configuration

Configure an application registration in Microsoft Entra ID with:

**Platform:** Web

**Redirect URI:**

```text
https://example.com/auth/callback
```

The redirect URI must match the public CloudFront hostname and the configured callback path.

Create a client secret under **Certificates & secrets**. Use the secret's value when configuring the deployed application.

The application uses the tenant-specific OpenID Connect discovery endpoint:

```text
https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration
```

The recommended scopes are:

```text
openid profile email
```

Implicit grant is not required.

For internal applications, access can be restricted through Enterprise Application user/group assignments and Microsoft Entra Conditional Access policies.

## Deployment

Deploy an instance of the application from the [AWS Serverless Application Repository](https://us-east-1.console.aws.amazon.com/serverlessrepo/home?region=us-east-1#/published-applications/arn:aws:serverlessrepo:us-east-1:539182614893:applications~Atom-Auth-at-Edge).

### Application parameters

| Parameter | Description | Example |
|---|---|---|
| `StackName` | Application namespace used for Lambda names and SSM parameters | `docs-prod` |
| `DiscoveryDocument` | Tenant-specific Entra OIDC discovery URL | `https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration` |
| `Oauth2RedirectPath` | OAuth callback path | `/auth/callback` |
| `ClientId` | Microsoft Entra application Client ID | Entra application GUID |
| `ClientSecret` | Microsoft Entra client secret value | Application secret |
| `PublicUrl` | Base public URL of the CloudFront distribution | `https://example.com` |
| `SessionSecret` | Secret used to sign application session cookies | Random 64-character hex string |
| `Scope` | OpenID Connect scopes | `openid profile email` |

Generate a session signing secret:

```bash
openssl rand -hex 32
```

**PublicUrl** must contain the scheme and hostname only, without a path or trailing slash.

For example, if a CloudFront distribution serves:

```text
https://example.com/docs/
https://example.com/app/
https://example.com/api/
```

The `PublicUrl` is:

```text
https://example.com
```

All protected paths on the same hostname can use the same OAuth callback and authentication session.

### Created resources

A deployed application creates:

- **Auth Handler:** Lambda@Edge function for session verification and authentication redirects.
- **Callback Handler:** Lambda@Edge function for completing Entra authentication and issuing sessions.
- **ReWrite Handler:** Optional Lambda@Edge function for static website index rewriting.
- **SSM Parameters:** Configuration values under `/{StackName}/auth/`, including the Entra configuration, Client ID, client secret, public URL and session signing secret.
- **IAM Roles:** Execution roles with the permissions needed by each Lambda.

The client secret and session secret are stored as ordinary SSM `String` parameters. Restrict access to these parameters appropriately.

## CloudFront integration

After deployment, obtain the published Lambda version ARNs from the Lambda console.

**Lambda@Edge requires numbered function versions.** An ARN referencing `$LATEST` or a Lambda alias cannot be used.

### Protected behaviours

Associate the Auth Handler with the **Viewer request** event of every CloudFront behaviour requiring authentication.

For example:

| Path pattern | Origin | Lambda@Edge event | Function |
|---|---|---|---|
| `/docs/*` | Documentation S3 bucket | Viewer request | Auth Handler |
| `/app/*` | Application S3 bucket | Viewer request | Auth Handler |
| `/api/*` | API origin | Viewer request | Auth Handler |
| `/auth/callback` | Callback origin | Viewer request | Callback Handler |

The same Auth Handler Lambda version can be attached to multiple behaviours and origins within a CloudFront distribution.

All behaviours using the same hostname and authentication configuration share the same session cookie.

**Important:** CloudFront behaviours are evaluated by path precedence. Ensure the callback behaviour takes precedence over any general protected behaviour that might otherwise match `/auth/callback`.

### Callback behaviour

Create a CloudFront cache behaviour matching the configured OAuth redirect path:

```text
/auth/callback
```

Configure it as follows:

- **Origin:** Any suitable valid origin, such as the existing S3 origin, since the Callback Handler generates the response.
- **Cache policy:** Caching disabled.
- **Viewer protocol policy:** Redirect HTTP to HTTPS or HTTPS only.
- **Lambda@Edge:** Callback Handler on the Viewer request event.
- **Allowed methods:** GET and HEAD are sufficient for the configured query-response callback.

Do not attach the normal Auth Handler to this behaviour.

The Callback Handler must be able to receive the authorization code, state and temporary authentication cookies from the viewer request.

### Static website rewriting

The optional ReWrite Handler can be attached to an **Origin request** event for static website behaviours.

This supports websites that use directory-style paths, such as:

```text
/docs/getting-started/
```

which need to resolve to:

```text
/docs/getting-started/index.html
```

Origin-request functions execute only when CloudFront needs to retrieve content from the origin, so they do not run for every cached request.

### Multiple origins

A single authentication deployment can protect multiple origins, provided they share:

- The same public CloudFront hostname.
- The same Entra application registration.
- The same authentication and authorization policy.
- The same session configuration.

There is no need to deploy separate authentication functions for each S3 bucket or origin.

If different origins require different authorized users, Entra applications or session policies, additional authorization logic or separate deployments may be necessary.

### Origin security

Lambda@Edge protects requests passing through the configured CloudFront behaviours.

It does not protect an origin that is independently accessible through another public URL.

For S3 origins, use a private bucket with CloudFront Origin Access Control (OAC), where supported.

For application and API origins, restrict direct access using appropriate origin-level controls.

## Session management

Authentication uses a short-lived, signed application session cookie.

The cookie is:

- Signed using the configured `SessionSecret`.
- Marked `Secure` and `HttpOnly`.
- Restricted using `SameSite=Lax`.
- Shared across paths on the configured hostname.
- Valid for up to one hour.

The Auth Handler validates the session locally rather than making a request to Microsoft Entra ID on every page load.

OIDC discovery configuration and signing keys are cached where possible to reduce network requests.

After the application session expires, the browser is redirected through Entra authentication again.

**Note:** Session validity is independent of subsequent changes to an Entra account or Conditional Access policy. Existing sessions remain usable until their local expiry unless additional revocation mechanisms are implemented.

## Limitations

- **Single application client:** Each deployed instance supports one Entra application Client ID and its associated configuration.
- **Single public hostname:** Each instance is configured with one `PublicUrl`. Multiple paths and origins on that hostname are supported.
- **Shared access policy:** All behaviours using the same handler currently share the same authentication requirements. Per-origin or per-path authorization requires additional logic.
- **Session revocation:** Existing signed sessions remain valid until expiry. Immediate revocation is not currently implemented.
- **Lambda@Edge restrictions:** Functions must be deployed in `us-east-1` and associated with CloudFront using numbered versions.
- **CloudFront behaviour restrictions:** A cache behaviour supports only one Lambda@Edge association for each event type.
- **No automatic CloudFront updates:** Publishing or updating the Serverless Application Repository application does not automatically update CloudFront associations.

## Updating Lambda@Edge integrations

CloudFront requires a versioned Lambda ARN, for example:

```text
arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:auth-handler-docs-prod:3
```

When deploying an updated application version, AWS may publish new Lambda versions.

To use the updated code:

1. Deploy the new application version.
2. Locate the newly published Auth Handler and Callback Handler versions.
3. Update the Lambda@Edge associations in the CloudFront distribution.
4. Save the distribution changes.
5. Allow CloudFront to finish deploying the updated configuration.

Any behaviours still referencing older versions will continue running the older authentication implementation.

Where multiple behaviours share the same handler, update each association consistently.

## Troubleshooting

### Redirect loop after login

Check that:

- The registered Entra redirect URI matches `PublicUrl` and `Oauth2RedirectPath`.
- `/auth/callback` routes to the Callback Handler.
- The Auth Handler is not intercepting the callback.
- Authentication cookies are accepted by the browser.
- The session signing secret is consistent across the deployed handlers.

### Authentication callback returns 400 or 403

Check that:

- The Entra Client ID and client secret are correct.
- The configured redirect URI matches the registered Entra Web redirect URI.
- The discovery URL references the correct tenant.
- The OAuth state, nonce and PKCE verifier cookies are preserved during the redirect.
- The application has the appropriate Entra permissions and user assignments.

Inspect the Callback Handler's CloudWatch logs for additional information.

### CloudFront returns 502 or 503

Check that:

- CloudFront references a published Lambda version.
- Lambda functions are deployed in `us-east-1`.
- The function execution role trusts both `lambda.amazonaws.com` and `edgelambda.amazonaws.com`.
- The functions have permission to access their SSM configuration.
- The deployed dependencies are compatible with the Lambda runtime.
- The function completes within its configured timeout.

### Authentication works for one path but not another

Verify that the Auth Handler is associated with every intended protected CloudFront behaviour.

CloudFront's default behaviour does not automatically protect requests matching more specific behaviours.

## Security considerations

- Use HTTPS for all public endpoints.
- Keep S3 buckets and other origins inaccessible through unprotected public endpoints.
- Restrict access to SSM parameters containing authentication secrets.
- Use sufficiently random session signing secrets.
- Use Entra Enterprise Application assignments where access should be limited to specific users or groups.
- Apply Conditional Access policies to the Entra application as required.
- Avoid logging authorization codes, client secrets, ID tokens or authentication cookies.
- Keep Lambda dependencies and published application versions up to date.
