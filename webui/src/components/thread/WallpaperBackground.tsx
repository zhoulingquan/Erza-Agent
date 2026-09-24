import { cn } from "@/lib/utils";
import type { WallpaperSettings } from "@/hooks/useWallpaper";

/** 聊天主页背景层:背景图(可模糊) + 主题色遮罩(保证可读性)。
 * pointer-events-none,内容层需用 relative z-10 浮于其上。 */
export function WallpaperBackground({
  wallpaper,
  visible,
  className,
}: {
  wallpaper: WallpaperSettings;
  visible: boolean;
  className?: string;
}) {
  if (!visible || !wallpaper.image) return null;
  return (
    <div aria-hidden className={cn("pointer-events-none absolute inset-0 overflow-hidden", className)}>
      <div
        className="absolute inset-0"
        style={{
          backgroundImage: `url("${wallpaper.image}")`,
          backgroundSize: "cover",
          backgroundPosition: "center",
          backgroundRepeat: "no-repeat",
          filter: wallpaper.blur > 0 ? `blur(${wallpaper.blur}px)` : undefined,
          // 模糊会露出边缘,放大一点盖住
          transform: wallpaper.blur > 0 ? "scale(1.06)" : undefined,
        }}
      />
      {/* 主题色遮罩:亮色用 background 白,暗色用黑,通过 CSS 变量跟随主题 */}
      <div
        className="absolute inset-0 bg-background"
        style={{ opacity: wallpaper.dim }}
      />
      {/* 底部渐隐,让 composer 区域过渡更自然 */}
      <div className="absolute inset-x-0 bottom-0 h-56 bg-gradient-to-t from-background/70 to-transparent" />
    </div>
  );
}
