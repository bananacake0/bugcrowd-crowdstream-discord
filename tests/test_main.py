import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


class FakeResponse:
    def __init__(self, status, *, payload=None, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, **kwargs):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class DateAndEmbedTests(unittest.TestCase):
    def test_format_iso_date(self):
        self.assertEqual(
            main.format_iso_date("2026-07-12T16:38:11.110Z"), "12 Jul 2026"
        )
        self.assertEqual(main.format_iso_date("not-a-date"), "not-a-date")
        self.assertEqual(main.format_iso_date(None), "")

    def test_disclosure_embed_uses_disclosure_date_and_link(self):
        embed = main.build_discord_embed(
            {
                "engagement_name": "NASA VDP",
                "engagement_path": "/engagements/nasa-vdp",
                "created_at": "2026-07-12T16:38:11.110Z",
                "accepted_at": "13 Jul 2026",
                "disclosed_at": "27 Aug 2026",
                "disclosed": True,
                "title": "Disclosure example",
                "disclosure_report_url": "/disclosures/example",
                "priority": 5,
                "logo_url": "https://example.com/nasa-logo.png",
                "researcher_username": "hidden-user",
                "researcher_profile_path": "https://bugcrowd.com/h/hidden-user",
                "crowdstream_name_visible": False,
            }
        )

        self.assertEqual(embed["title"], "Disclosed: Disclosure example")
        self.assertEqual(embed["url"], "https://bugcrowd.com/disclosures/example")
        self.assertEqual(embed["footer"]["text"], "Submitted on 12 Jul 2026")
        self.assertEqual(
            embed["thumbnail"]["url"],
            "https://example.com/nasa-logo.png",
        )
        self.assertEqual(
            embed["fields"][-1],
            {
                "name": "\u200b",
                "value": "Disclosed on 27 Aug 2026",
                "inline": False,
            },
        )
        self.assertNotIn("Researcher", {field["name"] for field in embed["fields"]})

    def test_acceptance_embed_uses_acceptance_date(self):
        embed = main.build_discord_embed(
            {
                "engagement_name": "Example",
                "engagement_path": "/engagements/example",
                "submission_state_text": "Submission accepted on target: example.com",
                "accepted_at": "28 Aug 2026",
                "submission_state_date_text": "Accepted on 28 Aug 2026",
                "created_at": "2026-08-20T12:00:00Z",
                "amount": "$500",
                "priority": 3,
                "researcher_username": "researcher",
                "researcher_profile_path": "/researchers/researcher",
                "crowdstream_name_visible": True,
                "logo_url": "https://example.com/program-logo.png",
            }
        )

        self.assertEqual(embed["title"], "Submission accepted on target: example.com")
        self.assertEqual(embed["footer"]["text"], "Submitted on 20 Aug 2026")
        self.assertEqual(
            embed["thumbnail"], {"url": "https://example.com/program-logo.png"}
        )
        self.assertEqual(
            [field["name"] for field in embed["fields"]],
            ["Researcher", "Engagement", "Reward", "Priority", "\u200b"],
        )
        self.assertEqual(embed["fields"][2]["value"], "$500")
        self.assertEqual(embed["fields"][3]["value"], "`P3`")
        self.assertEqual(
            embed["fields"][-1],
            {
                "name": "\u200b",
                "value": "Accepted on 28 Aug 2026",
                "inline": False,
            },
        )

    def test_each_priority_has_readable_text_badge_and_sidebar_color(self):
        for priority in main.COLOR_BY_PRIORITY:
            with self.subTest(priority=priority):
                embed = main.build_discord_embed({"priority": priority})
                priority_field = next(
                    field for field in embed["fields"] if field["name"] == "Priority"
                )

                self.assertEqual(priority_field["value"], f"`P{priority}`")
                self.assertEqual(embed["color"], main.COLOR_BY_PRIORITY[priority])
                self.assertNotIn("thumbnail", embed)

    def test_program_logo_remains_thumbnail_for_high_priority(self):
        embed = main.build_discord_embed(
            {
                "priority": 1,
                "logo_url": "https://example.com/program-logo.png",
            }
        )

        self.assertEqual(
            embed["thumbnail"], {"url": "https://example.com/program-logo.png"}
        )
        self.assertNotIn("Reward", {field["name"] for field in embed["fields"]})

    def test_external_bugcrowd_path_is_rejected(self):
        self.assertEqual(
            main.bugcrowd_url("https://example.com/path"), main.BUGCROWD_ORIGIN
        )

    def test_malformed_optional_fields_use_safe_defaults(self):
        embed = main.build_discord_embed(
            {
                "engagement_name": 123,
                "engagement_path": 456,
                "submission_state_text": [],
                "submission_state_date_text": {},
                "created_at": False,
                "disclosed": True,
                "disclosed_at": None,
                "title": None,
                "disclosure_report_url": 789,
            }
        )

        self.assertEqual(embed["title"], "Disclosed: Report")
        self.assertEqual(embed["footer"]["text"], "Submitted on Unknown")
        self.assertEqual(embed["fields"][-1]["value"], "Disclosed on Recently")

    def test_crowdstream_sort_key_uses_event_date(self):
        reports = [
            {"id": "newest", "accepted_at": "30 Aug 2026"},
            {
                "id": "middle",
                "disclosed": True,
                "disclosed_at": "20 Aug 2026",
            },
            {"id": "oldest", "accepted_at": "5 Aug 2026"},
        ]

        reports.sort(key=main.crowdstream_sort_key)

        self.assertEqual(
            [report["id"] for report in reports], ["oldest", "middle", "newest"]
        )


class HistoryTests(unittest.TestCase):
    def test_missing_history_is_empty_and_save_is_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            self.assertEqual(main.load_processed_ids(history_file), set())

            main.save_processed_ids({"second", "first"}, history_file)

            self.assertEqual(main.load_processed_ids(history_file), {"first", "second"})
            self.assertEqual(json.loads(history_file.read_text()), ["first", "second"])
            self.assertEqual(list(history_file.parent.glob("*.tmp")), [])

    def test_corrupt_history_stops_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            history_file.write_text("not-json", encoding="utf-8")

            with self.assertRaises(main.HistoryError):
                main.load_processed_ids(history_file)

    def test_invalid_history_shape_stops_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            history_file.write_text('{"id": "unexpected"}', encoding="utf-8")

            with self.assertRaises(main.HistoryError):
                main.load_processed_ids(history_file)


class ProgramsTests(unittest.TestCase):
    def test_loads_valid_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            programs_file = Path(directory) / "programs.json"
            programs_file.write_text(
                json.dumps(
                    [
                        {
                            "slug": "atlassian",
                            "name": "Atlassian",
                            "crowdstream_enabled": True,
                        },
                        {
                            "slug": "launchdarkly-mbb-og",
                            "name": "LaunchDarkly",
                            "crowdstream_enabled": False,
                        },
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                main.load_programs(programs_file),
                [
                    {
                        "slug": "atlassian",
                        "name": "Atlassian",
                        "crowdstream_enabled": True,
                    },
                    {
                        "slug": "launchdarkly-mbb-og",
                        "name": "LaunchDarkly",
                        "crowdstream_enabled": False,
                    },
                ],
            )

    def test_rejects_duplicate_or_unsafe_program_slugs(self):
        invalid_program_lists = [
            [
                {
                    "slug": "duplicate",
                    "name": "First",
                    "crowdstream_enabled": True,
                },
                {
                    "slug": "duplicate",
                    "name": "Second",
                    "crowdstream_enabled": True,
                },
            ],
            [
                {
                    "slug": "../unsafe",
                    "name": "Unsafe",
                    "crowdstream_enabled": True,
                }
            ],
        ]

        for programs in invalid_program_lists:
            with self.subTest(programs=programs):
                with tempfile.TemporaryDirectory() as directory:
                    programs_file = Path(directory) / "programs.json"
                    programs_file.write_text(json.dumps(programs), encoding="utf-8")

                    with self.assertRaises(main.ProgramsError):
                        main.load_programs(programs_file)


class SelectionTests(unittest.TestCase):
    def test_selection_skips_processed_invalid_and_duplicate_ids(self):
        results = [
            {"id": "new"},
            {"id": "old"},
            {"id": "new"},
            {"missing": "id"},
            "invalid",
        ]

        self.assertEqual(main.select_new_items(results, {"old"}), [{"id": "new"}])


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_validates_http_status(self):
        session = FakeSession([FakeResponse(503)])

        with self.assertRaises(main.CrowdstreamError):
            await main.fetch_crowdstream_page(session, "atlassian", page=1)

    async def test_fetch_accepts_expected_response(self):
        payload = {"results": [{"id": "one"}]}
        session = FakeSession([FakeResponse(200, payload=payload)])

        self.assertEqual(
            await main.fetch_crowdstream_page(session, "atlassian", page=1),
            payload,
        )
        self.assertEqual(
            session.calls,
            [
                (
                    "GET",
                    "https://bugcrowd.com/engagements/atlassian/crowdstream.json",
                    {"headers": {"User-Agent": main.USER_AGENT}, "params": {"page": 1}},
                )
            ],
        )

    @patch("main.fetch_crowdstream_page", new_callable=AsyncMock)
    async def test_fetches_program_pages_then_globally_sorts_oldest_first(
        self, fetch_page
    ):
        fetch_page.side_effect = [
            {"results": [{"id": "atlassian-older", "accepted_at": "10 Aug 2026"}]},
            {"results": [{"id": "atlassian-new", "accepted_at": "30 Aug 2026"}]},
            {"results": [{"id": "rapyd-oldest", "accepted_at": "5 Aug 2026"}]},
            {"results": [{"id": "rapyd-middle", "accepted_at": "20 Aug 2026"}]},
        ]
        session = object()
        programs = [
            {
                "slug": "atlassian",
                "name": "Atlassian",
                "crowdstream_enabled": True,
            },
            {
                "slug": "launchdarkly",
                "name": "LaunchDarkly",
                "crowdstream_enabled": False,
            },
            {"slug": "rapyd", "name": "Rapyd", "crowdstream_enabled": True},
        ]

        new_items = await main.fetch_new_items(session, set(), programs)

        self.assertEqual(
            [call.args[1] for call in fetch_page.await_args_list],
            ["atlassian", "atlassian", "rapyd", "rapyd"],
        )
        self.assertEqual(
            [call.kwargs["page"] for call in fetch_page.await_args_list],
            [2, 1, 2, 1],
        )
        self.assertEqual(
            [item["id"] for item in new_items],
            [
                "rapyd-oldest",
                "atlassian-older",
                "rapyd-middle",
                "atlassian-new",
            ],
        )

    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    async def test_discord_prefers_json_retry_after_then_succeeds(self, sleep):
        session = FakeSession(
            [
                FakeResponse(
                    429,
                    payload={"retry_after": 0.4, "global": False},
                    headers={
                        "Retry-After": "400",
                        "X-RateLimit-Scope": "shared",
                        "X-RateLimit-Limit": "5",
                        "X-RateLimit-Remaining": "0",
                    },
                ),
                FakeResponse(204),
            ]
        )

        cooldown = await main.send_to_discord(
            session, "https://discord.com/webhook", [{"id": "one"}]
        )

        self.assertEqual(cooldown, 0.0)
        self.assertEqual(len(session.calls), 2)
        sleep.assert_awaited_once_with(0.4)

    async def test_discord_returns_cooldown_when_bucket_is_exhausted(self):
        session = FakeSession(
            [
                FakeResponse(
                    204,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset-After": "1.25",
                    },
                )
            ]
        )

        cooldown = await main.send_to_discord(
            session, "https://discord.com/webhook", [{"id": "one"}]
        )

        self.assertEqual(cooldown, 1.25)

    async def test_discord_does_not_retry_permanent_client_error(self):
        session = FakeSession([FakeResponse(400)])

        with self.assertRaises(main.DiscordDeliveryError):
            await main.send_to_discord(
                session, "https://discord.com/webhook", [{"id": "one"}]
            )
        self.assertEqual(len(session.calls), 1)

    def test_delivery_batches_use_at_most_ten_embeds(self):
        items = [{"id": str(index)} for index in range(23)]

        batches = main.batch_items_for_discord(items)

        self.assertEqual([len(batch) for batch in batches], [10, 10, 3])
        self.assertEqual(
            [item["id"] for batch in batches for item in batch],
            [item["id"] for item in items],
        )

    @patch("main.asyncio.sleep", new_callable=AsyncMock)
    @patch("main.send_to_discord", new_callable=AsyncMock)
    async def test_only_successful_delivery_batches_are_recorded(self, send, sleep):
        send.side_effect = [0.0, main.DiscordDeliveryError("failed")]
        new_items = [{"id": str(index)} for index in range(21)]
        processed_ids = {"existing"}

        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "processed_ids.json"
            failures = await main.deliver_new_items(
                object(),
                "https://discord.com/webhook",
                new_items,
                processed_ids,
                history_file,
            )

            self.assertEqual(failures, 11)
            self.assertEqual(
                main.load_processed_ids(history_file),
                {"existing", *(str(index) for index in range(10))},
            )
            self.assertEqual(
                [
                    [item["id"] for item in call.args[2]]
                    for call in send.await_args_list
                ],
                [
                    [str(index) for index in range(10)],
                    [str(index) for index in range(10, 20)],
                ],
            )
            sleep.assert_awaited_once_with(3.0)


if __name__ == "__main__":
    unittest.main()
