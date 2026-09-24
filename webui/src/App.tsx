import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Monitor, Moon, Sun, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResourceDeleteConfirmDialog } from "@/components/ui/resource-delete-confirm-dialog";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { RenameChatDialog } from "@/components/RenameChatDialog";
import { Sidebar } from "@/components/Sidebar";
import type { SettingsSectionKey } from "@/components/settings/types";
import { WallpaperBackground } from "@/components/thread/WallpaperBackground";
import { useWallpaper, useWallpaperVisible, isGlassActive, WALLPAPER_GLASS_BLUR_PX } from "@/hooks/useWallpaper";
import { SearchDialog } from "@/components/search/SearchDialog";
import { ThreadShell, resolvedModelProvider } from "@/components/thread/ThreadShell";
import { TopBar } from "@/components/thread/TopBar";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { VIEW_REGISTRY, getSidebarNavItems, getView, type ViewRenderContext } from "@/views/registry";

import { useSessions } from "@/hooks/useSessions";
import { useDeferredTitleRefresh } from "@/hooks/useDeferredTitleRefresh";
import { useSidebarState } from "@/hooks/useSidebarState";
import { ThemeProvider, useTheme, type ThemeMode } from "@/hooks/useTheme";
import { useChatRunStatus } from "@/hooks/useChatRunStatus";
import { useDeleteRenameDialog } from "@/hooks/useDeleteRenameDialog";
import { useRestartFlow } from "@/hooks/useRestartFlow";
import { useSidebarActions } from "@/hooks/useSidebarActions";
import {
  useWorkspaceScope,
  normalizeWorkspaceScope,
} from "@/hooks/useWorkspaceScope";
import { cn } from "@/lib/utils";
import {
  supportedLocales,
  persistLocale,
  applyDocumentLocale,
  type SupportedLocale,
} from "@/i18n/config";
import {
  BootstrapError,
  deriveWsUrl,
  fetchBootstrapWithRetry,
  loadSavedSecret,
  saveSecret,
} from "@/lib/bootstrap";
import { deriveTitle } from "@/lib/format";
import { ErzaClient } from "@/lib/erza-client";
import { ClientProvider, useClient } from "@/providers/ClientProvider";
import type {
  ChatSummary,
  RuntimeSurface,
  SettingsPayload,
  WorkspaceScopePayload,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchSettings, updateSettings } from "@/lib/api";
import {
  createRuntimeHost,
  toRuntimeSurface,
} from "@/lib/runtime";
import { projectNameFromPath } from "@/lib/workspace";
import { STORAGE_KEYS } from "@/lib/storage";

type BootState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "auth"; failed?: boolean }
  | {
      status: "ready";
      client: ErzaClient;
      token: string;
      tokenExpiresAt: number;
      modelName: string | null;
      runtimeSurface: RuntimeSurface;
      version: string | null;
      /** 服务端 WebSocket 帧大小上限(字节),由 bootstrap 响应回传。
       * 用于在 Composer 发送前校验附件总字节数(见设计 §4.5)。 */
      maxMessageBytes: number;
    };

const SIDEBAR_STORAGE_KEY = STORAGE_KEYS.sidebar;
const SIDEBAR_WIDTH = 272;
const SIDEBAR_RAIL_WIDTH = 56;
const TOKEN_REFRESH_MARGIN_MS = 30_000;
const TOKEN_REFRESH_MIN_DELAY_MS = 5_000;
/** 当 bootstrap 响应缺失或未携带 ``max_message_bytes`` 时的回退值。
 * 与后端 ``WebSocketConfig.max_message_bytes`` 默认值保持一致(36 MiB),
 * 保证 Composer 在拿到 bootstrap 响应前/后行为一致。 */
const DEFAULT_MAX_MESSAGE_BYTES = 37_748_736;
// ShellView 包含 "chat" + VIEW_REGISTRY 中所有已注册视图的 key
// 新增视图时只需在 registry 加一项，此类型自动同步
type ShellView = "chat" | (typeof VIEW_REGISTRY)[number]["key"];

function bootstrapTokenExpiresAt(expiresInSeconds: number): number {
  return Date.now() + Math.max(0, expiresInSeconds) * 1000;
}

function tokenRefreshDelayMs(expiresAt: number): number {
  const remaining = Math.max(0, expiresAt - Date.now());
  const margin = Math.min(
    TOKEN_REFRESH_MARGIN_MS,
    Math.max(1_000, remaining / 2),
  );
  return Math.max(TOKEN_REFRESH_MIN_DELAY_MS, remaining - margin);
}

function AuthForm({
  failed,
  onSecret,
}: {
  failed: boolean;
  onSecret: (secret: string) => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const secret = value.trim();
    if (!secret) return;
    setSubmitting(true);
    onSecret(secret);
  };

  return (
    <div className="flex h-full w-full items-center justify-center px-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4"
      >
        <div className="flex flex-col items-center gap-1 text-center">
          <p className="text-lg font-semibold">{t("app.auth.title")}</p>
          <p className="text-sm text-muted-foreground">{t("app.auth.hint")}</p>
        </div>
        {failed && (
          <p className="text-center text-sm text-destructive">
            {t("app.auth.invalid")}
          </p>
        )}
        <Input
          type="password"
          placeholder={t("app.auth.placeholder")}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={submitting}
          autoFocus
        />
        <Button
          type="submit"
          className="w-full"
          disabled={!value.trim() || submitting}
        >
          {t("app.auth.submit")}
        </Button>
      </form>
    </div>
  );
}

function readSidebarOpen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (raw === null) return true;
    return raw === "1";
  } catch {
    return true;
  }
}

function HostChrome({
  mode,
  onToggleTheme,
  onToggleLanguage,
  showThemeButton = true,
}: {
  mode: ThemeMode;
  onToggleTheme: () => void;
  onToggleLanguage: () => void;
  showThemeButton?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const isEn = (i18n.resolvedLanguage ?? i18n.language) === "en";

  return (
    <header className="host-drag-region pointer-events-none absolute inset-x-0 top-0 z-40 flex h-11 items-start justify-between bg-transparent px-3 pt-2 text-foreground/90">
      <div className="flex min-w-[8rem] items-center" />
      <div className="flex items-center -space-x-1">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t("thread.header.toggleLanguage")}
          onClick={onToggleLanguage}
          className="host-no-drag pointer-events-auto h-8 w-8 rounded-full hover:bg-accent/40 hover:text-foreground"
        >
          <span className="flex items-baseline gap-[1px] text-[10px] leading-none tracking-tight">
            <span className={cn(
              "font-semibold text-foreground",
              !isEn && "font-normal text-muted-foreground/45",
            )}>A</span>
            <span className={cn(
              "font-semibold text-foreground",
              isEn && "font-normal text-muted-foreground/45",
            )}>文</span>
          </span>
        </Button>
        {showThemeButton ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t("thread.header.toggleTheme")}
            onClick={onToggleTheme}
            className="host-no-drag pointer-events-auto h-8 w-8 rounded-full text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          >
            {mode === "light" ? (
              <Sun className="h-4 w-4" />
            ) : mode === "dark" ? (
              <Moon className="h-4 w-4" />
            ) : (
              <Monitor className="h-4 w-4" />
            )}
          </Button>
        ) : (
          <div aria-hidden className="h-8 w-8" />
        )}
      </div>
    </header>
  );
}

export default function App() {
  const { t } = useTranslation();
  const [state, setState] = useState<BootState>({ status: "loading" });
  const bootstrapSecretRef = useRef("");

  const bootstrapWithSecret = useCallback(
    (secret: string) => {
      let cancelled = false;
      // AbortController lets us cancel in-flight retry loops when the
      // component unmounts or a new bootstrap is initiated.
      const abortController = new AbortController();
      (async () => {
        setState({ status: "loading" });
        try {
          // Initial bootstrap uses capped exponential backoff for transient
          // errors (network/5xx). 401/403 immediately transitions to auth.
          const boot = await fetchBootstrapWithRetry("", secret, {
            signal: abortController.signal,
          });
          if (cancelled) return;
          if (secret) saveSecret(secret);
          const url = deriveWsUrl(boot.ws_path, boot.token, boot.ws_url);
          const runtimeSurface = toRuntimeSurface(boot.runtime_surface);
          const runtimeHost = createRuntimeHost(runtimeSurface, boot.runtime_capabilities);
          const client = new ErzaClient({
            url,
            socketFactory: runtimeHost.socketFactory,
            onReauth: async () => {
              try {
                // Reauth retries transient errors too. A 401/403 here
                // means the saved secret is no longer valid — we close
                // the old client and transition to auth.
                const refreshed = await fetchBootstrapWithRetry(
                  "",
                  bootstrapSecretRef.current,
                );
                const refreshedUrl = deriveWsUrl(
                  refreshed.ws_path,
                  refreshed.token,
                  refreshed.ws_url,
                );
                const tokenExpiresAt = bootstrapTokenExpiresAt(refreshed.expires_in);
                setState((current) =>
                  current.status === "ready" && current.client === client
                    ? {
                        ...current,
                        token: refreshed.token,
                        tokenExpiresAt,
                        modelName: refreshed.model_name ?? current.modelName,
                        runtimeSurface:
                          refreshed.runtime_surface
                            ? toRuntimeSurface(refreshed.runtime_surface)
                            : current.runtimeSurface,
                        version: refreshed.version ?? current.version,
                        maxMessageBytes:
                          typeof refreshed.max_message_bytes === "number" &&
                          refreshed.max_message_bytes > 0
                            ? refreshed.max_message_bytes
                            : current.maxMessageBytes,
                      }
                    : current,
                );
                return refreshedUrl;
              } catch (e) {
                if (e instanceof BootstrapError && e.isAuth) {
                  // Saved secret no longer valid: close the old client
                  // (cancels reconnection and pending reconnect timers)
                  // and transition to auth so the user can re-enter it.
                  client.close();
                  setState({ status: "auth", failed: true });
                }
                // Transient errors that exhausted retries: return null so
                // the WS client stays disconnected; the proactive refresh
                // timer will keep trying in the background.
                return null;
              }
            },
          });
          bootstrapSecretRef.current = secret;
          client.connect();
          setState({
            status: "ready",
            client,
            token: boot.token,
            tokenExpiresAt: bootstrapTokenExpiresAt(boot.expires_in),
            modelName: boot.model_name ?? null,
            runtimeSurface,
            version: boot.version ?? null,
            maxMessageBytes:
              typeof boot.max_message_bytes === "number" &&
              boot.max_message_bytes > 0
                ? boot.max_message_bytes
                : DEFAULT_MAX_MESSAGE_BYTES,
          });
        } catch (e) {
          if (cancelled) return;
          if (e instanceof BootstrapError && e.isAuth) {
            setState({ status: "auth", failed: true });
          } else {
            setState({ status: "error", message: (e as Error).message });
          }
        }
      })();
      return () => {
        cancelled = true;
        abortController.abort();
      };
    },
    [],
  );

  const readyClient = state.status === "ready" ? state.client : null;
  const readyTokenExpiresAt = state.status === "ready" ? state.tokenExpiresAt : null;

  const handleModelNameChange = useCallback((modelName: string | null) => {
    setState((current) =>
      current.status === "ready" ? { ...current, modelName } : current,
    );
  }, []);

  useEffect(() => {
    if (state.status !== "ready") return;
    const client = state.client;
    // AbortController cancels the in-flight retry loop if the component
    // unmounts or the token expiry changes (re-scheduling this effect).
    const abortController = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        // Proactive refresh retries transient errors. 401/403 closes the
        // old client and transitions to auth.
        const boot = await fetchBootstrapWithRetry(
          "",
          bootstrapSecretRef.current,
          { signal: abortController.signal },
        );
        if (abortController.signal.aborted) return;
        const url = deriveWsUrl(boot.ws_path, boot.token, boot.ws_url);
        const tokenExpiresAt = bootstrapTokenExpiresAt(boot.expires_in);
        client.updateUrl(url);
        setState((current) =>
          current.status === "ready" && current.client === client
            ? {
                ...current,
                token: boot.token,
                tokenExpiresAt,
                modelName: boot.model_name ?? current.modelName,
                runtimeSurface: boot.runtime_surface
                  ? toRuntimeSurface(boot.runtime_surface)
                  : current.runtimeSurface,
                version: boot.version ?? current.version,
                maxMessageBytes:
                  typeof boot.max_message_bytes === "number" &&
                  boot.max_message_bytes > 0
                    ? boot.max_message_bytes
                    : current.maxMessageBytes,
              }
            : current,
        );
      } catch (e) {
        if (abortController.signal.aborted) return;
        if (e instanceof BootstrapError && e.isAuth) {
          // Saved secret no longer valid: close the old client (cancels
          // reconnection and pending reconnect timers) and the old refresh
          // timer (this effect's cleanup), then transition to auth.
          client.close();
          setState({ status: "auth", failed: true });
        }
        // Transient errors that exhausted retries: leave the current state
        // alone. The effect will re-run on the next state change and
        // schedule another refresh attempt.
      }
    }, tokenRefreshDelayMs(state.tokenExpiresAt));
    return () => {
      window.clearTimeout(timer);
      abortController.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- readyClient/readyTokenExpiresAt are extracted from state union to avoid TS narrowing errors
  }, [state.status, readyClient, readyTokenExpiresAt]);

  useEffect(() => {
    const saved = loadSavedSecret();
    return bootstrapWithSecret(saved);
  }, [bootstrapWithSecret]);

  if (state.status === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 animate-in fade-in-0 duration-300">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/40" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/60" />
            </span>
            {t("app.loading.connecting")}
          </div>
        </div>
      </div>
    );
  }
  if (state.status === "auth") {
    return (
      <AuthForm
        failed={!!state.failed}
        onSecret={(s) => bootstrapWithSecret(s)}
      />
    );
  }
  if (state.status === "error") {
    return (
      <div className="flex h-full w-full items-center justify-center px-4 text-center">
        <div className="flex max-w-md flex-col items-center gap-3">
          <p className="text-lg font-semibold">{t("app.error.title")}</p>
          <p className="text-sm text-muted-foreground">{state.message}</p>
          <p className="text-xs text-muted-foreground">
            {t("app.error.gatewayHint")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <ErrorBoundary>
      <ClientProvider
        client={state.client}
        token={state.token}
        modelName={state.modelName}
      >
        <Shell
          runtimeSurface={state.runtimeSurface}
          version={state.version}
          maxMessageBytes={state.maxMessageBytes}
          onModelNameChange={handleModelNameChange}
        />
      </ClientProvider>
    </ErrorBoundary>
  );
}

function Shell({
  runtimeSurface,
  version,
  maxMessageBytes,
  onModelNameChange,
}: {
  runtimeSurface: RuntimeSurface;
  version: string | null;
  maxMessageBytes: number;
  onModelNameChange: (modelName: string | null) => void;
}) {
  const { t, i18n } = useTranslation();
  const { client, token, modelName } = useClient();
  const { theme, mode, toggle, setMode } = useTheme();

  const toggleLanguage = useCallback(() => {
    const current = i18n.resolvedLanguage ?? i18n.language;
    const codes = supportedLocales.map((l) => l.code);
    const idx = codes.indexOf(current as SupportedLocale);
    const next = codes[(idx + 1) % codes.length] ?? codes[0];
    void i18n.changeLanguage(next);
    persistLocale(next as SupportedLocale);
    applyDocumentLocale(next as SupportedLocale);
  }, [i18n]);
  const { sessions, loading, refresh, createChat, deleteChat } = useSessions();
  const { state: sidebarState, update: updateSidebarState } =
    useSidebarState(sessions, !loading);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [view, setView] = useState<ShellView>("chat");
  const [searchOpen, setSearchOpen] = useState(false);
  const [settingsInitialSection, setSettingsInitialSection] = useState<SettingsSectionKey>("overview");
  const [hostSidebarOpen, setHostSidebarOpen] =
    useState<boolean>(readSidebarOpen);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  /* 视口 < lg(1024px) 时侧边栏常驻为图标栏(rail),不再彻底隐藏;
   * 完整列表仍通过抽屉(Sheet)访问。 */
  const [isNarrowViewport, setIsNarrowViewport] = useState(() =>
    typeof window !== "undefined"
      ? !window.matchMedia("(min-width: 1024px)").matches
      : false,
  );
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => setIsNarrowViewport(!e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  const [settingsSnapshot, setSettingsSnapshot] = useState<SettingsPayload | null>(null);
  /** Currently selected subagent id (routes outbound turns to that agent). */
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) setSettingsSnapshot(payload);
      })
      .catch(() => {
        if (!cancelled) setSettingsSnapshot(null);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // 当前激活的 provider(从 settings + modelName 派生),供 TopBar 的 provider 切换器使用。
  const currentProvider = useMemo(
    () => resolvedModelProvider(settingsSnapshot, modelName),
    [settingsSnapshot, modelName],
  );

  // 用户在 TopBar 切换 provider。
  // 后端 model_preset/provider/model 是三个独立字段,但运行时 resolve_preset()
  // 在 model_preset 指向命名 preset 时完全使用该 preset 的 provider/model,
  // 忽略 defaults.provider。因此切换策略:
  //  1. 目标 provider 下有命名 preset → 切到第一个 preset(preset 自带 provider/model/凭证)
  //  2. 目标 provider 下无命名 preset → 切回 default preset 并设置 provider
  //     (只有 model_preset=default/None 时 defaults.provider 才会生效)
  const handleSelectProvider = useCallback(
    async (provider: string) => {
      if (!token || provider === currentProvider) return;
      try {
        const targetRow = settingsSnapshot?.providers?.find((p) => p.name === provider);
        // 虚拟 preset row(如 custom__xxx):直接切到对应 preset(preset 自带 provider/model/凭证)
        if (targetRow?.preset_name) {
          const next = await updateSettings(token, { modelPreset: targetRow.preset_name });
          setSettingsSnapshot(next);
          return;
        }
        // 常规 provider:
        //  1. 目标 provider 下有命名 preset → 切到第一个 preset
        //  2. 目标 provider 下无命名 preset → 切回 default preset 并设置 provider
        const providerPresets = targetRow?.presets ?? [];
        let next: SettingsPayload;
        if (providerPresets.length > 0) {
          next = await updateSettings(token, { modelPreset: providerPresets[0].name });
        } else {
          next = await updateSettings(token, { modelPreset: "default", provider });
        }
        setSettingsSnapshot(next);
      } catch (err) {
        console.error("[App] switch provider failed", err);
      }
    },
    [token, currentProvider, settingsSnapshot],
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SIDEBAR_STORAGE_KEY,
        hostSidebarOpen ? "1" : "0",
      );
    } catch {
      // ignore storage errors (private mode, etc.)
    }
  }, [hostSidebarOpen]);

  const activeSession = useMemo<ChatSummary | null>(() => {
    if (!activeKey) return null;
    return sessions.find((s) => s.key === activeKey) ?? null;
  }, [sessions, activeKey]);
  const activeChatId = activeSession?.chatId ?? null;

  // 顺序很重要:useChatRunStatus 派生 activeChatRunning,useWorkspaceScope 依赖它。
  const {
    runningChatIdList,
    completedChatIdList,
    activeChatRunning,
    clearCompleted,
  } = useChatRunStatus({ client, sessions, loading, activeChatId });
  const {
    workspaces,
    workspaceError,
    setDraftWorkspaceScope,
    setWorkspaceError,
    setWorkspaceOverrides,
    activeWorkspaceScope,
    applyWorkspaceScope,
  } = useWorkspaceScope({
    token,
    client,
    sessions,
    loading,
    activeSession,
    activeChatId,
    activeChatRunning,
  });
  const { isRestarting, restartToast, onRestart } = useRestartFlow({
    client,
    activeChatId: activeSession?.chatId ?? client.defaultChatId,
  });
  const {
    pendingDelete,
    pendingRename,
    pendingProjectRename,
    requestDelete,
    requestRename,
    requestProjectRename,
    cancelDelete,
    cancelRename,
    cancelProjectRename,
  } = useDeleteRenameDialog();

  const closeHostSidebar = useCallback(() => {
    setHostSidebarOpen(false);
  }, []);

  const openHostSidebar = useCallback(() => {
    setHostSidebarOpen(true);
  }, []);

  const closeMobileSidebar = useCallback(() => {
    setMobileSidebarOpen(false);
  }, []);

  const toggleSidebar = useCallback(() => {
    const isNativeHost =
      typeof window !== "undefined" &&
      window.matchMedia("(min-width: 1024px)").matches;
    if (isNativeHost) {
      setHostSidebarOpen((v) => !v);
    } else {
      setMobileSidebarOpen((v) => !v);
    }
  }, []);

  // 图标栏顶部展开键分流:桌面端展开侧边栏,窄屏下开抽屉(抽屉入口原来在顶栏切换键上,
  // 切换键搬进侧边栏后由这里承接)。
  const expandSidebar = useCallback(() => {
    if (isNarrowViewport) {
      setMobileSidebarOpen(true);
    } else {
      openHostSidebar();
    }
  }, [isNarrowViewport, openHostSidebar]);

  const onCreateChat = useCallback(async (workspaceScope?: WorkspaceScopePayload | null) => {
    try {
      const scope = workspaceScope ?? activeWorkspaceScope;
      const chatId = await createChat(scope);
      setActiveKey(`websocket:${chatId}`);
      setView("chat");
      setMobileSidebarOpen(false);
      if (scope) {
        setWorkspaceOverrides((current) => ({
          ...current,
          [chatId]: normalizeWorkspaceScope(scope),
        }));
      }
      return chatId;
    } catch (e) {
      console.error("Failed to create chat", e);
      if (e instanceof Error && e.message.startsWith("workspace_scope_rejected:")) {
        setWorkspaceError(t("errors.workspaceScopeRejected.body"));
      }
      return null;
    }
  }, [activeWorkspaceScope, createChat, t]);

  const onNewChat = useCallback(() => {
    setActiveKey(null);
    setDraftWorkspaceScope(null);
    setWorkspaceError(null);
    setView("chat");
    setMobileSidebarOpen(false);
  }, []);

  const onNewChatInProject = useCallback(
    (projectPath: string, projectName: string) => {
      const base = workspaces?.default_scope ?? activeWorkspaceScope;
      const trimmed = projectPath.trim();
      if (!base || !trimmed) {
        onNewChat();
        return;
      }
      setActiveKey(null);
      setDraftWorkspaceScope(normalizeWorkspaceScope({
        project_path: trimmed,
        project_name: projectName || projectNameFromPath(trimmed),
        access_mode: base.access_mode,
        restrict_to_workspace: base.access_mode === "restricted",
      }));
      setWorkspaceError(null);
      setView("chat");
      setMobileSidebarOpen(false);
    },
    [activeWorkspaceScope, onNewChat, workspaces?.default_scope],
  );

  const onSelectChat = useCallback(
    (key: string) => {
      const selected = sessions.find((session) => session.key === key);
      const selectedChatId = selected?.chatId;
      if (selectedChatId) {
        clearCompleted(selectedChatId);
      }
      if (selected?.workspaceScope) {
        setDraftWorkspaceScope(normalizeWorkspaceScope(selected.workspaceScope));
      } else {
        setDraftWorkspaceScope(null);
      }
      setWorkspaceError(null);
      setActiveKey(key);
      setView("chat");
      setMobileSidebarOpen(false);
    },
    [clearCompleted, sessions],
  );

  const {
    onTogglePin,
    onConfirmRename,
    onToggleGroup,
    onConfirmProjectRename,
    onToggleArchive,
    onToggleArchived,
  } = useSidebarActions({
    sidebarState,
    updateSidebarState,
    activeKey,
    sessions,
    setActiveKey,
    pendingRename,
    pendingProjectRename,
    cancelRename,
    cancelProjectRename,
  });

  const openView = useCallback((name: ShellView) => {
    setView(name);
    setMobileSidebarOpen(false);
  }, []);

  // Sidebar 声明式导航：接收 registry item key（string），转交 openView
  const onNavigate = useCallback((key: string) => {
    openView(key as ShellView);
  }, [openView]);

  // Sidebar 顶部按钮区导航项：排除 settings（settings 在底部独立渲染）
  const sidebarNavItems = useMemo(
    () => getSidebarNavItems().filter((v) => v.key !== "settings"),
    [],
  );

  const onSelectAgent = useCallback((agentId: string) => {
    setSelectedAgentId(agentId);
  }, []);

  const onClearAgent = useCallback(() => {
    setSelectedAgentId(null);
  }, []);

  /** Called from AgentsView to start a chat with a specific subagent. */
  const onUseAgent = useCallback((agentId: string) => {
    setSelectedAgentId(agentId);
    setView("chat");
    setMobileSidebarOpen(false);
  }, []);

  const onOpenSettings = useCallback((section: SettingsSectionKey = "overview") => {
    setSettingsInitialSection(section);
    setView("settings");
    setMobileSidebarOpen(false);
  }, []);

  const onBackToChat = useCallback(() => {
    setView("chat");
    setMobileSidebarOpen(false);
    setActiveKey((current) => {
      if (!current) return null;
      if (sessions.some((session) => session.key === current)) return current;
      return sessions[0]?.key ?? null;
    });
  }, [sessions]);

  /** Cmd/Ctrl+K 打开会话搜索;Esc 关闭由 Dialog 自身处理。 */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  /** SearchDialog 选中会话:关闭弹窗 + 切到对应 chat。 */
  const onSelectFromSearch = useCallback((key: string) => {
    setSearchOpen(false);
    onSelectChat(key);
  }, [onSelectChat]);

  useEffect(() => {
    return client.onRuntimeModelUpdate((modelName) => {
      onModelNameChange(modelName);
      // 模型变化通常伴随 provider/preset 切换,同步刷新 settings 以更新 TopBar 的 provider 切换器。
      fetchSettings(token)
        .then(setSettingsSnapshot)
        .catch(() => {
          // 忽略:settings 会在下次 token 变化时重新拉取。
        });
    });
  }, [client, onModelNameChange, token]);

  const onTurnEnd = useDeferredTitleRefresh(activeSession, refresh);

  const onConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const key = pendingDelete.key;
    const deletingActive = activeKey === key;
    const currentIndex = sessions.findIndex((s) => s.key === key);
    const fallbackKey = deletingActive
      ? (sessions[currentIndex + 1]?.key ?? sessions[currentIndex - 1]?.key ?? null)
      : activeKey;
    cancelDelete();
    if (deletingActive) setActiveKey(fallbackKey);
    try {
      await deleteChat(key);
    } catch (e) {
      if (deletingActive) setActiveKey(key);
      console.error("Failed to delete session", e);
    }
  }, [cancelDelete, pendingDelete, deleteChat, activeKey, sessions]);

  const headerTitle = activeSession
    ? sidebarState.title_overrides[activeSession.key] ||
      activeSession.title ||
      deriveTitle(activeSession.preview, t("chat.newChat"))
    : t("app.brand");

  useEffect(() => {
    if (view === "settings") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.sidebar.title"),
      });
      return;
    }
    document.title = activeSession
      ? t("app.documentTitle.chat", { title: headerTitle })
      : t("app.documentTitle.base");
  }, [activeSession, headerTitle, i18n.resolvedLanguage, t, view]);

  const sidebarProps = useMemo(() => ({
    sessions,
    activeKey,
    loading,
    onNewChat,
    onSelect: onSelectChat,
    onRequestDelete: requestDelete,
    onTogglePin,
    onRequestRename: requestRename,
    onToggleArchive,
    onToggleGroup,
    onRequestRenameProject: requestProjectRename,
    onNewChatInProject,
    navItems: sidebarNavItems,
    onNavigate,
    onOpenSettings: () => onOpenSettings(),
    onOpenSearch: () => setSearchOpen(true),
    onToggleArchived,
    pinnedKeys: sidebarState.pinned_keys,
    archivedKeys: sidebarState.archived_keys,
    titleOverrides: sidebarState.title_overrides,
    projectNameOverrides: sidebarState.project_name_overrides,
    collapsedGroups: sidebarState.collapsed_groups,
    runningChatIds: runningChatIdList,
    completedChatIds: completedChatIdList,
    viewState: sidebarState.view,
    showArchived: sidebarState.view.show_archived,
    archivedCount: sidebarState.archived_keys.length,
    defaultWorkspacePath: workspaces?.default_scope.project_path ?? null,
  }), [
    sessions,
    activeKey,
    loading,
    onNewChat,
    onSelectChat,
    requestDelete,
    onTogglePin,
    requestRename,
    onToggleArchive,
    onToggleGroup,
    requestProjectRename,
    onNewChatInProject,
    sidebarNavItems,
    onNavigate,
    onOpenSettings,
    onToggleArchived,
    sidebarState.pinned_keys,
    sidebarState.archived_keys,
    sidebarState.title_overrides,
    sidebarState.project_name_overrides,
    sidebarState.collapsed_groups,
    sidebarState.view,
    runningChatIdList,
    completedChatIdList,
    workspaces?.default_scope?.project_path,
  ]);
  const effectiveRuntimeSurface =
    settingsSnapshot?.surface ?? settingsSnapshot?.runtime_surface ?? runtimeSurface;
  const isNativeHostSetupSurface = effectiveRuntimeSurface === "native";
  const showHostChrome = isNativeHostSetupSurface;
  // 视图弹窗化后侧边栏常驻显示,不再为 settings 等视图让位。
  // 壁纸渲染在整行(侧边栏+主区)背后:浮动侧边栏的留白与主区共用同一张背景,
  // 不再断开。可见性由 ThreadShell 发布;视图弹窗化后主页常驻,不再按视图门控,
  // 打开设置等弹窗时背景保持不动。
  const { wallpaper } = useWallpaper();
  const chatWallpaperVisible = useWallpaperVisible();
  const appWallpaperVisible = chatWallpaperVisible;
  // 浮动侧边栏毛玻璃:与输入框同款强度,强度 0 即关闭,仅壁纸可见时生效。
  const sidebarGlass = appWallpaperVisible && isGlassActive(wallpaper);
  // 视图弹窗毛玻璃:与侧边栏同条件,保证三处质感一致。
  const modalGlass = appWallpaperVisible && isGlassActive(wallpaper);

  return (
    <ThemeProvider theme={theme}>
      <div
        className={cn(
          "relative flex h-full w-full flex-col overflow-hidden",
          showHostChrome && "bg-sidebar",
        )}
      >
        {/* 整列背景壁纸:顶栏 + 整行(侧边栏+主区)共用同一张,浮动元素都浮于其上 */}
        <WallpaperBackground wallpaper={wallpaper} visible={appWallpaperVisible} />
        {showHostChrome ? (
          <HostChrome
            mode={mode}
            onToggleTheme={toggle}
            onToggleLanguage={toggleLanguage}
            showThemeButton={view !== "chat"}
          />
        ) : (
          /* web 模式全局固定顶栏:跨整个窗口宽度,独立于 sidebar + main 的 flex 容器。
           * 展开时左侧宽度跟随侧边栏(272px);折叠时 logo/版本号保留,宽度按内容自适应。 */
          <TopBar
            title={view === "chat" ? headerTitle : null}
            showTitle={view === "chat" && !!activeSession}
            theme={theme}
            themeMode={mode}
            onToggleTheme={toggle}
            onToggleLanguage={toggleLanguage}
            providers={settingsSnapshot?.providers}
            currentProvider={currentProvider}
            onSelectProvider={handleSelectProvider}
            sidebarWidth={
              isNarrowViewport ? SIDEBAR_RAIL_WIDTH : SIDEBAR_WIDTH
            }
            sidebarCollapsed={!isNarrowViewport && !hostSidebarOpen}
            /* logo/版本号/更新按钮不受折叠影响,窄视口也一并显示。 */
            version={version}
            glass={sidebarGlass}
            glassOpacity={wallpaper.glassOpacity}
          />
        )}
        <div
          className={cn(
            "relative flex min-h-0 flex-1 w-full overflow-hidden",
            sidebarGlass && "sidebar-glass",
          )}
          style={
            sidebarGlass
              ? ({
                  "--wallpaper-glass-blur": `${WALLPAPER_GLASS_BLUR_PX}px`,
                  "--wallpaper-glass-opacity": `${wallpaper.glassOpacity}`,
                } as CSSProperties)
              : undefined
          }
        >
          {/* 整行背景:壁纸已上移至整列,此处仅保留毛玻璃变量(侧边栏用) */}
          {/* Host sidebar: in normal flow, so the thread area width stays honest.
           * 窄视口(< lg)不再 hidden,而是常驻 56px 图标栏;完整会话列表走 Sheet 抽屉。
           * 视图弹窗化后侧边栏常驻,不再为 settings 等视图让位。 */}
          <aside
              className={cn(
                "relative z-20 shrink-0 overflow-hidden",
                "transition-[width] duration-300 ease-out",
              )}
              style={{
                width: isNarrowViewport
                  ? SIDEBAR_RAIL_WIDTH
                  : hostSidebarOpen
                    ? SIDEBAR_WIDTH
                    : SIDEBAR_RAIL_WIDTH,
              }}
            >
               {/* 浮动式侧边栏:内层卡片留白+圆角+边框+阴影,悬浮于背景之上。
                * aside 本体保持原宽度参与布局,过渡动画不变。
                * 折叠态外层仅 56px,若仍 p-4 会把卡片压到 24px 导致图标全被裁掉,
                * 因此折叠时横向收到 px-1.5;纵向保持 py-4,卡片高度与展开态一致。 */}
              <div
                className={cn(
                  "absolute inset-0 transition-[padding] duration-300 ease-out",
                  (isNarrowViewport || !hostSidebarOpen) ? "px-1.5 py-4" : "p-4",
                )}
              >
                <div
                  data-sidebar-card
                  className={cn(
                    "h-full w-full overflow-hidden rounded-2xl border",
                    sidebarGlass
                      ? "border-border/40"
                      : "border-border/50",
                    "shadow-[0_4px_16px_rgba(15,23,42,0.05)] dark:shadow-[0_4px_16px_rgba(0,0,0,0.25)]",
                  )}
                  style={
                    sidebarGlass
                      ? {
                          backgroundColor:
                            "hsl(var(--sidebar) / var(--wallpaper-glass-opacity, 0.65))",
                          backdropFilter: `blur(${WALLPAPER_GLASS_BLUR_PX}px) saturate(1.4)`,
                          WebkitBackdropFilter: `blur(${WALLPAPER_GLASS_BLUR_PX}px) saturate(1.4)`,
                        }
                      : undefined
                  }
                >
                  <Sidebar
                    {...sidebarProps}
                    collapsed={isNarrowViewport ? true : !hostSidebarOpen}
                    hostChromeInset={showHostChrome}
                    transparent={sidebarGlass}
                    onCollapse={closeHostSidebar}
                    onExpand={expandSidebar}
                  />
                </div>
              </div>
            </aside>

          <Sheet
            open={mobileSidebarOpen}
            onOpenChange={(open) => setMobileSidebarOpen(open)}
          >
              <SheetContent
                side="left"
                showCloseButton={false}
                aria-describedby={undefined}
                className="p-0 lg:hidden"
                style={{ width: SIDEBAR_WIDTH, maxWidth: SIDEBAR_WIDTH }}
              >
                <SheetTitle className="sr-only">{t("sidebar.navigation")}</SheetTitle>
                <Sidebar
                  {...sidebarProps}
                  onCollapse={closeMobileSidebar}
                  containActionMenus
                />
              </SheetContent>
            </Sheet>

          <main
            className={cn(
              "relative z-10 flex h-full min-w-0 flex-1 flex-col overflow-hidden",
              // 壁纸显示时主区透明,露出整行背后同一张壁纸;否则保持实底色
              appWallpaperVisible ? "bg-transparent" : "bg-background",
              showHostChrome &&
                "rounded-l-[28px] shadow-[-18px_0_32px_-30px_rgb(0_0_0/0.45)] dark:shadow-[-18px_0_32px_-30px_rgb(0_0_0/0.85)]",
            )}
          >
            {/*
              ThreadShell 常驻挂载、不再随视图切换卸载或隐藏:
              各视图(settings/mcp/skills 等)改走毛玻璃弹窗,聊天主页一直在底下。
              常驻的原因不变:ThreadShell 持有 WebSocket 订阅与流式状态,
              卸载会断开 WS 并丢失消息列表/输入草稿;弹窗关闭后状态原地保留。
            */}
            <ThreadShell
              session={activeSession}
              onCreateChat={onCreateChat}
              onTurnEnd={onTurnEnd}
              workspaceScope={activeWorkspaceScope}
              workspaceDefaultScope={workspaces?.default_scope ?? null}
              workspaceControls={workspaces?.controls ?? null}
              workspaceScopeDisabled={activeChatRunning}
              workspaceError={workspaceError}
              onWorkspaceScopeChange={applyWorkspaceScope}
              settingsSnapshot={settingsSnapshot}
              onSettingsChange={setSettingsSnapshot}
              currentProvider={currentProvider}
              selectedAgentId={selectedAgentId}
              onSelectAgent={onSelectAgent}
              onClearAgent={onClearAgent}
              maxMessageBytes={maxMessageBytes}
            />
          </main>
          {/* 非 chat 视图改为毛玻璃弹窗:主页常驻背后,Esc/遮罩/X/视图内返回均关闭。
              与侧边栏、输入框共用同一套半透明 + 模糊(开关与强度跟随壁纸毛玻璃设置)。 */}
          {view !== "chat" && (() => {
            const reg = getView(view);
            if (!reg) return null;
            const ctx: ViewRenderContext = {
              token,
              onBack: onBackToChat,
              onUseAgent,
              themeMode: mode,
              initialSection: settingsInitialSection,
              showSidebar: view === "settings",
              onSetThemeMode: setMode,
              onModelNameChange,
              onSettingsChange: setSettingsSnapshot,
              onRestart,
              isRestarting,
              hostChromeInset: showHostChrome,
              sidebarCollapsed: !hostSidebarOpen,
              onToggleSidebar: toggleSidebar,
            };
            const content = (
              <Suspense fallback={null}>
                {reg.render(ctx)}
              </Suspense>
            );
            const body = reg.showBoundary === false ? (
              content
            ) : (
              <ErrorBoundary key={reg.key}>{content}</ErrorBoundary>
            );
            return (
              <Dialog open onOpenChange={(open) => { if (!open) setView("chat"); }}>
                <DialogContent
                  className={cn(
                    "flex h-[min(82vh,54rem)] w-[min(60rem,calc(100vw-2rem))] max-w-none flex-col gap-0 overflow-hidden rounded-[24px] border-border/50 p-0 shadow-2xl",
                    modalGlass ? "bg-transparent" : "bg-background",
                  )}
                  style={
                    modalGlass
                      ? {
                          backgroundColor: `hsl(var(--background) / ${wallpaper.glassOpacity})`,
                          backdropFilter: `blur(${WALLPAPER_GLASS_BLUR_PX}px) saturate(1.4)`,
                          WebkitBackdropFilter: `blur(${WALLPAPER_GLASS_BLUR_PX}px) saturate(1.4)`,
                        }
                      : undefined
                  }
                >
                  <DialogTitle className="sr-only">
                    {t(reg.labelKey, { defaultValue: reg.key })}
                  </DialogTitle>
                  <div className="flex min-h-0 flex-1 flex-col">
                    {body}
                  </div>
                </DialogContent>
              </Dialog>
            );
          })()}
        </div>

        <ResourceDeleteConfirmDialog
          open={!!pendingDelete}
          resourceName={pendingDelete?.label ?? ""}
          icon={Trash2}
          titleKey="deleteConfirm.title"
          descriptionKey="deleteConfirm.description"
          cancelKey="deleteConfirm.cancel"
          confirmKey="deleteConfirm.confirm"
          onCancel={cancelDelete}
          onConfirm={onConfirmDelete}
        />
        <RenameChatDialog
          open={!!pendingRename}
          title={pendingRename?.label ?? ""}
          onCancel={cancelRename}
          onConfirm={onConfirmRename}
        />
        <RenameChatDialog
          open={!!pendingProjectRename}
          title={pendingProjectRename?.label ?? ""}
          dialogTitle={t("chat.renameProjectTitle")}
          description={t("chat.renameProjectDescription")}
          placeholder={t("chat.renameProjectPlaceholder")}
          onCancel={cancelProjectRename}
          onConfirm={onConfirmProjectRename}
        />
        <SearchDialog
          open={searchOpen}
          onOpenChange={setSearchOpen}
          sessions={sessions}
          titleOverrides={sidebarState.title_overrides}
          onSelect={onSelectFromSearch}
        />
        {restartToast ? (
          <div
            role="status"
            className="fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-full border border-border/70 bg-popover px-4 py-2 text-sm font-medium text-popover-foreground shadow-lg"
          >
            {restartToast}
          </div>
        ) : null}
      </div>
    </ThemeProvider>
  );
}
