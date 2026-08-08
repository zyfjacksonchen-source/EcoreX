"""Immutable declarative Skill contributions for real Turn execution.

This module never imports Python from an extension and never accepts a host
path or command.  A local Skill is static UTF-8 content in the product CAS;
every read is bound to the exact Extension snapshot captured by the Turn and
is fenced against later disable, quarantine, replacement, or CAS tampering.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Any

from ecorex.capabilities import (
    ToolExecutionScope,
    ToolInvocationContext,
    ToolProviderProvenance,
    normalize_reference,
)

from .errors import (
    ExtensionIntegrityError,
    ExtensionNotFound,
    ExtensionProviderRevoked,
    SkillNotExecutable,
    SkillStateChanged,
)
from .local_bundle import LocalBundleFile, LocalSkillBundle, LocalSkillBundleStore
from .local_bundle import SKILL_RUNTIME_FILE, parse_skill_runtime_manifest
from .models import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionExportKind,
    ExtensionHealth,
    ExtensionKind,
    ExtensionManifest,
    ExtensionSource,
    ExtensionStatus,
    canonical_digest,
)
from .service import ExtensionCatalogSnapshot, ExtensionService
from .skill_runner import ControlledSkillRunRequest, ControlledSkillRunResult


CONTRIBUTION_CONTRACT_VERSION = "1.0"
MAX_SKILL_INSTRUCTION_BYTES = 128 * 1024
MAX_SKILL_REFERENCE_BYTES = 64 * 1024
MAX_SKILL_RESPONSE_BYTES = 256 * 1024
MAX_SKILL_ESTIMATED_TOKENS = 32_000
MAX_SKILL_REFERENCES = 32
_TEXT_REFERENCE_SUFFIXES = frozenset(
    {".csv", ".json", ".md", ".tsv", ".txt", ".xml", ".yaml", ".yml"}
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FRONTMATTER = re.compile(r"\A---(?:\r?\n)(.*?)(?:\r?\n)---(?:\r?\n|\Z)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class SkillReferenceContribution:
    reference_id: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SkillContribution:
    extension_id: str
    revision_id: str
    state_revision: int
    export_id: str
    export_digest: str
    artifact_sha256: str
    name: str
    description: str
    tags: tuple[str, ...]
    instruction_sha256: str
    references: tuple[SkillReferenceContribution, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "skill",
            "extension_id": self.extension_id,
            "revision_id": self.revision_id,
            "state_revision": self.state_revision,
            "export_id": self.export_id,
            "export_digest": self.export_digest,
            "artifact_sha256": self.artifact_sha256,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "instruction_sha256": self.instruction_sha256,
            "references": [item.to_dict() for item in self.references],
        }


@dataclass(frozen=True, slots=True)
class MCPContribution:
    extension_id: str
    revision_id: str
    artifact_sha256: str
    transport: str
    protocol_version: str
    export_digest: str
    tool_catalog_digest: str
    tool_ids: tuple[str, ...]
    provider: ToolProviderProvenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "mcp_server",
            "extension_id": self.extension_id,
            "revision_id": self.revision_id,
            "artifact_sha256": self.artifact_sha256,
            "transport": self.transport,
            "protocol_version": self.protocol_version,
            "export_digest": self.export_digest,
            "tool_catalog_digest": self.tool_catalog_digest,
            "tool_ids": list(self.tool_ids),
            "provider": self.provider.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ExtensionContributionSnapshot:
    snapshot_id: str
    extension_snapshot_id: str
    contract_version: str
    skills: tuple[SkillContribution, ...]
    mcp_contributions: tuple[MCPContribution, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "extension_snapshot_id": self.extension_snapshot_id,
            "skills": [item.to_dict() for item in self.skills],
            "mcp_contributions": [item.to_dict() for item in self.mcp_contributions],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, **self.payload()}


@dataclass(frozen=True, slots=True)
class SkillSearchResult:
    discovery_id: str
    name: str
    description: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        # The exact revision identity is the authorization handle.  CAS
        # digests and local resource paths remain deliberately absent.
        return {
            "discovery_id": self.discovery_id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class SkillSearchFact:
    """Minimal immutable projection of one completed Runtime tool fact."""

    tool_call_id: str
    arguments: Mapping[str, Any]
    result: Any
    result_sha256: str


@dataclass(frozen=True, slots=True)
class SkillReadFact:
    """Durable proof that one exact searched Skill revision was read."""

    tool_call_id: str
    arguments: Mapping[str, Any]
    result: Any
    result_sha256: str


class SkillRuntime:
    """Progressive Skill discovery over immutable Extension snapshots."""

    def __init__(
        self,
        service: ExtensionService,
        *,
        snapshot_resolver: Callable[[ToolExecutionScope], str] | None = None,
        turn_intent_resolver: Callable[[ToolExecutionScope], str] | None = None,
        search_fact_resolver: Callable[
            [ToolInvocationContext, str, str, str], SkillSearchFact | None
        ]
        | None = None,
        read_fact_resolver: Callable[
            [ToolInvocationContext, str, str, str], SkillReadFact | None
        ]
        | None = None,
        controlled_runner: Any | None = None,
    ) -> None:
        if service.local_bundle_store is None:
            raise ValueError("Skill Runtime requires the product local Skill CAS")
        self.service = service
        self.store: LocalSkillBundleStore = service.local_bundle_store
        self.snapshot_resolver = snapshot_resolver
        self.turn_intent_resolver = turn_intent_resolver
        self.search_fact_resolver = search_fact_resolver
        self.read_fact_resolver = read_fact_resolver
        self.controlled_runner = (
            controlled_runner if controlled_runner is not None else service.skill_runner
        )
        self.native_runner: Any | None = None
        self._snapshots: dict[str, ExtensionContributionSnapshot] = {}

    def bind_native_runner(self, runner: Any) -> None:
        if self.native_runner is not None and self.native_runner is not runner:
            raise RuntimeError("native Skill runner is already bound")
        self.native_runner = runner

    def contribution_snapshot(
        self,
        extension_snapshot_id: str,
        *,
        mcp_contributions: Sequence[MCPContribution] = (),
    ) -> ExtensionContributionSnapshot:
        catalog = self.service.repository.snapshot_payload(extension_snapshot_id)
        if catalog.get("contract_version") != EXTENSION_CONTRACT_VERSION:
            raise ExtensionIntegrityError("extension catalog contract is incompatible")
        raw_items = catalog.get("items")
        if not isinstance(raw_items, list):
            raise ExtensionIntegrityError("extension catalog items are invalid")
        snapshot = self._build_contribution_snapshot(
            extension_snapshot_id,
            raw_items,
            mcp_contributions=mcp_contributions,
            persist=True,
        )
        self._snapshots[extension_snapshot_id] = snapshot
        return snapshot

    def _build_contribution_snapshot(
        self,
        extension_snapshot_id: str,
        raw_items: Sequence[Any],
        *,
        mcp_contributions: Sequence[MCPContribution],
        persist: bool,
    ) -> ExtensionContributionSnapshot:
        skills: list[SkillContribution] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise ExtensionIntegrityError("extension catalog item is invalid")
            if (
                item.get("kind") != ExtensionKind.SKILL.value
                or item.get("status") != ExtensionStatus.ENABLED.value
                or item.get("health") != ExtensionHealth.HEALTHY.value
            ):
                continue
            revision_id = item.get("active_revision_id")
            if not isinstance(revision_id, str):
                raise ExtensionIntegrityError("active Skill has no exact revision")
            manifest = self.service.repository.manifest(revision_id)
            if manifest.source not in {
                ExtensionSource.LOCAL_BUNDLE,
                ExtensionSource.CORE_BUNDLE,
            }:
                continue
            state_revision = item.get("revision")
            if isinstance(state_revision, bool) or not isinstance(state_revision, int):
                raise ExtensionIntegrityError("active Skill state revision is invalid")
            skills.append(
                self._skill_contribution(manifest, state_revision=state_revision)
            )
        skills.sort(
            key=lambda value: (normalize_reference(value.name), value.extension_id)
        )
        mcp = tuple(
            sorted(
                mcp_contributions,
                key=lambda item: (item.extension_id, item.revision_id),
            )
        )
        payload = {
            "contract_version": CONTRIBUTION_CONTRACT_VERSION,
            "extension_snapshot_id": extension_snapshot_id,
            "skills": [item.to_dict() for item in skills],
            "mcp_contributions": [item.to_dict() for item in mcp],
        }
        digest = canonical_digest(payload)
        snapshot = ExtensionContributionSnapshot(
            snapshot_id="extcontrib_" + digest,
            extension_snapshot_id=extension_snapshot_id,
            contract_version=CONTRIBUTION_CONTRACT_VERSION,
            skills=tuple(skills),
            mcp_contributions=mcp,
        )
        if persist:
            saved_id, saved_digest = self.service.repository.save_snapshot(
                payload,
                prefix="extcontrib",
            )
            if saved_digest != digest or saved_id != snapshot.snapshot_id:
                raise ExtensionIntegrityError(
                    "contribution snapshot persistence diverged"
                )
        return snapshot

    def project_contribution_snapshot(
        self,
        extension_snapshot: ExtensionCatalogSnapshot,
        *,
        mcp_contributions: Sequence[MCPContribution] = (),
    ) -> ExtensionContributionSnapshot:
        """Derive the exact contribution catalog without saving a snapshot."""

        payload = extension_snapshot.to_dict()
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ExtensionIntegrityError("extension catalog items are invalid")
        snapshot = self._build_contribution_snapshot(
            extension_snapshot.snapshot_id,
            raw_items,
            mcp_contributions=mcp_contributions,
            persist=False,
        )
        self._snapshots[extension_snapshot.snapshot_id] = snapshot
        return snapshot

    def search(
        self,
        extension_snapshot_id: str,
        query: str,
        *,
        explicit_names: Sequence[str] = (),
        limit: int = 10,
    ) -> tuple[SkillSearchResult, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("Skill search limit must be between 1 and 50")
        snapshot = self._snapshot(extension_snapshot_id)
        query_tokens = _tokens(query)
        explicit = tuple(
            normalize_reference(value) for value in explicit_names if str(value).strip()
        )
        ranked: list[tuple[int, str, SkillContribution]] = []
        for skill in snapshot.skills:
            self._assert_skill_current(extension_snapshot_id, skill)
            identity = normalize_reference(skill.name)
            extension_identity = normalize_reference(skill.extension_id)
            haystack = " ".join(
                normalize_reference(value)
                for value in (
                    skill.name,
                    skill.description,
                    skill.extension_id,
                    *skill.tags,
                )
            )
            if query_tokens and not all(token in haystack for token in query_tokens):
                continue
            explicit_rank = (
                0 if identity in explicit or extension_identity in explicit else 1
            )
            ranked.append((explicit_rank, identity, skill))
        ranked.sort(key=lambda item: (item[0], item[1], item[2].extension_id))
        return tuple(
            SkillSearchResult(
                _skill_discovery_id(skill),
                skill.name,
                skill.description,
                skill.tags,
            )
            for _, _, skill in ranked[:limit]
        )

    def search_projection(
        self,
        extension_snapshot_id: str,
        query: str,
        *,
        explicit_names: Sequence[str] = (),
        limit: int = 10,
    ) -> dict[str, Any]:
        snapshot = self._snapshot(extension_snapshot_id)
        results = self.search(
            extension_snapshot_id,
            query,
            explicit_names=explicit_names,
            limit=limit,
        )
        return {
            "schema_version": 1,
            "extension_snapshot_id": extension_snapshot_id,
            "extension_contribution_snapshot_id": snapshot.snapshot_id,
            "query": query,
            "skills": [item.to_dict() for item in results],
        }

    def read(
        self,
        extension_snapshot_id: str,
        discovery_id: str,
        *,
        reference_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        snapshot = self._snapshot(extension_snapshot_id)
        skill = self._resolve_skill(snapshot, discovery_id)
        self._assert_skill_current(extension_snapshot_id, skill)
        bundle = self.store.verify(skill.artifact_sha256)
        instructions = _instruction_body(
            self.store.read_verified_file(skill.artifact_sha256, "SKILL.md")
        )
        if (
            hashlib.sha256(instructions.encode("utf-8")).hexdigest()
            != skill.instruction_sha256
        ):
            raise ExtensionIntegrityError(
                "Skill instructions changed after snapshot capture"
            )
        by_id = {item.reference_id: item for item in skill.references}
        requested = tuple(reference_ids)
        if (
            len(requested) != len(set(requested))
            or len(requested) > MAX_SKILL_REFERENCES
        ):
            raise ValueError("Skill reference selection is invalid")
        unknown = set(requested) - set(by_id)
        if unknown:
            raise ExtensionNotFound(
                "Skill reference is absent from the frozen revision"
            )
        resources = _reference_paths(bundle, skill.revision_id)
        selected = requested or ()
        references: list[dict[str, Any]] = []
        response_bytes = len(instructions.encode("utf-8"))
        for reference_id in selected:
            resource = resources.get(reference_id)
            if resource is None:
                raise ExtensionIntegrityError(
                    "Skill reference inventory no longer matches"
                )
            content = self.store.read_verified_file(
                skill.artifact_sha256, resource.path
            )
            if len(content) > MAX_SKILL_REFERENCE_BYTES:
                raise ExtensionIntegrityError(
                    "Skill reference exceeds the read boundary"
                )
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ExtensionIntegrityError(
                    "Skill reference is not strict UTF-8"
                ) from error
            _validate_text(
                text, label="Skill reference", maximum=MAX_SKILL_REFERENCE_BYTES
            )
            expected = by_id[reference_id]
            if hashlib.sha256(content).hexdigest() != expected.sha256:
                raise ExtensionIntegrityError(
                    "Skill reference changed after snapshot capture"
                )
            response_bytes += len(content)
            if response_bytes > MAX_SKILL_RESPONSE_BYTES:
                raise ExtensionIntegrityError(
                    "Skill read response exceeds the product boundary"
                )
            references.append({"reference_id": reference_id, "content": text})
        _validate_token_budget(instructions, *(item["content"] for item in references))
        return {
            "discovery_id": discovery_id,
            "name": skill.name,
            "instructions": instructions,
            "available_references": [
                {
                    "reference_id": item.reference_id,
                    "size_bytes": item.size_bytes,
                }
                for item in skill.references
            ],
            "references": references,
        }

    def workflow_instructions(
        self,
        extension_snapshot_id: str,
        workflow_skill_ids: Sequence[str],
    ) -> dict[str, Any] | None:
        """Read product-linked workflow guidance without granting Skill tools."""

        requested = tuple(dict.fromkeys(workflow_skill_ids))
        if not requested or len(requested) > 8:
            return None
        snapshot = self._snapshot(extension_snapshot_id)
        selected: list[SkillContribution] = []
        for workflow_id in requested:
            logical_name = normalize_reference(str(workflow_id).removeprefix("skill."))
            matches = [
                skill
                for skill in snapshot.skills
                if normalize_reference(skill.name) == logical_name
                or normalize_reference(skill.extension_id)
                == normalize_reference(str(workflow_id))
            ]
            if len(matches) != 1:
                return None
            selected.append(matches[0])
        payloads = []
        for skill in selected:
            self._assert_skill_current(extension_snapshot_id, skill)
            payload = self.read(
                extension_snapshot_id,
                _skill_discovery_id(skill),
            )
            payloads.append((skill, payload))
        instructions = "\n\n".join(
            str(payload["instructions"]) for _skill, payload in payloads
        )
        _validate_token_budget(instructions)
        return {
            "instructions": instructions,
            "instruction_sha256": hashlib.sha256(
                instructions.encode("utf-8")
            ).hexdigest(),
            "skills": [
                {
                    "workflow_skill_id": workflow_id,
                    "extension_id": skill.extension_id,
                    "revision_id": skill.revision_id,
                    "instruction_sha256": skill.instruction_sha256,
                }
                for workflow_id, (skill, _payload) in zip(
                    requested,
                    payloads,
                    strict=True,
                )
            ],
        }

    def explicit_skill_names(
        self,
        extension_snapshot_id: str,
        intent: str,
    ) -> tuple[str, ...]:
        normalized = normalize_reference(intent)
        matches: list[str] = []
        for skill in self._snapshot(extension_snapshot_id).skills:
            if any(
                reference
                and re.search(rf"(?:^|-){re.escape(reference)}(?:-|$)", normalized)
                for reference in {
                    normalize_reference(skill.name),
                    normalize_reference(skill.extension_id),
                }
            ):
                matches.append(skill.extension_id)
        return tuple(matches)

    def handlers(self) -> Mapping[str, Any]:
        return {
            "skill_search": _SkillSearchHandler(self),
            "skill_read": _SkillReadHandler(self),
            "skill_run": _SkillRunHandler(self),
        }

    def _assert_skill_current(
        self,
        extension_snapshot_id: str,
        skill: SkillContribution,
    ) -> None:
        try:
            self.service.assert_export_invocable(
                extension_snapshot_id,
                export_kind=ExtensionExportKind.SKILL,
                export_id=skill.export_id,
                expected_revision_id=skill.revision_id,
                expected_state_revision=skill.state_revision,
            )
        except ExtensionProviderRevoked as error:
            raise SkillStateChanged(
                "Skill was disabled, uninstalled, or changed after discovery"
            ) from error

    def _snapshot(self, extension_snapshot_id: str) -> ExtensionContributionSnapshot:
        snapshot = self._snapshots.get(extension_snapshot_id)
        if snapshot is None:
            snapshot = self.contribution_snapshot(extension_snapshot_id)
        return snapshot

    def _skill_contribution(
        self, manifest: ExtensionManifest, *, state_revision: int
    ) -> SkillContribution:
        bundle = self.store.verify(manifest.artifact_sha256)
        instructions = _instruction_body(
            self.store.read_verified_file(manifest.artifact_sha256, "SKILL.md")
        )
        references = tuple(
            SkillReferenceContribution(reference_id, record.sha256, record.size_bytes)
            for reference_id, record in sorted(
                _reference_paths(bundle, manifest.revision_id).items()
            )
        )
        export = next(
            (
                item
                for item in manifest.exports
                if item.kind is ExtensionExportKind.SKILL
                and item.export_id == manifest.extension_id
            ),
            None,
        )
        if export is None:
            raise ExtensionIntegrityError("Skill manifest lacks its exact export")
        export_digest = canonical_digest(
            {
                "revision_id": manifest.revision_id,
                "artifact_sha256": manifest.artifact_sha256,
                "export": export.to_dict(),
            }
        )
        return SkillContribution(
            extension_id=manifest.extension_id,
            revision_id=manifest.revision_id,
            state_revision=state_revision,
            export_id=export.export_id,
            export_digest=export_digest,
            artifact_sha256=manifest.artifact_sha256,
            name=bundle.metadata.name,
            description=bundle.metadata.description,
            tags=bundle.metadata.tags,
            instruction_sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            references=references,
        )

    @staticmethod
    def _resolve_skill(
        snapshot: ExtensionContributionSnapshot,
        discovery_id: str,
    ) -> SkillContribution:
        parsed = _parse_skill_discovery_id(discovery_id)
        if parsed is None:
            raise ExtensionNotFound("Skill discovery identity is invalid")
        extension_id, revision_id = parsed
        matches = tuple(
            skill
            for skill in snapshot.skills
            if skill.extension_id == extension_id and skill.revision_id == revision_id
        )
        if not matches:
            raise ExtensionNotFound(
                "Skill is absent from the frozen Extension snapshot"
            )
        if len(matches) != 1:
            raise ExtensionIntegrityError("Skill discovery identity is ambiguous")
        return matches[0]

    def _snapshot_for_context(self, context: ToolInvocationContext) -> str:
        scope = context.execution_scope
        if scope is None or self.snapshot_resolver is None:
            raise ExtensionIntegrityError(
                "Skill tools require a durable Turn execution scope"
            )
        if (
            not isinstance(scope.execution_batch_id, str)
            or not scope.execution_batch_id
        ):
            raise ExtensionIntegrityError(
                "Skill tools require a durable execution batch"
            )
        snapshot_id = self.snapshot_resolver(scope)
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("ext_"):
            raise ExtensionIntegrityError("Turn has no valid frozen Extension snapshot")
        return snapshot_id

    def _explicit_names_for_context(
        self,
        context: ToolInvocationContext,
        extension_snapshot_id: str,
    ) -> tuple[str, ...]:
        scope = context.execution_scope
        if scope is None or self.turn_intent_resolver is None:
            return ()
        intent = self.turn_intent_resolver(scope)
        return self.explicit_skill_names(extension_snapshot_id, intent)


class _SkillSearchHandler:
    def __init__(self, runtime: SkillRuntime) -> None:
        self.runtime = runtime

    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        snapshot_id = await asyncio.to_thread(
            self.runtime._snapshot_for_context,
            context,
        )
        explicit_names = await asyncio.to_thread(
            self.runtime._explicit_names_for_context,
            context,
            snapshot_id,
        )
        return await asyncio.to_thread(
            self.runtime.search_projection,
            snapshot_id,
            str(arguments.get("query", "")),
            explicit_names=explicit_names,
            limit=int(arguments.get("limit", 10)),
        )


class _SkillReadHandler:
    def __init__(self, runtime: SkillRuntime) -> None:
        self.runtime = runtime

    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        snapshot_id = await asyncio.to_thread(
            self.runtime._snapshot_for_context,
            context,
        )
        snapshot = await asyncio.to_thread(self.runtime._snapshot, snapshot_id)
        discovery_id = str(arguments["discovery_id"])
        if self.runtime.search_fact_resolver is None:
            raise ExtensionIntegrityError("Skill search authority is unavailable")
        search_fact = await asyncio.to_thread(
            self.runtime.search_fact_resolver,
            context,
            snapshot_id,
            snapshot.snapshot_id,
            discovery_id,
        )
        if search_fact is None:
            raise ExtensionNotFound(
                "Skill was not returned by a completed search in this execution batch"
            )
        if (
            not isinstance(search_fact.result, Mapping)
            or search_fact.result.get("extension_snapshot_id") != snapshot_id
            or search_fact.result.get("extension_contribution_snapshot_id")
            != snapshot.snapshot_id
        ):
            raise SkillStateChanged(
                "Extension generation changed after Skill discovery; search again"
            )
        search_arguments = dict(search_fact.arguments)
        if set(search_arguments) not in ({"query"}, {"query", "limit"}):
            raise ExtensionIntegrityError("Skill search fact has invalid arguments")
        explicit_names = await asyncio.to_thread(
            self.runtime._explicit_names_for_context,
            context,
            snapshot_id,
        )
        expected_search = await asyncio.to_thread(
            self.runtime.search_projection,
            snapshot_id,
            str(search_arguments.get("query", "")),
            explicit_names=explicit_names,
            limit=int(search_arguments.get("limit", 10)),
        )
        if search_fact.result != expected_search:
            raise ExtensionIntegrityError(
                "Skill search result failed Runtime recomputation"
            )
        try:
            canonical_result = json.dumps(
                search_fact.result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ExtensionIntegrityError(
                "Skill search result is not canonical JSON"
            ) from error
        expected_result_sha256 = hashlib.sha256(canonical_result).hexdigest()
        if not isinstance(search_fact.result_sha256, str) or not hmac.compare_digest(
            search_fact.result_sha256,
            expected_result_sha256,
        ):
            raise ExtensionIntegrityError("Skill search result digest is invalid")
        read_result = await asyncio.to_thread(
            self.runtime.read,
            snapshot_id,
            discovery_id,
            reference_ids=tuple(arguments.get("reference_ids") or ()),
        )
        return {
            "schema_version": 1,
            "extension_snapshot_id": snapshot_id,
            "extension_contribution_snapshot_id": snapshot.snapshot_id,
            "search_tool_call_id": search_fact.tool_call_id,
            "search_result_sha256": search_fact.result_sha256,
            **read_result,
        }


class _SkillRunHandler:
    def __init__(self, runtime: SkillRuntime) -> None:
        self.runtime = runtime

    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        snapshot_id = await asyncio.to_thread(
            self.runtime._snapshot_for_context,
            context,
        )
        snapshot = await asyncio.to_thread(self.runtime._snapshot, snapshot_id)
        discovery_id = str(arguments["discovery_id"])
        try:
            skill = self.runtime._resolve_skill(snapshot, discovery_id)
        except ExtensionNotFound as error:
            # A discovery identity can legitimately disappear between tool
            # rounds when its Extension is disabled, uninstalled, or upgraded.
            # Expose the generation fence instead of misreporting corruption.
            if _parse_skill_discovery_id(discovery_id) is not None:
                raise SkillStateChanged(
                    "Skill state changed after read; search and read again"
                ) from error
            raise
        await asyncio.to_thread(
            self.runtime._assert_skill_current,
            snapshot_id,
            skill,
        )
        if self.runtime.read_fact_resolver is None:
            raise ExtensionIntegrityError("Skill read authority is unavailable")
        read_fact = await asyncio.to_thread(
            self.runtime.read_fact_resolver,
            context,
            snapshot_id,
            snapshot.snapshot_id,
            discovery_id,
        )
        if read_fact is None:
            raise ExtensionNotFound(
                "Skill was not read after search in this execution batch"
            )
        if (
            not isinstance(read_fact.result, Mapping)
            or read_fact.result.get("extension_snapshot_id") != snapshot_id
            or read_fact.result.get("extension_contribution_snapshot_id")
            != snapshot.snapshot_id
        ):
            raise SkillStateChanged(
                "Extension generation changed after Skill read; search and read again"
            )
        # Recompute the exact read before any executable boundary is considered.
        recomputed = await asyncio.to_thread(
            self.runtime.read,
            snapshot_id,
            discovery_id,
            reference_ids=tuple(read_fact.arguments.get("reference_ids") or ()),
        )
        if not isinstance(read_fact.result, Mapping) or any(
            read_fact.result.get(key) != value for key, value in recomputed.items()
        ):
            raise ExtensionIntegrityError(
                "Skill read result failed Runtime recomputation"
            )
        try:
            canonical_result = json.dumps(
                read_fact.result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ExtensionIntegrityError(
                "Skill read result is not canonical JSON"
            ) from error
        if not isinstance(read_fact.result_sha256, str) or not hmac.compare_digest(
            read_fact.result_sha256,
            hashlib.sha256(canonical_result).hexdigest(),
        ):
            raise ExtensionIntegrityError("Skill read result digest is invalid")
        bundle = await asyncio.to_thread(
            self.runtime.store.verify, skill.artifact_sha256
        )
        files = {record.path: b"" for record in bundle.files}
        if SKILL_RUNTIME_FILE not in files:
            native_runner = self.runtime.native_runner
            if native_runner is None or not native_runner.supports(skill):
                raise SkillNotExecutable(
                    "Skill has no declared controlled executable entry"
                )

            def state_fence() -> None:
                self.runtime._assert_skill_current(snapshot_id, skill)

            state_fence()
            result = await native_runner.run(
                skill,
                dict(arguments.get("parameters") or {}),
                context,
                state_fence=state_fence,
            )
            state_fence()
            if not isinstance(result, Mapping):
                raise ExtensionIntegrityError("native Skill runner result is invalid")
            return {
                "schema_version": 1,
                "discovery_id": discovery_id,
                "result": dict(result),
            }
        runner = self.runtime.controlled_runner
        if runner is None:
            raise SkillNotExecutable(
                "Skill has no available controlled executable runner"
            )
        files[SKILL_RUNTIME_FILE] = await asyncio.to_thread(
            self.runtime.store.read_verified_file,
            skill.artifact_sha256,
            SKILL_RUNTIME_FILE,
        )
        runtime_manifest = parse_skill_runtime_manifest(files)
        if (
            runtime_manifest is None
            or runtime_manifest.external_commands
            or runtime_manifest.network_domains
            or not runner.supports(runtime_manifest.runtime)
        ):
            raise SkillNotExecutable(
                "Skill has no available controlled executable entry"
            )
        environment: Mapping[str, str] = {}
        if runtime_manifest.environment:
            vault = self.runtime.service.credential_vault
            if vault is None:
                raise SkillNotExecutable("Skill configuration is unavailable")
            try:
                environment = vault.get(
                    self.runtime.service._skill_credential_reference(
                        skill.extension_id, skill.revision_id
                    )
                )
            except (KeyError, RuntimeError) as error:
                raise SkillNotExecutable(
                    "Skill configuration is unavailable"
                ) from error
            if set(environment) != set(runtime_manifest.environment):
                raise SkillNotExecutable("Skill configuration is incomplete")

        def state_fence() -> None:
            self.runtime._assert_skill_current(snapshot_id, skill)

        request = ControlledSkillRunRequest(
            extension_id=skill.extension_id,
            revision_id=skill.revision_id,
            artifact_sha256=skill.artifact_sha256,
            extension_generation=self.runtime.service.repository.generation(),
            runtime=runtime_manifest.runtime,
            entrypoint=runtime_manifest.entrypoint,
            parameters=dict(arguments.get("parameters") or {}),
            environment=dict(environment),
            network_domains=runtime_manifest.network_domains,
            effects=runtime_manifest.effects,
        )
        state_fence()
        result = await runner.run(request, state_fence=state_fence)
        state_fence()
        if not isinstance(result, ControlledSkillRunResult):
            raise ExtensionIntegrityError("controlled Skill runner result is invalid")
        return {
            "schema_version": 1,
            "discovery_id": discovery_id,
            "result": dict(result.result),
        }


def _instruction_body(payload: bytes) -> str:
    if not 1 <= len(payload) <= MAX_SKILL_INSTRUCTION_BYTES:
        raise ExtensionIntegrityError(
            "Skill instructions exceed the execution boundary"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtensionIntegrityError(
            "Skill instructions are not strict UTF-8"
        ) from error
    if text.startswith("\ufeff"):
        raise ExtensionIntegrityError("Skill instructions contain a BOM")
    match = _FRONTMATTER.match(text)
    if match is None:
        raise ExtensionIntegrityError("Skill instructions lack canonical frontmatter")
    body = text[match.end() :]
    _validate_text(
        body, label="Skill instructions", maximum=MAX_SKILL_INSTRUCTION_BYTES
    )
    _validate_token_budget(body)
    return body


def _validate_text(text: str, *, label: str, maximum: int) -> None:
    if len(text.encode("utf-8")) > maximum or _CONTROL.search(text):
        raise ExtensionIntegrityError(f"{label} contains invalid or excessive text")


def _validate_token_budget(*values: str) -> None:
    payload = "\n".join(values)
    lexical = len(re.findall(r"[\w\u3400-\u9fff]+|[^\s]", payload, re.UNICODE))
    byte_bound = (len(payload.encode("utf-8")) + 2) // 3
    if max(lexical, byte_bound) > MAX_SKILL_ESTIMATED_TOKENS:
        raise ExtensionIntegrityError(
            "Skill read exceeds the model context token boundary"
        )


def _reference_paths(
    bundle: LocalSkillBundle,
    revision_id: str,
) -> dict[str, LocalBundleFile]:
    result: dict[str, LocalBundleFile] = {}
    for record in bundle.files:
        folded = record.path.casefold()
        suffix = "." + folded.rsplit(".", 1)[-1] if "." in folded else ""
        if (
            not folded.startswith("references/")
            or suffix not in _TEXT_REFERENCE_SUFFIXES
        ):
            continue
        reference_id = (
            "skillref_"
            + hashlib.sha256(
                f"{revision_id}\0{record.path}\0{record.sha256}".encode("utf-8")
            ).hexdigest()
        )
        result[reference_id] = record
    if len(result) > MAX_SKILL_REFERENCES:
        raise ExtensionIntegrityError("Skill contains too many readable references")
    return result


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.split(r"[^\w\u3400-\u9fff]+", normalize_reference(value))
        if token
    )


def _skill_discovery_id(skill: SkillContribution) -> str:
    return f"skill:{skill.extension_id}@{skill.revision_id}"


def _parse_skill_discovery_id(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith("skill:"):
        return None
    extension_id, separator, revision_id = value[6:].rpartition("@")
    if (
        not separator
        or not extension_id
        or not revision_id
        or len(value) > 512
        or value != f"skill:{extension_id}@{revision_id}"
    ):
        return None
    return extension_id, revision_id


__all__ = [
    "CONTRIBUTION_CONTRACT_VERSION",
    "ExtensionContributionSnapshot",
    "MCPContribution",
    "SkillContribution",
    "SkillReferenceContribution",
    "SkillRuntime",
    "SkillReadFact",
    "SkillSearchFact",
    "SkillSearchResult",
]
