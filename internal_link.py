from __future__ import annotations

import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class Crawler:
    """A small same-domain crawler for discovering internal links."""

    def __init__(
        self,
        start_url: str,
        max_pages: int = 100,
        delay_seconds: float = 1.0,
        user_agent: str = "SimpleCrawler/1.0 (+https://example.com/contact)",
    ) -> None:
        self.start_url = start_url.rstrip("/")
        self.max_pages = max_pages
        self.delay_seconds = delay_seconds
        self.user_agent = user_agent

        parsed_start_url = urlparse(self.start_url)
        self.base_domain = parsed_start_url.netloc

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip, deflate",
            }
        )

        self.visited_urls: set[str] = set()
        self.urls_to_visit: deque[str] = deque([self.start_url])

    def download_url(self, url: str) -> str:
        """Download a URL and return its HTML content."""
        response = self.session.get(url, timeout=15)

        if response.status_code in {403, 429}:
            retry_after = response.headers.get("Retry-After")
            message = f"Rate limited or blocked: {url} ({response.status_code})"

            if retry_after:
                message += f". Retry-After: {retry_after}s"

            print(message)
            return ""

        response.raise_for_status()

        content_type = response.headers.get("content-type", "")

        if "text/html" not in content_type:
            return ""

        return response.text

    def normalize_url(self, current_url: str, href: str | None) -> str | None:
        """Convert a link into a normalized absolute URL."""
        if not href:
            return None

        absolute_url = urljoin(current_url, href)
        absolute_url, _fragment = urldefrag(absolute_url)

        parsed = urlparse(absolute_url)

        if parsed.scheme not in {"http", "https"}:
            return None

        if parsed.netloc != self.base_domain:
            return None

        return absolute_url.rstrip("/")

    def get_linked_urls(self, url: str, html: str) -> set[str]:
        """Extract same-domain links from a page."""
        soup = BeautifulSoup(html, "html.parser")
        linked_urls: set[str] = set()

        for link in soup.find_all("a", href=True):
            normalized_url = self.normalize_url(url, str(link.get("href"))) 

            if normalized_url:
                linked_urls.add(normalized_url)

        return linked_urls

    def add_url_to_visit(self, url: str) -> None:
        """Add a URL to the queue if it has not been visited yet."""
        if url not in self.visited_urls and url not in self.urls_to_visit:
            self.urls_to_visit.append(url)

    def crawl(self, url: str) -> None:
        """Download a page and queue all discovered internal links."""
        html = self.download_url(url)

        if not html:
            return

        for linked_url in self.get_linked_urls(url, html):
            self.add_url_to_visit(linked_url)

    def run(self) -> set[str]:
        """Run the crawler and return all visited URLs."""
        while self.urls_to_visit and len(self.visited_urls) < self.max_pages:
            url = self.urls_to_visit.popleft()

            if url in self.visited_urls:
                continue

            try:
                print(f"Crawling: {url}")
                self.crawl(url)
            except requests.RequestException as exc:
                print(f"Failed to crawl {url}: {exc}")
            finally:
                self.visited_urls.add(url)
                time.sleep(self.delay_seconds)

        return self.visited_urls


if __name__ == "__main__":
    crawler = Crawler(
        start_url="https://en.wikipedia.org/",
        max_pages=100,
        delay_seconds=2.0,
        # Replace with your actual contact info:
        user_agent="SimpleCrawlerDemo/1.0 (+https://example.com/contact)",
    )

    urls = crawler.run()

    print(f"\nFound {len(urls)} URLs:")
    for url in sorted(urls):
        print(url)
        
# 如何发现隐藏的页面
'''
1 出现隐藏页面的一些原因
    1、old landing pages 已经从导览页面 移除, 但是没有被删除
    2、在网站迁移时, 产品或类别 页面被替换掉了, 旧页面没有被删除
    3、为了 广告或用于邮件流量 创建的 营销活动（campaign）页面。
    4、存在于内容管理系统（CMS）中的页面, 但是内部没有链接
    5、这些 URL 依然通过外部链接或搜索引擎获取流量。但是没有出现在内部链接中
    
2 如何去发现隐藏页面
    1、查看server 日志文件, 展示用户或机器人请求的URLs, 可能包含 没有被内部链接的 页面
    2、Google Analytics - 即使页面无法通过网站导航找到，也能发现那些仍有流量的页面。
    3、Google Search Console -通过 Google Search Console, 可显示 your crawler没有发现，但是被索引或被点击的 URLs
    4、查看网站的 XML sitemaps, 可能包含 没有被内部链接的 页面
    5、采用Backlink tools,  可以找出那些仍被外部网站链接的页面
    
3 实际工作流程
    1、Crawl the website with a spider tool and export all discovered internal URLs.
    2、从Google Analytics 导出landing page URLS
    3、从Google Search Console 导出 被索引或被点击的 URLs
    4、从server log 导出 访问的URLs 
    5、Compare the lists and look for URLs that appear in analytics, logs, Search Console, or sitemaps, but not in your crawl.
    
发现这些隐藏页面后，
    如果你是网站owner, 可以采取相应行动
         Important pages should be linked from relevant internal pages. 
         Outdated pages should be redirected, updated, noindexed, or removed depending on the situation. 
'''

# How to find specific file types: PDFs, images, and documents