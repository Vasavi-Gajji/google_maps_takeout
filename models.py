"""Data models for the Google Maps Takeout feasibility POC."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class PlaceHint:
    """Place information supplied by the user's Takeout export."""

    name: str | None = None
    address: str | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class SourceReview:
    """A normalized, still-private review signal from Google Maps."""

    source_locator: str
    content_fingerprint: str
    state: Literal["published", "draft"]
    rating_value: int | None
    review_text: str | None
    source_modified_at: str | None
    google_maps_url: str | None
    place: PlaceHint
    source_file: str
    source_index: int
    rating_scale: int = 5
    source: str = "google_maps"
    warnings: tuple[str, ...] = ()

    def as_dict(self, *, include_sensitive: bool = False) -> dict:
        """Return a report-safe representation.

        Review text, URLs, exact place hints, and source paths are omitted unless
        the caller explicitly opts in. This method is for a feasibility report,
        not the eventual import contract.
        """
        result = {
            "source": self.source,
            "source_locator": self.source_locator,
            "content_fingerprint": self.content_fingerprint,
            "state": self.state,
            "rating_value": self.rating_value,
            "rating_scale": self.rating_scale,
            "has_source_modified_at": bool(self.source_modified_at),
            "has_review_text": bool(self.review_text),
            "has_coordinates": (
                self.place.latitude is not None and self.place.longitude is not None
            ),
            "warnings": list(self.warnings),
        }
        if include_sensitive:
            result.update(
                {
                    "review_text": self.review_text,
                    "source_modified_at": self.source_modified_at,
                    "google_maps_url": self.google_maps_url,
                    "place": asdict(self.place),
                    "source_file": self.source_file,
                    "source_index": self.source_index,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """A diagnostic that deliberately contains no imported user content."""

    code: str
    source_file: str
    source_index: int | None = None

    def as_dict(self, *, include_sensitive: bool = False) -> dict:
        result = {"code": self.code, "source_index": self.source_index}
        if include_sensitive:
            result["source_file"] = self.source_file
        return result


@dataclass(slots=True)
class ImportPreview:
    """Parser output plus non-sensitive feasibility counters."""

    records: list[SourceReview] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    files_scanned: int = 0
    feature_collections_seen: int = 0
    features_seen: int = 0
    drafts_skipped: int = 0
    duplicates_skipped: int = 0
    invalid_skipped: int = 0

    @property
    def published_records(self) -> int:
        return sum(record.state == "published" for record in self.records)

    @property
    def draft_records(self) -> int:
        return sum(record.state == "draft" for record in self.records)

    def summary(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "feature_collections_seen": self.feature_collections_seen,
            "features_seen": self.features_seen,
            "importable_records": len(self.records),
            "published_records": self.published_records,
            "draft_records": self.draft_records,
            "drafts_skipped": self.drafts_skipped,
            "duplicates_skipped": self.duplicates_skipped,
            "invalid_skipped": self.invalid_skipped,
            "issues": len(self.issues),
        }

    def as_dict(self, *, include_sensitive: bool = False) -> dict:
        return {
            "summary": self.summary(),
            "records": [
                record.as_dict(include_sensitive=include_sensitive)
                for record in self.records
            ],
            "issues": [
                issue.as_dict(include_sensitive=include_sensitive)
                for issue in self.issues
            ],
        }


MatchStatus = Literal[
    "exact",
    "automatic",
    "needs_review",
    "unmatched",
    "ineligible",
    "not_attempted",
    "error",
]


@dataclass(frozen=True, slots=True)
class PlaceCandidate:
    """Ephemeral Google Places candidate; only ``place_id`` should be retained."""

    place_id: str
    name: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    country_code: str | None = None
    types: tuple[str, ...] = ()
    business_status: str | None = None
    moved_place_id: str | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """One review's proposed mapping to a canonical Google Place ID."""

    source_locator: str
    status: MatchStatus
    place_id: str | None = None
    confidence: float | None = None
    method: str | None = None
    runner_up_margin: float | None = None
    candidate_count: int = 0
    name_score: float | None = None
    address_score: float | None = None
    geo_score: float | None = None
    address_number_conflict: bool = False
    runner_up_confidence: float | None = None
    recz_category_supported: bool | None = None
    error_code: str | None = None
    runner_up_place_id: str | None = None
    candidate_place_ids: tuple[str, ...] = ()

    def as_dict(
        self, *, include_sensitive: bool = False, include_audit: bool = False
    ) -> dict:
        result = {
            "status": self.status,
            "confidence": self.confidence,
            "method": self.method,
            "runner_up_margin": self.runner_up_margin,
            "candidate_count": self.candidate_count,
            "name_score": self.name_score,
            "address_score": self.address_score,
            "geo_score": self.geo_score,
            "address_number_conflict": self.address_number_conflict,
            "runner_up_confidence": self.runner_up_confidence,
            "recz_category_supported": self.recz_category_supported,
            "error_code": self.error_code,
        }
        if include_sensitive or include_audit:
            result.update(
                {
                    "source_locator": self.source_locator,
                    "place_id": self.place_id,
                    "runner_up_place_id": self.runner_up_place_id,
                    "candidate_place_ids": list(self.candidate_place_ids),
                }
            )
        return result
