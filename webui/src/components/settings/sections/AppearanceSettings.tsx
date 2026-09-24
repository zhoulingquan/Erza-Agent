// Appearance section:主题、语言、本地偏好(density/activity/codeWrap)。
// 从 SettingsView.tsx 拆分而来。

import type { Dispatch, SetStateAction } from "react";
import { useTranslation } from "react-i18next";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { cn } from "@/lib/utils";
import type { ThemeMode } from "@/hooks/useTheme";

import {
  type LocalActivityMode,
  type LocalDensity,
  type LocalPreferences,
} from "../types";
import {
  SegmentedControl,
  ToggleButton,
} from "../components/SegmentedControl";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "../components/SettingsRow";
import { WallpaperSettings } from "./WallpaperSettings";

export function AppearanceSettings({
  themeMode,
  onSetThemeMode,
  localPrefs,
  onChangeLocalPrefs,
}: {
  themeMode: ThemeMode;
  onSetThemeMode: (mode: ThemeMode) => void;
  localPrefs: LocalPreferences;
  onChangeLocalPrefs: Dispatch<SetStateAction<LocalPreferences>>;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const themeOptions: Array<{ value: ThemeMode; label: string }> = [
    { value: "light", label: t("settings.values.light") },
    { value: "dark", label: t("settings.values.dark") },
    { value: "system", label: tx("settings.values.system", "System") },
  ];
  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{t("settings.sections.interface")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.rows.theme")}
            description={t("settings.help.theme")}
          >
            <div className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground">
              {themeOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => onSetThemeMode(option.value)}
                  className={cn(
                    "rounded-full px-3 py-1 transition-colors",
                    themeMode === option.value && "bg-background text-foreground shadow-sm",
                  )}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </SettingsRow>

          <SettingsRow
            title={t("settings.rows.language")}
            description={t("settings.help.language")}
          >
            <LanguageSwitcher />
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.localPreferences", "Local preferences")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.density", "Density")}
            description={tx("settings.help.density", "Stored only in this browser.")}
          >
            <SegmentedControl
              value={localPrefs.density}
              options={[
                { value: "comfortable", label: tx("settings.values.comfortable", "Comfortable") },
                { value: "compact", label: tx("settings.values.compact", "Compact") },
              ]}
              onChange={(density) =>
                onChangeLocalPrefs((prev) => ({ ...prev, density: density as LocalDensity }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.activityMode", "Activity detail")}
            description={tx("settings.help.activityMode", "Choose how much agent activity chrome to show by default.")}
          >
            <SegmentedControl
              value={localPrefs.activityMode}
              options={[
                { value: "auto", label: tx("settings.values.auto", "Auto") },
                { value: "expanded", label: tx("settings.values.expanded", "Expanded") },
              ]}
              onChange={(activityMode) =>
                onChangeLocalPrefs((prev) => ({ ...prev, activityMode: activityMode as LocalActivityMode }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.codeWrap", "Code wrapping")}
            description={tx("settings.help.codeWrap", "Keep long code lines readable on smaller screens.")}
          >
            <ToggleButton
              checked={localPrefs.codeWrap}
              onChange={(codeWrap) => onChangeLocalPrefs((prev) => ({ ...prev, codeWrap }))}
              ariaLabel={tx("settings.rows.codeWrap", "Code wrapping")}
              label={localPrefs.codeWrap ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      <WallpaperSettings />
    </div>
  );
}
