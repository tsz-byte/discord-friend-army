from app.services import realtime_listener


def test_mapping_failure_opens_circuit_breaker():
    failures = {}
    breaker_until = {}
    mapping_id = 7

    for _ in range(realtime_listener._BREAKER_THRESHOLD):
        realtime_listener._record_mapping_failure(mapping_id, failures, breaker_until)

    assert failures[mapping_id] == realtime_listener._BREAKER_THRESHOLD
    assert mapping_id in breaker_until
