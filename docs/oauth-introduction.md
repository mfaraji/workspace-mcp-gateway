# OAuth access to the MCP gateway

The gateway now has an optional Clerk-based OAuth path for ChatGPT and other MCP
clients, including headless CLIs. It sits alongside the existing Open WebUI
trusted-header and static bearer-token paths; the Google Drive, Calendar, Tasks,
Apex, Apex Wealth, and Telegram tools still run through the same gateway and
per-user provider connections.

The gateway identifies an OAuth user by the verified Clerk issuer and subject,
regardless of whether ChatGPT or a CLI presents the token. On first use it
creates that user's gateway account automatically. Someone with existing Open
WebUI provider connections can explicitly link their Clerk sign-in from
Settings → Integrations to reuse those connections. The gateway publishes OAuth
discovery for `/mcp` and each product mount, and checks token signature,
issuer, expiry, exact MCP resource audience, and the `mcp:tools` scope. CLI
clients can use Clerk's device-code flow without a browser on the CLI machine;
approval happens on another device.

The gateway enables this path when `CLERK_ISSUER` is configured. During setup on
2026-10-25, ChatGPT sign-in succeeded but action discovery failed because Clerk
issued tokens without the required audience. We registered `/mcp` and the six
product URLs as Clerk MCP resources and enabled `aud_claim_enabled` through
Clerk's management MCP. After a fresh ChatGPT connection, the user confirmed
tools are available and `system_get_current_time` returned the time. Provider
actions and real CLI device-flow validation remain separate checks. The gateway
continues to reject tokens without the exact
resource audience. See the
[setup instructions](../README.md#oauth-for-chatgpt-and-headless-mcp-clients)
for the remaining steps.
