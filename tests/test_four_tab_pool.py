"""Four-tab-max pool policy (2026-11-12, user default).

User policy: always run 4 tabs when enough work exists, never more.
One course at a time: fill all 4 tabs with that course's pending
quizzes (fewer than 4 -> run them all together); if the course has
fewer than 4 pending quizzes, pull the next course's pending quizzes
into the free tabs so 4 tabs stay busy. Maximum concurrency, minimum
time.

Old behaviour: auto_complete_specialization ran up to 4 COURSES
concurrently, each opening up to 4 quiz tabs (up to 16 tabs), while a
course with 2 pending quizzes used only 2 tabs and left the rest idle.
New behaviour: one global queue, course-major order, one shared pool
of at most max_concurrency tabs. Titles are qualified with the course
slug ('Title [slug]', the MCP server's quiz-memory convention) so two
courses with an identically named quiz never share outcome state.

These tests drive the real auto_complete_specialization with fake
browser/crawler/solver objects and prove the pool behaviour by
observation: pages opened, peak concurrency, start order, and titles.
"""
import asyncio
from pathlib import Path

from quiz_pilot import course_orchestrator as co

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_FILES = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules",
    ".github/copilot-instructions.md",
    ".agents/skills/agentic-helper/SKILL.md",
    ".gemini/skills/agentic-helper/SKILL.md",
]


class FakePage:
    def __init__(self, url="https://www.coursera.org/"):
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, main_page):
        self.pages = [main_page]
        self._extra = []

    async def new_page(self):
        p = FakePage()
        self._extra.append(p)
        self.pages.append(p)
        return p

    @property
    def new_pages_opened(self):
        return len(self._extra)


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]


class FakePlaywright:
    def __init__(self, browser):
        self._browser = browser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def chromium(self):
        return self

    async def connect_over_cdp(self, url):
        return self._browser


class FakeAPI:
    @classmethod
    async def from_page(cls, page):
        raise RuntimeError("no direct api in test")


def make_fake_crawler(courses, plans, calls):
    class FakeCrawler:
        def __init__(self, page=None):
            self.page = page

        async def crawl_specialization_courses(self, spec_url):
            return courses

        async def crawl_course_assignments(self, course_url):
            calls[course_url] = calls.get(course_url, 0) + 1
            slug = course_url.rstrip("/").split("/")[-1]
            if calls[course_url] == 1:
                # First crawl: the pending items. Later crawls (verify):
                # empty plan, i.e. the course is complete.
                return {"course_slug": slug,
                        "execution_plan": [dict(it) for it in plans.get(course_url, [])]}
            return {"course_slug": slug, "execution_plan": []}

    return FakeCrawler


class FakeSolver:
    """Records start order and peak concurrency; optionally fails one
    title once so the retry loop is exercised."""

    def __init__(self, fail_once_titles=()):
        self.start_order = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = {}
        self.fail_once = set(fail_once_titles)

    async def __call__(self, page, item, direct_api=None):
        title = item["title"]
        self.start_order.append(title)
        self.calls[title] = self.calls.get(title, 0) + 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        passed = not (title in self.fail_once and self.calls[title] == 1)
        return {"title": title, "url": item["url"],
                "status": "submitted", "passed": passed}


def run_spec(monkeypatch, courses, plans, solver, max_concurrency=4):
    main_page = FakePage("https://www.coursera.org/specializations/test-spec")
    context = FakeContext(main_page)
    browser = FakeBrowser(context)
    calls = {}

    async def fake_dedicated(ctx, url_hint):
        return main_page

    monkeypatch.setattr(co, "async_playwright", lambda: FakePlaywright(browser))
    monkeypatch.setattr(co, "ensure_browser_visible_once", lambda *a, **k: True)
    monkeypatch.setattr(co, "_dedicated_page", fake_dedicated)
    monkeypatch.setattr(co, "SyllabusCrawler", make_fake_crawler(courses, plans, calls))
    monkeypatch.setattr(co, "CourseraDirectAPI", FakeAPI)
    monkeypatch.setattr(co.CourseOrchestrator, "_solve_item_on_page", solver)
    orch = co.CourseOrchestrator(cdp_url="http://127.0.0.1:9222",
                                 max_concurrency=max_concurrency)
    out = asyncio.run(orch.auto_complete_specialization(
        spec_url="https://www.coursera.org/specializations/test-spec"))
    return out, context


def _course(slug, title):
    return {"title": title, "slug": slug,
            "url": f"https://www.coursera.org/learn/{slug}"}


def _item(course_url, title):
    return {"title": title, "type": "quiz",
            "url": f"{course_url}/quiz/{title.replace(' ', '-')}"}


def test_pool_caps_at_four_and_backfills_from_next_course(monkeypatch):
    a = _course("course-a", "Course A")
    b = _course("course-b", "Course B")
    plans = {
        a["url"]: [_item(a["url"], "Quiz 1"), _item(a["url"], "Quiz 2")],
        b["url"]: [_item(b["url"], "Quiz 1"), _item(b["url"], "Quiz 2"),
                  _item(b["url"], "Quiz 3")],
    }
    solver = FakeSolver()
    out, context = run_spec(monkeypatch, [a, b], plans, solver)

    assert out["status"] == "completed"
    assert out["courses_completed"] == 2
    assert out["passes"] == 1
    # 4 tabs total: main page + 3 opened, even though 5 items exist.
    assert context.new_pages_opened == 3
    assert solver.max_in_flight == 4
    # Course-major order, backfilled: A's 2 items first, then B's items
    # fill the free tabs; the 5th item starts only after a slot frees.
    assert solver.start_order == [
        "Quiz 1 [course-a]", "Quiz 2 [course-a]",
        "Quiz 1 [course-b]", "Quiz 2 [course-b]", "Quiz 3 [course-b]",
    ]


def test_fewer_than_four_runs_all_together(monkeypatch):
    a = _course("course-a", "Course A")
    plans = {a["url"]: [_item(a["url"], "Quiz 1"), _item(a["url"], "Quiz 2")]}
    solver = FakeSolver()
    out, context = run_spec(monkeypatch, [a], plans, solver)

    assert out["status"] == "completed"
    assert context.new_pages_opened == 1  # 2 tabs total, no idle extras
    assert solver.max_in_flight == 2
    assert sorted(solver.start_order) == ["Quiz 1 [course-a]", "Quiz 2 [course-a]"]


def test_single_item_uses_one_tab(monkeypatch):
    a = _course("course-a", "Course A")
    plans = {a["url"]: [_item(a["url"], "Quiz 1")]}
    solver = FakeSolver()
    out, context = run_spec(monkeypatch, [a], plans, solver)

    assert out["status"] == "completed"
    assert context.new_pages_opened == 0
    assert solver.start_order == ["Quiz 1 [course-a]"]


def test_failed_quiz_retried_until_pass_in_pool(monkeypatch):
    a = _course("course-a", "Course A")
    plans = {a["url"]: [_item(a["url"], "Quiz 1"), _item(a["url"], "Quiz 2")]}
    solver = FakeSolver(fail_once_titles={"Quiz 1 [course-a]"})
    out, _ = run_spec(monkeypatch, [a], plans, solver)

    assert out["status"] == "completed"
    assert solver.calls["Quiz 1 [course-a]"] == 2  # failed once, retried, passed
    assert solver.calls["Quiz 2 [course-a]"] == 1  # passed first time, never retaken


def test_instruction_files_carry_four_tab_max_rule():
    for rel in INSTRUCTION_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Four-Tab Max Rule" in text, f"{rel}: four-tab rule missing"
        assert "fewer than 4" in text, f"{rel}: no fewer-than-4 backfill wording"
        assert "next course" in text, f"{rel}: no next-course backfill wording"
        assert "EACH child course in its own browser tab simultaneously" not in text, (
            f"{rel}: old one-tab-per-course wording still present")
