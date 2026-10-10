import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

/**
 * ADR-0027 §7: layer adapters place stored (date, value) pairs and nothing else. They
 * never read stored array positions or slopes (that would rebuild coordinates from
 * indices), and never scale, average or otherwise compute with numbers.
 */
const layerAdapterRules = {
  // The current-state view and the history scope select stored objects exactly as the
  // layers do (Issues 1 and 2).
  files: [
    "src/lib/layers/**/*.ts",
    "src/lib/current-state.ts",
    "src/components/CurrentState.tsx",
    "src/components/HistoryScope.tsx",
  ],
  ignores: ["src/lib/layers/**/*.test.ts"],
  rules: {
    "no-restricted-syntax": [
      "error",
      {
        selector: "MemberExpression[property.name=/(_index|slope_per_bar|anchor_value|anchor_1_price)$/]",
        message: "Layer adapters never place anything by a stored array position or slope (ADR-0027 §2).",
      },
      {
        selector: "BinaryExpression[operator=/^(\\*|\\/|\\*\\*|%)$/]",
        message: "Layer adapters never compute with values (ADR-0027 §1).",
      },
      {
        selector: "MemberExpression[object.name='Math']",
        message: "Layer adapters never compute with values (ADR-0027 §1).",
      },
    ],
  },
};

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  layerAdapterRules,
  globalIgnores([".next/**", "out/**", "next-env.d.ts"]),
]);
