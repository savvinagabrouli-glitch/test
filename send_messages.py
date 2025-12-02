import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

LOGGER = logging.getLogger(__name__)

RECIPIENT_RE = re.compile(r"/reply/(\d+)")


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_recipient_id(url: str) -> str:
    match = RECIPIENT_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract recipient id from {url}")
    return match.group(1)


def render_message(template: str, variables: Dict[str, Any]) -> str:
    try:
        return template.format(**variables)
    except KeyError as exc:  # pragma: no cover - straightforward formatting error
        missing = exc.args[0]
        raise KeyError(f"Missing template variable: {missing}") from exc


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"sent": {}}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def message_fingerprint(recipient_id: str, message: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(recipient_id.encode("utf-8"))
    hasher.update(message.encode("utf-8"))
    return hasher.hexdigest()


def should_skip(state: Dict[str, Any], fingerprint: str) -> bool:
    return fingerprint in state.get("sent", {})


def record_sent(state: Dict[str, Any], fingerprint: str, metadata: Dict[str, Any]) -> None:
    state.setdefault("sent", {})[fingerprint] = metadata


def append_log(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def rate_limit_sleep(last_sent_at: Optional[float], min_delay: int, max_delay: int) -> float:
    if last_sent_at is None:
        return time.time()
    now = time.time()
    target_delay = random.randint(min_delay, max_delay)
    elapsed = now - last_sent_at
    sleep_for = max(0.0, target_delay - elapsed)
    if sleep_for > 0:
        LOGGER.info("Sleeping %.1f seconds to respect rate limit", sleep_for)
        time.sleep(sleep_for)
    return time.time()


def send_via_api(
    api_base_url: str,
    token: str,
    recipient_id: str,
    message: str,
    max_attempts: int = 5,
    initial_backoff: float = 2.0,
) -> Tuple[bool, str]:
    url = f"{api_base_url.rstrip('/')}/send"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"recipientId": recipient_id, "body": message}
    backoff = initial_backoff

    for attempt in range(1, max_attempts + 1):
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.ok:
            return True, "sent"
        if response.status_code in (429, 500, 502, 503, 504):
            LOGGER.warning(
                "Attempt %s failed with status %s; backing off %.1fs",
                attempt,
                response.status_code,
                backoff,
            )
            time.sleep(backoff)
            backoff *= 2
            continue
        try:
            error_detail = response.json()
        except ValueError:
            error_detail = response.text
        return False, f"HTTP {response.status_code}: {error_detail}"
    return False, f"Exceeded retries; last status {response.status_code}"


def process_messages(config: Dict[str, Any], dry_run: bool) -> Dict[str, int]:
    message_template = config["messageTemplate"]
    recipients = config.get("recipients", [])
    accounts = config.get("accounts", [])
    log_path = Path(config.get("logFile", "logs.jsonl"))
    idempotency_path = Path(config.get("idempotencyFile", ".send_messages_state.json"))
    api_base_url = config.get("apiBaseUrl", "https://api.leboncoin.fr/messaging")
    min_delay = int(config.get("minDelaySeconds", 180))
    max_delay = int(config.get("maxDelaySeconds", 300))

    state = load_state(idempotency_path)
    summary = {"sent": 0, "skipped": 0, "failed": 0, "dry_run": 0}

    last_sent_per_account: Dict[str, Optional[float]] = {acc.get("label", acc.get("token")): None for acc in accounts}

    for account in accounts:
        account_label = account.get("label", account.get("token", "unknown"))
        token = account["token"]
        LOGGER.info("Processing account %s", account_label)

        for recipient in recipients:
            recipient_url = recipient if isinstance(recipient, str) else recipient.get("url", "")
            recipient_vars = recipient.get("variables", {}) if isinstance(recipient, dict) else {}
            recipient_id = parse_recipient_id(recipient_url)
            message = render_message(message_template, recipient_vars)
            fingerprint = message_fingerprint(recipient_id, message)

            now_iso = datetime.utcnow().isoformat() + "Z"

            if should_skip(state, fingerprint):
                summary["skipped"] += 1
                append_log(
                    log_path,
                    {
                        "timestamp": now_iso,
                        "account": account_label,
                        "recipient": recipient_id,
                        "status": "skipped",
                        "error": "duplicate",
                    },
                )
                continue

            last_sent = last_sent_per_account.get(account_label)
            last_sent_per_account[account_label] = rate_limit_sleep(last_sent, min_delay, max_delay)

            record = {
                "timestamp": now_iso,
                "account": account_label,
                "recipient": recipient_id,
                "status": "",  # to be updated
                "error": "",
            }

            if dry_run:
                record["status"] = "dry-run"
                append_log(log_path, record)
                summary["dry_run"] += 1
                continue

            try:
                ok, message_status = send_via_api(api_base_url, token, recipient_id, message)
                record["status"] = "sent" if ok else "failed"
                record["error"] = "" if ok else message_status
                append_log(log_path, record)
                if ok:
                    record_sent(state, fingerprint, {"account": account_label, "recipient": recipient_id})
                    summary["sent"] += 1
                else:
                    summary["failed"] += 1
            except Exception as exc:  # pragma: no cover - logging fallback
                record["status"] = "failed"
                record["error"] = str(exc)
                append_log(log_path, record)
                summary["failed"] += 1

    save_state(idempotency_path, state)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Send messages via leboncoin API")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Simulate sending messages")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config_path = Path(args.config)
    if not config_path.exists():
        LOGGER.error("Config file %s does not exist", config_path)
        return 1

    try:
        config = load_config(config_path)
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.error("Failed to load config: %s", exc)
        return 1

    summary = process_messages(config, args.dry_run)
    print("Summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
