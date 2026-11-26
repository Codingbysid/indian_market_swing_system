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
python -m pytest -q tests/test_messages.py
python -m pytest -q
```

## Validation and deployment boundary

Notification tests cover action preservation, remaining tranches, repeated intent labeling, system-loss exits, missing data, research counts, HTML escaping, UTF-8 chunk limits and mocked ntfy/SMTP delivery. They do not test a real phone or Gmail inbox, and no real test notification was sent.

Legacy v3–v6 model packages (`recommender`, `backtest`, `swing_v5`, `research_v6`) were removed in the clean-slate refactor. Presentation changes here must stay on the stubbed 5-slot Lambda path; do not reintroduce purged model modules to ship message formatting.
