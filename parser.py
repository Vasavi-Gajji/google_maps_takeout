"""Defensive parser for Google Maps reviews exported through Google Takeout.

The parser reads archives in memory and never extracts them to disk. It is
intentionally independent of Rexy's settings, database, and social-post paths.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from .models import ImportPreview, ParseIssue, PlaceHint, SourceReview

MAX_INPUT_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 25 * 1024 * 1024
MAX_ZIP_MEMBERS = 5_000
MAX_ZIP_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_JSON_NESTING = 40

_REVIEW_KEYS = frozenset(
    {
        "five_star_rating_published",
        "five_star_rating_draft",
        "review_text_published",
        "review_text_draft",
    }
)
_REGIONAL_GOOGLE_HOST = re.compile(
    r"^(?:www\.|maps\.)?google\.(?:[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$"
)


class TakeoutParseError(ValueError):
    """The supplied file cannot be processed safely as a Takeout export."""


def parse_takeout(path: str | Path, *, include_drafts: bool = False) -> ImportPreview:
    """Parse a Takeout ZIP, directory, JSON, or GeoJSON into a dry-run preview."""
    input_path = Path(path)
    if not input_path.exists():
        raise TakeoutParseError("input does not exist")

    suffixes = {suffix.lower() for suffix in input_path.suffixes}
    if suffixes.intersection({".tgz", ".tar", ".gz"}):
        raise TakeoutParseError("TGZ/TAR is not supported by this POC; export as ZIP")

    preview = ImportPreview()
    seen_fingerprints: set[str] = set()
    for source_name, payload in _iter_json_payloads(input_path):
        preview.files_scanned += 1
        try:
            document = json.loads(payload)
            _check_json_depth(document)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            RecursionError,
            ValueError,
            TakeoutParseError,
        ):
            preview.issues.append(ParseIssue("malformed_or_too_deep_json", source_name))
            continue

        for features in _iter_review_feature_sets(document, set()):
            preview.feature_collections_seen += 1
            for index, feature in enumerate(features):
                preview.features_seen += 1
                result = _normalize_feature(
                    feature,
                    source_file=source_name,
                    source_index=index,
                    include_drafts=include_drafts,
                )
                if result == "draft_skipped":
                    preview.drafts_skipped += 1
                    continue
                if isinstance(result, ParseIssue):
                    preview.invalid_skipped += 1
                    preview.issues.append(result)
                    continue
                if result.content_fingerprint in seen_fingerprints:
                    preview.duplicates_skipped += 1
                    continue
                seen_fingerprints.add(result.content_fingerprint)
                preview.records.append(result)

    if preview.feature_collections_seen == 0:
        preview.issues.append(
            ParseIssue("no_review_feature_collection", input_path.name)
        )
    return preview


def _iter_json_payloads(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.is_dir():
        candidates = sorted(
            candidate
            for candidate in path.rglob("*")
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.suffix.lower() in {".json", ".geojson"}
            )
        )
        total_size = 0
        for candidate in candidates:
            if candidate.stat().st_size > MAX_JSON_BYTES:
                raise TakeoutParseError("directory contains an oversized JSON file")
            total_size += candidate.stat().st_size
            if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise TakeoutParseError("directory JSON exceeds the POC size limit")
            yield candidate.relative_to(path).as_posix(), candidate.read_bytes()
        return

    if path.stat().st_size > MAX_INPUT_BYTES:
        raise TakeoutParseError("input exceeds the POC size limit")
    if zipfile.is_zipfile(path):
        try:
            yield from _iter_zip_json_payloads(path)
        except (
            zipfile.BadZipFile,
            zlib.error,
            NotImplementedError,
            RuntimeError,
            EOFError,
            OSError,
        ):
            raise TakeoutParseError("archive could not be read safely") from None
        return
    if path.suffix.lower() not in {".json", ".geojson"}:
        raise TakeoutParseError("expected a Takeout ZIP, directory, JSON, or GeoJSON")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise TakeoutParseError("JSON file exceeds the POC size limit")
    yield path.name, path.read_bytes()


def _iter_zip_json_payloads(path: Path) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise TakeoutParseError("archive has too many members")

        total_size = 0
        for member in members:
            _validate_zip_member(member)
            total_size += member.file_size
            if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise TakeoutParseError("archive expands beyond the POC size limit")

        for member in members:
            if member.is_dir() or Path(member.filename).suffix.lower() not in {
                ".json",
                ".geojson",
            }:
                continue
            if member.file_size > MAX_JSON_BYTES:
                continue
            with archive.open(member) as handle:
                payload = handle.read(MAX_JSON_BYTES + 1)
            if len(payload) > MAX_JSON_BYTES:
                raise TakeoutParseError("JSON member exceeds the POC size limit")
            yield member.filename, payload


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
    name = member.filename
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        PurePosixPath(normalized).is_absolute()
        or name.startswith(("/", "\\"))
        or ".." in parts
    ):
        raise TakeoutParseError("archive contains an unsafe member path")
    if member.flag_bits & 0x1:
        raise TakeoutParseError("encrypted ZIP members are not supported")
    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise TakeoutParseError("archive uses an unsupported compression method")
    unix_mode = member.external_attr >> 16
    if unix_mode and (unix_mode & 0o170000) == 0o120000:
        raise TakeoutParseError("archive contains a symbolic link")
    if member.file_size > MAX_JSON_BYTES and Path(name).suffix.lower() in {
        ".json",
        ".geojson",
    }:
        raise TakeoutParseError("JSON member exceeds the POC size limit")
    if member.file_size and member.compress_size == 0:
        raise TakeoutParseError("archive member has an invalid compression ratio")
    if (
        member.compress_size
        and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
    ):
        raise TakeoutParseError("archive member compression ratio is unsafe")


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_NESTING:
        raise TakeoutParseError("JSON nesting is too deep")
    if isinstance(value, dict):
        for child in value.values():
            _check_json_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth + 1)


def _iter_review_feature_sets(
    value: Any, seen_collections: set[int]
) -> Iterator[list[dict]]:
    """Find review-shaped FeatureCollections independent of localized filenames."""
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in seen_collections:
            return
        features = value.get("features")
        if isinstance(features, list) and _contains_review_signature(features):
            seen_collections.add(value_id)
            yield [feature for feature in features if isinstance(feature, dict)]
            return
        if value.get("type") == "Feature" and _feature_has_review_signature(value):
            seen_collections.add(value_id)
            yield [value]
            return
        for child in value.values():
            yield from _iter_review_feature_sets(child, seen_collections)
    elif isinstance(value, list):
        if _contains_review_signature(value):
            yield [feature for feature in value if isinstance(feature, dict)]
            return
        for child in value:
            yield from _iter_review_feature_sets(child, seen_collections)


def _contains_review_signature(features: list[Any]) -> bool:
    return any(
        isinstance(feature, dict) and _feature_has_review_signature(feature)
        for feature in features
    )


def _feature_has_review_signature(feature: dict) -> bool:
    properties = feature.get("properties")
    candidate = properties if isinstance(properties, dict) else feature
    return bool(_REVIEW_KEYS.intersection(candidate))


def _normalize_feature(
    feature: dict,
    *,
    source_file: str,
    source_index: int,
    include_drafts: bool,
) -> SourceReview | ParseIssue | str:
    properties = feature.get("properties")
    data = properties if isinstance(properties, dict) else feature

    published_rating_raw = data.get("five_star_rating_published")
    published_text_raw = data.get("review_text_published")
    published_text = _preserve_review_text(published_text_raw)
    draft_rating_raw = data.get("five_star_rating_draft")
    draft_text_raw = data.get("review_text_draft")
    draft_text = _preserve_review_text(draft_text_raw)
    has_published = published_rating_raw is not None or published_text is not None
    has_draft = draft_rating_raw is not None or draft_text is not None

    if has_published:
        state = "published"
        rating_raw = published_rating_raw
        review_text = published_text
        review_text_raw = published_text_raw
    elif has_draft and include_drafts:
        state = "draft"
        rating_raw = draft_rating_raw
        review_text = draft_text
        review_text_raw = draft_text_raw
    elif has_draft:
        return "draft_skipped"
    else:
        return ParseIssue("not_a_review_feature", source_file, source_index)

    rating = _parse_rating(rating_raw)
    warnings: list[str] = []
    if rating_raw is not None and rating is None:
        warnings.append("invalid_rating_ignored")
    if (
        isinstance(review_text_raw, str)
        and review_text_raw.strip()
        and review_text is None
    ):
        warnings.append("invalid_unicode_review_text_ignored")
    if rating is None and review_text is None:
        return ParseIssue(
            "review_has_no_valid_rating_or_text", source_file, source_index
        )

    name, address, country_code = _parse_location(data.get("location"))
    latitude, longitude, coordinate_warning = _parse_coordinates(
        feature.get("geometry")
    )
    if coordinate_warning:
        warnings.append(coordinate_warning)

    maps_url = _parse_url(data.get("google_maps_url"))
    if data.get("google_maps_url") and maps_url is None:
        warnings.append("invalid_google_maps_url_ignored")
    modified_at = _clean_text(data.get("date"))
    if modified_at and not _looks_like_iso_instant(modified_at):
        warnings.append("unrecognized_source_modified_at")

    locator_material = _locator_material(
        maps_url=maps_url,
        name=name,
        address=address,
        latitude=latitude,
        longitude=longitude,
    )
    source_locator = "gmaps_" + _digest(locator_material, length=24)
    content_material = _canonical_parts(
        locator_material,
        state,
        str(rating or ""),
        review_text or "",
        modified_at or "",
    )
    fingerprint = "gmaprev_" + _digest(content_material, length=24)
    return SourceReview(
        source_locator=source_locator,
        content_fingerprint=fingerprint,
        state=state,  # type: ignore[arg-type]
        rating_value=rating,
        review_text=review_text,
        source_modified_at=modified_at,
        google_maps_url=maps_url,
        place=PlaceHint(
            name=name,
            address=address,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
        ),
        source_file=source_file,
        source_index=source_index,
        warnings=tuple(warnings),
    )


def extract_query_place_id(url: str | None) -> str | None:
    """Extract only the documented ``query_place_id`` URL parameter.

    Numeric ``cid`` and Maps' internal ``0x...`` tokens are deliberately not
    treated as Places API IDs.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold()
        if (
            hostname != "google.com"
            and not hostname.endswith(".google.com")
            and not _REGIONAL_GOOGLE_HOST.fullmatch(hostname)
        ):
            return None
        values = parse_qs(parsed.query).get("query_place_id", [])
    except ValueError:
        return None
    if len(values) != 1:
        return None
    candidate = values[0].strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,256}", candidate):
        return candidate
    return None


def _parse_location(raw: Any) -> tuple[str | None, str | None, str | None]:
    locations: Iterable[Any]
    if isinstance(raw, list):
        locations = raw
    else:
        locations = [raw]
    name = address = country = None
    for location in locations:
        if not isinstance(location, dict):
            continue
        name = name or _clean_text(location.get("name"))
        address = address or _clean_text(location.get("address"))
        country = country or _clean_text(
            location.get("country_code") or location.get("country")
        )
    country_code = (
        country.upper() if country and re.fullmatch(r"[A-Za-z]{2}", country) else None
    )
    return name, address, country_code


def _parse_coordinates(geometry: Any) -> tuple[float | None, float | None, str | None]:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None, None, "missing_or_invalid_coordinates"
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None, None, "missing_or_invalid_coordinates"
    longitude = _finite_number(coordinates[0])
    latitude = _finite_number(coordinates[1])
    if longitude is None or latitude is None:
        return None, None, "missing_or_invalid_coordinates"
    if (longitude, latitude) == (0.0, 0.0):
        return None, None, "unknown_coordinates"
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None, None, "out_of_range_coordinates"
    return latitude, longitude, None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_rating(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        return None
    return parsed if 1 <= parsed <= 5 else None


def _parse_url(value: Any) -> str | None:
    candidate = _clean_text(value)
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    cleaned = value.strip()
    return cleaned or None


def _preserve_review_text(value: Any) -> str | None:
    """Recognize empty text without altering the user's published wording."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _looks_like_iso_instant(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value,
        )
    )


def _locator_material(
    *,
    maps_url: str | None,
    name: str | None,
    address: str | None,
    latitude: float | None,
    longitude: float | None,
) -> str:
    if maps_url:
        parsed = urlparse(maps_url)
        canonical_query = urlencode(
            sorted(parse_qsl(parsed.query, keep_blank_values=True))
        )
        return parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc=parsed.netloc.casefold(),
            query=canonical_query,
            fragment="",
        ).geturl()
    lat = f"{latitude:.5f}" if latitude is not None else ""
    lng = f"{longitude:.5f}" if longitude is not None else ""
    return _canonical_parts(
        _normalize_identity(name), _normalize_identity(address), lat, lng
    )


def _normalize_identity(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _canonical_parts(*values: str) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _digest(value: str, *, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
