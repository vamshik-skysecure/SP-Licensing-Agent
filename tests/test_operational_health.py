import asyncio
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.main import readiness
from app.core.dispatch import AzureBlobWebhookDispatcher, DirectWebhookDispatcher
from app.core.licensing.store import AzureBlobWorkflowStore, InMemoryWorkflowStore


class _RateCards:
    async def get(self) -> object:
        return SimpleNamespace(version="health-test-v1", items=[object(), object()])


class _ScenarioEngine:
    def __init__(self) -> None:
        self.calls = 0

    def validate_catalog(self, *_: object, **__: object) -> None:
        self.calls += 1


class _WorkflowStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self._error = error

    async def check_health(self) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error


def _request_state(
    *,
    credentials_valid: bool = True,
    dispatcher_running: bool = True,
    store: object | None = None,
) -> object:
    settings = SimpleNamespace(
        effective_runtime_profile="production",
        default_term_duration="P1Y",
        default_billing_plan="Annual",
        default_customer_segment="Commercial",
        workflow_store_backend="azure_blob",
        message_dispatch_backend="azure_blob",
    )
    state = SimpleNamespace(
        settings=settings,
        whatsapp_client=SimpleNamespace(credentials_valid=credentials_valid),
        webhook_dispatcher=SimpleNamespace(is_running=dispatcher_running),
        workflow_store=store or _WorkflowStore(),
        rate_cards=_RateCards(),
        scenario_engine=_ScenarioEngine(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_checks_all_runtime_dependencies(self) -> None:
        store = _WorkflowStore()
        request = _request_state(store=store)

        response = await readiness(request)  # type: ignore[arg-type]

        self.assertEqual(response["status"], "ready")
        self.assertEqual(response["workflow_store_status"], "ready")
        self.assertEqual(response["dispatcher_status"], "running")
        self.assertEqual(response["whatsapp_credentials"], "valid")
        self.assertEqual(store.calls, 1)
        self.assertEqual(request.app.state.scenario_engine.calls, 1)

    async def test_unreachable_workflow_store_returns_service_unavailable(self) -> None:
        request = _request_state(store=_WorkflowStore(RuntimeError("storage down")))

        with self.assertRaises(HTTPException) as raised:
            await readiness(request)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("storage down", str(raised.exception.detail))

    async def test_stopped_dispatcher_returns_service_unavailable(self) -> None:
        store = _WorkflowStore()
        request = _request_state(dispatcher_running=False, store=store)

        with self.assertRaises(HTTPException) as raised:
            await readiness(request)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(store.calls, 0)

    async def test_invalid_whatsapp_credentials_return_service_unavailable(self) -> None:
        store = _WorkflowStore()
        request = _request_state(credentials_valid=False, store=store)

        with self.assertRaises(HTTPException) as raised:
            await readiness(request)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(store.calls, 0)


class DependencyHealthPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_store_health_is_read_only(self) -> None:
        store = InMemoryWorkflowStore()

        await store.check_health()

        self.assertEqual(store._documents, {})

    async def test_blob_store_health_rechecks_container_access(self) -> None:
        class Container:
            def __init__(self) -> None:
                self.calls = 0

            async def get_container_properties(self) -> None:
                self.calls += 1

        container = Container()
        store = AzureBlobWorkflowStore(
            container_name="workflow",
            container_client=container,
        )

        await store.check_health()

        self.assertEqual(container.calls, 1)

    async def test_direct_dispatcher_reports_started_and_closed_state(self) -> None:
        dispatcher = DirectWebhookDispatcher()
        self.assertFalse(dispatcher.is_running)

        await dispatcher.start(SimpleNamespace(handle=lambda _: None))  # type: ignore[arg-type]
        self.assertTrue(dispatcher.is_running)

        await dispatcher.close()
        self.assertFalse(dispatcher.is_running)

    async def test_blob_dispatcher_reports_live_worker_state(self) -> None:
        class Container:
            async def get_container_properties(self) -> None:
                return None

            async def list_blobs(self, **_: object):
                if False:
                    yield None

        dispatcher = AzureBlobWebhookDispatcher(
            container_name="workflow",
            container_client=Container(),
            poll_seconds=0.1,
        )

        await dispatcher.start(SimpleNamespace(handle=lambda _: None))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        self.assertTrue(dispatcher.is_running)

        await dispatcher.close()
        self.assertFalse(dispatcher.is_running)

    async def test_blob_dispatcher_reports_poll_failure_as_not_ready(self) -> None:
        class Container:
            async def get_container_properties(self) -> None:
                return None

            def list_blobs(self, **_: object):
                raise RuntimeError("synthetic queue listing failure")

        dispatcher = AzureBlobWebhookDispatcher(
            container_name="workflow",
            container_client=Container(),
            poll_seconds=0.1,
        )

        await dispatcher.start(SimpleNamespace(handle=lambda _: None))  # type: ignore[arg-type]
        await asyncio.sleep(0.01)
        self.assertFalse(dispatcher.is_running)

        await dispatcher.close()


if __name__ == "__main__":
    unittest.main()
