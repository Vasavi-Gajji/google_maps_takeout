# Google Maps Takeout import POC

This read-only proof of concept tests whether Recz can parse a user's Google Maps reviews and match the reviewed places to Google Places. It never calls a Recz API, writes to a Recz database, or creates a recommendation or wall post.

Parse-only mode is the default and is fully local. Live matching is opt-in and sends place search hints to Google Places API (New), but still makes no Recz writes.

## What the POC accepts

`INPUT` may be:

- A Google Takeout `.zip` archive.
- An extracted Takeout directory.
- A `Reviews.json`, `.json`, or `.geojson` file.

The parser discovers review GeoJSON by content rather than relying only on an English filename or folder name. It imports published 1–5 ratings and optional published review text. Draft-only records are skipped unless `--include-drafts` is supplied.

Google's current schema describes a GeoJSON Feature with Point coordinates and review properties including published/draft ratings and text, place metadata, a Maps URL, question answers, and a `date`. See the official [Maps (your places) schema](https://developers.google.com/data-portability/schema-reference/local_actions).

Important: Google's `date` is the last time the contribution was created **or modified**. It is not a reliable visit date. Coordinates are `[longitude, latitude]`; Google documents `[0, 0]` as unknown.

## Get the smallest useful Takeout archive

1. Open [Google Takeout](https://takeout.google.com/).
2. Select **Deselect all**.
3. Select only **Maps (your places)**, described by Google as records of starred places and place reviews.
4. If Google offers a per-product selection, include Reviews and exclude data not needed for this test.
5. Choose a one-time export and `.zip` as the file type.
6. Download the archive when Google emails the link, then pass the ZIP directly to this POC.

Do not select a full-account export. It can contain email, location history, photos, account data, and other information unrelated to this test. Google documents archive creation, ZIP/TGZ choices, split archives, and third-party uploads in [How to download your Google data](https://support.google.com/accounts/answer/3024190?hl=en).

Google's direct Data Portability integration is available only in its listed countries and regions; Google explicitly recommends Takeout plus a manual third-party upload elsewhere. That makes the file route the appropriate first POC for India. See [Share a copy of your data with a third party](https://support.google.com/accounts/answer/14452558?hl=en).

## Prerequisites

- Python 3.10 or newer.
- The `httpx` package. `cli.py` imports `matcher.py` unconditionally, so `httpx` must be installed even for a parse-only run:

  ```bash
  python -m pip install httpx
  ```

  ```powershell
  python -m pip install httpx
  ```

This folder ships without a `.venv` or `requirements.txt`; install `httpx` into whatever interpreter runs the commands below.

## Run locally

The commands below assume your terminal's working directory is the **parent** of this `google_maps_takeout` folder — that is, one level above this README. `python -m google_maps_takeout` needs that parent folder on `sys.path` so the folder resolves as a top-level package.

### 1. Verify the setup against the synthetic fixture

```bash
python -m google_maps_takeout google_maps_takeout/fixtures/reviews.geojson
```

```powershell
python -m google_maps_takeout .\google_maps_takeout\fixtures\reviews.geojson
```

Expected output (key fields shown):

```json
{
  "feasibility": {
    "parser_route": "pass",
    "matching_route": "not_run",
    "ready_for_controlled_real_export_test": true
  },
  "mode": "dry_run",
  "writes_performed": false,
  "parser": {
    "summary": {
      "feature_collections_seen": 1,
      "features_seen": 3,
      "importable_records": 2,
      "published_records": 2,
      "draft_records": 0,
      "drafts_skipped": 1,
      "duplicates_skipped": 0,
      "invalid_skipped": 0,
      "issues": 0
    }
  }
}
```

The fixture has three features: two published (one with coordinates, one with `[0, 0]`/unknown coordinates) and one draft-only. A correct run shows `importable_records: 2`, `drafts_skipped: 1`, `parser_route: "pass"`, and exits `0`.

### 2. Parse your own Takeout export (no network calls)

Place your Takeout `.zip`, extracted folder, `Reviews.json`, or `.geojson` file anywhere accessible, then point the POC at it:

```bash
python -m google_maps_takeout google_maps_takeout/your-takeout-export.zip
```

```powershell
python -m google_maps_takeout ".\google_maps_takeout\your-takeout-export.zip"
```

Expected output shape (one record shown; your actual values will differ):

```json
{
  "feasibility": { "parser_route": "pass", "matching_route": "not_run", "ready_for_controlled_real_export_test": true },
  "mode": "dry_run",
  "writes_performed": false,
  "parser": {
    "records": [
      {
        "source": "google_maps",
        "state": "published",
        "rating_value": 5,
        "rating_scale": 5,
        "has_review_text": true,
        "has_coordinates": true,
        "warnings": []
      }
    ],
    "summary": { "importable_records": 3, "published_records": 3, "draft_records": 0, "issues": 0 }
  }
}
```

- `parser_route: "pass"` and exit code `0` mean at least one review `FeatureCollection` was discovered; `"fail"` and exit code `2` mean nothing parseable was found.
- Standard output is **always sanitized** — no review text, URLs, exact place names/addresses, or source file paths, no matter which flags are set. Only `--output` combined with an explicit opt-in flag can add those fields, and only to that file.

### 3. Useful bounded options

```bash
python -m google_maps_takeout INPUT \
  --limit 25 \
  --include-drafts \
  --output /private/tmp/google-maps-import-report.json \
  --include-sensitive-preview
```

```powershell
python -m google_maps_takeout INPUT `
  --limit 25 `
  --include-drafts `
  --output C:\path\to\private\report.json `
  --include-sensitive-preview
```

- `--limit` bounds the number of staged records considered by the POC; parsing counters (`features_seen`, etc.) still cover the full input.
- `--include-drafts` includes unpublished draft data. Use only when intentionally testing draft handling.
- `--include-match-audit` writes source place hints, candidate Place IDs, and Google Maps audit links without exporting review text, timestamps, source paths, or exact coordinates.
- `--include-sensitive-preview` adds fields that the default report suppresses to the explicitly requested `--output` file. Standard output remains sanitized.
- **Windows note:** `--output` writes the file with mode `0600`, but `os.chmod`/`fchmod` do not restrict NTFS ACLs the way they do on POSIX — on Windows this only toggles the read-only attribute. Run `icacls` on the written file and you'll still see it readable by `SYSTEM`, `Administrators`, and your account. Treat the file as protected only by ordinary user-folder permissions on Windows, and delete it once you're done evaluating it.

The JSON output includes a machine-readable `feasibility.parser_route` verdict. A valid review collection returns exit code `0`; an input with no discoverable review collection returns `2` so an automated experiment cannot mistake an empty or malformed input for success.

All of these remain dry runs. The only optional local write is a report explicitly requested with `--output`.

## Live Google Places matching

Live matching tests the uncertain part of this route: resolving Takeout's place name, address, coordinates, and Maps URL to a durable Google Place ID. The Takeout schema does not document a Place ID or review ID, and a numeric `cid` or `0x...:0x...` Maps token must not be treated as a Google Place ID.

To enable live matching:

1. Create or choose a Google Cloud project with billing enabled.
2. Enable **Places API (New)**.
3. Create an API key and restrict it to Places API (New). Apply an appropriate server/IP restriction outside local development.
4. Set a quota and billing alert before testing a large archive.
5. Supply the key through the environment; never commit it or place it in a command-line flag.

### Supplying the key from a `.env` file

This POC reads `GOOGLE_PLACES_API_KEY` from the process environment — it does **not** load a `.env` file on its own. If you keep the key in a local `.env` (for example `GOOGLE_PLACES_API_KEY = your_key_here`, and add `.env` to `.gitignore`), load it into your shell first so it never appears on the command line or in shell history:

```bash
export GOOGLE_PLACES_API_KEY=$(grep GOOGLE_PLACES_API_KEY .env | cut -d '=' -f2- | xargs)
```

```powershell
$envLine = Get-Content .env | Select-String '^\s*GOOGLE_PLACES_API_KEY\s*=\s*(.+)$'
$env:GOOGLE_PLACES_API_KEY = $envLine.Matches[0].Groups[1].Value.Trim().Trim("'").Trim('"')
```

### Run with matching enabled

```bash
python -m google_maps_takeout /path/to/takeout.zip \
  --live-match \
  --language-code hi \
  --max-live-lookups 25 \
  --include-match-audit \
  --output /private/tmp/google-maps-import-report.json
```

```powershell
python -m google_maps_takeout .\google_maps_takeout\your-takeout-export.zip `
  --live-match `
  --language-code en `
  --max-live-lookups 25 `
  --include-match-audit `
  --output C:\path\to\private\report.json
```

Set `--max-live-lookups` close to your actual eligible-record count as an explicit cost cap — the default is 100. `--max-live-lookups` is a hard cap on Places API requests and an explicit cost and privacy guardrail. Live matching can send a place name, address, country, and approximate coordinates from each eligible record to Google's Text Search endpoint. It does not send the review text.

Expected output shape (one result shown):

```json
{
  "feasibility": { "parser_route": "pass", "matching_route": "measurement_only", "ready_for_controlled_real_export_test": true },
  "matching": {
    "enabled": true,
    "results": [
      {
        "status": "automatic",
        "confidence": 0.96,
        "method": "text_search",
        "name_score": 1.0,
        "address_score": 0.82,
        "geo_score": 1.0,
        "candidate_count": 1,
        "error_code": null
      }
    ],
    "summary": {
      "algorithm_version": "gmaps-poc-v1",
      "records_considered": 3,
      "eligible_unique_places": 3,
      "attempted_unique_places": 3,
      "attempt_coverage_rate": 1.0,
      "high_confidence_matches": 3,
      "high_confidence_rate": 1.0,
      "places_api_calls": 3,
      "exact": 0,
      "automatic": 3,
      "needs_review": 0,
      "unmatched": 0,
      "ineligible": 0,
      "not_attempted": 0,
      "errors": 0
    }
  }
}
```

`status` is one of `exact`, `automatic`, `needs_review`, `unmatched`, `ineligible`, `not_attempted`, or `error` — see [Known limits](#known-limits) for when each applies. Stdout never includes `place_id`, candidate IDs, place names/addresses, or Maps links, regardless of `--live-match`; only `--include-match-audit` or `--include-sensitive-preview` add those, and only to the `--output` file.

Use `--language-code` when evaluating a non-English export so Google returns localized names where possible. The high-confidence rate is calculated over attempted eligible unique place locators, not raw review rows. Rows without a name, address, or trusted Place ID are `ineligible`; rows beyond the API-call cap are `not_attempted` and affect coverage, not match accuracy.

For an auditable evaluation, use the narrower `--include-match-audit` output. Each match then includes the selected and runner-up Place IDs, all candidate Place IDs, Google Maps audit links, and only the source name/address/country hints; it does not include review text, timestamps, source paths, or exact coordinates. The score evidence includes name, address, and geographic components plus address-number conflicts. Have a reviewer open those links in Google's attributed UI and label the selected ID as correct/incorrect/ambiguous. The POC intentionally does not persist Google-returned candidate names, addresses, or coordinates; those fields have stricter caching rules than Place IDs.

The matcher uses [Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search) with a minimal field mask. Fields needed to compare names, addresses, coordinates, and types trigger the **Places API Text Search Pro** SKU. Checked on 2026-08-25:

- Global billing includes 5,000 no-cost monthly Text Search Pro events, then starts at USD $32 per 1,000 events.
- Eligible India billing includes 35,000 no-cost monthly events, then starts at USD $9.60 per 1,000 events.
- If a trusted Maps URL contains the documented `query_place_id`, the POC first validates it with Place Details Pro. A missing ID can then fall back to Text Search, so both requests count toward the hard cap.
- Place Details Pro has the same respective no-cost thresholds, then starts at USD $17 per 1,000 globally or USD $5.10 per 1,000 for eligible India billing.
- A record without a trusted Place ID normally causes one Text Search request. This POC does not retry live requests, keeping the measured call count predictable.

Pricing and eligibility can change. Check Google's current [global pricing](https://developers.google.com/maps/billing-and-pricing/pricing) or [India pricing](https://developers.google.com/maps/billing-and-pricing/pricing-india) before running live matching.

Place IDs may be stored indefinitely, but caching and displaying other Places API content has additional restrictions and attribution requirements. A production implementation must follow the [Places API policies and attribution requirements](https://developers.google.com/maps/documentation/places/web-service/policies).

## Privacy and safety

- Run parse-only mode first. It reads local files and performs no external requests.
- Keep real Takeout files and generated reports out of Git, shared folders, logs, screenshots, and support tickets.
- Default output is intended to suppress review text and precise place data. Treat even the default report as personal data.
- `--include-sensitive-preview` is for local debugging with the user's informed consent.
- Live matching discloses place search hints to Google. It remains disabled unless `--live-match` is supplied.
- Use `/private/tmp` or another access-controlled temporary path for reports and delete them after evaluation.
- Draft reviews are unpublished content and are excluded by default.
- Imported data must remain private in any later Recz implementation. Publishing selected records must be a separate, explicit user action.
- Do not reuse Recz's existing add-post flow for an import; that flow can create wall-visible content.

This POC does not use the Data Portability API and therefore does not request an OAuth scope. A future direct integration would request the sensitive `maps.reviews` scope and would require Google verification, in-context consent, minimum necessary access, deletion controls, and compliance with Google's [Data Portability user data and developer policy](https://developers.google.com/data-portability/policy) and [scope classification](https://developers.google.com/data-portability/user-guide/scopes).

## Known limits

- It validates feasibility; it does not commit an import, update the Taste Engine, or publish posts.
- It targets Google Maps reviews, not Timeline, photos, custom Saved lists, starred places, or Maps activity.
- Google publishes key schema fields but not a versioned history of legacy Takeout layouts. Real exports across accounts and locales are still required.
- Optional fields can be absent. Rating-only reviews are valid, and `[0, 0]` means coordinates are unavailable.
- Review `date` is a creation-or-modification timestamp, not a visit timestamp.
- Google does not document a stable source review ID or Place ID in the export. Re-import deduplication must combine a deterministic source fingerprint with the resolved Place ID when available.
- Renamed, moved, closed, duplicate, and localized places can produce ambiguous matches. Low-confidence candidates must remain unresolved rather than being silently accepted.
- A Places response that points to a moved Place ID is always marked `needs_review`; the audit output includes both old and new IDs.
- Google Maps reviews include categories beyond restaurants. A product import must preserve staged records while applying an explicit Recz category policy.
- The POC does not reassemble multipart Takeout archives and does not accept `.tgz`; request a Maps-only ZIP instead.
- For upload-boundary safety, a JSON file is capped at 25 MiB, the input/archive at 200 MiB compressed, and ZIP expansion at 250 MiB. Oversized directory files fail the run rather than being silently omitted.
- Synthetic fixture names and URLs intentionally do not identify real businesses, so live matching the fixture is expected to return unresolved or irrelevant candidates and still incur requests.

## Go/no-go criteria

Test with at least three voluntary, access-controlled real Takeout exports spanning different account ages or locales. Never add those raw archives to the repository.

Proceed to an import-service design only if all of the following hold:

- At least 99% of valid review features are discovered and parsed.
- Every parsed published rating and review text is preserved exactly in normalized staging.
- Draft-only records are excluded by default and `[0, 0]` is never treated as a real location.
- Re-running the same input produces identical fingerprints and zero logical duplicates.
- At least 90% of eligible places receive a high-confidence match, with no known incorrect high-confidence matches in the evaluation sample.
- Every ambiguous match remains visibly unresolved for user confirmation.
- Live request counts respect `--max-live-lookups` and the measured cost agrees with the Google Cloud billing report.
- No Recz database, API, Taste Engine, or social-feed write occurs.
- No review text, address, coordinates, archive contents, or API key appears in application logs or error telemetry.

Stop or redesign the route if parsing loses valid ratings/reviews, matching silently chooses incorrect places, a re-run can duplicate records, sensitive data leaks into logs or public surfaces, or live matching cost cannot be bounded and explained before consent.
