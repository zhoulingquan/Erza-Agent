import { useCallback, useEffect, useState, type ReactNode } from "react";
import { AlertTriangle, Check, ChevronDown, Folder, FolderOpen, Hand } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError, pickWorkspaceFolder } from "@/lib/api";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type {
  WorkspaceAccessMode,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import { getHostApi } from "@/lib/runtime";
import { cn } from "@/lib/utils";
import {
  projectNameFromPath,
  scopeWithAccessMode,
  selectedProjectScope,
  shortWorkspacePath,
} from "@/lib/workspace";

export function WorkspaceProjectPicker({
  isHero,
  disabled,
  scope,
  defaultScope,
  controls,
  error,
  apiToken,
  onChange,
}: {
  isHero: boolean;
  disabled?: boolean;
  scope: WorkspaceScopePayload | null;
  defaultScope: WorkspaceScopePayload | null;
  controls: WorkspacesPayload["controls"] | null;
  error?: string | null;
  apiToken?: string;
  onChange?: (scope: WorkspaceScopePayload) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [pathError, setPathError] = useState<string | null>(null);
  const [pickingFolder, setPickingFolder] = useState(false);
  const currentProjectScope = selectedProjectScope(scope, defaultScope);
  const projectLabel = currentProjectScope
    ? currentProjectScope.project_name || projectNameFromPath(currentProjectScope.project_path)
    : t("thread.composer.workspace.projectPlaceholder");
  const visible = isHero
    && !!defaultScope
    && !!onChange
    && controls?.can_change_project !== false;
  const hostApi = getHostApi();

  useEffect(() => {
    if (!open) return;
    setPathError(null);
  }, [open]);

  useEffect(() => {
    if (error && visible) setOpen(true);
  }, [error, visible]);

  const applyProjectPath = useCallback(
    (projectPath: string, projectName?: string) => {
      const base = scope ?? defaultScope;
      if (!base || !onChange) return;
      onChange({
        ...base,
        project_path: projectPath,
        project_name: projectName || projectNameFromPath(projectPath),
        restrict_to_workspace: base.access_mode === "restricted",
      });
      setPathError(null);
      setOpen(false);
    },
    [defaultScope, onChange, scope],
  );

  // 打开本地文件夹:优先原生壳桥(window.erzaHost),否则请求后端在宿主机弹
  // 系统目录选择框。后端不可用(503/非本地)时提示错误。preventDefault 让菜
  // 单在弹窗期间保持展开:取消时无需重开,失败时错误就地显示;成功则收起。
  const pickFolder = useCallback(async () => {
    if (disabled) return;
    setPickingFolder(true);
    try {
      let picked: string | null = null;
      if (hostApi?.pickFolder) {
        picked = await hostApi.pickFolder();
      } else {
        const payload = await pickWorkspaceFolder(
          apiToken ?? "",
          currentProjectScope?.project_path,
          t("workspace.dialog.pickFolderTitle"),
        );
        if (!payload.picked || !payload.path) {
          return;
        }
        picked = payload.path;
      }
      if (picked) applyProjectPath(picked);
    } catch (err) {
      setPathError(
        err instanceof ApiError && err.status === 503
          ? t("workspace.dialog.pickUnavailable")
          : t("workspace.dialog.pickFailed"),
      );
    } finally {
      setPickingFolder(false);
    }
  }, [
    apiToken,
    applyProjectPath,
    currentProjectScope?.project_path,
    disabled,
    hostApi,
    t,
  ]);

  if (!visible || !defaultScope || !onChange) return null;

  return (
    <div className="flex items-center border-t border-border/25 bg-muted/60 px-4 py-1.5 dark:bg-white/[0.055]">
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label={t("thread.composer.workspace.projectAria")}
            className={cn(
              "inline-flex h-7 max-w-[18rem] items-center gap-2 rounded-full px-2.5",
              "text-[12px] font-medium text-muted-foreground/90 transition-colors",
              "hover:bg-background/70 hover:text-foreground disabled:pointer-events-none disabled:opacity-55",
              "focus-visible:outline-none focus-visible:ring-0",
              currentProjectScope && "text-foreground/82",
            )}
          >
            <Folder className={cn("h-3.5 w-3.5 shrink-0", currentProjectScope && "text-primary")} />
            <span className="truncate">{projectLabel}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          side="bottom"
          sideOffset={8}
          className="w-[min(25rem,calc(100vw-2rem))] rounded-[22px]"
        >
          <DropdownMenuItem
            onSelect={() => applyProjectPath(defaultScope.project_path, defaultScope.project_name)}
            className="flex min-h-[48px] cursor-default gap-3 rounded-[16px] px-3 py-2.5 focus:bg-muted/55"
          >
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[12px] bg-muted text-foreground/80">
              <Folder className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-semibold text-foreground">
                {t("workspace.dialog.defaultProject")}
              </span>
              <span className="block truncate text-[11.5px] text-muted-foreground">
                {shortWorkspacePath(defaultScope.project_path)}
              </span>
            </span>
            {!currentProjectScope ? <Check className="h-4 w-4 text-foreground/80" /> : null}
          </DropdownMenuItem>
          <div className="my-1 h-px bg-border/45" />
          <DropdownMenuItem
            disabled={disabled || pickingFolder}
            onSelect={(event) => {
              event.preventDefault();
              void pickFolder();
            }}
            className="flex min-h-[40px] cursor-default gap-3 rounded-[12px] px-3 py-2 focus:bg-muted/55"
          >
            <span className="grid h-6 w-6 shrink-0 place-items-center text-muted-foreground">
              <FolderOpen className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-muted-foreground">
              {pickingFolder
                ? t("workspace.dialog.pickingFolder")
                : t("workspace.dialog.browseFolder")}
            </span>
          </DropdownMenuItem>
          {pathError || error ? (
            <p role="alert" className="px-1 text-[11.5px] font-medium text-destructive">
              {pathError ?? error}
            </p>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

export function WorkspaceAccessMenu({
  scope,
  disabled,
  canUseFullAccess,
  isHero,
  onChange,
}: {
  scope: WorkspaceScopePayload;
  disabled?: boolean;
  canUseFullAccess: boolean;
  isHero: boolean;
  onChange?: (scope: WorkspaceScopePayload) => void;
}) {
  const { t } = useTranslation();
  const mode = scope.access_mode;
  const isFull = mode === "full";
  // 切到完全访问先弹确认框(全盘读写高危),确认后才真正切换。
  const [confirmFullOpen, setConfirmFullOpen] = useState(false);

  const setMode = (value: WorkspaceAccessMode) => {
    if (value === "full" && !canUseFullAccess) return;
    if (value === mode) return;
    onChange?.(scopeWithAccessMode(scope, value));
  };

  const requestFullAccess = () => {
    if (isFull) return;
    setConfirmFullOpen(true);
  };

  const confirmFullAccess = () => {
    setConfirmFullOpen(false);
    setMode("full");
  };

  return (
    <>
      {/* modal={false}:非模态菜单不锁 body 点击,避免与确认框的锁叠加后解不开
          (删除会话确认框同款处理,见 ChatList)。 */}
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild disabled={disabled || !onChange}>
          <Button
            type="button"
            variant="ghost"
            aria-label={t("thread.composer.workspace.accessAria")}
            className={cn(
              "max-w-[12.5rem] rounded-[10px] border border-transparent font-semibold shadow-none",
              isHero ? "h-8 px-2.5 text-[12px]" : "h-9 px-3 text-[12.5px]",
              isFull
                ? "bg-transparent text-orange-600 hover:bg-orange-500/8 dark:text-orange-300 dark:hover:bg-orange-400/10"
                : "bg-transparent text-muted-foreground hover:bg-foreground/[0.045] hover:text-foreground dark:hover:bg-white/[0.06]",
            )}
          >
            {isFull ? (
              <AlertTriangle className={cn("mr-1.5 shrink-0", isHero ? "h-3.5 w-3.5" : "h-3.5 w-3.5")} />
            ) : (
              <Hand className={cn("mr-1.5 shrink-0", isHero ? "h-3.5 w-3.5" : "h-3.5 w-3.5")} />
            )}
            <span className="truncate">
              {t(isFull ? "thread.composer.workspace.full" : "thread.composer.workspace.default")}
            </span>
            <ChevronDown className={cn("ml-1.5 shrink-0", isHero ? "h-3 w-3" : "h-3 w-3")} />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <AccessMenuItem
            icon={<Hand className="h-4 w-4" />}
            label={t("thread.composer.workspace.default")}
            selected={mode === "restricted"}
            onSelect={() => setMode("restricted")}
          />
          <AccessMenuItem
            icon={<AlertTriangle className="h-4 w-4" />}
            label={t("thread.composer.workspace.full")}
            selected={mode === "full"}
            disabled={!canUseFullAccess}
            warning
            onSelect={requestFullAccess}
          />
        </DropdownMenuContent>
      </DropdownMenu>
      <AlertDialog open={confirmFullOpen} onOpenChange={(o) => (!o ? setConfirmFullOpen(false) : undefined)}>
        <AlertDialogContent className="w-[min(calc(100vw-2rem),22.75rem)] gap-0 p-5 text-center">
          <AlertDialogHeader className="items-center space-y-0 text-center">
            <div className="mb-4 grid h-12 w-12 place-items-center rounded-full bg-orange-500/10">
              <AlertTriangle className="h-4 w-4 text-orange-600 dark:text-orange-300" aria-hidden />
            </div>
            <AlertDialogTitle className="text-center text-[14px] font-medium leading-5 text-foreground">
              {t("thread.composer.workspace.fullConfirmTitle", { defaultValue: "开启完全访问权限？" })}
            </AlertDialogTitle>
            <AlertDialogDescription className="mt-2 max-w-[17rem] text-center text-[12px] leading-4 text-muted-foreground">
              {t("thread.composer.workspace.fullConfirmDesc", {
                defaultValue: "Agent 将可以读写本机全盘文件，请确认后再继续。",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="mt-5 grid grid-cols-2 gap-2.5 space-x-0">
            <AlertDialogCancel onClick={() => setConfirmFullOpen(false)}>
              {t("settings.actions.cancel", { defaultValue: "取消" })}
            </AlertDialogCancel>
            <AlertDialogAction onClick={confirmFullAccess}>
              {t("thread.composer.workspace.fullConfirmAction", { defaultValue: "确认开启" })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function AccessMenuItem({
  icon,
  label,
  selected,
  disabled,
  warning,
  onSelect,
}: {
  icon: ReactNode;
  label: string;
  selected: boolean;
  disabled?: boolean;
  warning?: boolean;
  onSelect: () => void;
}) {
  return (
    <DropdownMenuItem
      disabled={disabled}
      onSelect={onSelect}
      className={cn(
        "flex h-10 items-center gap-3 rounded-xl px-3 text-[13.5px] font-semibold",
        warning && "text-orange-600 focus:text-orange-600 dark:text-orange-300 dark:focus:text-orange-300",
      )}
    >
      <span className="grid h-5 w-5 shrink-0 place-items-center text-current" aria-hidden>
        {icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {selected ? <Check className="h-4 w-4 shrink-0" aria-hidden /> : null}
    </DropdownMenuItem>
  );
}
