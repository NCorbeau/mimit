"""Register/check the configured webhook without exposing tokens in shell arguments."""

import argparse
import asyncio
import sys

import httpx

from mimit.config import Settings, get_settings
from mimit.telegram.sender import install_log_redaction


async def configure(settings: Settings, *, check: bool = False) -> str:
    settings.require_telegram_configuration()
    install_log_redaction()
    assert settings.telegram_bot_token is not None
    assert settings.telegram_webhook_secret is not None
    assert settings.public_base_url is not None
    method = "getWebhookInfo" if check else "setWebhook"
    endpoint = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}/{method}"
    )
    payload: dict[str, object] = (
        {}
        if check
        else {
            "url": settings.public_base_url + "/telegram/webhook",
            "secret_token": settings.telegram_webhook_secret.get_secret_value(),
            "allowed_updates": ["message"],
            "max_connections": 1,
        }
    )
    try:
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False
            ) as client,
        ):
            response = await client.post(endpoint, json=payload)
            if response.status_code != 200:
                raise RuntimeError(f"Telegram returned HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise RuntimeError("Telegram did not confirm the operation")
            if check:
                result = data.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Telegram returned an invalid webhook status")
                # Do not print raw Telegram errors: they can include URLs or payloads.
                expected = settings.public_base_url + "/telegram/webhook"
                return (
                    f"Webhook matches configuration: {result.get('url') == expected}; "
                    f"pending updates: {result.get('pending_update_count', 'unknown')}; "
                    f"delivery error reported: {'last_error_date' in result}"
                )
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise RuntimeError(
            "Telegram request failed; check connectivity and configuration"
        ) from None
    return "Webhook registered; message updates enabled with one connection."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Inspect registration without changes")
    args = parser.parse_args()
    try:
        print(asyncio.run(configure(get_settings(), check=args.check)))
    except (RuntimeError, ValueError):
        print(
            "Telegram setup failed; check credentials, HTTPS endpoint, and connectivity.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
