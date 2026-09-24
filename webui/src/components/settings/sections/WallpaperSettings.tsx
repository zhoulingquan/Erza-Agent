// 背景图片(壁纸)设置:上传/URL + 模糊/遮罩/毛玻璃/显示范围。
// browser-only,经 useWallpaper 持久化到 localStorage,主页 ThreadViewport 消费。

import { useRef, useState } from "react";
import { ImagePlus, Link2, RotateCcw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { fileToWallpaperDataUrl, useWallpaper } from "@/hooks/useWallpaper";

import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "../components/SettingsRow";

function Slider({
  value,
  min,
  max,
  step,
  onChange,
  ariaLabel,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  ariaLabel: string;
}) {
  return (
    <div className="flex items-center gap-2.5">
      <input
        type="range"
        aria-label={ariaLabel}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 w-36 cursor-pointer appearance-none rounded-full bg-muted accent-foreground sm:w-44"
      />
      <span className="w-12 shrink-0 text-right text-[12px] tabular-nums text-muted-foreground">
        {value}
      </span>
    </div>
  );
}

export function WallpaperSettings() {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { wallpaper, update, resetEffects, hasImage } = useWallpaper();
  const fileRef = useRef<HTMLInputElement>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const applyUrl = () => {
    const url = urlDraft.trim();
    if (!url) return;
    if (!/^https?:\/\//i.test(url) && !url.startsWith("data:image/")) {
      setError(tx("settings.wallpaper.invalidUrl", "请填写 http(s) 图片链接"));
      return;
    }
    setError(null);
    update({ image: url });
  };

  const onPickFile = async (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError(tx("settings.wallpaper.invalidFile", "请选择图片文件"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const dataUrl = await fileToWallpaperDataUrl(file);
      // localStorage 约 5MB 配额,压缩后仍超限则提示改用链接
      if (dataUrl.length > 4_000_000) {
        setError(tx("settings.wallpaper.tooLarge", "图片太大,建议压缩后重试或使用图片链接"));
        return;
      }
      update({ image: dataUrl });
    } catch {
      setError(tx("settings.wallpaper.decodeFailed", "图片读取失败,请换一张试试"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <SettingsSectionTitle>{tx("settings.sections.wallpaper", "主页背景")}</SettingsSectionTitle>
      <SettingsGroup>
        <SettingsRow
          title={tx("settings.wallpaper.image", "背景图片")}
          description={tx("settings.wallpaper.imageHint", "上传本地图片或粘贴图片链接,显示在聊天主页。")}
        >
          <div className="flex flex-col items-end gap-2.5">
            <div className="flex items-center gap-2">
              {hasImage ? (
                <div
                  className="h-11 w-[72px] shrink-0 overflow-hidden rounded-xl border border-border/60 bg-muted bg-cover bg-center"
                  style={{ backgroundImage: `url("${wallpaper.image}")` }}
                  aria-hidden
                />
              ) : (
                <div className="flex h-11 w-[72px] shrink-0 items-center justify-center rounded-xl border border-dashed border-border/70 bg-muted/50 text-[11px] text-muted-foreground">
                  {tx("settings.wallpaper.empty", "无")}
                </div>
              )}
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  void onPickFile(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                disabled={busy}
                onClick={() => fileRef.current?.click()}
              >
                <ImagePlus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                {busy
                  ? tx("settings.wallpaper.loading", "处理中…")
                  : tx("settings.wallpaper.upload", "上传")}
              </Button>
              {hasImage ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className="rounded-full px-2.5 text-muted-foreground"
                  onClick={() => {
                    update({ image: null });
                    setUrlDraft("");
                  }}
                  title={tx("settings.wallpaper.remove", "移除背景")}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              ) : null}
            </div>
            <div className="flex items-center gap-1.5">
              <Input
                value={urlDraft}
                onChange={(e) => setUrlDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") applyUrl();
                }}
                placeholder={tx("settings.wallpaper.urlPlaceholder", "粘贴图片链接…")}
                className="h-8 w-44 rounded-full text-[12px] sm:w-52"
              />
              <Button size="sm" variant="ghost" className="h-8 rounded-full px-2.5" onClick={applyUrl}>
                <Link2 className="h-3.5 w-3.5" aria-hidden />
              </Button>
            </div>
            {error ? <p className="text-[12px] text-destructive">{error}</p> : null}
          </div>
        </SettingsRow>

        <SettingsRow
          title={tx("settings.wallpaper.blur", "背景模糊")}
          description={tx("settings.wallpaper.blurHint", "模糊背景图,让文字更易读。")}
        >
          <Slider
            ariaLabel={tx("settings.wallpaper.blur", "背景模糊")}
            value={wallpaper.blur}
            min={0}
            max={24}
            step={1}
            onChange={(blur) => update({ blur })}
          />
        </SettingsRow>

        <SettingsRow
          title={tx("settings.wallpaper.dim", "遮罩浓度")}
          description={tx("settings.wallpaper.dimHint", "在背景上加一层主题色遮罩,数值越大文字越清晰。")}
        >
          <Slider
            ariaLabel={tx("settings.wallpaper.dim", "遮罩浓度")}
            value={Math.round(wallpaper.dim * 100)}
            min={0}
            max={90}
            step={5}
            onChange={(v) => update({ dim: v / 100 })}
          />
        </SettingsRow>

        <SettingsRow
          title={tx("settings.wallpaper.transparency", "透明度")}
          description={tx("settings.wallpaper.transparencyHint", "毛玻璃面板的不透明度,越小越透明。")}
        >
          <Slider
            ariaLabel={tx("settings.wallpaper.transparency", "透明度")}
            value={Math.round(wallpaper.glassOpacity * 100)}
            min={0}
            max={100}
            step={5}
            onChange={(v) => update({ glassOpacity: v / 100, glass: true })}
          />
        </SettingsRow>

        <div className="flex justify-end px-4 py-2.5 sm:px-5">
          <button
            type="button"
            onClick={resetEffects}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[12px] text-muted-foreground",
              "transition-colors hover:bg-muted hover:text-foreground",
            )}
          >
            <RotateCcw className="h-3 w-3" aria-hidden />
            {tx("settings.wallpaper.reset", "恢复默认")}
          </button>
        </div>
      </SettingsGroup>
    </section>
  );
}
