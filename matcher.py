"""Conservative Google Places identity matching for imported reviews."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Protocol
from urllib.parse import quote

import httpx

from .models import MatchResult, PlaceCandidate, SourceReview
from .parser import extract_query_place_id

PLACES_BASE_URL = "https://places.googleapis.com/v1"
SEARCH_FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.addressComponents",
        "places.types",
        "places.businessStatus",
        "places.movedPlaceId",
    )
)
DETAILS_FIELD_MASK = SEARCH_FIELD_MASK.replace("places.", "")
MATCH_ALGORITHM_VERSION = "gmaps-poc-v1"

_FOOD_DRINK_TYPES = frozenset(
    {
        "restaurant",
        "cafe",
        "bar",
        "bakery",
        "meal_takeaway",
        "meal_delivery",
        "food_court",
        "ice_cream_shop",
        "coffee_shop",
    }
)


class CandidateProvider(Protocol):
    """Injectable provider used by the pure matching policy."""

    async def search_candidates(
        self, review: SourceReview, *, limit: int = 5
    ) -> list[PlaceCandidate]: ...

    async def get_candidate(self, place_id: str) -> PlaceCandidate | None: ...


class LiveLookupLimitError(RuntimeError):
    """Raised before a Places request would exceed the explicit cost cap."""


class GooglePlacesCandidateProvider:
    """Minimal Places API (New) adapter with no cache or Recz side effects."""

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
        max_api_calls: int | None = None,
        language_code: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("a Google Places API key is required")
        self._api_key = api_key
        self._client = http_client
        self._timeout = timeout_seconds
        self._max_api_calls = max_api_calls
        self._language_code = language_code
        self.api_calls = 0

    async def search_candidates(
        self, review: SourceReview, *, limit: int = 5
    ) -> list[PlaceCandidate]:
        query = ", ".join(
            value for value in (review.place.name, review.place.address) if value
        )
        if not query:
            return []
        body: dict = {"textQuery": query, "pageSize": max(1, min(limit, 20))}
        if review.place.latitude is not None and review.place.longitude is not None:
            body["locationBias"] = {
                "circle": {
                    "center": {
                        "latitude": _truncate(review.place.latitude),
                        "longitude": _truncate(review.place.longitude),
                    },
                    "radius": 1_000.0,
                }
            }
        country = (review.place.country_code or "").upper()
        if re.fullmatch(r"[A-Z]{2}", country):
            body["regionCode"] = country
        if self._language_code:
            body["languageCode"] = self._language_code
        payload = await self._request(
            "POST",
            "/places:searchText",
            field_mask=SEARCH_FIELD_MASK,
            json=body,
        )
        return [
            candidate
            for raw in payload.get("places", [])
            if isinstance(raw, dict) and (candidate := _candidate_from_api(raw))
        ]

    async def get_candidate(self, place_id: str) -> PlaceCandidate | None:
        payload = await self._request(
            "GET",
            f"/places/{quote(place_id, safe='')}",
            field_mask=DETAILS_FIELD_MASK,
            params={"languageCode": self._language_code}
            if self._language_code
            else None,
        )
        return _candidate_from_api(payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        field_mask: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask,
        }
        if self._max_api_calls is not None and self.api_calls >= self._max_api_calls:
            raise LiveLookupLimitError("live Places API call cap reached")
        self.api_calls += 1
        if self._client is not None:
            response = await self._client.request(
                method,
                f"{PLACES_BASE_URL}{path}",
                headers=headers,
                json=json,
                params=params,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    f"{PLACES_BASE_URL}{path}",
                    headers=headers,
                    json=json,
                    params=params,
                )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}


class PlaceMatcher:
    """Deterministic policy that refuses low-confidence or ambiguous matches."""

    def __init__(self, provider: CandidateProvider) -> None:
        self._provider = provider

    async def match(self, review: SourceReview) -> MatchResult:
        url_place_id = extract_query_place_id(review.google_maps_url)
        if url_place_id:
            try:
                candidate = await self._provider.get_candidate(url_place_id)
            except LiveLookupLimitError:
                return _not_attempted(review, "max_live_lookups_reached")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    candidate = None
                else:
                    return _error(review, "place_id_validation_failed")
            except (httpx.HTTPError, TimeoutError):
                return _error(review, "place_id_validation_failed")
            if candidate:
                scores = _score_candidate(review, candidate)
                contradictory = _identity_is_contradictory(review, candidate, scores)
                moved = bool(candidate.moved_place_id)
                return MatchResult(
                    source_locator=review.source_locator,
                    status="needs_review" if contradictory or moved else "exact",
                    place_id=candidate.moved_place_id or candidate.place_id,
                    confidence=round(scores.total, 4) if contradictory else 1.0,
                    method=(
                        "maps_url_moved_place" if moved else "maps_url_query_place_id"
                    ),
                    candidate_count=1,
                    name_score=_round_score(scores.name),
                    address_score=_round_score(scores.address),
                    geo_score=_round_score(scores.geo),
                    address_number_conflict=scores.address_number_conflict,
                    recz_category_supported=_is_supported_category(candidate.types),
                    candidate_place_ids=tuple(
                        dict.fromkeys(
                            place_id
                            for place_id in (
                                candidate.place_id,
                                candidate.moved_place_id,
                            )
                            if place_id
                        )
                    ),
                )

        if not review.place.name and not review.place.address:
            return MatchResult(
                source_locator=review.source_locator,
                status="ineligible",
                method="no_searchable_place_hints",
            )

        try:
            candidates = await self._provider.search_candidates(review, limit=5)
        except LiveLookupLimitError:
            return _not_attempted(review, "max_live_lookups_reached")
        except (httpx.HTTPError, TimeoutError):
            return _error(review, "places_search_failed")
        if not candidates:
            return MatchResult(
                source_locator=review.source_locator,
                status="unmatched",
                method="text_search",
                candidate_count=0,
            )

        scored = sorted(
            (
                (_score_candidate(review, candidate), candidate)
                for candidate in candidates
            ),
            key=lambda item: item[0].total,
            reverse=True,
        )
        top_score, top = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        second_score = runner[0].total if runner else 0.0
        margin = top_score.total - second_score
        evidence = (
            (top_score.name or 0.0) >= 0.85
            and ((top_score.address or 0.0) >= 0.85 or (top_score.geo or 0.0) >= 0.75)
            and not top_score.address_number_conflict
        )
        auto = top_score.total >= 0.90 and margin >= 0.08 and evidence
        supported = _is_supported_category(top.types)

        if top.moved_place_id:
            status = "needs_review"
        elif auto and top.business_status != "CLOSED_PERMANENTLY":
            status = "automatic"
        elif top_score.total >= 0.75:
            status = "needs_review"
        else:
            status = "unmatched"
        return MatchResult(
            source_locator=review.source_locator,
            status=status,
            place_id=(top.moved_place_id or top.place_id)
            if status != "unmatched"
            else None,
            confidence=round(top_score.total, 4),
            method="text_search_moved_place" if top.moved_place_id else "text_search",
            runner_up_margin=round(margin, 4),
            candidate_count=len(candidates),
            name_score=_round_score(top_score.name),
            address_score=_round_score(top_score.address),
            geo_score=_round_score(top_score.geo),
            address_number_conflict=top_score.address_number_conflict,
            runner_up_confidence=round(second_score, 4) if runner else None,
            recz_category_supported=supported,
            runner_up_place_id=(
                runner[1].moved_place_id or runner[1].place_id if runner else None
            ),
            candidate_place_ids=_audit_candidate_ids(scored),
        )


class _Scores:
    __slots__ = ("address", "address_number_conflict", "geo", "name", "total")

    def __init__(
        self,
        name: float | None,
        address: float | None,
        geo: float | None,
        total: float,
        *,
        address_number_conflict: bool,
    ) -> None:
        self.name = name
        self.address = address
        self.geo = geo
        self.total = total
        self.address_number_conflict = address_number_conflict


def _audit_candidate_ids(
    scored: Sequence[tuple[_Scores, PlaceCandidate]],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            place_id
            for _, candidate in scored
            for place_id in (candidate.place_id, candidate.moved_place_id)
            if place_id
        )
    )


def _score_candidate(review: SourceReview, candidate: PlaceCandidate) -> _Scores:
    name = _name_similarity(review.place.name, candidate.name)
    address = _token_similarity(review.place.address, candidate.address)
    geo = _geo_similarity(
        review.place.latitude,
        review.place.longitude,
        candidate.latitude,
        candidate.longitude,
    )
    components = ((name, 0.50), (address, 0.20), (geo, 0.30))
    available = [component for component in components if component[0] is not None]
    total = (
        sum(value * weight for value, weight in available)
        / sum(weight for _, weight in available)
        if available
        else 0.0
    )
    if (
        review.place.country_code
        and candidate.country_code
        and review.place.country_code.casefold() != candidate.country_code.casefold()
    ):
        total *= 0.5
    return _Scores(
        name,
        address,
        geo,
        total,
        address_number_conflict=_address_number_conflict(
            review.place.address, candidate.address
        ),
    )


def _identity_is_contradictory(
    review: SourceReview, candidate: PlaceCandidate, scores: _Scores
) -> bool:
    name_score = scores.name or 0.0
    address_score = scores.address or 0.0
    geo_score = scores.geo or 0.0
    if (
        review.place.country_code
        and candidate.country_code
        and review.place.country_code.casefold() != candidate.country_code.casefold()
    ):
        return True
    name_compared = bool(review.place.name and candidate.name)
    address_compared = bool(review.place.address and candidate.address)
    geo_compared = None not in (
        review.place.latitude,
        review.place.longitude,
        candidate.latitude,
        candidate.longitude,
    )
    if (
        name_compared
        and address_compared
        and name_score < 0.25
        and address_score < 0.25
    ):
        return True
    if (
        geo_compared
        and name_compared
        and address_compared
        and geo_score < 0.05
        and name_score < 0.5
        and address_score < 0.4
    ):
        return True
    return scores.address_number_conflict and name_compared and name_score < 0.5


def _address_number_conflict(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_numbers = re.findall(r"\b\d+[a-z]?\b", _normalize(left))
    right_numbers = re.findall(r"\b\d+[a-z]?\b", _normalize(right))
    return bool(left_numbers and right_numbers and left_numbers[0] != right_numbers[0])


def _name_similarity(left: str | None, right: str | None) -> float | None:
    if not left or not right:
        return None
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return None
    return SequenceMatcher(None, a, b).ratio()


def _token_similarity(left: str | None, right: str | None) -> float | None:
    if not left or not right:
        return None
    a, b = set(_normalize(left).split()), set(_normalize(right).split())
    return len(a & b) / len(a | b) if a or b else None


def _geo_similarity(
    lat1: float | None,
    lng1: float | None,
    lat2: float | None,
    lng2: float | None,
) -> float | None:
    if None in (lat1, lng1, lat2, lng2):
        return None
    distance_km = _haversine_km(lat1, lng1, lat2, lng2)  # type: ignore[arg-type]
    if distance_km <= 0.15:
        return 1.0
    if distance_km <= 0.5:
        return 1.0 - ((distance_km - 0.15) / 0.35) * 0.2
    if distance_km <= 2.0:
        return 0.8 - ((distance_km - 0.5) / 1.5) * 0.4
    if distance_km <= 10.0:
        return 0.4 - ((distance_km - 2.0) / 8.0) * 0.4
    return 0.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_km = 6_371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _candidate_from_api(raw: dict) -> PlaceCandidate | None:
    place_id = raw.get("id")
    if not isinstance(place_id, str) or not place_id:
        return None
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    country_code = None
    for component in raw.get("addressComponents") or []:
        if isinstance(component, dict) and "country" in (component.get("types") or []):
            country_code = component.get("shortText")
            break
    display_name = raw.get("displayName")
    return PlaceCandidate(
        place_id=place_id,
        name=display_name.get("text") if isinstance(display_name, dict) else None,
        address=raw.get("formattedAddress"),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        country_code=country_code,
        types=tuple(
            value for value in (raw.get("types") or []) if isinstance(value, str)
        ),
        business_status=raw.get("businessStatus"),
        moved_place_id=raw.get("movedPlaceId"),
    )


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    no_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", no_marks).split())


def _truncate(value: float, decimals: int = 2) -> float:
    factor = 10**decimals
    return math.trunc(value * factor) / factor


def _round_score(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _is_supported_category(types: Sequence[str]) -> bool:
    return any(
        place_type in _FOOD_DRINK_TYPES or place_type.endswith("_restaurant")
        for place_type in types
    )


def _error(review: SourceReview, code: str) -> MatchResult:
    return MatchResult(
        source_locator=review.source_locator,
        status="error",
        method="places_api",
        error_code=code,
    )


def _not_attempted(review: SourceReview, code: str) -> MatchResult:
    return MatchResult(
        source_locator=review.source_locator,
        status="not_attempted",
        method="cost_cap",
        error_code=code,
    )


def summarize_matches(matches: Sequence[MatchResult], *, api_calls: int = 0) -> dict:
    unique_matches = list({match.source_locator: match for match in matches}.values())
    statuses = {
        status: 0
        for status in (
            "exact",
            "automatic",
            "needs_review",
            "unmatched",
            "ineligible",
            "not_attempted",
            "error",
        )
    }
    for match in unique_matches:
        statuses[match.status] += 1
    matched = statuses["exact"] + statuses["automatic"]
    eligible = len(unique_matches) - statuses["ineligible"]
    attempted = eligible - statuses["not_attempted"]
    return {
        "algorithm_version": MATCH_ALGORITHM_VERSION,
        "records_considered": len(matches),
        "unique_places_considered": len(unique_matches),
        "eligible_unique_places": eligible,
        "attempted_unique_places": attempted,
        "attempt_coverage_rate": round(attempted / eligible, 4) if eligible else None,
        "high_confidence_matches": matched,
        "high_confidence_rate": round(matched / attempted, 4) if attempted else None,
        "exact": statuses["exact"],
        "automatic": statuses["automatic"],
        "needs_review": statuses["needs_review"],
        "unmatched": statuses["unmatched"],
        "ineligible": statuses["ineligible"],
        "not_attempted": statuses["not_attempted"],
        "errors": statuses["error"],
        "unsupported_recz_category": sum(
            match.recz_category_supported is False for match in unique_matches
        ),
        "places_api_calls": api_calls,
    }
