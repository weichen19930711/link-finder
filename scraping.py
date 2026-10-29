from __future__ import annotations

import json
import logging
import os
import re
import time
from hashlib import blake2b
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import xmltodict
from dotenv import load_dotenv
from scrapingbee import ScrapingBeeClient
from tqdm import tqdm


# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------

load_dotenv()


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

SITEMAP_URL: str = "https://www.walmart.com/sitemap_category.xml"
SITE_NAME: str = "walmart"

CONCURRENCY_LIMIT: int = 5
MAX_URLS_TO_SCRAPE: int | None = 3  # Use None for a full run.

BASE_DIR: Path = Path("product-data")
DATA_DIR: Path = BASE_DIR / "data"
LOG_FILE: Path = BASE_DIR / "logs.csv"
CSV_OUTPUT: Path = BASE_DIR / "data.csv"
URL_OUTPUT: Path = Path(f"{SITE_NAME}_urls.txt")


# -----------------------------------------------------------------------------
# API client
# -----------------------------------------------------------------------------

SB_API_KEY: str | None = os.getenv("SCRAPINGBEE_API_KEY")

if not SB_API_KEY:
    raise RuntimeError(
        "Missing SCRAPINGBEE_API_KEY. Add it to your .env file before running the script."
    )

client = ScrapingBeeClient(api_key=SB_API_KEY)


# -----------------------------------------------------------------------------
# Types and shared state
# -----------------------------------------------------------------------------

class ScrapeLog(TypedDict):
    url: str
    status_code: int | None
    resolved_url: str
    message: str
    elapsed_seconds: float


iteration_times: list[float] = []
log_entries: list[ScrapeLog] = []


# -----------------------------------------------------------------------------
# Extraction rules
# -----------------------------------------------------------------------------

AI_EXTRACT_RULES: dict[str, dict[str, str]] = {
    "name": {
        "description": "Product name",
        "type": "string",
    },
    "description": {
        "description": "Product description",
        "type": "string",
    },
    "original_price": {
        "description": "Original price before discount, if available",
        "type": "string",
    },
    "offer_price": {
        "description": "Current offer price or sale price, if available",
        "type": "string",
    },
    "availability": {
        "description": "Product availability or stock status",
        "type": "string",
    },
    "rating": {
        "description": "Average customer rating, if available",
        "type": "string",
    },
    "review_count": {
        "description": "Number of customer reviews, if available",
        "type": "string",
    },
    "link": {
        "description": "Canonical product URL",
        "type": "string",
    },
}


# -----------------------------------------------------------------------------
# Setup helpers
# -----------------------------------------------------------------------------

def setup_logging() -> None:
    """Configure console logging for the script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def ensure_output_dirs() -> None:
    """Create output directories if they do not exist yet."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Sitemap discovery
# -----------------------------------------------------------------------------

def fetch_sitemap_urls(
    sitemap_url: str = SITEMAP_URL,
    output_file: Path = URL_OUTPUT,
    save_output: bool = True,
) -> list[str]:
    """Fetch URLs from a sitemap or sitemap index."""
    logging.info("Fetching sitemap: %s", sitemap_url)

    response = client.html_api(
        sitemap_url,
        params={
            "render_js": False,
            "premium_proxy": True,
            "country_code": "us",
        },
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch sitemap {sitemap_url}. "
            f"Status code: {response.status_code}"
        )

    sitemap_data = xmltodict.parse(response.text)
    urls: list[str] = []

    if "urlset" in sitemap_data:
        url_entries = sitemap_data["urlset"].get("url", [])

        if isinstance(url_entries, dict):
            url_entries = [url_entries]

        urls = [
            entry["loc"].strip()
            for entry in url_entries
            if isinstance(entry, dict) and entry.get("loc")
        ]

    elif "sitemapindex" in sitemap_data:
        sitemap_entries = sitemap_data["sitemapindex"].get("sitemap", [])

        if isinstance(sitemap_entries, dict):
            sitemap_entries = [sitemap_entries]

        child_sitemaps = [
            entry["loc"].strip()
            for entry in sitemap_entries
            if isinstance(entry, dict) and entry.get("loc")
        ]

        logging.info("Found %d child sitemaps.", len(child_sitemaps))

        for child_sitemap_url in child_sitemaps:
            urls.extend(
                fetch_sitemap_urls(
                    sitemap_url=child_sitemap_url,
                    output_file=output_file,
                    save_output=False,
                )
            )

    else:
        raise ValueError(f"Unsupported or empty sitemap structure: {sitemap_url}")

    urls = sorted(set(urls))

    if save_output:
        output_file.write_text("\n".join(urls), encoding="utf-8")
        logging.info("Saved %d URLs to %s.", len(urls), output_file)

    return urls


# -----------------------------------------------------------------------------
# URL and JSON helpers
# -----------------------------------------------------------------------------

def extract_slug(url: str) -> str:
    """Create a safe, mostly human-readable, unique slug from a URL."""
    parsed = urlparse(url)

    path_slug = parsed.path.rstrip("/").split("/")[-1].strip().lower()
    safe_slug = re.sub(r"[^a-z0-9_-]+", "-", path_slug)
    safe_slug = re.sub(r"-+", "-", safe_slug).strip("-")

    if not safe_slug:
        safe_slug = "page"

    url_hash = blake2b(url.encode("utf-8"), digest_size=6).hexdigest()

    return f"{safe_slug}-{url_hash}"


def parse_json_response(response_text: str) -> dict[str, Any]:
    """Parse a JSON response, allowing for occasional fenced JSON output."""
    cleaned_text = response_text.strip()

    if cleaned_text.startswith("```json"):
        cleaned_text = cleaned_text.removeprefix("```json").strip()

    if cleaned_text.startswith("```"):
        cleaned_text = cleaned_text.removeprefix("```").strip()

    if cleaned_text.endswith("```"):
        cleaned_text = cleaned_text.removesuffix("```").strip()

    parsed = json.loads(cleaned_text)

    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object from the extraction response.")

    return parsed


# -----------------------------------------------------------------------------
# Scraping
# -----------------------------------------------------------------------------

def scrape_product(url: str) -> ScrapeLog:
    """Scrape a page, save extracted JSON data, and return a log entry."""
    start_time = time.time()
    resolved_url = ""

    try:
        response = client.html_api(
            url,
            params={
                "premium_proxy": True,
                "country_code": "us",
                "render_js": True,
                "ai_extract_rules": AI_EXTRACT_RULES,
            },
        )

        status_code = response.status_code
        resolved_url = response.headers.get("spb-resolved-url", url)

        if status_code != 200:
            return {
                "url": url,
                "status_code": status_code,
                "resolved_url": resolved_url,
                "message": "Scraping failed",
                "elapsed_seconds": round(time.time() - start_time, 3),
            }

        try:
            extracted_data = parse_json_response(response.text)
        except (json.JSONDecodeError, ValueError) as exc:
            logging.error(
                "JSON parsing error for %s: %s. Response snippet: %s",
                url,
                exc,
                response.text[:500],
            )

            return {
                "url": url,
                "status_code": status_code,
                "resolved_url": resolved_url,
                "message": "JSON parsing error",
                "elapsed_seconds": round(time.time() - start_time, 3),
            }

        output_path = DATA_DIR / f"{extract_slug(url)}.json"
        output_path.write_text(
            json.dumps(extracted_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return {
            "url": url,
            "status_code": status_code,
            "resolved_url": resolved_url,
            "message": "Success",
            "elapsed_seconds": round(time.time() - start_time, 3),
        }

    except Exception as exc:
        logging.exception("Unexpected error while scraping %s", url)

        return {
            "url": url,
            "status_code": None,
            "resolved_url": resolved_url,
            "message": str(exc),
            "elapsed_seconds": round(time.time() - start_time, 3),
        }

    finally:
        elapsed = time.time() - start_time
        iteration_times.append(elapsed)

        if len(iteration_times) > 100:
            iteration_times.pop(0)


def execute_scraping(
    urls: list[str],
    max_urls: int | None = 100,
) -> list[ScrapeLog]:
    """Run scraping tasks concurrently and return scraping logs."""
    if max_urls is not None:
        urls = urls[:max_urls]

    if not urls:
        logging.warning("No URLs provided for scraping.")
        return []

    logging.info("Starting concurrent scraping for %d URLs.", len(urls))

    logs: list[ScrapeLog] = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY_LIMIT) as executor:
        futures = {
            executor.submit(scrape_product, url): url
            for url in urls
        }

        with tqdm(
            total=len(futures),
            desc="Scraping progress",
            dynamic_ncols=True,
        ) as progress_bar:
            for future in as_completed(futures):
                url = futures[future]

                try:
                    result = future.result()
                except Exception as exc:
                    logging.exception("Unhandled scraping error for %s", url)

                    result: ScrapeLog = {
                        "url": url,
                        "status_code": None,
                        "resolved_url": "",
                        "message": str(exc),
                        "elapsed_seconds": 0.0,
                    }

                logs.append(result)

                median_time = float(np.median(iteration_times)) if iteration_times else 0.0
                urls_per_second = (1 / median_time) if median_time > 0 else 0.0

                progress_bar.set_postfix(
                    {
                        "median_s/url": f"{median_time:.2f}",
                        "url/s": f"{urls_per_second:.2f}",
                    }
                )
                progress_bar.update(1)

    log_entries[:] = logs

    logging.info("Finished scraping %d URLs.", len(logs))

    return logs


# -----------------------------------------------------------------------------
# Output processing
# -----------------------------------------------------------------------------

def process_scraped_data() -> pd.DataFrame | None:
    """Combine scraped JSON files into a CSV file."""
    json_files = sorted(DATA_DIR.glob("*.json"))

    if not json_files:
        logging.warning("No JSON files found in %s.", DATA_DIR)
        return None

    dataframes: list[pd.DataFrame] = []
    error_files: list[Path] = []

    logging.info("Processing %d JSON files into CSV.", len(json_files))

    for file_path in json_files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))

            if isinstance(data, list):
                df = pd.json_normalize(data)
            elif isinstance(data, dict):
                df = pd.json_normalize([data])
            else:
                raise ValueError(f"Unexpected JSON format: {type(data).__name__}")

            df["source_file"] = file_path.name
            dataframes.append(df)

        except Exception as exc:
            logging.error("Error reading %s: %s", file_path, exc)
            error_files.append(file_path)

    if not dataframes:
        logging.warning("No valid JSON data found to save.")
        return None

    final_df = pd.concat(dataframes, ignore_index=True)
    final_df.to_csv(CSV_OUTPUT, index=False, encoding="utf-8")

    logging.info("Data successfully saved to %s.", CSV_OUTPUT)

    if error_files:
        logging.warning("The following files caused errors:")
        for error_file in error_files:
            logging.warning(" - %s", error_file)

    return final_df


def save_logs(logs: list[ScrapeLog] | None = None) -> pd.DataFrame:
    """Save scraping logs to a CSV file and return the logs DataFrame."""
    if logs is None:
        logs = log_entries

    logs_df = pd.DataFrame(logs)

    if logs_df.empty:
        logging.warning("No log entries to save.")
        # logs_df = pd.DataFrame(columns=ScrapeLog.__annotations__.keys())

    logs_df.to_csv(LOG_FILE, index=False, encoding="utf-8")

    logging.info("Logs saved to %s.", LOG_FILE)

    return logs_df


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    """Run the sitemap discovery, scraping, logging, and CSV export workflow."""
    setup_logging()
    ensure_output_dirs()

    log_entries.clear()
    iteration_times.clear()

    sitemap_urls = fetch_sitemap_urls()

    if not sitemap_urls:
        logging.error("No URLs found in the sitemap. Exiting.")
        return

    logging.info("Found %d URLs in the sitemap.", len(sitemap_urls))

    logs = execute_scraping(
        sitemap_urls,
        max_urls=MAX_URLS_TO_SCRAPE,
    )

    save_logs(logs)
    process_scraped_data()

    successful_requests = sum(1 for log in logs if log["message"] == "Success")
    failed_requests = len(logs) - successful_requests

    logging.info(
        "Script execution completed. Successful: %d. Failed: %d.",
        successful_requests,
        failed_requests,
    )


if __name__ == "__main__":
    main()