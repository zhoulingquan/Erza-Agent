import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { STORAGE_KEYS } from "@/lib/storage";

export interface WallpaperSettings {
  /** 背景图:dataURL(上传)或 http(s) URL;为空表示不启用背景图 */
  image: string | null;
  /** 背景图本身的模糊半径 px(0-24) */
  blur: number;
  /** 遮罩浓度 0-0.9:保证文字可读性,跟随当前主题(亮色用白/暗色用黑) */
  dim: number;
  /** 前景内容毛玻璃:composer 输入框/悬浮条的 backdrop-blur 强度 */
  glass: boolean;
  /** 毛玻璃面板不透明度 0-1:越小越透明,0 时面板几乎全透。 */
  glassOpacity: number;
}

/** 毛玻璃固定模糊半径(px):强度滑杆已移除,只保留透明度可调。 */
export const WALLPAPER_GLASS_BLUR_PX = 16;

export const DEFAULT_WALLPAPER: WallpaperSettings = {
  image: null,
  blur: 8,
  dim: 0.5,
  glass: true,
  glassOpacity: 0.7,
};

/** 毛玻璃是否实际生效。强度滑杆移除后只看兼容开关
 * (老存档可能为 false,拖动透明度滑杆后自动置 true)。 */
export function isGlassActive(wallpaper: WallpaperSettings): boolean {
  return wallpaper.glass;
}

const STORAGE_KEY = STORAGE_KEYS.wallpaper;
const EVENT_NAME = "erza:wallpaper-change";

function clamp(n: number, min: number, max: number, fallback: number): number {
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function sanitize(raw: Partial<WallpaperSettings> | null | undefined): WallpaperSettings {
  if (!raw || typeof raw !== "object") return { ...DEFAULT_WALLPAPER };
  return {
    image: typeof raw.image === "string" && raw.image.length > 0 ? raw.image : null,
    blur: clamp(Number(raw.blur), 0, 24, DEFAULT_WALLPAPER.blur),
    dim: clamp(Number(raw.dim), 0, 0.9, DEFAULT_WALLPAPER.dim),
    glass: raw.glass !== false,
    glassOpacity: clamp(Number(raw.glassOpacity), 0, 1, DEFAULT_WALLPAPER.glassOpacity),
  };
}

function readStored(): WallpaperSettings {
  if (typeof window === "undefined") return { ...DEFAULT_WALLPAPER };
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_WALLPAPER };
    return sanitize(JSON.parse(raw) as Partial<WallpaperSettings>);
  } catch {
    return { ...DEFAULT_WALLPAPER };
  }
}

function persist(next: WallpaperSettings): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // localStorage 配额不足(大图 dataURL)时静默忽略,由调用方提示用户改用 URL 或压缩
  }
  window.dispatchEvent(new CustomEvent<WallpaperSettings>(EVENT_NAME, { detail: next }));
}

/** 上传图片压缩到 dataURL:限制最长边 1920px,JPEG 0.82,避免 localStorage 爆配额 */
export function fileToWallpaperDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      try {
        const MAX_EDGE = 1920;
        const scale = Math.min(1, MAX_EDGE / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          reject(new Error("canvas unsupported"));
          return;
        }
        // JPEG 不支持透明,先铺白底再绘制
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, w, h);
        ctx.drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL("image/jpeg", 0.82));
      } catch (e) {
        reject(e instanceof Error ? e : new Error(String(e)));
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("image decode failed"));
    };
    img.src = url;
  });
}

export function useWallpaper(): {
  wallpaper: WallpaperSettings;
  update: (patch: Partial<WallpaperSettings>) => void;
  /** 仅恢复效果滑杆(模糊/遮罩/透明度)到默认值,保留背景图片。 */
  resetEffects: () => void;
  hasImage: boolean;
} {
  const [wallpaper, setWallpaper] = useState<WallpaperSettings>(readStored);

  useEffect(() => {
    const onChange = (e: Event) => {
      const detail = (e as CustomEvent<WallpaperSettings>).detail;
      setWallpaper(detail ? sanitize(detail) : readStored());
    };
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setWallpaper(readStored());
    };
    window.addEventListener(EVENT_NAME, onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(EVENT_NAME, onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const update = useCallback((patch: Partial<WallpaperSettings>) => {
    setWallpaper((prev) => {
      const next = sanitize({ ...prev, ...patch });
      persist(next);
      return next;
    });
  }, []);

  const resetEffects = useCallback(() => {
    setWallpaper((prev) => {
      const next = sanitize({
        ...prev,
        blur: DEFAULT_WALLPAPER.blur,
        dim: DEFAULT_WALLPAPER.dim,
        glassOpacity: DEFAULT_WALLPAPER.glassOpacity,
      });
      persist(next);
      return next;
    });
  }, []);

  return { wallpaper, update, resetEffects, hasImage: !!wallpaper.image };
}

// ---------------------------------------------------------------------------
// 背景可见性信号:ThreadShell 根据"是否有图 + 仅主页/所有会话 + 是否空态"
// 计算出背景该不该显示,通过这里发布;App 拿到后在整行(侧边栏+主区)背后
// 渲染同一张壁纸,保证浮动侧边栏的留白与主区背景连为一体。
// (单独的 store 而不是 prop 传递:ThreadShell 与 App 的背景层中间隔着多层,
// 用发布/订阅最直接;settings 等非 chat 视图由 App 用 view === "chat" 门控。)
// ---------------------------------------------------------------------------
let wallpaperVisibleSnapshot = true;
const wallpaperVisibleListeners = new Set<() => void>();

export function setWallpaperVisible(visible: boolean): void {
  if (wallpaperVisibleSnapshot === visible) return;
  wallpaperVisibleSnapshot = visible;
  wallpaperVisibleListeners.forEach((listener) => listener());
}

export function useWallpaperVisible(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      wallpaperVisibleListeners.add(onChange);
      return () => {
        wallpaperVisibleListeners.delete(onChange);
      };
    },
    () => wallpaperVisibleSnapshot,
  );
}
