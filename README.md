# Bugcrowd CrowdStream Discord bot

Posts new Bugcrowd CrowdStream acceptances and disclosures to Discord. This
proof monitors pages 2 and 1 of the engagement-specific CrowdStreams enabled in
`programs.json`: Atlassian, Nubank, OpenAI, Rapyd, and Tesla. LaunchDarkly is
recorded but disabled because its CrowdStream endpoint returns HTTP 404. Results
from every program are merged and sorted together by acceptance or disclosure
date before delivery, and IDs already recorded in `processed_ids.json` are
skipped. Discord deliveries are grouped into batches of up to ten reports with
a three-second pause between batches. Embeds retain the program logo and use a
compact priority badge matching the reference layout.

## Local run

1. Install [uv](https://docs.astral.sh/uv/).
2. Copy `.env.example` to `.env` and add your Discord webhook URL.
3. Run `uv run --locked main.py`.

## GitHub Actions

The workflow in `.github/workflows/crowdstream.yml` runs at the beginning of
every hour (`12:00`, `1:00`, `2:00`, and so on) and can also be started manually
from the Actions tab.

1. Create a GitHub repository and push this project to its default branch.
2. Open **Settings → Secrets and variables → Actions**.
3. Add a repository secret named `DISCORD_WEBHOOK_URL` containing the complete
   Discord webhook URL.
4. Open **Actions → CrowdStream to Discord → Run workflow** for the first run.

The workflow commits `processed_ids.json` to the default branch after
deliveries. This state is required because GitHub-hosted runners are temporary.
The workflow grants only `contents: write`, prevents overlapping runs, and
persists successful batches even if a later batch fails.

If the state commit is rejected, allow GitHub Actions to write repository
contents under **Settings → Actions → General → Workflow permissions** and make
sure branch protection permits the `github-actions[bot]` update.
