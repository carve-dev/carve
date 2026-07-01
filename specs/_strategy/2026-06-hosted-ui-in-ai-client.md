# Hosted UX delivered *through* the AI client (live MCP App) — parked option

- **Status:** **Parked option — not a decision.** Captured for evaluation when hosted/paid-product work begins; nothing here is committed, and no capability spec is edited on the strength of it. Revisit alongside the hosted control plane (PRD §5.11).
- **Date:** 2026-06-22
- **Extends the thread of:** [positioning #13 — "headless by default"](./2026-05-positioning.md) and PRD §5.10/§5.11 (hosted moats are *operational, not feature-functional*).
- **Origin:** a brainstorm, not a proposal. Recorded so the option and its research survive.

## The option

Take "headless by default" to its logical end for the **hosted** product: deliver the operational UX **primarily through the user's AI client** (Claude, ChatGPT) as a **live, interactive MCP App**, rather than as a standalone polished cloud UI. The user asks "how's my Stripe pipeline?" in chat and gets a real-time, updating pipeline view rendered inline — the whole monitor/observe experience happens without leaving Claude/ChatGPT.

This does **not** propose deleting the cloud UI line item from §5.11. It proposes a *different center of gravity*: the AI client becomes the primary surface; the hosted web UI shrinks toward a thin canonical backstop for the things chat is structurally bad at (see "What it does not solve").

## Why it's credible now (it wasn't, until 2026)

The brittle assumption — "bet the UX on immature, per-client generative-UI primitives" — was largely retired by the **MCP Apps** standard:

- **MCP Apps** launched 2026-01-26 (SEP-1865 / ext-apps): the first official MCP extension for returning interactive UI. Cross-host — **Claude (web + desktop), ChatGPT, Goose, VS Code** shipped support; build once via the `ui://` resource + `ui/*` JSON-RPC bridge.
- **Live connections are first-class.** The CSP key `connectDomains` (`connect-src`) explicitly covers **WebSocket** (plus `fetch`/XHR). A component can hold a `wss://` to Carve and mutate the DOM as run events stream — no model in the loop. (`frameDomains` also permits nested iframes, so embedding a Carve-origin page is a fallback path.)
- **Portable with an asterisk.** Core bridge + CSP keys are shared Claude↔ChatGPT; ChatGPT's `window.openai` extras (PiP/fullscreen negotiation, checkout, modals) are host-specific. Build to the standard, feature-detect the extras.

## The architecture, *if* pursued

The MCP Apps lifecycle forces a specific (and healthy) shape. Per the spec, **the iframe is a disposable viewer, not a durable session**: teardown may fire "at any point in the lifecycle... for any reason" and there are **no connection-persistence guarantees**; `setWidgetState`/`widgetState` persists **serializable UI state only**, not live sockets; display-mode transitions can remount the component from scratch. The spec's intended pattern for durable work is **server-side Tasks that outlive the iframe**.

So the design is a **resumable viewer over a Carve-owned stream**:

1. The run/stream lives in **Carve's backend** (event bus + run history = source of truth); the pipeline runs regardless of the iframe.
2. The component is a **resumable subscriber**: on every `ui/initialize` (mount *or* remount) it rehydrates a cursor + light snapshot from `widgetState`, opens a `wss://` carrying a **resume cursor** (last event id / state version); Carve replays the gap, then streams live.
3. On `ui/resource-teardown` it checkpoints the cursor and closes cleanly; same dance on `host-context-changed`.
4. Target **PiP** display mode for "watch it while you keep chatting."

This is the `EventSource` + `Last-Event-ID` pattern. The only new backend requirement is a **replayable-from-cursor event stream** — which the runtime's event bus / run history should support anyway. **If built, this would extend [`mcp-server`](../capabilities/mcp-server.md)** from a thin JSON-over-REST adapter to also serving `ui://` resources backed by a live WebSocket onto the event bus. (Capability spec deliberately *not* edited now.)

## What it does *not* solve

- **Proactive push.** MCP Apps render *when a tool is invoked* — they cannot let Carve interrupt an idle conversation. The "Carve sends you a morning summary" UX still needs the client's own scheduler (ChatGPT Tasks / Claude scheduled tasks) or a real push channel (Slack / email / PagerDuty) with a deep link. Live-on-demand and proactive-push are **separate problems**; this option only cracks the first.
- **Non-AI-client stakeholders & shared/audit surfaces.** An exec wanting a status link, a browsable audit log, a team dashboard — these still argue for a thin canonical hosted page. The option *reduces* that surface; it doesn't eliminate it.

## The decision-gating spike (cheap, do first if revisited)

Capability is no longer the question — **per-host reconnect quality is.** The spec punts most behavior "to the MCP Host," so Claude vs ChatGPT will differ in how aggressively they tear down / throttle / remount. Whether "live" *feels* continuous depends on reconnect+gap-replay being sub-second, which is **only knowable empirically**.

> **Spike:** a throwaway MCP App that opens a `wss://` to a counter endpoint with a resume cursor; force remounts (toggle display modes, scroll away, new turns, background the tab) and measure teardown→remount→resume latency and any idle-throttling, on **both** Claude and ChatGPT. ~half a day. This is demo-or-die: it decides whether the experience dazzles or flickers.

Also weigh: maturity (surface is months old; live-rendered-iframe bugs exist), and the `window.openai` lock-in line.

## Sources

- MCP Apps spec draft (lifecycle): https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/draft/apps.mdx
- MCP Apps launch: https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/
- OpenAI Apps SDK — MCP Apps in ChatGPT: https://developers.openai.com/apps-sdk/mcp-apps-in-chatgpt
- OpenAI Apps SDK — Managing State: https://developers.openai.com/apps-sdk/build/state-management
- MCP App CSP (connectDomains/frameDomains): https://sunpeak.ai/blogs/mcp-app-csp-external-api-calls/
- Display-mode reference (inline/fullscreen/PiP): https://sunpeak.ai/blogs/chatgpt-app-display-mode-reference/
