from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict

import requests
from bs4 import BeautifulSoup


API_KEY = "G6MQMF5WP3K1YGZBQ2R7GEYOA1J77KZ4YPWRDNQCE2YYYHNV7494MS23M0902IJKHUVNJBZ6RSYUGYFY"
SITEMAP_URL = "https://www.cnblogs.com/dong-bu-dong/sitemap.xml"

MAX_WORKERS = 5
OUTPUT_CSV = "metadata1.csv"


class PageMetadata(TypedDict):
    url: str
    title: str
    description: str


def fetch_sitemap_urls(sitemap_url: str) -> list[str]:
    """Fetch and parse a sitemap XML file."""
    response = requests.get(sitemap_url, timeout=30)
    response.raise_for_status()
    # print(response.text)
    root = ET.fromstring(response.text)

    namespace = {
        "sm": "http://www.sitemaps.org/schemas/sitemap/0.9"
    }

    urls = [
        loc.text.strip()
        for loc in root.findall(".//sm:loc", namespace)
        if loc.text
    ]

    return sorted(set(urls))


def fetch_page_metadata(url: str) -> PageMetadata:
    """Fetch a page through ScrapingBee and extract basic metadata."""
    try:
        response = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={
                "api_key": API_KEY,
                "url": url,
                "render_js": "false",
            },
            timeout=60,
        )

        response.raise_for_status()

    except requests.RequestException as exc:
        print(f"Error fetching {url}: {exc}")

        return {
            "url": url,
            "title": "",
            "description": "",
        }

    soup = BeautifulSoup(response.text, "html.parser")

    title = ""

    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    description = ""

    meta_description = soup.find(
        "meta",
        attrs={"name": "description"},
    )

    if meta_description:
        description = str(meta_description.get("content", "")).strip()

    return {
        "url": url,
        "title": title,
        "description": description,
    }


def save_to_csv(rows: list[PageMetadata], output_file: str) -> None:
    """Save extracted metadata to a CSV file."""
    # print(rows)
    # open方法 encoding='utf-8' 在 csv中会变成 乱码
    with open(output_file, "w", newline="") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=["url", "title", "description"],
        )

        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    urls = fetch_sitemap_urls(SITEMAP_URL)[0:1]

    print(f"Found {len(urls)} URLs in sitemap")

    results: list[PageMetadata] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {
            executor.submit(fetch_page_metadata, url): url
            for url in urls
        }

        for future in as_completed(future_to_url):
            result = future.result()
            results.append(result)

            print(
                f"Processed: {result['url']} | "
                f"Title length: {len(result['title'])}"
            )

    save_to_csv(results, OUTPUT_CSV)

    print(f"\nSaved metadata to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
    # fetch_sitemap_urls(SITEMAP_URL)