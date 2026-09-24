import * as React from "react";

import { cn } from "@/lib/utils";

export interface ViewShellProps {
  // 弹窗化后返回改走右上 X/遮罩/Esc,onBack 保留在 props(注册表仍传递)但不再渲染按钮。
  onBack: () => void;
  icon: React.ReactNode;
  title: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  bodyClassName?: string;
}

/** 各资源页（Skills/Tools/Mcp/Cron/Agents/Channels）通用的页面骨架：
 * 图标 + 标题 + 右侧操作区 + 可滚动主体(返回改走弹窗右上 X,不再渲染返回按钮)。
 * 根节点透明:这些视图只在 App 的毛玻璃弹窗里渲染,底色由弹窗提供。 */
export function ViewShell({
  icon,
  title,
  actions,
  children,
  bodyClassName,
}: ViewShellProps) {
  return (
    <div className="flex h-full flex-col bg-transparent">
      {/* 右侧预留 pr-12:弹窗右上 X 悬浮于此,避免与操作区按钮重叠 */}
      <header className="flex items-center gap-2 border-b py-3 pl-4 pr-12">
        <div className="flex items-center gap-2">
          {icon}
          <h1 className="text-sm font-semibold">{title}</h1>
        </div>
        <div className="ml-auto flex items-center gap-1.5">{actions}</div>
      </header>
      {/* pr-12:与 header 同步避让弹窗右上 X,滚动主体不遮关闭按钮 */}
      <div className={cn("flex-1 overflow-y-auto scrollbar-none pl-4 pr-12 py-3", bodyClassName)}>
        {children}
      </div>
    </div>
  );
}
