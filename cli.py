"""Command-line entry point for the Google Maps Takeout feasibility POC."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .matcher import (
    GooglePlacesCandidateProvider,
    PlaceMatcher,
    summarize_matches,
)
from .models import MatchResult, SourceReview
from .parser import TakeoutParseError, parse_takeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pocs.google_maps_takeout",
        description=(
            "Parse a Google Maps Takeout reviews export and optionally preview "
            "canonical Google Place matches. This POC never writes to Recz."
        ),
    )
    parser.add_argument(
        "input", type=Path, help="Takeout ZIP, directory, JSON, or GeoJSON"
    )
    parser.add_argument(
        "--include-drafts",
        action="store_true",
        help="include unpublished draft reviews in the preview",
    )
    parser.add_argument(
        "--live-match",
        action="store_true",
        help="call Places API (New) to preview canonical place matches",
    )
    parser.add_argument(
        "--max-live-lookups",
        type=_positive_int,
        default=100,
        help="hard cap on Places API calls (default: 100)",
    )
    parser.add_argument(
        "--language-code",
        type=_language_code,
        help="BCP-47 language preference for live Places results (for example, hi)",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="limit records shown and matched; parsing counters still cover the full input",
    )
    parser.add_argument(
        "--output", type=Path, help="write the JSON report with mode 0600"
    )
    parser.add_argument(
        "--include-match-audit",
        action="store_true",
        help="include source place hints, candidate IDs, and audit links in --output",
    )
    parser.add_argument(
        "--include-sensitive-preview",
        action="store_true",
        help="include review text, place hints, URLs, and source paths in --output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.include_sensitive_preview and not args.output:
        raise SystemExit("--include-sensitive-preview requires --output")
    if args.include_match_audit and not args.output:
        raise SystemExit("--include-match-audit requires --output")
    if args.include_match_audit and not args.live_match:
        raise SystemExit("--include-match-audit requires --live-match")
    if args.live_match and not os.getenv("GOOGLE_PLACES_API_KEY"):
        raise SystemExit("--live-match requires GOOGLE_PLACES_API_KEY")

    try:
        preview = parse_takeout(args.input, include_drafts=args.include_drafts)
    except TakeoutParseError as exc:
        raise SystemExit(f"Cannot parse Takeout export: {exc}") from exc

    selected = preview.records[: args.limit] if args.limit else preview.records
    matches: list[MatchResult] = []
    api_calls = 0
    if args.live_match:
        key = os.environ["GOOGLE_PLACES_API_KEY"]
        provider = GooglePlacesCandidateProvider(
            key,
            max_api_calls=args.max_live_lookups,
            language_code=args.language_code,
        )
        matches = asyncio.run(_match_records(selected, provider))
        api_calls = provider.api_calls

    parser_report = preview.as_dict(include_sensitive=args.include_sensitive_preview)
    parser_report["records"] = [
        record.as_dict(include_sensitive=args.include_sensitive_preview)
        for record in selected
    ]
    parser_report["summary"]["records_shown"] = len(selected)
    parser_route_passed = preview.feature_collections_seen > 0
    report: dict[str, Any] = {
        "poc": "google_maps_takeout",
        "mode": "dry_run",
        "writes_performed": False,
        "feasibility": {
            "parser_route": "pass" if parser_route_passed else "fail",
            "matching_route": "measurement_only" if args.live_match else "not_run",
            "ready_for_controlled_real_export_test": parser_route_passed,
        },
        "parser": parser_report,
        "matching": {
            "enabled": args.live_match,
            "summary": summarize_matches(matches, api_calls=api_calls)
            if args.live_match
            else None,
            "results": [
                _match_result_dict(
                    match,
                    record,
                    include_sensitive=args.include_sensitive_preview,
                    include_audit=(
                        args.include_match_audit or args.include_sensitive_preview
                    ),
                )
                for match, record in zip(matches, selected)
            ],
        },
    }

    if args.output:
        _write_private_json(args.output, report)

    stdout_report = {
        **report,
        "parser": preview.as_dict(include_sensitive=False),
        "matching": {
            **report["matching"],
            "results": [
                _match_result_dict(
                    match,
                    record,
                    include_sensitive=False,
                    include_audit=False,
                )
                for match, record in zip(matches, selected)
            ],
        },
    }
    stdout_report["parser"]["records"] = [
        record.as_dict(include_sensitive=False) for record in selected
    ]
    stdout_report["parser"]["summary"]["records_shown"] = len(selected)
    if args.output:
        stdout_report["report_written"] = True
    print(json.dumps(stdout_report, indent=2, sort_keys=True))
    return 0 if parser_route_passed else 2


async def _match_records(
    records: list[SourceReview],
    provider: GooglePlacesCandidateProvider,
) -> list[MatchResult]:
    matcher = PlaceMatcher(provider)
    cache: dict[str, MatchResult] = {}
    results: list[MatchResult] = []
    for record in records:
        if record.source_locator in cache:
            results.append(cache[record.source_locator])
            continue
        try:
            result = await matcher.match(record)
        except Exception:
            result = MatchResult(
                source_locator=record.source_locator,
                status="error",
                method="places_api",
                error_code="unexpected_match_failure",
            )
        cache[record.source_locator] = result
        results.append(result)
    return results


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _match_result_dict(
    match: MatchResult,
    review: SourceReview,
    *,
    include_sensitive: bool,
    include_audit: bool,
) -> dict:
    result = match.as_dict(
        include_sensitive=include_sensitive, include_audit=include_audit
    )
    if include_audit:
        query = review.place.name or review.place.address or "Place"
        result["source_place_hint"] = {
            "name": review.place.name,
            "address": review.place.address,
            "country_code": review.place.country_code,
        }
        result["candidate_audit_links"] = [
            {
                "place_id": place_id,
                "google_maps_url": "https://www.google.com/maps/search/?"
                + urlencode(
                    {
                        "api": "1",
                        "query": query,
                        "query_place_id": place_id,
                    }
                ),
            }
            for place_id in match.candidate_place_ids
        ]
    return result


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _language_code(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise argparse.ArgumentTypeError("must be a BCP-47-style language code")
    return value
