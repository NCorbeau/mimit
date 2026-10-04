"""Run one bounded product check for an existing consumable (MAC-31)."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from uuid import UUID

from mimit.clock import SystemClock
from mimit.config import get_settings
from mimit.db.session import create_engine, get_session_factory
from mimit.observability import configure_logging
from mimit.products.extractor import JsonLdExtractor
from mimit.products.fetcher import SafeProductFetcher
from mimit.products.service import PriceCheckServiceError, ProductCheckService


async def _run(consumable_id: UUID, household_id: UUID | None) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with SafeProductFetcher() as fetcher:
            service = ProductCheckService(
                get_session_factory(engine), SystemClock(), fetcher, JsonLdExtractor()
            )
            result = await service.check(consumable_id, household_id=household_id)
        print(
            json.dumps(
                {
                    "consumable_id": str(result.consumable_id),
                    "observation_id": str(result.observation_id),
                    "observed_at": result.observed_at.isoformat(),
                    "outcome": result.outcome,
                    "product_name": result.product_name,
                    "variant": result.variant,
                    "price": str(result.price) if result.price is not None else None,
                    "currency": result.currency,
                    "unit_price": str(result.unit_price) if result.unit_price is not None else None,
                    "unit": result.unit,
                    "availability": result.availability,
                    "error_code": result.error_code,
                },
                ensure_ascii=False,
            )
        )
        return 0 if result.outcome == "success" else 2
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("consumable_id", type=UUID, help="Existing item UUID from /stock")
    parser.add_argument("--household-id", type=UUID, help="Optionally restrict household ownership")
    args = parser.parse_args(argv)
    configure_logging()
    try:
        return asyncio.run(_run(args.consumable_id, args.household_id))
    except PriceCheckServiceError as error:
        print(json.dumps({"outcome": "error", "error_code": error.code}))
    except KeyboardInterrupt:
        print(json.dumps({"outcome": "cancelled"}))
        return 130
    except Exception:
        # Database and transport exceptions may contain secrets or URLs.
        print(json.dumps({"outcome": "error", "error_code": "configuration_or_runtime_error"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
