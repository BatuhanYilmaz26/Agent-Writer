# AgentWriter user guide

This guide describes the browser workspace in the local development profile. The
default profile uses synthetic model and research fixtures. It is a complete
workflow demonstration, not a live research or publishing service.

## Before you start

Open the app at `http://127.0.0.1:8000` after following
[Setup and operations](setup-and-operations.md). The header shows one of:

- **Local fixture profile** — synthetic model and research; no live AI or web
  research is used.
- **Local profile** — a local non-fixture provider is configured. Check the
  evidence labels before publishing.

The browser starts a loopback-only local session automatically. A session expires
after `SESSION_TTL_HOURS` (8 hours by default); reload the page to start a new
one.

The header also has a light/dark theme toggle. It follows your system colour
scheme until you choose a theme; the choice is remembered in this browser
(`agentwriter.theme`).

## Create a draft

Choose **New draft** to open the writing studio. Start with your own topic or use
**A practical guide**, **Explain an idea** or **Tell a story** as an editable
starting point. The character counter helps you keep the topic within its limit.
Use the word-target buttons for a quick choice, or enter another supported target.

Open **Additional direction** for structure, exclusions or extra questions. These
instructions are optional. The preferences beside the brief control the reader,
tone, length and research mode.

Fill in:

| Field | Required | Notes |
|---|---|---|
| Topic | Yes | 1–500 characters. Be specific about the reader, promise and facts to check. |
| Reader | Yes | 1–240 characters. Defaults to “General readers”. |
| Tone | Yes | One of the offered tones; stored with the brief. |
| Language | Yes | English, Türkçe, Español, Português or Deutsch. Defaults to English. |
| Target words | Yes | 500–3,000. |
| Research | Yes | **Required**, **Automatic** or **Off**. |
| Additional direction → Anything else? | No | Up to 4,000 characters for structure, exclusions or questions. |

### Article language

Choose **Language** beneath the brief to write in English, Turkish, Spanish,
Portuguese or German. The choice follows the draft through planning, writing,
revisions and downloads. Reopening a saved draft restores its original language.
**New draft** returns the settings to their defaults, including English.

The workspace, progress and review controls stay in English. Article headings,
prose, reference headings and generated disclosure notices use the selected
language. Source titles and quotations can remain in their original language.
Specify a regional preference such as Brazilian Portuguese or Spanish from Spain
in **Additional direction** when it matters to your reader.

Language selection is a writing instruction, not a guarantee of fluency from
every model. Other-language generated drafts remain **Needs review**: check
grammar, regional wording and whether translated claims match their evidence.
This warning alone does not cause extra model rewrite calls. The free router
can choose different models, so quality may vary; see the
[official router description](https://openrouter.ai/docs/guides/routing/routers/free-router).

The local fixture profile shows repetitive, translated demonstration paragraphs
with a synthetic disclosure. It does not translate arbitrary topics or measure
live model quality. It preserves the topic as you entered it. Downloads use UTF-8,
preserving characters such as `ğ`, `ñ`, `ã`, `ü` and `ß`.

### Research modes

- **Required** always gathers supporting evidence.
- **Automatic** researches when the brief contains current, numeric, named-product,
  legal, medical, financial or otherwise source-dependent language.
- **Off** is only for clear creative or general explanatory briefs. Choosing Off
  for a source-dependent topic shows a warning; the server rejects the brief with
  an explanation and a correlation reference if it is still submitted. This is a
  deliberate boundary, not a model judgement.

While a submission is in flight, retrying the same brief reuses one durable run
through an idempotency key. It does not create a duplicate draft. After the run
is accepted, a new submission of the same brief is a new run.

## Watch a run

The draft panel shows `Queued`, `Writing`, `Waiting to retry`, `Cancelling`,
`Needs review`, `Generated`, `Cancelled` or `Could not finish`. Progress is
factual: sections completed, sources recorded and claims needing attention. There
is no fabricated ETA.

A stage list shows how far the run has reached — **Brief accepted → Research →
Plan → Sections → Review**. Completed stages have a check and the current stage
is highlighted. The progress summary shows counts such as `2/2` sections or
`1 source`. A failed or cancelled run marks the stage where it stopped. While a
draft is in **Needs review**, the Review stage remains open. Saving your edits
creates an unassessed revision; it does not approve the article.

- Closing the tab does not stop the run. Reopen it from **Recent drafts**.
- A dropped live connection reconnects and reconciles from the durable run; it
  does not duplicate sections.
- **Cancel run** is available while the run is non-terminal. It reaches
  `Cancelled` and hides the review and article panes.

## Review evidence

When a draft reaches **Needs review**, check:

- **Research limitations** — shown when research was partial or unavailable.
- **Review findings** — errors and warnings such as unsupported claims or a
  synthetic-fixture notice.
- **Claims** — each cited factual sentence with its assessment, reason and stored
  evidence excerpt.
- **Sources** — provider, retrieval time and extraction state.

The fixture profile deliberately produces two unsupported claims and a
`synthetic_fixture` warning. That is expected: the fixture exercises the review
gate and never claims factual approval.

## Edit and revisions

The draft opens on a reading canvas, with editorial review, claim support,
sources and saved revisions in **Behind the draft**. Expand the evidence groups
you want to inspect. **Focus mode** gives the article more room and hides the
navigation and evidence inspector; **Exit focus mode** brings them back. On a
small screen, the inspector appears below the article.

Switch to **Edit Markdown**, change the article and choose **Save new revision**.
Human edits:

- create a new immutable revision;
- are marked **unassessed**;
- never inherit an earlier approval.

If the article changed while you were editing, the app preserves your text and
offers **Reload latest draft**, **Save my text on the latest draft** or **Copy my
text**. The revision chooser lists generated and human revisions with type, review
status and time; selecting one previews that exact immutable revision.
Leaving unsaved edits for a different draft, a new brief or a saved revision asks
for confirmation. Opening the library keeps the current edit in memory; return
with **Writing studio**. Reloading the page can lose unsaved text, so save before
closing it.

Raw HTML, images, external links and unsafe URL schemes are rejected in generated
Markdown. The preview uses text nodes only.

## Download

**Download Markdown** downloads the selected revision, not a newer or older one.
The page displays its SHA-256 next to a **Copy** button that places the hash
characters (without the `SHA-256` prefix) on the clipboard. To verify a download:

```powershell
Get-FileHash .\draft.md -Algorithm SHA256
```

or on Linux:

```bash
sha256sum draft.md
```

The computed hash must match the displayed hash exactly.

## History, reopening and deletion

The sidebar's **Recent drafts** lists up to six recent durable runs with a state chip and relative time
(such as “Needs review · 1 h ago”); the list refreshes when the selected run
reaches a terminal state, so completed drafts are never shown as queued.
Select one to recover its current revision and progress without regenerating it.
Open **Draft library** for draft cards, search and status filters. Search applies
to the recent drafts currently loaded; **Load more drafts** includes older pages
in the search. The count and summary show how many drafts are loaded rather than
claiming to count unloaded records. The refresh button reloads the latest page.

**Back to studio** opens the selected run's original brief for a new attempt.
**New draft** clears the brief. Neither action removes a saved draft.

**Delete run** asks for confirmation, then removes the run from history and clears
the saved run id. Deletion is a durable tombstone: the run is no longer readable,
and retention removes it (including its checkpoint thread) after
`DELETED_RUN_GRACE_HOURS` (24 hours by default). A deleted run's idempotency key
returns `410` rather than silently regenerating it.

Each local owner sees only their own runs. The local profile is a single-owner
development identity; it is not a multi-tenant account system.

## Cost, quotas and privacy

- The fixture profile calls no paid provider and uses zero budgets.
- `MODEL_POLICY=free_only` is the default. Budgeted profiles enforce
  `RUN_BUDGET_USD` and `USER_DAILY_BUDGET_USD` in the worker, not in the browser.
- `MAX_USER_NONTERMINAL_RUNS` (2 by default) limits concurrent non-terminal runs.
  When the queue or quota is full, the app shows a retryable `429` message.
- Provider credentials and database URLs are never sent to the browser.
- Local logs redact bearer tokens, cookies, API keys, passwords and DSNs.

## Accessibility

- On small screens, **Open navigation** reveals the sidebar. Choose a destination,
  click outside it or press Escape to close it.
- Ctrl+K (Command+K on macOS) opens and focuses library search. Ctrl+Enter
  (Command+Enter) submits the brief while the composer is open.
- The skip link moves focus to the main content.
- The Preview/Edit tabs use arrow keys, Home and End.
- Form controls have programmatic labels; status changes are announced; errors
  move focus to the error region.
- The stage list marks only the reachable stage with `aria-current="step"`.
- Light and dark themes use a shared text-contrast palette; the theme toggle is
  an accessible button (`aria-pressed`) and respects `prefers-color-scheme` until
  changed.
- Reduced-motion preferences are respected, including the stage pulse and loading
  animation.
- Long unbroken text does not create horizontal page overflow.

A manual screen-reader pass is still recommended before a public release.

The interface bundles [Manrope](https://github.com/google/fonts/tree/main/ofl/manrope)
under the SIL Open Font License and [Heroicons](https://github.com/tailwindlabs/heroicons)
under the MIT License. Their license notices ship with the local assets; no
external font or icon service is contacted when you use the app.

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| “Your session expired. Reload the page…” (`401`) | The local session ended or the process restarted. | Reload the page. |
| “Reload the page before trying this action.” (`403`) | CSRF token is stale or the request came from another origin. | Reload; use the app's own origin. |
| “That draft is no longer available.” (`404`) | The run does not exist or belongs to another owner. | Check Recent drafts. |
| “This draft was deleted. Start a new one.” (`410`) | The run is a deletion tombstone. | Start a new brief. |
| “The article changed while you were editing…” (`409`) | A newer revision exists. | Reload or save your text on the latest draft. |
| “The brief is too large…” (`413`) | Request body exceeds the configured limit. | Shorten the brief. |
| “You have reached the current run limit…” (`429`) | Too many non-terminal runs or queue full. | Wait, cancel or delete a run. |
| “The service is temporarily unavailable…” (`503`) | Database or provider dependency is unavailable. | Retry after the service recovers; saved work is durable. |

## Boundaries

The local profile binds to loopback and must not be exposed publicly. Hosted OIDC,
live provider qualification and human factual review are separate gates; see the
root [README](../README.md#boundaries).
