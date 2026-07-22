"""
test_data_processing.py
========================
Test suite for Data Processing & Circuit Breaker (Phase 4).
Verifies:
  1. GraphQL JSON comment extraction ("半路截胡"):
     Directly navigates JSON schema paths to extract text, username, id, likes, etc.
  2. Circuit breaker / Flow control ("熔断机制"):
     - Triggers when comment threshold (e.g. 50 comments) is reached.
     - Triggers when stale limit (e.g. 10 scrolls without new data) is reached.

Usage:
    python tests/test_data_processing.py
"""

import sys
from pathlib import Path

# Add src/ to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper.data_processor import DataProcessor, GraphQLCommentExtractor
from scraper.network_interceptor import CapturedResponse, CaptureCategory, NetworkInterceptor


def test_graphql_comment_extraction():
    print("\n[Test 1] GraphQLCommentExtractor - JSON Path Extraction...")

    # Mock Threads GraphQL response payload
    mock_payload = {
        "data": {
            "containing_thread": {
                "thread_items": [
                    {
                        "post": {
                            "id": "111111",
                            "user": {"username": "tech_lead"},
                            "caption": {"text": "What is your favorite Python framework? &amp; Why?"},
                            "like_count": 42,
                            "taken_at": 1718000000,
                        }
                    }
                ]
            },
            "reply_threads": [
                {
                    "thread_items": [
                        {
                            "post": {
                                "id": "222222",
                                "user": {"username": "dev_guy"},
                                "caption": {"text": "FastAPI all the way! Super fast."},
                                "like_count": 15,
                                "taken_at": 1718000100,
                            }
                        }
                    ],
                    "reply_threads": [
                        {
                            "thread_items": [
                                {
                                    "post": {
                                        "id": "333333",
                                        "user": {"username": "flask_fan"},
                                        "caption": {"text": "I still prefer Flask for simplicity."},
                                        "like_count": 5,
                                        "taken_at": 1718000200,
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

    comments = GraphQLCommentExtractor.extract_from_json(mock_payload)
    print(f"  Extracted {len(comments)} unique comments from JSON payload.")

    assert len(comments) == 3, f"Expected 3 comments, got {len(comments)}"
    assert comments[0]["username"] == "tech_lead"
    assert "favorite Python framework? & Why?" in comments[0]["text"]  # HTML entity unescaped
    assert comments[1]["username"] == "dev_guy"
    assert comments[2]["username"] == "flask_fan"

    print("  -> PASS: All comments extracted directly from JSON paths without CSS class dependency!")


def test_circuit_breaker_threshold():
    print("\n[Test 2] Circuit Breaker - Comment Count Threshold (50 comments)...")

    interceptor = NetworkInterceptor()
    processor = DataProcessor(interceptor, target_comment_threshold=50, stale_limit=10)

    # Simulate 52 comments coming in via intercepted payloads
    mock_payload_1 = {
        "data": {
            "reply_threads": [
                {
                    "thread_items": [
                        {
                            "post": {
                                "id": f"comment_{i}",
                                "user": {"username": f"user_{i}"},
                                "caption": {"text": f"This is comment number {i}"},
                                "like_count": i,
                                "taken_at": 1718000000 + i,
                            }
                        }
                    ]
                }
                for i in range(1, 53)
            ]
        }
    }

    cap = CapturedResponse(
        timestamp=1000.0,
        url="https://www.threads.net/api/graphql/query",
        category=CaptureCategory.COMMENTS,
        matched_pattern="BarcelonaThreadCommentsQuery",
        status=200,
        body=mock_payload_1,
    )
    interceptor._captures.append(cap)

    new_comments = processor.process_new_captures()
    processor.record_scroll_tick(len(new_comments))

    cb_reason = processor.should_circuit_break()
    print(f"  Total extracted comments: {processor.comment_count}")
    print(f"  Circuit breaker signal  : {cb_reason}")

    assert cb_reason is not None, "Circuit breaker should have triggered!"
    assert "target threshold reached" in cb_reason
    assert processor.comment_count == 52

    print("  -> PASS: Circuit breaker triggered successfully upon reaching target threshold!")


def test_circuit_breaker_stale_limit():
    print("\n[Test 3] Circuit Breaker - Stale Scrolls Limit (10 scrolls without new packets)...")

    interceptor = NetworkInterceptor()
    processor = DataProcessor(interceptor, target_comment_threshold=50, stale_limit=10)

    # Simulate 10 consecutive scrolls with NO new comment packets
    for tick in range(1, 11):
        processor.record_scroll_tick(new_comments_found=0)
        cb_reason = processor.should_circuit_break()
        if tick < 10:
            assert cb_reason is None, f"Should not break at scroll #{tick}"
        else:
            assert cb_reason is not None, "Should break at scroll #10"
            assert "stale limit reached (10/10" in cb_reason

    print(f"  Final circuit breaker signal at scroll #10: {cb_reason}")
    print("  -> PASS: Circuit breaker triggered successfully after 10 stale scrolls!")


if __name__ == "__main__":
    print("=" * 60)
    print("  Phase 4 Data Processing & Circuit Breaker Verification")
    print("=" * 60)

    test_graphql_comment_extraction()
    test_circuit_breaker_threshold()
    test_circuit_breaker_stale_limit()

    print("\n" + "=" * 60)
    print("  ALL PHASE 4 TESTS PASSED SUCCESSFULLY! [OK]")
    print("=" * 60)
