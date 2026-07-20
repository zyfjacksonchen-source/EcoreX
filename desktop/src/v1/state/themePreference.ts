export type ThemePreference = "dark" | "light";

export const THEME_PREFERENCE_KEY = "ecorex-theme";

/** New installations deliberately start dark; only an explicit saved light choice overrides it. */
export function resolveThemePreference(value: string | null): ThemePreference {
  return value === "light" ? "light" : "dark";
}
