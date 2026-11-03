"""Tests for search filter helpers and the SearchFilters model (#122)."""

from datetime import UTC, datetime

from supacrawl.models import SearchFilters
from supacrawl.services.search.filters import domain_operator_query, time_range_to_start_date


class TestSearchFiltersModel:
    def test_empty_when_no_filter_set(self) -> None:
        assert SearchFilters().is_empty() is True

    def test_not_empty_with_a_filter(self) -> None:
        assert SearchFilters(time_range="week").is_empty() is False
        assert SearchFilters(include_domains=["a.com"]).is_empty() is False
        assert SearchFilters(topic="news").is_empty() is False


class TestDomainOperatorQuery:
    def test_no_domains_returns_query_unchanged(self) -> None:
        assert domain_operator_query("python", None, None) == "python"

    def test_single_include_domain(self) -> None:
        assert domain_operator_query("asyncio", ["docs.python.org"], None) == "asyncio site:docs.python.org"

    def test_multiple_include_domains_are_or_grouped(self) -> None:
        out = domain_operator_query("ai", ["a.com", "b.com"], None)
        assert out == "ai (site:a.com OR site:b.com)"

    def test_exclude_domains(self) -> None:
        out = domain_operator_query("news", None, ["spam.com", "ads.com"])
        assert out == "news -site:spam.com -site:ads.com"

    def test_include_and_exclude_combined(self) -> None:
        out = domain_operator_query("topic", ["good.com"], ["bad.com"])
        assert out == "topic site:good.com -site:bad.com"


class TestTimeRangeToStartDate:
    def test_none_returns_none(self) -> None:
        assert time_range_to_start_date(None) is None

    def test_week_is_seven_days_back(self) -> None:
        now = datetime(2026, 6, 14, tzinfo=UTC)
        assert time_range_to_start_date("week", now=now) == "2026-06-07"

    def test_day_is_one_day_back(self) -> None:
        now = datetime(2026, 6, 14, tzinfo=UTC)
        assert time_range_to_start_date("day", now=now) == "2026-06-13"

    def test_unknown_range_returns_none(self) -> None:
        assert time_range_to_start_date("decade") is None
