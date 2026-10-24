# Model and research providers

AgentWriter keeps provider selection in server configuration. Your browser never
receives an API key or selects a provider URL. Fixture mode uses deterministic
synthetic content and makes no model or research network requests.

## Local fixtures

Use fixtures to develop the article workflow without credits or provider
accounts. Fixture articles and sources are labeled synthetic. They demonstrate
the workflow; they are not researched articles or evidence of writing quality.

`FixtureGateway` accepts a mapping from call identity or schema name to a JSON
payload. It validates that payload through the same Pydantic schema used by the
live adapter. `FixtureResearchGateway` accepts a local corpus of public-shaped
URLs, titles and text. Its fixed retrieval time and hashes make replay tests
reproducible. An empty corpus remains empty evidence.

## OpenRouter

The model adapter sends structured, nonstreaming chat requests to
`https://openrouter.ai/api/v1/chat/completions` through one reusable HTTPX client.
The workflow can still stream progress to the browser. Provider responses are
bounded to 1 MiB, redirects are disabled, and returned JSON must satisfy the
application model and its business validation.

Free-only configuration accepts `openrouter/free` or explicitly configured
`:free` model IDs. It never substitutes a paid model. Requests require parameter
support and deny provider data collection; no route satisfying that policy is a
visible provider failure, not permission to weaken the policy. The requested
router and actual returned model remain separate metadata.

Budgeted configuration requires explicit model/provider allowlists and a
positive reservation amount supplied by the trusted worker dispatch service.
The adapter does not create a durable reservation or decide that a price is
known. Do not enable paid calls until shared budget admission and account
qualification are verified in the tracker.

Failures use public-safe codes. The scheduler owns retry/backoff/deadline
decisions; this transport never retries by itself. A rate-limit error preserves
`Retry-After` where valid. A timeout can mean the remote request completed, so
the caller must retain its reservation. Missing usage or cost remains unknown,
not zero. Local cancellation does not prove remote processing or billing stopped.

No authenticated OpenRouter request has been used to qualify this adapter yet.
Contract tests use HTTPX's in-memory transport. Before activation, record current
account quotas, successful strict-schema calls, model/provider identity, data
policy behavior and observed cost in LLM-07.

Official references, consulted 24 October 2026:

- [Structured output and endpoint support](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Provider routing and data policies](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Account limits](https://openrouter.ai/docs/api_reference/limits)
- [HTTPX async client lifecycle](https://www.python-httpx.org/async/)

## Treg

Treg is retained as the planned research service. Its adapter currently refuses
calls with `research_provider_not_qualified`. It does not guess a REST schema,
silently switch vendors, or invoke an unverified paid waterfall.

Before enabling it, complete RES-01/02: save the actual account's catalog
definitions and schema hashes, verify token/organization requirements, capture
sanitized successful and failed search/extract fixtures, and test routing cost
controls. A preferred free vendor is not a free-only guarantee. If no verified
zero-cost non-paid-fallback route is available, free-only research must stop.

Official references:

- [Treg REST reference](https://treg.to/docs)
- [Maintainer WebAccess instructions](https://github.com/superdesigndev/treg/blob/main/skills/web-access/SKILL.md)
- [Maintainer usage reference](https://github.com/superdesigndev/treg/blob/main/USAGE.md)

## Source links and citations

Generated drafts use `[@source_id]` markers. AgentWriter resolves them from its
source ledger; models cannot invent clickable citation URLs. Deterministic
checks reject unknown/malformed markers, unextracted sources, authored links,
images and raw HTML. These checks establish reference identity, not whether the
source supports the claim. Claim-to-excerpt review remains a separate gate.

Source links must use public DNS HTTP(S) hostnames and standard ports, without
credentials, local hostnames, IP literals or markup/control characters. DNS
preflight additionally rejects any private/non-global resolved address. This is
not a direct fetcher: direct page fetching remains disabled until connection-time
and redirect-level egress enforcement is qualified. DNS preflight alone does
not prevent rebinding.

Citation rendering preserves source title, ledger URL and retrieval time. Human
edits must not inherit the previous generated revision's review approval.
