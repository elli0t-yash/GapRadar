import { useSyncExternalStore } from "react";
import { getTheme, subscribeToTheme, type Theme } from "../theme";

/** Subscribes a component to the app theme. */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribeToTheme, getTheme, getTheme);
}
