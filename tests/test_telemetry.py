from __future__ import annotations

import unittest

from aiventbus.telemetry import (
    metrics_response,
    record_action_execution,
    record_agent_run,
    record_assignment_created,
    record_assignment_state,
    record_chain_limit,
    record_classifier_fallback,
    record_event_deduped,
    record_event_published,
    record_llm_parse_failure,
    record_llm_request,
    record_llm_tokens,
    record_producer_emit,
    record_routing_result,
    record_system_event,
    set_queue_depth,
)


class TelemetryTests(unittest.TestCase):
    def test_metrics_endpoint_contains_custom_metric_families(self) -> None:
        record_event_published("user.query", "api")
        record_event_deduped("user.query")
        record_chain_limit("depth")
        record_routing_result("matched")
        record_assignment_created("agent_test", "interactive")
        record_llm_request("agent_test", "model-a", 0.123, "success")
        record_llm_parse_failure("agent_test", "model-a")
        record_agent_run("agent_test", "model-a", "completed", 0.456)
        record_action_execution("agent_test", "shell_exec", "completed", 0.789)
        record_assignment_state("agent_test", "claimed")
        record_system_event("system.agent_failure")
        record_llm_tokens("agent_test", "model-a", 42, 17)
        record_producer_emit("clipboard")
        record_classifier_fallback("matched")
        set_queue_depth("interactive", 3)

        response = metrics_response()
        body = response.body.decode("utf-8")

        self.assertIn("aiventbus_events_published_total", body)
        self.assertIn("aiventbus_events_deduped_total", body)
        self.assertIn("aiventbus_events_chain_limit_total", body)
        self.assertIn("aiventbus_routing_decisions_total", body)
        self.assertIn("aiventbus_assignments_created_total", body)
        self.assertIn("aiventbus_llm_requests_total", body)
        self.assertIn("aiventbus_llm_parse_failures_total", body)
        self.assertIn("aiventbus_agent_runs_total", body)
        self.assertIn("aiventbus_action_executions_total", body)
        self.assertIn("aiventbus_assignment_state_transitions_total", body)
        self.assertIn("aiventbus_assignment_queue_depth", body)
        self.assertIn("aiventbus_llm_tokens_total", body)
        self.assertIn("aiventbus_producer_events_emitted_total", body)
        self.assertIn("aiventbus_system_events_total", body)
        self.assertIn("aiventbus_classifier_fallbacks_total", body)
