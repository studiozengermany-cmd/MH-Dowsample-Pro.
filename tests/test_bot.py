from bot import format_counts, format_crawler_error, format_results, format_stats, source_name_from_url
from exceptions import AuthenticationRequiredError, CrawlTimeoutError, NetworkError, PathTraversalError


def test_empty_stats_are_presented_in_vietnamese() -> None:
    message = format_stats({"total": 0, "sites": [], "genres": []})
    assert "Kho âm thanh đang trống" in message
    assert "'total'" not in message


def test_stats_are_formatted_for_people_not_as_raw_dict() -> None:
    message = format_stats(
        {
            "total": 3,
            "sites": [{"site": "web", "total": 3, "loops": 1, "oneshots": 1, "fx": 1}],
            "genres": [{"genre": "DnB", "total": 2}],
        }
    )
    assert "Tổng số mẫu: <b>3</b>" in message
    assert "vòng lặp: 1" in message
    assert "DnB: 2 mẫu" in message
    assert "{'" not in message


def test_pipeline_counts_are_translated() -> None:
    message = format_counts({"passed": 2, "duplicate": 1, "error": 0})
    assert "Đã xử lý: <b>2</b>" in message
    assert "Bị trùng: <b>1</b>" in message


def test_crawl_results_show_saved_filename() -> None:
    message = format_results(
        [{"status": "passed", "output": "C:/audio/kick.wav"}],
        discovered_count=1,
    )
    assert "Đã lưu: <b>1</b>" in message
    assert "kick.wav" in message


def test_crawler_failures_do_not_expose_english_internals() -> None:
    assert "Trang này cần đăng nhập" in format_crawler_error(AuthenticationRequiredError("internal auth"))
    assert "quá lâu" in format_crawler_error(CrawlTimeoutError("internal timeout"))
    assert "Liên kết chưa hợp lệ" in format_crawler_error(PathTraversalError("internal path"))
    assert "kết nối an toàn" in format_crawler_error(NetworkError("internal network"))


def test_source_name_uses_main_domain_not_page_subdomain() -> None:
    assert source_name_from_url("https://sounds.example.com/catalogue") == "example.com"
    assert source_name_from_url("https://cdn.example.co.uk/audio/file.mp3") == "example.co.uk"
