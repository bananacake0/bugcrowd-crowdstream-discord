# Bugcrowd CrowdStream Discord bot

Fetches public CrowdStream reports from every paid Bugcrowd bug-bounty program and posts them to Discord. Runs can be started manually from GitHub Actions.

## Current behavior

The first run performs a complete backfill. It:

1. Fetches every page of Bugcrowd's paid `bug_bounty` program catalog. VDPs are excluded.
2. Probes page 1 of every program's engagement-specific CrowdStream.
3. Treats a genuine HTTP 404 as CrowdStream disabled for that program.
4. Reads `pagination_meta.total_pages` and fetches every advertised page, from the oldest page down to page 1.
5. Merges reports from every program, removes duplicate or previously delivered submission IDs, and globally sorts the remaining reports oldest-first by acceptance or disclosure date.
6. Logs fetched, unique, duplicate, and conflicting duplicate records.
7. Delivers the reports to Discord and records each successfully delivered batch.
8. Writes `full_scan_complete: true` only after the complete scan and all report deliveries succeed.

After that marker is present, each run still refreshes every paid-program catalog page and detects additions/removals, but checks only page 1 of each enabled CrowdStream. This keeps routine runs short while the processed-ID history prevents duplicates. Delete `scan_state.json` or set its marker to `false` to request another full backfill.

"All reports" means every report exposed by the programs' public CrowdStream JSON endpoints. It does not include private submissions or reports Bugcrowd does not publish to CrowdStream.

Bugcrowd requests are sequential. HTTP 429 and server errors are retried, including Bugcrowd's `Retry-After` delay, so rate limiting is never mistaken for a disabled CrowdStream.

## Discord messages

Report embeds can include:

- Report or acceptance title
- Researcher and profile link when Bugcrowd makes the name visible
- Engagement name and link
- Reward when `amount` is not null
- Priority and matching sidebar color
- Acceptance or disclosure date
- Original submission date from `created_at`
- Program logo thumbnail

Discord messages contain at most ten embeds and respect the 6,000-character embed limit. The bot pauses for at least three seconds between messages and retries rate limits and temporary server errors. If a batch fails permanently, delivery stops to preserve oldest-to-newest order. IDs from earlier successful batches remain saved, so the next manual run resumes safely.

When `DISCORD_PROGRAMS_WEBHOOK_URL` is configured, programs entering or leaving the paid catalog are posted to a separate Discord channel.

New-program alerts also include metadata from the program's latest public changelog: industry, status, participation, reward model, publish date, in-scope target count, logo, and a short tagline.

## State files

The repository intentionally starts with valid empty state files:

| File                 | Purpose                                                                  |
| -------------------- | ------------------------------------------------------------------------ |
| `programs.json`      | Current paid-program catalog, names, slugs, and CrowdStream availability |
| `processed_ids.json` | Submission IDs successfully delivered to Discord                         |
| `scan_state.json`    | Whether the initial all-pages CrowdStream scan completed successfully    |

On the first run, the bot repopulates `programs.json`, audits duplicate IDs, and treats every currently visible CrowdStream report as new. During the latest development scan on 30 August 2026, the complete backfill contained 3,832 unique reports, or about 384 Discord messages. The live total will change.

If the program-changes webhook is enabled while `programs.json` is empty, the first run also announces every paid program as newly added.

Both files are written atomically. Preserve them between runs unless a deliberate full backfill is required.

If an existing VPS already completed its full backfill before `scan_state.json` was added, seed the marker once before the next run:

```bash
sudo -u crowdstream sh -c \
  'printf "{\\n  \\\"full_scan_complete\\\": true\\n}\\n" > /var/lib/bugcrowd-crowdstream-discord/scan_state.json'
```

## Requirements

- Linux or macOS
- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- A Discord webhook for report alerts
- An optional second Discord webhook for paid-program changes

## Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Configure these values:

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | Yes | None | Webhook receiving report embeds |
| `DISCORD_PROGRAMS_WEBHOOK_URL` | No | None | Webhook receiving paid-program additions and removals |
| `CROWDSTREAM_HISTORY_FILE` | No | `processed_ids.json` | Persistent delivered-submission state |
| `CROWDSTREAM_PROGRAMS_FILE` | No | `programs.json` | Persistent paid-program catalog state |
| `CROWDSTREAM_SCAN_STATE_FILE` | No | `scan_state.json` | Initial full-scan completion marker |

Never commit real webhook URLs. Rotate any webhook that has been exposed in chat, logs, or version control.

If program-change alerts are not needed, remove `DISCORD_PROGRAMS_WEBHOOK_URL` instead of leaving the example placeholder configured.

## Run locally

Install the locked dependencies and run one scan:

```bash
uv sync --locked
uv run --locked main.py
```

The first command may take several minutes because it fetches every CrowdStream page sequentially. Later commands fetch page 1 only and honor Bugcrowd rate limits.

## GitHub Actions

The `CrowdStream to Discord` workflow runs every three hours, anchored at 4:17 PM East Africa Time. Daily runs occur at 1:17 AM, 4:17 AM, 7:17 AM, 10:17 AM, 1:17 PM, 4:17 PM, 7:17 PM, and 10:17 PM EAT. It can also be started manually with **Actions → CrowdStream to Discord → Run workflow**.

Add `DISCORD_WEBHOOK_URL` and optional `DISCORD_PROGRAMS_WEBHOOK_URL` as repository secrets. The workflow runs the checks, validates the persisted state, executes `uv run --locked main.py`, and commits updated `processed_ids.json`, `programs.json`, and `scan_state.json` back to the default branch.

## Development checks

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked -m unittest discover -s tests -v
```

The current suite contains 35 tests covering catalog pagination, complete CrowdStream pagination, page-one incremental scans, global ordering, date semantics, embed construction, retries, Discord batching, atomic state persistence, empty-state bootstrap, and VPS state-path configuration.
