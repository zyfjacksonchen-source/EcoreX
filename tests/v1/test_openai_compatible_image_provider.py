import asyncio
import base64
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from ecorex.image_orchestrator.cas import ImageContentStore
from ecorex.image_orchestrator.models import (
    ImageJob,
    ImageJobStatus,
    ImageOperation,
    ImageSubmitRequest,
)
from ecorex.image_orchestrator.openai_provider import (
    OpenAICompatibleImageProvider,
)
from ecorex.image_orchestrator.provider import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderState,
    ProviderUncertain,
    ProviderUnavailable,
)


def _image_bytes(
    mode: str,
    size: tuple[int, int],
    color,
    *,
    format: str,
) -> bytes:
    output = BytesIO()
    image = Image.new(mode, size, color)
    image.save(output, format=format)
    image.close()
    return output.getvalue()


def _oriented_jpeg_bytes() -> bytes:
    output = BytesIO()
    image = Image.new("RGB", (1200, 800), (90, 100, 110))
    exif = Image.Exif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    image.close()
    return output.getvalue()


PNG = _image_bytes("RGB", (1024, 1024), (230, 120, 30), format="PNG")
BASE = _image_bytes("RGB", (1024, 1024), (245, 245, 245), format="PNG")
REFERENCE = _image_bytes("RGB", (64, 64), (20, 40, 60), format="JPEG")
_mask_image = Image.new("L", (512, 512), 0)
_mask_image.paste(255, (0, 0, 256, 512))
_mask_output = BytesIO()
_mask_image.save(_mask_output, format="PNG")
_mask_image.close()
MASK = _mask_output.getvalue()
TOKEN = "provider-token-000000000001"


def _job(
    *,
    operation: ImageOperation = ImageOperation.GENERATE,
    inputs: tuple[str, ...] = (),
    instruction: str | None = None,
    client_request_id: str = "openai-image-request-0001",
    width: int = 1024,
    height: int = 1024,
) -> ImageJob:
    now = datetime.now(UTC)
    request = ImageSubmitRequest(
        operation=operation,
        model_id="gpt-image-2",
        client_request_id=client_request_id,
        prompt="Create a restrained office illustration",
        width=width,
        height=height,
        input_sha256=inputs,
        instruction=instruction,
    )
    return ImageJob(
        job_id="01K0IMAGEPROVIDER0000000001",
        account_id="account-001",
        request=request,
        status=ImageJobStatus.LEASED,
        weight=1,
        attempt=1,
        fair_finish=1.0,
        available_at=now,
        deadline=now + timedelta(minutes=10),
        created_at=now,
        updated_at=now,
        provider_idempotency_key="provider-idempotency-0001",
    )


def _provider(
    client: httpx.AsyncClient,
    *,
    input_store: ImageContentStore | None = None,
    max_concurrency: int = 4,
    max_image_bytes: int = 8 * 1024 * 1024,
) -> OpenAICompatibleImageProvider:
    return OpenAICompatibleImageProvider(
        provider_id="ecorex-managed-image",
        origin="https://images.example.invalid",
        allowed_origins=frozenset({"https://images.example.invalid"}),
        allowed_models=frozenset({"gpt-image-2"}),
        bearer_token=lambda: TOKEN,
        input_store=input_store,
        max_image_bytes=max_image_bytes,
        max_connections=4,
        max_concurrency=max_concurrency,
        client=client,
    )


def _completed() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "created": 1,
            "data": [{"b64_json": base64.b64encode(PNG).decode("ascii")}],
            "usage": {"input_tokens": 23, "output_tokens": 41},
        },
        headers={"x-request-id": "upstream-image-request-0001"},
    )


def test_generation_uses_fixed_inline_images_route_and_exact_model() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "gpt-image-2"}]})
            assert request.url.path == "/v1/images/generations"
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            assert request.headers["idempotency-key"] == "provider-idempotency-0001"
            payload = json.loads(request.content)
            assert payload == {
                "model": "gpt-image-2",
                "n": 1,
                "output_format": "png",
                "prompt": "Create a restrained office illustration",
                "quality": "medium",
                "size": "1024x1024",
            }
            assert "response_format" not in payload
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client)
        result = await provider.submit(
            _job(), idempotency_key="provider-idempotency-0001"
        )
        assert result.state is ProviderState.COMPLETED
        assert result.payload == PNG
        assert result.provider_request_id == "upstream-image-request-0001"
        assert result.usage is not None
        assert result.usage.input_units == 23
        assert result.usage.output_units == 41
        await provider.health()
        assert [request.url.path for request in requests] == [
            "/v1/images/generations",
            "/v1/models",
        ]
        await client.aclose()

    asyncio.run(scenario())


def test_structured_retouch_reads_cas_and_sends_base_reference_and_mask(
    tmp_path: Path,
) -> None:
    content = ImageContentStore(tmp_path / "cas", max_bytes=8 * 1024 * 1024)
    base = content.put(BASE, mime_type="image/png")
    reference = content.put(REFERENCE, mime_type="image/jpeg")
    mask = content.put(MASK, mime_type="image/png")
    instruction = json.dumps(
        {
            "schema_version": 1,
            "base": {"sha256": base.sha256},
            "selected": [],
            "references": [{"sha256": reference.sha256}],
            "annotations": [
                {
                    "kind": "rectangle",
                    "instruction": "把杯子的颜色改为品牌橙色",
                }
            ],
            "global_instruction": "其他像素保持不变",
            "mask": {"sha256": mask.sha256},
        },
        ensure_ascii=False,
    )

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/images/edits"
            content_type = request.headers["content-type"]
            assert content_type.startswith("multipart/form-data; boundary=")
            body = request.content
            assert REFERENCE in body
            assert b'name="image[]"' in body
            assert b'name="mask"' in body
            assert "把杯子的颜色改为品牌橙色".encode() in body
            assert "其他像素保持不变".encode() in body
            assert "Preserve every unmasked region".encode() in body
            boundary = content_type.split("boundary=", 1)[1].encode()
            image_start = body.index(b'name="image[]"')
            image_payload_start = body.index(b"\r\n\r\n", image_start) + 4
            image_payload_end = body.index(
                b"\r\n--" + boundary, image_payload_start
            )
            with Image.open(
                BytesIO(body[image_payload_start:image_payload_end])
            ) as prepared_base:
                assert prepared_base.format == "PNG"
                assert prepared_base.size == (1024, 1024)
            marker = b'name="mask"'
            start = body.index(marker)
            payload_start = body.index(b"\r\n\r\n", start) + 4
            payload_end = body.index(b"\r\n--" + boundary, payload_start)
            with Image.open(BytesIO(body[payload_start:payload_end])) as prepared:
                assert prepared.mode == "RGBA"
                assert prepared.size == (1024, 1024)
                alpha = prepared.getchannel("A")
                assert alpha.getpixel((100, 512)) == 0
                assert alpha.getpixel((900, 512)) == 255
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, input_store=content)
        job = _job(
            operation=ImageOperation.RETOUCH,
            inputs=(base.sha256, reference.sha256, mask.sha256),
            instruction=instruction,
        )
        result = await provider.submit(
            job, idempotency_key="provider-idempotency-0001"
        )
        assert result.state is ProviderState.COMPLETED
        assert result.payload == PNG
        await client.aclose()

    asyncio.run(scenario())


def test_uncertain_submit_is_never_resubmitted_by_recover() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client)
        job = _job()
        with pytest.raises(ProviderUncertain):
            await provider.submit(job, idempotency_key="provider-idempotency-0001")
        with pytest.raises(ProviderUncertain, match="reconciliation"):
            await provider.recover(
                job,
                idempotency_key="provider-idempotency-0001",
                provider_request_id=None,
            )
        assert calls == 1
        await client.aclose()

    asyncio.run(scenario())


def test_rate_limit_is_bounded_and_url_only_results_are_rejected() -> None:
    async def scenario() -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "999999999999999"}),
                httpx.Response(
                    200,
                    json={"data": [{"url": "https://untrusted.invalid/image.png"}]},
                ),
            ]
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: next(responses))
        )
        provider = _provider(client)
        job = _job()
        with pytest.raises(ProviderRateLimited) as raised:
            await provider.submit(job, idempotency_key="provider-idempotency-0001")
        assert raised.value.retry_after_seconds == 3600
        with pytest.raises(ProviderRejected, match="inline"):
            await provider.submit(job, idempotency_key="provider-idempotency-0001")
        await client.aclose()

    asyncio.run(scenario())


def test_health_requires_exact_model_and_concurrency_slot_covers_send() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "other-image"}]})
            if calls == 1:
                entered.set()
                await release.wait()
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, max_concurrency=1)
        first = asyncio.create_task(
            provider.submit(_job(), idempotency_key="provider-idempotency-0001")
        )
        await entered.wait()
        second = asyncio.create_task(
            provider.submit(
                _job(client_request_id="openai-image-request-0002"),
                idempotency_key="provider-idempotency-0002",
            )
        )
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.gather(first, second)
        with pytest.raises(ProviderUnavailable, match="model"):
            await provider.health()
        assert calls == 3
        await client.aclose()

    asyncio.run(scenario())


def test_retouch_rejects_aggregate_inputs_before_calling_upstream(
    tmp_path: Path,
) -> None:
    content = ImageContentStore(
        tmp_path / "oversized-cas", max_bytes=8 * 1024 * 1024
    )
    first = content.put(
        b"\x89PNG\r\n\x1a\n" + b"a" * 2_200_000,
        mime_type="image/png",
    )
    second = content.put(
        b"\x89PNG\r\n\x1a\n" + b"b" * 2_200_000,
        mime_type="image/png",
    )
    instruction = json.dumps(
        {
            "schema_version": 1,
            "base": {"sha256": first.sha256},
            "selected": [],
            "references": [{"sha256": second.sha256}],
            "annotations": [],
            "global_instruction": "preserve layout",
            "mask": None,
        }
    )

    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(
            client,
            input_store=content,
            max_image_bytes=4 * 1024 * 1024,
        )
        with pytest.raises(ProviderRejected, match="oversized"):
            await provider.submit(
                _job(
                    operation=ImageOperation.RETOUCH,
                    inputs=(first.sha256, second.sha256),
                    instruction=instruction,
                ),
                idempotency_key="provider-idempotency-0001",
            )
        assert calls == 0
        await client.aclose()

    asyncio.run(scenario())


def test_masked_jpeg_base_is_transcoded_to_matching_png(tmp_path: Path) -> None:
    content = ImageContentStore(tmp_path / "jpeg-cas", max_bytes=8 * 1024 * 1024)
    jpeg = _image_bytes("RGB", (1024, 1024), (100, 110, 120), format="JPEG")
    base = content.put(jpeg, mime_type="image/jpeg")
    mask = content.put(MASK, mime_type="image/png")
    instruction = json.dumps(
        {
            "schema_version": 1,
            "base": {"sha256": base.sha256},
            "selected": [],
            "references": [],
            "annotations": [{"instruction": "change the selected region"}],
            "global_instruction": "preserve the rest",
            "mask": {"sha256": mask.sha256},
        }
    )

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content
            boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()

            def part(name: str) -> bytes:
                start = body.index(f'name="{name}"'.encode())
                payload_start = body.index(b"\r\n\r\n", start) + 4
                payload_end = body.index(b"\r\n--" + boundary, payload_start)
                return body[payload_start:payload_end]

            with Image.open(BytesIO(part("image"))) as prepared_base:
                assert prepared_base.format == "PNG"
                assert prepared_base.size == (1024, 1024)
            with Image.open(BytesIO(part("mask"))) as prepared_mask:
                assert prepared_mask.format == "PNG"
                assert prepared_mask.mode == "RGBA"
                assert prepared_mask.size == (1024, 1024)
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, input_store=content)
        result = await provider.submit(
            _job(
                operation=ImageOperation.RETOUCH,
                inputs=(base.sha256, mask.sha256),
                instruction=instruction,
            ),
            idempotency_key="provider-idempotency-0001",
        )
        assert result.state is ProviderState.COMPLETED
        await client.aclose()

    asyncio.run(scenario())


def test_masked_jpeg_applies_exif_orientation_before_mask_coordinates(
    tmp_path: Path,
) -> None:
    content = ImageContentStore(tmp_path / "oriented-cas", max_bytes=16 * 1024 * 1024)
    base = content.put(_oriented_jpeg_bytes(), mime_type="image/jpeg")
    oriented_mask = _image_bytes("L", (800, 1200), 255, format="PNG")
    mask = content.put(oriented_mask, mime_type="image/png")
    instruction = json.dumps(
        {
            "schema_version": 1,
            "base": {"sha256": base.sha256},
            "selected": [],
            "references": [],
            "annotations": [{"instruction": "replace the selected portrait"}],
            "global_instruction": "preserve orientation",
            "mask": {"sha256": mask.sha256},
        }
    )
    output = _image_bytes("RGB", (800, 1200), (1, 2, 3), format="PNG")

    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            body = request.content
            boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()

            def part(name: str) -> bytes:
                start = body.index(f'name="{name}"'.encode())
                payload_start = body.index(b"\r\n\r\n", start) + 4
                payload_end = body.index(b"\r\n--" + boundary, payload_start)
                return body[payload_start:payload_end]

            with Image.open(BytesIO(part("image"))) as prepared_base:
                assert prepared_base.format == "PNG"
                assert prepared_base.size == (800, 1200)
            with Image.open(BytesIO(part("mask"))) as prepared_mask:
                assert prepared_mask.size == (800, 1200)
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(output).decode()}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(
            client,
            input_store=content,
            max_image_bytes=16 * 1024 * 1024,
        )
        result = await provider.submit(
            _job(
                operation=ImageOperation.RETOUCH,
                inputs=(base.sha256, mask.sha256),
                instruction=instruction,
                width=800,
                height=1200,
            ),
            idempotency_key="provider-idempotency-0001",
        )
        assert result.state is ProviderState.COMPLETED
        with pytest.raises(ProviderRejected, match="surface dimensions"):
            await provider.submit(
                _job(
                    operation=ImageOperation.RETOUCH,
                    inputs=(base.sha256, mask.sha256),
                    instruction=instruction,
                    width=1024,
                    height=1024,
                ),
                idempotency_key="provider-idempotency-0002",
            )
        assert calls == 1
        await client.aclose()

    asyncio.run(scenario())


def test_wrong_output_dimensions_and_transport_secrets_fail_closed() -> None:
    async def wrong_dimensions() -> None:
        small = _image_bytes("RGB", (64, 64), (1, 2, 3), format="PNG")
        response = httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(small).decode()}]},
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: response)
        )
        provider = _provider(client)
        with pytest.raises(ProviderRejected, match="dimensions"):
            await provider.submit(
                _job(), idempotency_key="provider-idempotency-0001"
            )
        await client.aclose()

    async def secret_transport_error() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connection failed with {TOKEN}", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client)
        with pytest.raises(ProviderUncertain) as raised:
            await provider.submit(
                _job(), idempotency_key="provider-idempotency-0001"
            )
        assert TOKEN not in str(raised.value)
        assert TOKEN not in repr(raised.value)
        await client.aclose()

    asyncio.run(wrong_dimensions())
    asyncio.run(secret_transport_error())


def test_retry_after_http_date_is_bounded() -> None:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    assert OpenAICompatibleImageProvider._retry_after(
        "Thu, 16 Jul 2026 00:02:00 GMT",
        now=now,
    ) == 120
    assert OpenAICompatibleImageProvider._retry_after(
        "Thu, 16 Jul 2036 00:00:00 GMT",
        now=now,
    ) == 3600


def test_adversarial_json_depth_fails_closed_without_upstream_call(
    tmp_path: Path,
) -> None:
    content = ImageContentStore(tmp_path / "deep-json-cas", max_bytes=8 * 1024 * 1024)
    base = content.put(BASE, mime_type="image/png")

    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b"[" * 2048 + b"]" * 2048)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, input_store=content)
        with pytest.raises(ProviderRejected, match="instruction"):
            await provider.submit(
                _job(
                    operation=ImageOperation.RETOUCH,
                    inputs=(base.sha256,),
                    instruction="[" * 2048 + "]" * 2048,
                ),
                idempotency_key="provider-idempotency-0001",
            )
        assert calls == 0

        with pytest.raises(ProviderRejected, match="response"):
            await provider.submit(
                _job(), idempotency_key="provider-idempotency-0002"
            )
        assert calls == 1
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (1024, 1000),
        (768, 768),
        (3840, 1248),
        (3840, 2176),
        (4096, 1920),
    ],
)
def test_gpt_image_2_rejects_invalid_flexible_sizes_before_upstream(
    width: int,
    height: int,
) -> None:
    async def scenario() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, max_image_bytes=64 * 1024 * 1024)
        with pytest.raises(ProviderRejected, match="dimensions"):
            await provider.submit(
                _job(width=width, height=height),
                idempotency_key="provider-idempotency-0001",
            )
        assert calls == 0
        await client.aclose()

    asyncio.run(scenario())


def test_gpt_image_2_flexible_size_and_decoded_memory_budget_are_admitted_locally(
) -> None:
    flexible = _image_bytes("RGB", (2048, 1152), (20, 40, 60), format="PNG")
    assert OpenAICompatibleImageProvider._provider_size(
        "gpt-image-2", 3840, 2160
    ) == "3840x2160"

    async def accepted() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["size"] == "2048x1152"
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(flexible).decode()}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, max_image_bytes=16 * 1024 * 1024)
        result = await provider.submit(
            _job(width=2048, height=1152),
            idempotency_key="provider-idempotency-0001",
        )
        assert result.state is ProviderState.COMPLETED
        await client.aclose()

    async def memory_rejected() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _completed()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, max_image_bytes=3 * 1024 * 1024)
        with pytest.raises(ProviderRejected, match="memory envelope"):
            await provider.submit(
                _job(), idempotency_key="provider-idempotency-0001"
            )
        assert calls == 0
        await client.aclose()

    asyncio.run(accepted())
    asyncio.run(memory_rejected())


def test_direct_provider_high_concurrency_stays_inside_its_hard_slot_bound() -> None:
    async def scenario() -> None:
        active = 0
        peak = 0
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return _completed()
            finally:
                active -= 1

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = _provider(client, max_concurrency=4)
        results = await asyncio.gather(
            *(
                provider.submit(
                    _job(client_request_id=f"openai-image-request-{index:04d}"),
                    idempotency_key=f"provider-idempotency-{index:04d}",
                )
                for index in range(32)
            )
        )
        assert calls == 32
        assert peak == 4
        assert all(result.state is ProviderState.COMPLETED for result in results)
        await client.aclose()

    asyncio.run(scenario())
