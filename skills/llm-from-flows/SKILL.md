---
name: llm-from-flows
description: Call an OpenAI-compatible or Anthropic-compatible LLM gateway from a cloud flow (built-in HTTP action) and from a canvas app through an app-called flow, including agentic tool-calling chat loops and RAG gateways. Use whenever a flow or app needs a model call, when parsing model responses or tool calls, when a gateway returns 200 with an error, or when planning live tests that spend tokens.
---

# Calling LLMs from flows and apps

## Architecture that works

App -> ONE app-called flow (PowerApp trigger, one JSON `payload`) -> built-in **HTTP** action to the gateway ->
Respond `result_json`. The built-in HTTP action inside an app-called flow does not make the app premium; an HTTP
*connector* reference (e.g. "HTTP with Microsoft Entra ID") on the app does (C-41). Keep the API key out of the app:
an `InitializeVariable` string in the flow (a placeholder in source; the real value is entered in the designer or
preserved across API redeploys by `devtenant.flows.preserve_secrets`), marked secure (Settings -> Secure inputs).
Check early that tenant DLP allows the gateway (L-15).

Contract (provider-neutral, so swapping gateways never touches the app):
payload `{messages:[{role, content}], ...options}` -> `result_json` `{status: ok|error|timeout, message, tool_name,
tool_args_json, references, httpStatus, detail}`. A `Switch` on a provider value inside the flow is the swap seam.

## Request shapes

OpenAI-compatible:
```json
{ "model": "<MODEL_ID>", "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "tools": [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}] }
```
Answer: `choices[0].message.content`; tool calls: `choices[0].message.tool_calls[]`.

Anthropic-compatible:
```json
{ "model": "<MODEL_ID>", "system": "...", "max_tokens": 2000,
  "messages": [{"role": "user", "content": "..."}], "tools": [{"name": "...", "input_schema": {...}}] }
```
Answer: `content[0].text`; `stop_reason: tool_use` with `tool_use` blocks.

## Traps (details: reference/platform-traps.md L-01..L-15)

- `system` is top-level for Anthropic-style; `max_tokens` must be an INTEGER (L-01).
- Reasoning models: `max_completion_tokens`, temperature only 1, errors one at a time -> learn per model (L-03).
- Tool calls come in two shapes behind one gateway; parse both, and never execute a call whose arguments did not
  parse (L-04). Decide on the presence of tool calls, not `finish_reason` (L-05).
- Errors come back as HTTP 200 -- inspect the body (status fields, "experiencing issues", "token limit") before
  treating a call as success; map to `status: error` in `result_json` (L-06).
- Strip ```` ```json ```` fences before `json()` (L-01).
- A history that ends on an assistant turn is rejected by some models (L-08).
- Normalise over-escaped `\n` from tool arguments before display (L-09).
- A listed model can be dead; an unlisted name can silently route elsewhere (L-07). Make the model a single setting.
- A system prompt inside a single-quoted WDL expression: double every apostrophe (L-14).
- Synchronous app calls time out around 2 minutes; `limit.timeout` on the HTTP action does not bound it (C-14, F-19).

## Agentic loop in a canvas app

Power Fx has no While: unroll N "hop" blocks, or drive hops from a Timer. `Select()` is queued (C-13), so dispatch one
call at a time with a busy flag; a `stop` tool ends the turn; cap hops; detect a repeated identical tool-call signature
and stop. Put the tool EXECUTORS in flows (e.g. a query flow that runs a fixed-`$select` SharePoint REST GET), and keep
three surfaces in lock-step: the flow's allow-list, the app's executor guard, and the tool enum in the prompt -- drift
means silent rejection. Citations: have the executor stamp a cite token on each row and the prompt append
`[[token]]`; the app renders them as superscripts with deep links (HtmlViewer opens `target=_blank` links, C-38).

## RAG gateways

If the gateway retrieves server-side (a dataset parameter on the query), retrieval is not a tool call -- tell the model
in the system prompt that retrieved excerpts are already in its context and to use list tools only for structured
data. Ingest endpoints commonly APPEND rather than overwrite and may not delete per document (L-13); chunk quality
(header-aware chunks with small overlap) materially changed answer correctness on niche syntax. Measure the gateway's
chunking and limits with a small probe before bulk ingest.

## Token discipline (L-11)

Before any live test, state the cost per question (system prompt x hops) and cap the run to a named handful of
questions. Prefer offline tests (stubbed responses) and reading existing run history. Ask the owner for the gateway's
meter reading when cost matters; character-based estimates can be off by 2x.

## Minimal flow skeleton

```
Compose_Payload        json(if(empty(triggerBody()?['payload']), '{}', triggerBody()?['payload']))
Initialize_ApiKey      string, value "<API_KEY>" in source (secure inputs on)
Compose_Request        the provider's request body built from outputs('Compose_Payload')
Call_LLM               built-in HTTP POST (see reference/attested-connector-shapes.md "Built-in HTTP")
Compose_Result         status from HTTP status AND body error fields; message from the provider's answer path
Respond_to_PowerApps   result_json = string(outputs('Compose_Result')), runAfter Call_LLM in all four states
```
