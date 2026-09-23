// Context window 状态徽章 + 手动输入框:ModelsSettings section 内嵌使用。
// 从 SettingsView.tsx 拆分而来。

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

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
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { StatusPill } from "./SettingsRow";

export function ContextWindowBadge({
  resolved,
  configured,
  status,
  error,
  timeout,
  onSave,
}: {
  resolved?: number;
  configured?: number;
  status?: "configured" | "learned" | "unknown" | "failed" | "default";
  error?: string | null;
  timeout?: boolean;
  onSave: (value: number) => Promise<void>;
}) {
  const { t } = useTranslation();
  const isConfigured = typeof configured === "number" && configured > 0;
  const value = typeof resolved === "number" && resolved > 0 ? resolved : configured ?? 0;
  // 已学习状态:HF 查询成功且用户未手动配置
  const isLearned = status === "learned" && !isConfigured;

  const [inputValue, setInputValue] = useState(value ? String(value) : "");
  const [saving, setSaving] = useState(false);
  // 手动修改确认框:先弹提醒(覆盖当前值),确认后才真正保存。
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingValue, setPendingValue] = useState<number | null>(null);

  // 同步外部值变化(如切换模型后)
  useEffect(() => {
    setInputValue(value ? String(value) : "");
  }, [value]);

  // 解析输入:支持纯数字(如 32000)和带 k/m 后缀(如 32k、1m、1.5m)。
  // 返回 { num: 转换后的整数 | null, hasSuffix: 是否使用了后缀语法 }
  const parsedInput = (() => {
    const raw = inputValue.trim();
    if (!raw) return { num: null, hasSuffix: false };
    let multiplier = 1;
    let body = raw;
    const last = raw[raw.length - 1];
    if (last === "k" || last === "K") {
      multiplier = 1_000;
      body = raw.slice(0, -1);
    } else if (last === "m" || last === "M") {
      multiplier = 1_000_000;
      body = raw.slice(0, -1);
    }
    const hasSuffix = multiplier !== 1;
    const parsed = Number(body);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return { num: null, hasSuffix };
    }
    return { num: Math.round(parsed * multiplier), hasSuffix };
  })();
  const numValue = parsedInput.num;
  const isValid = numValue !== null && numValue > 0;
  // 输入与当前值不一致即视为已修改(清空也算,即用户想取消已学习值)
  const initialInputValue = value ? String(value) : "";
  const inputChanged = inputValue !== initialInputValue;
  // 当用户使用 k/m 后缀时,显示转换后的数值提示
  const suffixPreview = parsedInput.hasSuffix && numValue !== null
    ? `= ${numValue.toLocaleString()}`
    : "";
  // 已配置状态:用户手动保存了 context_window_tokens,且输入框未修改
  const isConfiguredSaved = isConfigured && !inputChanged && !timeout && !saving;

  // 按钮文本:超时 → "查询超时,请手动输入";已学习且未修改 → "已学习";
  // 已配置且未修改 → "已配置";否则 → "保存"
  const buttonLabel = saving
    ? t("settings.actions.saving")
    : timeout && !isConfigured && !inputChanged
      ? t("settings.actions.queryTimeout")
      : isLearned && !inputChanged
        ? t("settings.models.contextWindowLearned")
        : isConfiguredSaved
          ? t("settings.byok.configured")
          : t("settings.actions.save");

  // 按钮禁用:保存中,或输入无效,或(已学习/已配置且未修改)
  const buttonDisabled = saving || !isValid || ((isLearned || isConfigured) && !inputChanged);

  // 已学习/已配置且未修改(且非超时/保存中)时,直接复用 StatusPill 渲染,
  // 与 opencode 卡片"已配置"徽章使用完全相同的 DOM 结构和样式,避免 Button
  // 基础类(h-9 / justify-center / ring-offset / disabled:opacity 等)造成视觉差异。
  const showAsLearnedPill = (isLearned || isConfiguredSaved) && !inputChanged && !timeout && !saving;

  // 点保存先弹确认框,确认后才真正写入(提醒用户覆盖的是学习值/已配置值)。
  const handleSave = () => {
    if (!isValid) return;
    setPendingValue(numValue);
    setConfirmOpen(true);
  };

  const handleConfirmSave = async () => {
    if (pendingValue === null) return;
    setConfirmOpen(false);
    setSaving(true);
    try {
      await onSave(pendingValue);
    } finally {
      setSaving(false);
      setPendingValue(null);
    }
  };

  return (
    <div
      className="inline-flex items-center gap-2"
      title={timeout ? t("settings.actions.queryTimeout") : (error ?? undefined)}
    >
      <div className="relative flex items-center">
        <Input
          type="text"
          inputMode="decimal"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          placeholder={t("settings.models.contextWindowPlaceholder")}
          className={cn(
            "h-7 w-28 text-center text-[12px] tabular-nums",
            timeout && !isConfigured && "border-amber-500/60 focus-visible:border-amber-500",
            suffixPreview && "pr-2",
          )}
          disabled={saving}
        />
        {suffixPreview ? (
          <span
            className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 text-[10px] text-muted-foreground tabular-nums"
            aria-hidden
          >
            {suffixPreview}
          </span>
        ) : null}
      </div>
      {showAsLearnedPill ? (
        <StatusPill tone="success">{buttonLabel}</StatusPill>
      ) : (
        <Button
          size="sm"
          variant={timeout && !isConfigured && !inputChanged ? "outline" : "default"}
          onClick={handleSave}
          disabled={buttonDisabled}
          className={cn(
            "h-7 gap-1 px-2.5 py-1 text-[12px] font-medium rounded-full",
            timeout && !isConfigured && !inputChanged &&
              "border-amber-500/60 text-amber-700 hover:bg-amber-500/10 hover:text-amber-700 dark:text-amber-300 dark:hover:text-amber-300",
          )}
        >
          {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          {buttonLabel}
        </Button>
      )}
      <AlertDialog open={confirmOpen} onOpenChange={(o) => (!o ? setConfirmOpen(false) : undefined)}>
        <AlertDialogContent className="w-[min(calc(100vw-2rem),22.75rem)] gap-0 p-5 text-center">
          <AlertDialogHeader className="items-center space-y-0 text-center">
            <AlertDialogTitle className="text-center text-[14px] font-medium leading-5 text-foreground">
              {t("settings.models.contextWindowManualConfirmTitle", { defaultValue: "手动设置上下文窗口？" })}
            </AlertDialogTitle>
            <AlertDialogDescription className="mt-2 max-w-[17rem] text-center text-[12px] leading-4 text-muted-foreground">
              {t("settings.models.contextWindowManualConfirmDesc", {
                defaultValue: "将覆盖当前值（{{old}}），保存为 {{new}}。",
                old: value ? value.toLocaleString() : "—",
                new: pendingValue !== null ? pendingValue.toLocaleString() : "—",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="mt-5 grid grid-cols-2 gap-2.5 space-x-0">
            <AlertDialogCancel onClick={() => setConfirmOpen(false)}>
              {t("settings.actions.cancel", { defaultValue: "取消" })}
            </AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmSave}>
              {t("settings.actions.save", { defaultValue: "保存" })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
