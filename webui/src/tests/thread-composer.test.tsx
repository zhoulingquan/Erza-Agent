import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThreadComposer } from "@/components/thread/ThreadComposer";
import type { SlashCommand } from "@/lib/types";

const COMMANDS: SlashCommand[] = [
  {
    command: "/stop",
    title: "Stop current task",
    description: "Cancel the active agent turn.",
    icon: "square",
  },
  {
    command: "/history",
    title: "Show conversation history",
    description: "Print the last N persisted messages.",
    icon: "history",
    argHint: "[n]",
  },
];

const ORIGINAL_INNER_HEIGHT = window.innerHeight;

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(window, "erzaHost");
  window.localStorage.clear();
  Object.defineProperty(window, "innerHeight", {
    value: ORIGINAL_INNER_HEIGHT,
    configurable: true,
  });
});

function rect(init: Partial<DOMRect>): DOMRect {
  const top = init.top ?? 0;
  const left = init.left ?? 0;
  const width = init.width ?? 0;
  const height = init.height ?? 0;
  return {
    x: init.x ?? left,
    y: init.y ?? top,
    top,
    left,
    width,
    height,
    right: init.right ?? left + width,
    bottom: init.bottom ?? top + height,
    toJSON: () => ({}),
  };
}

describe("ThreadComposer", () => {
  it("renders a readonly hero model composer when provided", () => {
    render(
      <ThreadComposer
        onSend={vi.fn()}
        modelLabel="deepseek-chat"
        placeholder="Ask anything..."
        variant="hero"
      />,
    );

    expect(screen.getByText("deepseek-chat")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Search" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reason" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deep research" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Voice input" })).not.toBeInTheDocument();
    expect(screen.getByTestId("composer-model-logo-deepseek")).toBeInTheDocument();
    const input = screen.getByPlaceholderText("Ask anything...");
    expect(input).toBeInTheDocument();
    expect(input.className).toContain("min-h-[60px]");
    expect(input.parentElement?.parentElement?.className).toContain("max-w-[44rem]");
  });

  it("keeps the thread composer compact while matching the hero style", () => {
    render(
      <ThreadComposer
        onSend={vi.fn()}
        modelLabel="gpt-4o"
        modelProvider="deepseek"
        modelProviderLabel="DeepSeek"
        placeholder="Type your message..."
      />,
    );

    expect(screen.getByText("gpt-4o")).toBeInTheDocument();
    const input = screen.getByPlaceholderText("Type your message...");
    expect(input.className).toContain("min-h-[42px]");
    expect(input.parentElement?.parentElement?.className).toContain("max-w-[40rem]");
    expect(input.parentElement?.parentElement?.className).toContain("rounded-[18px]");
    expect(input.parentElement?.parentElement?.className).toContain("shadow-[0_12px_30px_rgba(15,23,42,0.07)]");
    expect(screen.getByRole("button", { name: "Attach file" }).className).toContain("bg-card");
    expect(screen.getByRole("button", { name: "Send message" }).className).toContain("bg-foreground");
    expect(screen.queryByText(/Enter to send/)).not.toBeInTheDocument();
  });

  it("renders and changes workspace access mode", async () => {
    const onWorkspaceScopeChange = vi.fn();
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Type your message..."
        workspaceScope={{
          project_path: "/tmp/project",
          project_name: "project",
          access_mode: "restricted",
          restrict_to_workspace: true,
        }}
        workspaceControls={{ can_change_project: true, can_use_full_access: true }}
        onWorkspaceScopeChange={onWorkspaceScopeChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Workspace access mode" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Full Access/ }));

    // 切完全访问先弹确认框,确认后才真正切换
    expect(onWorkspaceScopeChange).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: /确认开启|Enable/ }));

    expect(onWorkspaceScopeChange).toHaveBeenCalledWith(
      expect.objectContaining({
        project_path: "/tmp/project",
        access_mode: "full",
        restrict_to_workspace: false,
      }),
    );
  });

  it("cancelling the full-access confirm keeps current scope", async () => {
    const onWorkspaceScopeChange = vi.fn();
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Type your message..."
        workspaceScope={{
          project_path: "/tmp/project",
          project_name: "project",
          access_mode: "restricted",
          restrict_to_workspace: true,
        }}
        workspaceControls={{ can_change_project: true, can_use_full_access: true }}
        onWorkspaceScopeChange={onWorkspaceScopeChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Workspace access mode" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Full Access/ }));
    fireEvent.click(await screen.findByRole("button", { name: /取消|Cancel/ }));

    expect(onWorkspaceScopeChange).not.toHaveBeenCalled();
  });

  it("keeps project selection as a compact composer dropdown", async () => {
    const onWorkspaceScopeChange = vi.fn();
    const defaultScope = {
      project_path: "/Users/test/.erza/workspace",
      project_name: "workspace",
      access_mode: "restricted" as const,
      restrict_to_workspace: true,
    };
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Ask anything..."
        variant="hero"
        workspaceScope={{
          ...defaultScope,
          access_mode: "full",
          restrict_to_workspace: false,
        }}
        workspaceDefaultScope={defaultScope}
        workspaceControls={{ can_change_project: true, can_use_full_access: true }}
        onWorkspaceScopeChange={onWorkspaceScopeChange}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Choose project" }));

    expect(await screen.findByRole("menuitem", { name: /Default workspace/ })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Open Local Folder/ })).toBeInTheDocument();
    // 已无粘贴路径的入口:打开项目文件夹与手动输入行均已移除。
    expect(screen.queryByRole("button", { name: "Use Path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Open Project Folder/ })).not.toBeInTheDocument();

    // 选回默认工作区(替代原"使用路径"的 scope 变更断言)。
    fireEvent.click(screen.getByRole("menuitem", { name: /Default workspace/ }));

    await waitFor(() =>
      expect(onWorkspaceScopeChange).toHaveBeenCalledWith(expect.objectContaining({
        project_path: "/Users/test/.erza/workspace",
        project_name: "workspace",
        access_mode: "full",
        restrict_to_workspace: false,
      })),
    );
  });

  it("uses the native folder picker for project selection on native host", async () => {
    const onWorkspaceScopeChange = vi.fn();
    const pickFolder = vi.fn().mockResolvedValue("/Users/test/native-project");
    const defaultScope = {
      project_path: "/Users/test/.erza/workspace",
      project_name: "workspace",
      access_mode: "full" as const,
      restrict_to_workspace: false,
    };
    Object.defineProperty(window, "erzaHost", {
      configurable: true,
      value: {
        getRuntimeInfo: vi.fn(),
        restartEngine: vi.fn(),
        pickFolder,
        openLogs: vi.fn(),
        exportDiagnostics: vi.fn(),
      },
    });

    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Ask anything..."
        variant="hero"
        workspaceScope={defaultScope}
        workspaceDefaultScope={defaultScope}
        workspaceControls={{ can_change_project: true, can_use_full_access: true }}
        onWorkspaceScopeChange={onWorkspaceScopeChange}
      />,
    );

    // 下拉打开后点击"打开本地文件夹"菜单项,菜单在弹窗期间保持展开。
    fireEvent.pointerDown(screen.getByRole("button", { name: "Choose project" }));
    fireEvent.click(
      await screen.findByRole("menuitem", { name: /Open Local Folder/ }),
    );

    await waitFor(() => expect(pickFolder).toHaveBeenCalled());
    expect(onWorkspaceScopeChange).toHaveBeenCalledWith(expect.objectContaining({
      project_path: "/Users/test/native-project",
      project_name: "native-project",
      access_mode: "full",
      restrict_to_workspace: false,
    }));
  });

  it("falls back to the gateway folder picker when no native host is available", async () => {
    const onWorkspaceScopeChange = vi.fn();
    const defaultScope = {
      project_path: "/Users/test/.erza/workspace",
      project_name: "workspace",
      access_mode: "full" as const,
      restrict_to_workspace: false,
    };
    const pickerResponse = { picked: true, path: "/Users/test/gateway-project" };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(pickerResponse), {
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Ask anything..."
        variant="hero"
        workspaceScope={defaultScope}
        workspaceDefaultScope={defaultScope}
        workspaceControls={{ can_change_project: true, can_use_full_access: true }}
        onWorkspaceScopeChange={onWorkspaceScopeChange}
        apiToken="token-123"
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Choose project" }));
    fireEvent.click(
      await screen.findByRole("menuitem", { name: /Open Local Folder/ }),
    );

    await waitFor(() =>
      expect(onWorkspaceScopeChange).toHaveBeenCalledWith(expect.objectContaining({
        project_path: "/Users/test/gateway-project",
        project_name: "gateway-project",
        access_mode: "full",
        restrict_to_workspace: false,
      })),
    );
    const [calledUrl] = fetchMock.mock.calls[0] as [string];
    expect(calledUrl).toContain("/api/workspaces/pick");
  });

  it("keeps the web path menu when no native host picker is available", async () => {
    const defaultScope = {
      project_path: "/Users/test/.erza/workspace",
      project_name: "workspace",
      access_mode: "full" as const,
      restrict_to_workspace: false,
    };

    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Ask anything..."
        variant="hero"
        workspaceScope={defaultScope}
        workspaceDefaultScope={defaultScope}
        workspaceControls={{ can_change_project: true, can_use_full_access: true }}
        onWorkspaceScopeChange={vi.fn()}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "Choose project" }));

    expect(await screen.findByRole("menuitem", { name: /Default workspace/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Paste path")).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Open Local Folder/ })).toBeInTheDocument();
  });

  it("shows turn run timer when runStartedAt is set", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date((1_000 + 125) * 1000));

    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Type your message..."
        runStartedAt={1000}
      />,
    );

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/Running/);
    expect(status).toHaveTextContent(/2:05/);

    vi.useRealTimers();
  });

  it("opens an upward anchored goal panel with markdown content when expand is clicked", async () => {
    const longObjective =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz0123456789GoalTail";
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Type your message..."
        goalState={{
          active: true,
          objective: longObjective,
          ui_summary: "Short summary for strip",
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show full goal" }));

    const dialog = await screen.findByRole("dialog", { name: "Goal" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Short summary for strip");
    expect(dialog).toHaveTextContent(longObjective);
  });

  it("opens a slash command palette and inserts the selected command", () => {
    const onSend = vi.fn();
    render(
      <ThreadComposer
        onSend={onSend}
        placeholder="Type your message..."
        slashCommands={COMMANDS}
      />,
    );

    const input = screen.getByLabelText("Message input");
    fireEvent.change(input, { target: { value: "/" } });

    const palette = screen.getByRole("listbox", { name: "Slash commands" });
    expect(palette).toBeInTheDocument();
    expect(palette).toHaveStyle({ maxHeight: "288px" });
    expect(screen.queryByRole("option", { name: /\/stop/i })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /\/history/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.keyDown(input, { key: "Enter" });

    expect(input).toHaveValue("/history ");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox", { name: "Slash commands" })).not.toBeInTheDocument();
  });

  it("renders slash commands as direct actions with current status", () => {
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Type your message..."
        modelLabel="deepseek-v4-pro"
        slashCommands={[
          {
            command: "/model",
            title: "Switch model preset",
            description: "Show or switch the active model preset.",
            icon: "brain",
            argHint: "[preset]",
          },
          COMMANDS[1],
        ]}
      />,
    );

    fireEvent.change(screen.getByLabelText("Message input"), {
      target: { value: "/" },
    });

    expect(screen.getByRole("option", { name: /Model deepseek-v4-pro/i })).toBeInTheDocument();
    expect(screen.getByText("Current")).toBeInTheDocument();
    expect(screen.getByText("/model [preset]")).toBeInTheDocument();
  });

  it("prioritizes stop as an immediate slash action while streaming", () => {
    const onStop = vi.fn();
    render(
      <ThreadComposer
        onSend={vi.fn()}
        onStop={onStop}
        isStreaming
        placeholder="Type your message..."
        slashCommands={[COMMANDS[1]]}
      />,
    );

    const input = screen.getByLabelText("Message input");
    fireEvent.change(input, { target: { value: "/" } });

    expect(screen.getByRole("option", { name: /Stop current task/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onStop).toHaveBeenCalledTimes(1);
    expect(input).toHaveValue("");
    expect(window.localStorage.getItem("erza.webui.slashCommandRecents")).toBeNull();
  });

  it("orders recent slash commands first for the blank slash menu", () => {
    window.localStorage.setItem("erza.webui.slashCommandRecents", JSON.stringify(["/history"]));
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Type your message..."
        slashCommands={COMMANDS}
      />,
    );

    fireEvent.change(screen.getByLabelText("Message input"), {
      target: { value: "/" },
    });

    expect(screen.getByRole("option", { name: /\/history/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("Recent")).toBeInTheDocument();
  });

  it("keeps keyboard-selected slash options visible while navigating", () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;

    try {
      render(
        <ThreadComposer
          onSend={vi.fn()}
          placeholder="Type your message..."
          slashCommands={Array.from({ length: 8 }, (_, index) => ({
            command: `/cmd-${index}`,
            title: `Command ${index}`,
            description: `Description ${index}`,
            icon: "activity",
          }))}
        />,
      );

      const input = screen.getByLabelText("Message input");
      fireEvent.change(input, { target: { value: "/" } });
      scrollIntoView.mockClear();

      fireEvent.keyDown(input, { key: "ArrowDown" });
      fireEvent.keyDown(input, { key: "ArrowDown" });

      expect(screen.getByRole("option", { name: /\/cmd-2/i })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "nearest" });
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it("opens the slash command palette downward when there is more room below", async () => {
    vi.spyOn(HTMLFormElement.prototype, "getBoundingClientRect").mockReturnValue(
      rect({ top: 40, bottom: 160, width: 800, height: 120 }),
    );
    Object.defineProperty(window, "innerHeight", {
      value: 330,
      configurable: true,
    });
    render(
      <ThreadComposer
        onSend={vi.fn()}
        placeholder="Ask anything..."
        slashCommands={COMMANDS}
        variant="hero"
      />,
    );
    const input = screen.getByLabelText("Message input");

    fireEvent.change(input, { target: { value: "/" } });

    await waitFor(() => {
      const palette = screen.getByRole("listbox", { name: "Slash commands" });
      expect(palette.className).toContain("top-full");
      expect(palette).toHaveStyle({ maxHeight: "162px" });
    });
  });

  it("dismisses the slash command palette on outside click", () => {
    render(
      <div>
        <button type="button">outside</button>
        <ThreadComposer
          onSend={vi.fn()}
          placeholder="Type your message..."
          slashCommands={COMMANDS}
        />
      </div>,
    );

    fireEvent.change(screen.getByLabelText("Message input"), {
      target: { value: "/" },
    });
    expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();

    fireEvent.pointerDown(screen.getByRole("button", { name: "outside" }));

    expect(screen.queryByRole("listbox", { name: "Slash commands" })).not.toBeInTheDocument();
  });

  it("shows a stop button while streaming", () => {
    const onStop = vi.fn();
    render(
      <ThreadComposer
        onSend={vi.fn()}
        onStop={onStop}
        isStreaming
        placeholder="Type your message..."
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop response" }));

    expect(onStop).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Send message" })).not.toBeInTheDocument();
  });

  it("enters backend screenshot mode when onCaptureScreen resolves with a URL", async () => {
    const onCaptureScreen = vi.fn().mockResolvedValue("blob:screenshot-1");
    render(
      <ThreadComposer onSend={vi.fn()} onCaptureScreen={onCaptureScreen} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Take a screenshot" }));

    await waitFor(() => {
      expect(onCaptureScreen).toHaveBeenCalledTimes(1);
    });
    const dialog = await screen.findByRole("dialog");
    // 后端截图模式:overlay 渲染静态 img,而不是 getDisplayMedia 的 video。
    const img = dialog.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe("blob:screenshot-1");
    expect(dialog.querySelector("video")).toBeNull();
  });

  it("falls back to the capture stream when onCaptureScreen resolves null", async () => {
    const onCaptureScreen = vi.fn().mockResolvedValue(null);
    render(
      <ThreadComposer onSend={vi.fn()} onCaptureScreen={onCaptureScreen} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Take a screenshot" }));

    await waitFor(() => {
      expect(onCaptureScreen).toHaveBeenCalledTimes(1);
    });
    const dialog = await screen.findByRole("dialog");
    // 回退模式:无后端截图 img,等待 getDisplayMedia 流。
    expect(dialog.querySelector("img")).toBeNull();
  });

  it("always shows the @ subagent button even without configured agents", () => {
    render(<ThreadComposer onSend={vi.fn()} />);

    expect(
      screen.getByRole("button", { name: "Agents" }),
    ).toBeInTheDocument();
  });

  it("opens the @ menu and selects a subagent", async () => {
    const onSelectAgent = vi.fn();
    render(
      <ThreadComposer
        onSend={vi.fn()}
        agents={[
          {
            name: "researcher",
            description: "Runs research",
            model: null,
            tools: null,
            avatar: null,
            system_prompt: "",
            path: null,
          },
        ]}
        onSelectAgent={onSelectAgent}
      />,
    );

    // Radix DropdownMenu 在 jsdom 中以 pointerDown 打开
    fireEvent.pointerDown(screen.getByRole("button", { name: "Agents" }));

    const item = await screen.findByRole("menuitem", { name: /researcher/ });
    fireEvent.click(item);

    expect(onSelectAgent).toHaveBeenCalledWith("researcher");
  });
});
