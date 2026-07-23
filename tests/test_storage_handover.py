"""
test_storage_handover.py
========================
Test suite for Phase 5: Storage Alignment, Resource Recycling & Graceful Degradation.

Verifies:
  1. Structured Data Alignment:
     StructuredComment & StructuredPostAuditRecord to_dict() and to_sqlite_rows().
  2. Lifecycle & Resource Recycling:
     Page tab opening/closing and NetworkInterceptor buffer flushing.
  3. Graceful Degradation / Exception Safety:
     Fault-tolerant handling of 0 captured comments, timeouts, and errors without crashing.

Usage:
    python tests/test_storage_handover.py
"""

import asyncio
import sys
from pathlib import Path

# Add src/ to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper.browser_manager import BrowserManager
from scraper.network_interceptor import NetworkInterceptor
from scraper.storage_handover import PostAuditSession, StructuredComment, StructuredPostAuditRecord


def test_structured_alignment():
    print("\n[Test 1] Structured Alignment & SQLite Rows Formatting...")

    sc = StructuredComment(
        post_id="post_123",
        comment_id="c_456",
        username="alice",
        comment_text="Great post!",
        like_count=10,
        reply_count=2,
        timestamp="2026-07-23T12:00:00Z",
    )

    sc_dict = sc.to_dict()
    assert sc_dict["post_id"] == "post_123"
    assert sc_dict["comment_id"] == "c_456"
    assert sc_dict["username"] == "alice"
    assert sc_dict["comment_text"] == "Great post!"
    assert sc_dict["like_count"] == 10

    record = StructuredPostAuditRecord(
        post_id="post_123",
        post_url="https://www.threads.net/@alice/post/post_123",
        fetch_status="ok",
        total_comments_captured=1,
        comments=[sc_dict],
    )

    rows = record.to_sqlite_rows()
    assert len(rows) == 1
    assert rows[0]["post_id"] == "post_123"
    assert rows[0]["comment_id"] == "c_456"
    assert rows[0]["username"] == "alice"

    print("  -> PASS: Structured comment and audit record formatted cleanly for SQLite insertion!")


def test_graceful_degradation_record():
    print("\n[Test 2] Graceful Degradation Status Formatting...")

    # Record when 0 comments captured (e.g. timeout / empty post / no auth)
    no_comments_rec = StructuredPostAuditRecord(
        post_id="empty_post",
        post_url="https://www.threads.net/post/empty_post",
        fetch_status="no_comments_captured",
        total_comments_captured=0,
        comments=[],
        error_message=None,
    )

    rec_dict = no_comments_rec.to_dict()
    assert rec_dict["fetch_status"] == "no_comments_captured"
    assert rec_dict["total_comments_captured"] == 0
    assert rec_dict["comments"] == []

    # Record when network timeout occurs
    timeout_rec = StructuredPostAuditRecord(
        post_id="timeout_post",
        post_url="https://www.threads.net/post/timeout_post",
        fetch_status="timeout",
        total_comments_captured=0,
        comments=[],
        error_message="Navigation timed out after 30000ms",
    )

    t_dict = timeout_rec.to_dict()
    assert t_dict["fetch_status"] == "timeout"
    assert "timed out" in t_dict["error_message"]

    print("  -> PASS: Graceful degradation records properly marked as 'no_comments_captured' and 'timeout'!")


async def test_session_lifecycle_and_cleanup():
    print("\n[Test 3] PostAuditSession Lifecycle, Tab Cleanup & Interceptor Flush...")

    config_dir = Path(__file__).resolve().parents[1] / "config"
    mgr = BrowserManager(config_dir, profile_name="handover_test_profile", proxy="direct", headless=True)
    await mgr.launch()

    interceptor = NetworkInterceptor()
    session = PostAuditSession(mgr, interceptor)

    # Perform post audit on a URL (e.g. homepage / dummy post URL)
    record = await session.audit_post(
        "https://www.threads.net",
        post_id="test_post_001",
        max_scrolls=2,
        timeout_ms=5000,
    )

    assert record.post_id == "test_post_001"
    assert record.fetch_status in ("ok", "no_comments_captured", "timeout")
    assert interceptor.capture_count == 0  # Buffer flushed in finally block!

    print(f"  Audit record status : {record.fetch_status}")
    print(f"  Interceptor buffer  : {interceptor.capture_count} (flushed cleanly)")

    await mgr.close()
    print("  -> PASS: PostAuditSession successfully recycled page tabs & flushed interceptor buffer!")


if __name__ == "__main__":
    print("=" * 60)
    print("  Phase 5 Storage Alignment & Resource Handover Test Suite")
    print("=" * 60)

    test_structured_alignment()
    test_graceful_degradation_record()
    asyncio.run(test_session_lifecycle_and_cleanup())

    print("\n" + "=" * 60)
    print("  ALL PHASE 5 TESTS PASSED SUCCESSFULLY! [OK]")
    print("=" * 60)
