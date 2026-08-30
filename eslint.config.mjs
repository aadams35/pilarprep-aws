import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import hooks from "eslint-plugin-react-hooks";
import globals from "globals";
import ts from "typescript-eslint";

export default defineConfig([
  globalIgnores(["node_modules/**", "dist/**", "work/**", "playwright-report/**", "test-results/**", "coverage/**", ".venv/**"]),
  js.configs.recommended,
  ts.configs.recommended,
  { languageOptions: { globals: { ...globals.browser, ...globals.node } } },
  {
    files: ["**/*.{ts,tsx}"],
    plugins: { "react-hooks": hooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" }],
    },
  },
]);
