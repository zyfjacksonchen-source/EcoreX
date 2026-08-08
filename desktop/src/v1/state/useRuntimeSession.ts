import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";

import type {
  ArtifactProjection,
  BootstrapResponse,
  CapabilityMentionCatalog,
  CapabilityMentionProjection,
  ConversationUsageProjection,
  EventEnvelope,
  InteractionResponse,
  InputAttachmentProjection,
  ItemProjection,
  LiveReplayResponse,
  MemorySnapshot,
  ModelDescriptor,
  MockReplayResponse,
  OutputLocationAlias,
  OutputLocationOption,
  OutputMaterialization,
  OutputPreference,
  ProjectProjection,
  RetouchAnnotation,
  RetouchViewState,
  RetouchWorkspaceProjection,
  ShareSnapshotProjection,
  SystemHealthSample,
  ThreadProjection,
  ThreadProjectionResponse,
  TurnProjection,
  UpdateSnapshot,
} from "../api/contracts.ts";
import { tryValidateArtifactProjection } from "../api/runtimeContract.ts";
import {
  createClientRequestId,
  eventClientMessageIds,
  EventCursorResetRequired,
  projectionClientMessageIds,
  RuntimeApiError,
  RuntimeClient,
} from "../api/runtimeClient.ts";
import type {
  ClientOperation,
  ClientOperationDisposition,
  TurnModelSelection,
} from "../api/runtimeClient.ts";
import { mergeArtifactProjections } from "./artifactActions.ts";
import { ArtifactPreviewCache } from "./artifactPreviewCache.ts";
import {
  emptyImageBatchFacts,
  loadImageBatchFactHistory,
  mergeImageBatchFacts,
  reduceImageBatchFacts,
  selectFailedImageBatchSlots,
} from "./imageBatchFacts.ts";
import {
  initialRuntimeViewState,
  runtimeReducer,
  selectActiveTurn,
  selectIsThinking,
  selectInteractions,
  selectItems,
  selectPendingInteractions,
  selectVisibleReasoning,
  selectTurns,
} from "./runtimeReducer.ts";
import { useConnectorSession } from "./useConnectorSession.ts";
import { useExtensionSession } from "./useExtensionSession.ts";
import { userFacingError } from "./userLanguage.ts";

type ClientOperationSupportModule = typeof import("../deferred/clientOperationOutbox.ts");
type ClientOperationOutboxInstance = InstanceType<
  ClientOperationSupportModule["ClientOperationOutbox"]
>;

export type SendDisposition = Exclude<ClientOperationDisposition, "create">;
export type LoadState = "loading" | "ready" | "error";

interface PendingPermissionMutation {
  profile: "default" | "full_access";
  expectedRevision: number;
  clientRequestId: string;
}

interface PendingUpdateActivation {
  transactionId: string;
  clientRequestId: string;
}

interface PendingThreadMutation {
  fingerprint: string;
  clientRequestId: string;
}

interface PendingMemoryMutation {
  operation: "reset" | "undo";
  target: string;
  clientRequestId: string;
}

interface PendingOutputPreferenceMutation {
  locationAlias: OutputLocationAlias;
  expectedRevision: number;
  clientRequestId: string;
}

export function preferredModel(
  models: readonly ModelDescriptor[],
): string {
  return models.find((model) => model.is_default)?.model_id ?? models[0]?.model_id ?? "";
}

export function reconcileModelSelection(
  current: string,
  models: readonly ModelDescriptor[],
): string {
  return models.some((model) => model.model_id === current)
    ? current
    : preferredModel(models);
}

export function modelSelectionForMutation(
  chatModel: string,
  imageModel: string,
  activeTurn: TurnProjection | null,
  disposition: SendDisposition,
): TurnModelSelection {
  if (activeTurn && disposition === "steer") {
    return {
      agentModelId: activeTurn.agent_model_id,
      imageModelId: activeTurn.image_model_id,
    };
  }
  return {
    agentModelId: chatModel,
    imageModelId: imageModel || null,
  };
}

function errorMessage(error: unknown): string {
  return userFacingError(error);
}

function itemArtifact(item: ItemProjection): ArtifactProjection | null {
  if (item.kind !== "artifact") return null;
  const raw = item.content.artifact ?? item.content;
  return tryValidateArtifactProjection(raw);
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function isFrameBatchableEvent(event: EventEnvelope): boolean {
  return event.event_type === "item.delta" || event.event_type === "reasoning.delta";
}

export function useRuntimeSession(providedClient?: RuntimeClient) {
  const fallbackClient = useMemo(() => new RuntimeClient(), []);
  const client = providedClient ?? fallbackClient;
  const operationSupportPromise = useRef<Promise<ClientOperationSupportModule> | null>(null);
  const operationOutboxRef = useRef<ClientOperationOutboxInstance | null>(null);
  const loadOperationSupport = useCallback(async () => {
    operationSupportPromise.current ??= import("../deferred/clientOperationOutbox.ts");
    const module = await operationSupportPromise.current;
    operationOutboxRef.current ??= new module.ClientOperationOutbox();
    return { module, outbox: operationOutboxRef.current };
  }, []);
  const [state, dispatch] = useReducer(runtimeReducer, initialRuntimeViewState);
  const stateRef = useRef(state);
  const watermarkRef = useRef(0);
  const serverClockOffsetMs = useRef(0);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [transportError, setTransportError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const recoveringOperationsRef = useRef(false);
  const recoverPendingOperationsRef = useRef<() => Promise<void>>(async () => undefined);
  const pendingSendOperation = useRef<ClientOperation | null>(null);
  const [permissionUpdating, setPermissionUpdating] = useState(false);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  const permissionUpdatingRef = useRef(false);
  const pendingPermissionMutation = useRef<PendingPermissionMutation | null>(null);
  const [updateBusy, setUpdateBusy] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const pendingUpdateActivation = useRef<PendingUpdateActivation | null>(null);
  const [sessionBusy, setSessionBusy] = useState(false);
  const sessionBusyRef = useRef(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [memory, setMemory] = useState<MemorySnapshot | null>(null);
  const [memoryLoadState, setMemoryLoadState] = useState<LoadState>("loading");
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const pendingMemoryMutation = useRef<PendingMemoryMutation | null>(null);
  const [outputLocations, setOutputLocations] = useState<OutputLocationOption[]>([]);
  const [outputPreference, setOutputPreference] = useState<OutputPreference | null>(null);
  const [outputLoadState, setOutputLoadState] = useState<LoadState>("loading");
  const [outputBusy, setOutputBusy] = useState(false);
  const [outputError, setOutputError] = useState<string | null>(null);
  const pendingOutputPreference = useRef<PendingOutputPreferenceMutation | null>(null);
  const pendingArtifactMaterializations = useRef(new Map<string, string>());
  const [systemHealth, setSystemHealth] = useState<SystemHealthSample | null>(null);
  const [systemHealthLoadState, setSystemHealthLoadState] = useState<LoadState>("loading");
  const [systemHealthError, setSystemHealthError] = useState<string | null>(null);
  const [conversationUsage, setConversationUsage] = useState<ConversationUsageProjection | null>(null);
  const [accountUsage, setAccountUsage] = useState<ConversationUsageProjection | null>(null);
  const [capabilityMentions, setCapabilityMentions] = useState<CapabilityMentionProjection[]>([]);
  const [capabilityMentionState, setCapabilityMentionState] = useState<LoadState>("loading");
  const [chatModel, setChatModel] = useState("");
  const [imageModel, setImageModel] = useState("");
  const [threads, setThreads] = useState<ThreadProjection[]>([]);
  const [projects, setProjects] = useState<ProjectProjection[]>([]);
  const [projectCatalogState, setProjectCatalogState] = useState<LoadState>("loading");
  const [projectCatalogError, setProjectCatalogError] = useState<string | null>(null);
  const [projectPickerBusy, setProjectPickerBusy] = useState(false);
  const [newConversationProject, setNewConversationProject] = useState<ProjectProjection | null>(null);
  const [threadCatalogState, setThreadCatalogState] = useState<LoadState>("loading");
  const [threadCatalogError, setThreadCatalogError] = useState<string | null>(null);
  const [switchingThreadId, setSwitchingThreadId] = useState<string | null>(null);
  const [threadMutationKey, setThreadMutationKey] = useState<string | null>(null);
  const threadCatalogGeneration = useRef(0);
  const threadSwitchGeneration = useRef(0);
  const eventStreamGeneration = useRef(0);
  const threadSwitchAbort = useRef<AbortController | null>(null);
  const threadSwitchInProgress = useRef(false);
  const selectedThreadId = useRef<string | null>(null);
  const pendingNewThreadMetadata = useRef<Record<string, string>>({
    conversation_kind: "general",
  });
  const pendingThreadMutations = useRef(new Map<string, PendingThreadMutation>());
  const pendingLiveReplayRequests = useRef(new Map<string, string>());
  const [artifacts, setArtifacts] = useState<ArtifactProjection[]>([]);
  const [imageBatchFacts, setImageBatchFacts] = useState(emptyImageBatchFacts);
  const [artifactPreviewUrls, setArtifactPreviewUrls] = useState<Record<string, string>>({});
  const previewCache = useMemo(() => new ArtifactPreviewCache({
    fetchPreview: (artifactId, signal) => client.artifactBlob(
      artifactId,
      "thumbnail",
      signal,
    ),
    onChange: setArtifactPreviewUrls,
  }), [client]);
  const effectiveArtifacts = useMemo(() => mergeArtifactProjections(
    selectItems(state)
      .map(itemArtifact)
      .filter((item): item is ArtifactProjection => item !== null),
    artifacts,
  ), [artifacts, state]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const acknowledgeClientMessages = useCallback((clientMessageIds: Iterable<string>) => {
    const operationOutbox = operationOutboxRef.current;
    if (!operationOutbox) return [];
    try {
      const removed = operationOutbox.acknowledge(clientMessageIds);
      if (
        pendingSendOperation.current
        && removed.includes(pendingSendOperation.current.operation_id)
      ) {
        pendingSendOperation.current = null;
      }
      return removed;
    } catch (error) {
      setTransportError(errorMessage(error));
      return [];
    }
  }, []);

  const applyProjection = useCallback((projection: ThreadProjectionResponse) => {
    acknowledgeClientMessages(projectionClientMessageIds(projection));
    const action = { type: "projection.received" as const, projection };
    stateRef.current = runtimeReducer(stateRef.current, action);
    watermarkRef.current = stateRef.current.watermark;
    dispatch(action);
  }, [acknowledgeClientMessages]);

  const applyEventBatch = useCallback((events: readonly EventEnvelope[]) => {
    if (events.length === 0) return;
    acknowledgeClientMessages(eventClientMessageIds(events));
    setImageBatchFacts((current) => reduceImageBatchFacts(current, events));
    const action = { type: "events.received" as const, events };
    stateRef.current = runtimeReducer(stateRef.current, action);
    watermarkRef.current = stateRef.current.watermark;
    dispatch(action);
  }, [acknowledgeClientMessages]);

  const clearThreadProjection = useCallback(() => {
    stateRef.current = {
      ...initialRuntimeViewState,
      bootstrap: stateRef.current.bootstrap,
    };
    watermarkRef.current = 0;
    setImageBatchFacts(emptyImageBatchFacts());
    setConversationUsage(null);
    dispatch({ type: "thread.cleared" });
  }, []);

  const clearArtifactView = useCallback(() => {
    previewCache.clear();
    setArtifacts([]);
  }, [previewCache]);

  const applyBootstrap = useCallback((bootstrap: BootstrapResponse) => {
    serverClockOffsetMs.current = Date.parse(bootstrap.server_time) - Date.now();
    client.acceptBootstrap(bootstrap);
    const action = { type: "bootstrap.received" as const, bootstrap };
    stateRef.current = runtimeReducer(stateRef.current, action);
    dispatch(action);
    setChatModel((current) => reconcileModelSelection(current, bootstrap.models.chat));
    setImageModel((current) => reconcileModelSelection(current, bootstrap.models.image));
  }, [client]);

  const loadBootstrap = useCallback(async (signal?: AbortSignal) => {
    setLoadState("loading");
    setTransportError(null);
    try {
      const bootstrap = await client.bootstrap(signal);
      applyBootstrap(bootstrap);
      setLoadState("ready");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setLoadState("error");
      setTransportError(errorMessage(error));
    }
  }, [applyBootstrap, client]);

  const refreshBootstrapQuietly = useCallback(async (signal?: AbortSignal) => {
    const bootstrap = await client.bootstrap(signal);
    applyBootstrap(bootstrap);
    return bootstrap;
  }, [applyBootstrap, client]);

  const refreshCapabilityMentions = useCallback(async (signal?: AbortSignal) => {
    setCapabilityMentionState("loading");
    try {
      const catalog: CapabilityMentionCatalog = await client.capabilityMentions(signal);
      if (signal?.aborted) return false;
      setCapabilityMentions(catalog.items);
      setCapabilityMentionState("ready");
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      setCapabilityMentionState("error");
      return false;
    }
  }, [client]);

  const refreshMemory = useCallback(async (signal?: AbortSignal) => {
    setMemoryLoadState("loading");
    setMemoryError(null);
    try {
      const snapshot = await client.memory(signal);
      if (signal?.aborted) return false;
      setMemory(snapshot);
      setMemoryLoadState("ready");
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      setMemoryLoadState("error");
      setMemoryError(errorMessage(error));
      return false;
    }
  }, [client]);

  const refreshSystemHealth = useCallback(async (signal?: AbortSignal) => {
    setSystemHealthLoadState((current) => current === "ready" ? current : "loading");
    try {
      const sample = await client.systemHealth({ signal });
      if (signal?.aborted) return false;
      setSystemHealth(sample);
      setSystemHealthLoadState("ready");
      setSystemHealthError(null);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      setSystemHealthLoadState("error");
      setSystemHealthError(errorMessage(error));
      return false;
    }
  }, [client]);

  const refreshOutput = useCallback(async (signal?: AbortSignal) => {
    setOutputLoadState((current) => current === "ready" ? current : "loading");
    try {
      const [catalog, preference] = await Promise.all([
        client.outputLocations(signal),
        client.outputPreference(signal),
      ]);
      if (signal?.aborted) return false;
      setOutputLocations(catalog.items);
      setOutputPreference(preference);
      setOutputLoadState("ready");
      setOutputError(null);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      setOutputLoadState("error");
      setOutputError(errorMessage(error));
      return false;
    }
  }, [client]);

  const loadSystemTechnicalHealth = useCallback(async () => {
    try {
      return await client.systemHealth({ technical: true });
    } catch (error) {
      throw new Error(errorMessage(error));
    }
  }, [client]);

  const refreshThreads = useCallback(async (signal?: AbortSignal) => {
    const generation = ++threadCatalogGeneration.current;
    setThreadCatalogState((current) => current === "ready" ? current : "loading");
    setThreadCatalogError(null);
    try {
      const items: ThreadProjection[] = [];
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      do {
        const response = await client.listThreads("all", 200, cursor, signal);
        items.push(...response.items);
        if (!response.next_cursor) {
          cursor = undefined;
          break;
        }
        if (seenCursors.has(response.next_cursor)) {
          throw new RuntimeApiError("Runtime 返回了重复的任务目录游标。", 502);
        }
        seenCursors.add(response.next_cursor);
        cursor = response.next_cursor;
      } while (!signal?.aborted);
      if (signal?.aborted || generation !== threadCatalogGeneration.current) return false;
      setThreads(items);
      setThreadCatalogState("ready");
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      if (generation !== threadCatalogGeneration.current) return false;
      setThreadCatalogState("error");
      setThreadCatalogError(errorMessage(error));
      return false;
    }
  }, [client]);

  const refreshProjects = useCallback(async (signal?: AbortSignal) => {
    setProjectCatalogState((current) => current === "ready" ? current : "loading");
    try {
      const response = await client.listProjects(signal);
      if (signal?.aborted) return false;
      setProjects(response.projects);
      setProjectCatalogState("ready");
      setProjectCatalogError(null);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      setProjectCatalogState("error");
      setProjectCatalogError(errorMessage(error));
      return false;
    }
  }, [client]);

  const pickProject = useCallback(async (): Promise<ProjectProjection | null> => {
    if (projectPickerBusy) return null;
    setProjectPickerBusy(true);
    setProjectCatalogError(null);
    try {
      const project = await client.pickProject(createClientRequestId("project_pick"));
      await refreshProjects();
      return project;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) return null;
      setProjectCatalogError(errorMessage(error));
      return null;
    } finally {
      setProjectPickerBusy(false);
    }
  }, [client, projectPickerBusy, refreshProjects]);

  const applyUpdateSnapshot = useCallback((update: UpdateSnapshot) => {
    const bootstrap = stateRef.current.bootstrap;
    if (!bootstrap) return;
    dispatch({
      type: "bootstrap.received",
      bootstrap: { ...bootstrap, update },
    });
  }, []);

  const bootstrapped = state.bootstrap !== null;
  const connectorSession = useConnectorSession({
    client,
    bootstrapped,
    refreshBootstrap: refreshBootstrapQuietly,
    formatError: errorMessage,
  });
  const extensionSession = useExtensionSession({
    client,
    bootstrap: state.bootstrap,
    formatError: errorMessage,
  });

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const response = await client.updateStatus(controller.signal);
        if (!controller.signal.aborted) applyUpdateSnapshot(response.update);
      } catch {
        // Runtime/SSE connectivity owns the global offline state. Update polling
        // stays quiet so a scheduled process restart does not surface a false
        // product error immediately before the page reloads.
      }
      try {
        await refreshBootstrapQuietly(controller.signal);
      } catch {
        // Keep the last validated model catalog during a transient refresh
        // failure; the Runtime still fences each new turn against its snapshot.
      }
    };
    const timer = window.setInterval(() => void poll(), 5_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [applyUpdateSnapshot, bootstrapped, client, refreshBootstrapQuietly]);

  useEffect(() => {
    const controller = new AbortController();
    void loadBootstrap(controller.signal);
    return () => controller.abort();
  }, [loadBootstrap]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshThreads(controller.signal);
    return () => controller.abort();
  }, [bootstrapped, refreshThreads]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    const refresh = () => {
      void refreshCapabilityMentions(controller.signal);
      void refreshBootstrapQuietly(controller.signal).catch(() => undefined);
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      controller.abort();
      window.removeEventListener("focus", refresh);
    };
  }, [bootstrapped, refreshBootstrapQuietly, refreshCapabilityMentions]);

  useEffect(() => {
    if (!bootstrapped || !threads.some((thread) => thread.active_turn_status !== null)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => void refreshThreads(controller.signal),
      2_500,
    );
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [bootstrapped, refreshThreads, threads]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshProjects(controller.signal);
    return () => controller.abort();
  }, [bootstrapped, refreshProjects]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshMemory(controller.signal);
    return () => controller.abort();
  }, [bootstrapped, refreshMemory]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshOutput(controller.signal);
    return () => controller.abort();
  }, [bootstrapped, refreshOutput]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshSystemHealth(controller.signal);
    const timer = window.setInterval(
      () => void refreshSystemHealth(controller.signal),
      15_000,
    );
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [bootstrapped, refreshSystemHealth]);

  const hydrateImageBatchFacts = useCallback(async (
    threadId: string,
    throughSeq: number,
    signal?: AbortSignal,
  ) => {
    const history = await loadImageBatchFactHistory(
      threadId,
      throughSeq,
      (afterSeq, pageSignal) => client.eventPage(threadId, afterSeq, 1_000, pageSignal),
      signal,
    );
    if (signal?.aborted || selectedThreadId.current !== threadId) return;
    setImageBatchFacts((current) => mergeImageBatchFacts(history, current));
  }, [client]);

  const refreshProjection = useCallback(async (threadId: string, signal?: AbortSignal) => {
    const projection = await client.projection(threadId, signal);
    if (selectedThreadId.current !== threadId) return null;
    applyProjection(projection);
    await hydrateImageBatchFacts(threadId, projection.watermark, signal);
    return projection;
  }, [applyProjection, client, hydrateImageBatchFacts]);

  const refreshArtifacts = useCallback(async (currentThreadId: string, signal?: AbortSignal) => {
    const response = await client.listArtifacts(currentThreadId, signal);
    if (selectedThreadId.current !== currentThreadId) return;
    setArtifacts(response.items);
  }, [client]);

  const threadId = state.thread?.thread_id ?? null;

  const refreshConversationUsage = useCallback(async (
    targetThreadId: string,
    signal?: AbortSignal,
  ) => {
    try {
      const usage = await client.conversationUsage(targetThreadId, signal);
      if (signal?.aborted || stateRef.current.thread?.thread_id !== targetThreadId) {
        return false;
      }
      setConversationUsage(usage);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      // Usage is informational. A temporary failure must not degrade the
      // conversation transport or make an otherwise ready Composer unusable.
      if (stateRef.current.thread?.thread_id === targetThreadId) {
        setConversationUsage(null);
      }
      return false;
    }
  }, [client]);

  const refreshAccountUsage = useCallback(async (signal?: AbortSignal) => {
    try {
      const usage = await client.accountUsage(signal);
      if (signal?.aborted) return false;
      setAccountUsage(usage);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      return false;
    }
  }, [client]);

  useEffect(() => {
    if (!bootstrapped) return;
    const controller = new AbortController();
    void refreshAccountUsage(controller.signal);
    return () => controller.abort();
  }, [bootstrapped, refreshAccountUsage]);

  useEffect(() => {
    watermarkRef.current = state.watermark;
  }, [state.watermark]);

  useEffect(() => {
    if (threadId) selectedThreadId.current = threadId;
  }, [threadId]);

  useEffect(() => {
    if (!threadId) return;
    const controller = new AbortController();
    setImageBatchFacts(emptyImageBatchFacts());
    void hydrateImageBatchFacts(threadId, state.watermark, controller.signal).catch((error) => {
      if (!controller.signal.aborted) setTransportError(errorMessage(error));
    });
    return () => controller.abort();
  }, [hydrateImageBatchFacts, threadId]);

  useEffect(() => {
    if (!threadId) return;
    const controller = new AbortController();
    void refreshArtifacts(threadId, controller.signal).catch((error) => {
      if (!controller.signal.aborted) setTransportError(errorMessage(error));
    });
    return () => controller.abort();
  }, [refreshArtifacts, threadId]);

  useEffect(() => {
    if (!threadId) {
      setConversationUsage(null);
      return;
    }
    const controller = new AbortController();
    void refreshConversationUsage(threadId, controller.signal);
    return () => controller.abort();
  }, [refreshConversationUsage, threadId]);

  useEffect(() => {
    previewCache.reconcile(
      effectiveArtifacts
        .filter((artifact) => (
          artifact.actions.includes("preview")
          && artifact.family === "image"
        ))
        .map((artifact) => ({
          artifactId: artifact.artifact_id,
          revisionId: artifact.revision_id,
        })),
    );
  }, [effectiveArtifacts, previewCache]);

  const prefetchArtifactPreview = useCallback((artifact: ArtifactProjection) => {
    if (
      !artifact.actions.includes("preview")
      || artifact.family !== "image"
    ) return;
    void previewCache.ensure({
      artifactId: artifact.artifact_id,
      revisionId: artifact.revision_id,
    }).catch(() => undefined);
  }, [previewCache]);

  const loadArtifactPreview = useCallback((
    artifact: ArtifactProjection,
    signal: AbortSignal,
  ) => client.artifactBlob(artifact.artifact_id, "preview", signal), [client]);

  useEffect(() => () => {
    previewCache.dispose();
  }, [previewCache]);

  useEffect(() => {
    if (!threadId) return;
    const controller = new AbortController();
    const generation = ++eventStreamGeneration.current;
    const ownsStream = () => (
      !controller.signal.aborted
      && eventStreamGeneration.current === generation
      && selectedThreadId.current === threadId
    );
    let retry = 0;
    let frameId: number | null = null;
    let fallbackTimer: number | null = null;
    let pendingEvents: EventEnvelope[] = [];

    const cancelScheduledFlush = () => {
      if (frameId !== null) window.cancelAnimationFrame(frameId);
      if (fallbackTimer !== null) window.clearTimeout(fallbackTimer);
      frameId = null;
      fallbackTimer = null;
    };

    const flushEvents = () => {
      cancelScheduledFlush();
      if (pendingEvents.length === 0) return;
      const events = pendingEvents;
      pendingEvents = [];
      if (!ownsStream()) return;
      applyEventBatch(events);
      if (events.some((event) => event.event_type === "model.response_completed")) {
        void refreshConversationUsage(threadId);
        void refreshAccountUsage();
      }
      if (events.some((event) => event.event_type.startsWith("artifact."))) {
        void refreshArtifacts(threadId).catch((error) => {
          setTransportError(errorMessage(error));
        });
      }
      if (events.some((event) => (
        event.event_type === "thread.title_generated"
        || event.event_type === "thread.renamed"
        || event.event_type === "thread.archived"
        || event.event_type === "thread.restored"
        || event.event_type === "thread.deleted"
        || event.event_type === "turn.accepted"
        || event.event_type === "turn.queued"
        || (
          event.event_type === "turn.status_changed"
          && ["completed", "partial", "failed", "cancelled", "interrupted", "superseded"]
            .includes(String(event.payload.to))
        )
      ))) {
        void refreshThreads();
      }
    };

    const scheduleFrameFlush = () => {
      if (frameId !== null || fallbackTimer !== null) return;
      frameId = window.requestAnimationFrame(flushEvents);
      // Background tabs may throttle animation frames for seconds. Keep the
      // durable cursor moving without allowing an unbounded event buffer.
      fallbackTimer = window.setTimeout(flushEvents, 50);
    };

    const receiveEvent = (event: EventEnvelope) => {
      if (!ownsStream()) return;
      pendingEvents.push(event);
      if (!isFrameBatchableEvent(event) || pendingEvents.length >= 128) {
        flushEvents();
      } else {
        scheduleFrameFlush();
      }
    };

    const run = async () => {
      while (ownsStream()) {
        dispatch({ type: "stream.state", state: retry ? "retrying" : "connecting" });
        try {
          dispatch({ type: "stream.state", state: "open" });
          await client.streamEvents(
            threadId,
            watermarkRef.current,
            receiveEvent,
            controller.signal,
            () => void recoverPendingOperationsRef.current(),
          );
          if (!ownsStream()) return;
          flushEvents();
          retry = Math.min(retry + 1, 6);
        } catch (error) {
          if (!ownsStream()) return;
          if (error instanceof EventCursorResetRequired) {
            try {
              await refreshProjection(threadId, controller.signal);
              if (!ownsStream()) return;
              retry = 0;
              continue;
            } catch (refreshError) {
              if (!ownsStream()) return;
              setTransportError(errorMessage(refreshError));
            }
          } else {
            setTransportError(errorMessage(error));
          }
          retry = Math.min(retry + 1, 6);
        }
        if (!ownsStream()) return;
        dispatch({ type: "stream.state", state: "retrying" });
        await abortableDelay(Math.min(500 * 2 ** retry, 8_000), controller.signal).catch(
          () => undefined,
        );
      }
    };
    void run();
    return () => {
      controller.abort();
      cancelScheduledFlush();
      pendingEvents = [];
      dispatch({ type: "stream.state", state: "closed" });
    };
  }, [
    applyEventBatch,
    client,
    refreshAccountUsage,
    refreshArtifacts,
    refreshConversationUsage,
    refreshProjection,
    refreshThreads,
    threadId,
  ]);

  useEffect(() => {
    if (!threadId || !state.resyncRequired) return;
    const controller = new AbortController();
    void refreshProjection(threadId, controller.signal).catch((error) => {
      if (!controller.signal.aborted) setTransportError(errorMessage(error));
    });
    return () => controller.abort();
  }, [refreshProjection, state.resyncRequired, threadId]);

  useEffect(() => () => threadSwitchAbort.current?.abort(), []);

  const openThread = useCallback(async (targetThreadId: string) => {
    if (
      targetThreadId === selectedThreadId.current
      && stateRef.current.thread?.thread_id === targetThreadId
    ) {
      return true;
    }
    const generation = ++threadSwitchGeneration.current;
    threadSwitchAbort.current?.abort();
    const controller = new AbortController();
    threadSwitchAbort.current = controller;
    threadSwitchInProgress.current = true;
    setSwitchingThreadId(targetThreadId);
    setTransportError(null);
    try {
      const projection = await client.projection(targetThreadId, controller.signal);
      if (
        controller.signal.aborted
        || generation !== threadSwitchGeneration.current
      ) {
        return false;
      }
      // Keep the currently rendered task authoritative until the requested
      // projection has been read in full.  A mistyped/stale task ID must not
      // blank the existing conversation, and its in-flight event stream must
      // not silently discard events while the lookup is pending.
      selectedThreadId.current = targetThreadId;
      pendingNewThreadMetadata.current = { conversation_kind: "general" };
      setNewConversationProject(null);
      clearArtifactView();
      setImageBatchFacts(emptyImageBatchFacts());
      applyProjection(projection);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      if (generation === threadSwitchGeneration.current) {
        setTransportError(errorMessage(error));
      }
      return false;
    } finally {
      if (generation === threadSwitchGeneration.current) {
        threadSwitchAbort.current = null;
        threadSwitchInProgress.current = false;
        setSwitchingThreadId(null);
      }
    }
  }, [applyProjection, clearArtifactView, client]);

  const pendingThreadRequestId = useCallback((key: string, fingerprint: string) => {
    const pending = pendingThreadMutations.current.get(key);
    if (pending?.fingerprint === fingerprint) return pending.clientRequestId;
    const clientRequestId = createClientRequestId(key.replaceAll(":", "_"));
    pendingThreadMutations.current.set(key, { fingerprint, clientRequestId });
    return clientRequestId;
  }, []);

  const renameThread = useCallback(async (targetThreadId: string, rawTitle: string) => {
    const title = rawTitle.trim();
    if (!title) {
      setThreadCatalogError("任务名称不能为空。");
      return false;
    }
    const key = `rename:${targetThreadId}`;
    const clientRequestId = pendingThreadRequestId(key, title);
    setThreadMutationKey(key);
    setThreadCatalogError(null);
    try {
      await client.renameThread(targetThreadId, title, clientRequestId);
      pendingThreadMutations.current.delete(key);
      if (selectedThreadId.current === targetThreadId) {
        await refreshProjection(targetThreadId);
      }
      await refreshThreads();
      return true;
    } catch (error) {
      setThreadCatalogError(errorMessage(error));
      return false;
    } finally {
      setThreadMutationKey((current) => current === key ? null : current);
    }
  }, [client, pendingThreadRequestId, refreshProjection, refreshThreads]);

  const setThreadArchived = useCallback(async (
    targetThreadId: string,
    archived: boolean,
  ) => {
    const operation = archived ? "archive" : "restore";
    const key = `${operation}:${targetThreadId}`;
    const clientRequestId = pendingThreadRequestId(key, operation);
    setThreadMutationKey(key);
    setThreadCatalogError(null);
    try {
      const projection = await client.setThreadArchived(
        targetThreadId,
        archived,
        clientRequestId,
      );
      pendingThreadMutations.current.delete(key);
      if (
        archived
        && projection.status === "archived"
        && selectedThreadId.current === targetThreadId
      ) {
        ++threadSwitchGeneration.current;
        threadSwitchAbort.current?.abort();
        threadSwitchAbort.current = null;
        threadSwitchInProgress.current = false;
        selectedThreadId.current = null;
        setSwitchingThreadId(null);
        clearArtifactView();
        clearThreadProjection();
      } else if (selectedThreadId.current === targetThreadId) {
        await refreshProjection(targetThreadId);
      }
      await refreshThreads();
      return true;
    } catch (error) {
      setThreadCatalogError(errorMessage(error));
      return false;
    } finally {
      setThreadMutationKey((current) => current === key ? null : current);
    }
  }, [
    clearArtifactView,
    clearThreadProjection,
    client,
    pendingThreadRequestId,
    refreshProjection,
    refreshThreads,
  ]);

  const setThreadPinned = useCallback(async (
    targetThreadId: string,
    pinned: boolean,
  ) => {
    const operation = pinned ? "pin" : "unpin";
    const key = `${operation}:${targetThreadId}`;
    const clientRequestId = pendingThreadRequestId(key, operation);
    setThreadMutationKey(key);
    setThreadCatalogError(null);
    try {
      const projection = await client.setThreadPinned(
        targetThreadId,
        pinned,
        clientRequestId,
      );
      pendingThreadMutations.current.delete(key);
      if (selectedThreadId.current === targetThreadId) {
        await refreshProjection(projection.thread_id);
      }
      await refreshThreads();
      return true;
    } catch (error) {
      setThreadCatalogError(errorMessage(error));
      return false;
    } finally {
      setThreadMutationKey((current) => current === key ? null : current);
    }
  }, [client, pendingThreadRequestId, refreshProjection, refreshThreads]);

  const deleteThread = useCallback(async (targetThreadId: string) => {
    const key = `delete:${targetThreadId}`;
    const clientRequestId = pendingThreadRequestId(key, "delete");
    setThreadMutationKey(key);
    setThreadCatalogError(null);
    try {
      const projection = await client.deleteThread(targetThreadId, clientRequestId);
      if (projection.status !== "deleted") {
        throw new RuntimeApiError("Runtime 未确认任务删除。", 502);
      }
      pendingThreadMutations.current.delete(key);
      if (selectedThreadId.current === targetThreadId) {
        ++threadSwitchGeneration.current;
        threadSwitchAbort.current?.abort();
        threadSwitchAbort.current = null;
        threadSwitchInProgress.current = false;
        selectedThreadId.current = null;
        setSwitchingThreadId(null);
        clearArtifactView();
        clearThreadProjection();
      }
      await refreshThreads();
      return true;
    } catch (error) {
      setThreadCatalogError(errorMessage(error));
      return false;
    } finally {
      setThreadMutationKey((current) => current === key ? null : current);
    }
  }, [
    clearArtifactView,
    clearThreadProjection,
    client,
    pendingThreadRequestId,
    refreshThreads,
  ]);

  const listShares = useCallback((targetThreadId: string, signal?: AbortSignal) => (
    client.listShares(targetThreadId, signal)
  ), [client]);

  const createShare = useCallback((
    targetThreadId: string,
    expiresInHours: number,
    clientRequestId?: string,
  ): Promise<ShareSnapshotProjection> => (
    client.createShare(targetThreadId, expiresInHours, clientRequestId)
  ), [client]);

  const getShare = useCallback((shareId: string, signal?: AbortSignal) => (
    client.share(shareId, signal)
  ), [client]);

  const revokeShare = useCallback((
    shareId: string,
    clientRequestId?: string,
  ): Promise<ShareSnapshotProjection> => (
    client.revokeShare(shareId, clientRequestId)
  ), [client]);

  const mockReplay = useCallback((
    targetThreadId: string,
    signal?: AbortSignal,
  ): Promise<MockReplayResponse> => (
    client.mockReplay(targetThreadId, signal)
  ), [client]);

  const liveReplay = useCallback(async (
    targetThreadId: string,
    sourceTurnId: string,
    clientRequestId: string,
  ): Promise<LiveReplayResponse> => {
    const requestKey = JSON.stringify([targetThreadId, sourceTurnId]);
    const stableClientRequestId = pendingLiveReplayRequests.current.get(requestKey)
      ?? clientRequestId;
    pendingLiveReplayRequests.current.set(requestKey, stableClientRequestId);
    const response = await client.liveReplay(
      targetThreadId,
      sourceTurnId,
      stableClientRequestId,
    );
    if (selectedThreadId.current === targetThreadId) {
      await refreshProjection(targetThreadId);
    }
    pendingLiveReplayRequests.current.delete(requestKey);
    return response;
  }, [client, refreshProjection]);

  const confirmOperationFromEvents = useCallback(async (
    operation: ClientOperation,
    targetThreadId: string,
    signal?: AbortSignal,
  ) => {
    const { module, outbox } = await loadOperationSupport();
    return module.confirmClientOperationEvents({
      operation,
      eventPage: (afterSeq, pageSignal) => (
        client.eventPage(targetThreadId, afterSeq, 1_000, pageSignal)
      ),
      acknowledge: acknowledgeClientMessages,
      stillPending: () => outbox.get(operation.operation_id) !== null,
      signal,
    });
  }, [acknowledgeClientMessages, client, loadOperationSupport]);

  const deliverClientOperation = useCallback(async (
    operation: ClientOperation,
    focusThread: boolean,
  ) => {
    const { module, outbox: operationOutbox } = await loadOperationSupport();
    let record = operationOutbox.stage(operation);
    pendingSendOperation.current = operation;
    let targetThreadId = module.resolvedOperationThreadId(record);
    if (!targetThreadId) {
      const created = await client.createThread(operation);
      record = operationOutbox.resolveThread(operation.operation_id, created.thread_id);
      targetThreadId = created.thread_id;
      void refreshThreads();
    }
    if (focusThread) selectedThreadId.current = targetThreadId;

    switch (operation.disposition) {
      case "create":
        await client.createTurn(targetThreadId, operation);
        break;
      case "steer":
        await client.steerTurn(operation);
        break;
      case "queue":
        await client.queueTurn(targetThreadId, operation);
        break;
      case "replace":
        await client.replaceTurn(operation);
        break;
    }

    const projection = await client.projection(targetThreadId);
    if (focusThread && selectedThreadId.current === targetThreadId) {
      applyProjection(projection);
    } else {
      acknowledgeClientMessages(projectionClientMessageIds(projection));
    }
    if (operationOutbox.get(operation.operation_id)) {
      await confirmOperationFromEvents(operation, targetThreadId);
    }
    const confirmed = operationOutbox.get(operation.operation_id) === null;
    if (confirmed && focusThread && selectedThreadId.current === targetThreadId) {
      void refreshArtifacts(targetThreadId).catch((error) => {
        setTransportError(errorMessage(error));
      });
    }
    void refreshThreads();
    return confirmed;
  }, [
    acknowledgeClientMessages,
    applyProjection,
    client,
    confirmOperationFromEvents,
    loadOperationSupport,
    refreshArtifacts,
    refreshThreads,
  ]);

  const recoverPendingOperations = useCallback(async () => {
    if (recoveringOperationsRef.current || submittingRef.current) return;
    let support: Awaited<ReturnType<typeof loadOperationSupport>>;
    try {
      support = await loadOperationSupport();
    } catch (error) {
      setTransportError(errorMessage(error));
      return;
    }
    const { module, outbox: operationOutbox } = support;
    const records = operationOutbox.list();
    if (!records.length) return;
    recoveringOperationsRef.current = true;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      for (const record of records) {
        const targetThreadId = module.resolvedOperationThreadId(record);
        const focusThread = !selectedThreadId.current
          || selectedThreadId.current === targetThreadId;
        try {
          await deliverClientOperation(record.operation, focusThread);
        } catch (error) {
          setTransportError(
            `待发送消息尚未确认：${errorMessage(error)}`,
          );
        }
      }
    } finally {
      recoveringOperationsRef.current = false;
      submittingRef.current = false;
      setSubmitting(false);
    }
  }, [deliverClientOperation, loadOperationSupport]);
  recoverPendingOperationsRef.current = recoverPendingOperations;

  const sendMessage = useCallback(async (
    rawInput: string,
    disposition: SendDisposition = "steer",
    attachments: readonly InputAttachmentProjection[] = [],
    explicitToolIds: readonly string[] = [],
  ) => {
    const uniqueToolIds = [...new Set(explicitToolIds)];
    const skillHints = uniqueToolIds
      .filter((reference) => reference.startsWith("skill:"))
      .map((reference) => `@${reference.slice("skill:".length)}`);
    const userInput = rawInput.trim() || (attachments.length > 0
      ? "请查看并处理随消息发送的附件。"
      : "");
    const input = [...skillHints, userInput].filter(Boolean).join(" ");
    if (!input || submittingRef.current || threadSwitchInProgress.current) return false;
    submittingRef.current = true;
    setSubmitting(true);
    setTransportError(null);
    let operationOutbox = operationOutboxRef.current;
    try {
      const support = await loadOperationSupport();
      operationOutbox = support.outbox;
      const currentThreadId = stateRef.current.thread?.thread_id ?? null;
      if (currentThreadId) selectedThreadId.current = currentThreadId;
      const current = stateRef.current;
      const active = selectActiveTurn(current);
      if (!chatModel) {
        throw new RuntimeApiError("无可用 Agent 模型，请刷新后重试。", 422);
      }
      const selectedModels = modelSelectionForMutation(
        chatModel,
        imageModel,
        active,
        disposition,
      );
      const pendingRecord = pendingSendOperation.current
        ? operationOutbox.get(pendingSendOperation.current.operation_id)
        : null;
      if (pendingRecord && !support.module.operationMatchesRetry(
        pendingRecord,
        input,
        currentThreadId,
        attachments,
        uniqueToolIds,
      )) {
        throw new Error("上一条消息待确认，请保留内容后重试。");
      }
      const operation = pendingRecord?.operation ?? support.module.createClientOperation({
            input,
            explicitToolIds: uniqueToolIds,
            attachments,
            threadId: currentThreadId,
            threadMetadata: currentThreadId ? undefined : pendingNewThreadMetadata.current,
            activeTurn: active,
            disposition,
            models: selectedModels,
            observedAfterSeq: current.watermark,
          });
      pendingSendOperation.current = operation;
      const delivered = await deliverClientOperation(operation, true);
      if (delivered && operation.thread.kind === "create") {
        pendingNewThreadMetadata.current = { conversation_kind: "general" };
        setNewConversationProject(null);
        void refreshProjects();
      }
      return delivered;
    } catch (error) {
      const pending = pendingSendOperation.current;
      if (pending && operationOutbox && !operationOutbox.get(pending.operation_id)) {
        pendingSendOperation.current = null;
        return true;
      }
      setTransportError(errorMessage(error));
      return false;
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }, [
    chatModel,
    deliverClientOperation,
    imageModel,
    loadOperationSupport,
    refreshProjects,
  ]);

  const uploadInputAttachment = useCallback(async (file: File) => {
    try {
      return await client.uploadInputAttachment(file);
    } catch (error) {
      setTransportError(errorMessage(error));
      return null;
    }
  }, [client]);

  const loadInputAttachment = useCallback((attachmentId: string, signal?: AbortSignal) => (
    client.inputAttachmentBlob(attachmentId, signal)
  ), [client]);

  const loadInputAttachmentThumbnail = useCallback((attachmentId: string, signal?: AbortSignal) => (
    client.inputAttachmentThumbnailBlob(attachmentId, signal)
  ), [client]);

  useEffect(() => {
    if (!bootstrapped) return;
    void recoverPendingOperations();
    const recover = () => void recoverPendingOperations();
    window.addEventListener("online", recover);
    return () => window.removeEventListener("online", recover);
  }, [bootstrapped, recoverPendingOperations]);

  const interrupt = useCallback(async () => {
    const active = selectActiveTurn(stateRef.current);
    if (!active) return;
    try {
      await client.interruptTurn(active.turn_id);
      if (stateRef.current.thread) {
        await refreshProjection(stateRef.current.thread.thread_id);
      }
    } catch (error) {
      setTransportError(errorMessage(error));
    }
  }, [client, refreshProjection]);

  const respondInteraction = useCallback(async (
    interactionId: string,
    response: InteractionResponse,
    clientRequestId: string,
  ) => {
    await client.respondInteraction(interactionId, response, clientRequestId);
    const threadId = stateRef.current.thread?.thread_id;
    if (threadId) {
      await refreshProjection(threadId);
    }
  }, [client, refreshProjection]);

  const updatePermission = useCallback(async (
    profile: "default" | "full_access",
  ) => {
    if (permissionUpdatingRef.current) return false;
    const currentBootstrap = stateRef.current.bootstrap;
    if (!currentBootstrap) {
      setPermissionError("权限尚未加载，请重新连接后再试。");
      return false;
    }
    if (currentBootstrap.permissions.profile === profile) {
      pendingPermissionMutation.current = null;
      setPermissionError(null);
      return true;
    }
    const existing = pendingPermissionMutation.current;
    const mutation = existing
      && existing.profile === profile
      && existing.expectedRevision === currentBootstrap.permissions.revision
      ? existing
      : {
          profile,
          expectedRevision: currentBootstrap.permissions.revision,
          clientRequestId: `permission_${crypto.randomUUID().replaceAll("-", "")}`,
        };
    pendingPermissionMutation.current = mutation;
    permissionUpdatingRef.current = true;
    setPermissionUpdating(true);
    setPermissionError(null);
    try {
      const response = await client.updatePermission(
        mutation.profile,
        mutation.expectedRevision,
        mutation.clientRequestId,
      );
      const bootstrap = stateRef.current.bootstrap;
      if (bootstrap) {
        const nextBootstrap = { ...bootstrap, permissions: response.permissions };
        stateRef.current = { ...stateRef.current, bootstrap: nextBootstrap };
        dispatch({
          type: "bootstrap.received",
          bootstrap: nextBootstrap,
        });
      }
      pendingPermissionMutation.current = null;
      if (response.permissions.profile !== profile) {
        setPermissionError("权限已在其他位置变更，已同步当前设置。");
        return false;
      }
      return true;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) {
        pendingPermissionMutation.current = null;
        try {
          const fresh = await client.bootstrap();
          client.acceptBootstrap(fresh);
          stateRef.current = { ...stateRef.current, bootstrap: fresh };
          dispatch({ type: "bootstrap.received", bootstrap: fresh });
          setPermissionError("权限已变更，已刷新设置，请确认后重试。");
        } catch (refreshError) {
          setPermissionError(
            `权限已发生并发变更，且刷新失败：${errorMessage(refreshError)}`,
          );
        }
      } else {
        setPermissionError(errorMessage(error));
      }
      return false;
    } finally {
      permissionUpdatingRef.current = false;
      setPermissionUpdating(false);
    }
  }, [chatModel, client, imageModel]);

  const checkUpdate = useCallback(async () => {
    if (updateBusy) return false;
    setUpdateBusy(true);
    setUpdateError(null);
    try {
      const response = await client.checkUpdate();
      applyUpdateSnapshot(response.update);
      return true;
    } catch (error) {
      setUpdateError(errorMessage(error));
      return false;
    } finally {
      setUpdateBusy(false);
    }
  }, [applyUpdateSnapshot, client, updateBusy]);

  const activateUpdate = useCallback(async () => {
    if (updateBusy) return false;
    let update = stateRef.current.bootstrap?.update;
    if (!update || !["available", "awaiting_user"].includes(update.state)) {
      setUpdateError("新版尚未准备好，请重新检查更新。");
      return false;
    }
    let updatedWindow: Window | null = null;
    try {
      updatedWindow = window.open(
        "about:blank",
        `emate-updated-runtime-${crypto.randomUUID()}`,
      );
      if (updatedWindow) {
        updatedWindow.document.title = "e-Mate 正在更新";
        updatedWindow.document.body.textContent = "正在下载、校验并安装新版 e-Mate…";
      }
    } catch {
      updatedWindow = null;
    }
    setUpdateBusy(true);
    setUpdateError(null);
    try {
      if (update.state === "available") {
        const prepared = await client.checkUpdate();
        applyUpdateSnapshot(prepared.update);
        update = prepared.update;
      }
      if (!update.transaction_id || !update.can_activate) {
        throw new Error(
          update.state === "failed"
            ? "更新下载或校验失败，当前版本未受影响，请稍后重试。"
            : "新版尚未完成校验，暂时不能安装。",
        );
      }
      const pending = pendingUpdateActivation.current;
      const activation = pending?.transactionId === update.transaction_id
        ? pending
        : {
            transactionId: update.transaction_id,
            clientRequestId: `update_${crypto.randomUUID().replaceAll("-", "")}`,
          };
      pendingUpdateActivation.current = activation;
      const response = await client.activateUpdate(
        activation.transactionId,
        activation.clientRequestId,
      );
      applyUpdateSnapshot(response.update);
      pendingUpdateActivation.current = null;
      if (response.restart_scheduled) {
        const { handOffToUpdatedRuntime } = await import("./updateActivationHandoff.ts");
        await handOffToUpdatedRuntime({
          readBootstrap: () => client.bootstrap(),
          targetVersion: update.target_version ?? "",
          initialDelayMs: response.reload_after_ms,
          currentUrl: window.location.href,
          replace: (url) => window.location.replace(url),
          openUpdatedRuntime: updatedWindow
            ? (url) => {
                if (!updatedWindow || updatedWindow.closed) return false;
                updatedWindow.location.replace(url);
                return true;
              }
            : undefined,
        });
      }
      return true;
    } catch (error) {
      if (updatedWindow && !updatedWindow.closed) updatedWindow.close();
      setUpdateError(errorMessage(error));
      return false;
    } finally {
      setUpdateBusy(false);
    }
  }, [applyUpdateSnapshot, client, updateBusy]);

  const resetLearnedMemory = useCallback(async () => {
    if (memoryBusy) return false;
    const existing = pendingMemoryMutation.current;
    const mutation: PendingMemoryMutation = existing?.operation === "reset"
      ? existing
      : {
          operation: "reset",
          target: "learned",
          clientRequestId: createClientRequestId("reset_memory"),
        };
    pendingMemoryMutation.current = mutation;
    setMemoryBusy(true);
    setMemoryError(null);
    try {
      const response = await client.resetLearnedMemory(mutation.clientRequestId);
      pendingMemoryMutation.current = null;
      setMemory(response.memory);
      setMemoryLoadState("ready");
      return true;
    } catch (error) {
      setMemoryError(errorMessage(error));
      return false;
    } finally {
      setMemoryBusy(false);
    }
  }, [client, memoryBusy]);

  const undoLearnedMemoryReset = useCallback(async (resetId: string) => {
    if (memoryBusy) return false;
    const existing = pendingMemoryMutation.current;
    const mutation: PendingMemoryMutation = (
      existing?.operation === "undo" && existing.target === resetId
    )
      ? existing
      : {
          operation: "undo",
          target: resetId,
          clientRequestId: createClientRequestId("undo_memory_reset"),
        };
    pendingMemoryMutation.current = mutation;
    setMemoryBusy(true);
    setMemoryError(null);
    try {
      const response = await client.undoLearnedMemoryReset(
        resetId,
        mutation.clientRequestId,
      );
      pendingMemoryMutation.current = null;
      setMemory(response.memory);
      setMemoryLoadState("ready");
      return true;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 410) {
        pendingMemoryMutation.current = null;
        await refreshMemory();
      }
      setMemoryError(errorMessage(error));
      return false;
    } finally {
      setMemoryBusy(false);
    }
  }, [client, memoryBusy, refreshMemory]);

  const updateOutputLocation = useCallback(async (locationAlias: OutputLocationAlias) => {
    if (outputBusy || !outputPreference) return false;
    const existing = pendingOutputPreference.current;
    const mutation = (
      existing?.locationAlias === locationAlias
      && existing.expectedRevision === outputPreference.revision
    )
      ? existing
      : {
          locationAlias,
          expectedRevision: outputPreference.revision,
          clientRequestId: createClientRequestId("output_preference"),
        };
    pendingOutputPreference.current = mutation;
    setOutputBusy(true);
    setOutputError(null);
    try {
      const preference = await client.updateOutputPreference(
        mutation.locationAlias,
        mutation.expectedRevision,
        mutation.clientRequestId,
      );
      pendingOutputPreference.current = null;
      setOutputPreference(preference);
      setOutputLoadState("ready");
      return true;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) {
        pendingOutputPreference.current = null;
        await refreshOutput();
      }
      setOutputError(errorMessage(error));
      return false;
    } finally {
      setOutputBusy(false);
    }
  }, [client, outputBusy, outputPreference, refreshOutput]);

  const pickOutputLocation = useCallback(async () => {
    if (outputBusy || !outputPreference) return false;
    setOutputBusy(true);
    setOutputError(null);
    try {
      const preference = await client.pickOutputLocation(
        outputPreference.revision,
        createClientRequestId("pick_output_location"),
      );
      pendingOutputPreference.current = null;
      setOutputPreference(preference);
      setOutputLoadState("ready");
      return true;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) {
        await refreshOutput();
      }
      setOutputError(errorMessage(error));
      return false;
    } finally {
      setOutputBusy(false);
    }
  }, [client, outputBusy, outputPreference, refreshOutput]);

  const newTask = useCallback((project: ProjectProjection | null = null) => {
    ++threadSwitchGeneration.current;
    threadSwitchAbort.current?.abort();
    threadSwitchAbort.current = null;
    threadSwitchInProgress.current = false;
    selectedThreadId.current = null;
    pendingNewThreadMetadata.current = project
      ? {
          conversation_kind: "project",
          project_id: project.project_id,
          project_name: project.name,
        }
      : { conversation_kind: "general" };
    setNewConversationProject(project);
    setSwitchingThreadId(null);
    clearArtifactView();
    clearThreadProjection();
    setTransportError(null);
  }, [clearArtifactView, clearThreadProjection]);

  const feedbackArtifact = useCallback(async (
    artifact: ArtifactProjection,
    signal: "thumbs_up" | "thumbs_down",
  ) => {
    try {
      await client.artifactFeedback(artifact, signal);
      if (stateRef.current.thread) {
        await refreshArtifacts(stateRef.current.thread.thread_id);
      }
    } catch (error) {
      setTransportError(errorMessage(error));
    }
  }, [client, refreshArtifacts]);

  const downloadArtifact = useCallback(async (
    artifact: ArtifactProjection,
  ): Promise<OutputMaterialization | null> => {
    const key = `${artifact.artifact_id}:${artifact.revision_id}`;
    const clientRequestId = pendingArtifactMaterializations.current.get(key)
      ?? createClientRequestId("materialize_artifact");
    pendingArtifactMaterializations.current.set(key, clientRequestId);
    try {
      const receipt = await client.materializeArtifact(artifact, clientRequestId);
      if (receipt.status === "completed") pendingArtifactMaterializations.current.delete(key);
      return receipt;
    } catch (error) {
      setTransportError(errorMessage(error));
      return null;
    }
  }, [client]);

  const performArtifactExternalAction = useCallback(async (
    artifact: ArtifactProjection,
    action: "open" | "reveal",
  ) => {
    try {
      const receipt = await client.artifactExternalAction(artifact.artifact_id, action);
      return receipt.status === "completed";
    } catch (error) {
      setTransportError(errorMessage(error));
      return false;
    }
  }, [client]);

  const submitRetouch = useCallback(async (
    artifact: ArtifactProjection,
    annotations: RetouchAnnotation[],
    instruction: string,
  ) => {
    if (!imageModel) {
      setTransportError("图片模型暂不可用，请刷新模型目录后重试。");
      throw new Error("图片模型暂不可用，请刷新模型目录后重试。");
    }
    try {
      return await client.requestRetouch(artifact, annotations, instruction, {
        agentModelId: chatModel,
        imageModelId: imageModel,
      });
    } catch (error) {
      setTransportError(errorMessage(error));
      return null;
    }
  }, [chatModel, client, imageModel]);

  const openRetouchWorkspace = useCallback(async (artifact: ArtifactProjection) => {
    return client.openRetouchWorkspace(artifact);
  }, [client]);

  const getRetouchWorkspace = useCallback(async (
    workspaceId: string,
    signal?: AbortSignal,
  ) => {
    return client.getRetouchWorkspace(workspaceId, signal);
  }, [client]);

  const saveRetouchWorkspace = useCallback(async (
    workspace: RetouchWorkspaceProjection,
    input: {
      annotations: RetouchAnnotation[];
      referenceArtifactIds: string[];
      globalInstruction: string;
      viewState: Partial<RetouchViewState>;
    },
  ) => {
    return client.saveRetouchWorkspace(workspace, input);
  }, [client]);

  const submitRetouchWorkspace = useCallback(async (
    workspace: RetouchWorkspaceProjection,
  ) => {
    if (!imageModel) {
      setTransportError("图片模型暂不可用，请刷新模型目录后重试。");
      throw new Error("图片模型暂不可用，请刷新模型目录后重试。");
    }
    return client.submitRetouchWorkspace(workspace, {
      agentModelId: chatModel,
      imageModelId: imageModel,
    });
  }, [chatModel, client, imageModel]);

  const reopenRetouchWorkspace = useCallback(async (
    workspace: RetouchWorkspaceProjection,
  ) => {
    return client.reopenRetouchWorkspace(workspace);
  }, [client]);

  const loadRetouchWorkspaceBlob = useCallback((
    workspaceId: string,
    kind: "surface" | "result" | "reference",
    referenceArtifactId?: string,
    signal?: AbortSignal,
  ) => client.retouchWorkspaceBlob(
    workspaceId,
    kind,
    referenceArtifactId,
    signal,
  ), [client]);

  const loginSession = useCallback(async (identifier: string, password: string) => {
    if (sessionBusyRef.current) return null;
    sessionBusyRef.current = true;
    setSessionBusy(true);
    setSessionError(null);
    try {
      const receipt = await client.loginSession(identifier, password);
      if (!receipt.restart_scheduled) {
        setSessionError(
          "登录已完成，正在自动重新连接 e-Mate…",
        );
        window.setTimeout(() => window.location.reload(), 1_500);
        return null;
      }
      if (await client.waitForCredentialRotation({ timeoutMs: 90_000 })) {
        window.location.reload();
        return receipt;
      }
      setSessionError(
        "e-Mate 正在重新连接，请稍候…",
      );
      // A local Runtime restart can temporarily outlive the credential
      // hand-off poll on slower machines.  Continue automatically instead of
      // asking a non-technical user to relaunch the desktop shortcut.
      window.setTimeout(() => window.location.reload(), 1_500);
      return receipt;
    } catch (error) {
      setSessionError(errorMessage(error));
      return null;
    } finally {
      sessionBusyRef.current = false;
      setSessionBusy(false);
    }
  }, [client]);

  const logoutSession = useCallback(async () => {
    if (sessionBusyRef.current) return null;
    const bootstrap = stateRef.current.bootstrap;
    const leaseDigest = bootstrap?.login.session_lease_digest;
    if (!bootstrap?.login.authenticated || !leaseDigest) {
      setSessionError("登录状态尚未准备好，请刷新页面后再退出。");
      return null;
    }
    sessionBusyRef.current = true;
    setSessionBusy(true);
    setSessionError(null);
    try {
      const receipt = await client.logoutSession(leaseDigest);
      const loggedOutBootstrap: BootstrapResponse = {
        ...bootstrap,
        login: {
          authenticated: false,
          account_id: null,
          display_name: null,
          organization_id: null,
          roles: [],
          session_revision: null,
          session_lease_digest: null,
        },
        policy_lease: null,
        model_service: {
          state: "unavailable",
          reason: "managed_session_unavailable",
        },
      };
      stateRef.current = { ...stateRef.current, bootstrap: loggedOutBootstrap };
      dispatch({ type: "bootstrap.received", bootstrap: loggedOutBootstrap });
      if (receipt.restart_scheduled) {
        window.setTimeout(() => window.location.reload(), 1_500);
      }
      return receipt;
    } catch (error) {
      if (error instanceof RuntimeApiError && error.code === "session_lease_changed") {
        setSessionError("登录状态刚刚发生变化，请刷新页面后再退出。");
      } else {
        setSessionError(errorMessage(error));
      }
      return null;
    } finally {
      sessionBusyRef.current = false;
      setSessionBusy(false);
    }
  }, [client]);

  return {
    client,
    state,
    serverClockOffsetMs: serverClockOffsetMs.current,
    loadState,
    transportError,
    clearTransportError: () => setTransportError(null),
    retryBootstrap: () => void loadBootstrap(),
    submitting,
    permissionUpdating,
    permissionError,
    clearPermissionError: () => setPermissionError(null),
    updateBusy,
    updateError,
    clearUpdateError: () => setUpdateError(null),
    sessionBusy,
    sessionError,
    clearSessionError: () => setSessionError(null),
    loginSession,
    logoutSession,
    memory,
    memoryLoadState,
    memoryBusy,
    memoryError,
    clearMemoryError: () => setMemoryError(null),
    refreshMemory: () => void refreshMemory(),
    outputLocations,
    outputPreference,
    outputLoadState,
    outputBusy,
    outputError,
    clearOutputError: () => setOutputError(null),
    refreshOutput: () => void refreshOutput(),
    updateOutputLocation,
    pickOutputLocation,
    systemHealth,
    systemHealthLoadState,
    systemHealthError,
    clearSystemHealthError: () => setSystemHealthError(null),
    refreshSystemHealth: () => void refreshSystemHealth(),
    loadSystemTechnicalHealth,
    accountUsage,
    refreshAccountUsage: () => refreshAccountUsage(),
    conversationUsage,
    capabilityMentions,
    capabilityMentionState,
    refreshCapabilityMentions,
    refreshConversationUsage: () => {
      const activeThreadId = stateRef.current.thread?.thread_id;
      return activeThreadId ? refreshConversationUsage(activeThreadId) : Promise.resolve(false);
    },
    chatModel,
    setChatModel,
    imageModel,
    setImageModel,
    threads,
    projects,
    projectCatalogState,
    projectCatalogError,
    projectPickerBusy,
    newConversationProject,
    clearProjectCatalogError: () => setProjectCatalogError(null),
    refreshProjects: () => void refreshProjects(),
    pickProject,
    threadCatalogState,
    threadCatalogError,
    clearThreadCatalogError: () => setThreadCatalogError(null),
    switchingThreadId,
    threadMutationKey,
    refreshThreads: () => void refreshThreads(),
    openThread,
    renameThread,
    pinThread: (targetThreadId: string) => setThreadPinned(targetThreadId, true),
    unpinThread: (targetThreadId: string) => setThreadPinned(targetThreadId, false),
    archiveThread: (targetThreadId: string) => setThreadArchived(targetThreadId, true),
    restoreThread: (targetThreadId: string) => setThreadArchived(targetThreadId, false),
    deleteThread,
    listShares,
    createShare,
    getShare,
    revokeShare,
    mockReplay,
    liveReplay,
    ...connectorSession,
    ...extensionSession,
    turns: selectTurns(state),
    items: selectItems(state),
    interactions: selectInteractions(state),
    activeTurn: selectActiveTurn(state),
    isThinking: selectIsThinking(state),
    visibleReasoning: selectVisibleReasoning(state),
    pendingInteractions: selectPendingInteractions(state),
    refreshProjection,
    artifacts: effectiveArtifacts,
    imageBatchFailures: selectFailedImageBatchSlots(imageBatchFacts),
    artifactPreviewUrls,
    sendMessage,
    uploadInputAttachment,
    loadInputAttachment,
    loadInputAttachmentThumbnail,
    interrupt,
    respondInteraction,
    updatePermission,
    checkUpdate,
    activateUpdate,
    resetLearnedMemory,
    undoLearnedMemoryReset,
    feedbackArtifact,
    downloadArtifact,
    performArtifactExternalAction,
    loadArtifactPreview,
    prefetchArtifactPreview,
    submitRetouch,
    openRetouchWorkspace,
    getRetouchWorkspace,
    saveRetouchWorkspace,
    submitRetouchWorkspace,
    reopenRetouchWorkspace,
    loadRetouchWorkspaceBlob,
    newTask,
  };
}
