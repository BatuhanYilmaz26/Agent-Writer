# Writing workflow

AgentWriter uses a fixed LangGraph workflow. It decides whether research is required, gathers bounded evidence, validates a plan, writes sections concurrently, assembles the article, checks citations, reviews the result, and rewrites affected sections within a fixed limit.

## Development output

The built-in fixture workflow uses deterministic synthetic responses and a small synthetic evidence page. It needs no model or search credentials and makes no network requests. Its article starts with a synthetic-development notice and ends in `needs_review`, even if internal formatting checks pass. This exercises the application journey; it does not assess a real model's writing quality or establish factual support.

The default fixture plan has two sections. It can produce initial drafts and at most two correction rounds. Source links are resolved from the supplied source ledger, not invented by a writer model. Opening an example.org fixture link is not a claim that the application fetched that page.

## Generation and review outcomes

| Outcome | Meaning |
|---|---|
| `succeeded` | The current generated draft passed its configured model review and independent deterministic gates; human publication review is still required |
| `needs_review` | A usable draft exists, but quality/evidence issues or synthetic data prevent acceptance |
| Failure | A required contract, provider, persistence or ownership step failed; the worker retains only explicitly recorded partial work |

Research-off drafts are labeled unresearched. Unsupported citations, incomplete task sets and invalid plan identities cannot silently become accepted articles. Human edits are new unassessed revisions; they never inherit generated approval.

## Section ordering and revision limits

Plans contain two to eight tasks with unique IDs, contiguous order and word budgets summing to the requested target. Workers may finish in any order. Assembly waits for the real LangGraph fan-out barrier and then selects one latest accepted revision for every expected task, sorting by plan order.

Results use identities such as `section_1:0` and `section_1:1`. Repeating an identical value is idempotent; different contents under one committed identity raise a conflict. Review issues can name particular tasks, so a correction does not regenerate unaffected sections. Every correction batch consumes one round. Remaining issues at the limit produce `needs_review` instead of an unbounded loop.

Citation validation checks source identity and allowed markup. It does not prove that an excerpt supports a factual statement. Live-provider claim-support and human quality evaluation remain separate tracker gates. The current deterministic word-budget check flags an assembled article outside 80–120% of its target for review; a successfully parsed model response alone does not bypass it.

## Integration contract

The public Python integration is `agentwriter.graph`:

```python
from agentwriter.domain import CreateRunRequest
from agentwriter.graph import build_graph, initial_state, make_fixture_dependencies

request = CreateRunRequest(topic="How forests grow", target_words=500)
dependencies = make_fixture_dependencies(request)
builder = build_graph(dependencies)
# In the application, the worker supplies its attempt-scoped PostgreSQL saver.
graph = builder.compile()
result = await graph.ainvoke(initial_state(), {"recursion_limit": 50})
print(result["outcome"])
```

This asynchronous snippet illustrates the fixture integration. Compiling without a saver is suitable only for an explicitly ephemeral development invocation. Use the actual worker startup path for durable work; a browser request must not own the graph execution.

`GraphDependencies` accepts a `ModelGateway`, the validated brief, an optional research gateway, hooks, and bounded concurrency/call/review settings. The helper returns a fully registered fixture model and research corpus. Application hooks are asynchronous and may:

- Recheck active lease, cancellation and deadline before/after model calls.
- Reserve durable capacity before **each physical dispatch** and reconcile results/errors.
- Persist plans, sources and accepted sections with ownership fences.
- Notify on the final candidate; authoritative terminal state/artifact/event persistence remains the worker's atomic transaction.

Hook names are `check_active`, `before_model_call`, `on_model_result`, `on_model_error`, `on_plan`, `on_source`, `on_section`, and `on_final`. Exceptions stop the graph; they are not converted into synthetic success. The model gateway owns transport retry classification, while graph review rounds remain a separate budget.

## Recovery boundary

Checkpoints contain JSON primitives. A worker compiles once per claimed execution attempt with a saver on the same non-reconnecting session that holds the run's advisory lock. Resume uses the existing thread and `None` input for failed work, not a fresh request payload. A new request receives a new run/thread.

The graph's `model_calls` field counts completed node calls for diagnostics. Its process-local limiter bounds a single attempt; the worker's durable reservation ledger is authoritative across crashes and uncertain upstream outcomes. `initial_model_calls` must reflect previously consumed capacity when reconstructing dependencies for recovery. Without the durable hook, an isolated graph invocation does not prove cross-process quota or exactly-once billing behavior.

The current graph carries bounded fixture excerpts for execution. It does not claim a qualified model-token estimator or complete claim-support audit. Large-scale evidence-reference optimization and external provider acceptance must retain their tracker residuals until exercised through the actual persistence/provider paths.

## Executed checks

The unit suite executes real LangGraph rather than a graph mock: two/eight-way out-of-order dispatch, stable assembly, targeted revision, round exhaustion, invalid plans, reducer conflicts, local call caps, cancellation hooks, and failed-node checkpoint resume. `InMemorySaver` is used only in the explicitly labeled unit recovery check. PostgreSQL recovery, leases, stale-session exclusion, external contracts and deployment behavior require their own integration evidence.

The documented patterns follow the official [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [workflow patterns](https://docs.langchain.com/oss/python/langgraph/workflows-agents), and [checkpointer](https://docs.langchain.com/oss/python/langgraph/checkpointers) references. No provider or production guarantee follows from a fixture run.
