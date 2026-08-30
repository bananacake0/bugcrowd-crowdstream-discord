import asyncio
import json
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
HISTORY_FILE = Path("processed_ids.json")
PROGRAMS_FILE = Path("programs.json")
PROGRAM_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PAGE_NUMBERS_DESCENDING = (2, 1)
REQUEST_TIMEOUT_SECONDS = 30
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
DEFAULT_EMBED_COLOR = 14586392


class HistoryError(RuntimeError):
    """Raised when processed-ID state cannot be trusted or persisted."""


class ProgramsError(RuntimeError):
    """Raised when the monitored-program configuration cannot be trusted."""


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
    temporary_path: Path | None = None
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=history_file.parent,
            prefix=f".{history_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(sorted(processed_ids), temporary_file, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, history_file)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise HistoryError(
            f"Could not save history file {history_file}: {exc}"
        ) from exc


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

    if not isinstance(data, list) or not data:
        raise ProgramsError(
            f"Programs file {programs_file} must contain a non-empty list"
        )

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

    if not any(program["crowdstream_enabled"] for program in programs):
        raise ProgramsError(
            f"Programs file {programs_file} has no CrowdStream-enabled programs"
        )
    return programs


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


async def fetch_crowdstream_page(
    session: aiohttp.ClientSession, program_slug: str, page: int = 1
) -> dict[str, Any]:
    crowdstream_url = f"{BUGCROWD_ORIGIN}/engagements/{program_slug}/crowdstream.json"
    params = {"page": page}
    headers = {"User-Agent": USER_AGENT}
    print(f"[*] Fetching {program_slug} page {page}...")

    try:
        async with session.get(
            crowdstream_url, headers=headers, params=params
        ) as response:
            if response.status != 200:
                raise CrowdstreamError(
                    f"Bugcrowd program {program_slug} page {page} returned "
                    f"HTTP {response.status}"
                )
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                raise CrowdstreamError(
                    f"Bugcrowd program {program_slug} page {page} returned invalid JSON"
                ) from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise CrowdstreamError(
            f"Could not fetch Bugcrowd program {program_slug} page {page}: {exc}"
        ) from exc

    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise CrowdstreamError(
            f"Bugcrowd program {program_slug} page {page} has an unexpected "
            "response shape"
        )
    return data


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
        f"`P{priority_number}`" if priority_number in COLOR_BY_PRIORITY else "N/A"
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


def seconds_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


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


async def send_to_discord(
    session: aiohttp.ClientSession,
    webhook_url: str,
    items: Sequence[Mapping[str, Any]],
) -> float:
    payload = {"embeds": [build_discord_embed(item) for item in items]}
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


async def fetch_new_items(
    session: aiohttp.ClientSession,
    processed_ids: set[str],
    programs: Sequence[Mapping[str, Any]],
    pages: Sequence[int] = PAGE_NUMBERS_DESCENDING,
) -> list[dict[str, Any]]:
    ordered_results: list[Any] = []
    for program in programs:
        if program.get("crowdstream_enabled") is not True:
            continue
        program_slug = program["slug"]
        for page in pages:
            page_data = await fetch_crowdstream_page(session, program_slug, page=page)
            ordered_results.extend(page_data["results"])

    valid_results = [item for item in ordered_results if isinstance(item, dict)]
    valid_results.sort(key=crowdstream_sort_key)
    return select_new_items(valid_results, processed_ids)


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
) -> int:
    try:
        processed_ids = load_processed_ids(history_file)
        programs = load_programs(programs_file)
    except (HistoryError, ProgramsError) as exc:
        print(f"[!] {exc}")
        return 1
    print(f"[*] Loaded {len(processed_ids)} historical submission entries.")
    enabled_programs = [
        program for program in programs if program["crowdstream_enabled"]
    ]
    page_list = ", ".join(str(page) for page in PAGE_NUMBERS_DESCENDING)
    print(
        f"[*] Monitoring pages {page_list} for {len(enabled_programs)} paid programs."
    )

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            new_items = await fetch_new_items(
                session,
                processed_ids,
                enabled_programs,
            )
            if not new_items:
                print(
                    f"[*] Checked pages {page_list} for every program. "
                    "No new reports detected."
                )
                return 0

            print(
                f"[*] Found {len(new_items)} new reports across pages {page_list}. "
                "Broadcasting oldest first..."
            )
            failures = await deliver_new_items(
                session,
                webhook_url,
                new_items,
                processed_ids,
                history_file,
            )
    except (CrowdstreamError, HistoryError) as exc:
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
    return await run_bot(webhook_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
