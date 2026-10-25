# OAuth implementation and outstanding issues

Status recorded: 2026-10-25. This summarizes the implementation and the checks
performed during the ChatGPT connection investigation; it is not a live monitor.

## Goal and current status

Connect ChatGPT and CLI clients to the existing Python MCP gateway at
`https://mcp.ashpazi.shop/mcp`, behind HTTPS nginx, using Clerk OAuth. The gateway
serves Google Calendar, Drive, Tasks, Telegram, Apex, and Apex Wealth through
one backend process. No Next.js application was added.

**ChatGPT authentication, tool discovery, and a tool call are working.** The user
confirmed tools are available after reconnecting with a fresh authorization and
`system_get_current_time` returned the time successfully. The earlier
`Authentication succeeded, action discovery failed` error was caused by missing
Clerk MCP resource registration and a disabled audience claim. We corrected
that configuration through Clerk's management MCP. Provider actions and CLI
device authorization remain separate verification steps.

## What was implemented

- Clerk authorization-server discovery and JWT verification in
  `src/gateway/oauth/clerk.py`. Verification checks the signature, configured
  issuer, expiry, subject, exact MCP resource audience, and `mcp:tools` scope.
- Protected-resource metadata for `/mcp` and product mounts, Bearer challenges,
  and OAuth security declarations on tool definitions.
- OAuth identity resolution alongside the existing Open WebUI trusted-header
  and `wmcp_` static-token paths.
- Stable user identity derived from the verified Clerk issuer and subject.
  The OAuth client ID does not determine ownership of provider connections.
- Optional explicit linking to an existing Open WebUI user, with link status,
  callback, and unlink routes. Migration `0004_oauth_identity_links` adds the
  persistence for this flow and was applied during setup.
- Headless CLI device authorization in `scripts/mcp_oauth_device.py`, including
  optional refresh-token output. The user approves sign-in on another device;
  this is not an unattended login mechanism.
- A token validation helper, `scripts/check_oauth_token.py`, that reads a token
  from stdin without displaying it.
- Configuration and deployment wiring in settings, `.env.example`, Docker
  Compose, nginx setup, and dependency files; setup documentation and tests.

Provider credentials remain encrypted on the gateway. Tools continue through
the existing identity, policy, confirmation, and audit pipeline.

## User connections versus OAuth clients

Provider connections belong to a gateway user. ChatGPT and a CLI have separate
OAuth registrations and authorization grants, but can resolve to the same
gateway user when they authenticate as the same Clerk issuer and subject.

An existing Open WebUI user and a direct Clerk user are separate gateway
identities until explicitly linked. The gateway does not merge accounts based
on email. Unlinking removes access through that link to the Open WebUI user's
connections; the Clerk identity can still use its own gateway account.

The existing Clerk applications reported during setup were:

- `mcp-gateway-link`: OAuth application intended for optional account linking.
- `mcp-gateway-cli`: public OAuth application intended for CLI access.

The audience fix was MCP resource registration and enabling the audience claim;
it did not require creating another OAuth client.

## What the investigation verified

| Check | Last observed result |
| --- | --- |
| Public `/health` and `/ready` | HTTP 200 |
| Public protected-resource metadata for `/mcp` | HTTP 200 |
| Public MCP `tools/list` using a temporary gateway static token | HTTP 200, 31 tools |
| `/mcp` redirect to `/mcp/` | Followed successfully in the static-token check |
| Initial ChatGPT reconnect using Clerk OAuth | HTTP 401; `missing required claim aud`; no matching alternate resource claim |
| Clerk management MCP | Connected and authenticated |
| Clerk MCP resource configuration | `/mcp` and six product mounts registered; `aud_claim_enabled=true` |
| Final Clerk public discovery check for `/mcp` | Passed; no configuration blockers |
| ChatGPT reconnect after configuration fix | User confirmed tools are available |
| Authenticated ChatGPT tool call | User confirmed `system_get_current_time` returned the time |
| Full test suite | 136 passed, 8 skipped |
| Ruff and `git diff --check` | Passed |

The temporary static token was deleted after testing. No bearer tokens were
printed or retained in diagnostic logs. The temporary resource-claim diagnostic
was removed; the safe missing-claim log remains.

The gateway was restarted to deploy the identity and diagnostic changes.
The static-token checks establish that transport and tool discovery work with a
gateway credential. The later user-confirmed ChatGPT reconnection and time tool
call establish OAuth tool discovery and authenticated execution. Provider
actions through ChatGPT and real CLI device authorization require their own
verification.

An Open WebUI OAuth-linking configuration change made early in the investigation
was reverted after the target client was clarified as ChatGPT. The final observed
Open WebUI linking flag was disabled, and Open WebUI remained healthy.

## Audience issue and configuration fix

The original Clerk authorization-code token did not satisfy the gateway's
resource audience requirement. Sign-in succeeded while subsequent MCP requests
failed authentication. Safe diagnostics identified a missing `aud` claim.

OpenAI's MCP guidance requires verifying that an access token was issued for
the target resource. Removing audience validation would not resolve that
requirement. The current gateway verifier specifically requires `aud` to match
the MCP resource URL, such as `https://mcp.ashpazi.shop/mcp`.

Once Clerk's management MCP was connected, its `configure_mcp_server` tool
exposed the required resource registration and audience-claim setting. The
gateway's live public metadata identified the correct application and instance:

- Application: `app_35ZT8HH0LB6HqP2Tj5yjFq94h5i` (Beacon).
- Instance: `ins_35ZT8HmHAtFp0IrnBFTReg3lv78` (development).
- Issuer: `https://robust-ibex-65.clerk.accounts.dev`.
- Registered resources: `https://mcp.ashpazi.shop/mcp` and its `/calendar`,
  `/drive`, `/tasks`, `/apex`, `/apex-wealth`, and `/telegram` product mounts.

We read the existing configuration and schema, previewed each resource change,
then saved it. The first registration enabled `aud_claim_enabled`. Existing
JWT access-token issuance and CIMD advertisement were already enabled. No
authorization-server migration or gateway audience-validation change was needed.

Clerk's public discovery checker passed the endpoint, resource metadata,
discovery, HTTPS, S256 advertisement, registration, and audience-setting checks.
It also reported missing scope guidance in the unauthenticated Bearer challenge;
the gateway was updated to advertise `scope="mcp:tools"` on OAuth challenges.
After deployment, a fresh public discovery check passed, including scope
guidance. Public discovery checks do not obtain tokens or call authenticated tools.

The audience backfill setting for old refresh grants was not enabled, so clients
should complete a fresh authorization instead of relying on an old refresh token
to repair missing resource binding. A real device-code token still needs its own
validation before CLI OAuth can be considered ready.

## Clerk management access and next steps

Clerk's management MCP is now connected and was used to apply the audience fix.
Earlier, the local Clerk CLI was installed, but `clerk auth --help` exited with
code 132; its failure was not diagnosed further and did not prevent MCP setup.

Clerk does provide an official management MCP server. The earlier plugin search
did not find it in the catalog, but its documented remote endpoint is available
for direct configuration in Codex:

```bash
codex mcp add clerk --url https://mcp.clerk.com/mcp
```

Complete the browser sign-in and select the workspace containing the existing
Clerk application. If sign-in needs to be started separately:

```bash
codex mcp login clerk
```

For login from a headless server, run `codex mcp login clerk --no-browser`, open
the printed authorization URL on another device, and paste the final callback
URL into the waiting terminal when prompted. Keep that callback URL out of chat.

Remaining verification:

1. Validate a real device-code token against its exact MCP resource and scope.
2. Connect the user's provider accounts, or explicitly link an existing Open
   WebUI user, before testing tools that require those connections.
3. Test a provider read and a controlled mutation through the confirmation flow
   when that provider action is explicitly authorized.

The OAuth changes were still uncommitted when this summary was written.

## References

- [Repository setup instructions](../README.md#oauth-for-chatgpt-and-headless-mcp-clients)
- [Short OAuth introduction](oauth-introduction.md)
- [OpenAI MCP authentication requirements](https://developers.openai.com/plugins/build/auth)
- [Clerk management MCP setup and tools](https://clerk.com/docs/guides/ai/mcp/clerk-mcp-server)
- [Clerk OAuth implementation](https://clerk.com/docs/guides/configure/auth-strategies/oauth/how-clerk-implements-oauth)
