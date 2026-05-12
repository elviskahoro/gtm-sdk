# Octolens mentions → Attio custom object

**Status:** proposed
**Date:** 2026-05-12
**Owner:** elvis
**Branch:** `claude/attio-mentions-research-EgbO4`

---

## Implementation prompt

> Paste the block below into a fresh agent session. It is self-contained: it does not assume prior conversation state.

---

You are working in `/home/user/gtm-sdk`, a Python (uv-managed) repository that wraps several external APIs and routes webhook traffic into them. Read `AGENTS.md` / `CLAUDE.md` first — the code-placement rules are strict.

### Goal

Enable the currently-disabled Octolens → Attio export path by modeling each Octolens mention as a record in a **new Attio custom object** named `octolens_mentions`. The webhook handler at `src/octolens/webhook/mention.py` must stop returning `attio_is_valid_webhook() == False` and start emitting an upsert operation per mention.

### Why a custom object (not Notes, not a List, not People)

- Octolens payloads have **no email and no company domain** for the author, so the existing "AddNote on a parent People/Companies record" pattern cannot resolve a parent. That is the explicit reason the path is currently disabled (`src/octolens/webhook/mention.py:71-78`).
- Mentions are first-class entities with their own lifecycle (`mention_created`, `mention_updated`), their own queryable dimensions (sentiment, relevance, source, keywords), and their own workflow (triage). Notes lack typed/filterable attributes, and Attio's note-update path in this repo is delete+recreate (`libs/attio/notes.py:102-118`) — not idempotent.
- Lists organize existing records; they do not replace the need for an underlying record.
- The Attio workspace is on a plan that supports custom objects — do not add plan-tier guards.

### Repo context — what's already wrapped

`libs/attio/` wraps the Attio Python SDK. Existing surface:

- `libs/attio/people.py`, `companies.py`, `meetings.py`, `notes.py` — per-object helpers (`upsert_*`, `search_*`, `add_*`, `update_*`).
- `libs/attio/attributes.py` — `create_companies_attribute(...)`. Calls `client.attributes.post_v2_target_identifier_attributes(target="objects", identifier="companies", ...)`. The target object is hardcoded to `"companies"`; generalize it.
- `libs/attio/client.py` — `get_client()` returns an SDK client; reads `ATTIO_API_KEY`.
- `libs/attio/sdk_boundary.py` — builders for record/note request payloads.
- `libs/attio/models.py`, `contracts.py`, `errors.py`, `values.py` — typed inputs/results plus a `ReliabilityEnvelope` return shape used by all public helpers. Match this shape.

`src/attio/`:
- `ops.py` — typed op vocabulary (`UpsertPerson`, `UpsertCompany`, `UpsertMeeting`, `AddNote`, plus `PersonRef`/`CompanyRef`/`MeetingRef`). Add `UpsertMention` here.
- `export.py` — dispatcher (`src/attio/export.py:287-326`) that executes ops fail-fast and maintains a `LookupTable` for cross-op references. Add a handler for `UpsertMention`.

`src/octolens/webhook/mention.py`:
- Subclasses `libs.octolens.Webhook`. Implements an ETL contract that writes to GCS and an Attio contract that currently no-ops. Flip `attio_is_valid_webhook` to True and implement `attio_get_operations`.

`libs/octolens/models.py` (lines 25-127) — the `Mention` Pydantic model. Use the field names there as the source of truth.

Sample payloads in `api/samples/octolens.mention.created.{twitter,reddit,bluesky,hackernews,dev,podcasts}.redacted.json` — use these as test fixtures.

### Target schema — `octolens_mentions` custom object

`api_slug`: `octolens_mentions` · `singular_noun`: `Octolens mention` · `plural_noun`: `Octolens mentions`

| api_slug | type | is_unique | Source on `Mention` |
|---|---|---|---|
| `mention_url` | text | ✅ | `url` — **matching attribute** for the assert endpoint |
| `source_platform` | select (twitter, reddit, bluesky, hackernews, dev, podcasts) | | `source` |
| `source_id` | text | | `source_id` |
| `mention_title` | text | | `title` |
| `mention_body` | text (multiline) | | `body` |
| `mention_timestamp` | timestamp | | `timestamp` |
| `author_handle` | text | | `author` |
| `author_profile_url` | text | | `author_profile_link` |
| `author_avatar_url` | text | | `author_avatar_url` |
| `relevance` | select (high, medium, low) | | `relevance_score` |
| `relevance_comment` | text | | `relevance_comment` |
| `primary_keyword` | text | | `keyword` |
| `keywords` | text (comma-joined) | | `keywords[]` — text not multi-select because keyword cardinality is open-ended |
| `octolens_tags` | multi-select | | `tags[]` |
| `sentiment` | select (Positive, Neutral, Negative) | | `sentiment_label` |
| `language` | text | | `language` |
| `subreddit` | text | | `subreddit` |
| `view_name` | text | | `view_name` |
| `bookmarked` | checkbox | | `bookmarked` |
| `image_url` | text | | `image_url` |
| `triage_status` | status (New → Reviewing → Responded → Ignored) | | **Attio-owned, never overwritten by webhook** |
| `related_person` | record-reference → people | | nullable; populate later when handle→person resolution lands |
| `related_company` | record-reference → companies | | nullable |
| `last_action` | select (mention_created, mention_updated) | | `Webhook.action` |

**Critical invariant:** `triage_status` and `related_person` / `related_company` are human/CRM-owned. The webhook must never include them in update payloads, only in create payloads (and even there, leave them unset). Implementation: separate "always-write" and "create-only" field sets in the values builder.

### Endpoints to use

- Object creation: `POST /v2/objects` — body `{ "data": { "api_slug": "octolens_mentions", "singular_noun": "...", "plural_noun": "..." } }`.
- Attribute creation: `POST /v2/objects/octolens_mentions/attributes` per attribute. Mirror the payload shape in `libs/attio/attributes.py:31-40`.
- Idempotent upsert: `PUT /v2/objects/octolens_mentions/records?matching_attribute=mention_url` (the "assert a record" endpoint). The SDK method is reachable on `client.records`; confirm the exact name from `libs/attio/sdk_boundary.py` and the installed `attio` package version (currently 0.22.8 per `pyproject.toml`).

### Deliverables

1. **`libs/attio/objects.py`** — new module. Functions:
   - `create_object(api_slug, singular_noun, plural_noun, *, apply: bool) -> ObjectCreateResult` — idempotent (check existence first via `client.objects.get_v2_objects_object(...)` or list).
2. **Generalize `libs/attio/attributes.py`** — replace the hardcoded `"companies"` target with a parameter. Keep `create_companies_attribute` as a thin wrapper for backwards compatibility, or migrate callers. (Search the repo for callers before deciding.)
3. **`libs/attio/mentions.py`** — new module. Functions:
   - `upsert_mention(input: MentionInput) -> ReliabilityEnvelope` calling the assert endpoint with `matching_attribute="mention_url"`. Follow the existing envelope/warnings pattern in `companies.py`.
   - A `MentionInput` Pydantic model added to `libs/attio/models.py`.
4. **`libs/attio/values.py`** — new builders `build_create_mention_values()` and `build_update_mention_values()`. The update builder MUST NOT emit `triage_status`, `related_person`, `related_company`. The create builder MUST also leave those unset.
5. **`src/attio/ops.py`** — add `UpsertMention` op + necessary discriminated-union wiring.
6. **`src/attio/export.py`** — add a handler that maps `UpsertMention` → `libs.attio.mentions.upsert_mention(...)`.
7. **`src/octolens/webhook/mention.py`**:
   - `attio_is_valid_webhook()` → `True` when `self.action in VALID_ACTIONS` (same set as ETL).
   - `attio_get_operations()` → `[UpsertMention(...)]` built from `self.data`.
   - Remove or rewrite the `attio_get_invalid_webhook_error_msg` body to reflect the new behavior.
8. **Bootstrap CLI** — extend an existing Typer subapp under `cli/` (likely `cli/attio/`) with a `bootstrap-octolens-mentions` command that, in order:
   - Asserts the `octolens_mentions` object exists (creates if not).
   - Asserts every attribute exists with the right type/config (creates if not).
   - Has a `--preview` / `--apply` toggle, matching the existing `apply: bool` convention in `attributes.py`.
   - Is idempotent — safe to re-run.
9. **Tests** in `tests/` mirroring source layout:
   - `tests/libs/attio/test_mentions.py` — happy path + `mention_updated` re-delivery (same URL, mutated `bookmarked`) hits the assert endpoint with the right matching attribute and body.
   - `tests/src/octolens/webhook/test_mention_attio.py` — feed each of the 6 redacted sample payloads through and assert the resulting `UpsertMention` op shape.
   - Mock the SDK boundary, do not hit live Attio.
10. **`CHANGELOG.md`** — one entry describing the new export path (per the repo's "significant changes → CHANGELOG.md" rule).

### Hard rules (from `AGENTS.md`)

- No cross-lib imports. `libs/attio/mentions.py` cannot import from `libs/octolens/`; mapping payload → `MentionInput` lives in `src/octolens/webhook/mention.py` or `src/attio/`, not in `libs/`.
- No orchestration in `libs/`. Multi-step flows (e.g. "ensure object exists then upsert record") go in `src/` or `cli/`.
- New top-level package? Not needed here — everything lands under existing `libs/`, `src/`, `cli/`, `tests/`.
- Package manager: `uv`. Never `pip`.
- Path anchoring: any new bootstrap script that reads/writes files must anchor on `Path(__file__).resolve().parent`.
- Branch: stay on `claude/attio-mentions-research-EgbO4` per the runtime task; do not rename despite the `agent/<slug>` rule in `AGENTS.md` (the runtime instruction takes precedence).

### Edge cases to handle

- **Webhook re-delivery / `mention_updated`** — `PUT` with `matching_attribute=mention_url` covers both. Verify with a test that fires the same `mention_created` twice and asserts a single record (no duplicate).
- **`triage_status` clobbering** — see invariant above. Cover with a test that pre-seeds a record with `triage_status=Responded`, fires `mention_updated`, and asserts `triage_status` is unchanged.
- **Missing optional fields** (e.g. `subreddit` is only set for Reddit, `image_url` may be empty) — the values builder must omit them rather than send empty strings. Reuse the optional-fallback pattern from `libs/attio/people.py:270-336` only if Attio rejects unknown empties; otherwise just skip.
- **Open-ended `keywords[]`** — represent as comma-joined text; do not try to dynamically create select options at runtime.
- **`tags[]` cardinality** — bootstrap should pre-create the known tag options (`competitor_mention`, `industry_insights`, plus anything else observed in `api/samples/`). If an unknown tag arrives at runtime, fall back to dropping it with a `WarningEntry` of code `attio_unknown_octolens_tag` rather than failing the whole upsert.
- **Volume** — do not log or trace per-mention at INFO level; reuse the no-op telemetry pattern in `libs/telemetry.py`.

### Acceptance criteria

- [ ] Running the bootstrap CLI against a clean workspace creates the object and every attribute; running it again is a no-op.
- [ ] All 6 redacted sample payloads produce a valid `UpsertMention` op and a successful (mocked) assert call.
- [ ] Replaying any sample payload a second time updates rather than duplicates.
- [ ] A payload with `action=mention_updated` does not emit `triage_status`, `related_person`, or `related_company` in its values.
- [ ] `uv run pytest` passes.
- [ ] `attio_is_valid_webhook()` returns True for `mention_created` and `mention_updated`, False otherwise.
- [ ] `CHANGELOG.md` updated.

### Out of scope (do not do)

- LinkedIn URL → Person resolution. Leave `related_person` / `related_company` perpetually null for now; that's a follow-up.
- Bulk backfill of historical mentions from GCS. The webhook handles new mentions only.
- A list view / saved view inside Attio. The CRM user creates those manually after the object exists.
- Any change to the ETL → GCS path. That keeps working unchanged.

---

## Open questions

- Confirm the exact SDK method name for the assert endpoint on `attio==0.22.8` — likely `client.records.put_v2_objects_object_records(...)` but verify against the installed package.
- Should `keywords` actually be `multi-select` with a dynamic option-create step on first-sight? Default proposal is text; revisit if the GTM team wants filterable keyword segments inside Attio.
- Should `last_action` exist at all? It's debug-only; happy to drop if the team prefers a leaner schema.
