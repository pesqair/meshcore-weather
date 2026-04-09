"""CLI tools for testing without hardware.

Usage:
    # Fetch EMWIN data from NOAA and show what we get
    meshcore-weather-cli fetch

    # Query weather from cached/fetched data
    meshcore-weather-cli query "Buffalo NY"

    # Interactive mode - type mesh commands (wx, warn, forecast) in your terminal
    meshcore-weather-cli interactive
"""

import asyncio
import logging
import sys

from meshcore_weather.config import settings
from meshcore_weather.emwin.fetcher import create_source
from meshcore_weather.geodata import resolver
from meshcore_weather.main import WeatherBot
from meshcore_weather.nlp import parse_intent
from meshcore_weather.parser.weather import WeatherStore, paginate


def cmd_fetch():
    """Fetch EMWIN products and print what we got."""
    async def _run():
        source = create_source()
        print(f"Fetching EMWIN data from: {settings.emwin_source}")
        print(f"URL: {settings.emwin_base_url}")
        print()

        await source.start()
        products = await source.fetch_products()
        await source.stop()

        print(f"Fetched {len(products)} products:\n")
        for p in products[:30]:
            awips = p.get("awips_id", "")
            text_preview = p["raw_text"][:70].replace("\n", " ")
            print(f"  {p['product_id']:10s} {p['station']:6s} {awips:10s} | {text_preview}")

        if len(products) > 30:
            print(f"  ... and {len(products) - 30} more")

        return products

    return asyncio.run(_run())


def cmd_query(location: str):
    """Fetch data then query for a location."""
    async def _run():
        resolver.load()
        source = create_source()
        store = WeatherStore()

        print("Fetching EMWIN data...")
        await source.start()
        products = await source.fetch_products()
        await source.stop()

        if not products:
            print("No products available. Run 'fetch' first to check connectivity.")
            return

        store.ingest(products)
        print(f"Loaded {len(products)} products\n")

        print(f"--- Weather for: {location} ---")
        print(store.get_summary(location))

    asyncio.run(_run())


def cmd_interactive():
    """Simulate mesh radio interaction from your terminal."""
    async def _run():
        resolver.load()
        source = create_source()
        store = WeatherStore()
        bot = WeatherBot()

        print("Fetching EMWIN data...")
        await source.start()
        products = await source.fetch_products()
        await source.stop()

        if products:
            store.ingest(products)
            bot.store = store
            print(f"Loaded {len(products)} products")
        else:
            print("Warning: No products fetched. Responses will show 'no data'.")

        print()
        print("=== Interactive Mesh Simulator ===")
        print("Type anything naturally or use commands. Examples:")
        print("  is it raining in austin")
        print("  any storms near miami florida")
        print("  forecast denver")
        print("  wx KAUS")
        print("  more (next page)")
        print("  help")
        print("Type 'quit' to exit.")
        print()

        paging = {}  # {full, offset}

        while True:
            try:
                text = input("mesh> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not text or text.lower() == "quit":
                break

            intent = await parse_intent(text)
            print(f"  [NLP: cmd={intent['command']} loc='{intent['location']}']")

            if intent["command"] == "more":
                if paging:
                    chunk, new_offset, has_more = paginate(paging["full"], paging["offset"])
                    if has_more:
                        paging["offset"] = new_offset
                    else:
                        paging = {}
                    print(f"\n{chunk}\n")
                else:
                    print("\n(no more data)\n")
                continue

            response = bot._process_command(intent["command"], intent["location"])
            if response:
                chunk, offset, has_more = paginate(response, 0)
                if has_more:
                    paging = {"full": response, "offset": offset}
                else:
                    paging = {}
                print(f"\n{chunk}\n")
            else:
                print("\n(no response)\n")

    asyncio.run(_run())


def main():
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: meshcore-weather-cli <command> [args]")
        print()
        print("Commands:")
        print("  fetch              Fetch EMWIN products and show results")
        print("  query <location>   Fetch data and query for a location")
        print("  interactive        Simulate mesh commands from terminal")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "fetch":
        cmd_fetch()
    elif cmd == "query":
        if len(sys.argv) < 3:
            print("Usage: meshcore-weather-cli query <location>")
            sys.exit(1)
        cmd_query(" ".join(sys.argv[2:]))
    elif cmd == "interactive":
        cmd_interactive()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
