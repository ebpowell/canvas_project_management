from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = Any  # type: ignore

try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError:
    Browser = Any  # type: ignore
    BrowserContext = Any  # type: ignore
    Page = Any  # type: ignore
    PlaywrightTimeoutError = Exception  # type: ignore
    async_playwright = None  # type: ignore

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ==============================================================================
# Pydantic Models for Extracted Aspen SIS Records
# ==============================================================================


class AspenCourseMapping(BaseModel):
    """Maps an Aspen course code to a canonical Canvas course."""

    canvas_course_id: int | None = None
    canonical_name: str


class AspenStudentConfig(BaseModel):
    """Per-student Aspen connection and mapping configuration."""

    enabled: bool = True
    portal_type: Literal["family", "student"] = "family"
    student_selector_name: str | None = None
    username: str = ""
    password: str = ""
    course_map: dict[str, AspenCourseMapping] = Field(default_factory=dict)


class AspenRuntimeConfig(BaseModel):
    """Global Aspen Playwright runtime parameters loaded from .env."""

    base_url: str = "https://tn-knox.myfollett.com/aspen/logon.do"
    headless: bool = True
    timeout_ms: int = 30_000
    throttle_seconds: float = 1.5
    debug_scraper: bool = False
    debug_dir: Path = Path("/app/data/debug/aspen")


class AspenAssignmentRecord(BaseModel):
    """Normalized assignment entry extracted from Follett Aspen gradebook."""

    aspen_course_code: str
    aspen_course_name: str
    canonical_course_name: str
    canvas_course_id: int | None = None
    title: str
    category: str = ""
    assigned_date: str | None = None  # YYYY-MM-DD
    due_date: str | None = None  # YYYY-MM-DD
    score_raw: str = ""
    score_numeric: float | None = None
    score_max: float | None = None
    score_pct: float | None = None
    status_flag: str = "Ungraded"  # "Missing" | "Zero" | "Low" | "Graded" | "Ungraded"
    is_missing_or_zero: bool = False


# ==============================================================================
# Score & Date Normalization Helpers
# ==============================================================================


def _parse_aspen_date(raw_text: str) -> str | None:
    """Convert MM/DD/YYYY or MM/DD/YY strings from Aspen tables into YYYY-MM-DD."""
    cleaned = raw_text.strip()
    if not cleaned:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _classify_aspen_score(
    raw_score: str, low_grade_threshold: float = 60.0
) -> tuple[float | None, float | None, float | None, str, bool]:
    """Parse Aspen score cells like '0 / 20', 'Missing', 'M', '85.0%', or '18/20'."""
    text = raw_score.strip()
    upper = text.upper()

    if not text or upper in {"--", "N/A", "UNG", "UNGRADED"}:
        return None, None, None, "Ungraded", False

    if upper in {"M", "MISSING", "INC", "INCOMPLETE"} or "MISSING" in upper:
        return 0.0, None, 0.0, "Missing", True

    # Match fraction format: e.g. "0 / 25" or "18.5/20"
    frac_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if frac_match:
        earned = float(frac_match.group(1))
        possible = float(frac_match.group(2))
        pct = round((earned / possible) * 100.0, 2) if possible > 0 else 0.0
        if earned == 0.0 and possible > 0:
            return earned, possible, 0.0, "Zero", True
        if pct < low_grade_threshold:
            return earned, possible, pct, "Low", True
        return earned, possible, pct, "Graded", False

    # Match standalone percentage or numeric value: e.g. "0", "0.0", "55%"
    num_match = re.search(r"^(\d+(?:\.\d+)?)\s*%?$", text)
    if num_match:
        val = float(num_match.group(1))
        if val == 0.0:
            return 0.0, None, 0.0, "Zero", True
        if "%" in text and val < low_grade_threshold:
            return val, 100.0, val, "Low", True
        return val, None, val if "%" in text else None, "Graded", False

    return None, None, None, text, False


# ==============================================================================
# Playwright Follett Aspen Scraper
# ==============================================================================


class AspenScraper:
    """Headless Playwright client for Follett Aspen Family and Student portals."""

    def __init__(
        self,
        runtime_config: AspenRuntimeConfig,
        low_grade_threshold: float = 60.0,
    ) -> None:
        self.runtime = runtime_config
        self.low_grade_threshold = low_grade_threshold

    async def _dump_debug_html(self, page: Page, label: str) -> None:
        """Persist page HTML when DEBUG_SCRAPER=true for selector troubleshooting."""
        if not self.runtime.debug_scraper:
            return
        self.runtime.debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.runtime.debug_dir / f"{timestamp}_{label}.html"
        out_path.write_text(await page.content(), encoding="utf-8")
        logger.debug("Saved Aspen debug snapshot: %s", out_path)

    async def _login(self, page: Page, student_cfg: AspenStudentConfig) -> None:
        """Authenticate into Follett Aspen logon.do."""
        logger.info("Navigating to Aspen portal: %s", self.runtime.base_url)
        await page.goto(
            self.runtime.base_url,
            wait_until="domcontentloaded",
            timeout=self.runtime.timeout_ms,
        )

        # Standard Follett Aspen login input selectors
        await page.fill('input[name="username"], #username', student_cfg.username)
        await page.fill('input[name="password"], #password', student_cfg.password)

        await asyncio.gather(
            page.wait_for_load_state("networkidle", timeout=self.runtime.timeout_ms),
            page.click('#logonButton, input[type="submit"], button[type="submit"]'),
        )

        # Verify login succeeded by checking for top navigation bar or error banner
        error_el = await page.query_selector(".errorText, #errorMessage, .loginError")
        if error_el:
            err_msg = (await error_el.inner_text()).strip()
            if err_msg:
                raise RuntimeError(f"Aspen authentication failed: {err_msg}")

        logger.info("Aspen authentication succeeded for user '%s'", student_cfg.username)

    async def _select_family_student(
        self, page: Page, student_cfg: AspenStudentConfig
    ) -> None:
        """Select the specific student record when using a multi-child Family portal."""
        if student_cfg.portal_type != "family":
            return

        # Click top-level 'Family' tab if present
        family_tab = page.locator('a:has-text("Family"), td:has-text("Family")').first
        if await family_tab.count() > 0:
            await family_tab.click()
            await page.wait_for_load_state("networkidle", timeout=self.runtime.timeout_ms)

        if not student_cfg.student_selector_name:
            return

        # Locate student row by display name on the Family student list
        student_link = page.locator(
            f'a:has-text("{student_cfg.student_selector_name}")'
        ).first
        if await student_link.count() > 0:
            logger.info(
                "Selecting student '%s' in Family portal",
                student_cfg.student_selector_name,
            )
            await student_link.click()
            await page.wait_for_load_state("networkidle", timeout=self.runtime.timeout_ms)
        else:
            logger.warning(
                "Student selector '%s' not found on Family tab; continuing with default context",
                student_cfg.student_selector_name,
            )

    async def _navigate_to_academics(self, page: Page) -> None:
        """Open the Academics class list view."""
        academics_tab = page.locator(
            'a:has-text("Academics"), td.navTab:has-text("Academics")'
        ).first
        if await academics_tab.count() == 0:
            raise RuntimeError("Could not locate 'Academics' tab in Aspen navigation.")

        await academics_tab.click()
        await page.wait_for_load_state("networkidle", timeout=self.runtime.timeout_ms)
        await self._dump_debug_html(page, "academics_class_list")

    def _parse_class_rows(self, html: str) -> list[dict[str, str]]:
        """Extract course codes, course names, and row identifiers from Academics grid."""
        soup = BeautifulSoup(html, "html.parser")
        courses: list[dict[str, str]] = []

        # Aspen data grids typically use id="dataGrid" or class="listGrid"
        grid = soup.find("table", {"id": "dataGrid"}) or soup.find(
            "table", class_=re.compile(r"listGrid|dataGrid", re.I)
        )
        if not grid:
            logger.warning("Aspen Academics class grid table not found in DOM.")
            return courses

        rows = grid.find_all("tr")
        if len(rows) < 2:
            return courses

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        code_idx = next((i for i, h in enumerate(headers) if "course" in h or "number" in h), 0)
        desc_idx = next(
            (i for i, h in enumerate(headers) if "description" in h or "class" in h or "name" in h),
            1 if len(headers) > 1 else 0,
        )

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(code_idx, desc_idx):
                continue
            link = row.find("a", href=True)
            if not link:
                continue

            course_code = cells[code_idx].get_text(strip=True)
            course_name = cells[desc_idx].get_text(strip=True)
            courses.append(
                {
                    "course_code": course_code,
                    "course_name": course_name,
                    "link_text": link.get_text(strip=True),
                    "href": link["href"],
                }
            )

        return courses

    def _parse_assignments_grid(
        self,
        html: str,
        course_code: str,
        course_name: str,
        student_cfg: AspenStudentConfig,
    ) -> list[AspenAssignmentRecord]:
        """Parse individual assignment rows from a class's Assignments sub-tab."""
        soup = BeautifulSoup(html, "html.parser")
        records: list[AspenAssignmentRecord] = []

        mapping = student_cfg.course_map.get(course_code)
        canonical_name = mapping.canonical_name if mapping else course_name
        canvas_id = mapping.canvas_course_id if mapping else None

        grid = soup.find("table", {"id": "dataGrid"}) or soup.find(
            "table", class_=re.compile(r"listGrid|dataGrid", re.I)
        )
        if not grid:
            return records

        rows = grid.find_all("tr")
        if len(rows) < 2:
            return records

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

        def _find_col(keywords: tuple[str, ...], default: int | None = None) -> int | None:
            for idx, header in enumerate(headers):
                if any(kw in header for kw in keywords):
                    return idx
            return default

        title_idx = _find_col(("title", "assignment", "name"), default=0)
        cat_idx = _find_col(("category", "type"))
        assigned_idx = _find_col(("assigned", "date assigned"))
        due_idx = _find_col(("due", "date due"))
        score_idx = _find_col(("score", "grade", "points"))

        for row in rows[1:]:
            cells = row.find_all("td")
            if title_idx is None or len(cells) <= title_idx:
                continue

            title = cells[title_idx].get_text(strip=True)
            if not title or title.lower() in {"no records found", "no matching records"}:
                continue

            category = (
                cells[cat_idx].get_text(strip=True)
                if cat_idx is not None and len(cells) > cat_idx
                else ""
            )
            assigned_raw = (
                cells[assigned_idx].get_text(strip=True)
                if assigned_idx is not None and len(cells) > assigned_idx
                else ""
            )
            due_raw = (
                cells[due_idx].get_text(strip=True)
                if due_idx is not None and len(cells) > due_idx
                else ""
            )
            score_raw = (
                cells[score_idx].get_text(strip=True)
                if score_idx is not None and len(cells) > score_idx
                else ""
            )

            earned, possible, pct, status_flag, is_flagged = _classify_aspen_score(
                score_raw, low_grade_threshold=self.low_grade_threshold
            )

            records.append(
                AspenAssignmentRecord(
                    aspen_course_code=course_code,
                    aspen_course_name=course_name,
                    canonical_course_name=canonical_name,
                    canvas_course_id=canvas_id,
                    title=title,
                    category=category,
                    assigned_date=_parse_aspen_date(assigned_raw),
                    due_date=_parse_aspen_date(due_raw),
                    score_raw=score_raw,
                    score_numeric=earned,
                    score_max=possible,
                    score_pct=pct,
                    status_flag=status_flag,
                    is_missing_or_zero=is_flagged,
                )
            )

        return records

    async def scrape_student(
        self, student_cfg: AspenStudentConfig
    ) -> list[AspenAssignmentRecord]:
        """Execute full login, course traversal, and assignment extraction for one student."""
        if not student_cfg.enabled:
            logger.info("Aspen scraping disabled for student configuration; skipping.")
            return []

        all_records: list[AspenAssignmentRecord] = []

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(
                headless=self.runtime.headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
            )
            page: Page = await context.new_page()

            try:
                await self._login(page, student_cfg)
                await self._select_family_student(page, student_cfg)
                await self._navigate_to_academics(page)

                courses = self._parse_class_rows(await page.content())
                logger.info("Discovered %d Aspen courses to inspect", len(courses))

                for course in courses:
                    code = course["course_code"]
                    name = course["course_name"]
                    logger.debug("Opening Aspen course %s (%s)", code, name)

                    try:
                        # Open class details from Academics list
                        class_link = page.locator(f'a:has-text("{course["link_text"]}")').first
                        await class_link.click()
                        await page.wait_for_load_state(
                            "networkidle", timeout=self.runtime.timeout_ms
                        )

                        # Click 'Assignments' side-tab inside the class view
                        assignments_side_tab = page.locator(
                            'a:has-text("Assignments"), td:has-text("Assignments")'
                        ).first
                        if await assignments_side_tab.count() > 0:
                            await assignments_side_tab.click()
                            await page.wait_for_load_state(
                                "networkidle", timeout=self.runtime.timeout_ms
                            )

                        safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", code)
                        await self._dump_debug_html(page, f"assignments_{safe_code}")

                        course_records = self._parse_assignments_grid(
                            html=await page.content(),
                            course_code=code,
                            course_name=name,
                            student_cfg=student_cfg,
                        )
                        all_records.extend(course_records)
                        logger.info(
                            "Extracted %d Aspen assignments for %s (%s)",
                            len(course_records),
                            code,
                            name,
                        )

                        # Throttle requests and return to Academics class list
                        await asyncio.sleep(self.runtime.throttle_seconds)
                        await self._navigate_to_academics(page)

                    except PlaywrightTimeoutError:
                        logger.error(
                            "Timeout scraping Aspen course %s (%s); attempting recovery",
                            code,
                            name,
                        )
                        await self._navigate_to_academics(page)

            finally:
                await context.close()
                await browser.close()

        return all_records


def scrape_aspen_sync(
    runtime_config: AspenRuntimeConfig,
    student_cfg: AspenStudentConfig,
    low_grade_threshold: float = 60.0,
) -> list[AspenAssignmentRecord]:
    """Synchronous entrypoint wrapper for calling AspenScraper from main.py."""
    scraper = AspenScraper(
        runtime_config=runtime_config,
        low_grade_threshold=low_grade_threshold,
    )
    return asyncio.run(scraper.scrape_student(student_cfg))