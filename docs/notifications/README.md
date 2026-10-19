# Cleaner trading messages

Presentation changes based on PR #8 (`79879a1`). Nothing in this change deploys Lambda, publishes notifications, promotes strategies, or modifies the broker book.

## Message layout

- BUY and SELL cards lead with symbol and quantity, followed by entry/limit, stop/target where supplied, cash/risk and validity. Every supplied action is retained.
- WATCH candidates are explicitly non-actionable; only three are shown in the scan notification, with the rest referenced in the existing scan log. Unvalidated model probabilities are omitted from the human-facing message.
- Holdings cards show breakeven, remaining T1/T2 quantities and trailing status. Completed tranches disappear. System trades explicitly retain loss exits. Repeated unconfirmed SELL instructions carry a reminder to check broker fills.
- Informational monitor events no longer receive an “action required” subject/high priority. Existing delivery cadence is preserved: monitor messages are sent only when the original `events` list is nonempty. A no-event HOLD preview is available locally, not a new scheduled notification.
- Missing prices and old equity marks are labeled. Missing/non-finite values display `Unavailable`; real zero remains zero. Alert times are IST; completed-session dates remain separate.
- Paper reports are headed `PAPER`, distinguish evaluated/no-setup from blocked/not-evaluated, and show snapshot activity separately from cumulative book counts. These reports are local only.

## Channels

ntfy receives plain text, divided at card boundaries into numbered parts if necessary. Each UTF-8 body is at most 3,800 bytes, below ntfy's documented 4,096-byte message threshold. No BUY/SELL is silently dropped. Oversized individual lines split safely between Unicode characters. Sending failures are logged; this change does not promise delivery or add retries.

Gmail receives multipart email with the complete plain-text version and a mobile-friendly HTML version. HTML uses inline styles, escaped input, system fonts and no external assets. Both transports retain the existing configuration. The formatter adds only standard-library dependencies.

Source: https://docs.ntfy.sh/publish/

## Inspect without sending

```bash
python scripts/preview_messages.py
python -m pytest -q tests/test_messages.py
python -m pytest -q
```

Open `docs/notifications/previews/*.html` for email or `*.txt` for push layout. Every preview is prominently marked illustrative and is not a signal or a report of current holdings. Preview prices, dates and quantities do not establish trading eligibility.

The existing research report command also writes `artifacts/v5/forward/message_preview.txt` and `.html` from its ledger inputs:

```bash
python -m backtest.v5.report --ledger artifacts/v5/forward/paper_book.sqlite
```

That command retains its existing local report writes and creates no notifications. It does not advance the book.

## Validation and deployment boundary

Notification tests cover action preservation, remaining tranches, repeated intent labeling, system-loss exits, missing data, research counts, HTML escaping, UTF-8 chunk limits and mocked ntfy/SMTP delivery. They do not test a real phone or Gmail inbox, and no real test notification was sent.

Review this change against the PR #8 branch. That research branch includes earlier engine work which is not the deployed October code. Do not deploy the whole branch solely to obtain these UI changes. First reconcile the actual deployed source and port/review the presentation changes against that source; then use the existing deployment process with explicit approval. No risk, strategy, promotion, EventBridge, model, S3-state or execution-policy change is part of this presentation work.

Validation in this workspace: 121 tests passed (109 existing + 12 notification tests); syntax compilation and diff whitespace checks passed. Browser rendering could not be inspected because the Chromium download failed. HTML structure and escaping were tested, but actual Gmail/ntfy client rendering still needs a review before deployment.
