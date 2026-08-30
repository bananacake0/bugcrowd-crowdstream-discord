import asyncio
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from dotenv import load_dotenv

BUGCROWD_ORIGIN = "https://bugcrowd.com"
PAID_PROGRAMS_URL = f"{BUGCROWD_ORIGIN}/engagements-us.json"
HISTORY_FILE = Path("processed_ids.json")
PROGRAMS_FILE = Path("programs.json")
SCAN_STATE_FILE = Path("scan_state.json")
PROGRAM_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
PROGRAM_PATH_PATTERN = re.compile(r"^/engagements/([A-Za-z0-9][A-Za-z0-9-]*)/?$")
REQUEST_TIMEOUT_SECONDS = 30
BUGCROWD_MAX_ATTEMPTS = 5
PROGRAM_FETCH_CONCURRENCY = 4
DISCORD_MAX_ATTEMPTS = 3
DISCORD_RETRY_BASE_SECONDS = 1.0
DISCORD_MAX_EMBEDS_PER_MESSAGE = 10
DISCORD_MAX_EMBED_CHARACTERS = 6000
MESSAGE_DELAY_SECONDS = 3.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
COLOR_BY_PRIORITY = {
    1: 15158332,
    2: 15105570,
    3: 15844367,
    4: 3066993,
    5: 3447003,
}
EMOJI_BY_PRIORITY = {
    1: "<:P1:1543720548964765836>",
    2: "<:P2:1543720589938655322>",
    3: "<:P3:1543720633911738531>",
    4: "<:P4:1543720669077053480>",
    5: "<:P5:1543720716682268682>",
}
DEFAULT_EMBED_COLOR = 14586392


class HistoryError(RuntimeError):
    """Raised when processed-ID state cannot be trusted or persisted."""


class ProgramsError(RuntimeError):
    """Raised when the monitored-program configuration cannot be trusted."""


class ScanStateError(RuntimeError):
    """Raised when full-scan completion state cannot be trusted or persisted."""


class BugcrowdRequestError(RuntimeError):
    """Raised when Bugcrowd cannot return a usable response after retries."""


class CrowdstreamError(RuntimeError):
    """Raised when Bugcrowd's CrowdStream response cannot be used."""


class DiscordDeliveryError(RuntimeError):
    """Raised when a Discord notification cannot be delivered."""


def format_iso_date(date_str: str | None) -> str:
    if not date_str:
        return ""

    normalized = f"{date_str[:-1]}+00:00" if date_str.endswith("Z") else date_str
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return date_str
    return parsed.strftime("%d %b %Y")


def parse_crowdstream_date(date_str: str) -> date | None:
    normalized = f"{date_str[:-1]}+00:00" if date_str.endswith("Z") else date_str
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass

    try:
        return datetime.strptime(date_str, "%d %b %Y").date()
    except ValueError:
        return None


def text_value(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) and value else default


def seconds_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def save_json_atomically(
    data: Any,
    output_file: Path,
    error_type: type[RuntimeError],
    description: str,
) -> None:
    temporary_path: Path | None = None
    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_file.parent,
            prefix=f".{output_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_file)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise error_type(
            f"Could not save {description} file {output_file}: {exc}"
        ) from exc


def load_processed_ids(history_file: Path = HISTORY_FILE) -> set[str]:
    try:
        raw_data = history_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise HistoryError(
            f"Could not read history file {history_file}: {exc}"
        ) from exc

    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise HistoryError(
            f"History file {history_file} contains invalid JSON"
        ) from exc

    if not isinstance(data, list) or not all(
        isinstance(item, str) and item for item in data
    ):
        raise HistoryError(
            f"History file {history_file} must contain a list of non-empty IDs"
        )
    return set(data)


def save_processed_ids(
    processed_ids: set[str], history_file: Path = HISTORY_FILE
) -> None:
    save_json_atomically(sorted(processed_ids), history_file, HistoryError, "history")


def load_programs(programs_file: Path = PROGRAMS_FILE) -> list[dict[str, Any]]:
    try:
        raw_data = programs_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProgramsError(f"Programs file {programs_file} does not exist") from exc
    except OSError as exc:
        raise ProgramsError(
            f"Could not read programs file {programs_file}: {exc}"
        ) from exc

    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise ProgramsError(
            f"Programs file {programs_file} contains invalid JSON"
        ) from exc

    if not isinstance(data, list):
        raise ProgramsError(f"Programs file {programs_file} must contain a list")

    programs: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise ProgramsError(
                f"Programs file {programs_file} contains an invalid entry"
            )

        slug = item.get("slug")
        name = item.get("name")
        crowdstream_enabled = item.get("crowdstream_enabled")
        if (
            not isinstance(slug, str)
            or not PROGRAM_SLUG_PATTERN.fullmatch(slug)
            or not isinstance(name, str)
            or not name
            or not isinstance(crowdstream_enabled, bool)
        ):
            raise ProgramsError(
                f"Programs file {programs_file} entries require a valid slug, name, "
                "and crowdstream_enabled flag"
            )
        if slug in seen_slugs:
            raise ProgramsError(f"Programs file {programs_file} repeats slug {slug}")

        seen_slugs.add(slug)
        programs.append(
            {
                "slug": slug,
                "name": name,
                "crowdstream_enabled": crowdstream_enabled,
            }
        )

    return programs


def load_scan_state(scan_state_file: Path = SCAN_STATE_FILE) -> dict[str, Any]:
    try:
        raw_data = scan_state_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"full_scan_complete": False}
    except OSError as exc:
        raise ScanStateError(
            f"Could not read scan state file {scan_state_file}: {exc}"
        ) from exc

    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise ScanStateError(
            f"Scan state file {scan_state_file} contains invalid JSON"
        ) from exc
    if not isinstance(data, Mapping) or not isinstance(
        data.get("full_scan_complete"), bool
    ):
        raise ScanStateError(
            f"Scan state file {scan_state_file} must contain a boolean "
            "full_scan_complete value"
        )
    return dict(data)


def save_programs(
    programs: Sequence[Mapping[str, Any]], programs_file: Path = PROGRAMS_FILE
) -> None:
    ordered_programs = sorted(programs, key=lambda program: program["slug"])
    save_json_atomically(ordered_programs, programs_file, ProgramsError, "programs")


def save_scan_state(
    state: Mapping[str, Any], scan_state_file: Path = SCAN_STATE_FILE
) -> None:
    if not isinstance(state.get("full_scan_complete"), bool):
        raise ScanStateError("Scan state requires a boolean full_scan_complete value")
    save_json_atomically(dict(state), scan_state_file, ScanStateError, "scan state")


def bugcrowd_url(path: str | None) -> str:
    if not path:
        return BUGCROWD_ORIGIN

    candidate = urljoin(f"{BUGCROWD_ORIGIN}/", path)
    parsed = urlparse(candidate)
    if parsed.scheme == "https" and parsed.netloc == "bugcrowd.com":
        return candidate
    return BUGCROWD_ORIGIN


def crowdstream_event(item: Mapping[str, Any]) -> tuple[str, str]:
    raw_state_date_text = text_value(item.get("submission_state_date_text"), "Recently")
    if item.get("disclosed") is True:
        return (
            "Disclosed on",
            text_value(
                item.get("disclosed_at"),
                raw_state_date_text.removeprefix("Disclosed on "),
            ),
        )
    return (
        "Accepted on",
        text_value(
            item.get("accepted_at"),
            raw_state_date_text.removeprefix("Accepted on "),
        ),
    )


def crowdstream_sort_key(item: Mapping[str, Any]) -> int:
    _, event_date = crowdstream_event(item)
    parsed = parse_crowdstream_date(event_date)
    return parsed.toordinal() if parsed is not None else date.max.toordinal()


async def fetch_bugcrowd_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: Mapping[str, Any],
    description: str,
    allow_not_found: bool = False,
) -> dict[str, Any] | None:
    last_error = "unknown Bugcrowd response"
    for attempt in range(1, BUGCROWD_MAX_ATTEMPTS + 1):
        try:
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                params=params,
            ) as response:
                if response.status == 404 and allow_not_found:
                    return None
                if response.status == 200:
                    try:
                        data = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                        raise BugcrowdRequestError(
                            f"{description} returned invalid JSON"
                        ) from exc
                    if not isinstance(data, dict):
                        raise BugcrowdRequestError(
                            f"{description} returned an unexpected response shape"
                        )
                    return data

                last_error = f"{description} returned HTTP {response.status}"
                if response.status != 429 and response.status < 500:
                    raise BugcrowdRequestError(last_error)
                delay = retry_delay_seconds(response.headers, attempt)
        except (aiohttp.ClientError, TimeoutError) as exc:
            last_error = f"Could not fetch {description}: {exc}"
            delay = DISCORD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))

        if attempt < BUGCROWD_MAX_ATTEMPTS:
            print(
                f"[!] {last_error}; retrying in {delay:g}s "
                f"({attempt}/{BUGCROWD_MAX_ATTEMPTS})"
            )
            await asyncio.sleep(delay)

    raise BugcrowdRequestError(f"{last_error} after {BUGCROWD_MAX_ATTEMPTS} attempts")


async def fetch_crowdstream_page(
    session: aiohttp.ClientSession,
    program_slug: str,
    page: int = 1,
    *,
    allow_not_found: bool = False,
) -> dict[str, Any] | None:
    crowdstream_url = f"{BUGCROWD_ORIGIN}/engagements/{program_slug}/crowdstream.json"
    print(f"[*] Fetching {program_slug} page {page}...")

    try:
        data = await fetch_bugcrowd_json(
            session,
            crowdstream_url,
            params={"page": page},
            description=f"Bugcrowd program {program_slug} page {page}",
            allow_not_found=allow_not_found,
        )
    except BugcrowdRequestError as exc:
        raise CrowdstreamError(str(exc)) from exc

    if data is None:
        return None

    if not isinstance(data.get("results"), list):
        raise CrowdstreamError(
            f"Bugcrowd program {program_slug} page {page} has an unexpected "
            "response shape"
        )
    return data


def paid_program_from_catalog(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ProgramsError("Bugcrowd paid-program catalog contains an invalid entry")
    name = item.get("name")
    brief_url = item.get("briefUrl")
    match = (
        PROGRAM_PATH_PATTERN.fullmatch(brief_url)
        if isinstance(brief_url, str)
        else None
    )
    if not isinstance(name, str) or not name.strip() or match is None:
        raise ProgramsError(
            "Bugcrowd paid-program catalog entry is missing a valid name or slug"
        )
    return {"slug": match.group(1), "name": name.strip()}


async def fetch_paid_programs(
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    params = {
        "category": "bug_bounty",
        "sort_by": "promoted",
        "sort_direction": "desc",
    }

    async def fetch_page(page: int) -> dict[str, Any]:
        try:
            data = await fetch_bugcrowd_json(
                session,
                PAID_PROGRAMS_URL,
                params={**params, "page": page},
                description=f"Bugcrowd paid-program catalog page {page}",
            )
        except BugcrowdRequestError as exc:
            raise ProgramsError(str(exc)) from exc
        assert data is not None
        if not isinstance(data.get("engagements"), list):
            raise ProgramsError(
                f"Bugcrowd paid-program catalog page {page} has an unexpected shape"
            )
        return data

    first_page = await fetch_page(1)
    pagination = first_page.get("paginationMeta")
    if not isinstance(pagination, Mapping):
        raise ProgramsError("Bugcrowd paid-program catalog has no pagination metadata")
    limit = pagination.get("limit")
    total_count = pagination.get("totalCount")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 1
    ):
        raise ProgramsError("Bugcrowd paid-program catalog pagination is invalid")

    rows = list(first_page["engagements"])
    total_pages = math.ceil(total_count / limit)
    for page in range(2, total_pages + 1):
        page_data = await fetch_page(page)
        rows.extend(page_data["engagements"])
    if len(rows) != total_count:
        raise ProgramsError(
            f"Bugcrowd paid-program catalog returned {len(rows)} of "
            f"{total_count} expected rows"
        )

    programs_by_slug: dict[str, dict[str, Any]] = {}
    for row in rows:
        program = paid_program_from_catalog(row)
        existing = programs_by_slug.get(program["slug"])
        if existing is not None and existing["name"] != program["name"]:
            raise ProgramsError(
                f"Bugcrowd paid-program catalog repeats slug {program['slug']} "
                "with conflicting names"
            )
        programs_by_slug[program["slug"]] = program
    return sorted(programs_by_slug.values(), key=lambda program: program["slug"])


async def refresh_programs(
    session: aiohttp.ClientSession,
    previous_programs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    catalog_programs = await fetch_paid_programs(session)
    previous_by_slug = {program["slug"]: program for program in previous_programs}
    current_slugs = {program["slug"] for program in catalog_programs}
    added: list[dict[str, Any]] = []
    refreshed: list[dict[str, Any]] = []

    for catalog_program in catalog_programs:
        previous = previous_by_slug.get(catalog_program["slug"])
        if previous is None:
            program = {
                **catalog_program,
                "crowdstream_enabled": False,
            }
            added.append(program)
        else:
            program = {
                **catalog_program,
                "crowdstream_enabled": previous["crowdstream_enabled"],
            }
        refreshed.append(program)

    removed = [
        dict(program)
        for program in previous_programs
        if program["slug"] not in current_slugs
    ]
    return refreshed, added, removed


async def fetch_program_metadata(
    session: aiohttp.ClientSession, program_slug: str
) -> dict[str, Any]:
    changelog_url = f"{BUGCROWD_ORIGIN}/engagements/{program_slug}/changelog.json"
    try:
        changelog_data = await fetch_bugcrowd_json(
            session,
            changelog_url,
            params={},
            description=f"Bugcrowd program {program_slug} changelog index",
        )
    except BugcrowdRequestError as exc:
        raise ProgramsError(str(exc)) from exc
    assert changelog_data is not None

    changelogs = changelog_data.get("changelogs")
    if not isinstance(changelogs, list):
        raise ProgramsError(
            f"Bugcrowd program {program_slug} changelog has an unexpected shape"
        )
    valid_changelogs = [
        changelog
        for changelog in changelogs
        if isinstance(changelog, Mapping)
        and isinstance(changelog.get("id"), str)
        and changelog["id"]
    ]
    if not valid_changelogs:
        return {}

    latest = next(
        (
            changelog
            for changelog in valid_changelogs
            if changelog.get("changelogState") == "Latest"
        ),
        max(
            valid_changelogs,
            key=lambda changelog: text_value(changelog.get("publishedAt")),
        ),
    )
    changelog_id = latest["id"]
    detail_url = (
        f"{BUGCROWD_ORIGIN}/engagements/{program_slug}/changelog/{changelog_id}.json"
    )
    try:
        detail = await fetch_bugcrowd_json(
            session,
            detail_url,
            params={},
            description=f"Bugcrowd program {program_slug} latest changelog",
        )
    except BugcrowdRequestError as exc:
        raise ProgramsError(str(exc)) from exc
    assert detail is not None

    detail_data = detail.get("data")
    if not isinstance(detail_data, Mapping):
        raise ProgramsError(
            f"Bugcrowd program {program_slug} changelog has no data metadata"
        )
    brief = detail_data.get("brief")
    if not isinstance(brief, Mapping):
        raise ProgramsError(
            f"Bugcrowd program {program_slug} changelog has no brief metadata"
        )

    scope = detail_data.get("scope")
    target_count = 0
    if isinstance(scope, list):
        for group in scope:
            if not isinstance(group, Mapping) or group.get("inScope") is not True:
                continue
            targets = group.get("targets")
            if isinstance(targets, list):
                target_count += sum(isinstance(target, Mapping) for target in targets)

    return {
        "industry": text_value(detail.get("industryName")),
        "status": text_value(detail.get("statusLabel")),
        "participation": text_value(detail.get("participation")),
        "reward_model": text_value(detail.get("rewardAllocation")),
        "published_at": text_value(detail.get("publishedAt")),
        "target_count": target_count,
        "logo_url": text_value(detail.get("logoUrl")),
        "tagline": text_value(brief.get("tagline")),
    }


def crowdstream_total_pages(data: Mapping[str, Any], program_slug: str) -> int:
    pagination = data.get("pagination_meta")
    if not isinstance(pagination, Mapping):
        raise CrowdstreamError(
            f"Bugcrowd program {program_slug} has no CrowdStream pagination metadata"
        )

    total_pages = pagination.get("total_pages")
    if (
        not isinstance(total_pages, int)
        or isinstance(total_pages, bool)
        or total_pages < 0
    ):
        raise CrowdstreamError(
            f"Bugcrowd program {program_slug} has invalid CrowdStream pagination"
        )
    return total_pages


def build_discord_embed(item: Mapping[str, Any]) -> dict[str, Any]:
    engagement_name = text_value(item.get("engagement_name"), "Unknown Program")
    program_url = bugcrowd_url(text_value(item.get("engagement_path")))
    is_disclosed = item.get("disclosed") is True
    state_text = text_value(item.get("submission_state_text"), "Submission accepted")
    event_date_label, event_date_value = crowdstream_event(item)

    priority = item.get("priority")
    priority_number = (
        priority
        if isinstance(priority, int) and not isinstance(priority, bool)
        else None
    )
    priority_value = (
        EMOJI_BY_PRIORITY.get(priority_number, f"`P{priority_number}`")
        if priority_number in COLOR_BY_PRIORITY
        else "N/A"
    )
    embed: dict[str, Any] = {
        "color": COLOR_BY_PRIORITY.get(priority_number, DEFAULT_EMBED_COLOR),
        "footer": {
            "text": "Submitted on "
            f"{format_iso_date(text_value(item.get('created_at'))) or 'Unknown'}"
        },
    }

    report_title = text_value(item.get("title"), "Report")
    disclosure_path = text_value(item.get("disclosure_report_url"))
    if is_disclosed:
        embed["title"] = f"Disclosed: {report_title}"
        if disclosure_path:
            embed["url"] = bugcrowd_url(disclosure_path)
    else:
        embed["title"] = state_text.replace("https://", "").replace("http://", "")

    fields: list[dict[str, Any]] = []
    researcher = text_value(item.get("researcher_username"))
    profile_path = text_value(item.get("researcher_profile_path"))
    if item.get("crowdstream_name_visible") is True and researcher and profile_path:
        fields.append(
            {
                "name": "Researcher",
                "value": f"[{researcher}]({bugcrowd_url(profile_path)})",
                "inline": False,
            }
        )

    fields.append(
        {
            "name": "Engagement",
            "value": f"[{engagement_name}]({program_url})",
            "inline": False,
        }
    )
    amount = item.get("amount")
    if amount is not None and str(amount):
        fields.append(
            {
                "name": "Reward",
                "value": str(amount),
                "inline": False,
            }
        )
    fields.extend(
        [
            {"name": "Priority", "value": priority_value, "inline": False},
            {
                "name": "\u200b",
                "value": f"{event_date_label} {event_date_value}",
                "inline": False,
            },
        ]
    )
    embed["fields"] = fields

    if logo_url := text_value(item.get("logo_url")):
        embed["thumbnail"] = {"url": logo_url}
    return embed


def embed_character_count(embed: Mapping[str, Any]) -> int:
    total = sum(
        len(value)
        for key in ("title", "description")
        if isinstance(value := embed.get(key), str)
    )
    for container_name, text_key in (("footer", "text"), ("author", "name")):
        container = embed.get(container_name)
        if isinstance(container, Mapping):
            value = container.get(text_key)
            if isinstance(value, str):
                total += len(value)

    fields = embed.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, Mapping):
                continue
            for key in ("name", "value"):
                value = field.get(key)
                if isinstance(value, str):
                    total += len(value)
    return total


def batch_items_for_discord(
    items: Sequence[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_characters = 0

    for item in items:
        item_characters = embed_character_count(build_discord_embed(item))
        batch_is_full = len(current_batch) >= DISCORD_MAX_EMBEDS_PER_MESSAGE
        message_is_full = (
            current_batch
            and current_characters + item_characters > DISCORD_MAX_EMBED_CHARACTERS
        )
        if batch_is_full or message_is_full:
            batches.append(current_batch)
            current_batch = []
            current_characters = 0

        current_batch.append(item)
        current_characters += item_characters

    if current_batch:
        batches.append(current_batch)
    return batches


def retry_delay_seconds(
    headers: Mapping[str, str], attempt: int, response_retry_after: Any = None
) -> float:
    if (delay := seconds_value(response_retry_after)) is not None:
        return delay

    retry_after = headers.get("Retry-After")
    if (delay := seconds_value(retry_after)) is not None:
        return delay
    return DISCORD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))


def rate_limit_cooldown_seconds(headers: Mapping[str, str]) -> float:
    if headers.get("X-RateLimit-Remaining") != "0":
        return 0.0
    return seconds_value(headers.get("X-RateLimit-Reset-After")) or 0.0


async def rate_limit_retry_delay(response: Any, attempt: int) -> float:
    body: Mapping[str, Any] = {}
    try:
        response_body = await response.json(content_type=None)
    except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    else:
        if isinstance(response_body, Mapping):
            body = response_body

    delay = retry_delay_seconds(
        response.headers,
        attempt,
        response_retry_after=body.get("retry_after"),
    )
    details = [f"retry_after={delay:g}s"]
    for header, label in (
        ("X-RateLimit-Scope", "scope"),
        ("X-RateLimit-Limit", "limit"),
        ("X-RateLimit-Remaining", "remaining"),
        ("X-RateLimit-Reset-After", "reset_after"),
    ):
        if value := response.headers.get(header):
            details.append(f"{label}={value}")
    if isinstance(body.get("global"), bool):
        details.append(f"global={str(body['global']).lower()}")
    print(f"[!] Discord rate limit details: {', '.join(details)}")
    return delay


async def send_discord_payload(
    session: aiohttp.ClientSession,
    webhook_url: str,
    payload: Mapping[str, Any],
) -> float:
    last_error = "unknown delivery error"

    for attempt in range(1, DISCORD_MAX_ATTEMPTS + 1):
        try:
            async with session.post(webhook_url, json=payload) as response:
                if 200 <= response.status < 300:
                    return rate_limit_cooldown_seconds(response.headers)

                last_error = f"Discord returned HTTP {response.status}"
                if response.status != 429 and response.status < 500:
                    raise DiscordDeliveryError(last_error)
                if response.status == 429:
                    delay = await rate_limit_retry_delay(response, attempt)
                else:
                    delay = retry_delay_seconds(response.headers, attempt)
        except (aiohttp.ClientError, TimeoutError) as exc:
            last_error = f"Discord request failed: {exc}"
            delay = DISCORD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))

        if attempt < DISCORD_MAX_ATTEMPTS:
            print(
                f"[!] {last_error}; retrying in {delay:g}s "
                f"({attempt}/{DISCORD_MAX_ATTEMPTS})"
            )
            await asyncio.sleep(delay)

    raise DiscordDeliveryError(f"{last_error} after {DISCORD_MAX_ATTEMPTS} attempts")


async def send_to_discord(
    session: aiohttp.ClientSession,
    webhook_url: str,
    items: Sequence[Mapping[str, Any]],
) -> float:
    payload = {"embeds": [build_discord_embed(item) for item in items]}
    return await send_discord_payload(session, webhook_url, payload)


def build_program_change_embed(
    program: Mapping[str, Any],
    change: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    added = change == "added"
    slug = program["slug"]
    name = program["name"]
    metadata = metadata or {}
    fields = [
        {
            "name": "CrowdStream",
            "value": (
                "Enabled"
                if program.get("crowdstream_enabled") is True
                else "Not enabled"
            ),
            "inline": False,
        },
    ]
    for label, key in (
        ("Industry", "industry"),
        ("Status", "status"),
        ("Participation", "participation"),
        ("Reward model", "reward_model"),
    ):
        if value := text_value(metadata.get(key)):
            fields.append(
                {
                    "name": label,
                    "value": value.replace("_", " ").title(),
                    "inline": False,
                }
            )
    if (
        isinstance(target_count := metadata.get("target_count"), int)
        and target_count > 0
    ):
        fields.append(
            {"name": "In-scope targets", "value": str(target_count), "inline": False}
        )
    if published_at := text_value(metadata.get("published_at")):
        fields.append(
            {
                "name": "Published",
                "value": format_iso_date(published_at),
                "inline": False,
            }
        )
    embed = {
        "title": "New paid Bugcrowd program" if added else "Paid program removed",
        "description": f"[{name}]({BUGCROWD_ORIGIN}/engagements/{slug})",
        "color": 3066993 if added else 15158332,
        "fields": fields,
    }
    if logo_url := text_value(metadata.get("logo_url")):
        embed["thumbnail"] = {"url": logo_url}
    if tagline := text_value(metadata.get("tagline")):
        embed["description"] += f"\n\n{tagline[:500]}"
    return embed


async def deliver_program_changes(
    session: aiohttp.ClientSession,
    webhook_url: str,
    added: Sequence[Mapping[str, Any]],
    removed: Sequence[Mapping[str, Any]],
    metadata_by_slug: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    metadata_by_slug = metadata_by_slug or {}
    embeds = [
        build_program_change_embed(
            program, "added", metadata_by_slug.get(program["slug"])
        )
        for program in added
    ]
    embeds.extend(
        build_program_change_embed(
            program, "removed", metadata_by_slug.get(program["slug"])
        )
        for program in removed
    )
    for index in range(0, len(embeds), DISCORD_MAX_EMBEDS_PER_MESSAGE):
        cooldown = await send_discord_payload(
            session,
            webhook_url,
            {"embeds": embeds[index : index + DISCORD_MAX_EMBEDS_PER_MESSAGE]},
        )
        if index + DISCORD_MAX_EMBEDS_PER_MESSAGE < len(embeds):
            await asyncio.sleep(max(MESSAGE_DELAY_SECONDS, cooldown))


def select_new_items(
    results: list[Any], processed_ids: set[str]
) -> list[dict[str, Any]]:
    seen_ids = set(processed_ids)
    new_items: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        submission_id = item.get("id")
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or submission_id in seen_ids
        ):
            continue
        new_items.append(item)
        seen_ids.add(submission_id)
    return new_items


async def fetch_program_crowdstream(
    session: aiohttp.ClientSession,
    program_slug: str,
    *,
    full_scan: bool,
) -> list[Any] | None:
    """Return one program's reports oldest-first, or None when CrowdStream is off."""
    first_page = await fetch_crowdstream_page(
        session,
        program_slug,
        page=1,
        allow_not_found=True,
    )
    if first_page is None:
        return None
    if not full_scan:
        return list(first_page["results"])

    total_pages = crowdstream_total_pages(first_page, program_slug)
    if total_pages == 0 and first_page["results"]:
        raise CrowdstreamError(
            f"Bugcrowd program {program_slug} returned reports but zero pages"
        )
    print(f"[*] {program_slug} exposes {total_pages} CrowdStream page(s).")

    results: list[Any] = []
    for page in range(total_pages, 1, -1):
        page_data = await fetch_crowdstream_page(session, program_slug, page=page)
        if page_data is None:
            raise CrowdstreamError(
                f"Bugcrowd program {program_slug} page {page} disappeared"
            )
        results.extend(page_data["results"])
    results.extend(first_page["results"])
    return results


async def fetch_new_items(
    session: aiohttp.ClientSession,
    processed_ids: set[str],
    programs: Sequence[dict[str, Any]],
    *,
    full_scan: bool = True,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(PROGRAM_FETCH_CONCURRENCY)

    async def fetch_program(program: Mapping[str, Any]) -> list[Any] | None:
        async with semaphore:
            return await fetch_program_crowdstream(
                session, program["slug"], full_scan=full_scan
            )

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(fetch_program(program)) for program in programs]
    except* CrowdstreamError as error_group:
        raise error_group.exceptions[0] from None

    # Merge in program order, never completion order: crowdstream_sort_key resolves
    # to whole days, so the stable sort below leaves same-day reports in this order.
    ordered_results: list[Any] = []
    for program, task in zip(programs, tasks, strict=True):
        program_results = task.result()
        if program_results is None:
            program["crowdstream_enabled"] = False
            continue
        program["crowdstream_enabled"] = True
        ordered_results.extend(program_results)

    valid_results = [item for item in ordered_results if isinstance(item, dict)]
    valid_results.sort(key=crowdstream_sort_key)
    unique_results: list[dict[str, Any]] = []
    unique_by_id: dict[str, dict[str, Any]] = {}
    duplicate_records = 0
    conflicting_duplicates = 0
    for item in valid_results:
        submission_id = item.get("id")
        if not isinstance(submission_id, str) or not submission_id:
            continue
        previous = unique_by_id.get(submission_id)
        if previous is not None:
            duplicate_records += 1
            if previous != item:
                conflicting_duplicates += 1
            continue
        unique_by_id[submission_id] = item
        unique_results.append(item)
    print(
        f"[*] CrowdStream records: {len(valid_results)} fetched, "
        f"{len(unique_results)} unique IDs, {duplicate_records} duplicate records, "
        f"{conflicting_duplicates} conflicting duplicate payloads."
    )
    return select_new_items(unique_results, processed_ids)


async def deliver_new_items(
    session: aiohttp.ClientSession,
    webhook_url: str,
    new_items: list[dict[str, Any]],
    processed_ids: set[str],
    history_file: Path = HISTORY_FILE,
) -> int:
    failures = 0
    batches = batch_items_for_discord(new_items)
    for index, batch in enumerate(batches):
        submission_ids = [item["id"] for item in batch]
        cooldown = 0.0
        try:
            cooldown = await send_to_discord(session, webhook_url, batch)
        except DiscordDeliveryError as exc:
            failures = sum(len(remaining_batch) for remaining_batch in batches[index:])
            print(
                f"[!] Could not deliver batch of {len(batch)} alerts "
                f"({submission_ids[0]} through {submission_ids[-1]}): {exc}"
            )
            print("[!] Stopping here to preserve oldest-to-newest delivery order.")
            break
        else:
            processed_ids.update(submission_ids)
            save_processed_ids(processed_ids, history_file)

        if index < len(batches) - 1:
            await asyncio.sleep(max(MESSAGE_DELAY_SECONDS, cooldown))
    return failures


async def run_bot(
    webhook_url: str,
    history_file: Path = HISTORY_FILE,
    programs_file: Path = PROGRAMS_FILE,
    programs_webhook_url: str | None = None,
    scan_state_file: Path = SCAN_STATE_FILE,
) -> int:
    try:
        processed_ids = load_processed_ids(history_file)
        programs = load_programs(programs_file)
        scan_state = load_scan_state(scan_state_file)
    except (HistoryError, ProgramsError, ScanStateError) as exc:
        print(f"[!] {exc}")
        return 1
    full_scan = not scan_state["full_scan_complete"]
    print(f"[*] Loaded {len(processed_ids)} historical submission entries.")
    print(
        f"[*] Refreshing the paid-program catalog from {len(programs)} saved programs."
    )

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    failures = 0
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            programs, added_programs, removed_programs = await refresh_programs(
                session, programs
            )
            mode = "every CrowdStream page" if full_scan else "page 1 only"
            print(f"[*] Scanning {mode} for {len(programs)} paid programs.")
            new_items = await fetch_new_items(
                session,
                processed_ids,
                programs,
                full_scan=full_scan,
            )
            if added_programs or removed_programs:
                print(
                    f"[*] Paid-program changes: {len(added_programs)} added, "
                    f"{len(removed_programs)} removed."
                )
                if programs_webhook_url:
                    metadata_by_slug: dict[str, Mapping[str, Any]] = {}
                    for program in added_programs:
                        try:
                            metadata_by_slug[
                                program["slug"]
                            ] = await fetch_program_metadata(session, program["slug"])
                        except ProgramsError as exc:
                            print(
                                f"[!] Could not fetch metadata for "
                                f"{program['slug']}: {exc}"
                            )
                    await deliver_program_changes(
                        session,
                        programs_webhook_url,
                        added_programs,
                        removed_programs,
                        metadata_by_slug,
                    )
                else:
                    print(
                        "[!] DISCORD_PROGRAMS_WEBHOOK_URL is not configured; "
                        "program changes were recorded without a Discord alert."
                    )
            save_programs(programs, programs_file)

            if not new_items:
                print(f"[*] Checked {mode}. No new reports detected.")
            else:
                print(
                    f"[*] Found {len(new_items)} new reports across {mode}. "
                    "Broadcasting globally oldest first..."
                )
                failures = await deliver_new_items(
                    session,
                    webhook_url,
                    new_items,
                    processed_ids,
                    history_file,
                )

            if full_scan and (not new_items or failures == 0):
                save_scan_state(
                    {
                        **scan_state,
                        "full_scan_complete": True,
                        "completed_at": datetime.now().astimezone().isoformat(),
                    },
                    scan_state_file,
                )
                print("[*] Full CrowdStream scan completed; future runs use page 1.")
    except (
        CrowdstreamError,
        DiscordDeliveryError,
        HistoryError,
        ProgramsError,
        ScanStateError,
    ) as exc:
        print(f"[!] {exc}")
        return 1

    if failures:
        print(f"[!] Completed with {failures} undelivered alert(s).")
        return 1
    print(f"[*] Delivered and recorded {len(new_items)} new alert(s).")
    return 0


async def main() -> int:
    load_dotenv()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[!] Error: DISCORD_WEBHOOK_URL environment secret is missing.")
        return 1
    return await run_bot(
        webhook_url,
        history_file=Path(
            os.environ.get("CROWDSTREAM_HISTORY_FILE", str(HISTORY_FILE))
        ),
        programs_file=Path(
            os.environ.get("CROWDSTREAM_PROGRAMS_FILE", str(PROGRAMS_FILE))
        ),
        programs_webhook_url=os.environ.get("DISCORD_PROGRAMS_WEBHOOK_URL"),
        scan_state_file=Path(
            os.environ.get("CROWDSTREAM_SCAN_STATE_FILE", str(SCAN_STATE_FILE))
        ),
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
